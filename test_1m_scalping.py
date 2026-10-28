"""Fast 1m & 5m High-Frequency Scalping Research & Auto-Registration Script (<45s).

Evaluates the 4 new 1m/5m scalping archetypes across BTCUSD, ETHUSD, and BNBUSD:
  1. scalp_vwap_ema   (VWAP + EMA9/21/50 Micro-Pullback Scalp)
  2. scalp_rsi4_bb    (Fast RSI(4) + Bollinger Band Snapback Scalp)
  3. scalp_vol_burst  (Volume Spike + Micro-Range Ignition Scalp)
  4. scalp_ema_ribbon (EMA 8/13/21/55 Ribbon + Fast MACD(8,21,5) Impulse Scalp)

Compares `taker_maker_tp` vs `maker_maker` (Limit Entry + Limit TP @ 0.02% + 18% GST)
at ₹50,000 (15x) and ₹1,00,000 (20x) over 180 days (3 x 60d walk-forward windows),
and registers the top 1m & 5m winners into data/strategies.json & data/saved_backtests.json.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd

import backtester as bt
import data_manager as dm
import strategies as st


SYMBOLS = ["BTCUSD", "ETHUSD", "BNBUSD"]

# Cache _closed_4h_regime per (id(bars), ema_custom) for ultra-fast 1m/5m grid evaluation
_orig_closed_4h = st._closed_4h_regime
_reg4_cache: dict[tuple[int, int], pd.DataFrame] = {}


def _cached_closed_4h(bars: pd.DataFrame, ema_custom: int = 100) -> pd.DataFrame:
    key = (id(bars), int(ema_custom))
    if key not in _reg4_cache:
        _reg4_cache[key] = _orig_closed_4h(bars, ema_custom)
    return _reg4_cache[key]


st._closed_4h_regime = _cached_closed_4h


CANDIDATES_5M = [
    # --- 1. scalp_vwap_ema ---
    ("scalp_vwap_ema", "VWAP+EMA9/21 Pullback", {"fast_ema": 9, "slow_ema": 21, "trend_ema": 50, "macro_ema": 200, "rsi_period": 7, "rsi_long_min": 48, "rsi_long_max": 66, "rsi_short_min": 34, "rsi_short_max": 52, "pullback_tol": 0.0008, "adx_min": 22, "vol_mult": 1.6, "cost_gate_mult": 2.2, "rt_cost": 0.0010, "use_4h_trend": 1, "breakout_bars": 4}),
    ("scalp_vwap_ema", "VWAP+EMA12/26 Surge", {"fast_ema": 12, "slow_ema": 26, "trend_ema": 55, "macro_ema": 200, "rsi_period": 7, "rsi_long_min": 50, "rsi_long_max": 68, "rsi_short_min": 32, "rsi_short_max": 50, "pullback_tol": 0.0010, "adx_min": 25, "vol_mult": 1.8, "cost_gate_mult": 2.4, "rt_cost": 0.0010, "use_4h_trend": 1, "breakout_bars": 6}),
    ("scalp_vwap_ema", "VWAP+EMA9/21 Fast", {"fast_ema": 9, "slow_ema": 21, "trend_ema": 50, "macro_ema": 200, "rsi_period": 7, "rsi_long_min": 52, "rsi_long_max": 70, "rsi_short_min": 30, "rsi_short_max": 48, "pullback_tol": 0.0012, "adx_min": 24, "vol_mult": 1.5, "cost_gate_mult": 2.0, "rt_cost": 0.0010, "use_4h_trend": 0, "breakout_bars": 5}),

    # --- 2. scalp_rsi4_bb ---
    ("scalp_rsi4_bb", "RSI(4)+BB20 Extreme", {"bb_period": 20, "bb_std": 2.4, "rsi_period": 4, "rsi_os": 10, "rsi_ob": 90, "ema_filter": 200, "adx_max": 32, "bb_tol": 0.0, "cost_gate_mult": 2.2, "rt_cost": 0.0010, "use_4h_trend": 1}),
    ("scalp_rsi4_bb", "RSI(4)+BB25 Snapback", {"bb_period": 25, "bb_std": 2.5, "rsi_period": 4, "rsi_os": 12, "rsi_ob": 88, "ema_filter": 200, "adx_max": 35, "bb_tol": 0.0, "cost_gate_mult": 2.0, "rt_cost": 0.0010, "use_4h_trend": 1}),
    ("scalp_rsi4_bb", "RSI(4)+BB20 MeanRev", {"bb_period": 20, "bb_std": 2.3, "rsi_period": 4, "rsi_os": 14, "rsi_ob": 86, "ema_filter": 200, "adx_max": 30, "bb_tol": 0.0, "cost_gate_mult": 2.0, "rt_cost": 0.0010, "use_4h_trend": 0}),

    # --- 3. scalp_vol_burst ---
    ("scalp_vol_burst", "VolSpike 2.5x DC24", {"dc_period": 24, "vol_mult": 2.5, "body_ratio": 0.55, "adx_min": 24, "ema_filter": 100, "squeeze_q": 0.50, "cost_gate_mult": 2.2, "rt_cost": 0.0010, "use_4h_trend": 1}),
    ("scalp_vol_burst", "VolSpike 2.2x DC20", {"dc_period": 20, "vol_mult": 2.2, "body_ratio": 0.50, "adx_min": 22, "ema_filter": 50, "squeeze_q": 0.55, "cost_gate_mult": 2.0, "rt_cost": 0.0010, "use_4h_trend": 1}),
    ("scalp_vol_burst", "VolSpike 2.8x DC30", {"dc_period": 30, "vol_mult": 2.8, "body_ratio": 0.55, "adx_min": 25, "ema_filter": 100, "squeeze_q": 0.60, "cost_gate_mult": 2.4, "rt_cost": 0.0010, "use_4h_trend": 1}),
    ("scalp_vol_burst", "VolSpike 2.0x DC20 Fast", {"dc_period": 20, "vol_mult": 2.0, "body_ratio": 0.50, "adx_min": 20, "ema_filter": 100, "squeeze_q": 0.50, "cost_gate_mult": 2.0, "rt_cost": 0.0010, "use_4h_trend": 0}),

    # --- 4. scalp_ema_ribbon ---
    ("scalp_ema_ribbon", "Ribbon 8/13/21/55 Brk16", {"ema1": 8, "ema2": 13, "ema3": 21, "ema4": 55, "macro_ema": 200, "adx_min": 24, "vol_mult": 1.8, "cost_gate_mult": 2.2, "rt_cost": 0.0010, "use_4h_trend": 1, "breakout_bars": 16}),
    ("scalp_ema_ribbon", "Ribbon 8/13/21/55 Brk24", {"ema1": 8, "ema2": 13, "ema3": 21, "ema4": 55, "macro_ema": 200, "adx_min": 25, "vol_mult": 2.0, "cost_gate_mult": 2.4, "rt_cost": 0.0010, "use_4h_trend": 1, "breakout_bars": 24}),
    ("scalp_ema_ribbon", "Ribbon 9/21/34/89 Brk20", {"ema1": 9, "ema2": 21, "ema3": 34, "ema4": 89, "macro_ema": 200, "adx_min": 22, "vol_mult": 1.7, "cost_gate_mult": 2.0, "rt_cost": 0.0010, "use_4h_trend": 1, "breakout_bars": 20}),
]

CANDIDATES_1M = [
    ("scalp_vol_burst", "1m VolSpike 3.0x DC45", {"dc_period": 45, "vol_mult": 3.0, "body_ratio": 0.60, "adx_min": 28, "ema_filter": 200, "squeeze_q": 0.45, "cost_gate_mult": 2.2, "rt_cost": 0.0008, "use_4h_trend": 1}),
    ("scalp_vol_burst", "1m VolSpike 2.8x DC30", {"dc_period": 30, "vol_mult": 2.8, "body_ratio": 0.55, "adx_min": 26, "ema_filter": 200, "squeeze_q": 0.50, "cost_gate_mult": 2.0, "rt_cost": 0.0008, "use_4h_trend": 1}),
    ("scalp_ema_ribbon", "1m Ribbon 8/21/55/144 Brk30", {"ema1": 8, "ema2": 21, "ema3": 55, "ema4": 144, "macro_ema": 200, "adx_min": 26, "vol_mult": 2.2, "cost_gate_mult": 2.2, "rt_cost": 0.0008, "use_4h_trend": 1, "breakout_bars": 30}),
    ("scalp_vwap_ema", "1m VWAP+EMA9/21 Surge", {"fast_ema": 9, "slow_ema": 21, "trend_ema": 55, "macro_ema": 200, "rsi_period": 7, "rsi_long_min": 52, "rsi_long_max": 68, "rsi_short_min": 32, "rsi_short_max": 48, "pullback_tol": 0.0006, "adx_min": 26, "vol_mult": 2.2, "cost_gate_mult": 2.2, "rt_cost": 0.0008, "use_4h_trend": 1, "breakout_bars": 15}),
    ("scalp_rsi4_bb", "1m RSI(4)+BB25 Extreme", {"bb_period": 25, "bb_std": 2.6, "rsi_period": 4, "rsi_os": 8, "rsi_ob": 92, "ema_filter": 200, "adx_max": 28, "bb_tol": 0.0, "cost_gate_mult": 2.2, "rt_cost": 0.0008, "use_4h_trend": 1}),
]

# Notice: trail=0.0 lets winning scalps hit resting Limit TP (0.02% Maker fee, 0 slip)
# instead of getting choked on bar 1 by an unclamped 1m/5m trailing stop!
EXIT_PROFILES_5M = [
    {"sl_type": "fixed_pct", "sl": 0.70, "tp_type": "fixed_pct", "tp": 0.85, "trail": 0.0, "be": True,  "sl_min": 0.45, "lbl": "0.70%/0.85%+BE(Mkr)"},
    {"sl_type": "fixed_pct", "sl": 0.80, "tp_type": "fixed_pct", "tp": 1.20, "trail": 0.0, "be": True,  "sl_min": 0.45, "lbl": "0.80%/1.20%+BE(Mkr)"},
    {"sl_type": "fixed_pct", "sl": 0.90, "tp_type": "fixed_pct", "tp": 1.50, "trail": 0.0, "be": False, "sl_min": 0.45, "lbl": "0.90%/1.50%(Mkr)"},
    {"sl_type": "atr_mult",  "sl": 2.5,  "tp_type": "atr_mult",  "tp": 3.0,  "trail": 0.0, "be": True,  "sl_min": 0.55, "lbl": "2.5x/3.0x+BE(Mkr)"},
    {"sl_type": "atr_mult",  "sl": 2.2,  "tp_type": "atr_mult",  "tp": 3.5,  "trail": 0.0, "be": False, "sl_min": 0.55, "lbl": "2.2x/3.5x(Mkr)"},
]

EXIT_PROFILES_1M = [
    {"sl_type": "fixed_pct", "sl": 0.55, "tp_type": "fixed_pct", "tp": 0.75, "trail": 0.0, "be": True,  "sl_min": 0.35, "lbl": "0.55%/0.75%+BE(Mkr)"},
    {"sl_type": "fixed_pct", "sl": 0.65, "tp_type": "fixed_pct", "tp": 1.00, "trail": 0.0, "be": True,  "sl_min": 0.35, "lbl": "0.65%/1.00%+BE(Mkr)"},
    {"sl_type": "fixed_pct", "sl": 0.75, "tp_type": "fixed_pct", "tp": 1.25, "trail": 0.0, "be": False, "sl_min": 0.35, "lbl": "0.75%/1.25%(Mkr)"},
]


def run_eval(spec, df_1m, signal, tf, atr14, start, end, cap, lev, ex, fee_mode="maker_maker", wf_edges=None):
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
        cooldown_bars=5 if tf == "1m" else 2,
        breakeven_at_1r=ex["be"],
        trailing_atr_mult=ex["trail"],
        atr_sl_min_pct=ex["sl_min"],
        **stops,
    )
    leg = bt.Leg(spec, df_1m, signal, tf, atr14, "custom", None, cfg)
    r = bt.Backtester(cfg).run_portfolio([leg], start, end)
    s = r.summary
    pos_w = 0
    w_nets = []
    if wf_edges:
        for i in range(len(wf_edges) - 1):
            rw = bt.Backtester(cfg).run_portfolio([leg], wf_edges[i], wf_edges[i + 1])
            nw = float(rw.summary["net_pnl_inr"])
            w_nets.append(round(nw, 2))
            if nw > 0:
                pos_w += 1
    return r, s, pos_w, w_nets


def main() -> None:
    t0 = time.perf_counter()
    print("=" * 122)
    print("HIGH-FREQUENCY 1m & 5m SCALPING DISCOVERY (BTCUSD, ETHUSD, BNBUSD | maker_maker vs taker_maker_tp)")
    print("=" * 122)

    all_rows = []
    for sym in SYMBOLS:
        t_sym = time.perf_counter()
        df_full = dm.load_unified_1m_dataset(sym)
        end = df_full.index[-1]
        start_180 = max(df_full.index[0] + pd.Timedelta(days=15), end - pd.Timedelta(days=180))
        eval_days = round((end - start_180) / pd.Timedelta(days=1), 1)
        warmup_start = max(df_full.index[0], start_180 - pd.Timedelta(days=45))
        df_1m = df_full[df_full.index >= warmup_start].copy()
        df_1m.attrs["symbol"] = sym
        spec = bt.ProductSpec.from_metadata(sym)
        wf_edges = [start_180 + (end - start_180) * i / 3 for i in range(4)]

        for tf in ("5m", "1m"):
            bars = dm.resample_candles(df_1m, tf)
            atr14 = st.atr(bars, 14)
            exits = EXIT_PROFILES_1M if tf == "1m" else EXIT_PROFILES_5M
            cands = CANDIDATES_1M if tf == "1m" else CANDIDATES_5M

            for rule_type, label, params in cands:
                p_use = dict(params)
                out = st.evaluate_custom_strategy(rule_type, p_use, bars)
                sig = out["signal"]
                in_win_sigs = int((sig[(bars.index >= start_180) & (bars.index < end)] != 0).sum())
                if in_win_sigs < 6 or in_win_sigs > 600:
                    continue

                for ex in exits:
                    r50, s50, _, _ = run_eval(
                        spec, df_1m, sig, tf, atr14, start_180, end,
                        50000.0, 15.0, ex, fee_mode="maker_maker", wf_edges=None
                    )
                    net50 = float(s50["net_pnl_inr"])
                    trades = int(s50["total_trades"])
                    wr = float(s50["win_rate_pct"])
                    pf = float(s50["profit_factor"]) if np.isfinite(s50["profit_factor"]) else 9.99
                    dd = abs(float(s50["max_drawdown_pct"]))
                    if trades < 6 or net50 <= 0:
                        continue

                    _, _, pos_w, w_nets = run_eval(
                        spec, df_1m, sig, tf, atr14, start_180, end,
                        50000.0, 15.0, ex, fee_mode="maker_maker", wf_edges=wf_edges
                    )
                    r100, s100, _, _ = run_eval(
                        spec, df_1m, sig, tf, atr14, start_180, end,
                        100000.0, 20.0, ex, fee_mode="maker_maker", wf_edges=None
                    )
                    _, s50_taker, _, _ = run_eval(
                        spec, df_1m, sig, tf, atr14, start_180, end,
                        50000.0, 15.0, ex, fee_mode="taker_maker_tp", wf_edges=None
                    )

                    score = (
                        (wr / 100.0) * 45.0
                        + min(pf, 3.0) * 20.0
                        + float(s50["net_return_pct"]) * 0.65
                        + pos_w * 12.0
                        - max(0.0, dd - 12.0) * 1.5
                    )
                    all_rows.append({
                        "symbol": sym,
                        "tf": tf,
                        "rule_type": rule_type,
                        "label": label,
                        "params": {**p_use, "breakeven_at_1r": 1 if ex["be"] else 0, "atr_sl_min_pct": ex["sl_min"]},
                        "exit": ex,
                        "trades": trades,
                        "wr": wr,
                        "pf": pf,
                        "dd50": dd,
                        "net50_mm": net50,
                        "ret50_mm": float(s50["net_return_pct"]),
                        "fees50_mm": float(s50["fees_gst_inr"]),
                        "net50_tm": float(s50_taker["net_pnl_inr"]),
                        "fees50_tm": float(s50_taker["fees_gst_inr"]),
                        "net100_mm": float(s100["net_pnl_inr"]),
                        "ret100_mm": float(s100["net_return_pct"]),
                        "dd100": abs(float(s100["max_drawdown_pct"])),
                        "pos_w": pos_w,
                        "w_nets": w_nets,
                        "score": score,
                        "r50": r50,
                        "r100": r100,
                        "start": start_180,
                        "end": end,
                        "eval_days": eval_days,
                    })
        print(f"  [{sym}] evaluated 1m & 5m candidates in {time.perf_counter() - t_sym:.1f}s")

    print("\n" + "=" * 124)
    print(f"{'COIN':<7} {'TF':<4} {'ARCHETYPE / LABEL':<34} {'EXITS':<20} {'TRD':>4} {'WIN%':>6} {'PF':>5} {'DD%':>6} {'WF':>4} {'₹50k Taker/Mkr':>15} {'₹50k Mkr/Mkr':>15} {'₹1L Mkr/Mkr (20x)':>18}")
    print("-" * 124)

    winners_to_register = []
    for sym in SYMBOLS:
        for tf in ("5m", "1m"):
            sub = [r for r in all_rows if r["symbol"] == sym and r["tf"] == tf]
            sub.sort(key=lambda x: (x["pos_w"] >= 2, x["score"]), reverse=True)
            seen_rules = set()
            picked = []
            for r in sub:
                if r["rule_type"] in seen_rules and len(picked) < 2:
                    continue
                seen_rules.add(r["rule_type"])
                picked.append(r)
                if len(picked) >= 2:
                    break
            for idx, r in enumerate(picked):
                tag = f"{r['rule_type']}:{r['label']}"[:33]
                print(
                    f"{sym:<7} {tf:<4} {tag:<34} {r['exit']['lbl']:<20} "
                    f"{r['trades']:>4d} {r['wr']:>5.1f}% {r['pf']:>5.2f} {r['dd50']:>5.1f}% "
                    f"{r['pos_w']}/3  "
                    f"₹{r['net50_tm']:>+8,.0f}      "
                    f"₹{r['net50_mm']:>+8,.0f} ({r['ret50_mm']:>+4.1f}%) "
                    f"₹{r['net100_mm']:>+9,.0f} ({r['ret100_mm']:>+4.1f}%)"
                )
                if idx == 0:
                    winners_to_register.append(r)
        print("-" * 124)

    # Register top 1m & 5m winners into data/strategies.json and data/saved_backtests.json
    strat_file = Path("data/strategies.json")
    saved_file = Path("data/saved_backtests.json")
    existing_strats = json.loads(strat_file.read_text(encoding="utf-8")) if strat_file.exists() else []
    existing_saved = json.loads(saved_file.read_text(encoding="utf-8")) if saved_file.exists() else []

    existing_strats = [s for s in existing_strats if not str(s.get("id", "")).startswith("scalp1m5m_")]
    existing_saved = [r for r in existing_saved if not str(r.get("id", "")).startswith("res_scalp1m5m_")]

    new_strats = []
    new_saved = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for r in winners_to_register:
        sym = r["symbol"]
        tf = r["tf"]
        ex = r["exit"]
        for cap_tag, cap_val, lev_val, bt_res in [
            ("50k", 50000.0, 15, r["r50"]),
            ("100k", 100000.0, 20, r["r100"]),
        ]:
            sid = f"scalp1m5m_{sym.lower()}_{tf}_{cap_tag}"
            cap_lbl = "₹50k" if cap_tag == "50k" else "₹1L"
            sname = f"🚀 {tf} Scalp: {sym} {r['label']} ({cap_lbl} · {lev_val}x · Maker)"
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
                "fee_mode": "maker_maker",
                "slippage_bps": 0.0,
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
                "period_days": float(r.get("eval_days", 180.0)),
                "capital_inr": cap_val,
                "leverage": float(lev_val),
                "risk_pct": 2.0,
                "sl_type": ex["sl_type"],
                "sl_value": ex["sl"],
                "tp_type": ex["tp_type"],
                "tp_value": ex["tp"],
                "trail_mult": ex["trail"],
                "fee_mode": "maker_maker",
                "slippage_bps": 0.0,
                "rule_type": r["rule_type"],
                "params": r["params"],
                "net_pnl_inr": round(float(s["net_pnl_inr"]), 2),
                "return_pct": round(float(s["net_return_pct"]), 2),
                "win_rate_pct": round(float(s["win_rate_pct"]), 2),
                "profit_factor": round(float(s["profit_factor"]), 2) if np.isfinite(s["profit_factor"]) else 99.0,
                "max_dd_pct": round(float(s["max_drawdown_pct"]), 2),
                "total_trades": int(s["total_trades"]),
                "walk_forward_pos_windows": r["pos_w"],
                "walk_forward_nets_inr": r["w_nets"],
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
    print(f"\nRegistered {len(new_strats)} 1m/5m scalping strategies into data/strategies.json and data/saved_backtests.json.")
    print(f"Completed in {time.perf_counter() - t0:.1f}s.")


if __name__ == "__main__":
    main()
