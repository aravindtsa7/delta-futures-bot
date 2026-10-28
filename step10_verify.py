"""Fast, self-contained Step 10 verification (< 5 seconds)."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module

ROOT = Path(__file__).resolve().parent
STRAT_FILE = ROOT / "data" / "strategies.json"
SAVED_FILE = ROOT / "data" / "saved_backtests.json"


def main() -> None:
    t0 = time.perf_counter()
    orig_strats = STRAT_FILE.read_text(encoding="utf-8") if STRAT_FILE.exists() else None
    orig_saved = SAVED_FILE.read_text(encoding="utf-8") if SAVED_FILE.exists() else None

    try:
        client = TestClient(app_module.app)

        # 1. Create, tune, and list custom strategies + verify coin filtering
        r_eth = client.post("/api/strategies", json={
            "name": "ETH Custom EMA",
            "symbol": "ETHUSD",
            "timeframe": "1h",
            "rule_type": "ema_cross",
            "params": {"fast_ema": 8, "slow_ema": 21, "trend_ema": 50, "rsi_period": 14, "rsi_ob": 70, "rsi_os": 30},
            "capital_inr": 25000,
            "leverage": 20,
            "risk_pct": 2.0,
            "sl_type": "fixed_pct",
            "sl_value": 1.5,
            "tp_type": "fixed_pct",
            "tp_value": 3.0,
        })
        assert r_eth.status_code == 200, r_eth.text
        eth_strat = r_eth.json()
        sid_eth = eth_strat["id"]

        r_btc = client.post("/api/strategies", json={
            "name": "BTC Only Supertrend",
            "symbol": "BTCUSD",
            "timeframe": "1h",
            "rule_type": "supertrend",
            "params": {"atr_period": 10, "atr_mult": 3.0, "ema_filter": 200},
        })
        assert r_btc.status_code == 200, r_btc.text
        sid_btc = r_btc.json()["id"]

        # Tune ETH strategy via PUT
        r_tune = client.put(f"/api/strategies/{sid_eth}", json={
            "name": "ETH Custom EMA Tuned",
            "params": {"fast_ema": 10, "slow_ema": 25},
        })
        assert r_tune.status_code == 200, r_tune.text
        assert r_tune.json()["name"] == "ETH Custom EMA Tuned"
        assert r_tune.json()["params"]["fast_ema"] == 10

        # List strategies for ETHUSD -> should include ETHUSD and ALL, but hide BTCUSD-only
        r_list = client.get("/api/strategies?symbol=ETHUSD")
        assert r_list.status_code == 200, r_list.text
        ids_for_eth = {s["id"] for s in r_list.json()["strategies"]}
        assert sid_eth in ids_for_eth, "ETHUSD strategy should be visible for ETHUSD"
        assert sid_btc not in ids_for_eth, "BTCUSD-only strategy must be hidden when ETHUSD is selected"

        # 2. Run backtest in-memory with custom capital ₹25,000, leverage 20x, Fixed % SL 1.5% / TP 3.0%
        saved_before = json.loads(SAVED_FILE.read_text(encoding="utf-8"))
        r_run = client.post("/api/backtest/run", json={
            "symbol": "ETHUSD",
            "strategy_id": sid_eth,
            "timeframe": "1h",
            "days": 30,
            "capital_inr": 25000.0,
            "leverage": 20,
            "risk_pct": 2.0,
            "sl_type": "fixed_pct",
            "sl_value": 1.5,
            "tp_type": "fixed_pct",
            "tp_value": 3.0,
            "walk_forward_windows": 0,
            "include_chart": True,
        })
        assert r_run.status_code == 200, r_run.text
        bt_data = r_run.json()
        assert bt_data["config"]["capital_inr"] == 25000.0
        assert bt_data["config"]["leverage"] == 20.0
        assert bt_data["config"]["sl_type"] == "fixed_pct"
        assert bt_data["config"]["sl_value"] == 1.5
        assert bt_data["config"]["tp_type"] == "fixed_pct"
        assert bt_data["config"]["tp_value"] == 3.0
        assert "ETHUSD" in bt_data["charts"]

        saved_after_run = json.loads(SAVED_FILE.read_text(encoding="utf-8"))
        assert saved_after_run == saved_before, "data/saved_backtests.json must NOT be modified by /api/backtest/run"

        # 3. Explicitly save result and query with filters
        r_save = client.post("/api/backtest/save_result", json={
            "strategy_id": sid_eth,
            "strategy_name": "ETH Custom EMA Tuned",
            "symbol": "ETHUSD",
            "timeframe": "1h",
            "period_days": 30,
            "capital_inr": 25000.0,
            "leverage": 20,
            "sl_type": "fixed_pct",
            "sl_value": 1.5,
            "tp_type": "fixed_pct",
            "tp_value": 3.0,
            "summary": bt_data["summary"],
            "trades": bt_data["trades"],
        })
        assert r_save.status_code == 200, r_save.text
        saved_id = r_save.json()["id"]

        r_res = client.get("/api/backtest/results", params={
            "symbol": "ETHUSD",
            "strategy_name": "Tuned",
            "min_win_rate": 0,
            "min_pf": 0,
        })
        assert r_res.status_code == 200, r_res.text
        results = r_res.json()["results"]
        assert any(x["id"] == saved_id for x in results), "Saved result must appear in filtered results"

        r_res_btc = client.get("/api/backtest/results", params={"symbol": "BTCUSD"})
        assert all(x["id"] != saved_id for x in r_res_btc.json()["results"])

        # 4. Verify static/index.html & static/app.js syntax + /#bot and /#manual endpoints
        html_text = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        for marker in ("Research overview", "Backtest setup", "Save result", "Backtest Results", "botView", "manualView"):
            assert marker in html_text, f"Missing {marker!r} in static/index.html"

        node_bin = shutil.which("node")
        if node_bin:
            chk = subprocess.run([node_bin, "--check", str(ROOT / "static" / "app.js")], capture_output=True, text=True)
            assert chk.returncode == 0, f"node --check static/app.js failed: {chk.stderr}"

        r_bot = client.get("/api/bot/status")
        assert r_bot.status_code == 200, f"/api/bot/status returned {r_bot.status_code}"
        r_man = client.get("/api/manual/state?symbol=BTCUSD&resolution=5m")
        assert r_man.status_code == 200, f"/api/manual/state returned {r_man.status_code}"

        elapsed = time.perf_counter() - t0
        print(f"Step 10 verification PASSED in {elapsed:.2f}s")
    finally:
        if orig_strats is not None:
            STRAT_FILE.write_text(orig_strats, encoding="utf-8")
        if orig_saved is not None:
            SAVED_FILE.write_text(orig_saved, encoding="utf-8")


if __name__ == "__main__":
    main()

