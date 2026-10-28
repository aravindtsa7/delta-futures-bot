"""Discover, validate & auto-register the best 365-day walk-forward strategies for BTCUSD & ETHUSD (< 20s)."""

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

CANDIDATES: list[dict[str, Any]] = [
    # 1. bb_squeeze_adx
    {
        "title": "BB Squeeze ADX Ignition 1h",
        "rule_type": "bb_squeeze_adx",
        "tf": "1h",
        "params": {"bb_period": 20, "bb_std": 2.0, "adx_min": 18, "vol_mult": 1.3, "body_ratio": 0.45, "ema_filter": 50, "cost_gate_mult": 2.0, "use_4h_trend": 1, "squeeze_q": 0.65},
        "sl": 2.0, "tp": 5.0, "trail": 2.5, "risk": 1.0, "fee_mode": "taker_maker_tp", "slip": 2.0, "be": False,
    },
    {
        "title": "BB Squeeze ADX CostGate 1h (5bps Taker)",
        "rule_type": "bb_squeeze_adx",
        "tf": "1h",
        "params": {"bb_period": 20, "bb_std": 2.0, "adx_min": 16, "vol_mult": 1.3, "body_ratio": 0.35, "ema_filter": 50, "cost_gate_mult": 2.0, "use_4h_trend": 1, "squeeze_q": 0.60},
        "sl": 2.0, "tp": 5.0, "trail": 2.5, "risk": 1.0, "fee_mode": "taker", "slip": 5.0, "be": False,
    },
    {
        "title": "BB Squeeze ADX Breakout 1h (No 4h Gate)",
        "rule_type": "bb_squeeze_adx",
        "tf": "1h",
        "params": {"bb_period": 20, "bb_std": 2.0, "adx_min": 18, "vol_mult": 1.3, "body_ratio": 0.45, "ema_filter": 50, "cost_gate_mult": 2.0, "use_4h_trend": 0, "squeeze_q": 0.60},
        "sl": 2.0, "tp": 5.0, "trail": 2.5, "risk": 1.0, "fee_mode": "taker_maker_tp", "slip": 2.0, "be": False,
    },
    {
        "title": "BB Squeeze ADX High-Conviction 1h (2% Risk)",
        "rule_type": "bb_squeeze_adx",
        "tf": "1h",
        "params": {"bb_period": 20, "bb_std": 2.1, "adx_min": 20, "vol_mult": 1.35, "body_ratio": 0.45, "ema_filter": 50, "cost_gate_mult": 2.2, "use_4h_trend": 1, "squeeze_q": 0.60},
        "sl": 2.0, "tp": 5.5, "trail": 2.5, "risk": 2.0, "fee_mode": "taker_maker_tp", "slip": 2.0, "be": True,
    },
    # 2. supertrend_ema_pullback
    {
        "title": "Supertrend EMA21 Pullback 1h",
        "rule_type": "supertrend_ema_pullback",
        "tf": "1h",
        "params": {"atr_period": 10, "atr_mult": 3.0, "pullback_ema": 21, "ema_4h": 100, "pullback_tol": 0.003, "rsi_period": 14, "rsi_long_min": 45, "rsi_long_max": 68, "rsi_short_min": 32, "rsi_short_max": 55, "adx_min": 18, "cost_gate_mult": 2.0},
        "sl": 2.0, "tp": 5.0, "trail": 2.5, "risk": 1.0, "fee_mode": "taker_maker_tp", "slip": 2.0, "be": False,
    },
    {
        "title": "Supertrend EMA21 Continuation 1h (BE+Trail)",
        "rule_type": "supertrend_ema_pullback",
        "tf": "1h",
        "params": {"atr_period": 10, "atr_mult": 3.0, "pullback_ema": 21, "ema_4h": 200, "pullback_tol": 0.003, "rsi_period": 14, "rsi_long_min": 48, "rsi_long_max": 70, "rsi_short_min": 30, "rsi_short_max": 52, "adx_min": 20, "cost_gate_mult": 2.0},
        "sl": 2.0, "tp": 5.0, "trail": 2.5, "risk": 1.0, "fee_mode": "taker_maker_tp", "slip": 2.0, "be": True,
    },
    {
        "title": "Supertrend Macro Pullback 4h",
        "rule_type": "supertrend_ema_pullback",
        "tf": "4h",
        "params": {"atr_period": 10, "atr_mult": 3.0, "pullback_ema": 21, "ema_4h": 100, "pullback_tol": 0.004, "rsi_period": 14, "rsi_long_min": 45, "rsi_long_max": 68, "rsi_short_min": 32, "rsi_short_max": 55, "adx_min": 18, "cost_gate_mult": 2.0},
        "sl": 2.0, "tp": 5.0, "trail": 2.5, "risk": 1.0, "fee_mode": "taker_maker_tp", "slip": 2.0, "be": False,
    },
    # 3. donchian_adx_breakout
    {
        "title": "Donchian 20 ADX Breakout 1h",
        "rule_type": "donchian_adx_breakout",
        "tf": "1h",
        "params": {"dc_period": 20, "adx_min": 20, "vol_mult": 1.25, "ema_filter": 200, "use_4h_trend": 1, "cost_gate_mult": 2.0},
        "sl": 2.0, "tp": 5.0, "trail": 2.5, "risk": 1.0, "fee_mode": "taker_maker_tp", "slip": 2.0, "be": False,
    },
    {
        "title": "Donchian 24 Chameleon Trail 1h",
        "rule_type": "donchian_adx_breakout",
        "tf": "1h",
        "params": {"dc_period": 24, "adx_min": 22, "vol_mult": 1.3, "ema_filter": 200, "use_4h_trend": 1, "cost_gate_mult": 2.0},
        "sl": 2.0, "tp": 5.5, "trail": 2.5, "risk": 1.0, "fee_mode": "taker_maker_tp", "slip": 2.0, "be": True,
    },
    {
        "title": "Donchian 15 ADX Breakout 4h",
        "rule_type": "donchian_adx_breakout",
        "tf": "4h",
        "params": {"dc_period": 15, "adx_min": 20, "vol_mult": 1.2, "ema_filter": 100, "use_4h_trend": 1, "cost_gate_mult": 2.0},
        "sl": 2.0, "tp": 5.0, "trail": 2.5, "risk": 1.0, "fee_mode": "taker_maker_tp", "slip": 2.0, "be": False,
    },
    # 4. ema_swing_4h
    {
        "title": "EMA 21/55 Swing Trend 4h",
        "rule_type": "ema_swing_4h",
        "tf": "4h",
        "params": {"fast_ema": 21, "slow_ema": 55, "trend_ema": 200, "adx_min": 18, "rsi_long": 52, "rsi_short": 48, "cost_gate_mult": 2.0},
        "sl": 2.0, "tp": 5.0, "trail": 2.5, "risk": 1.0, "fee_mode": "taker_maker_tp", "slip": 2.0, "be": False,
    },
    {
        "title": "EMA 21/55 Institutional Swing 1h",
        "rule_type": "ema_swing_4h",
        "tf": "1h",
        "params": {"fast_ema": 21, "slow_ema": 55, "trend_ema": 200, "adx_min": 20, "rsi_long": 52, "rsi_short": 48, "cost_gate_mult": 2.0},
        "sl": 2.0, "tp": 5.0, "trail": 2.5, "risk": 1.0, "fee_mode": "taker_maker_tp", "slip": 2.0, "be": False,
    },
    {
        "title": "EMA 13/34 Momentum Swing 4h (2% Risk)",
        "rule_type": "ema_swing_4h",
        "tf": "4h",
        "params": {"fast_ema": 13, "slow_ema": 34, "trend_ema": 100, "adx_min": 20, "rsi_long": 52, "rsi_short": 48, "cost_gate_mult": 2.0},
        "sl": 2.0, "tp": 5.0, "trail": 2.5, "risk": 2.0, "fee_mode": "taker_maker_tp", "slip": 2.0, "be": True,
    },
    # 5. keltner_momentum
    {
        "title": "Keltner MACD Impulse 1h",
        "rule_type": "keltner_momentum",
        "tf": "1h",
        "params": {"kc_period": 20, "kc_mult": 1.5, "atr_period": 14, "adx_min": 20, "ema_filter": 100, "use_4h_trend": 1, "cost_gate_mult": 2.0},
        "sl": 2.0, "tp": 5.0, "trail": 2.5, "risk": 1.0, "fee_mode": "taker_maker_tp", "slip": 2.0, "be": False,
    },
    {
        "title": "Keltner Squeeze Impulse 1h (BE+Trail)",
        "rule_type": "keltner_momentum",
        "tf": "1h",
        "params": {"kc_period": 20, "kc_mult": 1.5, "atr_period": 14, "adx_min": 20, "ema_filter": 100, "use_4h_trend": 0, "cost_gate_mult": 2.0},
        "sl": 2.0, "tp": 5.0, "trail": 2.5, "risk": 1.0, "fee_mode": "taker_maker_tp", "slip": 2.0, "be": True,
    },
    {
        "title": "Keltner MACD Impulse 4h",
        "rule_type": "keltner_momentum",
        "tf": "4h",
        "params": {"kc_period": 20, "kc_mult": 1.4, "atr_period": 14, "adx_min": 18, "ema_filter": 100, "use_4h_trend": 1, "cost_gate_mult": 2.0},
        "sl": 2.0, "tp": 5.0, "trail": 2.5, "risk": 1.0, "fee_mode": "taker_maker_tp", "slip": 2.0, "be": False,
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


def main() -> None:
    t0 = time.perf_counter()
    symbols = ["BTCUSD", "ETHUSD"]
    datasets = {s: dm.load_unified_1m_dataset(s) for s in symbols}
    specs = {s: ProductSpec.from_metadata(s) for s in symbols}

    resampled: dict[tuple[str, str], tuple[pd.DataFrame, pd.Series]] = {}
    for s in symbols:
        for tf in ("1h", "4h"):
            b = dm.resample_candles(datasets[s], tf)
            resampled[(s, tf)] = (b, strategies.atr(b, 14))

    winners_by_sym: dict[str, list[dict[str, Any]]] = {}

    for sym in symbols:
        df_1m = datasets[sym]
        spec = specs[sym]
        end = df_1m.index[-1] + pd.Timedelta(minutes=1)
        start_365 = end - pd.Timedelta(days=365)
        mid_180 = start_365 + pd.Timedelta(days=180)
        w6_edges = [start_365 + (end - start_365) * i / 6 for i in range(7)]

        sym_results = []
        for cand in CANDIDATES:
            tf = cand["tf"]
            bars, atr14 = resampled[(sym, tf)]
            out = strategies.evaluate_custom_strategy(cand["rule_type"], cand["params"], bars)
            sig = out["signal"]

            cfg = BacktestConfig(
                initial_capital_inr=50_000.0,
                leverage=10.0,
                risk_per_trade_pct=cand["risk"],
                max_daily_loss_pct=5.0,
                sl_mode="atr",
                sl_atr_mult=cand["sl"],
                tp_atr_mult=cand["tp"],
                trailing_atr_mult=cand["trail"],
                breakeven_at_1r=cand.get("be", False),
                fee_mode=cand["fee_mode"],
                slippage_bps=cand["slip"],
                cooldown_bars=1,
            )
            leg = Leg(spec, df_1m, sig, tf, atr14, cand["title"], None, cfg)
            bt = Backtester(cfg)

            res_365 = bt.run_portfolio([leg], start_365, end)
            s365 = res_365.summary
            tr = res_365.trades
            if s365["total_trades"] < 6 or tr.empty:
                continue

            # Fast walk-forward slice from trade timestamps
            h1_net = float(tr[tr["entry_time"] < mid_180]["net_pnl_inr"].sum())
            h2_net = float(tr[tr["entry_time"] >= mid_180]["net_pnl_inr"].sum())
            w_nets = [
                float(tr[(tr["entry_time"] >= w6_edges[wi]) & (tr["entry_time"] < w6_edges[wi + 1])]["net_pnl_inr"].sum())
                for wi in range(6)
            ]
            w_won = sum(1 for x in w_nets if x > 0)

            pf_val = float(s365["profit_factor"]) if s365["profit_factor"] != float("inf") else 5.0
            dd_val = max(0.5, float(s365["max_drawdown_pct"]))
            ret_dd = float(s365["net_return_pct"]) / dd_val
            net_365 = float(s365["net_pnl_inr"])
            both_halves = (h1_net > 0) and (h2_net > 0)
            passes_strict = (net_365 > 0) and both_halves and (w_won >= 4) and (dd_val <= 12.0)

            score = (
                (5000.0 if net_365 > 0 else -5000.0)
                + (2000.0 if passes_strict else 0.0)
                + (1000.0 if both_halves else 0.0)
                + (500.0 if dd_val <= 12.0 else -20.0 * (dd_val - 12.0))
                + (w_won * 80.0)
                + (pf_val * 200.0)
                + (ret_dd * 50.0)
                + (net_365 / 50.0)
            )
            sym_results.append({
                "cand": cand,
                "cfg": cfg,
                "leg": leg,
                "bt": bt,
                "res_365": res_365,
                "net_365": net_365,
                "ret_365": float(s365["net_return_pct"]),
                "wr_365": float(s365["win_rate_pct"]),
                "pf_365": pf_val,
                "dd_365": float(s365["max_drawdown_pct"]),
                "trades_365": int(s365["total_trades"]),
                "h1_net": h1_net,
                "h2_net": h2_net,
                "w_won": w_won,
                "passes_strict": passes_strict,
                "score": score,
                "mid_180": mid_180,
                "end": end,
            })

        sym_results.sort(key=lambda x: x["score"], reverse=True)
        top3 = sym_results[:3]
        for w in top3:
            w["res_180"] = w["bt"].run_portfolio([w["leg"]], w["mid_180"], w["end"])
        winners_by_sym[sym] = top3

    print("\n" + "=" * 120)
    print("365-DAY WALK-FORWARD STRATEGY DISCOVERY SUMMARY (Capital = ₹50,000, Leverage = 10x, Delta India Fees + 18% GST)")
    print("Baselines -> Step 4B BTC D_both: +₹2,107 (PF 1.15) | Step 4B ETH C_costgate: +₹4,687 (PF 1.12)")
    print("=" * 120)
    header = f"{'Rank & Strategy':<40} | {'Rule':<23} | {'TF':<3} | {'365d Net ₹ (%)':<20} | {'WR%':<5} | {'PF':<5} | {'MaxDD':<6} | {'Trd':<4} | {'H1 Net / H2 Net':<21} | {'W(6)'}"
    print(header)
    print("-" * 120)

    new_strategies: list[dict[str, Any]] = []
    new_saved_runs: list[dict[str, Any]] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for sym in symbols:
        short_sym = "BTC" if sym == "BTCUSD" else "ETH"
        for rank, w in enumerate(winners_by_sym[sym], start=1):
            c = w["cand"]
            sid = f"strat_{short_sym.lower()}_win_{rank}"
            sname = f"★ {short_sym} #{rank}: {c['title']} (365d Verified)"
            net_str = f"+₹{w['net_365']:,.0f} (+{w['ret_365']:.1f}%)" if w["net_365"] >= 0 else f"-₹{abs(w['net_365']):,.0f} ({w['ret_365']:.1f}%)"
            h_str = f"₹{w['h1_net']:,.0f} / ₹{w['h2_net']:,.0f}"
            print(f"{sname[:40]:<40} | {c['rule_type']:<23} | {c['tf']:<3} | {net_str:<20} | {w['wr_365']:>4.1f}% | {w['pf_365']:>5.2f} | -{w['dd_365']:>4.2f}% | {w['trades_365']:>4} | {h_str:<21} | {w['w_won']}/6")

            new_strategies.append({
                "id": sid,
                "name": sname,
                "symbol": sym,
                "timeframe": c["tf"],
                "rule_type": c["rule_type"],
                "params": c["params"],
                "capital_inr": 50000.0,
                "leverage": 10,
                "risk_pct": c["risk"],
                "sl_type": "atr_mult",
                "sl_value": c["sl"],
                "tp_type": "atr_mult",
                "tp_value": c["tp"],
                "trail_mult": c["trail"],
                "fee_mode": c["fee_mode"],
                "slippage_bps": c["slip"],
            })

            for period_days, res_obj in ((365, w["res_365"]), (180, w["res_180"])):
                s_dict = clean_json(res_obj.summary)
                new_saved_runs.append({
                    "id": f"bt_{short_sym.lower()}_{rank}_{period_days}d",
                    "saved_at": now_iso,
                    "strategy_id": sid,
                    "strategy_name": sname,
                    "symbol": sym,
                    "timeframe": c["tf"],
                    "period_days": float(period_days),
                    "capital_inr": 50000.0,
                    "leverage": 10,
                    "risk_pct": c["risk"],
                    "sl_type": "atr_mult",
                    "sl_value": c["sl"],
                    "tp_type": "atr_mult",
                    "tp_value": c["tp"],
                    "trail_mult": c["trail"],
                    "rule_type": c["rule_type"],
                    "params": c["params"],
                    "net_pnl_inr": round(float(res_obj.summary["net_pnl_inr"]), 2),
                    "return_pct": round(float(res_obj.summary["net_return_pct"]), 2),
                    "win_rate_pct": round(float(res_obj.summary["win_rate_pct"]), 2),
                    "profit_factor": round(float(res_obj.summary["profit_factor"]), 2) if res_obj.summary["profit_factor"] != float("inf") else "inf",
                    "max_dd_pct": round(float(res_obj.summary["max_drawdown_pct"]), 2),
                    "total_trades": int(res_obj.summary["total_trades"]),
                    "summary": s_dict,
                    "trades": serialize_trades(res_obj.trades, w["cfg"].usd_inr_rate)[:250],
                })
        print("-" * 120)

    starter_all = [
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

    all_strategies = new_strategies + starter_all
    STRATEGIES_FILE.write_text(json.dumps(clean_json(all_strategies), indent=2), encoding="utf-8")
    SAVED_BACKTESTS_FILE.write_text(json.dumps(clean_json(new_saved_runs), indent=2), encoding="utf-8")
    print(f"Saved {len(all_strategies)} strategies to {STRATEGIES_FILE.name} and {len(new_saved_runs)} backtest runs to {SAVED_BACKTESTS_FILE.name} in {time.perf_counter() - t0:.2f}s.")


if __name__ == "__main__":
    main()

