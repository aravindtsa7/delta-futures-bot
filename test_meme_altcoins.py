"""Download 60d 1m History for 5 Meme/Altcoins & Discover Top High-Profit Strategies.

Target 5 Meme & Small-Value Altcoins on Delta Exchange India:
  DOGEUSD, XRPUSD, SUIUSD, SOLUSD, ADAUSD (fallback: PEPEUSD / SHIBUSD / AVAXUSD / LINKUSD)

1. Fetches & saves metadata (`data/metadata/{symbol}.json`) and registers all 5 coins in `data/app_symbols.json`.
2. Downloads 60 days of 1m history (`TRADE`, `MARK`, `FUNDING`) into `data/raw_1m/` if not present.
3. Evaluates `meme_liq_snapback`, `meme_pump_ignition`, `alt_ema_wave`, `bb_rsi_snapback`,
   `ema_pullback_scalp`, `bb_squeeze_quick_tp`, and `scalp_ema_ribbon` across `5m`, `15m`, `30m`, `45m`
   at ₹50,000 (15x) and ₹1,00,000 (20x, capped at product max leverage) under `maker_maker` and `taker_maker_tp`.
4. Selects #1 and #2 winners per coin (Half-1 + Half-2 walk-forward, high WR%, PF >= 1.25, low DD%),
   prints the 5-Coin & Combined Portfolio summary tables, and auto-registers into
   `data/strategies.json` and `data/saved_backtests.json`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
import requests

import backtester as bt
from config import settings
import data_manager as dm
import strategies as st


PRIMARY_TARGETS = ["DOGEUSD", "XRPUSD", "SUIUSD", "SOLUSD", "ADAUSD"]
FALLBACK_TARGETS = ["PEPEUSD", "SHIBUSD", "AVAXUSD", "LINKUSD"]
DOWNLOAD_DAYS = 60

# Cache _closed_4h_regime per (id(bars), ema_custom) for fast multi-TF evaluation
_orig_closed_4h = st._closed_4h_regime
_reg4_cache: dict[tuple[int, int], pd.DataFrame] = {}


def _cached_closed_4h(bars: pd.DataFrame, ema_custom: int = 100) -> pd.DataFrame:
    key = (id(bars), int(ema_custom))
    if key not in _reg4_cache:
        _reg4_cache[key] = _orig_closed_4h(bars, ema_custom)
    return _reg4_cache[key]


st._closed_4h_regime = _cached_closed_4h


def _fetch_chunk(session: requests.Session, api_sym: str, a: int, b: int) -> list[dict[str, Any]]:
    url = f"{settings.env.rest_url}/v2/history/candles"
    for attempt in range(1, 6):
        try:
            resp = session.get(
                url,
                params={"resolution": "1m", "symbol": api_sym, "start": a, "end": b},
                timeout=settings.HTTP_TIMEOUT_SECONDS,
            )
            if resp.status_code in dm.RETRYABLE_STATUS:
                time.sleep(min(1.0 * attempt, 8.0))
                continue
            resp.raise_for_status()
            body = resp.json()
            if body.get("success"):
                return body.get("result") or []
        except Exception:
            if attempt == 5:
                raise
            time.sleep(min(0.8 * attempt, 5.0))
    return []


def fast_download_series(symbol: str, series_type: dm.SeriesType, days: int = DOWNLOAD_DAYS) -> int:
    """Download `days` of 1m data for (symbol, series_type) using concurrent chunk requests."""
    path = dm.raw_1m_path(symbol, series_type)
    existing = dm.load_raw_1m(symbol, series_type)
    min_rows_needed = int((days - 3) * 1440)
    if len(existing) >= min_rows_needed:
        return len(existing)

    req_end = dm._last_closed_minute()
    req_start = req_end + dm.MINUTE - days * 86_400
    chunk_start = req_start - req_start % dm.FUNDING_INTERVAL_SECONDS
    chunks: list[tuple[int, int]] = []
    while chunk_start <= req_end:
        chunk_end = chunk_start + dm.CHUNK_SECONDS - dm.MINUTE
        chunks.append((chunk_start, chunk_end))
        chunk_start += dm.CHUNK_SECONDS

    api_sym = dm.api_symbol(symbol, series_type)
    session = requests.Session()
    session.headers.update(settings.http_headers())

    frames: list[pd.DataFrame] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = {pool.submit(_fetch_chunk, session, api_sym, a, b): (a, b) for a, b in chunks}
        for fut in as_completed(futs):
            rows = fut.result()
            if rows:
                frames.append(pd.DataFrame.from_records(rows))

    if frames:
        df_new = dm._normalise_raw(pd.concat(frames, ignore_index=True))
        df_new = df_new[(df_new["time"] >= req_start) & (df_new["time"] <= req_end)]
        merged = dm._merge(existing, df_new)
    else:
        merged = existing

    # Repair any small gaps quickly
    gaps = dm.find_gaps(merged["time"]).gaps[:40]
    for gap in gaps:
        g_start, g_end = gap[0], gap[1]
        q_start = g_start - g_start % dm.FUNDING_INTERVAL_SECONDS - dm.FUNDING_INTERVAL_SECONDS
        rows = _fetch_chunk(session, api_sym, q_start, g_end)
        if rows:
            merged = dm._merge(merged, dm._normalise_raw(pd.DataFrame.from_records(rows)))

    dm._atomic_write_parquet(merged, path)
    return len(merged)


def resolve_and_sync_5_coins() -> list[str]:
    selected: list[str] = []
    candidates = PRIMARY_TARGETS + [c for c in FALLBACK_TARGETS if c not in PRIMARY_TARGETS]
    for sym in candidates:
        if len(selected) >= 5:
            break
        mpath = dm.metadata_path(sym)
        if mpath.exists():
            meta = json.loads(mpath.read_text(encoding="utf-8"))
        else:
            try:
                meta = dm.fetch_and_save_metadata([sym])[sym]
            except Exception as exc:
                print(f"  [skip] {sym} metadata lookup failed: {exc}")
                continue
        if meta.get("contract_type") == "perpetual_futures" and meta.get("state") == "live":
            selected.append(sym)

    print(f"Selected 5 Meme/Altcoins: {selected}")
    for sym in ["BNBUSD", *selected]:
        dm.add_app_symbol(sym)

    for sym in selected:
        t0 = time.perf_counter()
        counts = {}
        for st_type in dm.SERIES_TYPES:
            counts[st_type] = fast_download_series(sym, st_type, days=DOWNLOAD_DAYS)
        print(f"  [sync] {sym}: TRADE={counts['TRADE']} MARK={counts['MARK']} FUNDING={counts['FUNDING']} rows ({time.perf_counter() - t0:.1f}s)")
    return selected


CANDIDATES = [
    # --- 1. meme_liq_snapback (Liquidation Wick / Stop-Hunt Reversal) ---
    ("meme_liq_snapback", "LiqWick BB20 RSI28", {"bb_period": 20, "bb_std": 2.0, "dc_period": 15, "rsi_period": 7, "rsi_os": 28, "rsi_ob": 72, "wick_ratio": 0.35, "ema_filter": 200, "adx_max": 45, "cost_gate_mult": 1.5, "use_4h_trend": 1}),
    ("meme_liq_snapback", "LiqWick BB20 Deep RSI24", {"bb_period": 20, "bb_std": 2.2, "dc_period": 20, "rsi_period": 7, "rsi_os": 24, "rsi_ob": 76, "wick_ratio": 0.30, "ema_filter": 100, "adx_max": 48, "cost_gate_mult": 1.4, "use_4h_trend": 1}),
    ("meme_liq_snapback", "LiqWick Fast RSI30", {"bb_period": 18, "bb_std": 1.9, "dc_period": 12, "rsi_period": 7, "rsi_os": 30, "rsi_ob": 70, "wick_ratio": 0.35, "ema_filter": 100, "adx_max": 42, "cost_gate_mult": 1.3, "use_4h_trend": 0}),
    ("meme_liq_snapback", "LiqWick Pure StopHunt", {"bb_period": 20, "bb_std": 2.1, "dc_period": 15, "rsi_period": 6, "rsi_os": 26, "rsi_ob": 74, "wick_ratio": 0.40, "ema_filter": 0, "adx_max": 45, "cost_gate_mult": 1.4, "use_4h_trend": 1}),

    # --- 2. meme_pump_ignition (Dormant Squeeze + Volume Pump Breakout) ---
    ("meme_pump_ignition", "PumpIgnite Vol2.0x DC15", {"bb_period": 20, "bb_std": 2.0, "dc_period": 15, "vol_mult": 2.0, "body_ratio": 0.50, "adx_min": 20, "bbw_max": 0.09, "squeeze_q": 0.65, "ema_filter": 50, "cost_gate_mult": 1.6, "use_4h_trend": 1}),
    ("meme_pump_ignition", "PumpIgnite Vol2.4x DC20", {"bb_period": 20, "bb_std": 2.0, "dc_period": 20, "vol_mult": 2.4, "body_ratio": 0.52, "adx_min": 22, "bbw_max": 0.10, "squeeze_q": 0.70, "ema_filter": 50, "cost_gate_mult": 1.8, "use_4h_trend": 1}),
    ("meme_pump_ignition", "PumpIgnite Vol1.8x Fast", {"bb_period": 18, "bb_std": 1.9, "dc_period": 12, "vol_mult": 1.8, "body_ratio": 0.48, "adx_min": 18, "bbw_max": 0.10, "squeeze_q": 0.75, "ema_filter": 50, "cost_gate_mult": 1.4, "use_4h_trend": 0}),

    # --- 3. alt_ema_wave (High-Beta EMA 9/21/50 + VWAP Wave Surfer) ---
    ("alt_ema_wave", "EMA9/21/50 VWAP Wave", {"fast_ema": 9, "mid_ema": 21, "slow_ema": 50, "adx_min": 18, "pullback_tol": 0.003, "rsi_long_min": 42, "rsi_long_max": 65, "rsi_short_min": 35, "rsi_short_max": 58, "vol_mult": 1.0, "cost_gate_mult": 1.5, "use_4h_trend": 1}),
    ("alt_ema_wave", "EMA8/21/55 Tight Wave", {"fast_ema": 8, "mid_ema": 21, "slow_ema": 55, "adx_min": 20, "pullback_tol": 0.0025, "rsi_long_min": 44, "rsi_long_max": 66, "rsi_short_min": 34, "rsi_short_max": 56, "vol_mult": 1.15, "cost_gate_mult": 1.6, "use_4h_trend": 1}),
    ("alt_ema_wave", "EMA9/21/50 Fast Wave", {"fast_ema": 9, "mid_ema": 21, "slow_ema": 50, "adx_min": 16, "pullback_tol": 0.0035, "rsi_long_min": 40, "rsi_long_max": 66, "rsi_short_min": 34, "rsi_short_max": 60, "vol_mult": 0.9, "cost_gate_mult": 1.3, "use_4h_trend": 0}),

    # --- 4. Proven Harvesters (bb_rsi_snapback, ema_pullback_scalp, bb_squeeze_quick_tp, scalp_ema_ribbon) ---
    ("bb_rsi_snapback", "BB+RSI Snapback", {"bb_period": 20, "bb_std": 2.0, "rsi_period": 14, "rsi_os": 34, "rsi_ob": 66, "ema_filter": 200, "adx_max": 42, "cost_gate_mult": 1.6, "use_4h_trend": 1}),
    ("ema_pullback_scalp", "EMA20/50 Pullback", {"fast_ema": 20, "slow_ema": 50, "adx_min": 18, "pullback_tol": 0.0025, "cost_gate_mult": 1.6, "use_4h_trend": 1}),
    ("bb_squeeze_quick_tp", "BB Squeeze QuickTP", {"bb_period": 20, "bb_std": 2.0, "adx_min": 20, "vol_mult": 1.25, "body_ratio": 0.45, "cost_gate_mult": 1.8, "use_4h_trend": 1}),
    ("scalp_ema_ribbon", "Ribbon 8/13/21/55", {"ema1": 8, "ema2": 13, "ema3": 21, "ema4": 55, "macro_ema": 200, "adx_min": 20, "vol_mult": 1.35, "cost_gate_mult": 1.6, "use_4h_trend": 1, "breakout_bars": 8}),
]

EXITS_BY_TF = {
    "5m": [
        {"sl_type": "fixed_pct", "sl": 0.85, "tp_type": "fixed_pct", "tp": 1.35, "trail": 0.0, "be": True,  "sl_min": 0.50, "lbl": "0.85%/1.35%+BE(Mkr)"},
        {"sl_type": "fixed_pct", "sl": 1.00, "tp_type": "fixed_pct", "tp": 1.60, "trail": 0.0, "be": False, "sl_min": 0.50, "lbl": "1.00%/1.60%(Mkr)"},
    ],
    "15m": [
        {"sl_type": "atr_mult", "sl": 2.0, "tp_type": "atr_mult", "tp": 2.4, "trail": 1.8, "be": True,  "sl_min": 0.80, "lbl": "2.0x/2.4x+BE+Tr1.8"},
        {"sl_type": "atr_mult", "sl": 2.2, "tp_type": "atr_mult", "tp": 3.0, "trail": 0.0, "be": True,  "sl_min": 0.80, "lbl": "2.2x/3.0x+BE(Mkr)"},
        {"sl_type": "fixed_pct", "sl": 1.1, "tp_type": "fixed_pct", "tp": 1.8, "trail": 0.0, "be": True, "sl_min": 0.80, "lbl": "1.1%/1.8%+BE(Mkr)"},
    ],
    "30m": [
        {"sl_type": "atr_mult", "sl": 2.0, "tp_type": "atr_mult", "tp": 2.5, "trail": 1.8, "be": True,  "sl_min": 0.80, "lbl": "2.0x/2.5x+BE+Tr1.8"},
        {"sl_type": "atr_mult", "sl": 2.2, "tp_type": "atr_mult", "tp": 3.2, "trail": 0.0, "be": True,  "sl_min": 0.80, "lbl": "2.2x/3.2x+BE(Mkr)"},
        {"sl_type": "atr_mult", "sl": 1.8, "tp_type": "atr_mult", "tp": 2.2, "trail": 1.6, "be": True,  "sl_min": 0.80, "lbl": "1.8x/2.2x+BE+Tr1.6"},
    ],
    "45m": [
        {"sl_type": "atr_mult", "sl": 2.0, "tp_type": "atr_mult", "tp": 2.6, "trail": 1.8, "be": True,  "sl_min": 0.80, "lbl": "2.0x/2.6x+BE+Tr1.8"},
        {"sl_type": "atr_mult", "sl": 2.2, "tp_type": "atr_mult", "tp": 3.4, "trail": 0.0, "be": True,  "sl_min": 0.80, "lbl": "2.2x/3.4x+BE(Mkr)"},
        {"sl_type": "atr_mult", "sl": 1.8, "tp_type": "atr_mult", "tp": 2.2, "trail": 1.6, "be": True,  "sl_min": 0.80, "lbl": "1.8x/2.2x+BE+Tr1.6"},
    ],
}


def run_eval(spec, df_1m, signal, tf, atr14, start, end, cap, lev, ex, fee_mode="maker_maker", half_mid=None):
    slip = 0.0 if fee_mode == "maker_maker" else 2.0
    if ex["sl_type"] == "fixed_pct":
        stops = dict(sl_mode="pct", stop_loss_pct=ex["sl"], take_profit_pct=ex["tp"])
    else:
        stops = dict(sl_mode="atr", sl_atr_mult=ex["sl"], tp_atr_mult=ex["tp"])
    cfg = bt.BacktestConfig(
        initial_capital_inr=cap,
        leverage=lev,
        risk_per_trade_pct=2.0,
        max_daily_loss_pct=5.0,
        fee_mode=fee_mode,
        slippage_bps=slip,
        cooldown_bars=2 if tf == "5m" else 1,
        breakeven_at_1r=ex["be"],
        trailing_atr_mult=ex["trail"],
        atr_sl_min_pct=ex["sl_min"],
        **stops,
    )
    leg = bt.Leg(spec, df_1m, signal, tf, atr14, "custom", None, cfg)
    r = bt.Backtester(cfg).run_portfolio([leg], start, end)
    s = r.summary
    pos_halves = 0
    h_nets = []
    if half_mid is not None:
        for a, b in ((start, half_mid), (half_mid, end)):
            rh = bt.Backtester(cfg).run_portfolio([leg], a, b)
            nh = float(rh.summary["net_pnl_inr"])
            h_nets.append(round(nh, 2))
            if nh > 0:
                pos_halves += 1
    return r, s, pos_halves, h_nets, leg, cfg


def main() -> None:
    t0 = time.perf_counter()
    print("=" * 124)
    print("HIGH-BETA MEME & SMALL-VALUE ALTCOIN STRATEGY DISCOVERY (5 COINS | 5m, 15m, 30m, 45m | ₹50k & ₹1L)")
    print("=" * 124)

    coins = resolve_and_sync_5_coins()
    all_rows = []
    datasets: dict[str, pd.DataFrame] = {}
    specs: dict[str, bt.ProductSpec] = {}
    max_levs: dict[str, int] = {}

    for sym in coins:
        t_sym = time.perf_counter()
        df_1m = dm.load_unified_1m_dataset(sym)
        datasets[sym] = df_1m
        spec = bt.ProductSpec.from_metadata(sym)
        specs[sym] = spec
        meta = dm.load_metadata(sym)
        max_lev = int(float(meta.get("default_leverage") or 20))
        max_levs[sym] = max_lev

        end = df_1m.index[-1]
        # Reserve first 10 days for 4h EMA warm-up so test window is ~50-80 days
        total_days = (end - df_1m.index[0]) / pd.Timedelta(days=1)
        warmup_days = min(12.0, max(5.0, total_days * 0.18))
        start = df_1m.index[0] + pd.Timedelta(days=warmup_days)
        eval_days = round((end - start) / pd.Timedelta(days=1), 1)
        half_mid = start + (end - start) / 2

        lev50 = float(min(15, max_lev))
        lev100 = float(min(20, max_lev))

        for tf in ("5m", "15m", "30m", "45m"):
            bars = dm.resample_candles(df_1m, tf)
            atr14 = st.atr(bars, 14)
            exits = EXITS_BY_TF[tf]

            for rule_type, label, params in CANDIDATES:
                p_use = dict(params)
                if tf == "5m" and rule_type in ("scalp_ema_ribbon", "meme_liq_snapback", "alt_ema_wave"):
                    p_use["cost_gate_mult"] = max(1.8, float(p_use.get("cost_gate_mult", 1.5)))
                out = st.evaluate_custom_strategy(rule_type, p_use, bars)
                sig = out["signal"]
                in_win_sigs = int((sig[(bars.index >= start) & (bars.index < end)] != 0).sum())
                if in_win_sigs < 5 or in_win_sigs > 180:
                    continue

                for fee_mode in ("maker_maker", "taker_maker_tp"):
                    for ex in exits:
                        r50, s50, _, _, _, _ = run_eval(
                            spec, df_1m, sig, tf, atr14, start, end,
                            50000.0, lev50, ex, fee_mode=fee_mode, half_mid=None
                        )
                        net50 = float(s50["net_pnl_inr"])
                        trades = int(s50["total_trades"])
                        wr = float(s50["win_rate_pct"])
                        pf = float(s50["profit_factor"]) if np.isfinite(s50["profit_factor"]) else 9.99
                        dd = abs(float(s50["max_drawdown_pct"]))
                        if trades < 5 or net50 <= 0 or pf < 1.15:
                            continue

                        _, _, pos_h, h_nets, leg50, cfg50 = run_eval(
                            spec, df_1m, sig, tf, atr14, start, end,
                            50000.0, lev50, ex, fee_mode=fee_mode, half_mid=half_mid
                        )
                        r100, s100, _, _, leg100, cfg100 = run_eval(
                            spec, df_1m, sig, tf, atr14, start, end,
                            100000.0, lev100, ex, fee_mode=fee_mode, half_mid=None
                        )

                        is_new_meme_rule = rule_type in ("meme_liq_snapback", "meme_pump_ignition", "alt_ema_wave")
                        score = (
                            (wr / 100.0) * 45.0
                            + min(pf, 3.5) * 22.0
                            + float(s50["net_return_pct"]) * 0.75
                            + pos_h * 18.0
                            + (8.0 if is_new_meme_rule else 0.0)
                            - max(0.0, dd - 10.0) * 2.0
                        )
                        all_rows.append({
                            "symbol": sym,
                            "tf": tf,
                            "rule_type": rule_type,
                            "label": label,
                            "params": {**p_use, "breakeven_at_1r": 1 if ex["be"] else 0, "atr_sl_min_pct": ex["sl_min"]},
                            "exit": ex,
                            "fee_mode": fee_mode,
                            "lev50": int(lev50),
                            "lev100": int(lev100),
                            "trades": trades,
                            "wr": wr,
                            "pf": pf,
                            "dd50": dd,
                            "net50": net50,
                            "ret50": float(s50["net_return_pct"]),
                            "net100": float(s100["net_pnl_inr"]),
                            "ret100": float(s100["net_return_pct"]),
                            "dd100": abs(float(s100["max_drawdown_pct"])),
                            "pos_h": pos_h,
                            "h_nets": h_nets,
                            "score": score,
                            "r50": r50,
                            "r100": r100,
                            "leg50": leg50,
                            "leg100": leg100,
                            "cfg50": cfg50,
                            "cfg100": cfg100,
                            "start": start,
                            "end": end,
                            "eval_days": eval_days,
                        })
        print(f"  [{sym}] evaluated 5m/15m/30m/45m strategies in {time.perf_counter() - t_sym:.1f}s (max_lev={max_lev}x, window={eval_days}d)")

    print("\n" + "=" * 130)
    print(f"{'COIN':<8} {'RK':<3} {'TF':<4} {'ARCHETYPE / LABEL':<33} {'FEE':<9} {'EXITS':<20} {'TRD':>4} {'WIN%':>6} {'PF':>5} {'DD%':>5} {'WF':>4} {'₹50k Net (15x)':>17} {'₹1L Net (20x)':>18}")
    print("-" * 130)

    winners_by_coin: dict[str, list[dict[str, Any]]] = {}
    winners_to_register: list[dict[str, Any]] = []

    for sym in coins:
        sub = [r for r in all_rows if r["symbol"] == sym]
        sub.sort(key=lambda x: (x["pos_h"] == 2, x["pf"] >= 1.25 and x["wr"] >= 50.0 and x["dd50"] <= 12.0, x["score"]), reverse=True)
        picked = []
        seen_keys = set()
        for r in sub:
            k = (r["rule_type"], r["tf"])
            if k in seen_keys:
                continue
            seen_keys.add(k)
            picked.append(r)
            if len(picked) >= 2:
                break
        winners_by_coin[sym] = picked
        for idx, r in enumerate(picked):
            tag = f"{r['rule_type']}:{r['label']}"[:32]
            fee_lbl = "Mkr/Mkr" if r["fee_mode"] == "maker_maker" else "Tkr/MkrTP"
            print(
                f"{sym:<8} #{idx + 1:<2} {r['tf']:<4} {tag:<33} {fee_lbl:<9} {r['exit']['lbl']:<20} "
                f"{r['trades']:>4d} {r['wr']:>5.1f}% {r['pf']:>5.2f} {r['dd50']:>4.1f}% "
                f"{r['pos_h']}/2  "
                f"₹{r['net50']:>+8,.0f} ({r['ret50']:>+4.1f}%) "
                f"₹{r['net100']:>+9,.0f} ({r['ret100']:>+4.1f}%)"
            )
            winners_to_register.append((idx + 1, r))
        print("-" * 130)

    # Evaluate Combined 5-Coin Alt/Meme Portfolio (#1 winner from each coin sharing a single ₹50k / ₹1L wallet)
    top1_rows = [winners_by_coin[s][0] for s in coins if winners_by_coin.get(s)]
    if top1_rows:
        p_start = max(r["start"] for r in top1_rows)
        p_end = min(r["end"] for r in top1_rows)
        legs50 = [r["leg50"] for r in top1_rows]
        legs100 = [r["leg100"] for r in top1_rows]
        port50 = bt.Backtester(top1_rows[0]["cfg50"]).run_portfolio(legs50, p_start, p_end).summary
        port100 = bt.Backtester(top1_rows[0]["cfg100"]).run_portfolio(legs100, p_start, p_end).summary
        print(
            f"5-COIN COMBINED PORTFOLIO (Shared Wallet): "
            f"Trades={port50['total_trades']} | WinRate={port50['win_rate_pct']:.1f}% | PF={port50['profit_factor']:.2f} | "
            f"₹50k Net=₹{port50['net_pnl_inr']:+,.0f} ({port50['net_return_pct']:+.1f}%, DD {port50['max_drawdown_pct']:.1f}%) | "
            f"₹1L Net=₹{port100['net_pnl_inr']:+,.0f} ({port100['net_return_pct']:+.1f}%, DD {port100['max_drawdown_pct']:.1f}%)"
        )
        print("=" * 130)

    # Auto-register in data/strategies.json and data/saved_backtests.json
    strat_file = Path("data/strategies.json")
    saved_file = Path("data/saved_backtests.json")
    existing_strats = json.loads(strat_file.read_text(encoding="utf-8")) if strat_file.exists() else []
    existing_saved = json.loads(saved_file.read_text(encoding="utf-8")) if saved_file.exists() else []

    existing_strats = [s for s in existing_strats if not str(s.get("id", "")).startswith("meme_alt_")]
    existing_saved = [r for r in existing_saved if not str(r.get("id", "")).startswith("res_meme_alt_")]

    new_strats = []
    new_saved = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for rank, r in winners_to_register:
        sym = r["symbol"]
        tf = r["tf"]
        ex = r["exit"]
        # Register #1 at both ₹50k and ₹1L, and #2 at ₹50k so catalog is rich and clean
        tiers = [("50k", 50000.0, r["lev50"], r["r50"]), ("100k", 100000.0, r["lev100"], r["r100"])] if rank == 1 else [("50k", 50000.0, r["lev50"], r["r50"])]
        for cap_tag, cap_val, lev_val, bt_res in tiers:
            sid = f"meme_alt_{sym.lower()}_r{rank}_{tf}_{cap_tag}"
            cap_lbl = "₹50k" if cap_tag == "50k" else "₹1L"
            sname = f"💎 Meme/Alt #{rank}: {sym} {tf} {r['label']} ({cap_lbl} · {lev_val}x)"
            sobj = {
                "id": sid,
                "name": sname,
                "symbol": sym,
                "timeframe": tf,
                "rule_type": r["rule_type"],
                "params": r["params"],
                "capital_inr": cap_val,
                "leverage": lev_val,
                "risk_pct": 2.0,
                "sl_type": ex["sl_type"],
                "sl_value": ex["sl"],
                "tp_type": ex["tp_type"],
                "tp_value": ex["tp"],
                "trail_mult": ex["trail"],
                "fee_mode": r["fee_mode"],
                "slippage_bps": 0.0 if r["fee_mode"] == "maker_maker" else 2.0,
            }
            new_strats.append(sobj)

            s = bt_res.summary
            tdf = bt_res.trades
            trades_list = []
            for row in tdf.to_dict(orient="records"):
                clean_row = {}
                for k, v in row.items():
                    if isinstance(v, pd.Timestamp):
                        clean_row[k] = v.isoformat()
                    elif isinstance(v, (float, np.floating)):
                        clean_row[k] = None if np.isnan(v) or np.isinf(v) else float(v)
                    elif isinstance(v, (int, np.integer)):
                        clean_row[k] = int(v)
                    else:
                        clean_row[k] = v
                trades_list.append(clean_row)

            new_saved.append({
                "id": f"res_{sid}",
                "saved_at": now_iso,
                "strategy_id": sid,
                "strategy_name": sname,
                "symbol": sym,
                "timeframe": tf,
                "period_days": float(r["eval_days"]),
                "capital_inr": cap_val,
                "leverage": float(lev_val),
                "risk_pct": 2.0,
                "sl_type": ex["sl_type"],
                "sl_value": ex["sl"],
                "tp_type": ex["tp_type"],
                "tp_value": ex["tp"],
                "trail_mult": ex["trail"],
                "fee_mode": r["fee_mode"],
                "slippage_bps": 0.0 if r["fee_mode"] == "maker_maker" else 2.0,
                "rule_type": r["rule_type"],
                "params": r["params"],
                "net_pnl_inr": round(float(s["net_pnl_inr"]), 2),
                "return_pct": round(float(s["net_return_pct"]), 2),
                "win_rate_pct": round(float(s["win_rate_pct"]), 2),
                "profit_factor": round(float(s["profit_factor"]), 2) if np.isfinite(s["profit_factor"]) else 99.0,
                "max_dd_pct": round(float(s["max_drawdown_pct"]), 2),
                "total_trades": int(s["total_trades"]),
                "walk_forward_pos_windows": r["pos_h"],
                "walk_forward_nets_inr": r["h_nets"],
                "summary": {
                    "total_trades": int(s["total_trades"]),
                    "long_trades": int(s["long_trades"]),
                    "short_trades": int(s["short_trades"]),
                    "win_rate_pct": round(float(s["win_rate_pct"]), 2),
                    "gross_pnl_inr": round(float(s["gross_pnl_inr"]), 2),
                    "fees_gst_inr": round(float(s["fees_gst_inr"]), 2),
                    "funding_inr": round(float(s["funding_inr"]), 2),
                    "net_pnl_inr": round(float(s["net_pnl_inr"]), 2),
                    "net_return_pct": round(float(s["net_return_pct"]), 2),
                    "profit_factor": round(float(s["profit_factor"]), 3) if np.isfinite(s["profit_factor"]) else 99.0,
                    "max_drawdown_pct": round(float(s["max_drawdown_pct"]), 2),
                    "final_equity_inr": round(float(s["final_equity_inr"]), 2),
                    "exit_reason_counts": s["exit_reasons"],
                },
                "trades": trades_list,
            })

    strat_file.write_text(json.dumps(new_strats + existing_strats, indent=2), encoding="utf-8")
    saved_file.write_text(json.dumps(new_saved + existing_saved, indent=2), encoding="utf-8")
    print(f"\nRegistered {len(new_strats)} Meme/Altcoin strategies into data/strategies.json and data/saved_backtests.json.")
    print(f"Active app_symbols: {dm.app_symbols()}")
    print(f"Completed in {time.perf_counter() - t0:.1f}s.")


if __name__ == "__main__":
    main()
