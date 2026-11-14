"""Bot operations API (Step 7), mounted by app.py under /api/bot.

Single-writer rule: when a bot loop holds data/live_bot.lock it is the only writer of data/bot_state.db,
so pause / resume / flatten / stop are delivered as control files (live_bot.ControlChannel) and executed
by that process. With no runner active, the API performs the action itself (taking the same lock).
Status is read through a read-only SQLite connection (WAL allows it while the bot writes).
"""

from __future__ import annotations

import json
from functools import wraps
from contextlib import contextmanager
from dataclasses import asdict
import math
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import backup_state
import paper_accounts
from dataset_lock import dataset_lock
import data_manager as dm
import run_store
import execution_engine as ee
import live_bot as lb
from config import PROJECT_ROOT, settings
from exchange_client import DeltaRestClientAdapter, ExchangeError
from notifier import TelegramNotifier

router = APIRouter(prefix="/api/bot", tags=["bot"])

PATHS: dict[str, Path] = {
    "db": settings.data_path / settings.BOT_DB_FILE,
    "lock": lb.LOCK_PATH,
    "control": lb.CONTROL_DIR,
    "backups": backup_state.BACKUP_DIR,
    "logs": settings.data_path / "logs",
    "manual_mode": settings.data_path / "manual_mode.json",
}
EXEC_MODES = ("paper", "testnet", "live")
_proc: subprocess.Popen | None = None
_proc_log: Path | None = None
_proc_bot_pid: int | None = None  # pid holding the lock (the venv launcher spawns the real interpreter as a child)
_op_lock = threading.RLock()
_stopping = threading.Event()
_runner_probe_lock = threading.Lock()
_notifiers: dict[str, TelegramNotifier] = {}
_cache: dict[str, Any] = {}


@contextmanager
def _startup_operation():
    if not _op_lock.acquire(blocking=False):
        raise HTTPException(409, 'Another bot/account operation is in progress. Retry after it finishes.')
    try:
        yield
    finally:
        _op_lock.release()


def configure(**paths: Path) -> None:
    """Point the API at other files (tests)."""
    for k, v in paths.items():
        if k not in PATHS:
            raise KeyError(k)
        PATHS[k] = Path(v)
    if "db" in paths and "manual_mode" not in paths:  # an isolated journal gets an isolated mode file too
        PATHS["manual_mode"] = Path(paths["db"]).parent / "manual_mode.json"
    _cache.clear()


def mode_db(mode: str) -> Path:
    """Per-mode journal next to PATHS['db'] (the paper journal): bot_state.db / bot_state_testnet.db / bot_state_live.db."""
    if mode not in EXEC_MODES:
        raise ValueError(mode)
    base = PATHS["db"]
    return base if mode == "paper" else base.with_name(f"{base.stem}_{mode}{base.suffix}")


def mode_state() -> tuple[str, int]:
    """Manual Trade's selected mode (persisted; paper unless switched) and its monotonic mode_generation."""
    f = PATHS["manual_mode"]
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "paper", 0
    m = d.get("mode", "paper")
    return (m if m in EXEC_MODES else "paper"), int(d.get("mode_generation") or 0)


def active_mode() -> str:
    return mode_state()[0]


def set_active_mode(mode: str) -> int:
    """Persist ``mode`` and bump mode_generation (every accepted switch invalidates tickets bound to the old one)."""
    f = PATHS["manual_mode"]
    f.parent.mkdir(parents=True, exist_ok=True)
    gen = mode_state()[1] + 1
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"mode": mode, "mode_generation": gen, "at": time.time()}), encoding="utf-8")
    os.replace(tmp, f)
    return gen


def status_mode() -> str:
    """Default Bot Operations journal when no mode parameter is supplied: running loop's mode, else paper."""
    run, info = runner()
    if run and (info or {}).get("mode") in EXEC_MODES:
        return info["mode"]
    return "paper"


def notifier(mode: str | None = None) -> TelegramNotifier:
    """One notifier per execution mode, so alerts carry [PAPER] / [TESTNET] / [LIVE] (default: the shown journal's mode)."""
    m = mode if mode in EXEC_MODES else status_mode()
    if m not in _notifiers:
        _notifiers[m] = TelegramNotifier.from_settings(m)
    return _notifiers[m]


def _clean(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return None if not math.isfinite(float(o)) else float(o)
    if isinstance(o, pd.Timestamp):
        return o.isoformat()
    if isinstance(o, sqlite3.Row):
        return _clean(dict(o))
    return o


def us_iso(us: int | None) -> str | None:
    return None if us is None else pd.Timestamp(int(us), unit="us", tz="UTC").isoformat()


# --------------------------------------------------------------------------- #
# Runner detection / read-only journal access
# --------------------------------------------------------------------------- #
def runner() -> tuple[bool, dict[str, Any] | None]:
    # Concurrent probes briefly acquire the same OS byte lock. Serialize them
    # so one probe cannot mistake another probe for an active runner.
    with _runner_probe_lock:
        held = lb.InstanceLock.is_held(PATHS["lock"])
        return held, (lb.InstanceLock.read_info(PATHS["lock"]) if held else None)


def _pid_alive(pid: int) -> bool:
    if sys.platform != 'win32':
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE; no terminate rights
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # no such PID
            return False
        raise OSError(error, 'Cannot verify runner process termination')
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        if result not in (0, 258):
            raise OSError('Cannot verify runner process termination')
        return result == 258
    finally:
        kernel.CloseHandle(handle)


def process_state() -> dict[str, Any]:
    """Process evidence only: never infer exit from a halt or a stop acknowledgement."""
    try:
        held, info = runner()
        if held:
            if (info or {}).get('mode') == 'paper-account-settings':
                return dict(state='unknown',running=None,pid=None,mode=None,
                            error='Paper account update in progress; waiting for lifecycle verification.')
            return dict(state='stopping' if _stopping.is_set() else 'running', running=True,
                        pid=(info or {}).get('pid'), mode=(info or {}).get('mode'), error=None)
        info = lb.InstanceLock.read_info(PATHS['lock']) or {}
        pid = info.get('pid') if info.get('mode') in EXEC_MODES else None
        # Account operations use the same lifecycle fence, but are not runners.
        if pid and pid != os.getpid() and _pid_alive(int(pid)):
            return dict(state='stopping', running=True, pid=pid, mode=info.get('mode'),
                        error='Runner lock released; waiting for process termination.')
        return dict(state='stopped', running=False, pid=None, mode=None, error=None)
    except Exception as exc:
        return dict(state='unknown', running=None, pid=None, mode=None,
                    error=f'Process verification failed: {exc}')


def ro_conn(mode: str | None = None, db_path: Path | None = None) -> sqlite3.Connection | None:
    db = db_path or mode_db(mode or status_mode())
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:  # a writer may have created the file but not the schema yet (events is the last table created)
        ready = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone()
    except sqlite3.DatabaseError as exc:
        con.close()
        raise HTTPException(503, f"Journal {db.name} could not be verified: {exc}") from exc
    if not ready:
        con.close()
        try:
            non_empty = db.stat().st_size > 0
        except OSError:
            non_empty = False
        if non_empty:
            raise HTTPException(503, f"Journal {db.name} is incomplete or unreadable.")
        return None
    return con


def _ticker(symbol: str) -> dict[str, Any] | None:
    key = f"tk:{symbol}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 5:
        return hit[1]
    try:
        tk = DeltaRestClientAdapter("paper").get_ticker(symbol)
    except Exception:  # noqa: BLE001 - status must render without the network
        tk = hit[1] if hit else None
    _cache[key] = (time.time(), tk)
    return tk


def _signals() -> dict[str, Any]:
    """State on the latest CLOSED bar of each deployed strategy's timeframe from local history (cached 60s)."""
    hit = _cache.get("signals")
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    out: dict[str, Any] = {}
    for sym, prof in ee.bot_profiles().items():
        meta = {
            "deployed_strategy": prof.custom_name or prof.variant or "-",
            "custom_name": prof.custom_name or prof.variant or "-",
            "timeframe": prof.timeframe or "1h", "leverage": prof.leverage, "risk_pct": prof.risk_pct,
            "sl_type": prof.sl_type, "sl_value": prof.sl_value, "tp_type": prof.tp_type, "tp_value": prof.tp_value,
            "sl_tp": f"{prof.sl_value:g}{'%' if prof.sl_type == 'fixed_pct' else 'x ATR'} / "
                     f"{prof.tp_value:g}{'R' if prof.tp_type == 'rr_ratio' else ('%' if prof.tp_type == 'fixed_pct' else 'x ATR')}",
            "enabled": prof.enabled,
        }
        try:
            hist = ee.load_history(sym)
            if hist is None or len(hist) == 0:
                out[sym] = {**meta, "status": "no_data", "signal": "FLAT", "signal_side": None, "close": 0.0}
                continue
            s = ee.evaluate_symbol(prof, hist)
            out[sym] = {k: s[k] for k in ("variant", "custom_name", "timeframe", "rule_type", "leverage", "risk_pct",
                                           "sl_type", "sl_value", "tp_type", "tp_value", "trail_mult",
                                           "bar_open", "bar_close", "last_1m_open", "close", "bb_upper", "bb_lower",
                                           "ema50_4h", "ema200_4h", "src_4h_open", "allow_long_4h", "allow_short_4h", "atr14",
                                           "atr_ratio", "gate_threshold", "cost_gate_pass", "cond_long", "cond_short",
                                           "signal_side", "fresh_signal")}
            out[sym]["trend_filter_used"] = prof.trend_filter_4h
            out[sym]["cost_gate_used"] = prof.cost_gate
            out[sym].update(meta)
        except Exception as exc:  # noqa: BLE001
            out[sym] = {**meta, "error": f"{type(exc).__name__}: {exc}"}
    _cache["signals"] = (time.time(), out)
    return out


def deployed_summary_view(sig_map: dict[str, Any] | None = None) -> dict[str, Any]:
    all_profs = ee.all_deployed_profiles()
    deployed_list: list[dict[str, Any]] = []
    for sym, prof in all_profs.items():
        sl_lbl = f"{prof.sl_value:g}{'%' if prof.sl_type == 'fixed_pct' else 'x ATR'}"
        tp_lbl = f"{prof.tp_value:g}{'R' if prof.tp_type == 'rr_ratio' else ('%' if prof.tp_type == 'fixed_pct' else 'x ATR')}"
        trail_lbl = f"{prof.trail_mult:g}x ATR" if prof.trailing_stop and prof.trail_mult > 0 else "Off"
        sig_info = (sig_map or {}).get(sym) or {}
        item = {
            "symbol": sym,
            "enabled": prof.enabled,
            "saved_result_id": prof.saved_result_id,
            "custom_name": prof.custom_name or prof.variant,
            "timeframe": prof.timeframe,
            "rule_type": prof.rule_type or prof.strategy,
            "params": prof.params or {},
            "capital_inr": prof.capital_inr,
            "leverage": prof.leverage,
            "risk_pct": prof.risk_pct,
            "sl_type": prof.sl_type,
            "sl_value": prof.sl_value,
            "tp_type": prof.tp_type,
            "tp_value": prof.tp_value,
            "trailing_stop": prof.trailing_stop,
            "trail_mult": prof.trail_mult,
            "sl_tp": f"{sl_lbl} / {tp_lbl}",
            "sl_tp_summary": f"SL {sl_lbl} · TP {tp_lbl} · Trail {trail_lbl}",
            "fee_mode": prof.fee_mode,
            "slippage_bps": prof.slippage_bps,
            "use_4h_trend": prof.trend_filter_4h,
            "use_cost_gate": prof.cost_gate,
            "deployed_at": prof.deployed_at,
            "last_signal": sig_info.get("signal_side") or "none",
            "last_bar_open": sig_info.get("bar_open"),
            "last_close": sig_info.get("close"),
        }
        deployed_list.append(item)
    active_items = [x for x in deployed_list if x["enabled"]]
    combined_risk_pct = round(sum(float(x["risk_pct"]) for x in active_items), 2)
    combined_risk_inr = round(sum(float(x["capital_inr"]) * float(x["risk_pct"]) / 100.0 for x in active_items), 2)
    return _clean({
        "deployed": deployed_list,
        "profiles": {x["symbol"]: x for x in deployed_list},
        "active_count": len(active_items),
        "total_count": len(deployed_list),
        "combined_risk_pct": combined_risk_pct,
        "combined_risk_inr": combined_risk_inr,
    })


def loss_streak(trades: list[sqlite3.Row], floor_us: int) -> int:
    n = 0
    for t in reversed(trades):
        if t["closed_at_us"] <= floor_us or t["net_pnl_inr"] >= 0:
            break
        n += 1
    return n


def read_status(with_market: bool = True, with_signals: bool = True, mode: str | None = None,
                paper_session_id: str | None = None, evaluation_offset: int = 0) -> dict[str, Any]:
    run, info = runner()
    proc = process_state()
    if proc.get("state") == "stopping" and not run:
        run = True
        info = info or {"pid": proc.get("pid"), "mode": proc.get("mode")}
    jmode = mode if mode in EXEC_MODES else status_mode()
    jdb = mode_db(jmode)
    archived = paper_accounts.archive(mode_db('paper'), paper_session_id) if paper_session_id else None
    if archived:
        jmode, jdb, run, info = 'paper', archived, False, None
        with_market = with_signals = False
    base: dict[str, Any] = {
        "process": proc,
        "runner": {"running": run, "pid": (info or {}).get("pid"), "mode": (info or {}).get("mode"),
                   "started": (info or {}).get("started"), "managed_by_dashboard": bool(_proc and _proc.poll() is None),
                   "log_file": str(_proc_log) if _proc_log else None},
        "settings": {"MODE": settings.MODE, "LIVE_TRADING_ENABLED": settings.LIVE_TRADING_ENABLED,
                     "live_allowed": settings.is_live and settings.AUTO_BOT_LIVE_ENABLED, "manual_live_allowed": settings.is_live,
                     "testnet_keys": bool(settings.TESTNET_API_KEY and settings.TESTNET_API_SECRET),
                     "telegram_configured": bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)},
        "db": str(jdb), "db_exists": jdb.exists(), "journal_mode": jmode,
        "paper_session_catalog": paper_accounts.describe(mode_db('paper')) if jmode=='paper' else None,
    }
    con = ro_conn(jmode, jdb)
    if con is None:
        cap = ee.CAPITAL_INR if jmode == "paper" else 0.0
        sigs = _signals() if with_market and with_signals else {}
        dep = deployed_summary_view(sigs)
        base.update(readiness_state="NO_JOURNAL", mode=jmode, entries_paused=False, daily_halted=False, loss_streak=0,
                    positions=[], intents=[], trades=[], events=[], transitions=[],
                    wallet={"initial_capital_inr": cap, "realized_equity_inr": cap, "unrealized_pnl_inr": 0.0,
                            "total_equity_inr": cap, "available_margin_inr": cap, "used_margin_inr": 0.0, "today_pnl_inr": 0.0,
                            "today_pnl_pct": 0.0, "day_start_equity_inr": cap, "daily_loss_limit_pct": ee.DAILY_LOSS_LIMIT_PCT,
                            "daily_loss_limit_inr": cap * ee.DAILY_LOSS_LIMIT_PCT / 100, "closed_trades": 0,
                            "closed_net_inr": 0.0, "usd_inr_rate": settings.FIXED_USD_INR_RATE},
                    signals=sigs,
                    deployed_strategies=dep["deployed"],
                    deployed_summary=dep)
        return _clean(_exchange_status(base, jmode) if jmode in ("live", "testnet") else base)
    try:
        cfg = con.execute("SELECT * FROM bot_config_state WHERE id=1").fetchone()
        mode = cfg["mode"] if cfg else jmode
        rate = float(cfg["usd_inr_rate"]) if cfg else settings.FIXED_USD_INR_RATE
        capital = (float(cfg["capital_inr"]) if cfg else ee.CAPITAL_INR) if jmode == "paper" else 0.0
        trades = con.execute("SELECT * FROM trade_history WHERE mode=? ORDER BY trade_id", (mode,)).fetchall()
        positions = con.execute("SELECT * FROM open_positions WHERE mode=? ORDER BY symbol", (mode,)).fetchall()
        intents = con.execute("SELECT * FROM order_intents WHERE mode=? ORDER BY created_at_us DESC LIMIT 30", (mode,)).fetchall()
        events = con.execute("SELECT * FROM events WHERE mode=? OR mode IS NULL ORDER BY id DESC LIMIT 80", (mode,)).fetchall()
        started = con.execute("SELECT max(id) FROM events WHERE kind='runner_started' AND mode=?", (mode,)).fetchone()[0] or 0
        since = started if run else 0  # a running loop only reports state it wrote itself
        last_state = con.execute("SELECT * FROM events WHERE kind='state' AND mode=? AND id>? ORDER BY id DESC LIMIT 1",
                                 (mode, since)).fetchone()
        last_ready = con.execute("SELECT * FROM events WHERE kind='readiness' AND mode=? AND id>? ORDER BY id DESC LIMIT 1",
                                 (mode, since)).fetchone()
        if last_ready is None and run:
            last_ready = con.execute("SELECT * FROM events WHERE kind='readiness' AND mode=? AND id<=? ORDER BY id DESC LIMIT 1",
                                     (mode, since)).fetchone()
            last_ready_stale = True
        else:
            last_ready_stale = False
        floor = con.execute("SELECT max(at_us) FROM events WHERE kind='loss_streak_halt' AND mode=?", (mode,)).fetchone()[0] or 0
        unknown = con.execute("SELECT count(*) FROM order_intents WHERE mode=? AND state='OUTCOME_UNKNOWN'", (mode,)).fetchone()[0]
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        evaluations = con.execute(
            "SELECT * FROM signal_evaluations WHERE id IN (SELECT max(id) FROM signal_evaluations WHERE mode=? GROUP BY symbol)",
            (mode,)).fetchall() if 'signal_evaluations' in tables else []
        accounting = con.execute("SELECT * FROM funding_settlements WHERE mode=? AND status='PENDING' ORDER BY at_us",
                                 (mode,)).fetchall() if 'funding_settlements' in tables else []
        session_start = con.execute("SELECT at_us FROM events WHERE id=?", (started,)).fetchone()
        evaluation_history = con.execute('SELECT * FROM signal_evaluations WHERE mode=? ORDER BY id DESC LIMIT 101 OFFSET ?',
                                         (mode,evaluation_offset)).fetchall() if 'signal_evaluations' in tables else []
        funding_history = con.execute('SELECT * FROM funding_events WHERE mode=? ORDER BY at_us DESC',(mode,)).fetchall()
        settlement_history = con.execute('SELECT * FROM funding_settlements WHERE mode=? ORDER BY at_us DESC',(mode,)).fetchall() if 'funding_settlements' in tables else []
    finally:
        con.close()
    now_us = int(time.time() * 1e6)
    closed = sum(float(t["net_pnl_inr"]) for t in trades)
    open_adj = sum((float(p["realized_funding_usd"]) - float(p["entry_fee_usd"])) * rate for p in positions)
    realized = capital + closed + open_adj
    pos_out, unreal = [], 0.0
    for p in positions:
        d = 1 if p["side"] == "LONG" else -1
        tk = _ticker(p["symbol"]) if with_market else None
        mark = (tk or {}).get("mark_price")
        u = d * p["contracts"] * p["contract_value"] * (mark - p["entry_price"]) * rate if mark else None
        unreal += u or 0.0
        pd_ = dict(p)
        lev = float(pd_.get("leverage") or ee.LEVERAGE)  # journals not yet migrated have no leverage column
        try:
            mm = float(dm.load_metadata(p["symbol"]).get("maintenance_margin") or 0.5)
        except Exception:  # noqa: BLE001
            mm = 0.5
        margin_inr = p["margin_usd"] * rate
        pos_out.append({**pd_, "leverage": lev, "source": pd_.get("source", "bot"), "mark_price": mark, "unrealized_pnl_inr": u,
                        "roe_pct": (u / margin_inr * 100) if (u is not None and margin_inr) else None,
                        "liq_price_est": ee.liquidation_price(p["entry_price"], p["side"], lev, mm),
                        "opened_at": us_iso(p["opened_at_us"]),
                        "notional_inr": p["contracts"] * p["contract_value"] * (mark or p["entry_price"]) * rate,
                        "margin_inr": margin_inr})
    midnight_us = (now_us // 86_400_000_000) * 86_400_000_000
    today_closed = sum(float(t["net_pnl_inr"]) for t in trades if t["closed_at_us"] >= midnight_us)
    day_start = realized - today_closed
    total = realized + unreal
    account = (paper_accounts.describe(jdb) if archived else base['paper_session_catalog']) if mode=='paper' else None
    if account:
        day_start = account['daily_baseline_inr']
    halted = bool(cfg and cfg["halted"] and (archived or cfg["halt_until_us"] is None or now_us < cfg["halt_until_us"]))
    rdet = json.loads(last_ready["detail"]) if last_ready and last_ready["detail"] else {}
    sdet = json.loads(last_state["detail"]) if last_state and last_state["detail"] else {}
    last_known = sdet.get("to")
    base.update(
        mode=mode,
        readiness_state='ARCHIVED' if archived else ((last_known or "BOOTING") if run else "STOPPED"),
        last_known_state=last_known,
        entries_paused=bool(cfg["entries_paused"]) if cfg and "entries_paused" in cfg.keys() else False,
        daily_halted=halted, halt_reason=cfg["halt_reason"] if halted else None,
        halt_until=us_iso(cfg["halt_until_us"]) if halted and cfg["halt_until_us"] else None,
        loss_streak=loss_streak(trades, int(floor)), max_consecutive_losses=lb.MAX_CONSECUTIVE_LOSSES,
        ws_generation=None if last_ready_stale else rdet.get("ws_generation"),
        last_reconciled_at=us_iso(last_ready["at_us"]) if last_ready else None,
        last_reconcile_issues=rdet.get("issues"), outcome_unknown_intents=unknown,
        wallet={"initial_capital_inr": capital, "realized_equity_inr": realized, "unrealized_pnl_inr": unreal,
                "total_equity_inr": total, "available_margin_inr": realized - sum(p["margin_usd"] for p in positions) * rate,
                "used_margin_inr": sum(p["margin_usd"] for p in positions) * rate,
                "today_pnl_inr": total - day_start, "today_pnl_pct": (total / day_start - 1) * 100 if day_start else 0.0,
                "day_start_equity_inr": day_start, "daily_loss_limit_pct": cfg['daily_loss_limit_pct'] if cfg else ee.DAILY_LOSS_LIMIT_PCT,
                "daily_loss_limit_inr": day_start * (cfg['daily_loss_limit_pct'] if cfg else ee.DAILY_LOSS_LIMIT_PCT) / 100, "closed_trades": len(trades),
                "closed_net_inr": closed, "usd_inr_rate": rate},
        positions=pos_out,
        intents=[{**dict(i), "created_at": us_iso(i["created_at_us"]), "updated_at": us_iso(i["updated_at_us"]),
                  "alert": i["state"] == "OUTCOME_UNKNOWN", "raw_response": None} for i in intents],
        trades=[{**dict(t), "opened_at": us_iso(t["opened_at_us"]), "closed_at": us_iso(t["closed_at_us"])} for t in reversed(trades[-50:])],
        transitions=[{"at": us_iso(e["at_us"]), **json.loads(e["detail"] or "{}")} for e in events if e["kind"] == "state"][:25],
        events=[{"id": e["id"], "at": us_iso(e["at_us"]), "level": e["level"], "kind": e["kind"], "symbol": e["symbol"],
                 "detail": e["detail"]} for e in events if e["kind"] != "state"][:40],
        signals=(sigs := _signals() if with_market and with_signals else {}),
        paper_account=account,
        historical_session=bool(archived),
        selected_paper_session_id=paper_session_id or (account or {}).get('session_id'),
        evaluation_history=[{**dict(e),'bar_close':us_iso(e['bar_close_us']),
                             'evaluated_at':us_iso(e['evaluated_at_us'])} for e in evaluation_history[:100]],
        evaluation_offset=evaluation_offset, more_evaluations=len(evaluation_history)>100,
        funding_history=[dict(r) for r in funding_history],
        settlement_history=[dict(r) for r in settlement_history],
        runner_evaluations={e['symbol']: {**dict(e), 'detail': json.loads(e['detail']),
                            'evaluated_at': us_iso(e['evaluated_at_us']), 'bar_close': us_iso(e['bar_close_us']),
                            'from_current_run': bool(session_start and e['evaluated_at_us'] >= session_start[0]),
                            'source': 'runner journal'} for e in evaluations},
        accounting_status='PENDING' if accounting else 'RECONCILED',
        pending_funding=[dict(a) for a in accounting],
        wallet_provisional=bool(accounting),
        deployed_strategies=(dep := deployed_summary_view(sigs) if not archived else {'deployed':{}})["deployed"],
        deployed_summary=dep,
    )
    return _clean(_exchange_status(base, mode) if mode in ("live", "testnet") else base)


def _exchange_status(base: dict[str, Any], mode: str) -> dict[str, Any]:
    """Exchange GETs supply dashboard balances/positions, never reconstructed paper equity."""
    import manual_trade as manual  # lazy: this router is also used by the manual terminal
    try:
        snap = manual.exchange_snapshot(mode)
        if pd.Timestamp(snap["at"], unit="s", tz="UTC").date() != pd.Timestamp.now(tz="UTC").date():
            snap = manual.exchange_snapshot(mode, ttl=0)
        wallet = snap["wallet"]
        positions = manual._exchange_positions_view(mode, base.get("positions", []), snap)
        # The stopped dashboard can persist observations. A running engine owns
        # its StateStore writer fence and updates the same snapshot/anchor itself.
        with _op_lock:
            if not runner()[0]:
                store = ee.StateStore(mode_db(mode))
                try:
                    store.init_config(mode, ee.bot_profiles())
                    store.record_exchange_wallet(mode, wallet, int(snap["at"] * 1_000_000))
                finally:
                    store.close()
        con = ro_conn(mode)
        try:
            meta = {r[0]: r[1] for r in con.execute("SELECT key,value FROM system_meta")} if con else {}
        finally:
            if con is not None:
                con.close()
        date = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
        anchor = (float(meta["daily_start_equity_inr"]) if meta.get("utc_day_anchor_date") == date
                  and meta.get("utc_day_anchor_source") == f"delta_wallet:{mode}" else None)
        equity, available, used = (wallet[k] for k in ("wallet_equity_inr", "available_margin_inr", "used_margin_inr"))
        unreal = sum(p.get("unrealized_pnl_inr") or 0 for p in positions)
        old = base["wallet"]
        daily_pct = old.get("daily_loss_limit_pct", settings.MAX_DAILY_LOSS_PCT)
        today = equity - anchor if anchor is not None else None
        base["wallet"] = {**old, **wallet, "initial_capital_inr": None, "realized_equity_inr": equity,
                          "total_equity_inr": equity + unreal, "unrealized_pnl_inr": unreal,
                          "available_margin_inr": available, "used_margin_inr": used,
                          "day_start_equity_inr": anchor, "daily_start_equity_inr": anchor,
                          "today_pnl_inr": today, "today_pnl_pct": today / anchor * 100 if anchor else None,
                          "daily_loss_limit_inr": anchor * daily_pct / 100 if anchor is not None else None,
                          "baseline_basis": "First observed Delta wallet balance of the UTC day; includes wallet transfers",
                          "baseline_date_utc": meta.get("utc_day_anchor_date"),
                          "baseline_observed_at": us_iso(int(meta["utc_day_anchor_at_us"])) if meta.get("utc_day_anchor_at_us") else None,
                          "source": "delta", "observed_at": us_iso(int(snap["at"] * 1_000_000))}
        base["positions"] = positions
        base["exchange"] = {"connected": True, "snapshot_age_s": time.time() - snap["at"]}
        # A live deployment's rupee risk summary uses this wallet, while retaining
        # the saved backtest capital as explicitly labelled reference metadata.
        dep = base.get("deployed_summary", {})
        for profile in dep.get("deployed", []):
            profile.update(backtest_capital_inr=profile["capital_inr"], capital_inr=equity,
                           sizing_source="delta_wallet", runtime_fee_mode="taker",
                           risk_budget_inr=equity * profile["risk_pct"] / 100)
        dep["combined_risk_inr"] = sum(p["risk_budget_inr"] for p in dep.get("deployed", []) if p["enabled"])
    except (ExchangeError, httpx.HTTPError, HTTPException) as exc:
        base["exchange"] = {"connected": False, "error": str(getattr(exc, "detail", exc))}
        base["wallet"] = {**base["wallet"], **{key: None for key in (
            "initial_capital_inr", "wallet_equity_inr", "realized_equity_inr", "total_equity_inr", "unrealized_pnl_inr",
            "available_margin_inr", "used_margin_inr", "today_pnl_inr", "today_pnl_pct", "day_start_equity_inr", "daily_loss_limit_inr")},
            "source": "delta", "error": base["exchange"]["error"]}
        base["positions"] = []
    return base


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get("/status")
def bot_status(paper_session_id: str | None = None, evaluation_offset: int = 0,
               mode: Literal['paper','testnet','live'] | None = None) -> dict[str, Any]:
    try:
        if evaluation_offset < 0:
            raise HTTPException(422, 'Evaluation offset must be nonnegative.')
        return read_status(mode=mode, paper_session_id=paper_session_id, evaluation_offset=evaluation_offset)
    except paper_accounts.AccountError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


class PaperAccountRequest(BaseModel):
    daily_loss_limit_pct: float = Field(ge=.5, le=95, allow_inf_nan=False)
    mode: Literal['paper','live','testnet'] = 'paper'
    expected_session_id: str
    expected_revision: int = Field(ge=0)
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,100}$')


class NewPaperSessionRequest(PaperAccountRequest):
    capital_inr: float
    confirmed_equity_inr: float


@router.get('/paper/sessions')
def paper_sessions(paper_session_id: str | None = None) -> dict[str, Any]:
    try:
        catalog = paper_accounts.describe(mode_db('paper'))
        archived = paper_accounts.archive(mode_db('paper'),paper_session_id) if paper_session_id else None
        return _clean({**catalog, 'paper_account':paper_accounts.describe(archived) if archived else catalog,
                       'historical_session':bool(archived)})
    except paper_accounts.AccountError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


@router.get('/process')
def bot_process() -> dict[str, Any]:
    return {**process_state(), 'checked_at':pd.Timestamp.now(tz='UTC').isoformat()}


@router.get('/settings')
@router.get('/paper/settings')
def account_settings(mode: Literal['paper','live','testnet'] = 'paper') -> dict[str, Any]:
    try:
        status = (read_status(mode=mode, with_market=False, with_signals=False)
                  if mode!='paper' and not paper_accounts.pending_path(mode_db(mode)).exists() else None)
        if status and not status.get('exchange', {}).get('connected'):
            raise HTTPException(503, status.get('exchange', {}).get('error', 'Delta wallet unavailable'))
        account = paper_accounts.describe(mode_db(mode), mode)
        if status:
            account.update(equity_inr=status['wallet']['wallet_equity_inr'],
                           positions=len(status['positions']))
        return _clean({**account, 'account_settings':account, 'paper_account':account if mode=='paper' else None,
                       'historical_session':False})
    except paper_accounts.AccountError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


def _paper_account_operation(operation: str, req: PaperAccountRequest) -> dict[str, Any]:
    with _op_lock:
        lock = None
        mode = req.mode
        body = req.model_dump(exclude_defaults=True)
        try:
            if operation=='daily_limit':
                try:
                    running, info = runner()
                except OSError as exc:
                    raise HTTPException(409, f'Cannot verify the journal writer; refresh process status: {exc}') from exc
                if running and (info or {}).get('mode')==mode:
                    result = _send('daily_loss_limit', 30, body)
                    if not result.get('ok'):
                        raise HTTPException(int(result.get('status') or 503), result.get('error', 'Runner could not update the daily limit; retry the same request.'))
                    _cache.pop('signals',None)
                    return result
                if not running:
                    lock = lb.InstanceLock(PATHS['lock'], 'paper-account-settings')
                if mode!='paper' and not paper_accounts.pending_path(mode_db(mode)).exists():
                    import manual_trade as manual
                    wallet = manual.wallet_from_balances(manual.client_for(mode, read_only=True).get_wallet_balances())
                    store = ee.StateStore(mode_db(mode))
                    try:
                        store.init_config(mode, {})
                        store.record_exchange_wallet(mode, wallet)
                    finally:
                        store.close()
                result = paper_accounts.apply(mode_db(mode), ee.BOT_PROFILES_FILE, PATHS['backups'], operation, body)
                _cache.pop('signals',None)
                return result
            if mode!='paper':
                raise HTTPException(422, 'Capital resets are paper-only.')
            process = process_state()
            if process['state'] != 'stopped':
                raise HTTPException(409, process['error'] or 'Stop Bot Process and wait for confirmed termination before changing Paper Account Settings.')
            prev_info = lb.InstanceLock.read_info(PATHS['lock']) or {}
            prev_pid = prev_info.get('pid') if prev_info.get('mode') in EXEC_MODES else None
            lock = lb.InstanceLock(PATHS['lock'], 'paper-account-settings')
            if prev_pid and int(prev_pid) != os.getpid() and _pid_alive(int(prev_pid)):
                raise HTTPException(409, 'Runner lock released; waiting for process termination.')
            result = paper_accounts.apply(mode_db('paper'), ee.BOT_PROFILES_FILE, PATHS['backups'],
                                          operation, body)
            _cache.pop('signals',None)
            return result
        except (lb.BotAlreadyRunningError, ee.StateStoreLockedError) as exc:
            raise HTTPException(409, 'The account journal writer changed or is busy. Refresh and retry the same settings request.') from exc
        except paper_accounts.AccountError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        except HTTPException:
            raise
        except Exception as exc:
            if paper_accounts.pending_path(mode_db(mode)).exists():
                raise HTTPException(503, 'Account settings operation is pending recovery. Retry the same operation in Account Settings. Startup remains blocked.') from exc
            raise HTTPException(422, f'Account settings operation failed before applying settings: {exc}') from exc
        finally:
            if lock is not None:
                lock.release()


@router.put('/paper/account/daily-loss')
@router.put('/settings')
@router.put('/paper/settings')
def update_paper_daily_loss(req: PaperAccountRequest) -> dict[str, Any]:
    return _paper_account_operation('daily_limit',req)


@router.post('/paper/sessions')
def new_paper_session(req: NewPaperSessionRequest) -> dict[str, Any]:
    return _paper_account_operation('new_session',req)


SAVED_BACKTESTS_FILE = settings.data_path / "saved_backtests.json"


def _load_saved_results_for_deploy() -> list[dict[str, Any]]:
    if not SAVED_BACKTESTS_FILE.exists():
        return []
    try:
        raw = json.loads(SAVED_BACKTESTS_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except Exception:  # noqa: BLE001
        return []


class DeployStrategyRequest(BaseModel):
    mode: Literal['paper','testnet','live'] | None = None
    result_id: str | None = None
    saved_result_id: str | None = None
    id: str | None = None
    result_ids: list[str] | None = None
    symbol: str | None = None
    custom_name: str | None = None
    strategy_name: str | None = None
    timeframe: str | None = None
    rule_type: str | None = None
    params: dict[str, Any] | None = None
    capital_inr: float | None = None
    leverage: float | None = None
    risk_pct: float | None = None
    sl_type: str | None = None
    sl_value: float | None = None
    tp_type: str | None = None
    tp_value: float | None = None
    trailing_stop: bool | None = None
    trail_mult: float | None = None
    fee_mode: str | None = None
    slippage_bps: float | None = None
    use_4h_trend: bool | None = None
    use_cost_gate: bool | None = None
    enabled: bool = True


def _build_deploy_entry(saved: dict[str, Any] | None, req: DeployStrategyRequest) -> dict[str, Any]:
    src = dict(saved or {})
    verified = bool(saved and saved.get("run_id") and saved.get("verified_run"))
    if verified:
        # Reload the content-addressed server result; editable library summaries are not authority.
        try:
            evidence = run_store.get(saved["run_id"])
        except (ValueError, OSError, EOFError) as exc:
            raise HTTPException(422, f"deployment evidence unavailable: {exc}") from exc
        r = evidence["result"]
        if not r["audit"]["passed"] or len(r["legs"]) != 1:
            raise HTTPException(422, "deployment requires a passing single-symbol run")
        leg = r["legs"][0]
        cfg = evidence["leg_configs"][leg["symbol"]]
        c = r["config"]
        # Overrides require a new backtest, otherwise the displayed result would no longer apply.
        changed = set(req.model_fields_set) - {"result_id", "saved_result_id", "id", "enabled", "custom_name", "strategy_name"}
        if changed:
            raise HTTPException(422, "rerun the backtest to change execution settings before deployment")
        src.update(symbol=leg["symbol"], timeframe=leg["timeframe"], rule_type=c.get("rule_type") or leg["strategy"],
                   params=c.get("params", {}), capital_inr=cfg["initial_capital_inr"], leverage=cfg["leverage"],
                   risk_pct=cfg["risk_per_trade_pct"], sl_type=c["sl_type"], sl_value=c["sl_value"],
                   tp_type=c["tp_type"], tp_value=c["tp_value"], trail_mult=cfg["trailing_atr_mult"],
                   trailing_stop=cfg["trailing_atr_mult"] > 0 or cfg["trailing_sl_pct"] > 0,
                   fee_mode=cfg["fee_mode"], slippage_bps=cfg["slippage_bps"],
                   use_4h_trend=r["filters"]["trend_filter_4h"], use_cost_gate=r["filters"]["cost_gate"],
                   strict_lot_cap=not cfg["allow_min_one_lot"], effective_config=cfg,
                   history_start=evidence["history_starts"][leg["symbol"]],
                   cooldown_bars=cfg["cooldown_bars"], max_daily_loss_pct=cfg["max_daily_loss_pct"],
                   trigger_source=cfg["trigger_source"], exit_on_opposite_signal=cfg["exit_on_opposite_signal"])
    sym = str(req.symbol or src.get("symbol") or "").strip().upper()
    if not sym:
        raise HTTPException(422, "symbol or valid result_id is required to deploy a strategy")
    rid = req.result_id or req.saved_result_id or req.id or src.get("id")
    cname = (req.custom_name or req.strategy_name or src.get("custom_name")
             or src.get("strategy_name") or f"{sym} Custom Strategy")
    tf = str(req.timeframe or src.get("timeframe") or "1h")
    rt = req.rule_type or src.get("rule_type") or "bollinger_volume_breakout"
    params = req.params if req.params is not None else (src.get("params") or {})
    cap = float(req.capital_inr if req.capital_inr is not None else (src.get("capital_inr") or 50000.0))
    lev = float(req.leverage if req.leverage is not None else (src.get("leverage") or 10.0))
    risk = float(req.risk_pct if req.risk_pct is not None else (src.get("risk_pct") or 2.0))
    sl_type = str(req.sl_type or src.get("sl_type") or "atr_mult")
    sl_val = float(req.sl_value if req.sl_value is not None else (src.get("sl_value") or 2.0))
    tp_type = str(req.tp_type or src.get("tp_type") or "atr_mult")
    tp_val = float(req.tp_value if req.tp_value is not None else (src.get("tp_value") or 5.0))
    trail_m = float(req.trail_mult if req.trail_mult is not None else (src.get("trail_mult") if src.get("trail_mult") is not None else 2.5))
    trail_on = bool(req.trailing_stop if req.trailing_stop is not None else (src.get("trailing_stop", trail_m > 0)))
    fee_mode = str(req.fee_mode or src.get("fee_mode") or "taker_maker_tp")
    slippage_bps = float(req.slippage_bps if req.slippage_bps is not None else (src.get("slippage_bps") or 0.0))
    use_4h = bool(req.use_4h_trend if req.use_4h_trend is not None else src.get("use_4h_trend", bool(params.get("use_4h_trend", False))))
    use_gate = bool(req.use_cost_gate if req.use_cost_gate is not None else src.get("use_cost_gate", False))
    entry = {
        "enabled": bool(req.enabled),
        "saved_result_id": rid,
        "custom_name": cname,
        "symbol": sym,
        "timeframe": tf,
        "rule_type": rt,
        "params": params,
        "capital_inr": cap,
        "leverage": lev,
        "risk_pct": risk,
        "sl_type": sl_type,
        "sl_value": sl_val,
        "tp_type": tp_type,
        "tp_value": tp_val,
        "trailing_stop": trail_on,
        "trail_mult": trail_m if trail_on else 0.0,
        "fee_mode": fee_mode,
        "slippage_bps": slippage_bps,
        "use_4h_trend": use_4h,
        "use_cost_gate": use_gate,
        "deployed_at": pd.Timestamp.now(tz="UTC").isoformat(),
        **{k: src[k] for k in ("effective_config", "history_start", "cooldown_bars", "max_daily_loss_pct",
                               "trigger_source", "exit_on_opposite_signal", "strict_lot_cap", "run_id") if k in src},
    }
    try:
        if not verified:
            # Legacy records remain deployable, without claiming their defaults
            # reproduce a verified historical run. Keep the saved file intact.
            aliases = ("max_daily_loss_pct", "daily_loss_limit_pct", "daily_loss_stop_pct")
            source_config = src.get("config") or {}
            for name in aliases:
                if src.get(name) is not None:
                    entry["max_daily_loss_pct"] = float(src[name])
                    break
            cfg = asdict(ee.profile_config(ee._profile_from_dict(sym, entry)))
            for name in ("usd_inr_rate", "gst_rate", "maker_fee", "taker_fee", "margin_buffer"):
                value = src.get(name, source_config.get(name))
                if value is not None:
                    cfg[name] = float(value)
            if src.get("fx") is not None and src.get("usd_inr_rate") is None:
                cfg["usd_inr_rate"] = float(src["fx"])
            entry["effective_config"] = cfg
            entry["run_evidence_status"] = "legacy_defaults"
        ee.validate_deployable(ee._profile_from_dict(sym, entry))
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return entry


@router.get("/deployed")
def get_deployed_strategies() -> dict[str, Any]:
    return deployed_summary_view(_signals())


@router.post("/deploy")
def deploy_strategy_to_bot(req: DeployStrategyRequest) -> dict[str, Any]:
    # Serialize the entire read/merge/normalize/write operation: concurrent
    # single-row clicks must not overwrite another symbol's completed deploy.
    with _op_lock, dataset_lock(ee.BOT_PROFILES_FILE):
        try:
            paper_accounts.assert_ready(mode_db('paper'))
        except paper_accounts.AccountError as exc:
            raise HTTPException(exc.status,exc.detail) from exc
        return _deploy_strategy_to_bot(req)


def _deploy_strategy_to_bot(req: DeployStrategyRequest) -> dict[str, Any]:
    saved_all: dict[str, dict[str, Any]] = {}
    for r in _load_saved_results_for_deploy():
        for k in ("id", "result_id"):
            if r.get(k):
                saved_all[str(r[k])] = r
    raw_profiles = ee.load_bot_profiles_raw()
    deployed_symbols: list[str] = []

    if req.result_ids:
        for rid in req.result_ids:
            src = saved_all.get(str(rid))
            if not src:
                raise HTTPException(404, f"saved backtest result {rid!r} not found")
            sub_req = DeployStrategyRequest(result_id=rid, enabled=req.enabled)
            entry = _build_deploy_entry(src, sub_req)
            raw_profiles[entry["symbol"]] = entry
            deployed_symbols.append(entry["symbol"])
    else:
        rid = req.result_id or req.saved_result_id or req.id
        src = None
        for cand in (req.result_id, req.id, req.saved_result_id):
            if cand and str(cand) in saved_all:
                src = saved_all[str(cand)]
                break
        if rid and src is None:
            raise HTTPException(404, f"saved backtest result {rid!r} not found")
        entry = _build_deploy_entry(src, req)
        raw_profiles[entry["symbol"]] = entry
        deployed_symbols.append(entry["symbol"])

    paper_mode = (req.mode or status_mode()) == 'paper'
    if paper_mode:
        con = ro_conn('paper')
        try:
            wallet = con.execute('SELECT * FROM bot_config_state WHERE id=1').fetchone() if con else None
            if wallet:
                for sym in deployed_symbols:
                    entry = raw_profiles[sym]
                    cfg = ee.profile_config(ee._profile_from_dict(sym,entry))
                    effective = asdict(cfg)
                    effective.update(initial_capital_inr=wallet['capital_inr'],max_daily_loss_pct=wallet['daily_loss_limit_pct'])
                    entry.update(capital_inr=wallet['capital_inr'],max_daily_loss_pct=wallet['daily_loss_limit_pct'],
                                 daily_loss_limit_pct=wallet['daily_loss_limit_pct'],daily_loss_stop_pct=wallet['daily_loss_limit_pct'],
                                 effective_config=effective,
                                 paper_account_runtime_override={'capital_inr':wallet['capital_inr'],
                                                                 'daily_loss_limit_pct':wallet['daily_loss_limit_pct']})
        finally:
            if con is not None: con.close()
    _validate_active_wallet(raw_profiles, deployed_symbols[-1], use_paper_wallet=paper_mode)
    ee.save_bot_profiles_raw(raw_profiles)
    _cache.pop("signals", None)
    view = deployed_summary_view(_signals())
    return _clean({"ok": True, "deployed_symbols": deployed_symbols, **view})


def _validate_active_wallet(raw_profiles: dict[str, Any], preferred_symbol: str | None = None, *, use_paper_wallet: bool = True) -> None:
    """Refuse incompatible deployments; never rewrite a persisted wallet or peers."""
    try:
        active = {sym: ee.profile_config(ee._profile_from_dict(sym, ov))
                  for sym, ov in raw_profiles.items() if ov.get("enabled", True)}
        if not use_paper_wallet:
            return  # Live/testnet use Delta equity, runtime FX and the account's daily limit.
        con = ro_conn("paper") if use_paper_wallet else None
        try:
            row = con.execute("SELECT * FROM bot_config_state WHERE id=1").fetchone() if con else None
            wallet = ({"initial_capital_inr": row["capital_inr"], "usd_inr_rate": row["usd_inr_rate"],
                       "max_daily_loss_pct": row["daily_loss_limit_pct"]} if row else None)
        finally:
            if con is not None:
                con.close()
        if wallet is None and active:
            first = next(iter(active.values()))
            wallet = {k: getattr(first, k) for k in
                      ("initial_capital_inr", "usd_inr_rate", "max_daily_loss_pct")}
        for sym, cfg in active.items():
            mismatches = {k: {"profile": getattr(cfg, k), "wallet": value}
                          for k, value in wallet.items() if getattr(cfg, k) != value}
            if mismatches:
                raise HTTPException(409, {"code": "shared_wallet_mismatch", "symbol": sym,
                    "mismatches": mismatches,
                    "action": "Rerun/save with the existing paper wallet capital, FX and daily loss limit, "
                              "then deploy. Deployment never changes shared wallet risk settings."})
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


class ToggleDeployedRequest(BaseModel):
    enabled: bool | None = None


def _profile_mutation(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        with _op_lock, dataset_lock(ee.BOT_PROFILES_FILE):
            try:
                paper_accounts.assert_ready(mode_db('paper'))
                return fn(*args, **kwargs)
            except paper_accounts.AccountError as exc:
                raise HTTPException(exc.status,exc.detail) from exc
    return guarded


@router.put("/deployed/{symbol}/toggle")
@_profile_mutation
def toggle_deployed_strategy(symbol: str, req: ToggleDeployedRequest | None = None) -> dict[str, Any]:
    sym = symbol.strip().upper()
    raw_profiles = ee.load_bot_profiles_raw()
    if sym not in raw_profiles:
        raise HTTPException(404, f"no deployed strategy found for {sym}")
    cur = bool(raw_profiles[sym].get("enabled", True))
    new_state = (not cur) if (req is None or req.enabled is None) else bool(req.enabled)
    raw_profiles[sym]["enabled"] = new_state
    if new_state:
        _validate_active_wallet(raw_profiles, sym, use_paper_wallet=True)
    ee.save_bot_profiles_raw(raw_profiles)
    _cache.pop("signals", None)
    view = deployed_summary_view(_signals())
    return _clean({"ok": True, "symbol": sym, "enabled": new_state, **view})


@router.delete("/deployed/{symbol}")
@_profile_mutation
def undeploy_strategy(symbol: str) -> dict[str, Any]:
    sym = symbol.strip().upper()
    raw_profiles = ee.load_bot_profiles_raw()
    if sym == "ALL":
        raw_profiles.clear()
    else:
        if sym not in raw_profiles:
            raise HTTPException(404, f"no deployed strategy found for {sym}")
        raw_profiles.pop(sym, None)
    ee.save_bot_profiles_raw(raw_profiles)
    _cache.pop("signals", None)
    view = deployed_summary_view(_signals())
    return _clean({"ok": True, "undeployed": sym, **view})


@router.delete("/deployed")
@_profile_mutation
def clear_all_deployed_strategies() -> dict[str, Any]:
    ee.save_bot_profiles_raw({})
    _cache.pop("signals", None)
    view = deployed_summary_view({})
    return _clean({"ok": True, "undeployed": "ALL", **view})


def _runner(mode: str) -> lb.LiveBotRunner:
    try:
        return lb.LiveBotRunner(mode, profiles=ee.bot_profiles(), store_path=mode_db(mode), lock_path=PATHS["lock"],
                                control_dir=PATHS["control"], notifier=notifier(mode))
    except lb.BotAlreadyRunningError as exc:
        raise HTTPException(409, f"a bot runner is active: {exc}") from exc
    except RuntimeError as exc:  # e.g. journal bound to another mode
        raise HTTPException(409, str(exc)) from exc


def _journal_mode(mode: str | None = None) -> str | None:
    con = ro_conn(mode)
    if con is None:
        return None
    try:
        row = con.execute("SELECT mode FROM bot_config_state WHERE id=1").fetchone()
        return row["mode"] if row else None
    finally:
        con.close()


class BotActionRequest(BaseModel):
    mode: Literal["paper", "testnet", "live"] | None = None
    paper_session_id: str | None = None


def _action_target(req: BotActionRequest | None, *, paper_only: bool = False) -> str:
    if req and req.paper_session_id:
        raise HTTPException(409, "Archived paper session is read-only. Select Active paper account.")
    run, info = runner()
    active_runner_mode = (info or {}).get("mode") if run and (info or {}).get("mode") in EXEC_MODES else None
    requested = req.mode if (req and req.mode in EXEC_MODES) else None
    if active_runner_mode and requested and requested != active_runner_mode:
        raise HTTPException(
            409,
            f"Runner is active in {active_runner_mode.upper()} mode; switch Bot Operations mode to {active_runner_mode.upper()} or stop the runner first.",
        )
    target = requested or active_runner_mode or "paper"
    if paper_only and target != "paper":
        raise HTTPException(409, f"Run 1 Cycle is paper-only (selected mode: {target}).")
    return target


@router.post("/step")
def bot_step(req: BotActionRequest | None = None) -> dict[str, Any]:
    """One synchronous reconciliation + signal evaluation cycle (paper), only when no loop is running."""
    _action_target(req, paper_only=True)
    with _op_lock:
        if runner()[0]:
            raise HTTPException(409, "a bot loop holds live_bot.lock; it is already evaluating every closed bar")
        jm = _journal_mode("paper")
        if jm not in (None, "paper"):
            raise HTTPException(409, f"journal is bound to mode {jm!r}; Run 1 Cycle is paper-only")
        bot = _runner("paper")
        try:
            t0 = time.time()
            summary = bot.run_once()
            out = {"state": bot.state, "seconds": round(time.time() - t0, 2), "summary": summary,
                   "entries": bot.entries, "blocked_signals": bot.blocked_signals,
                   "evaluations": {s: {k: e.get(k) for k in ("bar_open", "close", "signal_side", "cost_gate_pass")}
                                   for s, e in bot.last_eval.items()},
                   "transitions": bot.transitions}
        finally:
            bot.close()
    return _clean(out)


class StartRequest(BaseModel):
    mode: Literal["paper", "testnet", "live"] = "paper"
    paper_session_id: str | None = None


@router.post("/start")
def bot_start(req: StartRequest | None = None) -> dict[str, Any]:
    global _proc, _proc_log, _proc_bot_pid
    if req and req.paper_session_id:
        raise HTTPException(409, "Archived paper session is read-only. Select Active paper account.")
    mode = (req or StartRequest()).mode
    if (why := lb.auto_bot_live_refusal(mode)) is not None:
        raise HTTPException(403, f"live auto-bot refused: {why}")
    if mode == "testnet" and not (settings.TESTNET_API_KEY and settings.TESTNET_API_SECRET):
        raise HTTPException(400, "testnet mode needs DELTA_TESTNET_API_KEY / DELTA_TESTNET_API_SECRET in .env")
    with _startup_operation():
        run, info = runner()
        if run:
            raise HTTPException(409, f"a bot runner already holds live_bot.lock: {info}")
        if mode == 'paper':
            try:
                paper_accounts.assert_ready(mode_db('paper'))
            except paper_accounts.AccountError as exc:
                raise HTTPException(exc.status,exc.detail) from exc
        jm = _journal_mode(mode)
        if jm not in (None, mode):
            raise HTTPException(409, f"journal {mode_db(mode).name} is bound to mode {jm!r}, not {mode!r}")
        PATHS["logs"].mkdir(parents=True, exist_ok=True)
        _proc_log = PATHS["logs"] / f"live_bot_{mode}_{pd.Timestamp.now(tz='UTC'):%Y%m%dT%H%M%SZ}.log"
        cmd = [sys.executable, str(PROJECT_ROOT / "live_bot.py"), "--mode", mode, "--loop", "--db-path", str(mode_db(mode)),
               "--lock-path", str(PATHS["lock"]), "--control-dir", str(PATHS["control"])]
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        logf = open(_proc_log, "w", encoding="utf-8")
        _proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT, stdout=logf, stderr=subprocess.STDOUT, creationflags=flags,
                                 env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
        logf.close()
        for _ in range(150):
            held, hinfo = runner()
            if held:
                _proc_bot_pid = (hinfo or {}).get("pid")
                break
            if _proc.poll() is not None:
                tail = _proc_log.read_text(encoding="utf-8", errors="replace")[-1500:]
                raise HTTPException(500, f"bot exited immediately (code {_proc.returncode}): {tail}")
            time.sleep(0.1)
    notifier(mode).notify_bot_lifecycle("Start requested", mode, "BOOTING", {"pid": _proc.pid})
    return {"started": True, "pid": _proc_bot_pid or _proc.pid, "launcher_pid": _proc.pid, "mode": mode,
            "log_file": str(_proc_log), "command": " ".join(cmd[1:])}


def _send(cmd: str, timeout: float, args: dict[str, Any] | None = None) -> dict[str, Any]:
    ch = lb.ControlChannel(PATHS["control"])
    cid = ch.submit(cmd, args)
    res = ch.wait_result(cid, timeout)
    if res is None:
        ch.cancel(cid)
        raise HTTPException(504, f"running bot did not acknowledge '{cmd}' within {timeout:.0f}s")
    return res


@router.post("/stop")
def bot_stop(req: BotActionRequest | None = None) -> dict[str, Any]:
    if req and req.paper_session_id:
        raise HTTPException(409, "Archived paper session is read-only. Select Active paper account.")
    _action_target(req)
    _stopping.set()
    try:
        return _stop_runner()
    finally:
        _stopping.clear()


def _stop_runner() -> dict[str, Any]:
    global _proc
    run, info = runner()
    if not run:
        process = process_state()
        return {'stopped':process['state']=='stopped','process':process,
                'reason':process['error'] or 'no bot runner is active'}
    forced = False
    try:
        ack = _send("stop", 20)
    except HTTPException:
        ack = None
    for _ in range(300):
        if not runner()[0]:
            break
        time.sleep(0.1)
    bot_pid = (info or {}).get("pid")
    ours = _proc is not None and bot_pid is not None and bot_pid == _proc_bot_pid
    if runner()[0] and bot_pid:  # did not exit after the stop command: hard-terminate the lock holder
        try:
            os.kill(bot_pid, 15)
            forced = True
        except OSError:
            pass
        for _ in range(100):
            if not runner()[0]:
                break
            time.sleep(0.1)
    code = None
    if ours:
        try:
            code = _proc.wait(15)
        except subprocess.TimeoutExpired:
            _proc.kill()
    released = not runner()[0]
    process = process_state()
    for _ in range(30):
        if process['state'] != 'stopping': break
        time.sleep(.1)
        process = process_state()
    return {"stopped": process['state']=='stopped', 'process':process, "pid": (info or {}).get("pid"), "ack": ack, "forced_terminate": forced, "exit_code": code,
            "lock_released": released, "note": "exchange-resident SL/TP orders are left in place"}


def _toggle_pause(paused: bool, req: BotActionRequest | None = None) -> dict[str, Any]:
    m = _action_target(req)
    if runner()[0]:
        return {"via": "control_channel", "mode": m, **_send("pause" if paused else "resume", 15)}
    with _op_lock:
        store = ee.StateStore(mode_db(m))
        try:
            if store.config() is None:
                store.init_config(m, ee.FROZEN_PROFILES)
            store.set_entries_paused(paused)
            store.event("INFO", "entries_paused" if paused else "entries_resumed", None, {"via": "dashboard"}, store.config()["mode"])
        except ee.StateStoreLockedError as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            store.close()
    notifier(m).notify_bot_lifecycle("Pause" if paused else "Resume", None, "STOPPED",
                                     "new entries paused" if paused else "new entries allowed")
    return {"via": "journal", "ok": True, "mode": m, "entries_paused": paused}


@router.post("/pause")
def bot_pause(req: BotActionRequest | None = None) -> dict[str, Any]:
    return _toggle_pause(True, req)


@router.post("/resume")
def bot_resume(req: BotActionRequest | None = None) -> dict[str, Any]:
    return _toggle_pause(False, req)


@router.post("/flatten")
def bot_flatten(req: BotActionRequest | None = None) -> dict[str, Any]:
    target = _action_target(req)
    if runner()[0]:
        return _clean({"via": "control_channel", "mode": target, **_send("flatten", 90, {"reason": "MANUAL_FLATTEN"})})
    with _op_lock:
        jm = _journal_mode(target) or target
        if jm == "live" and not settings.is_live:
            raise HTTPException(403, "journal is live but live trading is disabled")
        bot = _runner(jm)
        try:
            rep = bot.emergency_flatten_all("MANUAL_FLATTEN")
        finally:
            bot.close()
    return _clean({"via": "direct", "mode": jm, **rep})


@router.post("/backup")
def bot_backup(req: BotActionRequest | None = None) -> dict[str, Any]:
    target = _action_target(req)
    db = mode_db(target)
    if not db.exists():
        raise HTTPException(404, f"{db} does not exist yet (run a cycle first)")
    return _clean({"mode": target, **backup_state.backup(db, PATHS["backups"])})


@router.post("/test-telegram")
def bot_test_telegram(req: BotActionRequest | None = None) -> dict[str, Any]:
    target = _action_target(req)
    rec = notifier(target).send_test()
    return {"status": rec["status"], "mode": target, "error": rec.get("error"), "text": rec.get("text")}
