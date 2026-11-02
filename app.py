"""Research & backtesting dashboard API (FastAPI).

Run:  .venv\\Scripts\\python.exe -m uvicorn app:app --reload   ->  http://127.0.0.1:8000

The research endpoints in this module never place orders. Bot operations (Step 7) live in bot_ops.py,
mounted below under /api/bot; live mode stays refused unless MODE="live" AND LIVE_TRADING_ENABLED=True.
Backtests load the FULL local 1m history
(indicators are past-only, so appending newer candles never changes older values) and run the
requested window [end - days, end). Pinning ``end`` to the Step 3C anchor reproduces the CLI
walk-forward numbers exactly.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Literal

import numpy as np
import pandas as pd
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import data_manager as dm
import step3c_walkforward as s3c
import strategies
import run_store
from dataclasses import asdict
from trade_rules import stop_config
from backtester import Backtester, BacktestConfig, BacktestResult, Leg, ProductSpec, audit_result
from config import PROJECT_ROOT, settings

log = logging.getLogger("app")

STATIC_DIR = PROJECT_ROOT / "static"
RESULTS_DIR = settings.data_path / "results"
APP_SYMBOLS_FILE = settings.data_path / "app_symbols.json"
STRATEGIES_FILE = settings.data_path / "strategies.json"
SAVED_BACKTESTS_FILE = settings.data_path / "saved_backtests.json"
STEP3C_CSV = RESULTS_DIR / "step3c_walkforward.csv"
STEP3C_ANCHOR = RESULTS_DIR / "step3c_windows.json"
STEP3B_CSV = RESULTS_DIR / "step3b_grid.csv"
STEP4B_ANCHOR = RESULTS_DIR / "step4b_windows.json"
STEP4B_CSV = RESULTS_DIR / "step4b_comparison.csv"

TAG_IN_SAMPLE = "[In-Sample 3-Window Selection]"
TAG_WALK_FORWARD = "[Walk-Forward W1+W2 Selected]"
TAG_STEP4B = "[Step 4B Ablation Variant — ₹50,000]"
STEP4B_VARIANTS = {"A_baseline": (False, False), "B_trend4h": (True, False), "C_costgate": (False, True), "D_both": (True, True)}

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")
TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "45m", "1h", "2h", "4h"]
EXIT_MODES = ["fixed", "BE", "trail", "BE+trail", "opp", "BE+opp", "trail+opp"]
OVERLAY_COLS = ["ema9", "ema21", "ema50", "ema55", "ema100", "ema200", "ema_fast", "ema_slow", "ema_trend",
                "vwap", "bb_upper", "bb_mid", "bb_lower", "kc_upper", "kc_lower", "dc_high", "dc_low"]
MAX_EQUITY_POINTS = 4000

app = FastAPI(title="Delta Futures Lab", version="1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

import bot_ops  # noqa: E402  (Step 7: /api/bot/* operations router)
import manual_trade  # noqa: E402  (Step 8: /api/products/catalog + /api/manual/* terminal)

app.include_router(bot_ops.router)
app.include_router(manual_trade.router)


# --------------------------------------------------------------------------- #
# JSON helpers
# --------------------------------------------------------------------------- #
def clean(obj: Any) -> Any:
    """Make numpy/pandas values JSON-safe (NaN -> null, +/-inf -> 'inf'/'-inf')."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        x = float(obj)
        if math.isnan(x):
            return None
        if math.isinf(x):
            return "inf" if x > 0 else "-inf"
        return x
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return obj


def ts(x: pd.Timestamp) -> int:
    return int(x.timestamp())


def parse_utc(x: datetime | str | None) -> pd.Timestamp | None:
    if x is None:
        return None
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


# --------------------------------------------------------------------------- #
# Symbols + data cache
# --------------------------------------------------------------------------- #
_file_lock = threading.RLock()  # parquet reads/writes
_cache_lock = threading.RLock()
_data_cache: dict[str, tuple[tuple, pd.DataFrame]] = {}
_frame_cache: OrderedDict[tuple, tuple[pd.DataFrame, pd.DataFrame, pd.Series]] = OrderedDict()
_coverage_cache: dict[tuple, dict[str, Any]] = {}


def app_symbols() -> list[str]:
    return dm.app_symbols()  # shared with the bot / manual terminal (Step 8)


def add_app_symbol(symbol: str) -> None:
    dm.add_app_symbol(symbol)


def check_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    if not SYMBOL_RE.match(s):
        raise HTTPException(422, f"invalid symbol {symbol!r}")
    return s


def mtime_key(symbol: str) -> tuple:
    return tuple(
        p.stat().st_mtime_ns if p.exists() else 0
        for p in (dm.raw_1m_path(symbol, st) for st in dm.SERIES_TYPES)
    )


def get_data(symbol: str) -> pd.DataFrame:
    key = mtime_key(symbol)
    with _cache_lock:
        hit = _data_cache.get(symbol)
        if hit and hit[0] == key:
            return hit[1]
    if not all(key):
        raise HTTPException(404, f"no local 1m data for {symbol}; sync it first")
    with _file_lock:
        df = dm.load_unified_1m_dataset(symbol)  # full history
    with _cache_lock:
        _data_cache[symbol] = (key, df)
    return df


def get_history(symbol: str, history_start: pd.Timestamp | None = None) -> pd.DataFrame:
    """1m history starting at ``history_start`` (indicator warm-up starts there; None = all data).

    Pinned runs pass the warm-up start saved by the step that produced the result, because
    seeded EMAs depend on where the history begins (e.g. 4h EMA200 after only ~270 bars).
    """
    df = get_data(symbol)
    if history_start is None:
        return df
    if history_start < df.index[0]:
        raise HTTPException(422, f"{symbol}: history_start {history_start} is before local data ({df.index[0]})")
    return df[df.index >= history_start]


def get_frame(symbol: str, strategy: str, tf: str,
              history_start: pd.Timestamp | None = None,
              custom_def: dict[str, Any] | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """(bars, strategy output, ATR14) from ``history_start``, cached by data version."""
    c_key = json.dumps(custom_def, sort_keys=True) if custom_def else None
    key = (symbol, strategy, tf, history_start, c_key, mtime_key(symbol))
    with _cache_lock:
        if key in _frame_cache:
            _frame_cache.move_to_end(key)
            return _frame_cache[key]
    df = get_history(symbol, history_start)
    bars = dm.resample_candles(df, tf)
    out = strategies.generate_signals(strategy, bars, custom_def=custom_def, bars_4h=dm.resample_candles(df, "4h"))
    item = (bars, out, strategies.atr(bars, 14))
    with _cache_lock:
        _frame_cache[key] = item
        while len(_frame_cache) > 24:
            _frame_cache.popitem(last=False)
    return item


def coverage(symbol: str) -> dict[str, Any]:
    key = (symbol, mtime_key(symbol))
    with _cache_lock:
        if key in _coverage_cache:
            return _coverage_cache[key]
    series = {}
    for st in dm.SERIES_TYPES:
        p = dm.raw_1m_path(symbol, st)
        if not p.exists():
            series[st] = None
            continue
        with _file_lock:
            t = pd.read_parquet(p, columns=["time"])["time"]
        g = dm.find_gaps(t)
        series[st] = {
            "rows": g.rows, "gaps": g.gap_count, "missing_minutes": g.missing_minutes,
            "start": pd.Timestamp(g.start, unit="s", tz="UTC").isoformat() if g.start else None,
            "end": pd.Timestamp(g.end, unit="s", tz="UTC").isoformat() if g.end else None,
            "days": round(g.rows / 1440, 2),
        }
    out = {"symbol": symbol, "series": series}
    with _cache_lock:
        _coverage_cache[key] = out
    return out


def product_info(symbol: str) -> dict[str, Any] | None:
    try:
        m = dm.load_metadata(symbol)
    except Exception:  # noqa: BLE001
        return None
    return {k: m.get(k) for k in ("id", "symbol", "contract_type", "state", "contract_value", "tick_size",
                                  "maker_commission_rate", "taker_commission_rate")} | {
        "contract_unit_currency": m.get("contract_unit_currency"),
        "settling_asset": (m.get("settling_asset") or {}).get("symbol"),
    }


def step3c_anchor() -> dict[str, Any] | None:
    if STEP3C_ANCHOR.exists():
        return json.loads(STEP3C_ANCHOR.read_text(encoding="utf-8"))
    return None


def step4b_anchor() -> dict[str, Any] | None:
    if STEP4B_ANCHOR.exists():
        return json.loads(STEP4B_ANCHOR.read_text(encoding="utf-8"))
    return None


def get_bars_4h(symbol: str, history_start: pd.Timestamp | None) -> pd.DataFrame:
    key = (symbol, "__4h__", "4h", history_start, mtime_key(symbol))
    with _cache_lock:
        if key in _frame_cache:
            _frame_cache.move_to_end(key)
            return _frame_cache[key][0]
    bars = dm.resample_candles(get_history(symbol, history_start), "4h")
    with _cache_lock:
        _frame_cache[key] = (bars, bars, bars["close"])
    return bars


def filter_masks(symbol: str, timeframe: str, bars: pd.DataFrame, history_start,
                 trend_4h: bool, gate: bool, slippage_bps: float) -> dict[str, tuple[pd.Series, pd.Series]]:
    """Step 4B entry filters (applied before edge-triggering, see strategies.apply_entry_filters)."""
    filters: dict[str, tuple[pd.Series, pd.Series]] = {}
    if trend_4h:
        tf4 = strategies.trend_filter_4h(bars, get_bars_4h(symbol, history_start), dm.TIMEFRAME_MINUTES[timeframe])
        filters["trend4h"] = (tf4["allow_long"], tf4["allow_short"])
    if gate:
        g = strategies.cost_gate(bars, settings.TAKER_FEE, settings.GST_ON_FEES, slippage_bps)
        filters["costgate"] = (g["allow"], g["allow"])
    return filters


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def status() -> JSONResponse:
    syms = app_symbols()
    return JSONResponse(clean({
        "environment": settings.ENVIRONMENT,
        "rest_url": settings.env.rest_url,
        "mode": settings.MODE,
        "live_trading_enabled": settings.LIVE_TRADING_ENABLED,
        "is_live": settings.is_live,
        "symbols": syms,
        "config_symbols": settings.SYMBOLS,
        "products": {s: product_info(s) for s in syms},
        "coverage": {s: coverage(s) for s in syms},
        "strategies": [{"name": s.name, "description": s.description} for s in strategies.STRATEGIES.values()],
        "custom_strategies": _load_custom_strategies(),
        "timeframes": TIMEFRAMES,
        "exit_modes": EXIT_MODES,
        "fee_modes": ["taker_maker_tp", "maker_maker", "taker", "maker"],
        "trigger_sources": ["trade", "mark"],
        "defaults": {
            "initial_capital_inr": settings.INITIAL_CAPITAL_INR, "leverage": settings.DEFAULT_LEVERAGE,
            "risk_per_trade_pct": settings.RISK_PER_TRADE_PCT, "max_daily_loss_pct": settings.MAX_DAILY_LOSS_PCT,
            "usd_inr_rate": settings.FIXED_USD_INR_RATE, "gst_rate": settings.GST_ON_FEES,
            "maker_fee": settings.MAKER_FEE, "taker_fee": settings.TAKER_FEE,
        },
        "step3c_anchor": step3c_anchor(),
        "step4b_anchor": step4b_anchor(),
        "slippage_options_bps": [0, 2, 5],
        "sync": sync_state(),
    }))


# --------------------------------------------------------------------------- #
# Sync (background thread)
# --------------------------------------------------------------------------- #
_job_lock = threading.Lock()
_job: dict[str, Any] = {"state": "idle"}


def sync_state() -> dict[str, Any]:
    with _job_lock:
        return dict(_job)


class SyncRequest(BaseModel):
    symbols: list[str] | None = None
    add_symbol: str | None = None
    days: int = Field(90, ge=1, le=400, description="history to download for a newly added symbol")


def _sync_worker(job_id: str, symbols: list[str], new_symbol: str | None, days: int) -> None:
    results = []
    try:
        for sym in symbols:
            dm.fetch_and_save_metadata([sym])
            for st in dm.SERIES_TYPES:
                with _job_lock:
                    _job["message"] = f"{sym} {st}"
                # days=1 for known symbols: the resume logic only fetches minutes newer than stored data.
                with _file_lock:
                    r = dm.download_1m_series(sym, st, days if sym == new_symbol else 1)
                results.append({"symbol": sym, "series": st, "rows_before": r.rows_before,
                                "rows_after": r.rows_after, "fetched": r.rows_fetched, "requests": r.requests})
            if sym == new_symbol:
                add_app_symbol(sym)
        with _job_lock:
            _job.update(state="done", finished=datetime.now(timezone.utc).isoformat(), results=results, message="done")
    except Exception as exc:  # noqa: BLE001
        log.exception("sync failed")
        with _job_lock:
            _job.update(state="error", finished=datetime.now(timezone.utc).isoformat(), results=results, error=str(exc))


@app.post("/api/data/sync")
def start_sync(req: SyncRequest | None = None) -> JSONResponse:
    req = req or SyncRequest()
    new_symbol = None
    if req.add_symbol:
        new_symbol = check_symbol(req.add_symbol)
        try:
            product = dm.client().get(f"/v2/products/{new_symbol}")
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 502
            raise HTTPException(404 if code in (400, 404) else 502, f"{new_symbol}: not found on Delta Exchange ({code})")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(404, f"{new_symbol}: product lookup failed ({exc})")
        if product.get("contract_type") != "perpetual_futures" or product.get("state") != "live":
            raise HTTPException(400, f"{new_symbol}: contract_type={product.get('contract_type')}, "
                                     f"state={product.get('state')} (need a live perpetual future)")
    symbols = [check_symbol(s) for s in (req.symbols or app_symbols())]
    if new_symbol and new_symbol not in symbols:
        symbols.append(new_symbol)
    with _job_lock:
        if _job.get("state") == "running":
            raise HTTPException(409, "a sync is already running")
        job_id = uuid.uuid4().hex[:12]
        _job.clear()
        _job.update(id=job_id, state="running", started=datetime.now(timezone.utc).isoformat(),
                    symbols=symbols, add_symbol=new_symbol, message="starting")
    threading.Thread(target=_sync_worker, args=(job_id, symbols, new_symbol, req.days), daemon=True).start()
    return JSONResponse(sync_state(), status_code=202)


@app.get("/api/data/sync")
def get_sync() -> JSONResponse:
    return JSONResponse(clean(sync_state()))


# --------------------------------------------------------------------------- #
# Step 17: Data Lab — Inventory, Backfill, Add Coin & Gap Repair
# --------------------------------------------------------------------------- #
_bf_lock = threading.Lock()
_bf_state: dict[str, Any] = {
    "is_running": False,
    "state": "idle",
    "symbol": "",
    "symbols": [],
    "mode": "idle",
    "progress_pct": 0.0,
    "current_chunk": 0,
    "total_chunks": 0,
    "message": "Up to date ✔",
    "error": None,
    "started_at": None,
    "finished_at": None,
    "results": [],
}


def backfill_status_snapshot() -> dict[str, Any]:
    with _bf_lock:
        return dict(_bf_state)


def _invalidate_symbol_caches(symbol: str | None = None) -> None:
    with _cache_lock:
        if symbol is None:
            _data_cache.clear()
            _frame_cache.clear()
            _coverage_cache.clear()
        else:
            _data_cache.pop(symbol, None)
            for k in [k for k in _frame_cache if k and k[0] == symbol]:
                _frame_cache.pop(k, None)
            for k in [k for k in _coverage_cache if k and k[0] == symbol]:
                _coverage_cache.pop(k, None)


def _fmt_utc_minute(epoch_s: int | None) -> str | None:
    if not epoch_s:
        return None
    return pd.Timestamp(int(epoch_s), unit="s", tz="UTC").strftime("%Y-%m-%d %H:%M UTC")


def _build_symbol_inventory(sym: str, active_bf_sym: str | None, bf_running: bool) -> dict[str, Any]:
    cov = coverage(sym)["series"]
    meta: dict[str, Any] = {}
    mpath = dm.metadata_path(sym)
    if mpath.exists():
        try:
            meta = json.loads(mpath.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
    trade_cov = cov.get("TRADE") or {}
    mark_cov = cov.get("MARK") or {}
    fund_cov = cov.get("FUNDING") or {}

    t_rows = int(trade_cov.get("rows") or 0)
    m_rows = int(mark_cov.get("rows") or 0)
    f_rows = int(fund_cov.get("rows") or 0)

    gaps_count = int(trade_cov.get("gaps") or 0)
    missing_min = int(trade_cov.get("missing_minutes") or 0)

    size_bytes = 0
    for st_type in dm.SERIES_TYPES:
        p = dm.raw_1m_path(sym, st_type)
        if p.exists():
            size_bytes += p.stat().st_size
    file_size_mb = round(size_bytes / (1024 * 1024), 2)

    start_iso = trade_cov.get("start")
    end_iso = trade_cov.get("end")
    start_ts = int(pd.Timestamp(start_iso).timestamp()) if start_iso else None
    end_ts = int(pd.Timestamp(end_iso).timestamp()) if end_iso else None
    start_date = pd.Timestamp(start_iso).strftime("%Y-%m-%d %H:%M UTC") if start_iso else None
    end_date = pd.Timestamp(end_iso).strftime("%Y-%m-%d %H:%M UTC") if end_iso else None

    if start_ts and end_ts and end_ts >= start_ts:
        cov_days = round((end_ts - start_ts + 60) / 86_400, 2)
    else:
        cov_days = round(t_rows / 1440.0, 2)

    if bf_running and active_bf_sym in (sym, "ALL", "ALTCOINS"):
        st_label = "downloading"
    elif _job.get("state") == "running" and sym in (_job.get("symbols") or []):
        st_label = "syncing"
    elif t_rows > 0:
        st_label = "ready"
    else:
        st_label = "no_data"

    max_lev = 20
    try:
        max_lev = int(float(meta.get("default_leverage") or 20))
    except Exception:  # noqa: BLE001
        pass

    return {
        "symbol": sym,
        "name": meta.get("description") or f"{sym} Perpetual Future",
        "contract_value": meta.get("contract_value"),
        "tick_size": meta.get("tick_size"),
        "max_leverage": max_lev,
        "status": st_label,
        "coverage_days": cov_days,
        "start_date": start_date,
        "end_date": end_date,
        "start_iso": start_iso,
        "end_iso": end_iso,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "trade_rows": t_rows,
        "mark_rows": m_rows,
        "funding_rows": f_rows,
        "total_series_rows": t_rows + m_rows + f_rows,
        "gaps_count": gaps_count,
        "missing_minutes": missing_min,
        "gaps_by_series": {
            "TRADE": int(trade_cov.get("gaps") or 0),
            "MARK": int(mark_cov.get("gaps") or 0),
            "FUNDING": int(fund_cov.get("gaps") or 0),
        },
        "file_size_mb": file_size_mb,
    }


@app.get("/api/data/inventory")
def get_data_inventory() -> JSONResponse:
    syms = app_symbols()
    bf = backfill_status_snapshot()
    bf_running = bool(bf.get("is_running"))
    active_sym = bf.get("symbol")
    items = [_build_symbol_inventory(s, active_sym, bf_running) for s in syms]
    total_candles = sum(x["trade_rows"] for x in items)
    total_all_series = sum(x["total_series_rows"] for x in items)
    total_mb = round(sum(x["file_size_mb"] for x in items), 2)
    end_dates = [x["end_date"] for x in items if x.get("end_date")]
    latest_sync = max(end_dates) if end_dates else None
    payload = {
        "items": items,
        "inventory": items,
        "coins": items,
        "total_symbols": len(items),
        "total_candles_all_coins": total_candles,
        "total_rows_all_series": total_all_series,
        "total_disk_size_mb": total_mb,
        "latest_sync_utc": latest_sync,
        "backfill_status": bf,
    }
    return JSONResponse(clean(payload))


class BackfillRequest(BaseModel):
    symbol: str = "ALL"
    mode: Literal["older", "latest", "custom_range", "target_days"] = "older"
    days_to_add: int | None = Field(30, ge=0, le=1500)
    start_date: str | None = None
    end_date: str | None = None
    dry_run: bool = False


def _parse_date_to_epoch(val: str | None, is_end: bool = False) -> int | None:
    if not val:
        return None
    s = val.strip()
    t = pd.Timestamp(s)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    if len(s) <= 10 and is_end:
        t = t + pd.Timedelta(hours=23, minutes=59)
    return int(t.timestamp())


def _compute_symbol_target_range(
    sym: str, mode: str, days_to_add: int, start_date: str | None, end_date: str | None
) -> tuple[int, int]:
    now_end = dm._last_closed_minute()
    cov_t = (coverage(sym)["series"] or {}).get("TRADE") or {}
    start_iso = cov_t.get("start")
    end_iso = cov_t.get("end")
    have_min = int(pd.Timestamp(start_iso).timestamp()) if start_iso else None
    have_max = int(pd.Timestamp(end_iso).timestamp()) if end_iso else None

    if mode == "custom_range":
        s_ep = _parse_date_to_epoch(start_date, is_end=False)
        e_ep = _parse_date_to_epoch(end_date, is_end=True)
        if not s_ep or not e_ep or s_ep >= e_ep:
            raise ValueError("custom_range requires valid start_date < end_date")
        return s_ep, min(e_ep, now_end)

    if mode == "latest":
        req_start = (have_max - 3600) if have_max else (now_end + 60 - max(1, days_to_add) * 86_400)
        return req_start, now_end

    if mode == "target_days":
        d = max(1, days_to_add or 365)
        return now_end + 60 - d * 86_400, now_end

    # mode == "older"
    d = max(1, days_to_add or 30)
    if have_min:
        return have_min - d * 86_400, have_max or now_end
    return now_end + 60 - d * 86_400, now_end


def _backfill_worker(
    target_symbols: list[str],
    mode: str,
    days_to_add: int,
    start_date: str | None,
    end_date: str | None,
    dry_run: bool,
) -> None:
    results = []
    try:
        # Pre-calculate total chunks across all target symbols & series for accurate progress %
        plan: list[tuple[str, dm.SeriesType, int, int, int]] = []
        total_chunks = 0
        for sym in target_symbols:
            if not dm.metadata_path(sym).exists():
                try:
                    dm.fetch_and_save_metadata([sym])
                except Exception:  # noqa: BLE001
                    pass
            req_s, req_e = _compute_symbol_target_range(sym, mode, days_to_add, start_date, end_date)
            for st_type in dm.SERIES_TYPES:
                if dry_run or days_to_add == 0:
                    n_ch = 1
                else:
                    _, ranges = dm.missing_ranges_for_series(sym, st_type, req_s, req_e)
                    n_ch = len(dm.chunks_for_ranges(ranges))
                plan.append((sym, st_type, req_s, req_e, n_ch))
                total_chunks += n_ch

        total_chunks = max(1, total_chunks)
        with _bf_lock:
            _bf_state["total_chunks"] = total_chunks
            _bf_state["current_chunk"] = 0
            _bf_state["progress_pct"] = 2.0

        done_before = 0
        for sym, st_type, req_s, req_e, n_ch in plan:
            with _bf_lock:
                _bf_state["symbol"] = sym
                _bf_state["message"] = f"{sym} ({st_type}) · preparing range…"

            if dry_run or days_to_add == 0:
                done_before += 1
                pct = round(min(99.0, done_before * 100.0 / total_chunks), 1)
                with _bf_lock:
                    _bf_state.update(
                        current_chunk=done_before,
                        progress_pct=pct,
                        message=f"Verified chunk {done_before} of {total_chunks} · {pct}% · {sym} {st_type}",
                    )
                results.append({"symbol": sym, "series": st_type, "fetched": 0, "requests": 0})
                continue

            if n_ch == 0:
                results.append({"symbol": sym, "series": st_type, "fetched": 0, "requests": 0})
                continue

            def _on_chunk(idx: int, tot: int, cs: int, ce: int, s_type: str, _sym: str = sym, _base: int = done_before) -> None:
                cur = _base + idx
                pct = round(min(99.0, cur * 100.0 / total_chunks), 1)
                d1 = pd.Timestamp(cs, unit="s", tz="UTC").strftime("%Y-%m-%d")
                d2 = pd.Timestamp(ce, unit="s", tz="UTC").strftime("%Y-%m-%d")
                with _bf_lock:
                    _bf_state.update(
                        symbol=_sym,
                        current_chunk=cur,
                        progress_pct=pct,
                        message=f"Downloading chunk {cur} of {total_chunks} · {pct}% · {_sym} {s_type} ({d1} to {d2})…",
                    )

            with _file_lock:
                r = dm.download_1m_range(sym, st_type, req_s, req_e, progress_cb=_on_chunk)
            done_before += n_ch
            _invalidate_symbol_caches(sym)
            results.append({
                "symbol": sym,
                "series": st_type,
                "rows_before": r.rows_before,
                "rows_after": r.rows_after,
                "fetched": r.rows_fetched,
                "requests": r.requests,
            })

        _invalidate_symbol_caches(None)
        with _bf_lock:
            _bf_state.update(
                is_running=False,
                state="done",
                progress_pct=100.0,
                current_chunk=total_chunks,
                total_chunks=total_chunks,
                finished_at=datetime.now(timezone.utc).isoformat(),
                message=f"Backfill complete ✔ ({len(target_symbols)} coin(s) updated)",
                error=None,
                results=results,
            )
    except Exception as exc:  # noqa: BLE001
        log.exception("backfill failed")
        with _bf_lock:
            _bf_state.update(
                is_running=False,
                state="error",
                finished_at=datetime.now(timezone.utc).isoformat(),
                message=f"Backfill error: {exc}",
                error=str(exc),
                results=results,
            )


@app.post("/api/data/backfill")
def start_backfill(req: BackfillRequest) -> JSONResponse:
    raw_sym = (req.symbol or "ALL").strip().upper()
    all_syms = app_symbols()
    if raw_sym == "ALL":
        targets = all_syms
    elif raw_sym == "ALTCOINS":
        targets = [s for s in all_syms if s not in ("BTCUSD", "ETHUSD")] or all_syms
    else:
        s = check_symbol(raw_sym)
        if s not in all_syms:
            add_app_symbol(s)
        targets = [s]

    if req.mode == "custom_range":
        try:
            _compute_symbol_target_range(targets[0], req.mode, int(req.days_to_add or 30), req.start_date, req.end_date)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    with _bf_lock:
        if _bf_state.get("is_running"):
            raise HTTPException(409, "A data backfill/download job is already running")
        _bf_state.update(
            is_running=True,
            state="running",
            symbol=raw_sym,
            symbols=targets,
            mode=req.mode,
            progress_pct=1.0,
            current_chunk=0,
            total_chunks=max(1, len(targets) * 3),
            message=f"Starting {req.mode} backfill for {', '.join(targets)}…",
            error=None,
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=None,
            results=[],
        )

    threading.Thread(
        target=_backfill_worker,
        args=(targets, req.mode, int(req.days_to_add if req.days_to_add is not None else 30), req.start_date, req.end_date, req.dry_run),
        daemon=True,
    ).start()
    return JSONResponse(clean(backfill_status_snapshot()), status_code=202)


@app.get("/api/data/backfill/status")
def get_backfill_status() -> JSONResponse:
    return JSONResponse(clean(backfill_status_snapshot()))


class AddCoinRequest(BaseModel):
    symbol: str
    initial_days: int = Field(60, ge=0, le=1500)
    dry_run: bool = False


@app.post("/api/data/add_coin")
def add_coin_endpoint(req: AddCoinRequest) -> JSONResponse:
    sym = check_symbol(req.symbol)
    mpath = dm.metadata_path(sym)
    product: dict[str, Any] | None = None
    if mpath.exists():
        try:
            product = json.loads(mpath.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            product = None
    if product is None:
        try:
            product = dm.client().get(f"/v2/products/{sym}")
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 502
            raise HTTPException(404 if code in (400, 404) else 502, f"{sym}: not found on Delta Exchange ({code})") from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(404, f"{sym}: product lookup failed ({exc})") from exc
        if product.get("contract_type") != "perpetual_futures" or product.get("state") != "live":
            raise HTTPException(
                400,
                f"{sym}: contract_type={product.get('contract_type')}, state={product.get('state')} (need a live perpetual future)",
            )
        dm.metadata_dir().mkdir(parents=True, exist_ok=True)
        dm._atomic_write_bytes(mpath, json.dumps(product, indent=2, sort_keys=True).encode("utf-8"))

    add_app_symbol(sym)
    _invalidate_symbol_caches(sym)

    if req.initial_days > 0 and not req.dry_run:
        bf_req = BackfillRequest(symbol=sym, mode="target_days", days_to_add=req.initial_days, dry_run=False)
        start_backfill(bf_req)

    return JSONResponse(
        clean({
            "ok": True,
            "symbol": sym,
            "initial_days": req.initial_days,
            "symbols": app_symbols(),
            "product": product_info(sym),
            "backfill_status": backfill_status_snapshot(),
        })
    )


class RepairGapsRequest(BaseModel):
    symbol: str = "ALL"


@app.post("/api/data/repair_gaps")
def repair_gaps_endpoint(req: RepairGapsRequest) -> JSONResponse:
    raw_sym = (req.symbol or "ALL").strip().upper()
    targets = app_symbols() if raw_sym == "ALL" else [check_symbol(raw_sym)]
    reports = []
    for sym in targets:
        with _file_lock:
            rep = dm.repair_symbol_gaps(sym)
        _invalidate_symbol_caches(sym)
        reports.append(rep)
    return JSONResponse(
        clean({
            "ok": True,
            "symbol": raw_sym,
            "gaps_before": sum(r["gaps_before"] for r in reports),
            "gaps_after": sum(r["gaps_after"] for r in reports),
            "rows_added": sum(r["rows_added"] for r in reports),
            "reports": reports,
        })
    )


# --------------------------------------------------------------------------- #
# Presets + leaderboard (from CLI result files)
# --------------------------------------------------------------------------- #
_results_cache: dict[str, tuple[float, pd.DataFrame]] = {}


def _cached_csv(path, builder) -> pd.DataFrame:
    if not path.exists():
        raise HTTPException(404, f"{path.name} not found; run the corresponding step script first")
    m = path.stat().st_mtime
    hit = _results_cache.get(str(path))
    if hit and hit[0] == m:
        return hit[1]
    df = builder(pd.read_csv(path))
    _results_cache[str(path)] = (m, df)
    return df


def step3c_wide() -> pd.DataFrame:
    return _cached_csv(STEP3C_CSV, s3c.build_wide)


def _build_3b_wide(res: pd.DataFrame) -> pd.DataFrame:
    key = ["symbol", "strategy", "tf", "stops", "be", "fees"]
    full = res[res["window"] == "full"].set_index(key).drop(columns=["window"])
    for w in ("train", "test"):
        part = res[res["window"] == w].set_index(key)
        full[f"net_{w}"] = part["net"]
        full[f"trades_{w}"] = part["trades"]
    out = full.reset_index()
    out["exits"] = np.where(out["be"], "BE", "fixed")
    return out


def step3b_wide() -> pd.DataFrame:
    return _cached_csv(STEP3B_CSV, _build_3b_wide)


def stops_to_params(stops: str) -> dict[str, Any]:
    p = s3c.STOPS.get(stops)
    if p is None:  # step3b labels, e.g. "pct 1.5/3.5", "atr 1.5x/3.5x"
        mode, vals = stops.split(" ", 1)
        sl, tp = (float(v.rstrip("x")) for v in vals.split("/"))
        return {"stop_mode": mode, "sl_value": sl, "tp_value": tp}
    if p["sl_mode"] == "pct":
        return {"stop_mode": "pct", "sl_value": p["stop_loss_pct"], "tp_value": p["take_profit_pct"]}
    return {"stop_mode": "atr", "sl_value": p["sl_atr_mult"], "tp_value": p["tp_atr_mult"]}


def _leg(row: pd.Series | dict, symbol: str | None = None) -> dict[str, Any]:
    return {"symbol": symbol or row["symbol"], "strategy": row["strategy"], "timeframe": row["tf"],
            **stops_to_params(row["stops"]), "exit_mode": row["exits"]}


def _stats(row: pd.Series) -> dict[str, Any]:
    return {k: row[k] for k in ("net_W1", "net_W2", "net_W3", "net", "ret_pct", "trades", "fees_gst", "funding",
                                 "pf", "max_dd", "win_pct", "pos_windows")}


@app.get("/api/presets")
def presets() -> JSONResponse:
    wide = step3c_wide()
    anchor = step3c_anchor()
    end = anchor["end"] if anchor else None
    common = {"days": 180, "end": end, "history_start": anchor.get("warmup_start") if anchor else None,
              "anchor": "step3c", "fee_mode": s3c.FEE_MODE, "initial_capital_inr": 10_000.0, "leverage": 10,
              "risk_per_trade_pct": 2.0, "max_daily_loss_pct": settings.MAX_DAILY_LOSS_PCT,
              "trail_atr_mult": s3c.TRAIL_ATR, "trigger_source": "trade", "cooldown_bars": 1,
              "slippage_bps": 0.0, "trend_filter_4h": False, "cost_gate": False, "strict_lot_cap": False}
    out = []
    three = wide[wide["passes"] & (wide["pos_windows"] == 3)].sort_values("net", ascending=False)
    for _, r in three.iterrows():
        label = f"{r['symbol']} {r['strategy']} {r['tf']} {r['stops']} {r['exits']}"
        out.append({"id": "3of3:" + label.replace(" ", "_"), "group": "Won 3/3 windows (in-sample selection)",
                    "tag": TAG_IN_SAMPLE, "label": label, "kind": "single", "legs": [_leg(r)], "stats": _stats(r), **common})

    syms = settings.SYMBOLS
    by_key = wide.set_index(["symbol", *s3c.UKEY])

    def unified(key: tuple, pid: str, label: str, note: str, tag: str) -> dict[str, Any]:
        rows = {s: by_key.loc[(s, *key)] for s in syms}
        return {"id": pid, "group": "Shared ₹10,000 portfolios", "tag": tag, "label": label, "kind": "portfolio", "note": note,
                "legs": [_leg({"strategy": key[0], "tf": key[1], "stops": key[2], "exits": key[3]}, s) for s in syms],
                "standalone_stats": {s: _stats(r) for s, r in rows.items()}, **common}

    key_a, note_a = s3c.select_mode_a(wide, syms)
    out.append(unified(key_a, "modeA", f"Mode A unified: {' '.join(key_a)}", note_a + " (in-sample selection)", TAG_IN_SAMPLE))
    key_awf, note_awf = s3c.select_mode_a_wf(wide, syms)
    out.append(unified(key_awf, "modeA_WF", f"Mode A-WF unified: {' '.join(key_awf)}",
                       note_awf + " (chosen on W1+W2; W3 out-of-sample)", TAG_WALK_FORWARD))
    picks = s3c.select_mode_b(wide, syms)
    out.append({"id": "modeB", "group": "Shared ₹10,000 portfolios", "tag": TAG_WALK_FORWARD, "kind": "portfolio",
                "label": "Mode B specialists: " + ", ".join(f"{s} {r['strategy']} {r['tf']}" for s, (r, _) in picks.items()),
                "note": "each symbol chosen on W1+W2; W3 out-of-sample",
                "legs": [_leg(r) for r, _ in picks.values()],
                "standalone_stats": {s: _stats(r) for s, (r, _) in picks.items()}, **common})
    out.extend(step4b_presets())
    return JSONResponse(clean({"anchor": anchor, "step4b_anchor": step4b_anchor(), "presets": out}))


def step4b_presets() -> list[dict[str, Any]]:
    """Frozen Step 4B ablation variants at ₹50,000 (primary screen: 1% risk, all-taker, 5 bps/fill)."""
    a4 = step4b_anchor()
    if not a4:
        return []
    stats: dict[tuple[str, str], dict[str, Any]] = {}
    if STEP4B_CSV.exists():
        df = pd.read_csv(STEP4B_CSV)
        prim = df[(df["window"] == "full_extended") & (df["risk_pct"] == 1.0) & (df["trigger"] == "trade")
                  & (df["slippage_bps"] == 5.0) & (df["kind"] == "variant")]
        for _, r in prim.iterrows():
            stats[(r["symbol"], r["variant"])] = {k: r[k] for k in ("net_pnl_inr", "net_return_pct", "profit_factor",
                                                                     "max_drawdown_pct", "total_trades", "verdict")}
    out = []
    for sym in settings.SYMBOLS:
        for variant, (trend, gate) in STEP4B_VARIANTS.items():
            out.append({
                "id": f"4b:{sym}:{variant}", "group": "Step 4B ablation variants (₹50,000)", "tag": TAG_STEP4B,
                "label": f"{sym} {variant} — bollinger_volume_breakout 1h atr 2x/5x trail · 5 bps",
                "kind": "single", "note": "frozen spec; screened on the full extended history (retrospective)",
                "legs": [{"symbol": sym, "strategy": "bollinger_volume_breakout", "timeframe": "1h",
                          "stop_mode": "atr", "sl_value": 2.0, "tp_value": 5.0, "exit_mode": "trail"}],
                "days": a4["days"], "end": a4["end"], "history_start": a4["warmup_start"], "anchor": "step4b",
                "fee_mode": "taker", "initial_capital_inr": 50_000.0, "leverage": 10, "risk_per_trade_pct": 1.0,
                "max_daily_loss_pct": settings.MAX_DAILY_LOSS_PCT, "trail_atr_mult": 2.5, "trigger_source": "trade",
                "cooldown_bars": 1, "slippage_bps": 5.0, "trend_filter_4h": trend, "cost_gate": gate,
                "strict_lot_cap": True, "step4b_stats": stats.get((sym, variant)),
            })
    return out


@app.get("/api/leaderboard")
def leaderboard(
    source: Literal["step3c", "step3b"] = "step3c",
    symbol: str | None = None,
    strategy: str | None = None,
    tf: str | None = None,
    passing_only: bool = False,
    min_trades: int = 0,
    sort: str = "net",
    desc: bool = True,
    limit: int = Query(200, ge=1, le=5000),
) -> JSONResponse:
    if source == "step3c":
        df = step3c_wide()
        cols = ["symbol", "strategy", "tf", "stops", "exits", "trades", "win_pct", "net_W1", "net_W2", "net_W3",
                "pos_windows", "net", "ret_pct", "pf", "max_dd", "fees_gst", "funding", "passes"]
    else:
        df = step3b_wide()
        cols = ["symbol", "strategy", "tf", "stops", "exits", "fees", "trades", "win_pct", "net_train", "net_test",
                "net", "ret_pct", "pf", "max_dd", "fees_gst", "funding"]
    if symbol:
        df = df[df["symbol"] == check_symbol(symbol)]
    if strategy:
        df = df[df["strategy"] == strategy]
    if tf:
        df = df[df["tf"] == tf]
    if min_trades:
        df = df[df["trades"] >= min_trades]
    if passing_only and "passes" in df:
        df = df[df["passes"]]
    if sort not in cols:
        raise HTTPException(422, f"sort must be one of {cols}")
    df = df.sort_values(sort, ascending=not desc, kind="stable")
    anchor = step3c_anchor() if source == "step3c" else None
    return JSONResponse(clean({
        "source": source, "columns": cols, "total": int(len(df)), "anchor": anchor,
        "days": 180 if source == "step3c" else 90, "fee_mode_default": s3c.FEE_MODE if source == "step3c" else None,
        "rows": df[cols].head(limit).to_dict(orient="records"),
    }))


# --------------------------------------------------------------------------- #
# Custom Strategies & Saved Backtests (Step 10)
# --------------------------------------------------------------------------- #
_STARTER_STRATEGIES: list[dict[str, Any]] = [
    {
        "id": "strat_ema_cross_scalp",
        "name": "EMA Crossover Scalp",
        "symbol": "ALL",
        "timeframe": "15m",
        "rule_type": "ema_cross",
        "params": {"fast_ema": 9, "slow_ema": 21, "trend_ema": 50, "rsi_period": 14, "rsi_ob": 70, "rsi_os": 30},
        "capital_inr": 50000.0,
        "leverage": 10,
        "risk_pct": 2.0,
        "sl_type": "atr_mult",
        "sl_value": 2.0,
        "tp_type": "atr_mult",
        "tp_value": 4.0,
        "trail_mult": 2.5,
    },
    {
        "id": "strat_supertrend_trend",
        "name": "Supertrend Trend",
        "symbol": "ALL",
        "timeframe": "1h",
        "rule_type": "supertrend",
        "params": {"atr_period": 10, "atr_mult": 3.0, "ema_filter": 200, "rsi_period": 14, "adx_min": 18},
        "capital_inr": 50000.0,
        "leverage": 10,
        "risk_pct": 2.0,
        "sl_type": "atr_mult",
        "sl_value": 2.0,
        "tp_type": "atr_mult",
        "tp_value": 5.0,
        "trail_mult": 2.5,
    },
    {
        "id": "strat_bb_breakout_custom",
        "name": "Bollinger Breakout Custom",
        "symbol": "ALL",
        "timeframe": "1h",
        "rule_type": "bollinger_breakout",
        "params": {"bb_period": 20, "bb_std": 2.0, "vol_mult": 1.3, "ema_filter": 50},
        "capital_inr": 50000.0,
        "leverage": 10,
        "risk_pct": 2.0,
        "sl_type": "atr_mult",
        "sl_value": 2.0,
        "tp_type": "atr_mult",
        "tp_value": 5.0,
        "trail_mult": 2.5,
    },
]


def _load_custom_strategies() -> list[dict[str, Any]]:
    with _file_lock:
        if not STRATEGIES_FILE.exists():
            STRATEGIES_FILE.parent.mkdir(parents=True, exist_ok=True)
            STRATEGIES_FILE.write_text(json.dumps(_STARTER_STRATEGIES, indent=2), encoding="utf-8")
            return [dict(s) for s in _STARTER_STRATEGIES]
        try:
            data = json.loads(STRATEGIES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:  # noqa: BLE001
            pass
        return [dict(s) for s in _STARTER_STRATEGIES]


def _save_custom_strategies(items: list[dict[str, Any]]) -> None:
    with _file_lock:
        STRATEGIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        STRATEGIES_FILE.write_text(json.dumps(clean(items), indent=2), encoding="utf-8")


def _find_custom_strategy(identifier: str | None) -> dict[str, Any] | None:
    if not identifier:
        return None
    items = _load_custom_strategies()
    for s in items:
        if s.get("id") == identifier or s.get("name") == identifier:
            return s
    return None


def _load_saved_backtests() -> list[dict[str, Any]]:
    with _file_lock:
        if not SAVED_BACKTESTS_FILE.exists():
            SAVED_BACKTESTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SAVED_BACKTESTS_FILE.write_text("[]", encoding="utf-8")
            return []
        try:
            data = json.loads(SAVED_BACKTESTS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []


def _save_saved_backtests(items: list[dict[str, Any]]) -> None:
    with _file_lock:
        SAVED_BACKTESTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        dm._atomic_write_bytes(SAVED_BACKTESTS_FILE, json.dumps(clean(items), indent=2).encode("utf-8"))


class StrategyCreateRequest(BaseModel):
    id: str | None = None
    name: str
    symbol: str = "ALL"
    timeframe: str = "1h"
    rule_type: str = "ema_cross"
    params: dict[str, Any] = Field(default_factory=dict)
    capital_inr: float = Field(50000.0, gt=0)
    leverage: int = Field(10, ge=1, le=200)
    risk_pct: float = Field(2.0, gt=0, le=100)
    sl_type: Literal["atr_mult", "fixed_pct"] = "atr_mult"
    sl_value: float = Field(2.0, gt=0)
    tp_type: Literal["atr_mult", "rr_ratio", "fixed_pct"] = "atr_mult"
    tp_value: float = Field(5.0, gt=0)
    trail_mult: float = Field(2.5, ge=0)


class StrategyUpdateRequest(BaseModel):
    name: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    rule_type: str | None = None
    params: dict[str, Any] | None = None
    capital_inr: float | None = Field(None, gt=0)
    leverage: int | None = Field(None, ge=1, le=200)
    risk_pct: float | None = Field(None, gt=0, le=100)
    sl_type: Literal["atr_mult", "fixed_pct"] | None = None
    sl_value: float | None = Field(None, gt=0)
    tp_type: Literal["atr_mult", "rr_ratio", "fixed_pct"] | None = None
    tp_value: float | None = Field(None, gt=0)
    trail_mult: float | None = Field(None, ge=0)


@app.get("/api/strategies")
def list_strategies(symbol: str | None = None) -> JSONResponse:
    items = _load_custom_strategies()
    if symbol and symbol.strip().upper() != "ALL":
        sym = symbol.strip().upper()
        items = [s for s in items if str(s.get("symbol", "ALL")).upper() in (sym, "ALL")]
    return JSONResponse(clean({"strategies": items}))


@app.post("/api/strategies")
def create_strategy(req: StrategyCreateRequest) -> JSONResponse:
    name = req.name.strip()
    if not name:
        raise HTTPException(422, "strategy name is required")
    sym = req.symbol.strip().upper() or "ALL"
    if sym != "ALL":
        sym = check_symbol(sym)
    if req.timeframe not in TIMEFRAMES:
        raise HTTPException(422, f"timeframe must be one of {TIMEFRAMES}")
    if req.rule_type not in strategies.CUSTOM_RULE_TYPES:
        raise HTTPException(422, f"rule_type must be one of {strategies.CUSTOM_RULE_TYPES}")
    items = _load_custom_strategies()
    sid = req.id or f"strat_{uuid.uuid4().hex[:8]}"
    obj = {
        "id": sid,
        "name": name,
        "symbol": sym,
        "timeframe": req.timeframe,
        "rule_type": req.rule_type,
        "params": req.params or {},
        "capital_inr": float(req.capital_inr),
        "leverage": int(req.leverage),
        "risk_pct": float(req.risk_pct),
        "sl_type": req.sl_type,
        "sl_value": float(req.sl_value),
        "tp_type": req.tp_type,
        "tp_value": float(req.tp_value),
        "trail_mult": float(req.trail_mult),
    }
    items = [s for s in items if s.get("id") != sid]
    items.append(obj)
    _save_custom_strategies(items)
    return JSONResponse(clean(obj))


@app.put("/api/strategies/{strategy_id}")
def update_strategy(strategy_id: str, req: StrategyUpdateRequest) -> JSONResponse:
    items = _load_custom_strategies()
    target = next((s for s in items if s.get("id") == strategy_id), None)
    if target is None:
        raise HTTPException(404, f"strategy {strategy_id!r} not found")
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    if "name" in patch:
        patch["name"] = patch["name"].strip()
        if not patch["name"]:
            raise HTTPException(422, "strategy name cannot be empty")
    if "symbol" in patch:
        sym = patch["symbol"].strip().upper()
        patch["symbol"] = sym if sym == "ALL" else check_symbol(sym)
    if "timeframe" in patch and patch["timeframe"] not in TIMEFRAMES:
        raise HTTPException(422, f"timeframe must be one of {TIMEFRAMES}")
    if "rule_type" in patch and patch["rule_type"] not in strategies.CUSTOM_RULE_TYPES:
        raise HTTPException(422, f"rule_type must be one of {strategies.CUSTOM_RULE_TYPES}")
    if "params" in patch and isinstance(target.get("params"), dict):
        patch["params"] = {**target["params"], **patch["params"]}
    target.update(patch)
    _save_custom_strategies(items)
    with _cache_lock:
        _frame_cache.clear()
    return JSONResponse(clean(target))


@app.delete("/api/strategies/{strategy_id}")
def delete_strategy(strategy_id: str) -> JSONResponse:
    items = _load_custom_strategies()
    before = len(items)
    items = [s for s in items if s.get("id") != strategy_id]
    if len(items) == before:
        raise HTTPException(404, f"strategy {strategy_id!r} not found")
    _save_custom_strategies(items)
    return JSONResponse({"deleted": strategy_id, "ok": True})


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #
class LegSpec(BaseModel):
    symbol: str
    strategy: str | None = None
    timeframe: str | None = None
    stop_mode: Literal["pct", "atr"] | None = None
    sl_type: str | None = None
    sl_value: float | None = None
    tp_type: str | None = None
    tp_value: float | None = None
    exit_mode: str | None = None


class BacktestRequest(BaseModel):
    symbol: str | None = None
    symbols: list[str] = Field(default_factory=lambda: ["ETHUSD"])
    strategy: str = "bollinger_volume_breakout"
    strategy_id: str | None = None
    strategy_name: str | None = None
    rule_type: str | None = None
    params: dict[str, Any] | None = None
    timeframe: str = "1h"
    legs: list[LegSpec] | None = None  # per-symbol specialist overrides (portfolio)
    days: int | None = Field(180, ge=1, le=1000)
    period_days: int | None = None
    start: datetime | None = None
    end: datetime | None = None
    history_start: datetime | None = None  # indicator warm-up start; None = all local history
    frozen_params: bool = False
    expected_data_hashes: dict[str, str] | None = None
    capital_inr: float | None = Field(None, gt=0)
    initial_capital_inr: float = Field(settings.INITIAL_CAPITAL_INR, gt=0)
    leverage: float = Field(10, ge=1, le=200)
    risk_pct: float | None = Field(None, gt=0, le=100)
    risk_per_trade_pct: float = Field(2.0, gt=0, le=100)
    max_daily_loss_pct: float = Field(5.0, ge=0, le=100)
    stop_mode: Literal["pct", "atr"] = "atr"
    sl_type: Literal["atr_mult", "fixed_pct", "atr", "pct"] | None = None
    sl_value: float = Field(2.0, gt=0)
    tp_type: Literal["atr_mult", "rr_ratio", "fixed_pct", "atr", "pct"] | None = None
    tp_value: float = Field(5.0, gt=0)
    exit_mode: str = "trail"
    trailing_stop: bool | None = None
    trail_mult: float | None = Field(None, ge=0)
    trail_atr_mult: float = Field(2.5, ge=0)
    fee_mode: Literal["taker_maker_tp", "taker", "maker", "maker_maker"] = "taker_maker_tp"
    trigger_source: Literal["trade", "mark"] = "trade"
    cooldown_bars: int = Field(1, ge=0, le=100)
    walk_forward_windows: int = Field(3, ge=0, le=12)
    include_chart: bool = True
    # Step 4B
    slippage_bps: float = Field(0.0, ge=0, le=100)
    trend_filter_4h: bool = False
    cost_gate: bool = False
    strict_lot_cap: bool = False
    mark_fill_mode: Literal["trigger", "traded"] = "trigger"

    @field_validator("timeframe")
    @classmethod
    def _tf(cls, v: str) -> str:
        if v not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {TIMEFRAMES}")
        return v

    @field_validator("exit_mode")
    @classmethod
    def _exit(cls, v: str) -> str:
        if v not in EXIT_MODES:
            raise ValueError(f"exit_mode must be one of {EXIT_MODES}")
        return v


def _resolve_strategy_def(strategy_key: str, strategy_id: str | None, rule_type: str | None,
                          params: dict[str, Any] | None) -> tuple[str, str, dict[str, Any] | None]:
    """Return (strategy_id_or_key, display_name, custom_def_or_None)."""
    if rule_type and rule_type in strategies.CUSTOM_RULE_TYPES:
        cdef = _find_custom_strategy(strategy_id or strategy_key)
        merged_params = {**(cdef.get("params") or {} if cdef else {}), **(params or {})}
        sid = (cdef["id"] if cdef else strategy_id) or strategy_key
        sname = (cdef["name"] if cdef else None) or strategy_key
        return sid, sname, {"rule_type": rule_type, "params": merged_params}

    cdef = _find_custom_strategy(strategy_id or strategy_key)
    if cdef is not None:
        merged_params = {**(cdef.get("params") or {}), **(params or {})}
        return cdef["id"], cdef["name"], {"rule_type": cdef["rule_type"], "params": merged_params}

    if strategy_key in strategies.STRATEGIES:
        return strategy_key, strategy_key, None

    raise HTTPException(422, f"unknown strategy {strategy_key!r}")


def resolve_legs(req: BacktestRequest) -> list[dict[str, Any]]:
    eff_sl_type = req.sl_type or ("fixed_pct" if req.stop_mode == "pct" else "atr_mult")
    eff_tp_type = req.tp_type or ("fixed_pct" if eff_sl_type in ("fixed_pct", "pct") else "atr_mult")
    eff_stop_mode = "pct" if eff_sl_type in ("fixed_pct", "pct") else "atr"
    eff_exit_mode = req.exit_mode
    if req.trailing_stop is False and "trail" in eff_exit_mode:
        eff_exit_mode = "fixed"
    elif req.trailing_stop is True and "trail" not in eff_exit_mode:
        eff_exit_mode = "trail"

    base = {
        "strategy": req.strategy_id or req.strategy,
        "timeframe": req.timeframe,
        "stop_mode": eff_stop_mode,
        "sl_type": "fixed_pct" if eff_sl_type in ("fixed_pct", "pct") else "atr_mult",
        "sl_value": req.sl_value,
        "tp_type": "fixed_pct" if eff_tp_type in ("fixed_pct", "pct") else ("rr_ratio" if eff_tp_type == "rr_ratio" else "atr_mult"),
        "tp_value": req.tp_value,
        "exit_mode": eff_exit_mode,
    }
    syms = [req.symbol] if req.symbol else req.symbols
    specs = req.legs or [LegSpec(symbol=s) for s in syms]
    if not specs:
        raise HTTPException(422, "no symbols selected")
    legs, seen = [], set()
    known = app_symbols()
    for spec in specs:
        sym = check_symbol(spec.symbol)
        if sym in seen:
            raise HTTPException(422, f"duplicate symbol {sym}")
        if sym not in known:
            raise HTTPException(422, f"{sym} is not in the symbol list; add it with + Add Coin first")
        seen.add(sym)
        leg = {**base, **{k: v for k, v in spec.model_dump().items() if v is not None}, "symbol": sym}
        if req.frozen_params and req.rule_type in strategies.CUSTOM_RULE_TYPES:
            sid, sname = req.strategy_id or leg["strategy"], req.strategy_name or leg["strategy"]
            cdef = {"rule_type": req.rule_type, "params": dict(req.params or {})}
        else:
            sid, sname, cdef = _resolve_strategy_def(leg["strategy"], req.strategy_id, req.rule_type, req.params)
        leg["strategy_id"] = sid
        leg["strategy_name"] = req.strategy_name or sname
        leg["custom_def"] = cdef
        if leg["timeframe"] not in TIMEFRAMES:
            raise HTTPException(422, f"timeframe must be one of {TIMEFRAMES}")
        if leg["exit_mode"] not in EXIT_MODES:
            raise HTTPException(422, f"exit_mode must be one of {EXIT_MODES}")
        legs.append(leg)
    return legs


def leg_config(req: BacktestRequest, leg: dict[str, Any], median_atr_pct: float = 1.0) -> BacktestConfig:
    cap = float(req.capital_inr if req.capital_inr is not None else req.initial_capital_inr)
    risk = float(req.risk_pct if req.risk_pct is not None else req.risk_per_trade_pct)
    trail_m = float(req.trail_mult if req.trail_mult is not None else req.trail_atr_mult)
    sl_type = leg.get("sl_type") or ("fixed_pct" if leg["stop_mode"] == "pct" else "atr_mult")
    tp_type = leg.get("tp_type") or ("fixed_pct" if sl_type == "fixed_pct" else "atr_mult")
    sl_val = float(leg["sl_value"])
    tp_val = float(leg["tp_value"])
    stops = stop_config(sl_type, sl_val, tp_type, tp_val)

    em = leg["exit_mode"]
    cparams = (leg.get("custom_def") or {}).get("params") or {}
    be_flag = ("BE" in em) or bool(cparams.get("breakeven_at_1r", False))
    sl_min = float(cparams.get("atr_sl_min_pct", 0.35 if leg["timeframe"] in ("1m", "3m", "5m") else 0.8))
    sl_max = float(cparams.get("atr_sl_max_pct", 4.0))
    try:
        cfg = BacktestConfig(
            initial_capital_inr=cap, leverage=float(req.leverage), risk_per_trade_pct=risk,
            max_daily_loss_pct=req.max_daily_loss_pct, fee_mode=req.fee_mode, trigger_source=req.trigger_source,
            cooldown_bars=req.cooldown_bars, breakeven_at_1r=be_flag,
            trailing_atr_mult=trail_m if "trail" in em else 0.0,
            exit_on_opposite_signal="opp" in em, slippage_bps=req.slippage_bps,
            allow_min_one_lot=not req.strict_lot_cap, mark_fill_mode=req.mark_fill_mode,
            atr_sl_min_pct=sl_min, atr_sl_max_pct=sl_max, **stops,
        )
        return cfg
    except ValueError as exc:
        raise HTTPException(422, f"{leg['symbol']}: {exc}") from exc


def summary_view(r: BacktestResult) -> dict[str, Any]:
    s = r.summary
    keys = ["total_trades", "long_trades", "short_trades", "win_rate_pct", "gross_pnl_inr", "fees_gst_inr", "funding_inr",
            "net_pnl_inr", "net_return_pct", "profit_factor", "max_drawdown_pct", "final_equity_inr", "signals",
            "funding_events", "ambiguous_1m_bars", "min_lot_floor_used", "skipped_in_position", "skipped_cooldown",
            "skipped_daily_halt", "skipped_insufficient_capital", "skipped_lot_budget", "skipped_no_atr", "daily_halts",
            "slippage_inr", "risk_pct_min", "risk_pct_median", "risk_pct_max", "exposure_pct"]
    out = {k: s[k] for k in keys}
    out["exit_reason_counts"] = s["exit_reasons"]
    out["trades_by_symbol"] = r.trades["symbol"].value_counts().to_dict() if len(r.trades) else {}
    return out


def chart_payload(symbol: str, tf: str, bars: pd.DataFrame, out: pd.DataFrame, trades: pd.DataFrame,
                  start: pd.Timestamp, end: pd.Timestamp) -> dict[str, Any]:
    tf_sec = dm.TIMEFRAME_MINUTES[tf] * 60
    m = (bars.index >= start) & (bars.index < end)
    b = bars[m]
    o = out[m]
    if len(b) > 5000:
        b = b.iloc[-5000:]
        o = o.iloc[-5000:]
    t = b.index.as_unit("s").asi8
    t_list = t.tolist()
    candles = [{"time": a, "open": o_, "high": h, "low": l, "close": c}
               for a, o_, h, l, c in zip(t_list, *(b[k].tolist() for k in ("open", "high", "low", "close")))]
    volume = [{"time": a, "value": v, "up": c >= o_}
              for a, v, o_, c in zip(t_list, b["volume"].tolist(), b["open"].tolist(), b["close"].tolist())]

    overlays = []
    for col in OVERLAY_COLS:
        if col in o.columns:
            vals = o[col].to_numpy(float)
            overlays.append({"name": col, "data": [{"time": a, "value": v} for a, v in zip(t_list, vals.tolist()) if np.isfinite(v)]})
    if "supertrend" in o.columns:
        st, d = o["supertrend"].to_numpy(float), o["st_direction"].to_numpy(float)
        for name, sign in (("supertrend_up", 1.0), ("supertrend_down", -1.0)):
            data = [{"time": a, "value": v} if (dd == sign and np.isfinite(v)) else {"time": a}
                    for a, v, dd in zip(t_list, st.tolist(), d.tolist())]
            overlays.append({"name": name, "data": data})

    markers = []
    if len(trades) and len(t):
        min_t = b.index[0]
        def snap(x: pd.Timestamp) -> int:
            k = int(np.searchsorted(t, (ts(x) // tf_sec) * tf_sec, side="right")) - 1
            return int(t[max(k, 0)])
        for tr in trades.itertuples():
            if tr.exit_time < min_t:
                continue
            long = tr.side == "LONG"
            markers.append({"time": snap(tr.entry_time), "position": "belowBar" if long else "aboveBar",
                            "shape": "arrowUp" if long else "arrowDown", "color": "#2dd4bf" if long else "#f87171",
                            "text": f"{'L' if long else 'S'}#{tr.trade_id}", "trade_id": int(tr.trade_id), "kind": "entry"})
            win = tr.net_pnl_usd > 0
            markers.append({"time": snap(tr.exit_time), "position": "aboveBar" if long else "belowBar",
                            "shape": "circle", "color": "#4ade80" if win else "#fb923c",
                            "text": f"{tr.exit_reason}#{tr.trade_id}", "trade_id": int(tr.trade_id), "kind": "exit"})
        markers.sort(key=lambda x: (x["time"], x["kind"] != "exit"))
    return {"symbol": symbol, "timeframe": tf, "tf_sec": tf_sec, "candles": candles, "volume": volume,
            "overlays": overlays, "markers": markers}


def equity_payload(equity: pd.Series) -> dict[str, Any]:
    if equity.empty:
        return {"equity": [], "drawdown": []}
    peak = equity.cummax()
    dd = (equity / peak - 1.0) * 100.0
    sec = equity.index.as_unit("s").asi8
    span = int(sec[-1] - sec[0]) or 60
    bucket = max(60, math.ceil(span / MAX_EQUITY_POINTS / 60) * 60)
    g = sec // bucket
    eq_last = pd.Series(equity.to_numpy(), index=g).groupby(level=0).last()
    dd_min = pd.Series(dd.to_numpy(), index=g).groupby(level=0).min()
    times = (eq_last.index.to_numpy() * bucket).tolist()
    return {
        "bucket_sec": bucket,
        "equity": [{"time": a, "value": round(v, 4)} for a, v in zip(times, eq_last.tolist())],
        "drawdown": [{"time": a, "value": round(v, 4)} for a, v in zip(times, dd_min.tolist())],
    }


def trades_payload(trades: pd.DataFrame, rate: float) -> list[dict[str, Any]]:
    if trades.empty:
        return []
    t = trades.copy()
    for c in ("gross_pnl", "fees", "funding", "entry_fee", "exit_fee", "gst"):
        t[f"{c}_inr"] = t[f"{c}_usd"] * rate
    for c in ("signal_bar", "entry_time", "exit_time"):
        t[f"{c}_ts"] = t[c].map(ts)
        t[c] = t[c].map(lambda x: x.isoformat())
    return t.to_dict(orient="records")


@app.post("/api/backtest/run")
def run_backtest(req: BacktestRequest) -> JSONResponse:
    t0 = time.perf_counter()
    if req.period_days and not req.start:
        req.days = req.period_days
    legs_spec = resolve_legs(req)
    history_start = parse_utc(req.history_start)
    data = {leg["symbol"]: get_history(leg["symbol"], history_start) for leg in legs_spec}

    data_end = min(df.index[-1] for df in data.values()) + pd.Timedelta(minutes=1)
    end = parse_utc(req.end) or data_end
    end = min(end, data_end)
    if req.start is not None:
        start = parse_utc(req.start)
    elif req.days:
        start = end - pd.Timedelta(days=req.days)
    else:
        raise HTTPException(422, "provide days or start")
    data_start = max(df.index[0] for df in data.values())
    if start < data_start:
        raise HTTPException(422, f"start {start} is before local data ({data_start}); sync more history")
    if start >= end:
        raise HTTPException(422, "empty window: start must be before end")

    fingerprints = {sym: run_store.fingerprint(df[df.index < end]) for sym, df in data.items()}
    if req.expected_data_hashes is not None:
        if {s: f["sha256"] for s, f in fingerprints.items()} != req.expected_data_hashes:
            raise HTTPException(422, "saved run source data changed; run a new backtest instead of an exact replay")
    legs, frames, filter_stats, configs = [], {}, {}, {}
    for spec in legs_spec:
        sym = spec["symbol"]
        bars, out, atr14 = get_frame(sym, spec["strategy"], spec["timeframe"], history_start, spec.get("custom_def"))
        configs[sym] = leg_config(req, spec)
        signal = out["signal"]
        if req.trend_filter_4h or req.cost_gate:
            masks = filter_masks(sym, spec["timeframe"], bars, history_start, req.trend_filter_4h, req.cost_gate,
                                 req.slippage_bps)
            tf_delta = pd.Timedelta(minutes=dm.TIMEFRAME_MINUTES[spec["timeframe"]])
            in_win = ((bars.index + tf_delta >= start) & (bars.index + tf_delta < end))
            signal, st = strategies.apply_entry_filters(out, masks, count_mask=in_win)
            filter_stats[sym] = st
        frames[sym] = (spec, bars, out.assign(signal=signal))
        exits = out["exit_signal"] if "exit_signal" in out.columns else None
        legs.append(Leg(ProductSpec.from_metadata(sym), data[sym], signal, spec["timeframe"],
                        atr14, spec["strategy_name"], exits, configs[sym]))

    base_cfg = configs[legs_spec[0]["symbol"]]
    try:
        result = Backtester(base_cfg).run_portfolio(legs, start, end)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    errors = audit_result(result)

    windows = []
    k = req.walk_forward_windows
    if k >= 2:
        edges = [start + (end - start) * i / k for i in range(k + 1)]
        for i in range(k):
            a, b = edges[i], edges[i + 1]
            r = Backtester(base_cfg).run_portfolio(legs, a, b)
            e = audit_result(r)
            windows.append({"name": f"W{i + 1}", "start": a.isoformat(), "end": b.isoformat(),
                            "days": round((b - a) / pd.Timedelta(days=1), 2), "summary": summary_view(r),
                            "audit_passed": not e, "audit_errors": e[:5]})

    charts = {}
    if req.include_chart:
        for sym, (spec, bars, out) in frames.items():
            tr = result.trades[result.trades["symbol"] == sym] if len(result.trades) else result.trades
            charts[sym] = chart_payload(sym, spec["timeframe"], bars, out, tr, start, end)

    leg0 = legs_spec[0]
    cdef0 = leg0.get("custom_def") or {}
    clean_legs = [{k: v for k, v in sp.items() if k != "custom_def"} for sp in legs_spec]
    anchor = step3c_anchor()
    payload = {
        "window": {"start": start.isoformat(), "end": end.isoformat(),
                   "days": round((end - start) / pd.Timedelta(days=1), 4),
                   "data_end": data_end.isoformat(),
                   "pinned_to_step3c": bool(anchor and parse_utc(anchor["end"]) == end)},
        "legs": clean_legs,
        "config": {
            "initial_capital_inr": base_cfg.initial_capital_inr, "capital_inr": base_cfg.initial_capital_inr,
            "leverage": base_cfg.leverage, "risk_per_trade_pct": base_cfg.risk_per_trade_pct,
            "risk_pct": base_cfg.risk_per_trade_pct, "max_daily_loss_pct": req.max_daily_loss_pct,
            "sl_type": leg0["sl_type"], "sl_value": leg0["sl_value"],
            "tp_type": leg0["tp_type"], "tp_value": leg0["tp_value"],
            "strategy_id": leg0["strategy_id"], "strategy_name": leg0["strategy_name"],
            "rule_type": cdef0.get("rule_type"), "params": cdef0.get("params") or {},
            "fee_mode": req.fee_mode, "trigger_source": req.trigger_source, "cooldown_bars": req.cooldown_bars,
            "trail_atr_mult": base_cfg.trailing_atr_mult, "usd_inr_rate": base_cfg.usd_inr_rate, "gst_rate": base_cfg.gst_rate,
        },
        "summary": summary_view(result),
        "filters": {"trend_filter_4h": req.trend_filter_4h, "cost_gate": req.cost_gate,
                    "slippage_bps": req.slippage_bps, "strict_lot_cap": req.strict_lot_cap,
                    "history_start": history_start.isoformat() if history_start is not None else None,
                    "stats": filter_stats},
        "audit": {"passed": not errors, "errors": errors[:20], "trades_checked": int(len(result.trades))},
        "windows": windows,
        "charts": charts,
        "equity": equity_payload(result.equity),
        "trades": trades_payload(result.trades, base_cfg.usd_inr_rate),
        "elapsed_ms": round((time.perf_counter() - t0) * 1000),
    }
    # Completed result is the authority for later save/deploy, not the editable form.
    request = req.model_dump(mode="json")
    request.update(start=start.isoformat(), end=end.isoformat(), days=None,
                   history_start=min(df.index[0] for df in data.values()).isoformat(),
                   frozen_params=True, rule_type=cdef0.get("rule_type"), params=cdef0.get("params") or {},
                   expected_data_hashes={sym: fp["sha256"] for sym, fp in fingerprints.items()})
    evidence = clean({
        "schema_version": 2, "result": {k: v for k, v in payload.items() if k != "charts"},
        "request": request, "effective_legs": legs_spec,
        "leg_configs": {sym: asdict(cfg) for sym, cfg in configs.items()},
        "history_starts": {sym: df.index[0].isoformat() for sym, df in data.items()},
        "products": {sym: asdict(prod) for sym, prod in result.products.items()},
        "data_fingerprints": fingerprints,
        "code_fingerprints": run_store.code_fingerprint(),
        "funding": result.funding.to_dict(orient="records"),
        "equity_full": [{"time": t.isoformat(), "equity_inr": float(v)} for t, v in result.equity.items()],
    })
    payload["run_id"] = run_store.put(evidence)
    return JSONResponse(clean(payload))


class SaveResultRequest(BaseModel):
    run_id: str | None = None
    id: str | None = None
    custom_name: str | None = None
    notes: str | None = None
    strategy_id: str | None = None
    strategy_name: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    period_days: float | None = None
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
    strict_lot_cap: bool | None = None
    rule_type: str | None = None
    params: dict[str, Any] | None = None
    net_pnl_inr: float | None = None
    return_pct: float | None = None
    win_rate_pct: float | None = None
    profit_factor: float | str | None = None
    max_dd_pct: float | None = None
    total_trades: int | None = None
    trades: list[dict[str, Any]] | None = None
    summary: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    window: dict[str, Any] | None = None
    filters: dict[str, Any] | None = None


@app.post("/api/backtest/save_result")
def save_backtest_result(req: SaveResultRequest) -> JSONResponse:
    evidence = None
    if req.run_id:
        try:
            evidence = run_store.get(req.run_id)
        except (ValueError, OSError, EOFError) as exc:
            raise HTTPException(422, f"backtest run unavailable: {exc}") from exc
        r = evidence["result"]
        if not r["audit"]["passed"]:
            raise HTTPException(422, "cannot save a failed-audit run; fix and rerun")
        if len(r["legs"]) != 1:
            raise HTTPException(422, "save/deploy currently requires a single-symbol run")
        leg = r["legs"][0]
        req = SaveResultRequest(run_id=req.run_id, id=req.id, custom_name=req.custom_name,
                                notes=req.notes, symbol=leg["symbol"], timeframe=leg["timeframe"],
                                config=r["config"], window=r["window"], filters=r["filters"],
                                use_4h_trend=r["filters"]["trend_filter_4h"],
                                summary=r["summary"], trades=r["trades"])
    s = req.summary or {}
    c = req.config or {}
    w = req.window or {}
    f = req.filters or {}
    rid = req.id or f"bt_{uuid.uuid4().hex[:10]}"
    sym = (req.symbol or "ETHUSD").strip().upper()
    tf = req.timeframe or "1h"
    rt = req.rule_type or c.get("rule_type")
    params = req.params if req.params is not None else (c.get("params") or {})
    default_name = req.strategy_name or c.get("strategy_name") or f"{sym} {tf} {rt or 'Strategy'}"
    custom_name = (req.custom_name or req.strategy_name or default_name).strip()
    trail_m = float(req.trail_mult if req.trail_mult is not None else (c.get("trail_atr_mult") if c.get("trail_atr_mult") is not None else 2.5))
    trail_on = bool(req.trailing_stop if req.trailing_stop is not None else (trail_m > 0))
    use_4h = bool(req.use_4h_trend if req.use_4h_trend is not None else (f.get("trend_filter_4h") or bool(params.get("use_4h_trend", False))))
    use_gate = bool(req.use_cost_gate if req.use_cost_gate is not None else f.get("cost_gate", False))
    strict_lot = bool(req.strict_lot_cap if req.strict_lot_cap is not None else f.get("strict_lot_cap", False))

    entry = {
        "id": rid,
        "custom_name": custom_name,
        "strategy_name": custom_name,
        "notes": (req.notes or "").strip(),
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "strategy_id": req.strategy_id or c.get("strategy_id") or "custom",
        "symbol": sym,
        "timeframe": tf,
        "period_days": round(float(req.period_days if req.period_days is not None else (w.get("days") or 180)), 1),
        "rule_type": rt,
        "params": params,
        "capital_inr": float(req.capital_inr if req.capital_inr is not None else (c.get("capital_inr") or c.get("initial_capital_inr") or 50000.0)),
        "leverage": float(req.leverage if req.leverage is not None else (c.get("leverage") or 10)),
        "risk_pct": float(req.risk_pct if req.risk_pct is not None else (c.get("risk_pct") or c.get("risk_per_trade_pct") or 2.0)),
        "sl_type": req.sl_type or c.get("sl_type") or "atr_mult",
        "sl_value": float(req.sl_value if req.sl_value is not None else (c.get("sl_value") or 2.0)),
        "tp_type": req.tp_type or c.get("tp_type") or "atr_mult",
        "tp_value": float(req.tp_value if req.tp_value is not None else (c.get("tp_value") or 5.0)),
        "trailing_stop": trail_on,
        "trail_mult": trail_m,
        "fee_mode": req.fee_mode or c.get("fee_mode") or "taker_maker_tp",
        "slippage_bps": float(req.slippage_bps if req.slippage_bps is not None else (f.get("slippage_bps") or 0.0)),
        "use_4h_trend": use_4h,
        "use_cost_gate": use_gate,
        "strict_lot_cap": strict_lot,
        "net_pnl_inr": float(req.net_pnl_inr if req.net_pnl_inr is not None else (s.get("net_pnl_inr") or 0.0)),
        "return_pct": float(req.return_pct if req.return_pct is not None else (s.get("net_return_pct") or 0.0)),
        "win_rate_pct": float(req.win_rate_pct if req.win_rate_pct is not None else (s.get("win_rate_pct") or 0.0)),
        "profit_factor": req.profit_factor if req.profit_factor is not None else (s.get("profit_factor") or 0.0),
        "max_dd_pct": float(req.max_dd_pct if req.max_dd_pct is not None else (s.get("max_drawdown_pct") or 0.0)),
        "total_trades": int(req.total_trades if req.total_trades is not None else (s.get("total_trades") or 0)),
        "summary": s,
        "trades": req.trades or [],
    }
    entry["verified_run"] = evidence is not None
    if evidence is not None:
        cfg = evidence["leg_configs"][sym]
        entry.update(run_id=req.run_id, effective_config=cfg, request=evidence["request"],
                     window=w, history_start=evidence["history_starts"][sym],
                     cooldown_bars=cfg["cooldown_bars"], max_daily_loss_pct=cfg["max_daily_loss_pct"],
                     trigger_source=cfg["trigger_source"], exit_on_opposite_signal=cfg["exit_on_opposite_signal"],
                     audit=evidence["result"]["audit"], products=evidence["products"],
                     data_fingerprints=evidence["data_fingerprints"], code_fingerprints=evidence["code_fingerprints"])
    items = _load_saved_backtests()
    items = [r for r in items if r.get("id") != rid]
    items.insert(0, entry)
    _save_saved_backtests(items)
    return JSONResponse(clean(entry))


class UpdateSavedResultRequest(BaseModel):
    custom_name: str | None = None
    strategy_name: str | None = None
    notes: str | None = None


@app.put("/api/backtest/results/{result_id}")
def update_backtest_result(result_id: str, req: UpdateSavedResultRequest) -> JSONResponse:
    items = _load_saved_backtests()
    target = next((r for r in items if r.get("id") == result_id), None)
    if target is None:
        raise HTTPException(404, f"saved result {result_id!r} not found")
    new_name = (req.custom_name if req.custom_name is not None else req.strategy_name)
    if new_name is not None and new_name.strip():
        target["custom_name"] = new_name.strip()
        target["strategy_name"] = new_name.strip()
    if req.notes is not None:
        target["notes"] = req.notes.strip()
    _save_saved_backtests(items)
    return JSONResponse(clean(target))


@app.get("/api/backtest/results")
def list_backtest_results(
    symbol: str | None = None,
    strategy_name: str | None = None,
    custom_name: str | None = None,
    search: str | None = None,
    timeframe: str | None = None,
    min_win_rate: float | None = None,
    min_pf: float | None = None,
) -> JSONResponse:
    items = _load_saved_backtests()
    def _norm(v):
        v = (v or "").strip()
        return "" if (not v or v.lower().startswith("all")) else v
    symbol, timeframe = _norm(symbol), _norm(timeframe)
    q_str = (search or custom_name or strategy_name or "").strip().lower()
    out = []
    for r in items:
        if not r.get("custom_name"):
            r["custom_name"] = r.get("strategy_name") or "Custom Strategy"
        if symbol and symbol.strip().upper() not in ("", "ALL"):
            if str(r.get("symbol", "")).upper() != symbol.strip().upper():
                continue
        if q_str:
            hay = " ".join([
                str(r.get("custom_name", "")),
                str(r.get("strategy_name", "")),
                str(r.get("notes", "")),
                str(r.get("rule_type", "")),
                str(r.get("symbol", "")),
            ]).lower()
            if q_str not in hay:
                continue
        if timeframe and timeframe.strip().lower() not in ("", "all"):
            if str(r.get("timeframe", "")).lower() != timeframe.strip().lower():
                continue
        if min_win_rate is not None:
            if float(r.get("win_rate_pct") or 0.0) < float(min_win_rate):
                continue
        if min_pf is not None:
            raw_pf = r.get("profit_factor")
            pf_val = float("inf") if raw_pf == "inf" else float(raw_pf or 0.0)
            if pf_val < float(min_pf):
                continue
        out.append(r)
    return JSONResponse(clean({"results": out, "total": len(out)}))


@app.delete("/api/backtest/results/{result_id}")
def delete_backtest_result(result_id: str) -> JSONResponse:
    items = _load_saved_backtests()
    before = len(items)
    items = [r for r in items if r.get("id") != result_id]
    if len(items) == before:
        raise HTTPException(404, f"saved result {result_id!r} not found")
    _save_saved_backtests(items)
    return JSONResponse({"deleted": result_id, "ok": True})
