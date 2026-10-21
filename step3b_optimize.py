"""Step 3B: grid search with train (first 60d) / validation (last 30d) split + shared-wallet portfolio.

Usage:
    python step3b_optimize.py                 # 90 days, all CPU cores
    python step3b_optimize.py --workers 4

Every run is audited with backtester.audit_result (sizing, levels, fees+GST, funding, net, cooldown,
equity identity). Indicators are computed once on the full 90-day bar history (past-only), so the
validation window starts with warmed-up indicators but never sees future data.
"""

from __future__ import annotations

import argparse
import itertools
import logging
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from typing import Any

import pandas as pd
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

import data_manager as dm
import strategies
from backtester import Backtester, BacktestConfig, Leg, ProductSpec, audit_result
from config import settings

console = Console()

TIMEFRAMES = ["5m", "15m", "1h"]
TRAIN_DAYS = 60
STOP_GRID: dict[str, dict[str, Any]] = {
    "pct 1.5/3.5": dict(sl_mode="pct", stop_loss_pct=1.5, take_profit_pct=3.5),
    "pct 2/4.5": dict(sl_mode="pct", stop_loss_pct=2.0, take_profit_pct=4.5),
    "atr 1.5x/3.5x": dict(sl_mode="atr", sl_atr_mult=1.5, tp_atr_mult=3.5),
}
BREAKEVEN = [False, True]
FEE_MODES = ["taker", "taker_maker_tp"]
BASE = dict(initial_capital_inr=10_000.0, leverage=10, risk_per_trade_pct=2.0, cooldown_bars=1,
            max_daily_loss_pct=settings.MAX_DAILY_LOSS_PCT, exit_on_opposite_signal=False)

KEY = ["symbol", "strategy", "tf", "stops", "be", "fees"]


def grid() -> list[tuple[str, bool, str, BacktestConfig]]:
    return [
        (stop, be, fee, BacktestConfig(**BASE, **STOP_GRID[stop], breakeven_at_1r=be, fee_mode=fee))
        for stop, be, fee in itertools.product(STOP_GRID, BREAKEVEN, FEE_MODES)
    ]


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
_DATA: dict[str, pd.DataFrame] = {}


def _init_worker(days: int) -> None:
    logging.basicConfig(level=logging.ERROR)
    for sym in settings.SYMBOLS:
        _DATA[sym] = dm.load_unified_1m_dataset(sym, days=days)


def build_leg(df: pd.DataFrame, symbol: str, strategy: str, tf: str) -> Leg:
    bars = dm.resample_candles(df, tf)
    sig = strategies.generate_signals(strategy, bars)["signal"]
    return Leg(ProductSpec.from_metadata(symbol), df, sig, tf, strategies.atr(bars, 14), strategy)


def run_group(task: tuple[str, str, str, dict[str, tuple[pd.Timestamp, pd.Timestamp]]]) -> list[dict[str, Any]]:
    symbol, strategy, tf, windows = task
    leg = build_leg(_DATA[symbol], symbol, strategy, tf)
    rows = []
    for stop, be, fee, cfg in grid():
        for wname, (a, b) in windows.items():
            r = Backtester(cfg).run_portfolio([leg], a, b)
            s = r.summary
            rows.append({
                "symbol": symbol, "strategy": strategy, "tf": tf, "stops": stop, "be": be, "fees": fee,
                "window": wname, "trades": s["total_trades"], "win_pct": s["win_rate_pct"],
                "gross": s["gross_pnl_inr"], "fees_gst": s["fees_gst_inr"], "funding": s["funding_inr"],
                "net": s["net_pnl_inr"], "ret_pct": s["net_return_pct"], "pf": s["profit_factor"],
                "max_dd": s["max_drawdown_pct"], "daily_halts": s["daily_halts"],
                "exits": s["exit_reasons"], "audit_errors": len(audit_result(r)),
            })
    return rows


# --------------------------------------------------------------------------- #
# Reporting helpers
# --------------------------------------------------------------------------- #
def money(x: float) -> str:
    style = "green" if x > 0 else "red"
    return f"[{style}]{x:,.2f}[/]"


def pf_str(x: float) -> str:
    return "-" if math.isnan(x) else ("inf" if math.isinf(x) else f"{x:.2f}")


def config_label(row: pd.Series) -> str:
    return f"{row['stops']}{' +BE' if row['be'] else ''}"


def windows_for(df: pd.DataFrame) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    start = df.index[0]
    end = df.index[-1] + pd.Timedelta(minutes=1)
    split = start + pd.Timedelta(days=TRAIN_DAYS)
    return {"full": (start, end), "train": (start, split), "test": (split, end)}


def print_top(wide: pd.DataFrame, n: int, title: str) -> None:
    t = Table(title=title)
    cols = [("#", "right"), ("symbol", "left"), ("strategy", "left"), ("tf", "left"), ("stops", "left"),
            ("fees", "left"), ("trades", "right"), ("win %", "right"), ("gross ₹", "right"), ("fees+GST ₹", "right"),
            ("funding ₹", "right"), ("net ₹", "right"), ("ret %", "right"), ("PF", "right"), ("max DD %", "right"),
            ("IS 60d net ₹", "right"), ("OOS 30d net ₹", "right"), ("OOS PF", "right"), ("OOS trades", "right")]
    for c, j in cols:
        t.add_column(c, justify=j)
    for rank, (_, r) in enumerate(wide.head(n).iterrows(), 1):
        t.add_row(
            str(rank), r["symbol"], r["strategy"], r["tf"], config_label(r), r["fees"], str(r["trades"]),
            f"{r['win_pct']:.1f}", f"{r['gross']:,.2f}", f"{r['fees_gst']:,.2f}", f"{r['funding']:,.2f}",
            money(r["net"]), f"{r['ret_pct']:+.2f}", pf_str(r["pf"]), f"{r['max_dd']:.2f}",
            money(r["net_train"]), money(r["net_test"]), pf_str(r["pf_test"]), str(r["trades_test"]),
        )
    console.print(t)


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[RichHandler(console=console, show_path=False, show_time=False)])
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # 1) Data --------------------------------------------------------------- #
    console.rule(f"[bold]1. Extend 1m data to {args.days} days")
    dm.fetch_and_save_metadata(settings.SYMBOLS)
    dm.download_all(settings.SYMBOLS, args.days)
    data = {sym: dm.load_unified_1m_dataset(sym, days=args.days) for sym in settings.SYMBOLS}
    for sym, df in data.items():
        g = df.attrs["gaps"]
        console.print(
            f"{sym}: {len(df):,} 1m rows  {df.index[0]} -> {df.index[-1]}  gaps T/M/F = "
            f"{g['TRADE'].gap_count}/{g['MARK'].gap_count}/{g['FUNDING'].gap_count}"
        )
    windows = windows_for(data[settings.SYMBOLS[0]])
    for name, (a, b) in windows.items():
        console.print(f"  window {name:<5} {a} -> {b}  ({(b - a) / pd.Timedelta(days=1):.2f} days)")
    console.print()

    # 2) Grid ---------------------------------------------------------------- #
    configs = grid()
    tasks = [(sym, strat, tf, windows) for sym in settings.SYMBOLS for strat in strategies.STRATEGIES for tf in TIMEFRAMES]
    n_cfg = len(tasks) * len(configs)
    console.rule("[bold]2. Grid search")
    console.print(
        f"{len(settings.SYMBOLS)} symbols x {len(strategies.STRATEGIES)} strategies x {len(TIMEFRAMES)} timeframes x "
        f"{len(STOP_GRID)} stop sets x {len(BREAKEVEN)} breakeven x {len(FEE_MODES)} fee modes = {n_cfg} configurations "
        f"x 3 windows = {n_cfg * 3} backtests ({args.workers} workers)"
    )
    console.print(
        f"Fixed: ₹{BASE['initial_capital_inr']:,.0f}, {BASE['leverage']}x, risk {BASE['risk_per_trade_pct']}%/trade, "
        f"cooldown {BASE['cooldown_bars']} bar, daily-loss stop {BASE['max_daily_loss_pct']}%, ATR stop clamp 0.8-4.0%, "
        f"edge-triggered signals, SL/TP on trade price, ambiguous 1m -> SL first"
    )
    t0 = time.perf_counter()
    rows: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_init_worker, initargs=(args.days,)) as pool:
        for chunk in pool.map(run_group, tasks):
            rows.extend(chunk)
    res = pd.DataFrame(rows)
    elapsed = time.perf_counter() - t0
    audit_bad = int((res["audit_errors"] > 0).sum())
    console.print(
        f"Ran {len(res):,} backtests in {elapsed:.1f}s; audited {int(res['trades'].sum()):,} trades -> "
        f"{'[green]0 runs with ledger errors' if not audit_bad else f'[red]{audit_bad} runs with ledger errors'}"
    )
    out_dir = settings.data_path / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    res.drop(columns=["exits"]).to_csv(out_dir / "step3b_grid.csv", index=False)
    console.print(f"Saved all rows to {out_dir / 'step3b_grid.csv'}\n")

    full = res[res["window"] == "full"].set_index(KEY)
    train = res[res["window"] == "train"].set_index(KEY)
    test = res[res["window"] == "test"].set_index(KEY)
    wide = full.join(train[["net", "trades"]].add_suffix("_train")).join(test[["net", "trades", "pf"]].add_suffix("_test"))
    wide = wide.reset_index().sort_values("net", ascending=False)

    n_full = int((wide["net"] > 0).sum())
    n_train = int((wide["net_train"] > 0).sum())
    n_test = int((wide["net_test"] > 0).sum())
    consistent = wide[(wide["net_train"] > 0) & (wide["net_test"] > 0)]
    console.print(
        f"Net-profitable configurations: full 90d {n_full}/{len(wide)}, train 60d {n_train}/{len(wide)}, "
        f"validation 30d {n_test}/{len(wide)}, BOTH train and validation {len(consistent)}/{len(wide)}\n"
    )

    # 3) Reports -------------------------------------------------------------- #
    console.rule("[bold]3. Top 15 configurations over the full 90 days (ranked by net ₹)")
    print_top(wide, 15, "Full 90 days, with in-sample (first 60d) and out-of-sample (last 30d) net")
    console.print()

    console.rule("[bold]4. Consistent winners: net-profitable in BOTH train (60d) AND validation (30d)")
    if consistent.empty:
        console.print("[yellow]No configuration was net-profitable in both windows.\n")
    else:
        print_top(consistent, 5, "Top 5 consistent configurations (ranked by full-90d net)")
        console.print()

    # 4) Portfolio ------------------------------------------------------------ #
    pick = consistent.iloc[0] if not consistent.empty else wide.iloc[0]
    label = "#1 consistent" if not consistent.empty else "#1 overall (no consistent winner)"
    console.rule(f"[bold]5. Shared ₹10,000 BTCUSD + ETHUSD portfolio: {label}")
    cfg = BacktestConfig(**BASE, **STOP_GRID[pick["stops"]], breakeven_at_1r=bool(pick["be"]), fee_mode=pick["fees"])
    console.print(
        f"Strategy {pick['strategy']} {pick['tf']}  stops {config_label(pick)}  fees {pick['fees']}  "
        f"(selected on {pick['symbol']}); both symbols trade from one wallet, one position per symbol, "
        f"risk {cfg.risk_per_trade_pct}% of realized equity per entry, portfolio-level daily-loss stop\n"
    )
    legs = [build_leg(data[s], s, pick["strategy"], pick["tf"]) for s in settings.SYMBOLS]
    pt = Table(title="Portfolio results")
    for c, j in [("window", "left"), ("trades", "right"), ("BTC/ETH", "right"), ("win %", "right"), ("gross ₹", "right"),
                 ("fees+GST ₹", "right"), ("funding ₹", "right"), ("net ₹", "right"), ("ret %", "right"),
                 ("PF", "right"), ("max DD %", "right"), ("max concurrent", "right"), ("daily halts", "right"), ("audit", "left")]:
        pt.add_column(c, justify=j)
    port_results = {}
    for wname, (a, b) in windows.items():
        r = Backtester(cfg).run_portfolio(legs, a, b)
        port_results[wname] = r
        s, tr = r.summary, r.trades
        errs = audit_result(r)
        by_sym = tr["symbol"].value_counts() if len(tr) else pd.Series(dtype=int)
        conc = 0
        if len(tr):
            ev = pd.concat([pd.Series(1, index=tr["entry_time"]), pd.Series(-1, index=tr["exit_time"] + pd.Timedelta(seconds=1))])
            conc = int(ev.sort_index().cumsum().max())
        pt.add_row(
            {"full": "Full 90d", "train": "Train 60d", "test": "Validation 30d"}[wname], str(s["total_trades"]),
            f"{by_sym.get('BTCUSD', 0)}/{by_sym.get('ETHUSD', 0)}", f"{s['win_rate_pct']:.1f}",
            f"{s['gross_pnl_inr']:,.2f}", f"{s['fees_gst_inr']:,.2f}", f"{s['funding_inr']:,.2f}", money(s["net_pnl_inr"]),
            f"{s['net_return_pct']:+.2f}", pf_str(s["profit_factor"]), f"{s['max_drawdown_pct']:.2f}", str(conc),
            str(s["daily_halts"]), "[green]OK" if not errs else f"[red]{len(errs)} errors",
        )
    console.print(pt)

    full_r = port_results["full"]
    if len(full_r.trades):
        st = Table(title="Full 90d portfolio: per-symbol contribution")
        for c, j in [("symbol", "left"), ("trades", "right"), ("win %", "right"), ("gross ₹", "right"), ("fees+GST ₹", "right"),
                     ("funding ₹", "right"), ("net ₹", "right"), ("exits", "left")]:
            st.add_column(c, justify=j)
        rate = cfg.usd_inr_rate
        for sym, g in full_r.trades.groupby("symbol"):
            st.add_row(
                sym, str(len(g)), f"{(g['net_pnl_usd'] > 0).mean() * 100:.1f}", f"{g['gross_pnl_usd'].sum() * rate:,.2f}",
                f"{g['fees_usd'].sum() * rate:,.2f}", f"{g['funding_usd'].sum() * rate:,.2f}", money(g["net_pnl_inr"].sum()),
                str(g["exit_reason"].value_counts().to_dict()),
            )
        console.print(st)
        console.print(f"Final equity ₹{full_r.summary['final_equity_inr']:,.2f}  "
                      f"(exit reasons {full_r.summary['exit_reasons']}, skipped: cooldown {full_r.summary['skipped_cooldown']}, "
                      f"in-position {full_r.summary['skipped_in_position']}, daily-halt {full_r.summary['skipped_daily_halt']})\n")

    ok = audit_bad == 0 and all(not audit_result(r) for r in port_results.values())
    console.rule(f"Step 3B: {'[green]ledger audit PASS' if ok else '[red]ledger audit FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
