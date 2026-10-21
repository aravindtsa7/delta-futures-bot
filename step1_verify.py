"""Step 1 verification: product metadata + candle ordering on Delta Exchange India."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from typing import Any

import requests
from rich.console import Console
from rich.table import Table

from config import settings

console = Console()
session = requests.Session()
session.headers.update(settings.http_headers())

BASE_URL = settings.env.rest_url

# FUNDING:<sym> candles come back empty unless `start` is at or before an 8h
# funding boundary (00/08/16 UTC); rows begin at that boundary. So we query from
# the boundary and trim to the requested window client-side.
FUNDING_INTERVAL_SECONDS = 8 * 3600

PRODUCT_FIELDS: list[tuple[str, str]] = [
    ("product_id", "id"),
    ("symbol", "symbol"),
    ("contract_type", "contract_type"),
    ("state", "state"),
    ("trading_status", "trading_status"),
    ("contract_value", "contract_value"),
    ("contract_unit_currency", "contract_unit_currency"),
    ("tick_size", "tick_size"),
    ("quoting_asset.symbol", "quoting_asset.symbol"),
    ("settling_asset.symbol", "settling_asset.symbol"),
    ("spot_index.symbol", "spot_index.symbol"),
    ("maker_commission_rate", "maker_commission_rate"),
    ("taker_commission_rate", "taker_commission_rate"),
]


def get(path: str, params: dict[str, Any] | None = None) -> Any:
    resp = session.get(f"{BASE_URL}{path}", params=params, timeout=settings.HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success", False):
        raise RuntimeError(f"API error on {path}: {body}")
    return body["result"]


def dig(obj: dict[str, Any], dotted: str) -> Any:
    for key in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def verify_products() -> bool:
    console.rule("[bold]1. Product metadata  GET /v2/products/{symbol}")
    products: dict[str, dict[str, Any]] = {}
    ok = True
    for sym in settings.SYMBOLS:
        try:
            products[sym] = get(f"/v2/products/{sym}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]FAILED {sym}: {exc}")
            ok = False

    table = Table(show_lines=False)
    table.add_column("field", style="cyan")
    for sym in products:
        table.add_column(sym, style="white")
    for label, path in PRODUCT_FIELDS:
        table.add_row(label, *[str(dig(p, path)) for p in products.values()])
    console.print(table)

    for sym, p in products.items():
        if p.get("contract_type") != "perpetual_futures":
            console.print(f"[yellow]WARN {sym}: contract_type={p.get('contract_type')!r} (expected perpetual_futures)")
        if p.get("state") != "live":
            console.print(f"[yellow]WARN {sym}: state={p.get('state')!r}")
    return ok


def verify_candles() -> bool:
    console.rule("[bold]2. Candles  GET /v2/history/candles  (resolution=1m, last 30 min)")
    end = int(time.time())
    start = end - 30 * 60
    console.print(f"Window: {fmt_ts(start)}  ->  {fmt_ts(end)}   (start={start}, end={end})\n")

    ok = True
    for series in ("BTCUSD", "MARK:BTCUSD", "FUNDING:BTCUSD"):
        q_start = start
        if series.startswith("FUNDING:"):
            q_start = start // FUNDING_INTERVAL_SECONDS * FUNDING_INTERVAL_SECONDS
        params = {"resolution": "1m", "symbol": series, "start": q_start, "end": end}
        try:
            rows = get("/v2/history/candles", params)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]FAILED {series}: {exc}\n")
            ok = False
            continue
        if q_start != start:
            raw_n = len(rows)
            rows = [r for r in rows if r["time"] >= start]
            console.print(
                f"[dim]{series}: queried from 8h funding boundary {fmt_ts(q_start)} "
                f"({raw_n} rows), trimmed to window -> {len(rows)} rows[/]"
            )

        n = len(rows)
        if n == 0:
            console.print(f"[yellow]{series}: 0 rows returned\n")
            ok = False
            continue

        times = [r["time"] for r in rows]
        if n == 1:
            order = "single row (order undetermined)"
        elif all(a > b for a, b in zip(times, times[1:])):
            order = "NEWEST-FIRST (descending)"
        elif all(a < b for a, b in zip(times, times[1:])):
            order = "OLDEST-FIRST (ascending)"
        else:
            order = "UNSORTED / mixed"

        steps = {a - b for a, b in zip(times, times[1:])} if n > 1 else set()
        console.print(f"[bold]{series}[/]  rows={n}  order=[magenta]{order}[/]  step_seconds={sorted(abs(s) for s in steps)}")

        table = Table(show_header=True)
        table.add_column("row")
        table.add_column("time")
        table.add_column("time (UTC)")
        for col in ("open", "high", "low", "close", "volume"):
            table.add_column(col, justify="right")
        for label, r in (("first", rows[0]), ("last", rows[-1])):
            table.add_row(
                label, str(r["time"]), fmt_ts(r["time"]),
                *[str(r.get(c)) for c in ("open", "high", "low", "close", "volume")],
            )
        console.print(table)
        console.print()
    return ok


def main() -> int:
    console.print(
        f"[bold]Environment:[/] {settings.ENVIRONMENT}  ({BASE_URL})   "
        f"[bold]Mode:[/] {settings.MODE}   [bold]Live trading:[/] {settings.is_live}   "
        f"[bold]Symbols:[/] {settings.SYMBOLS}\n"
    )
    p_ok = verify_products()
    console.print()
    c_ok = verify_candles()
    status = "[green]PASS" if (p_ok and c_ok) else "[red]FAIL"
    console.rule(f"Step 1 verification: {status}")
    return 0 if (p_ok and c_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
