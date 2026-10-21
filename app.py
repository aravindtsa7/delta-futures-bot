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
from backtester import Backtester, BacktestConfig, BacktestResult, Leg, ProductSpec, audit_result
from config import PROJECT_ROOT, settings

log = logging.getLogger("app")

STATIC_DIR = PROJECT_ROOT / "static"
RESULTS_DIR = settings.data_path / "results"
APP_SYMBOLS_FILE = settings.data_path / "app_symbols.json"
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
TIMEFRAMES = ["3m", "5m", "15m", "1h", "2h", "4h"]
EXIT_MODES = ["fixed", "BE", "trail", "opp", "BE+opp", "trail+opp"]
OVERLAY_COLS = ["ema9", "ema21", "ema50", "ema55", "ema100", "ema200", "vwap", "bb_upper", "bb_mid", "bb_lower",
                "kc_upper", "kc_lower", "dc_high", "dc_low"]
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
              history_start: pd.Timestamp | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """(bars, strategy output, ATR14) from ``history_start``, cached by data version."""
    key = (symbol, strategy, tf, history_start, mtime_key(symbol))
    with _cache_lock:
        if key in _frame_cache:
            _frame_cache.move_to_end(key)
            return _frame_cache[key]
    df = get_history(symbol, history_start)
    bars = dm.resample_candles(df, tf)
    out = strategies.generate_signals(strategy, bars)
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
        "timeframes": TIMEFRAMES,
        "exit_modes": EXIT_MODES,
        "fee_modes": ["taker_maker_tp", "taker", "maker"],
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
    # Legacy Step 3C presets stay at ₹10,000 / 2% risk and pin both the window and the warm-up start.
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
# Backtest
# --------------------------------------------------------------------------- #
class LegSpec(BaseModel):
    symbol: str
    strategy: str | None = None
    timeframe: str | None = None
    stop_mode: Literal["pct", "atr"] | None = None
    sl_value: float | None = None
    tp_value: float | None = None
    exit_mode: str | None = None


class BacktestRequest(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: ["ETHUSD"])
    strategy: str = "bollinger_volume_breakout"
    timeframe: str = "1h"
    legs: list[LegSpec] | None = None  # per-symbol specialist overrides (portfolio)
    days: int | None = Field(180, ge=1, le=1000)
    start: datetime | None = None
    end: datetime | None = None
    history_start: datetime | None = None  # indicator warm-up start; None = all local history
    initial_capital_inr: float = Field(settings.INITIAL_CAPITAL_INR, gt=0)
    leverage: float = Field(10, ge=1, le=100)
    risk_per_trade_pct: float = Field(2.0, gt=0, le=100)
    max_daily_loss_pct: float = Field(5.0, ge=0, le=100)
    stop_mode: Literal["pct", "atr"] = "atr"
    sl_value: float = Field(2.0, gt=0)
    tp_value: float = Field(5.0, gt=0)
    exit_mode: str = "trail"
    trail_atr_mult: float = Field(2.5, ge=0)
    fee_mode: Literal["taker_maker_tp", "taker", "maker"] = "taker_maker_tp"
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

    @field_validator("strategy")
    @classmethod
    def _strategy(cls, v: str) -> str:
        if v not in strategies.STRATEGIES:
            raise ValueError(f"unknown strategy; choose from {list(strategies.STRATEGIES)}")
        return v

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


def resolve_legs(req: BacktestRequest) -> list[dict[str, Any]]:
    base = {"strategy": req.strategy, "timeframe": req.timeframe, "stop_mode": req.stop_mode,
            "sl_value": req.sl_value, "tp_value": req.tp_value, "exit_mode": req.exit_mode}
    specs = req.legs or [LegSpec(symbol=s) for s in req.symbols]
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
        if leg["strategy"] not in strategies.STRATEGIES:
            raise HTTPException(422, f"unknown strategy {leg['strategy']}")
        if leg["timeframe"] not in TIMEFRAMES:
            raise HTTPException(422, f"timeframe must be one of {TIMEFRAMES}")
        if leg["exit_mode"] not in EXIT_MODES:
            raise HTTPException(422, f"exit_mode must be one of {EXIT_MODES}")
        legs.append(leg)
    return legs


def leg_config(req: BacktestRequest, leg: dict[str, Any]) -> BacktestConfig:
    if leg["stop_mode"] == "pct":
        stops = dict(sl_mode="pct", stop_loss_pct=leg["sl_value"], take_profit_pct=leg["tp_value"])
    else:
        stops = dict(sl_mode="atr", sl_atr_mult=leg["sl_value"], tp_atr_mult=leg["tp_value"])
    em = leg["exit_mode"]
    try:
        return BacktestConfig(
            initial_capital_inr=req.initial_capital_inr, leverage=req.leverage, risk_per_trade_pct=req.risk_per_trade_pct,
            max_daily_loss_pct=req.max_daily_loss_pct, fee_mode=req.fee_mode, trigger_source=req.trigger_source,
            cooldown_bars=req.cooldown_bars, breakeven_at_1r="BE" in em,
            trailing_atr_mult=req.trail_atr_mult if "trail" in em else 0.0,
            exit_on_opposite_signal="opp" in em, slippage_bps=req.slippage_bps,
            allow_min_one_lot=not req.strict_lot_cap, mark_fill_mode=req.mark_fill_mode, **stops,
        )
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
    t = b.index.as_unit("s").asi8
    t_list = t.tolist()
    candles = [{"time": a, "open": o, "high": h, "low": l, "close": c}
               for a, o, h, l, c in zip(t_list, *(b[k].tolist() for k in ("open", "high", "low", "close")))]
    volume = [{"time": a, "value": v, "up": c >= o}
              for a, v, o, c in zip(t_list, b["volume"].tolist(), b["open"].tolist(), b["close"].tolist())]

    o = out[m]
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
        def snap(x: pd.Timestamp) -> int:
            k = int(np.searchsorted(t, (ts(x) // tf_sec) * tf_sec, side="right")) - 1
            return int(t[max(k, 0)])
        for tr in trades.itertuples():
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
    legs_spec = resolve_legs(req)
    configs = {leg["symbol"]: leg_config(req, leg) for leg in legs_spec}
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

    legs, frames, filter_stats = [], {}, {}
    for spec in legs_spec:
        sym = spec["symbol"]
        bars, out, atr14 = get_frame(sym, spec["strategy"], spec["timeframe"], history_start)
        signal = out["signal"]
        if req.trend_filter_4h or req.cost_gate:
            masks = filter_masks(sym, spec["timeframe"], bars, history_start, req.trend_filter_4h, req.cost_gate,
                                 req.slippage_bps)
            # Entry fills at bar close (t + tf), so count signal bars whose fill lands inside [start, end).
            tf_delta = pd.Timedelta(minutes=dm.TIMEFRAME_MINUTES[spec["timeframe"]])
            in_win = ((bars.index + tf_delta >= start) & (bars.index + tf_delta < end))
            signal, st = strategies.apply_entry_filters(out, masks, count_mask=in_win)
            filter_stats[sym] = st
        frames[sym] = (spec, bars, out.assign(signal=signal))
        exits = out["exit_signal"] if "exit_signal" in out.columns else None
        legs.append(Leg(ProductSpec.from_metadata(sym), data[sym], signal, spec["timeframe"],
                        atr14, spec["strategy"], exits, configs[sym]))

    base_cfg = configs[legs_spec[0]["symbol"]]
    result = Backtester(base_cfg).run_portfolio(legs, start, end)
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

    anchor = step3c_anchor()
    payload = {
        "window": {"start": start.isoformat(), "end": end.isoformat(),
                   "days": round((end - start) / pd.Timedelta(days=1), 4),
                   "data_end": data_end.isoformat(),
                   "pinned_to_step3c": bool(anchor and parse_utc(anchor["end"]) == end)},
        "legs": legs_spec,
        "config": {
            "initial_capital_inr": req.initial_capital_inr, "leverage": req.leverage,
            "risk_per_trade_pct": req.risk_per_trade_pct, "max_daily_loss_pct": req.max_daily_loss_pct,
            "fee_mode": req.fee_mode, "trigger_source": req.trigger_source, "cooldown_bars": req.cooldown_bars,
            "trail_atr_mult": req.trail_atr_mult, "usd_inr_rate": base_cfg.usd_inr_rate, "gst_rate": base_cfg.gst_rate,
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
    return JSONResponse(clean(payload))
