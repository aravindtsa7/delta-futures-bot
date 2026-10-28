"""Discover, validate & auto-register High-Win-Rate 'Small-Move Profit Harvester' strategies on BTCUSD, ETHUSD & BNBUSD (< 35s)."""

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

QUICK_CANDIDATES: list[dict[str, Any]] = [
    # --- 1. bb_squeeze_quick_tp (Squeeze Ignition + Quick Target Harvester) ---
    {
        "title": "BB Squeeze Quick-TP 2.2x ATR Harvester 1h",
        "rule_type": "bb_squeeze_quick_tp",
        "tf": "1h",
        "params": {"bb_period": 20, "bb_std": 2.0, "adx_min": 20, "adx_rising": 1, "bbw_expanding": 1, "vol_mult": 1.3, "body_ratio": 0.45, "ema_filter": 50, "cost_gate_mult": 2.8, "rt_cost": 0.00158, "use_4h_trend": 1, "squeeze_q": 0.65, "squeeze_lookback": 5},
        "sl": 2.0, "tp": 2.2, "trail": 2.2, "be": False,
    },
    {
        "title": "BB Squeeze Quick-TP 1.8x ATR Pure Maker 1h",
        "rule_type": "bb_squeeze_quick_tp",
        "tf": "1h",
        "params": {"bb_period": 20, "bb_std": 2.0, "adx_min": 20, "adx_rising": 1, "bbw_expanding": 1, "vol_mult": 1.3, "body_ratio": 0.45, "ema_filter": 50, "cost_gate_mult": 2.8, "rt_cost": 0.00158, "use_4h_trend": 1, "squeeze_q": 0.65, "squeeze_lookback": 5},
        "sl": 2.0, "tp": 1.8, "trail": 0.0, "be": False,
    },
    {
        "title": "BB Squeeze Quick-TP 2.5x ATR Momentum 1h",
        "rule_type": "bb_squeeze_quick_tp",
        "tf": "1h",
        "params": {"bb_period": 20, "bb_std": 2.0, "adx_min": 18, "adx_rising": 0, "bbw_expanding": 1, "vol_mult": 1.3, "body_ratio": 0.45, "ema_filter": 50, "cost_gate_mult": 2.0, "rt_cost": 0.00158, "use_4h_trend": 0, "squeeze_q": 0.60, "squeeze_lookback": 1},
        "sl": 2.0, "tp": 2.5, "trail": 2.2, "be": False,
    },
    {
        "title": "BB Squeeze Quick-TP 2.0x ATR Maker Sniper 1h",
        "rule_type": "bb_squeeze_quick_tp",
        "tf": "1h",
        "params": {"bb_period": 20, "bb_std": 2.0, "adx_min": 18, "adx_rising": 0, "bbw_expanding": 1, "vol_mult": 1.3, "body_ratio": 0.45, "ema_filter": 50, "cost_gate_mult": 2.0, "rt_cost": 0.00158, "use_4h_trend": 0, "squeeze_q": 0.60, "squeeze_lookback": 1},
        "sl": 2.0, "tp": 2.0, "trail": 0.0, "be": False, "risk": 1.5,
    },
    {
        "title": "BB Squeeze Quick-TP 2.5x ATR Trend 1h",
        "rule_type": "bb_squeeze_quick_tp",
        "tf": "1h",
        "params": {"bb_period": 20, "bb_std": 2.0, "adx_min": 19, "adx_rising": 1, "bbw_expanding": 1, "vol_mult": 1.25, "body_ratio": 0.40, "ema_filter": 50, "cost_gate_mult": 2.4, "rt_cost": 0.00158, "use_4h_trend": 1, "squeeze_q": 0.65, "squeeze_lookback": 4},
        "sl": 2.0, "tp": 2.5, "trail": 1.8, "be": False,
    },
    {
        "title": "BB Squeeze Quick-TP 2.2x ATR Scalp 15m",
        "rule_type": "bb_squeeze_quick_tp",
        "tf": "15m",
        "params": {"bb_period": 20, "bb_std": 2.1, "adx_min": 24, "adx_rising": 1, "bbw_expanding": 1, "vol_mult": 1.5, "body_ratio": 0.50, "ema_filter": 50, "cost_gate_mult": 2.8, "rt_cost": 0.00158, "use_4h_trend": 1, "squeeze_q": 0.50, "squeeze_lookback": 4},
        "sl": 1.8, "tp": 2.2, "trail": 2.0, "be": False,
    },
    # --- 2. ema_pullback_scalp (Trend EMA Pullback Bounce Harvester) ---
    {
        "title": "EMA20 Pullback Bounce Harvester 1h",
        "rule_type": "ema_pullback_scalp",
        "tf": "1h",
        "params": {"fast_ema": 20, "slow_ema": 50, "adx_min": 20, "pullback_tol": 0.002, "rsi_period": 14, "rsi_long_min": 44, "rsi_long_max": 60, "rsi_short_min": 40, "rsi_short_max": 56, "vol_mult": 1.1, "cost_gate_mult": 2.2, "rt_cost": 0.00158, "use_4h_trend": 1},
        "sl": 1.8, "tp": 2.2, "trail": 1.6, "be": False,
    },
    {
        "title": "EMA20 Pullback Pure Maker 1.8x TP 1h",
        "rule_type": "ema_pullback_scalp",
        "tf": "1h",
        "params": {"fast_ema": 20, "slow_ema": 50, "adx_min": 20, "pullback_tol": 0.002, "rsi_period": 14, "rsi_long_min": 44, "rsi_long_max": 60, "rsi_short_min": 40, "rsi_short_max": 56, "vol_mult": 1.1, "cost_gate_mult": 2.2, "rt_cost": 0.00158, "use_4h_trend": 1},
        "sl": 1.8, "tp": 1.8, "trail": 0.0, "be": False,
    },
    {
        "title": "EMA20 Pullback 2.5x ATR Bounce 1h",
        "rule_type": "ema_pullback_scalp",
        "tf": "1h",
        "params": {"fast_ema": 20, "slow_ema": 50, "adx_min": 18, "pullback_tol": 0.0025, "rsi_period": 14, "rsi_long_min": 42, "rsi_long_max": 60, "rsi_short_min": 40, "rsi_short_max": 58, "vol_mult": 1.0, "cost_gate_mult": 2.0, "rt_cost": 0.00158, "use_4h_trend": 1},
        "sl": 1.8, "tp": 2.5, "trail": 2.0, "be": False,
    },
    # --- 3. bb_rsi_snapback (Bollinger Band + RSI Extreme Snapback) ---
    {
        "title": "BB + RSI Extreme Snapback (4h Trend) 1h",
        "rule_type": "bb_rsi_snapback",
        "tf": "1h",
        "params": {"bb_period": 20, "bb_std": 2.0, "rsi_period": 14, "rsi_os": 35, "rsi_ob": 65, "cost_gate_mult": 2.2, "rt_cost": 0.00158, "use_4h_trend": 1},
        "sl": 1.8, "tp": 2.0, "trail": 2.0, "be": False,
    },
    {
        "title": "BB + RSI Snapback Pure Maker 1.8x TP 1h",
        "rule_type": "bb_rsi_snapback",
        "tf": "1h",
        "params": {"bb_period": 20, "bb_std": 2.0, "rsi_period": 14, "rsi_os": 35, "rsi_ob": 65, "cost_gate_mult": 2.2, "rt_cost": 0.00158, "use_4h_trend": 1},
        "sl": 1.8, "tp": 1.8, "trail": 0.0, "be": False,
    },
    # --- 4. vwap_st_burst (VWAP + Supertrend Micro-Momentum Burst) ---
    {
        "title": "VWAP + Supertrend Burst Harvester 1h",
        "rule_type": "vwap_st_burst",
        "tf": "1h",
        "params": {"atr_period": 10, "atr_mult": 2.5, "ema_period": 20, "vol_mult": 1.25, "adx_min": 20, "body_ratio": 0.40, "cost_gate_mult": 2.2, "rt_cost": 0.00158, "use_4h_trend": 1},
        "sl": 1.8, "tp": 2.3, "trail": 2.0, "be": False,
    },
    {
        "title": "VWAP + Supertrend Pure Maker 2.0x TP 1h",
        "rule_type": "vwap_st_burst",
        "tf": "1h",
        "params": {"atr_period": 10, "atr_mult": 2.5, "ema_period": 20, "vol_mult": 1.25, "adx_min": 20, "body_ratio": 0.40, "cost_gate_mult": 2.2, "rt_cost": 0.00158, "use_4h_trend": 1},
        "sl": 1.8, "tp": 2.0, "trail": 0.0, "be": False,
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


def ensure_bnb_data() -> None:
    dm.add_app_symbol("BNBUSD")
    p = dm.raw_1m_path("BNBUSD", "trade")
    if not p.exists():
        print("Downloading 90d BNBUSD 1m data...")
        dm.download_symbol_data("BNBUSD", days=90)


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
    ensure_bnb_data()
    symbols = ["BTCUSD", "ETHUSD", "BNBUSD"]
    datasets = {s: dm.load_unified_1m_dataset(s) for s in symbols}
    specs = {s: ProductSpec.from_metadata(s) for s in symbols}

    resampled: dict[tuple[str, str], tuple[pd.DataFrame, pd.Series]] = {}
    for s in symbols:
        for tf in ("15m", "1h"):
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

        print(f"\n--- {sym} ({eval_days}d) QUICK-MOVE HARVESTER SCAN ---")
        sym_cands = []
        for cand in QUICK_CANDIDATES:
            tf = cand["tf"]
            bars, atr14 = resampled[(sym, tf)]
            out = strategies.evaluate_custom_strategy(cand["rule_type"], cand["params"], bars)
            sig = out["signal"]

            r_pct = float(cand.get("risk", 2.0))
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
            if s50["total_trades"] < 6 or tr50.empty:
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

            print(f"  {cand['title']:<44} | {cand['rule_type']:<20} | {tf:<3} | WR={wr_val:>5.1f}% | Net50k={net50:>8.0f} | PF={pf_val:>4.2f} | DD={dd_val:>5.2f}% | Trd={s50['total_trades']:>3} | AvgW=₹{tstats50['avg_win_inr']:>5.0f} | H1={h1_net:>6.0f} H2={h2_net:>6.0f} | W={w_won}/6")

            score = (
                (5000.0 if net50 > 0 else -5000.0)
                + (1500.0 if both_halves else 0.0)
                + (500.0 if dd_val <= 15.0 else -20.0 * (dd_val - 15.0))
                + (wr_val * 25.0)
                + (w_won * 90.0)
                + (pf_val * 250.0)
                + (net50 / 40.0)
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
        top2 = sym_cands[:2]
        for w in top2:
            c = w["cand"]
            r_pct = float(c.get("risk", 2.0))
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
        best_by_sym[sym] = top2

    # Combined 3-Coin Portfolio:
    # (1) 365d BTC+ETH 2-Leg Portfolio + 86d BNB #1 Winner Combined
    # (2) Shared 86d Simultaneous 3-Leg Portfolio (BTC + ETH + BNB)
    bnb_w = best_by_sym["BNBUSD"][0]
    port_start_86 = bnb_w["start_eval"]
    port_end = bnb_w["end"]
    port_start_365 = best_by_sym["BTCUSD"][0]["start_eval"]

    legs50_2 = [best_by_sym["BTCUSD"][0]["leg50"], best_by_sym["ETHUSD"][0]["leg50"]]
    legs100_2 = [best_by_sym["BTCUSD"][0]["leg100"], best_by_sym["ETHUSD"][0]["leg100"]]
    legs50_3 = [best_by_sym[s][0]["leg50"] for s in symbols]
    legs100_3 = [best_by_sym[s][0]["leg100"] for s in symbols]
    bt_port50 = Backtester(best_by_sym["BTCUSD"][0]["cfg50"])
    bt_port100 = Backtester(best_by_sym["BTCUSD"][0]["cfg100"])

    port50_btc_eth = bt_port50.run_portfolio(legs50_2, port_start_365, port_end)
    port100_btc_eth = bt_port100.run_portfolio(legs100_2, port_start_365, port_end)
    port50_86 = bt_port50.run_portfolio(legs50_3, port_start_86, port_end)
    port100_86 = bt_port100.run_portfolio(legs100_3, port_start_86, port_end)

    comb_tr50 = pd.concat([best_by_sym[s][0]["res50"].trades for s in symbols], ignore_index=True).sort_values("entry_time")
    comb_tr100 = pd.concat([best_by_sym[s][0]["res100"].trades for s in symbols], ignore_index=True).sort_values("entry_time")

    print("\n" + "=" * 142)
    print("SMALL-MOVE PROFIT HARVESTER SUMMARY — PLAN A (₹50,000, 15x) & PLAN B (₹1,00,000, 20x) | Maker Limit TP (0.02%) + 18% GST + 2bps")
    print("=" * 142)
    hdr = f"{'Coin & Strategy':<42} | {'TF':<3} | {'SL/TP/Trl':<11} | {'WR%':<5} | {'AvgWin 50k/1L':<15} | {'₹50k Net (%)':<17} | {'₹1L Net (%)':<18} | {'PF':<4} | {'MaxDD':<6} | {'Trd':<3} | {'H1 / H2 (₹1L)':<17} | {'W'}"
    print(hdr)
    print("-" * 142)

    new_strats: list[dict[str, Any]] = []
    new_runs: list[dict[str, Any]] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for sym in symbols:
        short_s = sym.replace("USD", "")
        for rank, w in enumerate(best_by_sym[sym], start=1):
            c = w["cand"]
            sl_tp_str = f"{c['sl']}/{c['tp']}/{c['trail']}"
            avg_w_str = f"₹{w['tstats50']['avg_win_inr']:,.0f} / ₹{w['tstats100']['avg_win_inr']:,.0f}"
            n50_str = f"+₹{w['net50']:,.0f} (+{w['ret50']:.1f}%)" if w["net50"] >= 0 else f"-₹{abs(w['net50']):,.0f} ({w['ret50']:.1f}%)"
            n100_str = f"+₹{w['net100']:,.0f} (+{w['ret100']:.1f}%)" if w["net100"] >= 0 else f"-₹{abs(w['net100']):,.0f} ({w['ret100']:.1f}%)"
            h12_str = f"₹{w['h1_net100']:,.0f}/₹{w['h2_net100']:,.0f}"
            label = f"⚡ {short_s} #{rank}: {c['title']} ({w['eval_days']}d)"
            print(f"{label[:42]:<42} | {c['tf']:<3} | {sl_tp_str:<11} | {w['wr']:>4.1f}% | {avg_w_str:<15} | {n50_str:<17} | {n100_str:<18} | {w['pf']:>4.2f} | -{w['dd']:>4.2f}% | {w['trades']:>3} | {h12_str:<17} | {w['w_won']}/6")

            for cap_label, cap_val, lev_val, res_obj, tstats_obj in (
                ("₹1L", 100000.0, 20, w["res100"], w["tstats100"]),
                ("₹50k", 50000.0, 15, w["res50"], w["tstats50"]),
            ):
                if rank > 1 and cap_label == "₹50k":
                    continue
                sid = f"strat_scalp_{short_s.lower()}_{rank}_{cap_label.replace('₹', '').lower()}"
                sname = f"⚡ Scalp/Quick {short_s} #{rank} ({cap_label}): {c['title']}"
                new_strats.append({
                    "id": sid,
                    "name": sname,
                    "symbol": sym,
                    "timeframe": c["tf"],
                    "rule_type": c["rule_type"],
                    "params": {**c["params"], "breakeven_at_1r": c.get("be", False)},
                    "capital_inr": cap_val,
                    "leverage": lev_val,
                    "risk_pct": 2.0,
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
                    "id": f"bt_scalp_{short_s.lower()}_{rank}_{cap_label.replace('₹', '').lower()}_{w['eval_days']}d",
                    "saved_at": now_iso,
                    "strategy_id": sid,
                    "strategy_name": sname,
                    "symbol": sym,
                    "timeframe": c["tf"],
                    "period_days": w["eval_days"],
                    "capital_inr": cap_val,
                    "leverage": lev_val,
                    "risk_pct": 2.0,
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

    print("-" * 142)
    # Combined 3-Coin Full-History Composite (BTC 365d + ETH 365d + BNB 86d)
    ts50_c = trade_stats(comb_tr50)
    ts100_c = trade_stats(comb_tr100)
    c_net50 = float(comb_tr50["net_pnl_inr"].sum())
    c_net100 = float(comb_tr100["net_pnl_inr"].sum())
    c_wr100 = float((comb_tr100["net_pnl_inr"] > 0).mean() * 100.0)
    c_gw100 = float(comb_tr100[comb_tr100["net_pnl_inr"] > 0]["net_pnl_inr"].sum())
    c_gl100 = abs(float(comb_tr100[comb_tr100["net_pnl_inr"] <= 0]["net_pnl_inr"].sum()))
    c_pf100 = c_gw100 / max(1.0, c_gl100)
    c_dd100 = max(best_by_sym["BTCUSD"][0]["dd100"], best_by_sym["ETHUSD"][0]["dd100"])
    avg_w_c = f"₹{ts50_c['avg_win_inr']:,.0f} / ₹{ts100_c['avg_win_inr']:,.0f}"
    n50_c = f"+₹{c_net50:,.0f} (+{c_net50 / 500.0:.1f}%)"
    n100_c = f"+₹{c_net100:,.0f} (+{c_net100 / 1000.0:.1f}%)"
    day_c = f"₹{ts100_c['net_per_active_day']:,.0f}/active d"
    print(f"{'⚡ 3-COIN COMBINED (BTC+ETH 365d + BNB 86d)':<42} | {'1h':<3} | {'3-Coin Top1':<11} | {c_wr100:>4.1f}% | {avg_w_c:<15} | {n50_c:<17} | {n100_c:<18} | {c_pf100:>4.2f} | -{c_dd100:>4.2f}% | {len(comb_tr100):>3} | {day_c:<17} | ALL")

    for p_label, p_days, p50, p100 in (
        ("⚡ 2-COIN PORTFOLIO (BTC+ETH 365d Shared)", 365, port50_btc_eth, port100_btc_eth),
        ("⚡ 3-COIN PORTFOLIO (BTC+ETH+BNB 86d Shared)", 86, port50_86, port100_86),
    ):
        ts50 = trade_stats(p50.trades)
        ts100 = trade_stats(p100.trades)
        s50, s100 = p50.summary, p100.summary
        pf_p = float(s100["profit_factor"]) if s100["profit_factor"] != float("inf") else 5.0
        avg_w_p = f"₹{ts50['avg_win_inr']:,.0f} / ₹{ts100['avg_win_inr']:,.0f}"
        n50_p = f"+₹{s50['net_pnl_inr']:,.0f} (+{s50['net_return_pct']:.1f}%)"
        n100_p = f"+₹{s100['net_pnl_inr']:,.0f} (+{s100['net_return_pct']:.1f}%)"
        day_p = f"₹{ts100['net_per_active_day']:,.0f}/active d"
        print(f"{p_label:<42} | {'1h':<3} | {'Multi-Leg':<11} | {s100['win_rate_pct']:>4.1f}% | {avg_w_p:<15} | {n50_p:<17} | {n100_p:<18} | {pf_p:>4.2f} | -{s100['max_drawdown_pct']:>4.2f}% | {s100['total_trades']:>3} | {day_p:<17} | ALL")
    print("=" * 142)

    existing_strats = []
    if STRATEGIES_FILE.exists():
        try:
            existing_strats = json.loads(STRATEGIES_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing_strats = []
    kept_strats = [s for s in existing_strats if not str(s.get("name", "")).startswith("⚡")]
    final_strats = new_strats + kept_strats
    STRATEGIES_FILE.write_text(json.dumps(final_strats, indent=2), encoding="utf-8")

    existing_runs = []
    if SAVED_BACKTESTS_FILE.exists():
        try:
            existing_runs = json.loads(SAVED_BACKTESTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing_runs = []
    kept_runs = [r for r in existing_runs if not str(r.get("id", "")).startswith("bt_scalp_")]
    final_runs = new_runs + kept_runs
    SAVED_BACKTESTS_FILE.write_text(json.dumps(final_runs, indent=2), encoding="utf-8")

    elapsed = time.perf_counter() - t0
    print(f"Registered {len(new_strats)} Quick-Move Harvester presets (total {len(final_strats)}) & {len(new_runs)} backtest runs (total {len(final_runs)}) in {elapsed:.2f}s.")


if __name__ == "__main__":
    main()
