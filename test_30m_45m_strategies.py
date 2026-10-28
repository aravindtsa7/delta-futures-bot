"""Discover, validate & auto-register the best 30m & 45m strategies on BTCUSD, ETHUSD & BNBUSD (< 35s)."""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import data_manager as dm
import strategies
from backtester import Backtester, BacktestConfig, Leg, ProductSpec
from config import settings

STRATEGIES_FILE = settings.data_path / "strategies.json"
SAVED_BACKTESTS_FILE = settings.data_path / "saved_backtests.json"

CANDIDATES_30M_45M: list[dict[str, Any]] = [
    # --- 1. bb_squeeze_adx & bb_squeeze_quick_tp (30m & 45m) ---
    {
        "title": "BB Squeeze ADX Ignition 45m (4h + CostGate)",
        "rule_type": "bb_squeeze_adx",
        "tf": "45m",
        "params": {"bb_period": 20, "bb_std": 2.0, "adx_min": 21, "adx_rising": 1, "bbw_expanding": 1, "vol_mult": 1.35, "body_ratio": 0.45, "ema_filter": 50, "cost_gate_mult": 2.5, "rt_cost": 0.00158, "use_4h_trend": 1, "squeeze_q": 0.60, "squeeze_lookback": 4},
        "sl": 2.0, "tp": 4.5, "trail": 2.4, "be": False, "risk": 1.8,
    },
    {
        "title": "BB Squeeze Quick-TP 2.0x Maker Sniper 45m",
        "rule_type": "bb_squeeze_quick_tp",
        "tf": "45m",
        "params": {"bb_period": 20, "bb_std": 2.0, "adx_min": 20, "adx_rising": 1, "bbw_expanding": 1, "vol_mult": 1.35, "body_ratio": 0.45, "ema_filter": 50, "cost_gate_mult": 2.2, "rt_cost": 0.00158, "use_4h_trend": 1, "squeeze_q": 0.60, "squeeze_lookback": 4},
        "sl": 2.0, "tp": 2.0, "trail": 0.0, "be": False, "risk": 1.8,
    },
    {
        "title": "BB Squeeze Momentum 45m (CostGate + EMA50)",
        "rule_type": "bb_squeeze_adx",
        "tf": "45m",
        "params": {"bb_period": 20, "bb_std": 2.0, "adx_min": 19, "adx_rising": 0, "bbw_expanding": 1, "vol_mult": 1.35, "body_ratio": 0.45, "ema_filter": 50, "cost_gate_mult": 1.8, "rt_cost": 0.00158, "use_4h_trend": 0, "squeeze_q": 0.55, "squeeze_lookback": 2},
        "sl": 2.0, "tp": 4.5, "trail": 2.4, "be": False, "risk": 1.5,
    },
    {
        "title": "BB Squeeze Quick-TP 2.0x Momentum 45m",
        "rule_type": "bb_squeeze_quick_tp",
        "tf": "45m",
        "params": {"bb_period": 20, "bb_std": 2.0, "adx_min": 19, "adx_rising": 0, "bbw_expanding": 1, "vol_mult": 1.35, "body_ratio": 0.45, "ema_filter": 50, "cost_gate_mult": 1.8, "rt_cost": 0.00158, "use_4h_trend": 0, "squeeze_q": 0.55, "squeeze_lookback": 2},
        "sl": 2.0, "tp": 2.0, "trail": 0.0, "be": False, "risk": 1.5,
    },
    {
        "title": "BB Squeeze Quick-TP 2.2x ATR Harvester 30m",
        "rule_type": "bb_squeeze_quick_tp",
        "tf": "30m",
        "params": {"bb_period": 20, "bb_std": 2.0, "adx_min": 20, "adx_rising": 0, "bbw_expanding": 1, "vol_mult": 1.35, "body_ratio": 0.45, "ema_filter": 50, "cost_gate_mult": 1.7, "rt_cost": 0.00158, "use_4h_trend": 0, "squeeze_q": 0.50, "squeeze_lookback": 2},
        "sl": 2.0, "tp": 2.2, "trail": 2.2, "be": False, "risk": 1.5,
    },
    # --- 2. ema_pullback_scalp (30m & 45m) ---
    {
        "title": "EMA20 Pullback Bounce Harvester 30m",
        "rule_type": "ema_pullback_scalp",
        "tf": "30m",
        "params": {"fast_ema": 20, "slow_ema": 50, "adx_min": 22, "pullback_tol": 0.0018, "rsi_period": 14, "rsi_long_min": 45, "rsi_long_max": 58, "rsi_short_min": 42, "rsi_short_max": 55, "vol_mult": 1.25, "cost_gate_mult": 2.2, "rt_cost": 0.00158, "use_4h_trend": 1},
        "sl": 1.8, "tp": 2.2, "trail": 1.8, "be": False, "risk": 2.0,
    },
    {
        "title": "EMA20 Pullback Bounce Harvester 45m",
        "rule_type": "ema_pullback_scalp",
        "tf": "45m",
        "params": {"fast_ema": 20, "slow_ema": 50, "adx_min": 20, "pullback_tol": 0.002, "rsi_period": 14, "rsi_long_min": 44, "rsi_long_max": 60, "rsi_short_min": 40, "rsi_short_max": 56, "vol_mult": 1.15, "cost_gate_mult": 2.0, "rt_cost": 0.00158, "use_4h_trend": 1},
        "sl": 1.8, "tp": 2.2, "trail": 1.8, "be": False, "risk": 2.0,
    },
    {
        "title": "EMA20 Pullback 2.4x ATR Bounce 45m",
        "rule_type": "ema_pullback_scalp",
        "tf": "45m",
        "params": {"fast_ema": 20, "slow_ema": 50, "adx_min": 18, "pullback_tol": 0.0025, "rsi_period": 14, "rsi_long_min": 42, "rsi_long_max": 60, "rsi_short_min": 40, "rsi_short_max": 58, "vol_mult": 1.0, "cost_gate_mult": 1.6, "rt_cost": 0.00158, "use_4h_trend": 1},
        "sl": 1.8, "tp": 2.4, "trail": 2.0, "be": False, "risk": 2.0,
    },
    {
        "title": "EMA20 Pullback 2.4x ATR Bounce 30m",
        "rule_type": "ema_pullback_scalp",
        "tf": "30m",
        "params": {"fast_ema": 20, "slow_ema": 50, "adx_min": 18, "pullback_tol": 0.0022, "rsi_period": 14, "rsi_long_min": 42, "rsi_long_max": 60, "rsi_short_min": 40, "rsi_short_max": 58, "vol_mult": 1.0, "cost_gate_mult": 1.5, "rt_cost": 0.00158, "use_4h_trend": 1},
        "sl": 1.8, "tp": 2.4, "trail": 2.0, "be": False, "risk": 2.0,
    },
    # --- 3. bb_rsi_snapback (30m & 45m) ---
    {
        "title": "BB + RSI Snapback Pure Maker 1.9x TP 30m",
        "rule_type": "bb_rsi_snapback",
        "tf": "30m",
        "params": {"bb_period": 20, "bb_std": 2.2, "rsi_period": 14, "rsi_os": 30, "rsi_ob": 70, "cost_gate_mult": 2.4, "rt_cost": 0.00158, "use_4h_trend": 1},
        "sl": 1.8, "tp": 1.9, "trail": 0.0, "be": False, "risk": 1.8,
    },
    {
        "title": "BB + RSI Snapback Pure Maker 1.8x TP 30m",
        "rule_type": "bb_rsi_snapback",
        "tf": "30m",
        "params": {"bb_period": 20, "bb_std": 2.2, "rsi_period": 14, "rsi_os": 30, "rsi_ob": 70, "cost_gate_mult": 2.4, "rt_cost": 0.00158, "use_4h_trend": 1},
        "sl": 1.8, "tp": 1.8, "trail": 0.0, "be": False, "risk": 1.8,
    },
    {
        "title": "BB + RSI Snapback Pure Maker 1.9x TP 45m",
        "rule_type": "bb_rsi_snapback",
        "tf": "45m",
        "params": {"bb_period": 20, "bb_std": 2.1, "rsi_period": 14, "rsi_os": 31, "rsi_ob": 69, "cost_gate_mult": 2.2, "rt_cost": 0.00158, "use_4h_trend": 1},
        "sl": 1.8, "tp": 1.9, "trail": 0.0, "be": False, "risk": 1.8,
    },
    {
        "title": "BB + RSI Snapback Pure Maker 2.0x TP 45m",
        "rule_type": "bb_rsi_snapback",
        "tf": "45m",
        "params": {"bb_period": 20, "bb_std": 2.0, "rsi_period": 14, "rsi_os": 33, "rsi_ob": 67, "cost_gate_mult": 1.8, "rt_cost": 0.00158, "use_4h_trend": 1},
        "sl": 1.8, "tp": 2.0, "trail": 0.0, "be": False, "risk": 1.8,
    },
    # --- 4. vwap_st_burst & supertrend_ema_pullback (30m & 45m) ---
    {
        "title": "VWAP + Supertrend Pure Maker 2.1x TP 45m",
        "rule_type": "vwap_st_burst",
        "tf": "45m",
        "params": {"atr_period": 10, "atr_mult": 2.5, "ema_period": 20, "vol_mult": 1.25, "adx_min": 18, "body_ratio": 0.38, "cost_gate_mult": 1.7, "rt_cost": 0.00158, "use_4h_trend": 1},
        "sl": 1.8, "tp": 2.1, "trail": 0.0, "be": False, "risk": 2.0,
    },
    {
        "title": "Supertrend EMA21 Pullback Quick-TP 45m",
        "rule_type": "supertrend_ema_pullback",
        "tf": "45m",
        "params": {"atr_period": 10, "atr_mult": 3.0, "pullback_ema": 21, "ema_4h": 100, "pullback_tol": 0.0025, "rsi_period": 14, "rsi_long_min": 42, "rsi_long_max": 60, "rsi_short_min": 40, "rsi_short_max": 58, "adx_min": 18, "vol_mult": 1.15, "cost_gate_mult": 1.8, "rt_cost": 0.00158, "use_4h_200": 1},
        "sl": 1.8, "tp": 2.2, "trail": 1.8, "be": False, "risk": 1.8,
    },
]


def clean_json(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): clean_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean_json(v) for v in obj]
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
        return round(x, 4)
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return obj


def serialize_trades(trades: pd.DataFrame, rate: float) -> list[dict[str, Any]]:
    if trades.empty:
        return []
    t = trades.copy()
    for c in ("gross_pnl", "fees", "funding", "entry_fee", "exit_fee", "gst"):
        t[f"{c}_inr"] = t[f"{c}_usd"] * rate
    for c in ("signal_bar", "entry_time", "exit_time"):
        t[f"{c}_ts"] = t[c].map(lambda x: int(x.timestamp()))
        t[c] = t[c].map(lambda x: x.isoformat())
    return clean_json(t.to_dict(orient="records"))


def trade_stats(tr: pd.DataFrame) -> dict[str, float]:
    if tr.empty:
        return {"avg_win_inr": 0.0, "avg_loss_inr": 0.0, "active_days": 1.0, "net_per_active_day": 0.0}
    wins = tr[tr["net_pnl_inr"] > 0]["net_pnl_inr"]
    losses = tr[tr["net_pnl_inr"] <= 0]["net_pnl_inr"]
    active_days = max(1, int(tr["entry_time"].dt.floor("D").nunique()))
    return {
        "avg_win_inr": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_inr": float(losses.mean()) if len(losses) else 0.0,
        "active_days": float(active_days),
        "net_per_active_day": float(tr["net_pnl_inr"].sum()) / active_days,
    }


def main() -> None:
    t0 = time.perf_counter()
    symbols = ["BTCUSD", "ETHUSD", "BNBUSD"]
    datasets = {s: dm.load_unified_1m_dataset(s) for s in symbols}
    specs = {s: ProductSpec.from_metadata(s) for s in symbols}

    resampled: dict[tuple[str, str], tuple[pd.DataFrame, pd.Series]] = {}
    for s in symbols:
        for tf in ("30m", "45m"):
            b = dm.resample_candles(datasets[s], tf)
            resampled[(s, tf)] = (b, strategies.atr(b, 14))

    best_by_sym: dict[str, list[dict[str, Any]]] = {}

    for sym in symbols:
        df_1m = datasets[sym]
        spec = specs[sym]
        end = df_1m.index[-1] + pd.Timedelta(minutes=1)
        total_days = (end - df_1m.index[0]).total_seconds() / 86400.0
        eval_days = 365 if total_days >= 370 else max(30, int(total_days - 10))
        start_eval = end - pd.Timedelta(days=eval_days)
        mid_eval = start_eval + pd.Timedelta(days=eval_days / 2.0)
        w6_edges = [start_eval + (end - start_eval) * i / 6 for i in range(7)]

        print(f"\n--- {sym} ({eval_days}d) 30m & 45m STRATEGY SCAN ---")
        sym_cands = []
        for cand in CANDIDATES_30M_45M:
            tf = cand["tf"]
            bars, atr14 = resampled[(sym, tf)]
            out = strategies.evaluate_custom_strategy(cand["rule_type"], cand["params"], bars)
            sig = out["signal"]

            r_pct = float(cand.get("risk", 1.8))
            cfg50 = BacktestConfig(
                initial_capital_inr=50_000.0,
                leverage=15.0,
                risk_per_trade_pct=r_pct,
                max_daily_loss_pct=5.0,
                sl_mode="atr",
                sl_atr_mult=cand["sl"],
                tp_atr_mult=cand["tp"],
                trailing_atr_mult=cand["trail"],
                breakeven_at_1r=cand.get("be", False),
                fee_mode="taker_maker_tp",
                slippage_bps=2.0,
                cooldown_bars=1,
            )
            leg50 = Leg(spec, df_1m, sig, tf, atr14, cand["title"], None, cfg50)
            bt50 = Backtester(cfg50)
            res50 = bt50.run_portfolio([leg50], start_eval, end)
            s50 = res50.summary
            tr50 = res50.trades
            if s50["total_trades"] < 5 or tr50.empty:
                continue

            h1_net = float(tr50[tr50["entry_time"] < mid_eval]["net_pnl_inr"].sum())
            h2_net = float(tr50[tr50["entry_time"] >= mid_eval]["net_pnl_inr"].sum())
            w_nets = [
                float(tr50[(tr50["entry_time"] >= w6_edges[wi]) & (tr50["entry_time"] < w6_edges[wi + 1])]["net_pnl_inr"].sum())
                for wi in range(6)
            ]
            w_won = sum(1 for x in w_nets if x > 0)
            pf_val = float(s50["profit_factor"]) if s50["profit_factor"] != float("inf") else 5.0
            dd_val = max(0.5, float(s50["max_drawdown_pct"]))
            wr_val = float(s50["win_rate_pct"])
            net50 = float(s50["net_pnl_inr"])
            both_halves = (h1_net > 0) and (h2_net > 0)
            tstats50 = trade_stats(tr50)

            print(f"  {cand['title']:<44} | {cand['rule_type']:<22} | {tf:<3} | WR={wr_val:>5.1f}% | Net50k={net50:>8.0f} | PF={pf_val:>4.2f} | DD={dd_val:>5.2f}% | Trd={s50['total_trades']:>3} | AvgW=₹{tstats50['avg_win_inr']:>5.0f} | H1={h1_net:>6.0f} H2={h2_net:>6.0f} | W={w_won}/6")

            score = (
                (5000.0 if net50 > 0 else -5000.0)
                + (2500.0 if both_halves else 0.0)
                + (1200.0 if pf_val >= 1.35 else (800.0 if pf_val >= 1.20 else 0.0))
                + (500.0 if dd_val <= 12.0 else -25.0 * (dd_val - 12.0))
                + (wr_val * 12.0)
                + (w_won * 90.0)
                + (pf_val * 350.0)
                + (net50 / 20.0)
            )
            sym_cands.append({
                "cand": cand,
                "sig": sig,
                "atr14": atr14,
                "eval_days": eval_days,
                "start_eval": start_eval,
                "mid_eval": mid_eval,
                "end": end,
                "cfg50": cfg50,
                "leg50": leg50,
                "res50": res50,
                "net50": net50,
                "ret50": float(s50["net_return_pct"]),
                "wr": wr_val,
                "pf": pf_val,
                "dd": dd_val,
                "trades": int(s50["total_trades"]),
                "h1_net50": h1_net,
                "h2_net50": h2_net,
                "w_won": w_won,
                "tstats50": tstats50,
                "score": score,
            })

        sym_cands.sort(key=lambda x: x["score"], reverse=True)
        # Ensure we pick top 45m and top 30m (up to 3 for BTCUSD, 2 for ETHUSD/BNBUSD)
        best_30m = [x for x in sym_cands if x["cand"]["tf"] == "30m" and x["net50"] > 0]
        top_k = 3 if sym == "BTCUSD" else 2
        top_list = sym_cands[:top_k]
        if best_30m and not any(x["cand"]["tf"] == "30m" for x in top_list):
            top_list[-1] = best_30m[0]

        for w in top_list:
            c = w["cand"]
            r_pct = float(c.get("risk", 1.8))
            cfg100 = BacktestConfig(
                initial_capital_inr=100_000.0,
                leverage=20.0,
                risk_per_trade_pct=r_pct,
                max_daily_loss_pct=5.0,
                sl_mode="atr",
                sl_atr_mult=c["sl"],
                tp_atr_mult=c["tp"],
                trailing_atr_mult=c["trail"],
                breakeven_at_1r=c.get("be", False),
                fee_mode="taker_maker_tp",
                slippage_bps=2.0,
                cooldown_bars=1,
            )
            leg100 = Leg(spec, df_1m, w["sig"], c["tf"], w["atr14"], c["title"], None, cfg100)
            bt100 = Backtester(cfg100)
            res100 = bt100.run_portfolio([leg100], w["start_eval"], w["end"])
            w["cfg100"] = cfg100
            w["leg100"] = leg100
            w["res100"] = res100
            w["net100"] = float(res100.summary["net_pnl_inr"])
            w["ret100"] = float(res100.summary["net_return_pct"])
            w["wr100"] = float(res100.summary["win_rate_pct"])
            w["pf100"] = float(res100.summary["profit_factor"]) if res100.summary["profit_factor"] != float("inf") else 5.0
            w["dd100"] = float(res100.summary["max_drawdown_pct"])
            w["tstats100"] = trade_stats(res100.trades)
            w["h1_net100"] = float(res100.trades[res100.trades["entry_time"] < w["mid_eval"]]["net_pnl_inr"].sum()) if not res100.trades.empty else 0.0
            w["h2_net100"] = float(res100.trades[res100.trades["entry_time"] >= w["mid_eval"]]["net_pnl_inr"].sum()) if not res100.trades.empty else 0.0
        best_by_sym[sym] = top_list

    print("\n" + "=" * 144)
    print("30m & 45m STRATEGY DISCOVERY SUMMARY — PLAN A (₹50,000, 15x) & PLAN B (₹1,00,000, 20x) | Maker Limit TP (0.02%) + 18% GST + 2bps")
    print("=" * 144)
    hdr = f"{'Coin & 30m/45m Winner':<42} | {'TF':<3} | {'SL/TP/Trl':<11} | {'WR%':<5} | {'AvgWin 50k/1L':<15} | {'₹50k Net (%)':<18} | {'₹1L Net (%)':<19} | {'PF':<4} | {'MaxDD':<6} | {'Trd':<3} | {'H1 / H2 (₹1L)':<17} | {'W'}"
    print(hdr)
    print("-" * 144)

    new_strats: list[dict[str, Any]] = []
    new_runs: list[dict[str, Any]] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for sym in symbols:
        short_s = sym.replace("USD", "")
        for rank, w in enumerate(best_by_sym[sym], start=1):
            c = w["cand"]
            r_pct = float(c.get("risk", 1.8))
            sl_tp_str = f"{c['sl']}/{c['tp']}/{c['trail']}"
            avg_w_str = f"₹{w['tstats50']['avg_win_inr']:,.0f} / ₹{w['tstats100']['avg_win_inr']:,.0f}"
            n50_str = f"+₹{w['net50']:,.0f} (+{w['ret50']:.1f}%)" if w["net50"] >= 0 else f"-₹{abs(w['net50']):,.0f} ({w['ret50']:.1f}%)"
            n100_str = f"+₹{w['net100']:,.0f} (+{w['ret100']:.1f}%)" if w["net100"] >= 0 else f"-₹{abs(w['net100']):,.0f} ({w['ret100']:.1f}%)"
            h12_str = f"₹{w['h1_net100']:,.0f}/₹{w['h2_net100']:,.0f}"
            label = f"🔥 {c['tf']} {short_s} #{rank}: {c['title']}"
            print(f"{label[:42]:<42} | {c['tf']:<3} | {sl_tp_str:<11} | {w['wr']:>4.1f}% | {avg_w_str:<15} | {n50_str:<18} | {n100_str:<19} | {w['pf']:>4.2f} | -{w['dd']:>4.2f}% | {w['trades']:>3} | {h12_str:<17} | {w['w_won']}/6")

            for cap_label, cap_val, lev_val, res_obj, tstats_obj in (
                ("₹1L", 100000.0, 20, w["res100"], w["tstats100"]),
                ("₹50k", 50000.0, 15, w["res50"], w["tstats50"]),
            ):
                if rank > 1 and cap_label == "₹50k":
                    continue
                sid = f"strat_{c['tf']}_{short_s.lower()}_{rank}_{cap_label.replace('₹', '').lower()}"
                sname = f"🔥 {c['tf']} {short_s} #{rank} ({cap_label}): {c['title']}"
                new_strats.append({
                    "id": sid,
                    "name": sname,
                    "symbol": sym,
                    "timeframe": c["tf"],
                    "rule_type": c["rule_type"],
                    "params": {**c["params"], "breakeven_at_1r": c.get("be", False)},
                    "capital_inr": cap_val,
                    "leverage": lev_val,
                    "risk_pct": r_pct,
                    "sl_type": "atr_mult",
                    "sl_value": c["sl"],
                    "tp_type": "atr_mult",
                    "tp_value": c["tp"],
                    "trail_mult": c["trail"],
                    "fee_mode": "taker_maker_tp",
                    "slippage_bps": 2.0,
                })
                s_dict = clean_json(res_obj.summary)
                new_runs.append({
                    "id": f"bt_{c['tf']}_{short_s.lower()}_{rank}_{cap_label.replace('₹', '').lower()}_{w['eval_days']}d",
                    "saved_at": now_iso,
                    "strategy_id": sid,
                    "strategy_name": sname,
                    "symbol": sym,
                    "timeframe": c["tf"],
                    "period_days": w["eval_days"],
                    "capital_inr": cap_val,
                    "leverage": lev_val,
                    "risk_pct": r_pct,
                    "sl_type": "atr_mult",
                    "sl_value": c["sl"],
                    "tp_type": "atr_mult",
                    "tp_value": c["tp"],
                    "trail_mult": c["trail"],
                    "fee_mode": "taker_maker_tp",
                    "slippage_bps": 2.0,
                    "params": c["params"],
                    "net_pnl_inr": round(float(res_obj.summary["net_pnl_inr"]), 2),
                    "return_pct": round(float(res_obj.summary["net_return_pct"]), 2),
                    "win_rate_pct": round(float(res_obj.summary["win_rate_pct"]), 2),
                    "profit_factor": round(float(res_obj.summary["profit_factor"]), 2) if res_obj.summary["profit_factor"] != float("inf") else 99.0,
                    "max_dd_pct": round(float(res_obj.summary["max_drawdown_pct"]), 2),
                    "total_trades": int(res_obj.summary["total_trades"]),
                    "avg_win_inr": round(tstats_obj["avg_win_inr"], 2),
                    "net_per_active_day_inr": round(tstats_obj["net_per_active_day"], 2),
                    "summary": s_dict,
                    "trades": serialize_trades(res_obj.trades, w["cfg100"].usd_inr_rate),
                })

    print("=" * 144)

    existing_strats = []
    if STRATEGIES_FILE.exists():
        try:
            existing_strats = json.loads(STRATEGIES_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing_strats = []
    kept_strats = [s for s in existing_strats if not str(s.get("name", "")).startswith("🔥")]
    final_strats = new_strats + kept_strats
    STRATEGIES_FILE.write_text(json.dumps(final_strats, indent=2), encoding="utf-8")

    existing_runs = []
    if SAVED_BACKTESTS_FILE.exists():
        try:
            existing_runs = json.loads(SAVED_BACKTESTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing_runs = []
    kept_runs = [r for r in existing_runs if not str(r.get("id", "")).startswith(("bt_30m_", "bt_45m_"))]
    final_runs = new_runs + kept_runs
    SAVED_BACKTESTS_FILE.write_text(json.dumps(final_runs, indent=2), encoding="utf-8")

    elapsed = time.perf_counter() - t0
    print(f"Registered {len(new_strats)} 30m/45m strategies (total {len(final_strats)}) & {len(new_runs)} backtest runs (total {len(final_runs)}) in {elapsed:.2f}s.")


if __name__ == "__main__":
    main()
