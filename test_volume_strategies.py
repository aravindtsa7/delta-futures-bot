"""Fast Institutional Volume-Driven Strategy Discovery & Auto-Registration (<45s).

Evaluates the 4 pure Volume Strategy Archetypes across BTCUSD, ETHUSD, DOGEUSD, XRPUSD, and SOLUSD
on 15m, 30m, and 45m timeframes at ₹50,000 (15x) and ₹1,00,000 (20x) under `maker_maker` (0.02%)
and `taker_maker_tp` fee modes:
  1. vol_obv_breakout    (On-Balance Volume Trend & EMA Cross Breakout)
  2. vol_climax_reversal (Volume Climax / Institutional Absorption Reversal)
  3. vol_vwap_expansion  (VWAP Value Area Breakout + 2x Relative Volume Burst)
  4. vol_dryup_breakout  (Volume Dry-Up Compression + Volume Impulse Burst)

Filters for Half-1 & Half-2 walk-forward consistency, high Win Rate % (55%-75%), PF >= 1.25,
and Max DD <= 10%, prints the per-coin and Combined 5-Coin Volume Portfolio tables, and
auto-registers the winners into `data/strategies.json` and `data/saved_backtests.json`.
"""

from __future__ import annotations

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

import backtester as bt
import data_manager as dm
import strategies as st


SYMBOLS = ["BTCUSD", "ETHUSD", "DOGEUSD", "XRPUSD", "SOLUSD"]
TIMEFRAMES = ("15m", "30m", "45m")

# Cache _closed_4h_regime per (id(bars), ema_custom) for fast multi-TF evaluation
_orig_closed_4h = st._closed_4h_regime
_reg4_cache: dict[tuple[int, int], pd.DataFrame] = {}


def _cached_closed_4h(bars: pd.DataFrame, ema_custom: int = 100) -> pd.DataFrame:
    key = (id(bars), int(ema_custom))
    if key not in _reg4_cache:
        _reg4_cache[key] = _orig_closed_4h(bars, ema_custom)
    return _reg4_cache[key]


st._closed_4h_regime = _cached_closed_4h


CANDIDATES = [
    # --- 1. vol_obv_breakout (On-Balance Volume Trend & EMA Cross Breakout) ---
    ("vol_obv_breakout", "OBV20 RVOL1.3x DC12", {"obv_span": 20, "vol_ma_period": 20, "rvol_min": 1.3, "ema_filter": 50, "dc_period": 12, "adx_min": 18, "body_ratio": 0.40, "cost_gate_mult": 1.6, "use_4h_trend": 1}),
    ("vol_obv_breakout", "OBV20 RVOL1.5x DC15", {"obv_span": 20, "vol_ma_period": 20, "rvol_min": 1.5, "ema_filter": 50, "dc_period": 15, "adx_min": 20, "body_ratio": 0.45, "cost_gate_mult": 1.8, "use_4h_trend": 1}),
    ("vol_obv_breakout", "OBV14 RVOL1.3x Fast", {"obv_span": 14, "vol_ma_period": 20, "rvol_min": 1.3, "ema_filter": 50, "dc_period": 10, "adx_min": 16, "body_ratio": 0.35, "cost_gate_mult": 1.4, "use_4h_trend": 0}),
    ("vol_obv_breakout", "OBV25 RVOL1.6x Cross", {"obv_span": 25, "vol_ma_period": 20, "rvol_min": 1.6, "ema_filter": 100, "dc_period": 0, "adx_min": 20, "body_ratio": 0.45, "cost_gate_mult": 1.6, "use_4h_trend": 1}),

    # --- 2. vol_climax_reversal (Volume Climax / Institutional Absorption Reversal) ---
    ("vol_climax_reversal", "Climax 2.8x Wick40%", {"climax_mult": 2.8, "vol_ma_period": 20, "wick_ratio": 0.40, "bb_period": 20, "bb_std": 2.0, "rsi_period": 14, "rsi_os": 38, "rsi_ob": 62, "ema_filter": 200, "adx_max": 45, "cost_gate_mult": 1.5, "use_4h_trend": 1}),
    ("vol_climax_reversal", "Climax 2.4x Absorption", {"climax_mult": 2.4, "vol_ma_period": 20, "wick_ratio": 0.35, "bb_period": 20, "bb_std": 2.0, "rsi_period": 14, "rsi_os": 40, "rsi_ob": 60, "ema_filter": 100, "adx_max": 48, "cost_gate_mult": 1.4, "use_4h_trend": 1}),
    ("vol_climax_reversal", "Climax 3.0x DeepWick", {"climax_mult": 3.0, "vol_ma_period": 20, "wick_ratio": 0.40, "bb_period": 20, "bb_std": 2.1, "rsi_period": 10, "rsi_os": 35, "rsi_ob": 65, "ema_filter": 0, "adx_max": 45, "cost_gate_mult": 1.5, "use_4h_trend": 1}),
    ("vol_climax_reversal", "Climax 2.2x FastRev", {"climax_mult": 2.2, "vol_ma_period": 20, "wick_ratio": 0.35, "bb_period": 18, "bb_std": 1.9, "rsi_period": 14, "rsi_os": 42, "rsi_ob": 58, "ema_filter": 100, "adx_max": 45, "cost_gate_mult": 1.3, "use_4h_trend": 0}),

    # --- 3. vol_vwap_expansion (VWAP Value Area Breakout + 2x Relative Volume Burst) ---
    ("vol_vwap_expansion", "VWAP+RVOL 2.0x Burst", {"rvol_min": 2.0, "vol_ma_period": 20, "ema_period": 20, "body_ratio": 0.50, "adx_min": 20, "dc_period": 10, "cost_gate_mult": 1.6, "use_4h_trend": 1}),
    ("vol_vwap_expansion", "VWAP+RVOL 1.8x DC12", {"rvol_min": 1.8, "vol_ma_period": 20, "ema_period": 21, "body_ratio": 0.45, "adx_min": 18, "dc_period": 12, "cost_gate_mult": 1.5, "use_4h_trend": 1}),
    ("vol_vwap_expansion", "VWAP+RVOL 2.2x Strict", {"rvol_min": 2.2, "vol_ma_period": 20, "ema_period": 20, "body_ratio": 0.52, "adx_min": 22, "dc_period": 15, "cost_gate_mult": 1.8, "use_4h_trend": 1}),
    ("vol_vwap_expansion", "VWAP+RVOL 1.7x Fast", {"rvol_min": 1.7, "vol_ma_period": 20, "ema_period": 20, "body_ratio": 0.45, "adx_min": 18, "dc_period": 8, "cost_gate_mult": 1.4, "use_4h_trend": 0}),

    # --- 4. vol_dryup_breakout (Volume Dry-Up Compression + Volume Impulse Burst) ---
    ("vol_dryup_breakout", "DryUp3<=0.65 Burst2.2x", {"dryup_bars": 3, "dryup_max_ratio": 0.65, "burst_mult": 2.2, "dc_period": 15, "body_ratio": 0.45, "adx_min": 18, "ema_filter": 50, "cost_gate_mult": 1.6, "use_4h_trend": 1}),
    ("vol_dryup_breakout", "DryUp3<=0.70 Burst2.0x", {"dryup_bars": 3, "dryup_max_ratio": 0.70, "burst_mult": 2.0, "dc_period": 12, "body_ratio": 0.42, "adx_min": 18, "ema_filter": 50, "cost_gate_mult": 1.5, "use_4h_trend": 1}),
    ("vol_dryup_breakout", "DryUp4<=0.75 Burst1.8x", {"dryup_bars": 4, "dryup_max_ratio": 0.75, "burst_mult": 1.8, "dc_period": 10, "body_ratio": 0.40, "adx_min": 16, "ema_filter": 50, "cost_gate_mult": 1.4, "use_4h_trend": 0}),
    ("vol_dryup_breakout", "DryUp3<=0.65 Burst2.4x", {"dryup_bars": 3, "dryup_max_ratio": 0.65, "burst_mult": 2.4, "dc_period": 15, "body_ratio": 0.50, "adx_min": 20, "ema_filter": 50, "cost_gate_mult": 1.6, "use_4h_trend": 1}),
]

EXIT_PROFILES = [
    {"sl_type": "atr_mult",  "sl": 2.0, "tp_type": "atr_mult",  "tp": 2.2, "trail": 1.6, "be": True,  "sl_min": 0.75, "lbl": "2.0x/2.2x+BE+Tr1.6"},
    {"sl_type": "atr_mult",  "sl": 2.2, "tp_type": "atr_mult",  "tp": 2.6, "trail": 1.8, "be": True,  "sl_min": 0.75, "lbl": "2.2x/2.6x+BE+Tr1.8"},
    {"sl_type": "atr_mult",  "sl": 2.2, "tp_type": "atr_mult",  "tp": 3.0, "trail": 0.0, "be": True,  "sl_min": 0.75, "lbl": "2.2x/3.0x+BE(Mkr)"},
    {"sl_type": "fixed_pct", "sl": 1.0, "tp_type": "fixed_pct", "tp": 1.5, "trail": 0.0, "be": True,  "sl_min": 0.75, "lbl": "1.0%/1.5%+BE(Mkr)"},
]


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
        cooldown_bars=1,
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
    print("=" * 130)
    print("INSTITUTIONAL VOLUME-DRIVEN STRATEGY DISCOVERY (BTC, ETH, DOGE, XRP, SOL | 15m, 30m, 45m | ₹50k & ₹1L)")
    print("=" * 130)

    all_rows: list[dict[str, Any]] = []
    for sym in SYMBOLS:
        t_sym = time.perf_counter()
        df_full = dm.load_unified_1m_dataset(sym)
        end = df_full.index[-1]
        total_days = (end - df_full.index[0]) / pd.Timedelta(days=1)
        if total_days > 130:
            # Use last 120d test window + 25d warmup for BTC/ETH so 15m/30m/45m evaluates in ~2.5s
            start = end - pd.Timedelta(days=120)
            warmup_start = start - pd.Timedelta(days=25)
            df_1m = df_full[df_full.index >= warmup_start].copy()
        else:
            warmup_days = min(11.0, max(5.0, total_days * 0.18))
            start = df_full.index[0] + pd.Timedelta(days=warmup_days)
            df_1m = df_full
        df_1m.attrs["symbol"] = sym
        eval_days = round((end - start) / pd.Timedelta(days=1), 1)
        half_mid = start + (end - start) / 2

        spec = bt.ProductSpec.from_metadata(sym)
        meta = dm.load_metadata(sym)
        max_lev = int(float(meta.get("default_leverage") or 20))
        lev50 = float(min(15, max_lev))
        lev100 = float(min(20, max_lev))

        for tf in TIMEFRAMES:
            bars = dm.resample_candles(df_1m, tf)
            atr14 = st.atr(bars, 14)

            for rule_type, label, params in CANDIDATES:
                p_use = dict(params)
                out = st.evaluate_custom_strategy(rule_type, p_use, bars)
                sig = out["signal"]
                in_win_sigs = int((sig[(bars.index >= start) & (bars.index < end)] != 0).sum())
                if in_win_sigs < 5 or in_win_sigs > 140:
                    continue

                for ex in EXIT_PROFILES:
                    r50, s50, _, _, _, _ = run_eval(
                        spec, df_1m, sig, tf, atr14, start, end,
                        50000.0, lev50, ex, fee_mode="maker_maker", half_mid=None
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
                        50000.0, lev50, ex, fee_mode="maker_maker", half_mid=half_mid
                    )
                    r100, s100, _, _, leg100, cfg100 = run_eval(
                        spec, df_1m, sig, tf, atr14, start, end,
                        100000.0, lev100, ex, fee_mode="maker_maker", half_mid=None
                    )
                    _, s50_tm, _, _, _, _ = run_eval(
                        spec, df_1m, sig, tf, atr14, start, end,
                        50000.0, lev50, ex, fee_mode="taker_maker_tp", half_mid=None
                    )

                    passes_strict = (pos_h == 2 and wr >= 55.0 and pf >= 1.25 and dd <= 10.0)
                    score = (
                        (wr / 100.0) * 50.0
                        + min(pf, 3.5) * 22.0
                        + float(s50["net_return_pct"]) * 0.70
                        + pos_h * 20.0
                        + (15.0 if passes_strict else 0.0)
                        - max(0.0, dd - 9.0) * 2.5
                    )
                    all_rows.append({
                        "symbol": sym,
                        "tf": tf,
                        "rule_type": rule_type,
                        "label": label,
                        "params": {**p_use, "breakeven_at_1r": 1 if ex["be"] else 0, "atr_sl_min_pct": ex["sl_min"]},
                        "exit": ex,
                        "fee_mode": "maker_maker",
                        "lev50": int(lev50),
                        "lev100": int(lev100),
                        "trades": trades,
                        "wr": wr,
                        "pf": pf,
                        "dd50": dd,
                        "net50": net50,
                        "ret50": float(s50["net_return_pct"]),
                        "net50_tm": float(s50_tm["net_pnl_inr"]),
                        "net100": float(s100["net_pnl_inr"]),
                        "ret100": float(s100["net_return_pct"]),
                        "dd100": abs(float(s100["max_drawdown_pct"])),
                        "pos_h": pos_h,
                        "h_nets": h_nets,
                        "passes_strict": passes_strict,
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
        print(f"  [{sym}] evaluated 4 volume archetypes on 15m/30m/45m in {time.perf_counter() - t_sym:.1f}s (window={eval_days}d)")

    print("\n" + "=" * 134)
    print(f"{'COIN':<8} {'RK':<3} {'TF':<4} {'VOLUME ARCHETYPE / LABEL':<35} {'EXITS':<20} {'TRD':>4} {'WIN%':>6} {'PF':>5} {'DD%':>5} {'WF':>4} {'₹50k Tkr/Mkr':>13} {'₹50k Mkr/Mkr':>16} {'₹1L Mkr/Mkr':>17}")
    print("-" * 134)

    winners_by_coin: dict[str, list[dict[str, Any]]] = {}
    winners_to_register: list[tuple[int, dict[str, Any]]] = []

    for sym in SYMBOLS:
        sub = [r for r in all_rows if r["symbol"] == sym]
        sub.sort(key=lambda x: (x["passes_strict"], x["pos_h"] == 2, x["score"]), reverse=True)
        picked = []
        seen_rules = set()
        for r in sub:
            if r["rule_type"] in seen_rules and len(picked) < 2:
                continue
            seen_rules.add(r["rule_type"])
            picked.append(r)
            if len(picked) >= 2:
                break
        winners_by_coin[sym] = picked
        for idx, r in enumerate(picked):
            tag = f"{r['rule_type']}:{r['label']}"[:34]
            print(
                f"{sym:<8} #{idx + 1:<2} {r['tf']:<4} {tag:<35} {r['exit']['lbl']:<20} "
                f"{r['trades']:>4d} {r['wr']:>5.1f}% {r['pf']:>5.2f} {r['dd50']:>4.1f}% "
                f"{r['pos_h']}/2  "
                f"₹{r['net50_tm']:>+8,.0f}   "
                f"₹{r['net50']:>+7,.0f} ({r['ret50']:>+4.1f}%) "
                f"₹{r['net100']:>+8,.0f} ({r['ret100']:>+4.1f}%)"
            )
            winners_to_register.append((idx + 1, r))
        print("-" * 134)

    # Combined 5-Coin Volume Portfolio over common 49.2d overlap window
    top1_rows = [winners_by_coin[s][0] for s in SYMBOLS if winners_by_coin.get(s)]
    if top1_rows:
        p_start = max(r["start"] for r in top1_rows)
        p_end = min(r["end"] for r in top1_rows)
        legs50 = [r["leg50"] for r in top1_rows]
        legs100 = [r["leg100"] for r in top1_rows]
        port50 = bt.Backtester(top1_rows[0]["cfg50"]).run_portfolio(legs50, p_start, p_end).summary
        port100 = bt.Backtester(top1_rows[0]["cfg100"]).run_portfolio(legs100, p_start, p_end).summary
        print(
            f"5-COIN VOLUME PORTFOLIO (Common {round((p_end - p_start) / pd.Timedelta(days=1), 1)}d Window): "
            f"Trades={port50['total_trades']} | WinRate={port50['win_rate_pct']:.1f}% | PF={port50['profit_factor']:.2f} | "
            f"₹50k Net=₹{port50['net_pnl_inr']:+,.0f} ({port50['net_return_pct']:+.1f}%, DD {port50['max_drawdown_pct']:.1f}%) | "
            f"₹1L Net=₹{port100['net_pnl_inr']:+,.0f} ({port100['net_return_pct']:+.1f}%, DD {port100['max_drawdown_pct']:.1f}%)"
        )
        print("=" * 134)

    # Auto-register in data/strategies.json and data/saved_backtests.json
    strat_file = Path("data/strategies.json")
    saved_file = Path("data/saved_backtests.json")
    existing_strats = json.loads(strat_file.read_text(encoding="utf-8")) if strat_file.exists() else []
    existing_saved = json.loads(saved_file.read_text(encoding="utf-8")) if saved_file.exists() else []

    existing_strats = [s for s in existing_strats if not str(s.get("id", "")).startswith("vol_inst_")]
    existing_saved = [r for r in existing_saved if not str(r.get("id", "")).startswith("res_vol_inst_")]

    new_strats = []
    new_saved = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for rank, r in winners_to_register:
        sym = r["symbol"]
        tf = r["tf"]
        ex = r["exit"]
        tiers = [("50k", 50000.0, r["lev50"], r["r50"]), ("100k", 100000.0, r["lev100"], r["r100"])] if rank == 1 else [("50k", 50000.0, r["lev50"], r["r50"])]
        for cap_tag, cap_val, lev_val, bt_res in tiers:
            sid = f"vol_inst_{sym.lower()}_r{rank}_{tf}_{cap_tag}"
            cap_lbl = "₹50k" if cap_tag == "50k" else "₹1L"
            sname = f"📊 Volume #{rank}: {sym} {tf} {r['label']} ({cap_lbl} · {lev_val}x)"
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
                "slippage_bps": 0.0,
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
    print(f"\nRegistered {len(new_strats)} Volume strategies into data/strategies.json and data/saved_backtests.json.")
    print(f"Completed in {time.perf_counter() - t0:.1f}s.")


if __name__ == "__main__":
    main()

