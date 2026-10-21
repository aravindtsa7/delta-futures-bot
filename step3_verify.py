"""Step 3 verification: 30-day data, lookahead test, 18-run strategy comparison, ledger math audit.

Usage:
    python step3_verify.py               # 30 days, taker fees, trade-price triggers
    python step3_verify.py --days 30 --fee-mode maker --trigger mark
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time

import numpy as np
import pandas as pd
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

import data_manager as dm
import strategies
from backtester import Backtester, BacktestConfig, BacktestResult, ProductSpec, audit_result
from config import settings

console = Console()

TIMEFRAMES = ["3m", "5m", "15m"]
TOL = 1e-9


def inr(x: float) -> str:
    return f"{x:,.2f}"


def close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=TOL, abs_tol=TOL)


# --------------------------------------------------------------------------- #
def step_data(days: int) -> dict[str, pd.DataFrame]:
    console.rule(f"[bold]1. Extend 1m data to {days} days")
    dm.fetch_and_save_metadata(settings.SYMBOLS)
    dm.download_all(settings.SYMBOLS, days)

    table = Table()
    for col in ("symbol", "series", "rows", "start (UTC)", "end (UTC)", "gaps"):
        table.add_column(col, justify="right" if col in ("rows", "gaps") else "left")
    for sym in settings.SYMBOLS:
        for st in dm.SERIES_TYPES:
            g = dm.find_gaps(dm.load_raw_1m(sym, st)["time"])
            table.add_row(sym, st, f"{g.rows:,}", str(pd.Timestamp(g.start, unit="s")), str(pd.Timestamp(g.end, unit="s")), str(g.gap_count))
    console.print(table)

    data: dict[str, pd.DataFrame] = {}
    bars_table = Table(title=f"Complete bars in the {days}-day unified dataset")
    bars_table.add_column("symbol")
    for tf in ["1m", *settings.SUPPORTED_RESAMPLE_TIMEFRAMES]:
        bars_table.add_column(tf, justify="right")
    for sym in settings.SYMBOLS:
        df = dm.load_unified_1m_dataset(sym, days=days)
        data[sym] = df
        bars_table.add_row(sym, f"{len(df):,}", *[f"{len(dm.resample_candles(df, tf)):,}" for tf in settings.SUPPORTED_RESAMPLE_TIMEFRAMES])
    console.print(bars_table)
    console.print()
    return data


def step_lookahead(data: dict[str, pd.DataFrame]) -> bool:
    """Signals/indicators computed on a truncated history must equal those on the full history."""
    console.rule("[bold]2. Lookahead test (truncated vs full history)")
    table = Table()
    for col in ("strategy", "comparisons", "signal mismatches", "indicator mismatches", "signals (full data)"):
        table.add_column(col, justify="left" if col == "strategy" else "right")
    all_ok = True
    for name, strat in strategies.STRATEGIES.items():
        comparisons = sig_bad = ind_bad = n_signals = 0
        for sym, df in data.items():
            for tf in TIMEFRAMES:
                bars = dm.resample_candles(df, tf)
                full = strat.generate(bars)
                n_signals += int((full["signal"] != 0).sum())
                for frac in (0.35, 0.5, 0.75, 0.9):
                    k = int(len(bars) * frac)
                    part = strat.generate(bars.iloc[:k])
                    ref = full.iloc[:k]
                    comparisons += 1
                    sig_bad += int((part["signal"] != ref["signal"]).sum())
                    num = part.select_dtypes("number").columns
                    a, b = part[num].to_numpy(float), ref[num].to_numpy(float)
                    ind_bad += int((~np.isclose(a, b, rtol=1e-10, atol=1e-10, equal_nan=True)).sum())
        ok = sig_bad == 0 and ind_bad == 0
        all_ok &= ok
        table.add_row(name, str(comparisons), f"[{'green' if not sig_bad else 'red'}]{sig_bad}", f"[{'green' if not ind_bad else 'red'}]{ind_bad}", f"{n_signals:,}")
    console.print(table)
    console.print(f"Lookahead test: {'[green]PASS' if all_ok else '[red]FAIL'}\n")
    return all_ok


def step_backtests(data: dict[str, pd.DataFrame], cfg: BacktestConfig) -> list[BacktestResult]:
    console.rule(
        f"[bold]3. Backtests: {len(data)} symbols x {len(strategies.STRATEGIES)} strategies x {len(TIMEFRAMES)} timeframes"
    )
    console.print(
        f"Capital ₹{cfg.initial_capital_inr:,.0f} (= ${cfg.initial_capital_inr / cfg.usd_inr_rate:,.4f} @ {cfg.usd_inr_rate:g})  "
        f"leverage {cfg.leverage:g}x  risk {cfg.risk_per_trade_pct:g}%  SL {cfg.stop_loss_pct:g}%  TP {cfg.take_profit_pct:g}%  "
        f"trailing {cfg.trailing_sl_pct:g}%  fees {cfg.fee_mode} + {cfg.gst_rate:.0%} GST  trigger={cfg.trigger_source}  "
        f"cooldown {cfg.cooldown_bars} bar  daily-loss stop {cfg.max_daily_loss_pct:g}%"
    )
    bt = Backtester(cfg)
    results: list[BacktestResult] = []
    t0 = time.perf_counter()
    for sym, df in data.items():
        product = ProductSpec.from_metadata(sym)
        for tf in TIMEFRAMES:
            bars = dm.resample_candles(df, tf)
            for name in strategies.STRATEGIES:
                sig = strategies.generate_signals(name, bars)["signal"]
                results.append(bt.run(df, sig, tf, product, name))
    console.print(f"Ran {len(results)} backtests in {time.perf_counter() - t0:.1f}s\n")

    results.sort(key=lambda r: r.summary["net_pnl_inr"], reverse=True)
    table = Table(title="Ranked by net P&L (INR)")
    cols = [
        ("#", "right"), ("symbol", "left"), ("strategy", "left"), ("tf", "left"), ("trades", "right"),
        ("L/S", "right"), ("win %", "right"), ("gross ₹", "right"), ("fees+GST ₹", "right"), ("funding ₹", "right"),
        ("net ₹", "right"), ("return %", "right"), ("PF", "right"), ("max DD %", "right"), ("ambig 1m", "right"),
        ("1-lot", "right"), ("skip pos", "right"),
    ]
    for c, j in cols:
        table.add_column(c, justify=j)
    for rank, r in enumerate(results, 1):
        s = r.summary
        pf = s["profit_factor"]
        net_style = "green" if s["net_pnl_inr"] > 0 else "red"
        table.add_row(
            str(rank), r.symbol, r.strategy, r.timeframe, str(s["total_trades"]),
            f"{s['long_trades']}/{s['short_trades']}", f"{s['win_rate_pct']:.1f}",
            inr(s["gross_pnl_inr"]), inr(s["fees_gst_inr"]), inr(s["funding_inr"]),
            f"[{net_style}]{inr(s['net_pnl_inr'])}", f"[{net_style}]{s['net_return_pct']:+.2f}",
            "-" if math.isnan(pf) else ("inf" if math.isinf(pf) else f"{pf:.2f}"),
            f"{s['max_drawdown_pct']:.2f}", str(s["ambiguous_1m_bars"]), str(s["min_lot_floor_used"]),
            str(s["skipped_in_position"]),
        )
    console.print(table)
    console.print()
    return results


def audit_run(r: BacktestResult) -> tuple[int, list[str]]:
    """Recompute every trade's sizing / levels / fees / GST / funding / net from first principles."""
    return len(r.trades), audit_result(r)


def step_detail(results: list[BacktestResult]) -> bool:
    console.rule("[bold]4. Ledger audit")
    all_ok = True
    total_trades = 0
    for r in results:
        n, errs = audit_run(r)
        total_trades += n
        if errs:
            all_ok = False
            console.print(f"[red]{r.symbol} {r.strategy} {r.timeframe}: {len(errs)} error(s): {errs[:5]}")
    console.print(
        f"Recomputed contracts, SL/TP tick rounding, gross, fees+GST, funding and net for {total_trades:,} trades "
        f"across {len(results)} runs; equity identity (initial + Σnet = final) checked per run: "
        f"{'[green]PASS' if all_ok else '[red]FAIL'}\n"
    )

    best = results[0]
    cfg, p = best.config, best.product
    cv, rate = p.contract_value, cfg.usd_inr_rate
    console.rule(f"[bold]5. Best run: {best.symbol} {best.strategy} {best.timeframe} - last 5 trades")
    console.print(
        f"{p.symbol}: contract_value={cv:g} {p.symbol[:3]}, tick_size={p.tick_size:g}, "
        f"fee_mode={cfg.fee_mode}: entry {cfg.entry_fee_rate:g}, SL exit {cfg.exit_fee_rate('SL'):g}, "
        f"TP exit {cfg.exit_fee_rate('TP'):g}, x (1 + {cfg.gst_rate:g} GST)\n"
    )
    if best.trades.empty:
        console.print("[yellow]No trades in best run")
        return all_ok

    bt = Backtester(cfg)
    last5 = best.trades.tail(5)
    for tr in last5.itertuples():
        d = 1 if tr.side == "LONG" else -1
        wallet = tr.equity_at_entry_usd
        n, n_risk, n_cap, floor_used = bt.size_position(wallet, tr.entry_price, tr.initial_sl, p, tr.available_at_entry_usd)
        fm, fx = cfg.with_gst(cfg.entry_fee_rate), cfg.with_gst(cfg.exit_fee_rate("SL"))
        loss_pc = cv * abs(tr.entry_price - tr.initial_sl) + cv * tr.entry_price * fm + cv * tr.initial_sl * fx
        risk_usd = wallet * cfg.risk_per_trade_pct / 100
        m_pc = cv * tr.entry_price / cfg.leverage
        rt_pc = cv * tr.entry_price * (fm + fx)
        qty = tr.contracts * cv
        console.print(
            f"[bold]Trade #{tr.trade_id} {tr.side}[/]  signal bar {tr.signal_bar:%Y-%m-%d %H:%M} -> "
            f"entry {tr.entry_time:%Y-%m-%d %H:%M} @ {tr.entry_price:g}  exit {tr.exit_time:%Y-%m-%d %H:%M} @ {tr.exit_price:g} "
            f"[{tr.exit_reason}]  held {tr.minutes_held}m"
        )
        console.print(
            f"  SL {tr.initial_sl:g} / TP {tr.tp_price:g}  (entry x (1 {'-' if d == 1 else '+'} {cfg.stop_loss_pct:g}%) / "
            f"(1 {'+' if d == 1 else '-'} {cfg.take_profit_pct:g}%), rounded to tick {p.tick_size:g})"
        )
        console.print(
            f"  sizing: equity ₹{wallet * rate:,.2f} = ${wallet:.4f}; risk {cfg.risk_per_trade_pct:g}% = ${risk_usd:.4f}; "
            f"loss/contract at SL = {cv:g}x{abs(tr.entry_price - tr.initial_sl):g} + fees {cv * tr.entry_price * fm:.6f} + {cv * tr.initial_sl * fx:.6f} "
            f"= ${loss_pc:.6f} -> floor = {n_risk}"
        )
        console.print(
            f"          margin cap = floor(${wallet:.4f} x {cfg.margin_buffer} / (${m_pc:.4f} margin + ${rt_pc:.6f} rt fees)) = {n_cap}"
            f"  -> contracts = {tr.contracts}{' (1-lot floor)' if floor_used else ''}  [recomputed {n}]"
        )
        console.print(
            f"  notional in  = {tr.contracts} x {cv:g} x {tr.entry_price:g} = ${tr.notional_entry_usd:.6f}; "
            f"entry fee = x {tr.entry_fee_rate:g} x {1 + cfg.gst_rate:g} = ${tr.entry_fee_usd:.6f}; margin = /{cfg.leverage:g} = ${tr.margin_usd:.6f}"
        )
        console.print(
            f"  notional out = {tr.contracts} x {cv:g} x {tr.exit_price:g} = ${tr.notional_exit_usd:.6f}; "
            f"exit fee = x {tr.exit_fee_rate:g} x {1 + cfg.gst_rate:g} = ${tr.exit_fee_usd:.6f}   (GST portion of both fees ${tr.gst_usd:.6f})"
        )
        console.print(
            f"  gross = {d:+d} x {qty:g} x ({tr.exit_price:g} - {tr.entry_price:g}) = ${tr.gross_pnl_usd:.6f}; "
            f"funding ({tr.funding_events} settlement(s)) = ${tr.funding_usd:.6f}"
        )
        console.print(
            f"  net = {tr.gross_pnl_usd:.6f} - {tr.entry_fee_usd:.6f} - {tr.exit_fee_usd:.6f} + {tr.funding_usd:.6f} "
            f"= ${tr.net_pnl_usd:.6f} = [bold]₹{tr.net_pnl_inr:,.4f}[/]   equity after ₹{tr.equity_after_inr:,.2f}\n"
        )

    f = best.funding
    if len(f):
        ft = Table(title=f"Funding settlements in best run (last {min(8, len(f))} of {len(f)})")
        for col in ("trade", "time (UTC)", "side", "contracts", "mark", "notional $", "rate %", "cashflow $", "cashflow ₹"):
            ft.add_column(col, justify="left" if col in ("time (UTC)", "side") else "right")
        for row in f.tail(8).itertuples():
            ft.add_row(
                str(row.trade_id), f"{row.time:%Y-%m-%d %H:%M}", row.side, str(row.contracts), f"{row.mark_price:.4f}",
                f"{row.notional_usd:.6f}", f"{row.funding_rate_pct:.8f}", f"{row.cashflow_usd:+.8f}", f"{row.cashflow_inr:+.6f}",
            )
        console.print(ft)
        console.print("  cashflow = -direction x contracts x contract_value x mark x (rate% / 100); longs pay when rate > 0\n")
    else:
        console.print("[yellow]Best run held no position across a funding timestamp.\n")

    s = best.summary
    console.print(
        f"Best run summary: trades={s['total_trades']} win%={s['win_rate_pct']:.1f} gross=₹{inr(s['gross_pnl_inr'])} "
        f"fees+GST=₹{inr(s['fees_gst_inr'])} funding=₹{s['funding_inr']:,.4f} net=₹{inr(s['net_pnl_inr'])} "
        f"({s['net_return_pct']:+.2f}%) PF={s['profit_factor']:.2f} maxDD={s['max_drawdown_pct']:.2f}% "
        f"exits={s['exit_reasons']} final equity=₹{s['final_equity_inr']:,.2f}\n"
    )
    return all_ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--fee-mode", choices=["taker", "taker_maker_tp", "maker"], default="taker")
    ap.add_argument("--trigger", choices=["trade", "mark"], default="trade")
    ap.add_argument("--trailing-sl", type=float, default=0.0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[RichHandler(console=console, show_path=False, show_time=False)])
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    cfg = BacktestConfig(
        initial_capital_inr=10_000.0, leverage=10, stop_loss_pct=1.0, take_profit_pct=2.0,
        fee_mode=args.fee_mode, trigger_source=args.trigger, trailing_sl_pct=args.trailing_sl,
    )
    data = step_data(args.days)
    look_ok = step_lookahead(data)
    results = step_backtests(data, cfg)
    audit_ok = step_detail(results)

    passed = look_ok and audit_ok
    console.rule(f"Step 3 verification: {'[green]PASS' if passed else '[red]FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
