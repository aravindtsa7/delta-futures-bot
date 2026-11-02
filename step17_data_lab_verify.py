"""Step 17 Verification: Dedicated Data Lab Tab (/#data), Inventory, Backfill & Gap Repair (<5s)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient

import app as app_mod
import data_manager as dm


def main() -> None:
    t0 = time.perf_counter()
    client = TestClient(app_mod.app)

    # 1. Verify GET /api/data/inventory
    r_inv = client.get("/api/data/inventory")
    assert r_inv.status_code == 200, f"inventory failed: {r_inv.text}"
    inv = r_inv.json()
    assert inv["total_symbols"] >= 8, f"expected >= 8 symbols, got {inv['total_symbols']}"
    assert inv["total_candles_all_coins"] > 1_000_000, f"expected >1M candles, got {inv['total_candles_all_coins']}"
    assert inv["total_disk_size_mb"] > 1.0, f"expected >1 MB disk size, got {inv['total_disk_size_mb']}"
    assert inv["latest_sync_utc"] is not None

    by_sym = {x["symbol"]: x for x in inv["items"]}
    for sym in ("BTCUSD", "ETHUSD", "BNBUSD", "DOGEUSD", "XRPUSD", "SUIUSD", "SOLUSD", "ADAUSD"):
        assert sym in by_sym, f"{sym} missing from /api/data/inventory"
        row = by_sym[sym]
        assert row["status"] in ("ready", "downloading", "syncing")
        assert row["coverage_days"] >= 55.0, f"{sym} coverage_days={row['coverage_days']}"
        assert row["trade_rows"] >= 80_000, f"{sym} trade_rows={row['trade_rows']}"
        assert row["mark_rows"] >= 80_000
        assert row["funding_rows"] >= 80_000
        assert row["start_date"] and row["end_date"]
        assert row["file_size_mb"] > 0

    print(
        f"[1/4] GET /api/data/inventory OK: {inv['total_symbols']} coins, "
        f"{inv['total_candles_all_coins']:,} TRADE candles, {inv['total_disk_size_mb']:.2f} MB on disk"
    )

    # 2. Verify POST /api/data/backfill & GET /api/data/backfill/status
    r_bf = client.post(
        "/api/data/backfill",
        json={"symbol": "DOGEUSD", "mode": "older", "days_to_add": 0, "dry_run": True},
    )
    assert r_bf.status_code == 202, f"backfill start failed: {r_bf.text}"
    for _ in range(30):
        st = client.get("/api/data/backfill/status").json()
        if not st["is_running"]:
            break
        time.sleep(0.05)
    st = client.get("/api/data/backfill/status").json()
    assert st["state"] == "done" and st["progress_pct"] == 100.0, f"unexpected backfill status: {st}"
    assert st["error"] is None
    print(f"[2/4] POST /api/data/backfill & GET /api/data/backfill/status OK: {st['message']}")

    # 3. Verify POST /api/data/add_coin & POST /api/data/repair_gaps
    r_add = client.post("/api/data/add_coin", json={"symbol": "DOGEUSD", "initial_days": 0, "dry_run": True})
    assert r_add.status_code == 200 and r_add.json()["ok"] is True
    r_rep = client.post("/api/data/repair_gaps", json={"symbol": "DOGEUSD"})
    assert r_rep.status_code == 200 and r_rep.json()["ok"] is True
    assert r_rep.json()["gaps_after"] == 0
    print(f"[3/4] POST /api/data/add_coin & POST /api/data/repair_gaps OK (DOGEUSD gaps_after=0)")

    # 4. Verify UI HTML/JS/CSS & Existing Views (Research Lab, Bot Ops, Manual Trade, Data Lab)
    html = Path("static/index.html").read_text(encoding="utf-8")
    js = Path("static/app.js").read_text(encoding="utf-8")
    css = Path("static/styles.css").read_text(encoding="utf-8")

    for required_id in (
        'id="navData"',
        'data-view="data"',
        'id="dataView"',
        'id="dlAddCoinBtn"',
        'id="dlSyncAllBtn"',
        'id="dlExtendAltsBtn"',
        'id="dlInventoryTable"',
        'id="backfillModal"',
        'id="addCoinModal"',
        'id="labView"',
        'id="botView"',
        'id="manualView"',
    ):
        assert required_id in html, f"missing {required_id} in static/index.html"

    for fn_name in ("dlEnter", "loadDataInventory", "renderDataLab", "openBackfillModal", "triggerBackfill", "bindDataLab"):
        assert fn_name in js, f"missing {fn_name} in static/app.js"
    assert ".data-view" in css and ".dl-progress-fill" in css

    # Verify no regression on /api/status or /api/bot/status
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/bot/status").status_code == 200

    print(f"[4/4] UI & Route Verification OK (#navData -> /#data, modals, styles, and zero regressions)")
    print(f"Step 17 Data Lab verification passed in {time.perf_counter() - t0:.2f}s.")


if __name__ == "__main__":
    main()

