"""Step 2 verification: download/resume 1m data, coverage report, resample parity vs the API.

Usage:
    python step2_verify.py              # 7 days (fast)
    python step2_verify.py --days 90    # full backfill
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

import pandas as pd
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

import data_manager as dm
from config import settings

console = Console()

PARITY_SYMBOL = "BTCUSD"
PARITY_TIMEFRAMES = ["5m", "15m"]
PARITY_BARS = 10


def fmt_ts(ts: int | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fmt_size(n: int) -> str:
    return f"{n / 1024:,.1f} KiB" if n < 1024**2 else f"{n / 1024**2:,.2f} MiB"


def step_download(days: int) -> dict[tuple[str, str], dm.DownloadResult]:
    console.rule(f"[bold]1. Metadata + 1m download ({days} days)")
    meta = dm.fetch_and_save_metadata(settings.SYMBOLS)
    console.print(f"Saved metadata: {', '.join(f'{s} (id={p['id']})' for s, p in meta.items())}\n")
    results = {(r.symbol, r.series_type): r for r in dm.download_all(settings.SYMBOLS, days)}
    console.print()
    return results


def step_coverage(results: dict[tuple[str, str], dm.DownloadResult]) -> bool:
    console.rule("[bold]2. Coverage summary (raw 1m Parquet)")
    table = Table()
    for col, kw in [
        ("symbol", {}), ("series", {}), ("rows", {"justify": "right"}), ("start (UTC)", {}),
        ("end (UTC)", {}), ("gaps", {"justify": "right"}), ("missing 1m", {"justify": "right"}),
        ("file size", {"justify": "right"}), ("fetched / req", {"justify": "right"}),
    ]:
        table.add_column(col, **kw)

    ok = True
    for sym in settings.SYMBOLS:
        for st in dm.SERIES_TYPES:
            path = dm.raw_1m_path(sym, st)
            raw = dm.load_raw_1m(sym, st)
            g = dm.find_gaps(raw["time"])
            res = results.get((sym, st))
            ok &= g.rows > 0
            table.add_row(
                sym, st, f"{g.rows:,}", fmt_ts(g.start), fmt_ts(g.end),
                str(g.gap_count), str(g.missing_minutes),
                fmt_size(path.stat().st_size) if path.exists() else "-",
                f"{res.rows_fetched:,} / {res.requests}" if res else "-",
            )
            for a, b in g.gaps[:5]:
                console.print(f"[yellow]  {sym} {st} gap: {fmt_ts(a)} -> {fmt_ts(b)}")
    console.print(table)
    console.print()
    return ok


def step_resample(days: int) -> dict[str, pd.DataFrame]:
    console.rule(f"[bold]3. Unified dataset + resampling ({PARITY_SYMBOL})")
    df = dm.load_unified_1m_dataset(PARITY_SYMBOL, days=days)
    console.print(
        f"Unified 1m: {len(df):,} rows  {df.index[0]} -> {df.index[-1]}  "
        f"missing mark rows={df.attrs['missing_mark_rows']}  missing funding rows={df.attrs['missing_funding_rows']}"
    )
    console.print(f"Columns: {list(df.columns)}")
    last = df.iloc[-1]
    console.print(
        f"Last 1m row: close={last['close']}  mark_close={last['mark_close']:.4f}  "
        f"funding_rate_pct={last['funding_rate_pct']}  funding_rate={last['funding_rate']:.8f}\n"
    )

    table = Table()
    for col in ("timeframe", "bars", "first bar (UTC)", "last bar (UTC)", "dropped partial", "dropped empty"):
        table.add_column(col, justify="right" if col in ("bars", "dropped partial", "dropped empty") else "left")
    out: dict[str, pd.DataFrame] = {}
    for tf in settings.SUPPORTED_RESAMPLE_TIMEFRAMES:
        bars = dm.resample_candles(df, tf)
        out[tf] = bars
        table.add_row(
            tf, f"{len(bars):,}", str(bars.index[0]), str(bars.index[-1]),
            str(bars.attrs["dropped_partial_buckets"]), str(bars.attrs["dropped_empty_buckets"]),
        )
    console.print(table)
    console.print()
    return out


def step_parity(resampled: dict[str, pd.DataFrame]) -> bool:
    console.rule(f"[bold]4. OHLCV parity: local resample vs GET /v2/history/candles ({PARITY_SYMBOL})")
    all_ok = True
    for tf in PARITY_TIMEFRAMES:
        local = resampled[tf].tail(PARITY_BARS)
        start = int(local.index[0].timestamp())
        end = int(local.index[-1].timestamp())
        rows = dm.client().get(
            "/v2/history/candles",
            {"resolution": tf, "symbol": PARITY_SYMBOL, "start": start, "end": end},
        )
        api = dm._normalise_raw(pd.DataFrame.from_records(rows)).set_index("time")

        table = Table(title=f"{PARITY_SYMBOL} {tf} - last {PARITY_BARS} closed bars")
        table.add_column("bar (UTC)")
        for col in dm.OHLCV_COLUMNS:
            table.add_column(col, justify="right")
        table.add_column("API match")

        matched = 0
        for ts, bar in local.iterrows():
            t = int(ts.timestamp())
            if t not in api.index:
                verdict, ok = "[red]missing in API", False
            else:
                diffs = [
                    f"{c}: {bar[c]} vs {api.at[t, c]}"
                    for c in dm.OHLCV_COLUMNS
                    if bar[c] != api.at[t, c]
                ]
                ok = not diffs
                verdict = "[green]exact" if ok else "[red]" + "; ".join(diffs)
            matched += ok
            table.add_row(
                ts.strftime("%Y-%m-%d %H:%M"),
                *[f"{bar[c]:g}" for c in dm.OHLCV_COLUMNS],
                verdict,
            )
        console.print(table)
        status = "[green]PASS" if matched == len(local) else "[red]FAIL"
        console.print(f"{tf}: {matched}/{len(local)} bars match exactly -> {status}\n")
        all_ok &= matched == len(local)
    return all_ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=7, help="lookback window in days (default 7; use 90 for full backfill)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every chunk request")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, show_time=False)],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    console.print(
        f"[bold]Environment:[/] {settings.ENVIRONMENT} ({settings.env.rest_url})   "
        f"[bold]Data dir:[/] {settings.data_path}   [bold]Symbols:[/] {settings.SYMBOLS}\n"
    )
    results = step_download(args.days)
    cov_ok = step_coverage(results)
    resampled = step_resample(args.days)
    parity_ok = step_parity(resampled)

    passed = cov_ok and parity_ok
    console.rule(f"Step 2 verification: {'[green]PASS' if passed else '[red]FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
