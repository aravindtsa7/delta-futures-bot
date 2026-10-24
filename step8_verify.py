"""Step 8 verification: multi-coin support + manual trade terminal.

  1. GET /api/products/catalog against live Delta India (BTCUSD/ETHUSD/SOLUSD/XRPUSD, max leverage = 100/initial_margin)
  2. Add SOLUSD -> state + 25x preview immediately (no history backfill); auto-bot profile defaults / overrides
  3. Bot-stopped guard: /api/manual/order refused (409) while a real auto-bot loop holds the lock; works after /api/bot/stop
  4. Manual paper trading: SOLUSD LONG 20x sized by ₹5,000 margin with SL/TP, protection edit, BTCUSD SHORT 15x,
     live mark-to-market, add-to-position, resting limit (fill + cancel), SL/TP/liquidation/funding triggers,
     partial + full closes, full ledger audit
  5. Multi-coin auto-bot universe (managed symbols) + dashboard wiring
  6. Regression: step7_verify.py (runs step6 -> step5 -> step4)

Everything runs on isolated files under data/step8_test/ (journal, lock, control dir, coin list, bot overrides),
so a 24/7 bot running on data/bot_state.db is never touched.

Usage:  python step8_verify.py [--skip-regression]
"""

from __future__ import annotations

import testenv  # noqa: F401,E402  (must run before config is imported: research mode, no live keys)

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
import warnings
from pathlib import Path

from rich.console import Console
from rich.table import Table

warnings.filterwarnings("ignore", message=".*httpx.*starlette.testclient.*")
from fastapi.testclient import TestClient  # noqa: E402

import app as webapp  # noqa: E402
import bot_ops  # noqa: E402
import data_manager as dm  # noqa: E402
import execution_engine as ee  # noqa: E402
import live_bot as lb  # noqa: E402
import manual_trade as mtr  # noqa: E402
from config import PROJECT_ROOT, settings  # noqa: E402
from notifier import TelegramNotifier  # noqa: E402
from step5_verify import FakeClock, us  # noqa: E402
from step6_verify import FakeMarket  # noqa: E402

console = Console(width=200)
CHECKS: list[tuple[str, str, bool, str]] = []
T = settings.data_path / "step8_test"
RATE = settings.FIXED_USD_INR_RATE
FEE = settings.TAKER_FEE * (1 + settings.GST_ON_FEES)


def check(section: str, name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((section, name, bool(ok), detail))
    console.print(f"  {'[green]PASS[/]' if ok else '[red]FAIL[/]'} {name}" + (f"  [dim]{detail}[/]" if detail else ""))
    return bool(ok)


def mb(**extra) -> dict:
    """Step 9 mode binding for manual mutations: expected_mode + mode_generation (+ confirm='LIVE' in live)."""
    mode, gen = bot_ops.mode_state()
    return {"expected_mode": mode, "mode_generation": gen, **({"confirm": "LIVE"} if mode == "live" else {}), **extra}


def setup() -> TestClient:
    if T.exists():
        shutil.rmtree(T)
    T.mkdir(parents=True)
    bot_ops.configure(db=T / "bot_state.db", lock=T / "live_bot.lock", control=T / "bot_control", backups=T / "backups",
                      logs=T / "logs")
    dm.APP_SYMBOLS_FILE = T / "app_symbols.json"
    ee.BOT_PROFILES_FILE = T / "bot_profiles.json"
    return TestClient(webapp.app)


# --------------------------------------------------------------------------- #
def test_catalog(c: TestClient) -> None:
    console.rule("[bold]1. GET /api/products/catalog (live Delta India)")
    mtr._cache.pop("catalog", None)
    t0 = time.perf_counter()
    r = c.get("/api/products/catalog")
    t1 = time.perf_counter()
    r2 = c.get("/api/products/catalog")
    t2 = time.perf_counter()
    d = r.json()
    prods = {p["symbol"]: p for p in d["products"]}
    need = ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD"]
    check("1 catalog", "200 + live perpetual list includes BTCUSD, ETHUSD, SOLUSD, XRPUSD", r.status_code == 200
          and all(s in prods for s in need), f"{d['total_live_perpetuals']} live perpetual futures")
    t = Table(title="Catalog sample")
    for col in ("symbol", "product_id", "contract_value", "tick_size", "initial %", "maint %", "max leverage", "status"):
        t.add_column(col, justify="right")
    for s in need + ["DOGEUSD", "BNBUSD", "SUIUSD"]:
        p = prods.get(s)
        if p:
            t.add_row(s, str(p["product_id"]), f"{p['contract_value']:g}", f"{p['tick_size']:g}", f"{p['initial_margin_pct']:g}",
                      f"{p['maintenance_margin_pct']:g}", f"{p['max_leverage']:g}x", p["trading_status"])
    console.print(t)
    check("1 catalog", "fields: symbol, product_id, contract_value, tick_size, max_leverage = 100 / initial_margin",
          all(abs(prods[s]["max_leverage"] - 100 / prods[s]["initial_margin_pct"]) < 1e-9 and prods[s]["product_id"]
              and prods[s]["contract_value"] > 0 and prods[s]["tick_size"] > 0 for s in need)
          and prods["BTCUSD"]["product_id"] == 27 and prods["ETHUSD"]["product_id"] == 3136,
          f"BTC {prods['BTCUSD']['max_leverage']:g}x, SOL {prods['SOLUSD']['max_leverage']:g}x")
    check("1 catalog", "cached for 5 minutes (second call served from cache)", (t2 - t1) < (t1 - t0) and r2.json()["count"] == d["count"],
          f"first {1000 * (t1 - t0):.0f} ms, cached {1000 * (t2 - t1):.0f} ms")
    q = c.get("/api/products/catalog", params={"q": "sol"}).json()
    check("1 catalog", "search filter (q=sol)", any(p["symbol"] == "SOLUSD" for p in q["products"]), f"{q['count']} matches")


def test_add_coin(c: TestClient) -> None:
    console.rule("[bold]2. Add SOLUSD on the fly (no 410-day backfill) + auto-bot profile")
    parquet = dm.raw_1m_path("SOLUSD", "TRADE")
    had_hist = parquet.exists()
    t0 = time.perf_counter()
    r = c.post("/api/manual/symbols", json={"symbol": "SOLUSD", "backfill": False, "enable_auto_bot": True})
    a = r.json()
    check("2 coins", "POST /api/manual/symbols SOLUSD: metadata saved, added to the shared coin list",
          r.status_code == 200 and "SOLUSD" in a["app_symbols"] and dm.metadata_path("SOLUSD").exists() and "SOLUSD" in dm.app_symbols(),
          f"{a['app_symbols']} in {time.perf_counter() - t0:.1f}s")
    entry = json.loads(ee.BOT_PROFILES_FILE.read_text(encoding="utf-8")).get("SOLUSD") if ee.BOT_PROFILES_FILE.exists() else None
    t0 = time.perf_counter()
    s = c.get("/api/manual/state", params={"symbol": "SOLUSD", "resolution": "1m"}).json()
    pv = c.post("/api/manual/preview", json={"symbol": "SOLUSD", "side": "LONG", "leverage": 25, "margin_inr": 5000}).json()
    dt = time.perf_counter() - t0
    check("2 coins", "GET /api/manual/state?symbol=SOLUSD immediately: live ticker, L2, candles, product (no local history needed)",
          s["ticker"]["best_bid"] < s["ticker"]["best_ask"] and len(s["candles"]["bars"]) > 100 and s["product"]["max_leverage"] == 100
          and s["orderbook"]["bids"], f"bid {s['ticker']['best_bid']} ask {s['ticker']['best_ask']}, {len(s['candles']['bars'])} 1m bars, "
                                     f"local SOL parquet before test: {had_hist}")
    cv, e = pv["contract_value"], pv["est_entry_price"]
    n_exp = math.floor(5000 / RATE * 25 / (cv * e) + 1e-9)
    liq_exp = e * (1 - 1 / 25 + pv["maintenance_margin_pct"] / 100)
    check("2 coins", "POST /api/manual/preview SOLUSD 25x ₹5,000 margin: whole contracts, margin, fee+GST, liquidation",
          pv["contracts"] == n_exp and pv["required_margin_inr"] <= 5000 + 1e-6 and 5000 - pv["required_margin_inr"] < cv * e / 25 * RATE
          and abs(pv["est_entry_fee_inr"] - pv["notional_usd"] * FEE * RATE) < 1e-6 and abs(pv["est_liquidation_price"] - liq_exp) < 1e-9
          and pv["can_submit"], f"{pv['contracts']} contracts, notional ₹{pv['notional_inr']:,.2f}, margin ₹{pv['required_margin_inr']:,.2f}, "
                                f"fee ₹{pv['est_entry_fee_inr']:.2f}, liq {pv['est_liquidation_price']:.4f} ({dt:.1f}s for state+preview)")
    bad = c.post("/api/manual/preview", json={"symbol": "SOLUSD", "side": "LONG", "leverage": 150, "margin_inr": 5000})
    check("2 coins", "leverage above the product maximum (150x > 100x) refused", bad.status_code == 422, bad.json()["detail"])
    prof = ee.bot_profiles()
    check("2 coins", "auto-bot universe: BTCUSD D_both, ETHUSD C_costgate, SOLUSD added with enable_auto_bot=true -> C_costgate",
          prof["BTCUSD"].variant == "D_both" and prof["ETHUSD"].variant == "C_costgate" and prof["SOLUSD"].variant == "C_costgate"
          and prof["SOLUSD"].cost_gate and not prof["SOLUSD"].trend_filter_4h
          and entry == {"variant": "C_costgate", "enabled": True}, f"bot_profiles.json SOLUSD {entry}")
    ee.BOT_PROFILES_FILE.write_text(json.dumps({"SOLUSD": {"variant": "D_both"}}), encoding="utf-8")
    p2 = ee.bot_profiles()
    ee.BOT_PROFILES_FILE.write_text(json.dumps({"SOLUSD": {"enabled": False}}), encoding="utf-8")
    p3 = ee.bot_profiles()
    ee.BOT_PROFILES_FILE.unlink()
    check("2 coins", "data/bot_profiles.json overrides: variant change and disable", p2["SOLUSD"].variant == "D_both" and "SOLUSD" not in p3)


def test_guard(c: TestClient) -> None:
    console.rule("[bold]3. Bot-stopped guard: manual orders refused while the auto-bot loop holds the lock")
    order = {"symbol": "SOLUSD", "side": "LONG", "leverage": 20, "margin_inr": 5000, "sl_pct": 2.0, "tp_pct": 4.0}
    r = c.post("/api/bot/start", json={"mode": "paper"})
    st = r.json()
    t0 = time.time()
    while time.time() - t0 < 60 and not c.get("/api/manual/state", params={"symbol": "SOLUSD", "include_candles": False}).json()["bot"]["running"]:
        time.sleep(0.5)
    s = c.get("/api/manual/state", params={"symbol": "SOLUSD", "include_candles": False}).json()
    r1 = c.post("/api/manual/order", json={**order, **mb()})
    r1c = c.post("/api/manual/position/SOLUSD/close", json=mb())
    check("3 guard", "auto-bot running -> manual_enabled false; POST /api/manual/order 409 (and close 409)",
          r.status_code == 200 and s["bot"]["running"] and not s["manual_enabled"] and r1.status_code == 409 and r1c.status_code == 409,
          f"pid {st.get('pid')}: {r1.json()['detail'][:90]}")
    stop = c.post("/api/bot/stop").json()
    t0 = time.perf_counter()
    r2 = c.post("/api/manual/order", json={**order, **mb()})
    o = r2.json()
    check("3 guard", "after POST /api/bot/stop the same order succeeds immediately", stop["stopped"] and stop["exit_code"] == 0
          and r2.status_code == 200 and o["result"] == "opened", f"opened in {time.perf_counter() - t0:.1f}s")


def pos_of(s: dict, sym: str) -> dict | None:
    return next((p for p in s["positions"] if p["symbol"] == sym), None)


def test_trading(c: TestClient) -> None:
    console.rule("[bold]4. Manual paper trading: leverage, protection, mark-to-market, triggers, closes, ledger")
    s = c.get("/api/manual/state", params={"symbol": "SOLUSD", "include_candles": False}).json()
    sol = pos_of(s, "SOLUSD")
    cv = sol["contract_value"]
    check("4 trade", "SOLUSD LONG 20x sized by ₹5,000 margin: leverage, margin ≤ ₹5,000, SL/TP set, source manual",
          sol["leverage"] == 20 and sol["margin_inr"] <= 5000 + 1e-6 and 5000 - sol["margin_inr"] < cv * sol["entry_price"] / 20 * RATE
          and sol["sl_price"] < sol["entry_price"] < sol["tp_price"] and sol["source"] == "manual"
          and abs(sol["liq_price_est"] - sol["entry_price"] * (1 - 1 / 20 + 0.005)) < 1e-9,
          f"{sol['contracts']} @ {sol['entry_price']:.4f}, margin ₹{sol['margin_inr']:.2f}, SL {sol['sl_price']} TP {sol['tp_price']}, "
          f"liq {sol['liq_price_est']:.4f}")
    new_sl, new_tp = round(sol["entry_price"] * 0.985, 4), round(sol["entry_price"] * 1.03, 4)
    r = c.put("/api/manual/position/SOLUSD/protection", json={"sl_price": new_sl, "tp_price": new_tp, **mb()})
    bad = c.put("/api/manual/position/SOLUSD/protection", json={"sl_price": round(sol["entry_price"] * 1.05, 4), **mb()})
    pp = r.json().get("position", {})
    check("4 trade", "PUT /api/manual/position/SOLUSD/protection edits SL/TP; an SL above the mark is refused (422)",
          r.status_code == 200 and abs(pp["sl_price"] - new_sl) < 1e-9 and abs(pp["tp_price"] - new_tp) < 1e-9 and bad.status_code == 422,
          f"SL {sol['sl_price']} -> {pp.get('sl_price')}, TP {sol['tp_price']} -> {pp.get('tp_price')}")
    r = c.post("/api/manual/order", json={"symbol": "BTCUSD", "side": "SHORT", "leverage": 15, "margin_inr": 4000,
                                           "sl_pct": 1.5, "tp_pct": 3.0, **mb()})
    s = c.get("/api/manual/state", params={"symbol": "BTCUSD", "include_candles": False}).json()
    btc = pos_of(s, "BTCUSD")
    check("4 trade", "BTCUSD SHORT 15x placed (TP below / SL above entry)", r.status_code == 200 and btc and btc["side"] == "SHORT"
          and btc["leverage"] == 15 and btc["tp_price"] < btc["entry_price"] < btc["sl_price"],
          f"{btc['contracts']} @ {btc['entry_price']:.1f}, liq {btc['liq_price_est']:.1f}" if btc else r.text[:200])
    mtm_ok = True
    rows = []
    for p in s["positions"]:
        d = 1 if p["side"] == "LONG" else -1
        u = d * p["contracts"] * p["contract_value"] * (p["mark_price"] - p["entry_price"]) * RATE
        roe = u / p["margin_inr"] * 100
        mtm_ok &= abs(u - p["unrealized_pnl_inr"]) < 1e-6 and abs(roe - p["roe_pct"]) < 1e-6
        rows.append(f"{p['symbol']} mark {p['mark_price']:.4f} uPnL ₹{p['unrealized_pnl_inr']:.2f} ROE {p['roe_pct']:.2f}%")
    w = s["wallet"]
    check("4 trade", "live mark-to-market: unrealized ₹ and ROE % recomputed from the live mark; wallet totals consistent",
          mtm_ok and abs(w["unrealized_pnl_inr"] - sum(p["unrealized_pnl_inr"] for p in s["positions"])) < 1e-6
          and abs(w["used_margin_inr"] - sum(p["margin_inr"] for p in s["positions"])) < 1e-6
          and abs(w["available_margin_inr"] - (w["realized_equity_inr"] - w["used_margin_inr"])) < 1e-6, " · ".join(rows))

    # add to SOL (same leverage) and the refusal at a different leverage
    n0 = pos_of(s, "SOLUSD")["contracts"]
    r_add = c.post("/api/manual/order", json={"symbol": "SOLUSD", "side": "LONG", "leverage": 20, "contracts": 2, **mb()})
    r_bad = c.post("/api/manual/order", json={"symbol": "SOLUSD", "side": "LONG", "leverage": 10, "contracts": 1, **mb()})
    sol2 = pos_of(c.get("/api/manual/state", params={"symbol": "SOLUSD", "include_candles": False}).json(), "SOLUSD")
    check("4 trade", "add 2 contracts at 20x averages the entry; adding at 10x is refused", r_add.json().get("result") == "added"
          and sol2["contracts"] == n0 + 2 and r_bad.status_code == 422, f"{n0} -> {sol2['contracts']} @ {sol2['entry_price']:.4f}")

    # automatic trigger evaluation (watcher), with injected ticks
    w0 = mtr.watch_once({"SOLUSD": {"last": sol2["entry_price"], "mark": sol2["entry_price"]},
                         "BTCUSD": {"last": btc["entry_price"], "mark": btc["entry_price"]}})
    s_mid = c.get("/api/manual/state", params={"symbol": "SOLUSD", "include_candles": False}).json()
    check("4 trade", "watcher tick inside SL/TP leaves both positions open", w0["status"] == "ran" and len(s_mid["positions"]) == 2
          and not [a for a in w0["actions"] if a["kind"] == "exit"], json.dumps(w0["actions"])[:120])
    # ETH 100x long without SL -> liquidation on mark; XRP long with TP -> TP; a resting limit that fills; one cancelled
    r_eth = c.post("/api/manual/order", json={"symbol": "ETHUSD", "side": "LONG", "leverage": 100, "contracts": 2, **mb()})
    r_xrp = c.post("/api/manual/order", json={"symbol": "XRPUSD", "side": "LONG", "leverage": 10, "margin_inr": 1000, "tp_pct": 2.0, **mb()})
    s3 = c.get("/api/manual/state", params={"symbol": "XRPUSD", "include_candles": False}).json()
    eth, xrp = pos_of(s3, "ETHUSD"), pos_of(s3, "XRPUSD")
    dtk = c.get("/api/manual/state", params={"symbol": "DOGEUSD", "include_candles": False}).json()["ticker"]
    doge_lim = round(dtk["best_ask"] * 1.01, 6)
    r_lim = c.post("/api/manual/order", json={"symbol": "DOGEUSD", "side": "SHORT", "order_type": "limit",
                                               "limit_price": doge_lim, "leverage": 10, "contracts": 3, **mb()})
    r_lim2 = c.post("/api/manual/order", json={"symbol": "SOLUSD", "side": "LONG", "order_type": "limit",
                                                "limit_price": round(sol2["entry_price"] * 0.8, 4), "leverage": 20, "contracts": 1, **mb()})
    pend = c.get("/api/manual/state", params={"symbol": "SOLUSD", "include_candles": False}).json()["pending_orders"]
    r_cancel = c.delete(f"/api/manual/order/{r_lim2.json().get('intent_id')}", params=mb())
    check("4 trade", "resting limit orders journalled (ACKNOWLEDGED); cancel works",
          r_lim.json().get("result") == "resting" and r_lim2.json().get("result") == "resting" and len(pend) == 2
          and r_cancel.status_code == 200, f"{[(o['symbol'], o['side'], o['limit_price']) for o in pend]}")
    eth_liq = eth["liq_price_est"]
    w1 = mtr.watch_once({"ETHUSD": {"last": eth_liq * 1.0001, "mark": eth_liq * 0.9999},
                         "XRPUSD": {"last": xrp["tp_price"] * 1.001, "mark": xrp["tp_price"]},
                         "SOLUSD": {"last": sol2["entry_price"], "mark": sol2["entry_price"]},
                         "BTCUSD": {"last": btc["entry_price"], "mark": btc["entry_price"]},
                         "DOGEUSD": {"last": doge_lim * 1.001, "mark": doge_lim}})
    s4 = c.get("/api/manual/state", params={"symbol": "XRPUSD", "include_candles": False}).json()
    trades = {t["symbol"]: t for t in reversed(s4["trades"])}
    teth, txrp = trades.get("ETHUSD"), trades.get("XRPUSD")
    bk = eth["entry_price"] * (1 - 1 / 100)
    check("4 trade", "automatic triggers: ETH 100x liquidated on mark (exit at bankruptcy, ≈ whole margin lost); XRP hit TP",
          r_eth.status_code == 200 and r_xrp.status_code == 200 and teth and teth["exit_reason"] == "LIQUIDATION"
          and abs(teth["exit_price"] - bk) < 1e-9 and txrp and txrp["exit_reason"] == "TP"
          and abs(teth["gross_pnl_inr"] + eth["margin_inr"]) < 1e-6,
          f"ETH liq est {eth_liq:.2f}, exit {teth['exit_price'] if teth else '-'} gross ₹{teth['gross_pnl_inr'] if teth else 0:.2f} "
          f"(margin ₹{eth['margin_inr']:.2f}); XRP TP net ₹{txrp['net_pnl_inr'] if txrp else 0:.2f}")
    dshort = pos_of(s4, "DOGEUSD")
    check("4 trade", "DOGEUSD resting SHORT limit filled at its limit (maker fee) when the tick touched it",
          dshort is not None and dshort["side"] == "SHORT" and dshort["contracts"] == 3 and abs(dshort["entry_price"] - doge_lim) < 1e-12
          and abs(dshort["entry_fee_usd"] - 3 * dshort["contract_value"] * dshort["entry_price"]
                  * settings.MAKER_FEE * (1 + settings.GST_ON_FEES)) < 1e-12,
          f"limit {doge_lim}; watcher actions {[a['symbol'] + ':' + str(a['kind']) for a in w1['actions']]}")

    # closes
    sol_n = pos_of(s4, "SOLUSD")["contracts"]
    rp = c.post("/api/manual/position/SOLUSD/close", json=mb(contracts=3))
    rf = c.post("/api/manual/position/SOLUSD/close", json=mb())
    rb = c.post("/api/manual/position/BTCUSD/close", json=mb())
    rx = c.post("/api/manual/position/DOGEUSD/close", json=mb())
    s5 = c.get("/api/manual/state", params={"symbol": "BTCUSD", "include_candles": False}).json()
    check("4 trade", "POST .../close: SOL partial (3) then rest, BTC and DOGE full — MANUAL_CLOSE, nothing left open",
          rp.json().get("result") == "partial_exit" and rp.json().get("remaining") == sol_n - 3 and rf.json().get("result") == "exit"
          and rb.json().get("result") == "exit" and rx.json().get("result") == "exit" and not s5["positions"],
          f"SOL {sol_n} -> {sol_n - 3} -> 0")

    # ledger audit
    store = ee.StateStore(bot_ops.PATHS["db"])
    try:
        trs = store.trades("paper")
        eng = ee.ExecutionEngine(store, "paper", notifier=TelegramNotifier(None, None), profiles=ee.bot_profiles())
        final = eng.realized_equity_inr()
        bad_rows = []
        for t in trs:
            d = 1 if t["side"] == "LONG" else -1
            gross = d * t["contracts"] * t["contract_value"] * (t["exit_price"] - t["entry_price"])
            if abs(gross - t["gross_pnl_usd"]) > 1e-9 or abs(t["net_pnl_usd"] - (t["gross_pnl_usd"] - t["fees_gst_usd"] + t["funding_usd"])) > 1e-9 \
                    or abs(t["net_pnl_inr"] - t["net_pnl_usd"] * RATE) > 1e-9 or t["source"] != "manual":
                bad_rows.append(t["trade_id"])
        sum_net = sum(t["net_pnl_inr"] for t in trs)
        intents = store.q("SELECT state, count(*) n FROM order_intents GROUP BY state")
    finally:
        store.close()
    tab = Table(title="Manual trade ledger (trade_history)")
    for col in ("#", "symbol", "side", "lev", "lots", "entry", "exit", "reason", "gross ₹", "fees+GST ₹", "funding ₹", "net ₹", "equity after ₹"):
        tab.add_column(col, justify="right")
    for t in trs:
        tab.add_row(str(t["trade_id"]), t["symbol"], t["side"], f"{t['leverage']:g}x", str(t["contracts"]), f"{t['entry_price']:.4f}",
                    f"{t['exit_price']:.4f}", t["exit_reason"], f"{t['gross_pnl_inr']:.2f}", f"{t['fees_gst_inr']:.2f}",
                    f"{t['funding_inr']:.4f}", f"{t['net_pnl_inr']:.2f}", f"{t['equity_after_inr']:.2f}")
    console.print(tab)
    check("4 trade", "ledger audit: per-row gross/net/INR identities; ₹50,000 + Σ net == final equity == last equity_after",
          not bad_rows and abs(50_000 + sum_net - final) < 1e-6 and abs(trs[-1]["equity_after_inr"] - final) < 1e-6
          and abs(s5["wallet"]["realized_equity_inr"] - final) < 1e-6,
          f"{len(trs)} trades, Σ net ₹{sum_net:,.2f}, final ₹{final:,.2f}; intents {[(r['state'], r['n']) for r in intents]}")

    # funding + trailing on ticks (engine level, controlled clock)
    clock = FakeClock(us("2026-10-20 07:55:00"))
    st2 = ee.StateStore(T / "tick_engine.db", clock=clock)
    try:
        e2 = ee.ExecutionEngine(st2, "paper", clock=clock, notifier=TelegramNotifier(None, None))
        e2.manual_fill("SOLUSD", 1, 10, 100.0, 10, sl=95.0, tp=110.0, trail=2.0)
        p0 = st2.position("paper", "SOLUSD")
        clock.t = us("2026-10-20 07:59:30")
        a1 = e2.on_tick("SOLUSD", clock.t, 104.0, 104.0, 0.0001)
        clock.t = us("2026-10-20 08:00:05")
        e2.on_tick("SOLUSD", clock.t, 104.5, 104.5, 0.0001)
        fe = st2.q("SELECT * FROM funding_events")
        p1 = st2.position("paper", "SOLUSD")
        clock.t = us("2026-10-20 08:01:00")
        a3 = e2.on_tick("SOLUSD", clock.t, 101.9, 101.9, 0.0001)
        tr = st2.trades("paper")
        check("4 trade", "tick watcher semantics: trailing ratchets, 8h funding settles once when 08:00 UTC is crossed, trailed stop exits",
              a1 and a1["action"] == "trail" and abs(p1["sl_price"] - 102.5) < 1e-9 and len(fe) == 1
              and abs(fe[0]["cashflow_usd"] + 10 * p0["contract_value"] * 104.5 * 0.0001) < 1e-12
              and a3 and a3["action"] == "exit" and tr[0]["exit_reason"] == "TSL",
              f"SL 95 -> {p1['sl_price']}, funding ${fe[0]['cashflow_usd']:.5f}, exit {tr[0]['exit_price']:.4f}" if tr and fe else "")
    finally:
        st2.close()


def test_multicoin(c: TestClient) -> None:
    console.rule("[bold]5. Multi-coin auto-bot universe + dashboard wiring")
    prices = {"ETHUSD": 2400.0, "BTCUSD": 81_600.0, "SOLUSD": 105.0, "DOGEUSD": 0.2}
    clock = FakeClock(us("2026-10-20 05:00:30"))
    st = ee.StateStore(T / "multi.db", clock=clock)
    e = ee.ExecutionEngine(st, "paper", clock=clock, notifier=TelegramNotifier(None, None), profiles=ee.bot_profiles())
    e.manual_fill("DOGEUSD", 1, 50, 0.2, 5)
    st.close()
    bot = lb.LiveBotRunner("paper", profiles=ee.bot_profiles(), market=FakeMarket(clock.seconds, {}, prices),
                           store_path=T / "multi.db", lock_path=T / "multi.lock", control_dir=T / "multi_ctl", clock=clock.seconds,
                           notifier=TelegramNotifier(None, None))
    try:
        check("5 multi", "LiveBotRunner trades BTCUSD/ETHUSD/SOLUSD profiles and also manages the manual DOGEUSD position",
              set(bot.profiles) == {"BTCUSD", "ETHUSD", "SOLUSD"} and "DOGEUSD" in bot.symbols and "DOGEUSD" not in bot.profiles,
              f"profiles {sorted(bot.profiles)}, managed {bot.symbols}")
        sub = [c_["symbols"] for c_ in bot.ws.public_channels if c_["name"] == "candlestick_1m"][0]
        check("5 multi", "WebSocket subscribes candles for every managed symbol (explicit symbols)", all(s in sub for s in bot.symbols), str(sub))
    finally:
        bot.close()
    st_ = c.get("/api/bot/status").json()
    check("5 multi", "Bot Operations status lists signals for the added coin", "SOLUSD" in st_["signals"],
          str({k: (v.get("variant") or v.get("error", "")[:40]) for k, v in st_["signals"].items()}))
    html = c.get("/").text
    js = c.get("/static/app.js").text
    check("5 multi", "dashboard: 'Manual Trade' tab (#manual), ticket, chart, positions/trades tables, bot-stop banner",
          all(x in html for x in ('data-view="manual"', 'id="manualView"', 'id="mtBuy"', 'id="mtSell"', 'id="mtChart"', 'id="mtPositions"',
                                  'id="mtTrades"', 'id="mtStopBot"', 'id="mtLevRange"'))
          and all(x in js for x in ("mtEnter", "/api/manual/order", "/api/manual/preview", "/api/products/catalog", '"#manual"')))


def test_regression(skip: bool) -> None:
    console.rule("[bold]6. Regression: step7_verify.py (runs step6 -> step5 -> step4)")
    if skip:
        console.print("[yellow]skipped (--skip-regression)[/]")
        return
    t0 = time.time()
    p = subprocess.run([sys.executable, "step7_verify.py"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=PROJECT_ROOT, timeout=3600, env={**os.environ, "PYTHONIOENCODING": "utf-8", "COLUMNS": "200"})
    out = p.stdout
    console.print(out, markup=False, highlight=False)
    if p.stderr.strip():
        console.print(p.stderr[-3000:], markup=False, highlight=False)
    for step, n in (("Step 4", 77), ("Step 5", 92), ("Step 6", 47), ("Step 7", 42)):
        lines = [ln.strip("─ ") for ln in out.splitlines() if f"{step} verification:" in ln and "──" in ln]
        check("6 regression", f"{step} verification PASS - {n} checks", bool(lines) and f"PASS - {n} checks" in lines[-1],
              lines[-1] if lines else "verdict line not found")
    check("6 regression", "step7_verify.py exit code 0", p.returncode == 0, f"{time.time() - t0:.0f}s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-regression", action="store_true")
    args = ap.parse_args()
    console.print(f"MODE={settings.MODE!r}  LIVE_TRADING_ENABLED={settings.LIVE_TRADING_ENABLED}  "
                  f"real bot lock held: {lb.InstanceLock.is_held(lb.LOCK_PATH)} (left untouched; tests use data/step8_test/)")
    c = setup()
    try:
        test_catalog(c)
        test_add_coin(c)
        test_guard(c)
        test_trading(c)
        test_multicoin(c)
    finally:
        if bot_ops.runner()[0]:
            c.post("/api/bot/stop")
    test_regression(args.skip_regression)

    console.rule("[bold]Summary")
    t = Table()
    for col in ("section", "check", "result", "detail"):
        t.add_column(col)
    for sec, name, ok, detail in CHECKS:
        t.add_row(sec, name, "[green]PASS" if ok else "[red]FAIL", detail[:100])
    console.print(t)
    failed = [x for x in CHECKS if not x[2]]
    console.rule(f"Step 8 verification: {'[green]PASS' if not failed else f'[red]FAIL ({len(failed)} checks)'} - {len(CHECKS)} checks")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
