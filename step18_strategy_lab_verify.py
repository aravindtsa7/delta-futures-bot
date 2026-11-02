"""Step 18 Verification: Strategy Lab, Custom-Named Saves & Paper Bot 1-Click Deployment."""
from __future__ import annotations

import json
import time
from pathlib import Path
from fastapi.testclient import TestClient

import app as web_app
import execution_engine as ee

ROOT = Path(__file__).resolve().parent
BOT_PROFILES_PATH = ROOT / "data" / "bot_profiles.json"
SAVED_BACKTESTS_PATH = ROOT / "data" / "saved_backtests.json"


def main() -> None:
    t0 = time.perf_counter()
    client = TestClient(web_app.app)

    # 1. Verify clean Paper Bot reset state
    assert json.loads(BOT_PROFILES_PATH.read_text(encoding="utf-8")) == {}, "bot_profiles.json must start empty {}"
    dep0 = client.get("/api/bot/deployed").json()
    assert dep0["ok"] is True and dep0["deployed"] == {}, f"Expected empty deployed map, got {dep0}"
    print("[PASS] 1. Fresh Paper Bot state verified: data/bot_profiles.json is empty {}")

    # 2. Run a quick backtest & save with custom name and full execution blueprint
    bt_req = {
        "symbol": "ETHUSD",
        "strategy_id": "obv_Breakout",
        "timeframe": "15m",
        "days": 30,
        "capital_inr": 50000.0,
        "risk_pct": 2.0,
        "leverage": 10,
        "fee_mode": "taker_maker_tp",
        "slippage_bps": 3.0,
        "use_4h_trend": False,
        "use_cost_gate": True,
        "strict_lot_cap": True,
        "custom_rule_type": "obv_breakout",
        "custom_params": {
            "obv_span": 20, "adx_min": 20, "vol_mult": 1.1,
            "sl_type": "atr_mult", "sl_value": 1.6,
            "tp_type": "atr_mult", "tp_value": 2.4,
            "trailing_stop": True, "trail_mult": 0.9,
        },
    }
    bt_res = client.post("/api/backtest/run", json=bt_req)
    assert bt_res.status_code == 200, bt_res.text
    m = bt_res.json()["metrics"]

    save_payload = {
        "custom_name": "My ETH 15m OBV Winner",
        "notes": "High-WR 15m OBV breakout with 1.6x SL / 2.4x TP",
        "symbol": "ETHUSD",
        "strategy_id": "obv_breakout",
        "strategy_name": "OBV Trend Breakout",
        "rule_type": "obv_breakout",
        "params": bt_req["custom_params"],
        "timeframe": "15m",
        "days": 30,
        "capital_inr": 50000.0,
        "leverage": 10,
        "risk_pct": 2.0,
        "sl_type": "atr_mult",
        "sl_value": 1.6,
        "tp_type": "atr_mult",
        "tp_value": 2.4,
        "trailing_stop": True,
        "trail_mult": 0.9,
        "fee_mode": "taker_maker_tp",
        "slippage_bps": 3.0,
        "use_4h_trend": False,
        "use_cost_gate": True,
        "strict_lot_cap": True,
        "net_pnl_inr": m["net_pnl_inr"],
        "return_pct": m["return_pct"],
        "win_rate": m["win_rate"],
        "profit_factor": m["profit_factor"],
        "max_dd_pct": m["max_dd_pct"],
        "trades": m["trades"],
    }
    sr = client.post("/api/backtest/save_result", json=save_payload)
    assert sr.status_code == 200, sr.text
    saved_entry = sr.json()["entry"]
    rid = saved_entry["id"]
    assert saved_entry["custom_name"] == "My ETH 15m OBV Winner"
    assert saved_entry["leverage"] == 10 and saved_entry["risk_pct"] == 2.0
    assert saved_entry["sl_value"] == 1.6 and saved_entry["tp_value"] == 2.4
    print(f"[PASS] 2. Custom-named backtest saved: '{saved_entry['custom_name']}' (id={rid})")

    # 3. Verify library query + rename endpoint
    lib = client.get("/api/backtest/results?search=OBV%20Winner&timeframe=15m").json()
    assert any(x["id"] == rid for x in lib["results"]), "Saved strategy not found via search filter"
    ren = client.put(f"/api/backtest/results/{rid}", json={"custom_name": "My ETH 15m OBV Winner v2"})
    assert ren.status_code == 200 and ren.json()["entry"]["custom_name"] == "My ETH 15m OBV Winner v2"
    print("[PASS] 3. Strategy Lab library search & rename verified")

    # 4. Deploy saved strategy to Paper Auto-Bot
    dep_res = client.post("/api/bot/deploy", json={"result_id": rid, "enabled": True})
    assert dep_res.status_code == 200, dep_res.text
    dep_data = client.get("/api/bot/deployed").json()
    assert "ETHUSD" in dep_data["deployed"], "ETHUSD should be deployed"
    eth_dep = dep_data["deployed"]["ETHUSD"]
    assert eth_dep["custom_name"] == "My ETH 15m OBV Winner v2"
    assert eth_dep["timeframe"] == "15m"
    assert eth_dep["leverage"] == 10 and eth_dep["risk_pct"] == 2.0
    assert eth_dep["sl_value"] == 1.6 and eth_dep["tp_value"] == 2.4

    # Verify execution_engine evaluates the deployed 15m custom strategy accurately
    profiles = ee.bot_profiles()
    assert "ETHUSD" in profiles
    ev = ee.evaluate_symbol("ETHUSD", profiles["ETHUSD"])
    assert ev.timeframe == "15m" and ev.custom_name == "My ETH 15m OBV Winner v2"
    print(f"[PASS] 4. Deployed to Paper Bot & evaluated 15m signal: bar={ev.bar_open.isoformat()} close={ev.close:.2f}")

    # 5. Toggle pause/enable and undeploy to restore clean state
    tog = client.put("/api/bot/deployed/ETHUSD/toggle", json={"enabled": False})
    assert tog.status_code == 200 and tog.json()["entry"]["enabled"] is False
    assert "ETHUSD" not in ee.bot_profiles(), "Paused strategy must be excluded from active bot_profiles()"

    undep = client.delete("/api/bot/deployed/ETHUSD")
    assert undep.status_code == 200
    del_res = client.delete(f"/api/backtest/results/{rid}")
    assert del_res.status_code == 200

    # Ensure files on disk are completely clean for user
    BOT_PROFILES_PATH.write_text("{}\n", encoding="utf-8")
    SAVED_BACKTESTS_PATH.write_text("[]\n", encoding="utf-8")

    # Verify UI HTML contains Strategy Lab tab and modal
    idx_html = client.get("/").text
    assert 'id="navStrategies"' in idx_html and 'id="strategiesView"' in idx_html and 'id="saveResultModal"' in idx_html
    elapsed = time.perf_counter() - t0
    print(f"[PASS] 5. Toggle, undeploy & clean reset verified in {elapsed:.2f}s")


if __name__ == "__main__":
    main()

