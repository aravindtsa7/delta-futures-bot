"""Step 8B verification: per-mode journals, exchange-backed manual trading (live/testnet), live API read-only probe.

  1. LIVE .env read-only probe against https://api.india.delta.exchange: GET /v2/wallet/balances and
     GET /v2/positions/margined only. A read-only client is used, which refuses every POST/PUT/DELETE locally
     (proved below on the same client: nothing is sent). NO order is placed, edited or cancelled.
  2. Deterministic mocked exchange suite (FakeDeltaExchange, HMAC-checked): per-mode SQLite isolation, live gates,
     ₹0 wallet message, isolated margin + leverage sync, market/limit entries with SL/TP brackets, limit cancel,
     SL/TP edits (PUT /v2/orders) and creation, scale-in with leg resize, partial + full reduce-only closes,
     exchange-side TP fill, external (non-journalled) position close, wallet/position sync, testnet path, ledger audit.
  3. Regression: step8_verify.py (runs step7 -> step6 -> step5 -> step4).

All mocked tests use data/step8b_test/; the real journals, lock and coin list are never touched.
Usage:  python step8b_verify.py [--skip-regression]
"""

from __future__ import annotations

import testenv  # noqa: F401  (research mode, no live keys in settings — before config is imported)

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import warnings
from pathlib import Path

import httpx
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
from config import PROJECT_ROOT, get_mode_db_path, mask_secret, settings  # noqa: E402
from exchange_client import DeltaRestClientAdapter, RateLimitBudget, ReadOnlyClientError  # noqa: E402
from step5_verify import FAKE_KEY, FAKE_SECRET, FakeDelta  # noqa: E402

console = Console(width=200)
CHECKS: list[tuple[str, str, bool, str]] = []
T = settings.data_path / "step8b_test"
RATE = settings.FIXED_USD_INR_RATE


def check(section: str, name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((section, name, bool(ok), detail))
    console.print(f"  {'[green]PASS[/]' if ok else '[red]FAIL[/]'} {name}" + (f"  [dim]{detail}[/]" if detail else ""))
    return bool(ok)


def read_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# --------------------------------------------------------------------------- #
# 1. Real live read-only probe
# --------------------------------------------------------------------------- #
def test_live_probe() -> None:
    console.rule("[bold]1. LIVE .env read-only probe (https://api.india.delta.exchange) — zero orders")
    env = read_dotenv(PROJECT_ROOT / ".env")
    key = env.get("DELTA_API_KEY") or env.get("DELTA_LIVE_API_KEY") or ""
    secret = env.get("DELTA_API_SECRET") or env.get("DELTA_LIVE_API_SECRET") or ""
    gates = {k: env.get(k) or env.get(f"DELTA_{k}") for k in ("MODE", "LIVE_TRADING_ENABLED", "EXECUTION_MODE", "AUTO_BOT_LIVE_ENABLED")}
    console.print(f"  .env live key {mask_secret(key) or '<missing>'} · secret {'present' if secret else '<missing>'} · gates {gates}")
    if not (key and secret):
        console.print("  [yellow]SKIPPED: no DELTA_API_KEY / DELTA_API_SECRET in .env[/]")
        return
    c = DeltaRestClientAdapter("live", key, secret, read_only=True)
    check("1 live", "read-only client targets the production India host", c.base_url == "https://api.india.delta.exchange" and c.read_only)
    try:
        bals = c.get_wallet_balances()
        w = mtr.wallet_from_balances(bals)
        ok_w = True
    except Exception as exc:  # noqa: BLE001
        w, ok_w = {"error": mtr._diagnose(exc)}, False
    check("1 live", "GET /v2/wallet/balances authenticated (API key, HMAC signature, IP whitelist all accepted)", ok_w,
          f"equity ₹{w.get('wallet_equity_inr', 0):,.2f} (${w.get('wallet_equity_usd', 0):,.2f}), available ₹{w.get('available_margin_inr', 0):,.2f}, "
          f"INR via {w.get('inr_source')}" if ok_w else w["error"])
    try:
        poss = [p for p in c.get_margined_positions() if int(p.get("size") or 0) != 0]
        ok_p = True
    except Exception as exc:  # noqa: BLE001
        poss, ok_p = [], False
        console.print(f"  positions error: {mtr._diagnose(exc)}")
    check("1 live", "GET /v2/positions/margined authenticated", ok_p, f"{len(poss)} open position(s) {[(p.get('product_symbol'), p.get('size')) for p in poss]}")
    if ok_w:
        t = Table(title="Live Delta India wallet (read-only)")
        for col in ("asset", "balance", "available", "balance ₹ (Delta)"):
            t.add_column(col, justify="right")
        for a in w["assets"]:
            t.add_row(str(a["asset_symbol"]), str(a["balance"]), str(a["available_balance"]), str(a.get("balance_inr")))
        console.print(t)
        if w["wallet_equity_inr"] <= 0:
            console.print("  [yellow]Live wallet is ₹0.00: live manual orders will be refused with the 'please add funds' message.[/]")
    n_before = len(c.request_log)
    blocked = []
    for name, fn in (("POST /v2/orders", lambda: c.place_order(product_id=27, side="buy", size=1, client_order_id="probe-never-sent")),
                     ("DELETE /v2/orders", lambda: c.cancel_order(27, order_id=1)),
                     ("PUT /v2/users/margin_mode", lambda: c.set_margin_mode("isolated"))):
        try:
            fn()
        except ReadOnlyClientError:
            blocked.append(name)
    methods = sorted({r["method"] for r in c.request_log})
    check("1 live", "read-only guard: order / cancel / margin-mode calls refused locally, nothing sent; only GETs reached Delta",
          len(blocked) == 3 and len(c.request_log) == n_before and methods == ["GET"],
          f"refused {blocked}; requests sent: {[(r['method'], r['path'], r['status']) for r in c.request_log]}")


# --------------------------------------------------------------------------- #
# 2. Mocked exchange
# --------------------------------------------------------------------------- #
class RealClock:
    def __call__(self) -> int:
        return int(time.time() * 1_000_000)

    def seconds(self) -> float:
        return time.time()


class FakeDeltaExchange(FakeDelta):
    """Step 5 fake + more products, resting limit orders with brackets, margined positions with liq/margin."""

    def __init__(self, symbols: list[str]):
        super().__init__(RealClock())
        for sym in symbols:
            m = dm.load_metadata(sym)
            self.meta[sym] = m
            self.by_id[int(m["id"])] = m
        self.prices = {}
        for sym in symbols:
            tk = mtr.ticker(sym, ttl=0)
            self.prices[int(self.meta[sym]["id"])] = tk["best_ask"]
        self.margin_mode_calls = 0

    def fill_resting(self, order_id: int, price: float | None = None) -> None:
        o = self.orders[order_id]
        px = price if price is not None else float(o["limit_price"])
        self._fill(o, px)
        if o.get("bracket_stop_loss_price") or o.get("bracket_take_profit_price"):
            self._legs(o["product_id"], o["side"], o["size"], o.get("bracket_stop_loss_price"), o.get("bracket_take_profit_price"))

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/users/margin_mode":
            self.margin_mode_calls += 1
        if request.url.path == "/v2/positions/margined":
            r = super().handler(request)
            body = json.loads(r.content)
            for x in body["result"]:
                pid = int(x["product_id"])
                cv = float(self.by_id[pid]["contract_value"])
                lev = float(self.leverage.get(pid, "10"))
                e, sz = float(x["entry_price"]), int(x["size"])
                x.update(margin=str(abs(sz) * cv * e / lev), liquidation_price=str(ee.liquidation_price(e, "LONG" if sz > 0 else "SHORT", lev, 0.5)),
                         mark_price=str(self.prices.get(pid, e)), unrealized_pnl="0")
            return httpx.Response(200, json=body)
        return super().handler(request)


def factory_for(fakes: dict[str, FakeDeltaExchange]):
    def make(mode: str, read_only: bool) -> DeltaRestClientAdapter:
        f = fakes[mode]
        return DeltaRestClientAdapter(mode, FAKE_KEY, FAKE_SECRET, transport=httpx.MockTransport(f.handler), read_only=read_only,
                                      budget=RateLimitBudget(), sleep=lambda s: None)
    return make


def go_live_settings(on: bool) -> None:
    if on:
        settings.MODE = "live"
        settings.LIVE_TRADING_ENABLED = True
    else:
        settings.LIVE_TRADING_ENABLED = False
        settings.MODE = "research"


def calls(f: FakeDeltaExchange, method: str, path: str) -> list[dict]:
    return [c for c in f.calls if c["method"] == method and c["path"] == path]


def mb(**extra) -> dict:
    """Step 9 mode binding for manual mutations: expected_mode + mode_generation (+ confirm='LIVE' in live)."""
    mode, gen = bot_ops.mode_state()
    return {"expected_mode": mode, "mode_generation": gen, **({"confirm": "LIVE"} if mode == "live" else {}), **extra}


def test_mocked() -> None:
    console.rule("[bold]2. Mocked FakeDeltaExchange: exchange-backed manual trading (live + testnet)")
    if T.exists():
        shutil.rmtree(T)
    T.mkdir(parents=True)
    bot_ops.configure(db=T / "bot_state.db", lock=T / "live_bot.lock", control=T / "bot_control", backups=T / "backups", logs=T / "logs")
    dm.APP_SYMBOLS_FILE = T / "app_symbols.json"
    ee.BOT_PROFILES_FILE = T / "bot_profiles.json"
    syms = ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD"]
    live, tn = FakeDeltaExchange(syms), FakeDeltaExchange(syms)
    live.wallet_usd = 0.0
    mtr.CLIENT_FACTORY = factory_for({"live": live, "testnet": tn})
    c = TestClient(webapp.app)
    saved = (settings.LIVE_API_KEY, settings.LIVE_API_SECRET, settings.TESTNET_API_KEY, settings.TESTNET_API_SECRET)
    try:
        _mocked_body(c, live, tn)
    finally:
        mtr.CLIENT_FACTORY = None
        go_live_settings(False)
        settings.LIVE_API_KEY, settings.LIVE_API_SECRET, settings.TESTNET_API_KEY, settings.TESTNET_API_SECRET = saved
        mtr._margin_ok.clear()


def _mocked_body(c: TestClient, live: FakeDeltaExchange, tn: FakeDeltaExchange) -> None:
    # -- per-mode journals
    names = [get_mode_db_path(m).name for m in ("paper", "testnet", "live")]
    check("2 isolation", "runtime journals: paper bot_state.db, testnet bot_state_testnet.db, live bot_state_live.db",
          names == ["bot_state.db", "bot_state_testnet.db", "bot_state_live.db"], str(names))

    # -- gates
    r = c.post("/api/manual/mode", json={"mode": "live", "confirm": "LIVE"})
    check("2 gates", "switch to LIVE refused (403) while LIVE_TRADING_ENABLED=False", r.status_code == 403, r.json()["detail"][:110])
    go_live_settings(True)
    r = c.post("/api/manual/mode", json={"mode": "live", "confirm": "LIVE"})
    check("2 gates", "switch to LIVE refused (400) without live API key/secret", r.status_code == 400, r.json()["detail"][:110])
    settings.LIVE_API_KEY, settings.LIVE_API_SECRET = FAKE_KEY, FAKE_SECRET
    r = c.post("/api/manual/mode", json={"mode": "live"})
    check("2 gates", "switch to LIVE refused (422) without confirm='LIVE'", r.status_code == 422, r.json()["detail"][:110])
    holder = lb.InstanceLock(T / "live_bot.lock", "paper")
    r409 = c.post("/api/manual/mode", json={"mode": "live", "confirm": "LIVE"})
    holder.release()
    check("2 gates", "mode switch refused (409) while an auto-bot holds the lock", r409.status_code == 409)
    r = c.post("/api/manual/mode", json={"mode": "live", "confirm": "LIVE"})
    check("2 gates", "switch to LIVE accepted with confirm='LIVE' -> journal bot_state_live.db", r.status_code == 200
          and r.json()["mode"] == "live" and r.json()["journal"].endswith("bot_state_live.db"), r.json().get("journal", ""))
    refusal = lb.auto_bot_live_refusal("live")
    rb = c.post("/api/bot/start", json={"mode": "live"})
    check("2 gates", "auto-bot still refuses live (needs AUTO_BOT_LIVE_ENABLED) even with manual live armed",
          refusal is not None and "AUTO_BOT_LIVE_ENABLED" in refusal and rb.status_code == 403, rb.json()["detail"][:100])
    cs = c.get("/api/manual/connection_status", params={"verify": "true"}).json()
    lm = cs["modes"]["live"]
    check("2 gates", "connection_status?verify=true: masked key, read-only wallet check (never the full key)",
          lm["connected"] and lm["api_key_masked"] == mask_secret(FAKE_KEY) and FAKE_KEY not in json.dumps(cs) and FAKE_SECRET not in json.dumps(cs),
          f"{lm['api_key_masked']} · {lm['message']} · equity ₹{lm['wallet_equity_inr']:.2f}")

    # -- ₹0 wallet
    order = {"symbol": "SOLUSD", "side": "LONG", "leverage": 20, "margin_inr": 5000, "sl_pct": 2.0, "tp_pct": 4.0,
             "trigger_source": "mark_price"}
    r = c.post("/api/manual/order", json={**order, **mb()})
    check("2 orders", "LIVE wallet ₹0 -> friendly refusal, nothing sent", r.status_code == 422 and "₹0.00" in r.json()["detail"]
          and "add funds" in r.json()["detail"] and not calls(live, "POST", "/v2/orders"), r.json()["detail"][:120])
    live.wallet_usd = 50_000 / RATE
    mtr._cache.pop("snap:live", None)

    # -- market long with SL/TP
    r = c.post("/api/manual/order", json={**order, **mb()})
    o = r.json()
    post = calls(live, "POST", "/v2/orders")[-1]["body"] if calls(live, "POST", "/v2/orders") else {}
    lev = [x["body"] for x in calls(live, "POST", f"/v2/products/{live.meta['SOLUSD']['id']}/orders/leverage")]
    check("2 orders", "isolated margin set + leverage 20x synced before the entry (PUT margin_mode, POST .../orders/leverage)",
          live.margin_mode_calls == 1 and lev == [{"leverage": "20"}], f"margin_mode calls {live.margin_mode_calls}, leverage {lev}")
    st = c.get("/api/manual/state", params={"symbol": "SOLUSD", "include_candles": False}).json()
    sol = next((p for p in st["positions"] if p["symbol"] == "SOLUSD"), {})
    check("2 orders", "market LONG SOLUSD 20x: bracket SL/TP on the entry (trigger mark_price), legs bound in bot_state_live.db",
          r.status_code == 200 and o["result"] == "opened" and post.get("order_type") == "market_order"
          and post.get("bracket_stop_trigger_method") == "mark_price" and "bracket_stop_loss_price" in post
          and sol.get("sl_order_id") and sol.get("tp_order_id") and sol.get("journalled") and sol.get("leverage") == 20
          and (T / "bot_state_live.db").exists(),
          f"{sol.get('contracts')} @ {sol.get('entry_price')}, legs SL {sol.get('sl_order_id')} TP {sol.get('tp_order_id')}, liq (Delta) {sol.get('liq_price_est')}")
    check("2 sync", "state shows Delta's wallet (USD x 85) and exchange positions", abs(st["wallet"]["realized_equity_inr"] - 50_000) < 1e-6
          and st["exchange"]["connected"] and sol.get("liq_source") == "delta", f"wallet ₹{st['wallet']['realized_equity_inr']:,.2f}, used ₹{st['wallet']['used_margin_inr']:,.2f}")

    # -- market short with SL only
    r = c.post("/api/manual/order", json={"symbol": "BTCUSD", "side": "SHORT", "leverage": 15, "contracts": 3, "sl_pct": 1.5, **mb()})
    st = c.get("/api/manual/state", params={"symbol": "BTCUSD", "include_candles": False}).json()
    btc = next((p for p in st["positions"] if p["symbol"] == "BTCUSD"), {})
    check("2 orders", "market SHORT BTCUSD 15x with SL only: SL leg bound, no TP leg", r.status_code == 200 and btc.get("side") == "SHORT"
          and btc.get("sl_order_id") and not btc.get("tp_order_id") and live.leverage.get(27) == "15", f"{btc.get('contracts')} @ {btc.get('entry_price')}")

    # -- limit order + cancel; second limit fills later
    eth_px = live.prices[3136]
    lim = round(round(eth_px * 0.97 / 0.05) * 0.05, 2)  # on ETHUSD's 0.05 tick
    r1 = c.post("/api/manual/order", json={"symbol": "ETHUSD", "side": "LONG", "order_type": "limit", "limit_price": lim, "leverage": 10,
                                            "contracts": 5, "sl_pct": 2.0, "tp_pct": 4.0, **mb()})
    ex1 = r1.json().get("exchange", {})
    st = c.get("/api/manual/state", params={"symbol": "ETHUSD", "include_candles": False}).json()
    pend = [p for p in st["pending_orders"] if p["symbol"] == "ETHUSD"]
    rc = c.delete(f"/api/manual/order/{r1.json().get('intent_id')}", params=mb())
    dels = calls(live, "DELETE", "/v2/orders")
    check("2 orders", "limit LONG ETHUSD rests on the exchange (tick-snapped), listed, then cancelled via DELETE /v2/orders",
          r1.status_code == 200 and r1.json()["result"] == "resting" and pend and rc.status_code == 200
          and dels and str(dels[-1]["body"].get("id")) == str(ex1.get("exchange_order_id"))
          and live.orders[int(ex1["exchange_order_id"])]["state"] == "cancelled",
          f"order {ex1.get('exchange_order_id')} @ {lim} -> cancelled")
    r2 = c.post("/api/manual/order", json={"symbol": "ETHUSD", "side": "LONG", "order_type": "limit", "limit_price": lim, "leverage": 10,
                                            "contracts": 5, "sl_pct": 2.0, "tp_pct": 4.0, **mb()})
    oid2 = int(r2.json()["exchange"]["exchange_order_id"])
    live.fill_resting(oid2)
    st = c.get("/api/manual/state", params={"symbol": "ETHUSD", "include_candles": False}).json()
    eth = next((p for p in st["positions"] if p["symbol"] == "ETHUSD"), {})
    check("2 orders", "resting limit filled on the exchange -> next sync opens the journal position with its bracket legs",
          eth.get("contracts") == 5 and abs(eth.get("entry_price", 0) - lim) < 1e-9 and eth.get("sl_order_id") and eth.get("tp_order_id"),
          f"ETH 5 @ {eth.get('entry_price')}, legs {eth.get('sl_order_id')}/{eth.get('tp_order_id')}")

    # -- protection edits
    sol = next(p for p in c.get("/api/manual/state", params={"symbol": "SOLUSD", "include_candles": False}).json()["positions"] if p["symbol"] == "SOLUSD")
    new_sl, new_tp = round(sol["entry_price"] * 0.985, 4), round(sol["entry_price"] * 1.03, 4)
    n_put = len(calls(live, "PUT", "/v2/orders"))
    r = c.put("/api/manual/position/SOLUSD/protection", json={"sl_price": new_sl, "tp_price": new_tp, **mb()})
    puts = calls(live, "PUT", "/v2/orders")[n_put:]
    check("2 protect", "edit SL/TP on SOLUSD modifies the live legs via PUT /v2/orders", r.status_code == 200 and len(puts) == 2
          and {str(p["body"]["id"]) for p in puts} == {str(sol["sl_order_id"]), str(sol["tp_order_id"])}
          and abs(float(live.orders[int(sol["sl_order_id"])]["stop_price"]) - new_sl) < 1e-9
          and abs(float(live.orders[int(sol["tp_order_id"])]["stop_price"]) - new_tp) < 1e-9,
          f"{[(p['body']['id'], p['body'].get('stop_price')) for p in puts]}")
    n_br = len(calls(live, "POST", "/v2/orders/bracket"))
    btc_tp = round(btc["entry_price"] * 0.97, 1)
    r = c.put("/api/manual/position/BTCUSD/protection", json={"tp_price": btc_tp, **mb()})
    btc2 = r.json().get("position", {})
    check("2 protect", "adding a TP to BTCUSD (opened without one) creates the protective leg (POST /v2/orders/bracket) and saves its id",
          r.status_code == 200 and len(calls(live, "POST", "/v2/orders/bracket")) == n_br + 1 and btc2.get("tp_order_id"),
          f"new TP leg {btc2.get('tp_order_id')} @ {btc_tp}")
    rt = c.put("/api/manual/position/SOLUSD/protection", json={"trail_amount": 1.0, **mb()})
    check("2 protect", "trailing stop refused for exchange positions (paper-only) — 422", rt.status_code == 422)

    # -- scale-in
    n_put = len(calls(live, "PUT", "/v2/orders"))
    r = c.post("/api/manual/order", json={"symbol": "SOLUSD", "side": "LONG", "leverage": 20, "contracts": 2, **mb()})
    rbad = c.post("/api/manual/order", json={"symbol": "SOLUSD", "side": "LONG", "leverage": 10, "contracts": 1, **mb()})
    sol2 = next(p for p in c.get("/api/manual/state", params={"symbol": "SOLUSD", "include_candles": False}).json()["positions"] if p["symbol"] == "SOLUSD")
    ex_size = live.positions[int(live.meta["SOLUSD"]["id"])]["size"]
    resized = calls(live, "PUT", "/v2/orders")[n_put:]
    last_post = [x for x in calls(live, "POST", "/v2/orders")][-1]["body"]
    check("2 scale", "scale-in +2 SOL at 20x: no bracket on the add, journal synced to exchange size/avg entry, legs resized",
          r.status_code == 200 and r.json()["result"] == "added" and "bracket_stop_loss_price" not in last_post
          and sol2["contracts"] == ex_size == sol["contracts"] + 2 and len(resized) == 2 and all(x["body"]["size"] == ex_size for x in resized)
          and rbad.status_code == 422, f"{sol['contracts']} -> {sol2['contracts']} @ {sol2['entry_price']:.4f}; legs size {[x['body']['size'] for x in resized]}")

    # -- partial close
    n_before = sol2["contracts"]
    r = c.post("/api/manual/position/SOLUSD/close", json=mb(contracts=3))
    pc = r.json()
    ro = [x["body"] for x in calls(live, "POST", "/v2/orders")][-1]
    sol3 = next(p for p in c.get("/api/manual/state", params={"symbol": "SOLUSD", "include_candles": False}).json()["positions"] if p["symbol"] == "SOLUSD")
    if "trade" not in pc:
        console.print(f"  [red]partial close response {r.status_code}: {pc}[/]")
    fill = next((f for f in live.fills if str(f["order_id"]) == pc.get("trade", {}).get("exit_order_id")), {"price": "nan"})
    check("2 close", "partial close 3 SOL: reduce-only market sell, booked at the fill price + commission, remaining + legs resized",
          r.status_code == 200 and pc["result"] == "partial_exit" and ro.get("reduce_only") is True and ro.get("size") == 3 and ro.get("side") == "sell"
          and abs(pc["trade"]["exit_price"] - float(fill["price"])) < 1e-9 and sol3["contracts"] == n_before - 3
          and live.orders[int(sol3["sl_order_id"])]["size"] == n_before - 3,
          f"{n_before} -> {sol3['contracts']}, exit {pc['trade']['exit_price']}, net ₹{pc['trade']['net_pnl_inr']:.2f}")

    # -- exchange-side TP on BTC (leg fills on Delta, sync books it)
    btc_pos = live.positions[27]
    live.trigger_leg(int(btc2["tp_order_id"]), btc_tp)
    st = c.get("/api/manual/state", params={"symbol": "BTCUSD", "include_candles": False}).json()
    tb = next((t for t in st["trades"] if t["symbol"] == "BTCUSD"), None)
    check("2 close", "TP leg filled by Delta -> sync books the exit (TP, exit order = leg) and cancels the SL leg",
          tb is not None and tb["exit_reason"] == "TP" and tb["exit_order_id"] == str(btc2["tp_order_id"]) and live.positions[27]["size"] == 0
          and live.orders[int(btc2["sl_order_id"])]["state"] == "cancelled" and not any(p["symbol"] == "BTCUSD" for p in st["positions"]),
          f"BTC exit {tb['exit_price'] if tb else '-'} net ₹{tb['net_pnl_inr'] if tb else 0:.2f}; size before {btc_pos}")

    # -- full closes
    rs = c.post("/api/manual/position/SOLUSD/close", json=mb())
    re_ = c.post("/api/manual/position/ETHUSD/close", json=mb())
    st = c.get("/api/manual/state", params={"symbol": "ETHUSD", "include_candles": False}).json()
    legs_left = [o for o in live.orders.values() if o.get("stop_order_type") and o["state"] in ("open", "pending")]
    check("2 close", "full close SOL + ETH: reduce-only market, child SL/TP legs cancelled, exchange flat, journal empty",
          rs.json().get("result") == "exit" and re_.json().get("result") == "exit" and not legs_left
          and all(p["size"] == 0 for p in live.positions.values()) and not st["positions"], f"open legs left {len(legs_left)}")

    # -- external position
    xrp_id = int(live.meta["XRPUSD"]["id"])
    live.positions[xrp_id] = {"size": 50, "entry_price": live.prices[xrp_id]}
    mtr._cache.pop("snap:live", None)
    st = c.get("/api/manual/state", params={"symbol": "XRPUSD", "include_candles": False}).json()
    ext = next((p for p in st["positions"] if p["symbol"] == "XRPUSD"), {})
    rx = c.post("/api/manual/position/XRPUSD/close", json=mb())
    check("2 sync", "position opened outside the terminal shows as 'exchange (not journalled)' and can be closed reduce-only",
          ext.get("journalled") is False and "not journalled" in ext.get("source", "") and rx.json().get("result") == "external_close"
          and live.positions[xrp_id]["size"] == 0)

    # -- ledger audit (live journal)
    store = ee.StateStore(T / "bot_state_live.db")
    try:
        trs = store.trades("live")
        bad = []
        for t in trs:
            d = 1 if t["side"] == "LONG" else -1
            gross = d * t["contracts"] * t["contract_value"] * (t["exit_price"] - t["entry_price"])
            fills = [f for f in live.fills if str(f["order_id"]) == t["exit_order_id"]]
            exit_fee = sum(float(f["commission"]) for f in fills)
            if abs(gross - t["gross_pnl_usd"]) > 1e-9 or abs(t["net_pnl_usd"] - (t["gross_pnl_usd"] - t["fees_gst_usd"] + t["funding_usd"])) > 1e-9 \
                    or abs(t["net_pnl_inr"] - t["net_pnl_usd"] * RATE) > 1e-9 or not fills or exit_fee >= t["fees_gst_usd"]:
                bad.append(t["trade_id"])
        cfg = store.config()
        intents = store.q("SELECT state, count(*) n FROM order_intents GROUP BY state ORDER BY state")
    finally:
        store.close()
    tab = Table(title="Live (mocked) trade_history — bot_state_live.db")
    for col in ("#", "symbol", "side", "lev", "lots", "entry", "exit", "reason", "exit order", "gross ₹", "fees+GST ₹", "net ₹"):
        tab.add_column(col, justify="right")
    for t in trs:
        tab.add_row(str(t["trade_id"]), t["symbol"], t["side"], f"{t['leverage']:g}x", str(t["contracts"]), f"{t['entry_price']:.4f}",
                    f"{t['exit_price']:.4f}", t["exit_reason"], str(t["exit_order_id"]), f"{t['gross_pnl_inr']:.2f}",
                    f"{t['fees_gst_inr']:.2f}", f"{t['net_pnl_inr']:.2f}")
    console.print(tab)
    check("2 ledger", "live journal audit: bound to mode 'live'; every exit booked from its own fills (gross/net/INR identities)",
          cfg["mode"] == "live" and len(trs) == 4 and not bad, f"{len(trs)} trades; intents {[(r['state'], r['n']) for r in intents]}")
    check("2 ledger", "every authenticated request to the fake exchange carried a valid HMAC signature + headers",
          live.sig_failures == 0 and live.header_failures == 0, f"{live.sig_checked} signed requests")

    # -- testnet path + isolation
    settings.TESTNET_API_KEY, settings.TESTNET_API_SECRET = FAKE_KEY, FAKE_SECRET
    tn.wallet_usd = 50_000 / RATE
    r = c.post("/api/manual/mode", json={"mode": "testnet"})
    ro = c.post("/api/manual/order", json={"symbol": "ETHUSD", "side": "SHORT", "leverage": 5, "contracts": 2, "tp_pct": 3.0, **mb()})
    rcl = c.post("/api/manual/position/ETHUSD/close", json=mb())
    s_tn = ee.StateStore(T / "bot_state_testnet.db")
    try:
        tn_trades, tn_mode = s_tn.trades("testnet"), s_tn.config()["mode"]
    finally:
        s_tn.close()
    check("2 isolation", "TESTNET mode: same flow on the testnet fake, journalled in bot_state_testnet.db only",
          r.status_code == 200 and ro.status_code == 200 and rcl.json().get("result") == "exit" and tn_mode == "testnet"
          and len(tn_trades) == 1 and len(calls(tn, "POST", "/v2/orders")) == 2 and tn.margin_mode_calls == 1,
          f"testnet trades {len(tn_trades)}")
    c.post("/api/manual/mode", json={"mode": "paper"})
    rp = c.post("/api/manual/order", json={"symbol": "BTCUSD", "side": "LONG", "leverage": 10, "contracts": 1, **mb()})
    probe = ee.StateStore(T / "bot_state_live.db")
    try:
        probe.init_config("paper", {})
        wrong = False
    except RuntimeError:
        wrong = True
    finally:
        probe.close()
    check("2 isolation", "PAPER mode journals to bot_state.db; a mode's journal refuses to open as another mode",
          rp.status_code == 200 and (T / "bot_state.db").exists() and wrong, "live journal opened as paper -> RuntimeError")


def test_regression(skip: bool) -> None:
    console.rule("[bold]3. Regression: step8_verify.py (runs step7 -> step6 -> step5 -> step4)")
    if skip:
        console.print("[yellow]skipped (--skip-regression)[/]")
        return
    t0 = time.time()
    p = subprocess.run([sys.executable, "step8_verify.py"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=PROJECT_ROOT, timeout=3600, env={**os.environ, "PYTHONIOENCODING": "utf-8", "COLUMNS": "200"})
    out = p.stdout
    console.print(out, markup=False, highlight=False)
    if p.stderr.strip():
        console.print(p.stderr[-3000:], markup=False, highlight=False)
    for step, n in (("Step 4", 77), ("Step 5", 92), ("Step 6", 47), ("Step 7", 42), ("Step 8", 33)):
        lines = [ln.strip("─ ") for ln in out.splitlines() if f"{step} verification:" in ln and "──" in ln]
        check("3 regression", f"{step} verification PASS - {n} checks", bool(lines) and f"PASS - {n} checks" in lines[-1],
              lines[-1] if lines else "verdict line not found")
    check("3 regression", "step8_verify.py exit code 0", p.returncode == 0, f"{time.time() - t0:.0f}s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-regression", action="store_true")
    ap.add_argument("--skip-live-probe", action="store_true")
    args = ap.parse_args()
    console.print(f"test process: MODE={settings.MODE!r} LIVE_TRADING_ENABLED={settings.LIVE_TRADING_ENABLED} live key in settings="
                  f"{bool(settings.LIVE_API_KEY)} (testenv) · real bot lock held: {lb.InstanceLock.is_held(lb.LOCK_PATH)} (untouched)")
    if not args.skip_live_probe:
        test_live_probe()
    test_mocked()
    test_regression(args.skip_regression)
    console.rule("[bold]Summary")
    t = Table()
    for col in ("section", "check", "result", "detail"):
        t.add_column(col)
    for sec, name, ok, detail in CHECKS:
        t.add_row(sec, name, "[green]PASS" if ok else "[red]FAIL", detail[:100])
    console.print(t)
    failed = [x for x in CHECKS if not x[2]]
    console.rule(f"Step 8B verification: {'[green]PASS' if not failed else f'[red]FAIL ({len(failed)} checks)'} - {len(CHECKS)} checks")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
