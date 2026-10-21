"""Step 3C: 180-day, 3-window walk-forward on 15m/1h/2h/4h + unified and specialist portfolios.

Usage:
    python step3c_walkforward.py                    # 180 days (+45 warm-up days), all cores
    python step3c_walkforward.py --workers 6

Data: the last ``--days`` days are split into W1/W2/W3 (60 days each). An extra ``--warmup-days``
of history before W1 is downloaded and used ONLY to warm up indicators (4h EMA200 needs ~33 days),
so W1 is not handicapped. Indicators are past-only; no window ever sees later data.

Selection honesty:
  * "Filter" / Mode A use all three windows -> in-sample by construction.
  * Mode A-WF and Mode B choose using W1+W2 only; their W3 numbers are true out-of-sample.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any

import numpy as np
import pandas as pd
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

import data_manager as dm
import strategies
from backtester import Backtester, BacktestConfig, BacktestResult, Leg, audit_result, make_leg
from config import settings

console = Console()

TIMEFRAMES = ["15m", "1h", "2h", "4h"]
WINDOW_DAYS = 60
WINDOWS = ["W1", "W2", "W3"]
FEE_MODE = "taker_maker_tp"
TRAIL_ATR = 2.5
MIN_TRADES_FULL = 18
MIN_TRADES_W12 = 12

STOPS: dict[str, dict[str, Any]] = {
    "pct 1.5/4": dict(sl_mode="pct", stop_loss_pct=1.5, take_profit_pct=4.0),
    "pct 2/5": dict(sl_mode="pct", stop_loss_pct=2.0, take_profit_pct=5.0),
    "atr 1.5x/4x": dict(sl_mode="atr", sl_atr_mult=1.5, tp_atr_mult=4.0),
    "atr 2x/5x": dict(sl_mode="atr", sl_atr_mult=2.0, tp_atr_mult=5.0),
}
EXITS: dict[str, dict[str, Any]] = {
    "fixed": {},
    "BE": dict(breakeven_at_1r=True),
    "trail": dict(trailing_atr_mult=TRAIL_ATR),
    "opp": dict(exit_on_opposite_signal=True),
    "trail+opp": dict(trailing_atr_mult=TRAIL_ATR, exit_on_opposite_signal=True),
    "BE+opp": dict(breakeven_at_1r=True, exit_on_opposite_signal=True),
}
BASE = dict(initial_capital_inr=10_000.0, leverage=10, risk_per_trade_pct=2.0, cooldown_bars=1,
            max_daily_loss_pct=settings.MAX_DAILY_LOSS_PCT, fee_mode=FEE_MODE)

KEY = ["symbol", "strategy", "tf", "stops", "exits"]
UKEY = ["strategy", "tf", "stops", "exits"]
WINDOW_LABEL = {"W1": "W1 (days 1-60)", "W2": "W2 (days 61-120)", "W3": "W3 (days 121-180)", "full": "Full 180d"}


def make_config(stops: str, exits: str) -> BacktestConfig:
    return BacktestConfig(**BASE, **STOPS[stops], **EXITS[exits])


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
_DATA: dict[str, pd.DataFrame] = {}


def _init_worker(total_days: int) -> None:
    logging.basicConfig(level=logging.ERROR)
    for sym in settings.SYMBOLS:
        _DATA[sym] = dm.load_unified_1m_dataset(sym, days=total_days)


def run_group(task: tuple[str, str, str, dict[str, tuple[pd.Timestamp, pd.Timestamp]]]) -> list[dict[str, Any]]:
    symbol, strategy, tf, windows = task
    leg = make_leg(symbol, strategy, tf, df_1m=_DATA[symbol])
    rows = []
    for stops, exits in itertools.product(STOPS, EXITS):
        cfg = make_config(stops, exits)
        for wname, (a, b) in windows.items():
            r = Backtester(cfg).run_portfolio([leg], a, b)
            s = r.summary
            rows.append({
                "symbol": symbol, "strategy": strategy, "tf": tf, "stops": stops, "exits": exits, "window": wname,
                "trades": s["total_trades"], "win_pct": s["win_rate_pct"], "gross": s["gross_pnl_inr"],
                "fees_gst": s["fees_gst_inr"], "funding": s["funding_inr"], "net": s["net_pnl_inr"],
                "ret_pct": s["net_return_pct"], "pf": s["profit_factor"], "max_dd": s["max_drawdown_pct"],
                "audit_errors": len(audit_result(r)),
            })
    return rows


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def money(x: float) -> str:
    return f"[{'green' if x > 0 else 'red'}]{x:,.2f}[/]"


def pf_str(x: float) -> str:
    return "-" if (x is None or math.isnan(x)) else ("inf" if math.isinf(x) else f"{x:.2f}")


def save_window_anchor(windows: dict[str, tuple[pd.Timestamp, pd.Timestamp]], warmup_start: pd.Timestamp, path) -> None:
    """Persist the exact window boundaries so other tools can reproduce these results to the paisa."""
    payload = {
        "warmup_start": warmup_start.isoformat(),
        "start": windows["full"][0].isoformat(),
        "end": windows["full"][1].isoformat(),
        "window_days": WINDOW_DAYS,
        "windows": {w: [a.isoformat(), b.isoformat()] for w, (a, b) in windows.items()},
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def make_windows(df: pd.DataFrame, days: int) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    end = df.index[-1] + pd.Timedelta(minutes=1)
    start = end - pd.Timedelta(days=days)
    w = {f"W{k + 1}": (start + pd.Timedelta(days=WINDOW_DAYS * k), start + pd.Timedelta(days=WINDOW_DAYS * (k + 1)))
         for k in range(3)}
    w["W3"] = (w["W3"][0], end)
    w["full"] = (start, end)
    return w


def step_data(total_days: int, days: int) -> dict[str, pd.DataFrame]:
    console.rule(f"[bold]1. Extend 1m data to {days} days (+{total_days - days} warm-up days)")
    dm.fetch_and_save_metadata(settings.SYMBOLS)
    dm.download_all(settings.SYMBOLS, total_days)
    t = Table()
    for c in ("symbol", "series", "rows", "start (UTC)", "end (UTC)", "gaps"):
        t.add_column(c, justify="right" if c in ("rows", "gaps") else "left")
    data = {}
    for sym in settings.SYMBOLS:
        df = dm.load_unified_1m_dataset(sym, days=total_days)
        data[sym] = df
        cutoff = int(df.index[0].timestamp())
        for st in dm.SERIES_TYPES:
            raw = dm.load_raw_1m(sym, st)
            g = dm.find_gaps(raw.loc[raw["time"] >= cutoff, "time"])
            t.add_row(sym, st, f"{g.rows:,}", str(pd.Timestamp(g.start, unit="s")), str(pd.Timestamp(g.end, unit="s")), str(g.gap_count))
    console.print(t)
    return data


def step_parity(data: dict[str, pd.DataFrame]) -> bool:
    console.rule("[bold]2. Resample parity vs GET /v2/history/candles (last 10 closed bars)")
    t = Table()
    for c in ("symbol", "tf", "bars compared", "first bar (UTC)", "last bar (UTC)", "OHLCV exact", "result"):
        t.add_column(c)
    ok_all = True
    for sym, df in data.items():
        for tf in ["1h", "2h", "4h"]:
            local = dm.resample_candles(df, tf).tail(10)
            a, b = int(local.index[0].timestamp()), int(local.index[-1].timestamp())
            api = dm._normalise_raw(pd.DataFrame.from_records(
                dm.client().get("/v2/history/candles", {"resolution": tf, "symbol": sym, "start": a, "end": b})
            )).set_index("time")
            exact = 0
            for ts, bar in local.iterrows():
                k = int(ts.timestamp())
                if k in api.index and all(bar[c] == api.at[k, c] for c in dm.OHLCV_COLUMNS):
                    exact += 1
            ok = exact == len(local)
            ok_all &= ok
            t.add_row(sym, tf, str(len(local)), f"{local.index[0]:%Y-%m-%d %H:%M}", f"{local.index[-1]:%Y-%m-%d %H:%M}",
                      f"{exact}/{len(local)}", "[green]PASS" if ok else "[red]FAIL")
    console.print(t)
    return ok_all


def step_lookahead(data: dict[str, pd.DataFrame]) -> bool:
    console.rule("[bold]3. Lookahead test, new strategies (truncated vs full history)")
    ok = True
    for name in ("keltner_squeeze_momentum", "supertrend_donchian_trend"):
        strat = strategies.STRATEGIES[name]
        mism = comps = sigs = exits = 0
        for df in data.values():
            for tf in TIMEFRAMES:
                bars = dm.resample_candles(df, tf)
                full = strat.generate(bars)
                sigs += int((full["signal"] != 0).sum())
                exits += int((full.get("exit_signal", pd.Series(0)) != 0).sum())
                for frac in (0.4, 0.7, 0.9):
                    k = int(len(bars) * frac)
                    part = strat.generate(bars.iloc[:k])
                    num = part.select_dtypes("number").columns
                    mism += int((~np.isclose(part[num].to_numpy(float), full.iloc[:k][num].to_numpy(float),
                                             rtol=1e-10, atol=1e-10, equal_nan=True)).sum())
                    comps += 1
        ok &= mism == 0
        console.print(f"{name:<27} comparisons={comps}  mismatched values={mism}  entry signals={sigs:,}  exit signals={exits:,}  "
                      f"{'[green]PASS' if mism == 0 else '[red]FAIL'}")
    console.print()
    return ok


def build_wide(res: pd.DataFrame) -> pd.DataFrame:
    full = res[res["window"] == "full"].set_index(KEY)
    wide = full.drop(columns=["window"]).copy()
    for w in WINDOWS:
        part = res[res["window"] == w].set_index(KEY)
        wide[f"net_{w}"] = part["net"]
        wide[f"trades_{w}"] = part["trades"]
    wide["pos_windows"] = sum((wide[f"net_{w}"] > 0).astype(int) for w in WINDOWS)
    wide["net_W12"] = wide["net_W1"] + wide["net_W2"]
    wide["trades_W12"] = wide["trades_W1"] + wide["trades_W2"]
    wide["passes"] = (wide["trades"] >= MIN_TRADES_FULL) & (wide["pos_windows"] >= 2) & (wide["net"] > 0)
    return wide.reset_index()


def config_table(df: pd.DataFrame, title: str, n: int, extra_w12: bool = False) -> Table:
    t = Table(title=title)
    cols = [("#", "right"), ("symbol", "left"), ("strategy", "left"), ("tf", "left"), ("stops", "left"), ("exits", "left"),
            ("trades", "right"), ("win %", "right"), ("W1 ₹", "right"), ("W2 ₹", "right"), ("W3 ₹", "right"),
            ("+win", "right"), ("full net ₹", "right"), ("ret %", "right"), ("PF", "right"), ("max DD %", "right"),
            ("fees+GST ₹", "right")]
    if extra_w12:
        cols.insert(11, ("W1+W2 ₹", "right"))
    for c, j in cols:
        t.add_column(c, justify=j, no_wrap=c == "strategy")
    for rank, (_, r) in enumerate(df.head(n).iterrows(), 1):
        row = [str(rank), r["symbol"], r["strategy"], r["tf"], r["stops"], r["exits"], str(r["trades"]),
               f"{r['win_pct']:.1f}", money(r["net_W1"]), money(r["net_W2"]), money(r["net_W3"]),
               f"{r['pos_windows']}/3", money(r["net"]), f"{r['ret_pct']:+.2f}", pf_str(r["pf"]), f"{r['max_dd']:.2f}",
               f"{r['fees_gst']:,.2f}"]
        if extra_w12:
            row.insert(11, money(r["net_W12"]))
        t.add_row(*row)
    return t


def run_portfolio_mode(title: str, legs: list[Leg], windows: dict[str, tuple[pd.Timestamp, pd.Timestamp]],
                       oos_note: str) -> dict[str, BacktestResult]:
    base_cfg = BacktestConfig(**BASE)
    t = Table(title=title)
    for c, j in [("window", "left"), ("trades", "right"), ("BTC/ETH", "right"), ("win %", "right"), ("gross ₹", "right"),
                 ("fees+GST ₹", "right"), ("funding ₹", "right"), ("net ₹", "right"), ("ret %", "right"), ("PF", "right"),
                 ("max DD %", "right"), ("max conc.", "right"), ("halts", "right"), ("audit", "left")]:
        t.add_column(c, justify=j)
    results = {}
    for w in [*WINDOWS, "full"]:
        a, b = windows[w]
        r = Backtester(base_cfg).run_portfolio(legs, a, b)
        results[w] = r
        s, tr = r.summary, r.trades
        errs = audit_result(r)
        by = tr["symbol"].value_counts() if len(tr) else pd.Series(dtype=int)
        conc = 0
        if len(tr):
            ev = pd.concat([pd.Series(1, index=tr["entry_time"]), pd.Series(-1, index=tr["exit_time"] + pd.Timedelta(seconds=1))])
            conc = int(ev.sort_index().cumsum().max())
        label = WINDOW_LABEL[w] + (" [OOS]" if (w == "W3" and oos_note) else "")
        t.add_row(label, str(s["total_trades"]), f"{by.get('BTCUSD', 0)}/{by.get('ETHUSD', 0)}", f"{s['win_rate_pct']:.1f}",
                  f"{s['gross_pnl_inr']:,.2f}", f"{s['fees_gst_inr']:,.2f}", f"{s['funding_inr']:,.2f}", money(s["net_pnl_inr"]),
                  f"{s['net_return_pct']:+.2f}", pf_str(s["profit_factor"]), f"{s['max_drawdown_pct']:.2f}", str(conc),
                  str(s["daily_halts"]), "[green]OK" if not errs else f"[red]{len(errs)} err")
    console.print(t)
    full = results["full"]
    if len(full.trades):
        st = Table(title="Full 180d: per-symbol contribution")
        for c, j in [("symbol", "left"), ("strategy / tf / stops / exits", "left"), ("trades", "right"), ("win %", "right"),
                     ("gross ₹", "right"), ("fees+GST ₹", "right"), ("funding ₹", "right"), ("net ₹", "right"), ("exits", "left")]:
            st.add_column(c, justify=j)
        rate = base_cfg.usd_inr_rate
        for leg in legs:
            sym = leg.product.symbol
            g = full.trades[full.trades["symbol"] == sym]
            c = leg.config
            st.add_row(sym, f"{leg.strategy} / {leg.timeframe} / {c.stop_label} / {exit_label(c)}", str(len(g)),
                       f"{(g['net_pnl_usd'] > 0).mean() * 100:.1f}" if len(g) else "-",
                       f"{g['gross_pnl_usd'].sum() * rate:,.2f}", f"{g['fees_usd'].sum() * rate:,.2f}",
                       f"{g['funding_usd'].sum() * rate:,.2f}", money(g["net_pnl_inr"].sum()),
                       str(g["exit_reason"].value_counts().to_dict()))
        console.print(st)
    if oos_note:
        console.print(f"[dim]{oos_note}[/]")
    console.print()
    return results


def exit_label(c: BacktestConfig) -> str:
    for name, kw in EXITS.items():
        if all(getattr(c, k) == v for k, v in kw.items()) and (
            (c.trailing_atr_mult > 0) == ("trailing_atr_mult" in kw)
            and c.breakeven_at_1r == kw.get("breakeven_at_1r", False)
            and c.exit_on_opposite_signal == kw.get("exit_on_opposite_signal", False)
        ):
            return name
    return "custom"


# --------------------------------------------------------------------------- #
# Portfolio selection rules (shared with the dashboard API)
# --------------------------------------------------------------------------- #
def unified_pivot(wide: pd.DataFrame) -> pd.DataFrame:
    return wide.pivot_table(index=UKEY, columns="symbol",
                            values=["net", "passes", "net_W1", "net_W2", "net_W3", "trades_W12", "net_W12"], aggfunc="first")


def select_mode_a(wide: pd.DataFrame, symbols: list[str]) -> tuple[tuple, str]:
    """Same config on both symbols: must pass the filter on both; fallback best combined full net."""
    a, b = symbols
    both = unified_pivot(wide)
    both_pass = both[(both[("passes", a)] == True) & (both[("passes", b)] == True)]  # noqa: E712
    if len(both_pass):
        combined = (both_pass[("net", a)] + both_pass[("net", b)]).sort_values(ascending=False)
        return combined.index[0], f"passes the filter on BOTH symbols ({len(both_pass)} such configs), best combined full net"
    combined = (both[("net", a)] + both[("net", b)]).sort_values(ascending=False)
    return combined.index[0], "no config passes on both symbols -> fallback: best combined full-180d net"


def select_mode_a_wf(wide: pd.DataFrame, symbols: list[str]) -> tuple[tuple, str]:
    """Same config on both symbols, chosen on W1+W2 only."""
    a, b = symbols
    both = unified_pivot(wide)
    ok = both[(both[("net_W1", a)] > 0) & (both[("net_W2", a)] > 0) & (both[("net_W1", b)] > 0)
              & (both[("net_W2", b)] > 0) & (both[("trades_W12", a)] >= MIN_TRADES_W12)
              & (both[("trades_W12", b)] >= MIN_TRADES_W12)]
    pool = ok if len(ok) else both
    score = (pool[("net_W12", a)] + pool[("net_W12", b)]).sort_values(ascending=False)
    note = (f"W1>0 and W2>0 on both symbols ({len(ok)} such configs), best combined W1+W2 net" if len(ok)
            else "no config is positive in W1 and W2 on both symbols -> fallback: best combined W1+W2 net")
    return score.index[0], note


def select_mode_b(wide: pd.DataFrame, symbols: list[str]) -> dict[str, tuple[pd.Series, str]]:
    """Per-symbol specialist chosen on W1+W2 only."""
    out = {}
    for sym in symbols:
        sub = wide[wide["symbol"] == sym]
        cand = sub[(sub["net_W1"] > 0) & (sub["net_W2"] > 0) & (sub["trades_W12"] >= MIN_TRADES_W12)]
        note = f"{len(cand)} configs with W1>0, W2>0, >= {MIN_TRADES_W12} trades in W1+W2"
        if cand.empty:
            cand, note = sub, "none positive in both W1 and W2 -> fallback best W1+W2"
        out[sym] = (cand.sort_values("net_W12", ascending=False).iloc[0], note)
    return out


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--warmup-days", type=int, default=45)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    args = ap.parse_args()
    if args.days != 3 * WINDOW_DAYS:
        ap.error(f"--days must be {3 * WINDOW_DAYS} (3 x {WINDOW_DAYS}-day windows)")
    total_days = args.days + args.warmup_days

    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[RichHandler(console=console, show_path=False, show_time=False)])
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    data = step_data(total_days, args.days)
    windows = make_windows(data[settings.SYMBOLS[0]], args.days)
    console.print(f"Indicator warm-up only: {data[settings.SYMBOLS[0]].index[0]} -> {windows['W1'][0]}")
    for w in [*WINDOWS, "full"]:
        a, b = windows[w]
        console.print(f"  {WINDOW_LABEL[w]:<18} {a} -> {b}  ({(b - a) / pd.Timedelta(days=1):.2f} days)")
    console.print()
    parity_ok = step_parity(data)
    look_ok = step_lookahead(data)

    # Grid ------------------------------------------------------------------- #
    console.rule("[bold]4. Walk-forward grid")
    tasks = [(s, st, tf, windows) for s in settings.SYMBOLS for st in strategies.STRATEGIES for tf in TIMEFRAMES]
    n_cfg = len(tasks) * len(STOPS) * len(EXITS)
    console.print(
        f"{len(settings.SYMBOLS)} symbols x {len(strategies.STRATEGIES)} strategies x {len(TIMEFRAMES)} timeframes x "
        f"{len(STOPS)} stop profiles x {len(EXITS)} exit modes = {n_cfg} configurations x 4 windows = {n_cfg * 4} backtests"
    )
    console.print(
        f"Fixed: fee_mode={FEE_MODE}, ₹10,000, 10x, risk 2%/trade, cooldown 1 bar, daily-loss stop 5%, ATR stop clamp 0.8-4%, "
        f"trail = {TRAIL_ATR}x ATR(14) of signal bar; filter = trades >= {MIN_TRADES_FULL}, >= 2/3 windows net > 0, full 180d net > 0"
    )
    t0 = time.perf_counter()
    rows: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_init_worker, initargs=(total_days,)) as pool:
        for chunk in pool.map(run_group, tasks):
            rows.extend(chunk)
    res = pd.DataFrame(rows)
    audit_bad = int((res["audit_errors"] > 0).sum())
    console.print(f"Ran {len(res):,} backtests in {time.perf_counter() - t0:.1f}s; audited {int(res['trades'].sum()):,} trades -> "
                  f"{'[green]0 runs with ledger errors' if not audit_bad else f'[red]{audit_bad} runs with ledger errors'}")
    out_dir = settings.data_path / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    res.to_csv(out_dir / "step3c_walkforward.csv", index=False)
    save_window_anchor(windows, data[settings.SYMBOLS[0]].index[0], out_dir / "step3c_windows.json")
    console.print(f"Saved all rows to {out_dir / 'step3c_walkforward.csv'} (+ window anchor step3c_windows.json)\n")

    wide = build_wide(res)
    n = len(wide)
    p = [(wide[f"net_{w}"] > 0).mean() for w in WINDOWS]
    p_ge2 = p[0] * p[1] * p[2] + p[0] * p[1] * (1 - p[2]) + p[0] * (1 - p[1]) * p[2] + (1 - p[0]) * p[1] * p[2]
    console.print(
        f"Share of configurations net-profitable: W1 {p[0]:.1%}, W2 {p[1]:.1%}, W3 {p[2]:.1%}, full {(wide['net'] > 0).mean():.1%}"
    )
    console.print(
        f"Profitable in >= 2 windows: {int((wide['pos_windows'] >= 2).sum())} (chance baseline if windows were independent coin flips "
        f"at those rates: {n * p_ge2:.0f}); all 3 windows: {int((wide['pos_windows'] == 3).sum())} (baseline {n * p[0] * p[1] * p[2]:.0f})"
    )
    passing = wide[wide["passes"]].sort_values(["pos_windows", "net"], ascending=[False, False])
    console.print(f"Pass the filter (>= {MIN_TRADES_FULL} trades, >= 2/3 windows, full > 0): [bold]{len(passing)}[/] of {n}\n")

    # Pass-count matrix
    m = Table(title="Configurations passing the filter, by strategy x timeframe (out of 48 = 2 symbols x 24 stop/exit combos)")
    m.add_column("strategy", no_wrap=True)
    for tf in TIMEFRAMES:
        m.add_column(tf, justify="right")
    m.add_column("all-3-window", justify="right")
    m.add_column("median full net ₹", justify="right")
    for strat in strategies.STRATEGIES:
        sub = wide[wide["strategy"] == strat]
        cells = []
        for tf in TIMEFRAMES:
            x = sub[sub["tf"] == tf]
            cells.append(f"{int(x['passes'].sum())} ({int((x['passes'] & (x['symbol'] == 'BTCUSD')).sum())}B/"
                         f"{int((x['passes'] & (x['symbol'] == 'ETHUSD')).sum())}E)")
        m.add_row(strat, *cells, str(int(((sub["pos_windows"] == 3) & sub["passes"]).sum())), money(float(sub["net"].median())))
    console.print(m)
    console.print()

    console.rule("[bold]5. Walk-forward table: configurations passing the filter (top 25, ranked by windows won, then full net)")
    if passing.empty:
        console.print("[yellow]No configuration passes the filter.\n")
    else:
        console.print(config_table(passing, "Passing configurations (in-sample: selected using all 3 windows)", 25))
        console.print()

    # Mode A: unified --------------------------------------------------------- #
    console.rule("[bold]6. Mode A - unified strategy portfolio (same config on BTCUSD and ETHUSD)")
    pick_a, how_a = select_mode_a(wide, settings.SYMBOLS)
    console.print(f"Selected {dict(zip(UKEY, pick_a))}: {how_a}  [dim](in-sample selection)[/]")
    console.print(config_table(wide.set_index(UKEY).loc[[pick_a]].reset_index()[wide.columns], "Mode A config per symbol (standalone ₹10,000)", 2))
    cfg_a = make_config(pick_a[2], pick_a[3])
    legs_a = [make_leg(s, pick_a[0], pick_a[1], df_1m=data[s], config=cfg_a) for s in settings.SYMBOLS]
    run_portfolio_mode("Mode A portfolio - shared ₹10,000 wallet", legs_a, windows, "")

    # Mode A-WF: unified, chosen on W1+W2 only
    console.rule("[bold]6b. Mode A-WF - unified config chosen on W1+W2 only, W3 out-of-sample")
    pick_awf, how_awf = select_mode_a_wf(wide, settings.SYMBOLS)
    console.print(f"Selected {dict(zip(UKEY, pick_awf))}: {how_awf}")
    cfg_awf = make_config(pick_awf[2], pick_awf[3])
    legs_awf = [make_leg(s, pick_awf[0], pick_awf[1], df_1m=data[s], config=cfg_awf) for s in settings.SYMBOLS]
    run_portfolio_mode("Mode A-WF portfolio - shared ₹10,000 wallet", legs_awf, windows,
                       "Chosen with W1+W2 only: W3 is out-of-sample; W1/W2/Full include the selection period.")

    # Mode B: specialists ------------------------------------------------------ #
    console.rule("[bold]7. Mode B - specialist per-symbol portfolio (each symbol chosen on W1+W2, W3 out-of-sample)")
    picks_b = {}
    sel = []
    for sym, (best, note) in select_mode_b(wide, settings.SYMBOLS).items():
        picks_b[sym] = best
        sel.append(best)
        console.print(f"{sym}: {best['strategy']} {best['tf']} {best['stops']} {best['exits']}  ({note})")
    console.print(config_table(pd.DataFrame(sel), "Mode B picks, standalone ₹10,000 each (W3 = out-of-sample)", 2, extra_w12=True))
    legs_b = [make_leg(sym, b["strategy"], b["tf"], df_1m=data[sym], config=make_config(b["stops"], b["exits"]))
              for sym, b in picks_b.items()]
    run_portfolio_mode("Mode B portfolio - shared ₹10,000 wallet", legs_b, windows,
                       "Each symbol's config chosen with W1+W2 only: W3 is out-of-sample; W1/W2/Full include the selection period.")

    ok = parity_ok and look_ok and audit_bad == 0
    console.rule(f"Step 3C checks (parity, lookahead, ledger audit): {'[green]PASS' if ok else '[red]FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
