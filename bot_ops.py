"""Bot operations API (Step 7), mounted by app.py under /api/bot.

Single-writer rule: when a bot loop holds data/live_bot.lock it is the only writer of data/bot_state.db,
so pause / resume / flatten / stop are delivered as control files (live_bot.ControlChannel) and executed
by that process. With no runner active, the API performs the action itself (taking the same lock).
Status is read through a read-only SQLite connection (WAL allows it while the bot writes).
"""

from __future__ import annotations

import json
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
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import backup_state
import data_manager as dm
import execution_engine as ee
import live_bot as lb
from config import PROJECT_ROOT, settings
from exchange_client import DeltaRestClientAdapter
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
_op_lock = threading.Lock()
_notifier: TelegramNotifier | None = None
_cache: dict[str, Any] = {}


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


def active_mode() -> str:
    """Manual Trade's selected mode (persisted); paper unless switched."""
    f = PATHS["manual_mode"]
    try:
        m = json.loads(f.read_text(encoding="utf-8")).get("mode", "paper")
    except (OSError, ValueError):
        return "paper"
    return m if m in EXEC_MODES else "paper"


def set_active_mode(mode: str) -> None:
    f = PATHS["manual_mode"]
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"mode": mode, "at": time.time()}), encoding="utf-8")
    os.replace(tmp, f)


def status_mode() -> str:
    """Journal shown on the dashboard: the running loop's mode, else the Manual Trade mode."""
    run, info = runner()
    if run and (info or {}).get("mode") in EXEC_MODES:
        return info["mode"]
    return active_mode()


def notifier() -> TelegramNotifier:
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier.from_settings("paper")
    return _notifier


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
    held = lb.InstanceLock.is_held(PATHS["lock"])
    return held, (lb.InstanceLock.read_info(PATHS["lock"]) if held else None)


def ro_conn(mode: str | None = None) -> sqlite3.Connection | None:
    db = mode_db(mode or status_mode())
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:  # a writer may have created the file but not the schema yet (events is the last table created)
        ready = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone()
    except sqlite3.DatabaseError:
        ready = None
    if not ready:
        con.close()
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
    """Frozen-variant state on the latest CLOSED 1h/4h bars from the local history (cached 60s)."""
    hit = _cache.get("signals")
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    out: dict[str, Any] = {}
    for sym, prof in ee.bot_profiles().items():
        try:
            s = ee.evaluate_symbol(prof, ee.load_history(sym))
            out[sym] = {k: s[k] for k in ("variant", "bar_open", "bar_close", "last_1m_open", "close", "bb_upper", "bb_lower",
                                           "ema50_4h", "ema200_4h", "src_4h_open", "allow_long_4h", "allow_short_4h", "atr14",
                                           "atr_ratio", "gate_threshold", "cost_gate_pass", "cond_long", "cond_short",
                                           "signal_side", "fresh_signal")}
            out[sym]["trend_filter_used"] = prof.trend_filter_4h
            out[sym]["cost_gate_used"] = prof.cost_gate
        except Exception as exc:  # noqa: BLE001
            out[sym] = {"error": f"{type(exc).__name__}: {exc}"}
    _cache["signals"] = (time.time(), out)
    return out


def loss_streak(trades: list[sqlite3.Row], floor_us: int) -> int:
    n = 0
    for t in reversed(trades):
        if t["closed_at_us"] <= floor_us or t["net_pnl_inr"] >= 0:
            break
        n += 1
    return n


def read_status(with_market: bool = True, with_signals: bool = True, mode: str | None = None) -> dict[str, Any]:
    run, info = runner()
    jmode = mode or status_mode()
    jdb = mode_db(jmode)
    base: dict[str, Any] = {
        "runner": {"running": run, "pid": (info or {}).get("pid"), "mode": (info or {}).get("mode"),
                   "started": (info or {}).get("started"), "managed_by_dashboard": bool(_proc and _proc.poll() is None),
                   "log_file": str(_proc_log) if _proc_log else None},
        "settings": {"MODE": settings.MODE, "LIVE_TRADING_ENABLED": settings.LIVE_TRADING_ENABLED,
                     "live_allowed": settings.is_live and settings.AUTO_BOT_LIVE_ENABLED, "manual_live_allowed": settings.is_live,
                     "testnet_keys": bool(settings.TESTNET_API_KEY and settings.TESTNET_API_SECRET),
                     "telegram_configured": bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)},
        "db": str(jdb), "db_exists": jdb.exists(), "journal_mode": jmode,
    }
    con = ro_conn(jmode)
    if con is None:
        cap = ee.CAPITAL_INR
        base.update(readiness_state="NO_JOURNAL", mode=jmode, entries_paused=False, daily_halted=False, loss_streak=0,
                    positions=[], intents=[], trades=[], events=[], transitions=[],
                    wallet={"initial_capital_inr": cap, "realized_equity_inr": cap, "unrealized_pnl_inr": 0.0,
                            "total_equity_inr": cap, "available_margin_inr": cap, "used_margin_inr": 0.0, "today_pnl_inr": 0.0,
                            "today_pnl_pct": 0.0, "day_start_equity_inr": cap, "daily_loss_limit_pct": ee.DAILY_LOSS_LIMIT_PCT,
                            "daily_loss_limit_inr": cap * ee.DAILY_LOSS_LIMIT_PCT / 100, "closed_trades": 0,
                            "closed_net_inr": 0.0, "usd_inr_rate": settings.FIXED_USD_INR_RATE},
                    signals=_signals() if with_market and with_signals else {})
        return _clean(base)
    try:
        cfg = con.execute("SELECT * FROM bot_config_state WHERE id=1").fetchone()
        mode = cfg["mode"] if cfg else ((info or {}).get("mode") or "paper")
        rate = float(cfg["usd_inr_rate"]) if cfg else settings.FIXED_USD_INR_RATE
        capital = float(cfg["capital_inr"]) if cfg else ee.CAPITAL_INR
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
    halted = bool(cfg and cfg["halted"] and (cfg["halt_until_us"] is None or now_us < cfg["halt_until_us"]))
    rdet = json.loads(last_ready["detail"]) if last_ready and last_ready["detail"] else {}
    sdet = json.loads(last_state["detail"]) if last_state and last_state["detail"] else {}
    last_known = sdet.get("to")
    base.update(
        mode=mode,
        readiness_state=(last_known or "BOOTING") if run else "STOPPED",
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
                "day_start_equity_inr": day_start, "daily_loss_limit_pct": ee.DAILY_LOSS_LIMIT_PCT,
                "daily_loss_limit_inr": day_start * ee.DAILY_LOSS_LIMIT_PCT / 100, "closed_trades": len(trades),
                "closed_net_inr": closed, "usd_inr_rate": rate},
        positions=pos_out,
        intents=[{**dict(i), "created_at": us_iso(i["created_at_us"]), "updated_at": us_iso(i["updated_at_us"]),
                  "alert": i["state"] == "OUTCOME_UNKNOWN", "raw_response": None} for i in intents],
        trades=[{**dict(t), "opened_at": us_iso(t["opened_at_us"]), "closed_at": us_iso(t["closed_at_us"])} for t in reversed(trades[-50:])],
        transitions=[{"at": us_iso(e["at_us"]), **json.loads(e["detail"] or "{}")} for e in events if e["kind"] == "state"][:25],
        events=[{"id": e["id"], "at": us_iso(e["at_us"]), "level": e["level"], "kind": e["kind"], "symbol": e["symbol"],
                 "detail": e["detail"]} for e in events if e["kind"] != "state"][:40],
        signals=_signals() if with_market and with_signals else {},
    )
    return _clean(base)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get("/status")
def bot_status() -> dict[str, Any]:
    return read_status()


def _runner(mode: str) -> lb.LiveBotRunner:
    try:
        return lb.LiveBotRunner(mode, profiles=ee.bot_profiles(), store_path=mode_db(mode), lock_path=PATHS["lock"],
                                control_dir=PATHS["control"], notifier=notifier())
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


@router.post("/step")
def bot_step() -> dict[str, Any]:
    """One synchronous reconciliation + signal evaluation cycle (paper), only when no loop is running."""
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


@router.post("/start")
def bot_start(req: StartRequest | None = None) -> dict[str, Any]:
    global _proc, _proc_log, _proc_bot_pid
    mode = (req or StartRequest()).mode
    if (why := lb.auto_bot_live_refusal(mode)) is not None:
        raise HTTPException(403, f"live auto-bot refused: {why}")
    if mode == "testnet" and not (settings.TESTNET_API_KEY and settings.TESTNET_API_SECRET):
        raise HTTPException(400, "testnet mode needs DELTA_TESTNET_API_KEY / DELTA_TESTNET_API_SECRET in .env")
    with _op_lock:
        run, info = runner()
        if run:
            raise HTTPException(409, f"a bot runner already holds live_bot.lock: {info}")
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
    notifier().notify_bot_lifecycle("Start requested", mode, "BOOTING", {"pid": _proc.pid})
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
def bot_stop() -> dict[str, Any]:
    global _proc
    run, info = runner()
    if not run:
        return {"stopped": False, "reason": "no bot runner is active"}
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
    return {"stopped": released, "pid": (info or {}).get("pid"), "ack": ack, "forced_terminate": forced, "exit_code": code,
            "lock_released": released, "note": "exchange-resident SL/TP orders are left in place"}


def _toggle_pause(paused: bool) -> dict[str, Any]:
    if runner()[0]:
        return {"via": "control_channel", **_send("pause" if paused else "resume", 15)}
    with _op_lock:
        m = status_mode()
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
    notifier().notify_bot_lifecycle("Pause" if paused else "Resume", None, "STOPPED",
                                    "new entries paused" if paused else "new entries allowed")
    return {"via": "journal", "ok": True, "entries_paused": paused}


@router.post("/pause")
def bot_pause() -> dict[str, Any]:
    return _toggle_pause(True)


@router.post("/resume")
def bot_resume() -> dict[str, Any]:
    return _toggle_pause(False)


@router.post("/flatten")
def bot_flatten() -> dict[str, Any]:
    if runner()[0]:
        return _clean({"via": "control_channel", **_send("flatten", 90, {"reason": "MANUAL_FLATTEN"})})
    with _op_lock:
        jm = _journal_mode(status_mode()) or status_mode()
        if jm == "live" and not settings.is_live:
            raise HTTPException(403, "journal is live but live trading is disabled")
        bot = _runner(jm)
        try:
            rep = bot.emergency_flatten_all("MANUAL_FLATTEN")
        finally:
            bot.close()
    return _clean({"via": "direct", **rep})


@router.post("/backup")
def bot_backup() -> dict[str, Any]:
    db = mode_db(status_mode())
    if not db.exists():
        raise HTTPException(404, f"{db} does not exist yet (run a cycle first)")
    return _clean(backup_state.backup(db, PATHS["backups"]))


@router.post("/test-telegram")
def bot_test_telegram() -> dict[str, Any]:
    rec = notifier().send_test()
    return {"status": rec["status"], "error": rec.get("error"), "text": rec.get("text")}
