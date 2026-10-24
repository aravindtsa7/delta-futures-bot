"""Step 9 verification: execution & recovery correctness repairs for review findings F01-F11 and source findings #1-#7
(DELTA_FUTURES_BOT_DEEP_REVIEW.md, baseline bfcad919f69be62849fb4ee8473cb8e2eaf98773).

Every check below reproduces a reviewed failure case with deterministic fakes (FakeDelta9: the Step 5 HMAC-checking
fake exchange + partial fills, hidden order/fill views, injected margin/leverage behaviour, no-op bracket attaches,
refusable cancels, maker commissions; scripted WebSocket servers; synthetic candles) and asserts the repaired
behaviour, so each would have failed on the baseline commit. No request ever reaches a real Delta server here;
the regression section re-runs the full chain (step8b -> step8 -> step7 -> step6 -> step5 -> step4), whose only
real-server calls are Step 8B's read-only GET wallet / GET positions probe.

All files live in data/step9_test/; the real journals, lock, coin list and research artifacts are never touched.
Usage:  python step9_repair_verify.py [--skip-regression]
"""

from __future__ import annotations

import testenv  # noqa: F401  (research mode, no live keys in settings — before config is imported)

import argparse
import asyncio
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import httpx
import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

warnings.filterwarnings("ignore", message=".*httpx.*starlette.testclient.*")
from fastapi.testclient import TestClient  # noqa: E402

import app as webapp  # noqa: E402
import backtester as btm  # noqa: E402
import backup_state  # noqa: E402
import bot_ops  # noqa: E402
import data_manager as dm  # noqa: E402
import execution_engine as ee  # noqa: E402
import live_bot as lb  # noqa: E402
import manual_trade as mtr  # noqa: E402
import ws_manager as wm  # noqa: E402
from config import PROJECT_ROOT, settings  # noqa: E402
from exchange_client import DeltaAPIError, DeltaRestClientAdapter, ExchangeError, RateLimitBudget  # noqa: E402
from notifier import TelegramNotifier  # noqa: E402
from step5_verify import FAKE_KEY, FAKE_SECRET, FakeClock, FakeDelta, fake_client, us  # noqa: E402
from step6_verify import FakeMarket, FakeWSServer  # noqa: E402

console = Console(width=200)
CHECKS: list[tuple[str, str, bool, str]] = []
T = settings.data_path / "step9_test"
RATE = settings.FIXED_USD_INR_RATE
ETH, BTC = 3136, 27
EXEC_MODES = ("paper", "testnet", "live")


def check(section: str, name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((section, name, bool(ok), detail))
    console.print(f"  {'[green]PASS[/]' if ok else '[red]FAIL[/]'} {name}" + (f"  [dim]{detail}[/]" if detail else ""))
    return bool(ok)


def guarded(section: str, fn: Callable[[], None]) -> None:
    """Run one finding's test; an exception is a FAIL (this is what the baseline commit produces for most cases)."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc(limit=3).strip().splitlines()[-1]
        check(section, f"{fn.__name__} raised", False, f"{type(exc).__name__}: {str(exc)[:160]} | {tb[:120]}")


def fresh(path: Path) -> Path:
    for suffix in ("", "-wal", "-shm", ".lock"):
        p = path.with_name(path.name + suffix)
        if p.exists():
            p.unlink()
    return path


def raises(exc_type: type[BaseException] | tuple, fn: Callable, *a: Any, **kw: Any) -> tuple[bool, str]:
    try:
        fn(*a, **kw)
    except exc_type as exc:
        return True, f"{type(exc).__name__}: {str(exc)[:150]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"wrong exception {type(exc).__name__}: {str(exc)[:150]}"
    return False, "no exception"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class RealClock:
    def __call__(self) -> int:
        return int(time.time() * 1_000_000)

    def seconds(self) -> float:
        return time.time()


class FakeDelta9(FakeDelta):
    """Step 5 fake + partial fills, hidden order/fill views (F08), margin-mode / leverage injection (F07, F09),
    no-op bracket attaches and child-less entries (F06), refusable cancels (F05), maker commissions (#4)."""

    def __init__(self, clock: Any):
        super().__init__(clock)
        self.hidden_coids: set[str] = set()       # order views (client oid, active, history, by id) do not show them
        self.hidden_fill_orders: set[int] = set()  # GET /v2/fills does not show fills of these orders
        self.no_children = False                   # entry brackets create no child legs
        self.bracket_noop = False                  # POST /v2/orders/bracket answers OK but creates nothing
        self.margin_error: tuple[int, str] | None = None
        self.margin_mode = "isolated"
        self.leverage_sticky = False               # POST .../orders/leverage answers but keeps (and echoes) the old value
        self.cancel_reject: set[int] = set()
        self._bracket_call = False

    def _legs(self, pid: int, side: str, size: int, sl: str | None, tp: str | None) -> None:
        if (self.no_children and not self._bracket_call) or (self.bracket_noop and self._bracket_call):
            return
        super()._legs(pid, side, size, sl, tp)

    def _fill(self, o: dict[str, Any], price: float) -> None:
        self.fill_qty(o["id"], int(o["unfilled_size"]), price)

    def fill_qty(self, order_id: int, qty: int, price: float | None = None, role: str = "taker") -> None:
        """Fill ``qty`` of an order (partial fills keep a limit order open). Bracket children are created on the first
        fill with the FULL order size (the engine must size them to the filled position)."""
        o = self.orders[order_id]
        px = float(price if price is not None else (o["limit_price"] or self.prices[o["product_id"]]))
        pid, d = o["product_id"], 1 if o["side"] == "buy" else -1
        pos = self.positions.setdefault(pid, {"size": 0, "entry_price": 0.0})
        if o.get("reduce_only"):
            qty = min(qty, abs(int(pos["size"])))
        first = int(o["unfilled_size"]) == int(o["size"])
        if qty > 0:
            old = pos["size"]
            new = old + d * qty
            if old == 0 or (old > 0) == (d > 0):
                pos["entry_price"] = (abs(old) * pos["entry_price"] + qty * px) / (abs(old) + qty)
            elif new * old < 0:
                pos["entry_price"] = px
            pos["size"] = new
            if new == 0:
                pos["entry_price"] = 0.0
            done = int(o["size"]) - int(o["unfilled_size"])
            prev = float(o["average_fill_price"] or 0)
            o["average_fill_price"] = str((prev * done + px * qty) / (done + qty))
            o["unfilled_size"] = int(o["unfilled_size"]) - qty
            fee = qty * self.cv(pid) * px * (settings.MAKER_FEE if role == "maker" else settings.TAKER_FEE) * (1 + settings.GST_ON_FEES)
            f = {"id": self.next_fill_id, "order_id": o["id"], "product_id": pid, "side": o["side"], "size": qty, "price": str(px),
                 "role": role, "fill_type": "normal", "commission": str(fee), "created_at_us": self.clock()}
            if role == "maker":
                f["paid_commission"] = str(fee)
            self.fills.append(f)
            self.next_fill_id += 1
        o["state"] = "closed" if int(o["unfilled_size"]) == 0 or o["order_type"] == "market_order" else "open"
        if first and qty > 0 and not o.get("stop_order_type") and o["order_type"] == "limit_order" and (
                o.get("bracket_stop_loss_price") or o.get("bracket_take_profit_price")):
            self._legs(pid, o["side"], int(o["size"]), o.get("bracket_stop_loss_price"), o.get("bracket_take_profit_price"))

    def external(self, pid: int, side: str, qty: int, price: float | None = None, reduce_only: bool = False) -> int:
        """A market order placed outside the bot (website / another app)."""
        o = self._new_order({"product_id": pid, "size": qty, "side": side, "order_type": "market_order", "reduce_only": reduce_only})
        self._fill(o, price if price is not None else self.prices[pid])
        return o["id"]

    def _visible(self, o: dict[str, Any] | None) -> bool:
        return o is not None and o.get("client_order_id") not in self.hidden_coids

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, m = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        prev_state = None
        if m == "DELETE" and path == "/v2/orders" and body.get("id") is not None and int(body["id"]) in self.orders:
            prev_state = self.orders[int(body["id"])]["state"]
        prev_lev = None
        if m == "POST" and path.endswith("/orders/leverage"):
            prev_lev = self.leverage.get(int(path.split("/")[3]))
        self._bracket_call = m == "POST" and path == "/v2/orders/bracket"
        try:
            r = super().handler(request)
        finally:
            self._bracket_call = False
        if m == "PUT" and path == "/v2/users/margin_mode" and self.margin_error:
            status, code = self.margin_error
            return self.err(status, code)
        if m == "PUT" and path == "/v2/users/margin_mode" and r.status_code == 200:
            self.margin_mode = body.get("margin_mode", self.margin_mode)
        if path == "/v2/users/trading_preferences" and r.status_code == 200:
            b = json.loads(r.content)
            b["result"]["margin_mode"] = self.margin_mode
            return httpx.Response(200, json=b)
        if m == "POST" and path.endswith("/orders/leverage") and self.leverage_sticky:
            pid = int(path.split("/")[3])
            if prev_lev is None:
                self.leverage.pop(pid, None)
            else:
                self.leverage[pid] = prev_lev
            return self.ok({"leverage": prev_lev or "200", "order_margin": "0", "product_id": pid})
        if m == "DELETE" and prev_state is not None and int(body["id"]) in self.cancel_reject:
            self.orders[int(body["id"])]["state"] = prev_state
            return self.err(400, "cancel_rejected")
        if m == "GET" and path.startswith("/v2/orders/client_order_id/") and path.rsplit("/", 1)[1] in self.hidden_coids:
            return self.err(404, "not_found")
        if m == "GET" and path.startswith("/v2/orders/") and path.rsplit("/", 1)[1].isdigit():
            if not self._visible(self.orders.get(int(path.rsplit("/", 1)[1]))):
                return self.err(404, "not_found")
        if m == "GET" and path in ("/v2/orders", "/v2/orders/history", "/v2/fills") and r.status_code == 200:
            b = json.loads(r.content)
            if path == "/v2/fills":
                b["result"] = [f for f in b["result"] if int(f["order_id"]) not in self.hidden_fill_orders]
            else:
                b["result"] = [o for o in b["result"] if self._visible(o)]
            return httpx.Response(200, json=b)
        return r


def calls(f: FakeDelta, method: str, path: str) -> list[dict[str, Any]]:
    return [c for c in f.calls if c["method"] == method and c["path"] == path]


def tn_engine(fake: FakeDelta9, clock: FakeClock, name: str) -> tuple[ee.ExecutionEngine, ee.StateStore]:
    store = ee.StateStore(fresh(T / f"{name}.db"), clock=clock)
    eng = ee.ExecutionEngine(store, "testnet", fake_client(fake, clock), clock=clock, notifier=TelegramNotifier(None, None))
    return eng, store


def reopen(store: ee.StateStore, fake: FakeDelta9, clock: FakeClock) -> tuple[ee.ExecutionEngine, ee.StateStore]:
    """A process restart: close the journal, open it again with a new engine."""
    path = store.path
    store.close()
    st = ee.StateStore(path, clock=clock)
    return ee.ExecutionEngine(st, "testnet", fake_client(fake, clock), clock=clock, notifier=TelegramNotifier(None, None)), st


def tn_runner(fake: FakeDelta9, clock: FakeClock, name: str, server: Any = None) -> lb.LiveBotRunner:
    return lb.LiveBotRunner("testnet", client=fake_client(fake, clock), market=FakeMarket(clock.seconds, {}, {"ETHUSD": 3000.0, "BTCUSD": 81_600.0}),
                            store_path=fresh(T / f"{name}.db"), lock_path=T / f"{name}.lock", clock=clock.seconds,
                            ws_connector=(server or FakeWSServer()).connect, ws_api_key=FAKE_KEY, ws_api_secret=FAKE_SECRET,
                            control_dir=T / f"{name}_ctl", notifier=TelegramNotifier(None, None))


def intent_of(store: ee.StateStore, iid: str) -> sqlite3.Row:
    return store.intent(iid)


def kinds(store: ee.StateStore, symbol: str | None = None) -> list[str]:
    sql, args = "SELECT kind FROM events", ()
    if symbol:
        sql, args = sql + " WHERE symbol=?", (symbol,)
    return [r["kind"] for r in store.q(sql + " ORDER BY id", args)]


def legs_sizes(fake: FakeDelta9, pid: int) -> list[int]:
    return sorted(int(o["size"]) for o in fake.orders.values()
                  if o["product_id"] == pid and o.get("stop_order_type") and o["state"] in ("open", "pending"))


# --------------------------------------------------------------------------- #
# Engine-level findings
# --------------------------------------------------------------------------- #
def test_f03_exit_recovery() -> None:
    console.rule("[bold]F03 — a timed-out EXIT is recovered as an exit (never as a scale-in)")
    clock = FakeClock(us("2026-10-20 07:00:05"))
    fake = FakeDelta9(clock)
    eng, store = tn_engine(fake, clock, "f03")
    eng.manual_exchange_entry("ETHUSD", 1, 10, 10, ref_price=3000.0)
    fake.prices[ETH] = 3060.0
    clock.t += 60_000_000
    fake.timeout_after_processing_next_post = True
    ok_amb, det = raises(ExchangeError, eng.exchange_reduce, "ETHUSD", 3)
    it = store.one("SELECT * FROM order_intents WHERE role='EXIT' ORDER BY created_at_us DESC LIMIT 1")
    rec = eng.reconcile_unknown_intents()
    r = next((x for x in rec if x.get("intent_id") == it["intent_id"]), {})
    tr = store.trades("testnet")
    pos = store.position("testnet", "ETHUSD")
    ev = kinds(store, "ETHUSD")
    check("F03", "partial close 3/10 times out (fills at Delta) -> OUTCOME_UNKNOWN, not resent", ok_amb and it["state"] != "FILLED"
          and fake.positions[ETH]["size"] == 7 and len(calls(fake, "POST", "/v2/orders")) == 2, det)
    check("F03", "recovery books it through the EXIT path: 1 trade of 3 (fees + realized PnL), position 10 -> 7, legs n/a",
          r.get("action") == "partial_exit" and len(tr) == 1 and tr[0]["contracts"] == 3 and tr[0]["exit_reason"] == "MANUAL_CLOSE"
          and tr[0]["fees_gst_usd"] > 0 and abs(tr[0]["exit_price"] - 3060.0) < 1e-9 and tr[0]["gross_pnl_usd"] > 0
          and pos["contracts"] == 7 and "entry_added" not in ev and "manual_scale_in" not in ev,
          f"result {r.get('action')}, trades {[(t['contracts'], t['exit_reason'], round(t['net_pnl_inr'], 2)) for t in tr]}, journal {pos['contracts'] if pos else None}")
    clock.t += 60_000_000
    fake.timeout_after_processing_next_post = True
    raises(ExchangeError, eng.exchange_reduce, "ETHUSD", None)
    eng, store = reopen(store, fake, clock)  # restart before reconciling the full exit
    eng.reconcile_unknown_intents()
    eng.reconcile_unknown_intents()           # and again: exactly once
    tr = store.trades("testnet")
    check("F03", "full-close timeout + restart: booked exactly once (2 trades total, journal flat, exchange flat)",
          len(tr) == 2 and tr[1]["contracts"] == 7 and store.position("testnet", "ETHUSD") is None and fake.positions[ETH]["size"] == 0
          and len(store.q("SELECT * FROM booked_fills WHERE role='EXIT'")) == 2,
          f"trades {[t['contracts'] for t in tr]}, booked exit fills {len(store.q('SELECT * FROM booked_fills WHERE role=?', ('EXIT',)))}")
    store.close()


def test_f04_partial_fills() -> None:
    console.rule("[bold]F04 — an open partial fill is a managed position (4/10 -> 7/10 -> cancel -> restart)")
    clock = FakeClock(us("2026-10-20 07:00:05"))
    fake = FakeDelta9(clock)
    eng, store = tn_engine(fake, clock, "f04")
    r = eng.manual_exchange_entry("ETHUSD", 1, 10, 10, order_type="limit", ref_price=3000.0, limit_price=2950.0, sl=2900.0, tp=3100.0)
    oid, iid = int(r["exchange_order_id"]), r["intent_id"]
    clock.t += 30_000_000
    fake.fill_qty(oid, 4)
    eng.refresh_resting_orders()
    p1, it1, legs1 = store.position("testnet", "ETHUSD"), intent_of(store, iid), legs_sizes(fake, ETH)
    check("F04", "4 of 10 filled while 6 rest -> journal position 4 now (PARTIALLY_FILLED, filled_size_booked 4)",
          p1 is not None and p1["contracts"] == 4 and it1["state"] == "PARTIALLY_FILLED" and it1["filled_size_booked"] == 4
          and fake.positions[ETH]["size"] == 4, f"journal {p1['contracts'] if p1 else None}, intent {it1['state']} booked {it1['filled_size_booked']}")
    check("F04", "protective SL/TP legs bound and sized to the FILLED 4 contracts (venue children were 10)",
          legs1 == [4, 4] and p1["sl_order_id"] and p1["tp_order_id"], f"leg sizes {legs1}")
    clock.t += 30_000_000
    fake.fill_qty(oid, 3, 2949.0)
    eng.refresh_resting_orders()
    eng.refresh_resting_orders()  # a second pass must not book again
    p2, legs2 = store.position("testnet", "ETHUSD"), legs_sizes(fake, ETH)
    avg = (4 * 2950.0 + 3 * 2949.0) / 7
    check("F04", "7/10 -> position 7 at the VWAP of both slices, legs resized to 7, no double counting",
          p2["contracts"] == 7 and abs(p2["entry_price"] - avg) < 1e-9 and legs2 == [7, 7]
          and len(store.q("SELECT * FROM booked_fills WHERE intent_id=?", (iid,))) == 2, f"{p2['contracts']} @ {p2['entry_price']:.4f}, legs {legs2}")
    r_c = eng.client.cancel_order(ETH, order_id=oid)
    eng._apply_order(iid, r_c)
    eng, store = reopen(store, fake, clock)
    eng.reconcile_unknown_intents()
    eng.refresh_resting_orders()
    p3, it3 = store.position("testnet", "ETHUSD"), intent_of(store, iid)
    check("F04", "cancel the remaining 3 + restart: intent FILLED 7/10, position 7 = exchange 7, nothing booked twice",
          it3["state"] == "FILLED" and it3["filled_size"] == 7 and it3["filled_size_booked"] == 7 and p3["contracts"] == 7
          and fake.positions[ETH]["size"] == 7 and not store.trades("testnet"), f"intent {it3['state']} {it3['filled_size']}/{it3['size']}")
    store.close()


def test_f08_unknown_outcome() -> None:
    console.rule("[bold]F08 — negative order lookups never turn a real fill into REJECTED")
    clock = FakeClock(us("2026-10-20 07:00:05"))
    fake = FakeDelta9(clock)
    eng, store = tn_engine(fake, clock, "f08a")
    fake.timeout_after_processing_next_post = True
    r = eng.manual_exchange_entry("ETHUSD", 1, 5, 10, ref_price=3000.0)
    coid = intent_of(store, r["intent_id"])["client_order_id"]
    fake.hidden_coids.add(coid)                    # client-oid, active, history and by-id views show nothing
    clock.t += ee.UNKNOWN_GRACE_US + 1_000_000
    n_fills = len(calls(fake, "GET", "/v2/fills"))
    rec = eng.reconcile_unknown_intents()
    it, pos = intent_of(store, r["intent_id"]), store.position("testnet", "ETHUSD")
    check("F08", "order invisible for 61 s but its fill exists -> recovered from GET /v2/fills (FILLED, journal 5 = exchange 5)",
          it["state"] == "FILLED" and pos is not None and pos["contracts"] == 5 and fake.positions[ETH]["size"] == 5
          and len(calls(fake, "GET", "/v2/fills")) > n_fills, f"steps {rec[0].get('steps') if rec else None}")
    store.close()

    clock = FakeClock(us("2026-10-20 07:00:05"))
    fake = FakeDelta9(clock)
    eng, store = tn_engine(fake, clock, "f08b")
    fake.timeout_after_processing_next_post = True
    r = eng.manual_exchange_entry("ETHUSD", 1, 5, 10, ref_price=3000.0)
    it0 = intent_of(store, r["intent_id"])
    fake.hidden_coids.add(it0["client_order_id"])
    fake.hidden_fill_orders.update(o["id"] for o in fake.orders.values() if o["client_order_id"] == it0["client_order_id"])
    clock.t += ee.UNKNOWN_GRACE_US + 1_000_000
    rec = eng.reconcile_unknown_intents()
    it = intent_of(store, r["intent_id"])
    check("F08", "no order and no fill visible, but Delta holds 5 vs journal 0 -> stays OUTCOME_UNKNOWN (symbol blocked), "
          "CRITICAL unresolved_position_delta", rec and rec[0]["result"] == "UNRESOLVED_POSITION_DELTA" and it["state"] == "OUTCOME_UNKNOWN"
          and eng.symbol_blocked("ETHUSD") is not None and "unresolved_position_delta" in kinds(store, "ETHUSD")
          and len(calls(fake, "GET", "/v2/positions/margined")) >= 1, f"steps {rec[0]['steps'] if rec else None}")
    ok_blk, det = raises(ee.EntryBlockedError, eng.manual_exchange_entry, "ETHUSD", 1, 5, 10, ref_price=3000.0)
    check("F08", "...and a new manual entry on that symbol is refused (nothing sent)", ok_blk
          and len(calls(fake, "POST", "/v2/orders")) == 1, det)
    store.close()


def test_f09_margin_mode() -> None:
    console.rule("[bold]F09 — an isolated-margin rejection is never reported as success")
    clock = FakeClock(us("2026-10-20 07:00:05"))
    fake = FakeDelta9(clock)
    eng, store = tn_engine(fake, clock, "f09")
    fake.margin_error = (400, "cannot_switch_to_isolated_with_open_positions")
    ok1, det1 = raises(DeltaAPIError, eng.ensure_isolated_margin)
    check("F09", "HTTP 400 cannot_switch_to_isolated_with_open_positions -> propagated (not 'isolated, changed=false')", ok1, det1)
    fake.margin_error, fake.margin_mode = (400, "margin_mode_already_set"), "isolated"
    r2 = eng.ensure_isolated_margin()
    fake.margin_mode = "cross"
    ok3, det3 = raises(ExchangeError, eng.ensure_isolated_margin)
    check("F09", "'already set' is benign only when the readback confirms isolated (cross readback -> refused)",
          r2.get("changed") is False and r2.get("verified_by") == "readback" and ok3, f"{r2} / {det3}")
    store.close()


def test_src1_atomic() -> None:
    console.rule("[bold]#1 — fill booking is atomic; crash points recover exactly once; legacy journals migrate safely")
    clock = FakeClock(us("2026-10-20 07:00:05"))
    fake = FakeDelta9(clock)
    eng, store = tn_engine(fake, clock, "src1")
    cols = {r["name"] for r in store.q("PRAGMA table_info(order_intents)")}
    bcols = [r["name"] for r in store.q("PRAGMA table_info(booked_fills)")]
    check("#1", "schema: booked_fills(fill_id PK, intent_id, order_id, symbol, role, side, size, price, fee_usd, created_at) + "
          "order_intents.filled_size_booked", "filled_size_booked" in cols and bcols == ["fill_id", "intent_id", "order_id", "symbol",
                                                                                         "role", "side", "size", "price", "fee_usd", "created_at"])
    orig = store.record_fills
    state = {"armed": True}

    def crash(rows: list[dict[str, Any]]) -> None:
        if state["armed"]:
            state["armed"] = False
            raise RuntimeError("simulated crash between the position write and the fill ledger")
        orig(rows)
    store.record_fills = crash
    ok_c, det = raises(RuntimeError, eng.manual_exchange_entry, "ETHUSD", 1, 5, 10, ref_price=3000.0)
    it = store.one("SELECT * FROM order_intents ORDER BY created_at_us DESC LIMIT 1")
    check("#1", "crash mid-booking rolls back the whole unit: no position, intent not FILLED, nothing in booked_fills",
          ok_c and store.position("testnet", "ETHUSD") is None and it["state"] == "DISPATCHING" and it["filled_size_booked"] == 0
          and not store.q("SELECT * FROM booked_fills") and fake.positions[ETH]["size"] == 5, f"intent {it['state']}; {det}")
    eng, store = reopen(store, fake, clock)  # restart: DISPATCHING -> OUTCOME_UNKNOWN -> reconciled from the exchange
    eng.reconcile_unknown_intents()
    eng.reconcile_unknown_intents()
    p = store.position("testnet", "ETHUSD")
    check("#1", "restart recovers the crashed booking exactly once (position 5, 1 booked fill, intent FILLED)",
          p is not None and p["contracts"] == 5 and len(store.q("SELECT * FROM booked_fills")) == 1
          and intent_of(store, it["intent_id"])["state"] == "FILLED")
    r = eng.manual_exchange_entry("BTCUSD", 1, 4, 10, order_type="limit", ref_price=81_600.0, limit_price=81_000.0)
    fake.fill_qty(int(r["exchange_order_id"]), 2)
    store.transition(r["intent_id"], "PARTIALLY_FILLED", "pre-Step-9 view: recorded, never booked", filled_size=2)
    rec = eng.reconcile_unknown_intents()
    eng.reconcile_unknown_intents()
    pb = store.position("testnet", "BTCUSD")
    check("#1", "a PARTIALLY_FILLED intent with filled_size 2 > filled_size_booked 0 is booked by recovery, once",
          any(x.get("result") == "BOOKED_RECOVERY" for x in rec) and pb is not None and pb["contracts"] == 2
          and intent_of(store, r["intent_id"])["filled_size_booked"] == 2, f"{[x.get('result') for x in rec]}")
    store.close()

    legacy = fresh(T / "legacy.db")
    st = ee.StateStore(legacy)
    st.init_config("testnet", {})
    for iid, state_, n in (("old-filled", "FILLED", 5), ("old-partial", "PARTIALLY_FILLED", 3)):
        st.conn.execute("INSERT INTO order_intents (intent_id, client_order_id, mode, symbol, product_id, role, side, size, order_type,"
                        " state, filled_size, created_at_us, updated_at_us) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (iid, iid, "testnet", "ETHUSD", ETH, "ENTRY", "buy", 10, "limit_order", state_, n, 1, 1))
    st.conn.execute("ALTER TABLE order_intents DROP COLUMN filled_size_booked")
    st.close()
    st = ee.StateStore(legacy)
    rows = {r["intent_id"]: r["filled_size_booked"] for r in st.q("SELECT intent_id, filled_size_booked FROM order_intents")}
    st.close()
    check("#1", "migration of a pre-Step-9 journal: FILLED rows count as booked, PARTIALLY_FILLED rows stay unbooked (F04)",
          rows == {"old-filled": 5, "old-partial": 0}, str(rows))


def test_src3_external() -> None:
    console.rule("[bold]#3 — external partial close / add / reversal reconcile the journal position")
    clock = FakeClock(us("2026-10-20 07:00:05"))
    fake = FakeDelta9(clock)
    eng, store = tn_engine(fake, clock, "src3")
    eng.manual_exchange_entry("ETHUSD", 1, 10, 10, ref_price=3000.0, sl=2900.0, tp=3150.0)
    clock.t += 60_000_000
    fake.external(ETH, "sell", 4, 3020.0, reduce_only=True)
    r1 = eng.sync_exchange_position("ETHUSD")
    p1, tr = store.position("testnet", "ETHUSD"), store.trades("testnet")
    check("#3", "external reduce 10 -> 6: partial EXT trade of 4 at the fill price, journal 6, SL/TP legs resized to 6",
          (r1 or {}).get("action") == "partial_exit" and p1["contracts"] == 6 and len(tr) == 1 and tr[0]["contracts"] == 4
          and tr[0]["exit_reason"] == "EXT" and abs(tr[0]["exit_price"] - 3020.0) < 1e-9 and legs_sizes(fake, ETH) == [6, 6],
          f"{r1 and r1.get('action')}, legs {legs_sizes(fake, ETH)}")
    clock.t += 60_000_000
    fake.external(ETH, "buy", 3, 3030.0)
    r2 = eng.sync_exchange_position("ETHUSD")
    p2 = store.position("testnet", "ETHUSD")
    check("#3", "external add 6 -> 9: journal adopts Delta's size and average entry, legs resized to 9",
          (r2 or {}).get("action") == "external_add" and p2["contracts"] == 9
          and abs(p2["entry_price"] - fake.positions[ETH]["entry_price"]) < 1e-9 and legs_sizes(fake, ETH) == [9, 9],
          f"{p2['contracts']} @ {p2['entry_price']:.4f} (Delta {fake.positions[ETH]['entry_price']:.4f})")
    clock.t += 60_000_000
    fake.external(ETH, "sell", 12, 3010.0)
    r3 = eng.sync_exchange_position("ETHUSD")
    tr = store.trades("testnet")
    check("#3", "external reversal 9 -> -3: journal position closed (EXT trade of 9), legs cancelled, new side left unjournalled",
          (r3 or {}).get("action") == "exit" and r3.get("flipped") and store.position("testnet", "ETHUSD") is None
          and tr[-1]["contracts"] == 9 and legs_sizes(fake, ETH) == [] and fake.positions[ETH]["size"] == -3,
          f"trades {[t['contracts'] for t in tr]}")
    store.close()


def test_src4_fees() -> None:
    console.rule("[bold]#4 — venue commission (paid_commission) is booked instead of a taker estimate")
    clock = FakeClock(us("2026-10-20 07:00:05"))
    fake = FakeDelta9(clock)
    eng, store = tn_engine(fake, clock, "src4")
    r = eng.manual_exchange_entry("ETHUSD", 1, 5, 10, order_type="limit", ref_price=3000.0, limit_price=2950.0)
    fake.fill_qty(int(r["exchange_order_id"]), 5, role="maker")
    eng.refresh_resting_orders()
    p = store.position("testnet", "ETHUSD")
    paid = sum(float(f["paid_commission"]) for f in fake.fills if f.get("paid_commission"))
    taker = 5 * 0.01 * 2950.0 * settings.TAKER_FEE * (1 + settings.GST_ON_FEES)
    check("#4", "maker limit fill: entry_fee_usd = Delta's paid_commission (maker), not the taker estimate",
          abs(p["entry_fee_usd"] - paid) < 1e-12 and abs(p["entry_fee_usd"] - taker) > 1e-6, f"booked ${p['entry_fee_usd']:.6f}, paid ${paid:.6f}, taker est ${taker:.6f}")
    store.close()


def test_f02_engine_gate() -> None:
    console.rule("[bold]F02 — shared manual entry gate (engine): duplicate request id, unresolved outcome")
    clock = FakeClock(us("2026-10-20 07:00:05"))
    fake = FakeDelta9(clock)
    eng, store = tn_engine(fake, clock, "f02e")
    fake.timeout_after_processing_next_post = True
    r1 = eng.manual_exchange_entry("ETHUSD", 1, 5, 10, ref_price=3000.0, client_request_id="click-1")
    ok_dup, det_dup = raises(ee.EntryBlockedError, eng.manual_exchange_entry, "ETHUSD", 1, 5, 10, ref_price=3000.0, client_request_id="click-1")
    it = intent_of(store, r1["intent_id"])
    fake.hidden_coids.add(it["client_order_id"])
    fake.hidden_fill_orders.update(o["id"] for o in fake.orders.values() if o["client_order_id"] == it["client_order_id"])
    ok_blk, det_blk = raises(ee.EntryBlockedError, eng.manual_exchange_entry, "ETHUSD", 1, 5, 10, ref_price=3000.0, client_request_id="click-2")
    fake.hidden_coids.clear()
    fake.hidden_fill_orders.clear()
    eng, store = reopen(store, fake, clock)
    eng.reconcile_unknown_intents()
    p = store.position("testnet", "ETHUSD")
    check("F02", "timeout-after-fill, repeated click (same id), retry with a new id while unresolved, restart: ONE exchange entry, "
          "journal 5 = exchange 5", r1["action"] == "outcome_unknown" and ok_dup and ok_blk and len(calls(fake, "POST", "/v2/orders")) == 1
          and p is not None and p["contracts"] == 5 and fake.positions[ETH]["size"] == 5, f"{det_dup[:70]} | {det_blk[:70]}")
    store.close()


# --------------------------------------------------------------------------- #
# Runner-level findings
# --------------------------------------------------------------------------- #
def test_f05_flatten() -> None:
    console.rule("[bold]F05 — Emergency Flatten cancels entry orders first and reports FLAT only when nothing can reopen exposure")
    clock = FakeClock(us("2026-10-20 07:00:05"))
    bot = lb.LiveBotRunner("paper", market=FakeMarket(clock.seconds, {}, {"ETHUSD": 2400.0, "BTCUSD": 81_600.0}),
                           store_path=fresh(T / "f05p.db"), lock_path=T / "f05p.lock", clock=clock.seconds,
                           control_dir=T / "f05p_ctl", notifier=TelegramNotifier(None, None))
    try:
        it = bot.store.create_intent(intent_id="mn-rest", client_order_id="f05-rest", mode="paper", symbol="ETHUSD", product_id=ETH,
                                     role="ENTRY", side="buy", size=2, order_type="limit_order", entry_price=2390.0, leverage=10.0, source="manual")
        bot.store.transition(it["intent_id"], "ACKNOWLEDGED", "paper limit resting")
        rep = bot.emergency_flatten_all()
        fills = bot.engine.check_pending_limits("ETHUSD", us("2026-10-20 07:01:00"), 2380.0, 2400.0)
        check("F05", "paper: resting ETH buy limit, no position -> flatten cancels it; the next matching tick opens NOTHING",
              rep["status"] == "FLAT" and "mn-rest" in rep["cancelled_orders"] and bot.store.intent("mn-rest")["state"] == "CANCELLED"
              and not fills and bot.store.position("paper", "ETHUSD") is None and rep["entries_paused"], f"status {rep['status']}, cancelled {rep['cancelled_orders']}")
    finally:
        bot.close()

    clock = FakeClock(us("2026-10-20 07:00:05"))
    fake = FakeDelta9(clock)
    bot = tn_runner(fake, clock, "f05t")
    try:
        r = bot.engine.manual_exchange_entry("ETHUSD", 1, 5, 10, order_type="limit", ref_price=3000.0, limit_price=2950.0)
        oid = int(r["exchange_order_id"])
        fake.fill_qty(oid, 2)  # partial fill lands before the flatten
        rep = bot.emergency_flatten_all()
        tr = bot.store.trades("testnet")
        check("F05", "testnet: limit 5 with 2 filled -> DELETE the rest, book the 2, close them reduce-only, FLAT (0 positions, 0 entry orders)",
              rep["status"] == "FLAT" and any(int(c["body"]["id"]) == oid for c in calls(fake, "DELETE", "/v2/orders"))
              and fake.orders[oid]["state"] == "cancelled" and fake.positions[ETH]["size"] == 0 and len(tr) == 1 and tr[0]["contracts"] == 2
              and not rep["open_entry_orders"], f"status {rep['status']}, trades {[t['contracts'] for t in tr]}")
        r2 = bot.engine.manual_exchange_entry("ETHUSD", 1, 3, 10, order_type="limit", ref_price=3000.0, limit_price=2940.0)
        fake.cancel_reject.add(int(r2["exchange_order_id"]))
        rep2 = bot.emergency_flatten_all()
        check("F05", "a cancel Delta refuses (order still resting) -> FLATTEN_INCOMPLETE listing the open entry order, never FLAT",
              rep2["status"] == "FLATTEN_INCOMPLETE" and any(str(o.get("order_id")) == r2["exchange_order_id"] for o in rep2["open_entry_orders"]),
              f"open entry orders {rep2['open_entry_orders']}")
    finally:
        bot.close()


def test_f06_protection() -> None:
    console.rule("[bold]F06 — readiness requires confirmed protection (an attach ACK is not proof)")
    clock = FakeClock(us("2026-10-20 07:00:05"))
    fake = FakeDelta9(clock)
    bot = tn_runner(fake, clock, "f06")
    try:
        fake.no_children, fake.bracket_noop = True, True
        bot.engine.manual_exchange_entry("ETHUSD", 1, 5, 10, ref_price=3000.0, sl=2940.0, tp=3150.0)
        bot.store.set_halt(False)  # isolate the readiness rule from the engine's own unprotected-fill halt
        n_br = len(calls(fake, "POST", "/v2/orders/bracket"))
        s1 = bot.run_reconciliation("f06 #1")
        s2 = bot.run_reconciliation("f06 #2 (within the retry window)")
        n_br2 = len(calls(fake, "POST", "/v2/orders/bracket"))
        check("F06", "position 5, no SL/TP children, bracket attach ACKed but readback empty -> DEGRADED, "
              "missing_confirmed_protection:ETHUSD, can_enter False", s1["state"] == "DEGRADED" and "missing_confirmed_protection:ETHUSD" in s1["issues"]
              and not bot.can_enter() and s1["positions"]["ETHUSD"]["sl_leg"] is None, f"issues {s1['issues']}")
        check("F06", "next reconciliation within 5 min only reads back (no blind second attach), still DEGRADED",
              n_br2 == n_br + 1 and s2["state"] == "DEGRADED", f"bracket POSTs {n_br} -> {n_br2}")
        fake.bracket_noop = False
        clock.t += (lb.BRACKET_RETRY_S + 1) * 1_000_000
        s3 = bot.run_reconciliation("f06 #3")
        sl_id = s3["positions"]["ETHUSD"]["sl_leg"]
        check("F06", "attach that really creates the legs -> readback confirms (product, opposite side, reduce-only, size 5) -> OBSERVED_READY",
              s3["state"] == "OBSERVED_READY" and s3["positions"]["ETHUSD"]["protection_confirmed"] and legs_sizes(fake, ETH) == [5, 5],
              f"legs {legs_sizes(fake, ETH)}")
        fake.orders[int(sl_id)]["reduce_only"] = False
        fake.bracket_noop = True
        clock.t += (lb.BRACKET_RETRY_S + 1) * 1_000_000
        s4 = bot.run_reconciliation("f06 #4")
        check("F06", "an SL order that is not reduce-only does not count as protection -> DEGRADED again",
              s4["state"] == "DEGRADED" and "missing_confirmed_protection:ETHUSD" in s4["issues"], f"issues {s4['issues']}")
        fake.orders[int(sl_id)]["reduce_only"] = True
        tp_id = int(s3["positions"]["ETHUSD"]["tp_leg"])
        fake.orders[tp_id]["size"] = 3
        n_put = len(calls(fake, "PUT", "/v2/orders"))
        s5 = bot.run_reconciliation("f06 #5")
        check("F06", "a TP leg of the wrong size (3 != 5) is resized in place (PUT /v2/orders), then confirmed",
              s5["state"] == "OBSERVED_READY" and fake.orders[tp_id]["size"] == 5 and len(calls(fake, "PUT", "/v2/orders")) > n_put)
    finally:
        bot.close()


def test_f07_account_policy() -> None:
    console.rule("[bold]F07 — the runner applies and verifies isolated margin + 10x before bot entries")
    clock = FakeClock(us("2026-10-20 07:00:05"))
    fake = FakeDelta9(clock)  # the fake venue starts at 200x
    bot = tn_runner(fake, clock, "f07")
    try:
        bot.engine.manual_exchange_entry("BTCUSD", 1, 2, 25, ref_price=81_600.0)  # a manual 25x position stays as it is
        n_lev_btc = len(calls(fake, "POST", f"/v2/products/{BTC}/orders/leverage"))
        s = bot.run_reconciliation("f07")
        pol = s.get("account_policy") or {}
        check("F07", "reconciliation: PUT margin_mode isolated + POST 10x on the flat ETHUSD, confirmed by Delta's answer",
              len(calls(fake, "PUT", "/v2/users/margin_mode")) >= 1 and fake.leverage.get(ETH) == "10" and "ETHUSD" not in pol.get("blocked", {}),
              f"margin {pol.get('margin_mode')}, ETH leverage {fake.leverage.get(ETH)}")
        check("F07", "BTCUSD holds a manual 25x position: leverage NOT reset, symbol blocked for bot entries until flat",
              fake.leverage.get(BTC) == "25" and len(calls(fake, "POST", f"/v2/products/{BTC}/orders/leverage")) == n_lev_btc
              and "BTCUSD" in pol.get("blocked", {}) and bot.engine.account_policy_refusal("BTCUSD"), pol.get("blocked", {}).get("BTCUSD", "")[:90])
        res = bot.engine.on_signal("ETHUSD", 1, us("2026-10-20 06:00"), 20.0, 3000.0)
        p = bot.store.position("testnet", "ETHUSD")
        check("F07", "bot ETH entry after the policy: journal leverage 10 == exchange leverage 10",
              res.get("action") == "filled" and p is not None and p["leverage"] == 10 and fake.leverage.get(ETH) == "10")
    finally:
        bot.close()
    clock = FakeClock(us("2026-10-20 07:00:05"))
    fake = FakeDelta9(clock)
    fake.leverage_sticky = True  # Delta keeps (and reports) 200x
    eng, store = tn_engine(fake, clock, "f07b")
    res = eng.on_signal("ETHUSD", 1, us("2026-10-20 06:00"), 20.0, 3000.0)
    check("F07", "leverage readback still 200x -> bot entry refused (skip 'account policy'), no order sent",
          res["action"] == "skip" and "account policy" in res["reason"] and not calls(fake, "POST", "/v2/orders"), res["reason"][:110])
    store.close()


class _RejectingWS(FakeWSServer):
    """Scripted socket server: rejects (or never answers) the public / private subscriptions."""

    def __init__(self, reject_public: bool = False, reject_private: bool = False, silent: bool = False):
        super().__init__()
        self.reject_public, self.reject_private, self.mute = reject_public, reject_private, silent

    def on_message(self, conn: Any, m: dict) -> None:
        if m.get("type") != "subscribe":
            return super().on_message(conn, m)
        if self.mute:
            return None
        bad = self.reject_private if conn.private else self.reject_public
        conn.push({"type": "subscriptions", "channels": [{**ch, "error": "subscription forbidden on this channel"} if bad else ch
                                                         for ch in m["payload"]["channels"]]})
        if conn.private:
            conn.push({"type": "orders", "action": "snapshot", "result": [], "seq_no": 1})
            conn.push({"type": "positions", "action": "snapshot", "result": []})
        return None


class _Rec:
    def __init__(self) -> None:
        self.phases: list[str] = []
        self.ready: list[int] = []
        self.degraded: list[dict] = []
        self.orders: list[dict] = []

    def on_ws_phase(self, p: str, g: int) -> None:
        self.phases.append(p)

    def on_ws_ready(self, g: int) -> None:
        self.ready.append(g)

    def on_ws_degraded(self, g: int, problems: dict) -> None:
        self.degraded.append(problems)

    def on_order(self, m: dict) -> None:
        self.orders.append(m)


def test_f10_subscriptions() -> None:
    console.rule("[bold]F10 — rejected / missing subscriptions are never reported as a healthy READY stream")

    async def run(server: _RejectingWS, rec: _Rec) -> wm.DeltaWebSocketManager:
        mgr = wm.DeltaWebSocketManager("paper", ["BTCUSD", "ETHUSD"], rec, connector=server.connect)
        try:
            await mgr.connect_once()
        finally:
            await mgr.close_sockets()
        return mgr
    rec = _Rec()
    mgr = asyncio.run(run(_RejectingWS(reject_public=True), rec))
    probs = mgr.degraded.get("public", [])
    check("F10", "all 4 public subscriptions rejected -> phase DEGRADED_REST_FALLBACK (never READY), on_ws_degraded fired, "
          "REST reconciliation still runs", mgr.phase == "DEGRADED_REST_FALLBACK" and "READY" not in rec.phases and len(probs) == 4
          and rec.degraded and rec.ready, f"phases {rec.phases}, problems {[p['channel'] for p in probs]}")
    old = wm.HANDSHAKE_TIMEOUT_SECONDS
    wm.HANDSHAKE_TIMEOUT_SECONDS = 0.5
    try:
        rec2 = _Rec()
        mgr2 = asyncio.run(run(_RejectingWS(silent=True), rec2))
    finally:
        wm.HANDSHAKE_TIMEOUT_SECONDS = old
    check("F10", "subscriptions never acknowledged (timeout) -> DEGRADED_REST_FALLBACK with every channel missing (no exception, no READY)",
          mgr2.phase == "DEGRADED_REST_FALLBACK" and len(mgr2.degraded.get("public", [])) == 4 and rec2.ready, f"phases {rec2.phases}")

    clock = FakeClock(us("2026-10-20 07:00:05"))
    fake = FakeDelta9(clock)
    bot = tn_runner(fake, clock, "f10", server=_RejectingWS(reject_private=True))

    async def go() -> None:
        try:
            await bot.ws.connect_once()
        finally:
            await bot.ws.close_sockets()
    try:
        asyncio.run(go())
        r = bot.reconciliations[-1]
        check("F10", "testnet: private orders/positions/v2/user_trades rejected -> runner DEGRADED (can_enter False), "
              "private_stream_unavailable recorded", bot.state == "DEGRADED" and not bot.can_enter()
              and any(i.startswith("private_stream_unavailable") for i in r["issues"]) and "ws_rest_fallback" in kinds(bot.store),
              f"issues {r['issues']}")
    finally:
        bot.close()


def test_src2_private_stream() -> None:
    console.rule("[bold]#2 — private stream contract: order_id / id, snapshot envelopes, positions + user_trades handlers")
    rec = _Rec()
    mgr = wm.DeltaWebSocketManager("paper", ["ETHUSD"], rec)
    mgr.handle_message({"type": "orders", "action": "snapshot", "result": [
        {"order_id": 11, "product_symbol": "ETHUSD", "state": "open"}, {"id": 12, "symbol": "ETHUSD", "state": "open"}]}, mgr.generation, "private")
    mgr.handle_message({"type": "orders", "action": "update", "order_id": 13, "symbol": "ETHUSD", "state": "closed"}, mgr.generation, "private")
    ids = [(o["id"], o["order_id"], o["product_symbol"], o["snapshot"]) for o in rec.orders]
    check("#2", "orders snapshot envelope unpacked into 2 items; 'order_id' (docs wire key) and 'id' mapped consistently",
          ids == [(11, 11, "ETHUSD", True), (12, 12, "ETHUSD", True), (13, 13, "ETHUSD", False)], str(ids))

    clock = FakeClock(us("2026-10-20 07:00:05"))
    fake = FakeDelta9(clock)
    bot = tn_runner(fake, clock, "src2")
    try:
        r = bot.engine.manual_exchange_entry("ETHUSD", 1, 5, 10, order_type="limit", ref_price=3000.0, limit_price=2950.0)
        oid = int(r["exchange_order_id"])
        fake.fill_qty(oid, 3)
        bot.ws.handle_message({"type": "orders", "action": "update", "order_id": oid, "symbol": "ETHUSD", "state": "open",
                               "unfilled_size": 2, "size": 5}, bot.ws.generation, "private")
        p1 = bot.store.position("testnet", "ETHUSD")
        fake.fill_qty(oid, 2)
        bot.ws.handle_message({"type": "v2/user_trades", "sy": "ETHUSD", "oid": oid, "p": "2950", "s": 2}, bot.ws.generation, "private")
        p2, it = bot.store.position("testnet", "ETHUSD"), bot.store.intent(r["intent_id"])
        fake.external(ETH, "buy", 2, 2960.0)
        bot.ws.handle_message({"type": "positions", "action": "update", "symbol": "ETHUSD", "size": 7, "entry_price": "2952.857"},
                              bot.ws.generation, "private")
        p3 = bot.store.position("testnet", "ETHUSD")
        check("#2", "orders update carrying only order_id -> partial fill (3) booked at once",
              p1 is not None and p1["contracts"] == 3, f"journal {p1['contracts'] if p1 else None}")
        check("#2", "v2/user_trades frame (compact sy/oid/p/s) -> remaining fill booked (5, intent FILLED)",
              p2["contracts"] == 5 and it["state"] == "FILLED", f"journal {p2['contracts']}, intent {it['state']}")
        check("#2", "positions frame with a size the journal lacks -> on_position reconciles it (external add -> 7)",
              p3["contracts"] == 7 and fake.positions[ETH]["size"] == 7, f"journal {p3['contracts']}")
    finally:
        bot.close()


def test_src5_day_anchor() -> None:
    console.rule("[bold]#5 — the UTC day anchor (incl. mark-to-market at 00:00) survives a restart")
    clock = FakeClock(us("2026-10-20 23:58:30"))
    prices = {"ETHUSD": 2400.0, "BTCUSD": 81_600.0}
    db = fresh(T / "src5.db")

    def runner() -> lb.LiveBotRunner:
        return lb.LiveBotRunner("paper", market=FakeMarket(clock.seconds, {}, prices), store_path=db, lock_path=T / "src5.lock",
                                clock=clock.seconds, control_dir=T / "src5_ctl", notifier=TelegramNotifier(None, None))
    bot = runner()
    try:
        bot.engine.manual_fill("ETHUSD", 1, 10, 2400.0, 10)
        clock.t = us("2026-10-20 23:59:05")
        bot.on_minute_close("ETHUSD", int(pd.Timestamp("2026-10-20 23:58", tz="UTC").timestamp()), 2400, 2401, 2399, 2400)
        clock.t = us("2026-10-21 00:01:02")
        bot.on_minute_close("ETHUSD", int(pd.Timestamp("2026-10-21 00:00", tz="UTC").timestamp()), 2310, 2312, 2299, 2300)
        anchor = bot.day_start_equity
    finally:
        bot.close()
    clock.t = us("2026-10-21 00:31:02")
    bot2 = runner()
    try:
        bot2.on_minute_close("ETHUSD", int(pd.Timestamp("2026-10-21 00:30", tz="UTC").timestamp()), 2305, 2306, 2304, 2305)
        journal_based = bot2._day_start_from_journal(int(pd.Timestamp("2026-10-21", tz="UTC").timestamp()) // 86_400)
        meta = (bot2.store.meta_get("utc_day_anchor_date"), bot2.store.meta_get("utc_day_start_equity_usd"))
        check("#5", "restart at 00:31 keeps the persisted 00:00 baseline (with the open ETH loss marked to market), "
              "not the realized-only journal estimate", abs(bot2.day_start_equity - anchor) < 1e-6 and abs(anchor - journal_based) > 100
              and meta[0] == "2026-10-21", f"anchor ₹{anchor:,.2f} (persisted {meta}), journal-only ₹{journal_based:,.2f}")
    finally:
        bot2.close()


def test_src6_operations() -> None:
    console.rule("[bold]#6 — restore holds both locks; unique Parquet temp paths; REST work never stalls the event loop")
    db = fresh(T / "src6_restore.db")
    st = ee.StateStore(db)
    st.init_config("paper", ee.FROZEN_PROFILES)
    st.close()
    man = backup_state.backup(db, T / "src6_backups")
    lock = T / "src6.lock"
    probes: dict[str, str] = {}
    orig = backup_state._copy_online

    def spy(src: Path, dst: Path) -> None:
        if str(dst).endswith(".restore.tmp"):
            probes["bot_lock"] = raises(lb.BotAlreadyRunningError, lb.InstanceLock, lock, "intruder")[1]
            probes["journal_lock"] = raises(ee.StateStoreLockedError, ee.StateStore, db)[1]
        orig(src, dst)
    backup_state._copy_online = spy
    try:
        out = backup_state.restore(man["backup"], db, lock)
    finally:
        backup_state._copy_online = orig
    after = lb.InstanceLock(lock, "after")
    after.release()
    check("#6", "restore(): a bot runner AND a journal writer are both refused DURING the file replacement; both free after",
          out["verify"]["ok"] and probes.get("bot_lock", "").startswith("BotAlreadyRunningError")
          and probes.get("journal_lock", "").startswith("StateStoreLockedError"), str(probes)[:150])

    p = T / "src6.parquet"
    errors: list[str] = []
    barrier, serial = threading.Barrier(2), threading.Lock()
    real_replace = os.replace

    def interleaved_replace(a: Any, b: Any) -> None:  # both writers finish their temp file before either renames
        barrier.wait(5)
        with serial:
            real_replace(a, b)

    def write(i: int) -> None:
        try:
            dm._atomic_write_parquet(pd.DataFrame({"time": np.arange(1000) + i, "v": float(i)}), p)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
    os.replace = interleaved_replace
    try:
        with ThreadPoolExecutor(2) as ex:
            list(ex.map(write, range(2)))
    finally:
        os.replace = real_replace
    tmps = {dm._tmp_path(p) for _ in range(3)}
    leftovers = list(T.glob("src6.parquet*.tmp"))
    check("#6", "two candle writers interleaved on one Parquet file: unique temp paths ({path}.{pid}.{uuid8}.tmp), both renames "
          "succeed, no leftovers", len(tmps) == 3 and all(f".{os.getpid()}." in t.name for t in tmps) and not errors and not leftovers
          and len(pd.read_parquet(p)) == 1000, f"e.g. {min(t.name for t in tmps)}; errors {errors[:2]}")

    class Slow:
        def on_ws_ready(self, g: int) -> None:
            time.sleep(0.6)  # a REST reconciliation

    async def stall(coro_factory: Callable[[], Any]) -> float:
        ticks: list[float] = []
        done = asyncio.Event()

        async def ticker() -> None:
            while not done.is_set():
                ticks.append(time.perf_counter())
                await asyncio.sleep(0.02)
        t = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        await coro_factory()
        done.set()
        await t
        return max(b - a for a, b in zip(ticks, ticks[1:]))

    async def ws_case() -> float:
        mgr = wm.DeltaWebSocketManager("paper", ["ETHUSD"], Slow(), connector=FakeWSServer().connect, offload=True)
        try:
            return await stall(mgr.connect_once)
        finally:
            await mgr.close_sockets()
    gap_ws = asyncio.run(ws_case())

    clock = FakeClock(us("2026-10-20 07:00:05"))
    bot = lb.LiveBotRunner("paper", market=FakeMarket(clock.seconds, {}, {"ETHUSD": 2400.0, "BTCUSD": 81_600.0}),
                           store_path=fresh(T / "src6_bot.db"), lock_path=T / "src6_bot.lock", clock=clock.seconds,
                           control_dir=T / "src6_ctl", notifier=TelegramNotifier(None, None))
    try:
        real = bot.handle_command

        def slow_cmd(cmd: str, args: dict) -> dict:
            time.sleep(0.6)  # e.g. a flatten with REST round trips
            return real(cmd, args)
        bot.handle_command = slow_cmd
        bot.control.submit("pause")

        async def control_case() -> float:
            stop = asyncio.Event()

            async def drive() -> None:
                task = asyncio.create_task(bot._control_loop(stop))
                for _ in range(200):
                    await asyncio.sleep(0.02)
                    if not bot.control.pending():
                        break
                stop.set()
                await task
            return await stall(drive)
        gap_ctl = asyncio.run(control_case())
        paused = bot.engine.entries_paused()
    finally:
        bot.close()
    check("#6", "a 0.6 s blocking reconciliation / control command runs in a worker thread: the event loop never stalls > 0.25 s",
          gap_ws < 0.25 and gap_ctl < 0.25 and paused and bot.ws.offload and "to_thread" in inspect.getsource(lb.LiveBotRunner._poller),
          f"max loop stall: ws {gap_ws * 1000:.0f} ms, control {gap_ctl * 1000:.0f} ms")


def test_f11_zero_trades() -> None:
    console.rule("[bold]F11 — a valid zero-trade backtest passes its audit (engine + /api/backtest/run)")
    idx = pd.date_range("2026-09-01", periods=6 * 1440, freq="1min", tz="UTC")
    flat = pd.DataFrame({"open": 3000.0, "high": 3000.0, "low": 3000.0, "close": 3000.0, "volume": 1.0, "mark_open": 3000.0,
                         "mark_high": 3000.0, "mark_low": 3000.0, "mark_close": 3000.0, "funding_rate_pct": 0.0, "funding_rate": 0.0},
                        index=idx)
    leg = btm.Leg(btm.ProductSpec.from_metadata("ETHUSD"), flat, pd.Series(0, index=dm.resample_candles(flat, "1h").index), "1h")
    cfg = btm.BacktestConfig(initial_capital_inr=50_000.0)
    r = btm.Backtester(cfg).run_portfolio([leg])
    errs = btm.audit_result(r)
    check("F11", "no-signal run: empty trade ledger with the full canonical schema; audit passes (equity identity), final = initial",
          r.trades.empty and list(r.trades.columns) == list(btm.TRADE_COLUMNS) and not errs
          and abs(r.summary["final_equity_inr"] - 50_000.0) < 1e-9 and r.summary["total_trades"] == 0, f"audit errors {errs}")
    orig_data, orig_frame, orig_mtime = webapp.get_data, webapp.get_frame, webapp.mtime_key
    webapp.get_data = lambda symbol: flat

    def no_signal_frame(*a: Any, **kw: Any) -> tuple:
        bars, out, atr = orig_frame(*a, **kw)
        return bars, out.assign(signal=0), atr
    webapp.get_frame = no_signal_frame
    webapp.mtime_key = lambda symbol: ("step9-synthetic",)
    webapp._frame_cache.clear()
    try:
        res = TestClient(webapp.app).post("/api/backtest/run", json={"symbols": ["ETHUSD"], "days": 3, "walk_forward_windows": 2,
                                                                       "include_chart": False})
        body = res.json()
    finally:
        webapp.get_data, webapp.get_frame, webapp.mtime_key = orig_data, orig_frame, orig_mtime
        webapp._frame_cache.clear()
    check("F11", "POST /api/backtest/run on a no-signal window -> HTTP 200, 0 trades, audit passed (walk-forward windows too)",
          res.status_code == 200 and body["summary"]["total_trades"] == 0 and body["audit"]["passed"]
          and all(w["audit_passed"] for w in body["windows"]), f"HTTP {res.status_code} {str(body)[:120] if res.status_code != 200 else ''}")


# --------------------------------------------------------------------------- #
# API-level findings (manual terminal)
# --------------------------------------------------------------------------- #
def _seed_market() -> None:
    """Deterministic catalog / quotes for the terminal (the TestClient never needs the public network)."""
    far = time.time() + 1e9
    cat, quotes = {}, {"BTCUSD": (81_599.5, 81_600.0), "ETHUSD": (2_999.95, 3_000.0), "XRPUSD": (2.4999, 2.5), "DOGEUSD": (0.19999, 0.2)}
    for sym, (bid, ask) in quotes.items():
        m = dm.load_metadata(sym)
        im = float(m.get("initial_margin") or 1)
        cat[sym] = {"symbol": sym, "product_id": int(m["id"]), "description": sym, "contract_value": float(m["contract_value"]),
                    "tick_size": float(m["tick_size"]), "initial_margin_pct": im, "maintenance_margin_pct": float(m.get("maintenance_margin") or 0.5),
                    "max_leverage": round(100.0 / im, 4), "trading_status": "operational", "underlying": sym[:-3],
                    "settling_asset": "USD", "position_size_limit": None}
        tk = {"symbol": sym, "best_bid": bid, "best_ask": ask, "mark_price": ask, "last_price": ask, "spot_price": ask,
              "change_24h_pct": 0.0, "mark_change_24h_pct": 0.0, "high_24h": ask, "low_24h": bid, "open_24h": ask,
              "funding_rate_pct": 0.0, "volume_24h": 0.0, "turnover_usd_24h": 0.0, "at": far}
        mtr._cache[f"tk:{sym}"] = (far, tk)
        mtr._cache[f"ob:{sym}"] = (far, None)
        bot_ops._cache[f"tk:{sym}"] = (far, tk)
    mtr._cache["catalog"] = (far, cat)


def mb(**extra: Any) -> dict[str, Any]:
    mode, gen = bot_ops.mode_state()
    return {"expected_mode": mode, "mode_generation": gen, **({"confirm": "LIVE"} if mode == "live" else {}), **extra}


def test_api() -> None:
    console.rule("[bold]F01 / F02 / F09 / #7 / Section 5 — manual terminal API (isolated journals, fake Delta)")
    A = T / "api"
    if A.exists():
        shutil.rmtree(A)
    A.mkdir(parents=True)
    bot_ops.configure(db=A / "bot_state.db", lock=A / "live_bot.lock", control=A / "bot_control", backups=A / "backups", logs=A / "logs")
    dm.APP_SYMBOLS_FILE = A / "app_symbols.json"
    ee.BOT_PROFILES_FILE = A / "bot_profiles.json"
    if hasattr(bot_ops, "_notifiers"):
        bot_ops._notifiers.clear()
        bot_ops._notifiers.update({m: TelegramNotifier(None, None, mode=m) for m in EXEC_MODES})
    _seed_market()
    live, tn = FakeDelta9(RealClock()), FakeDelta9(RealClock())

    def factory(mode: str, read_only: bool) -> DeltaRestClientAdapter:
        f = live if mode == "live" else tn
        return DeltaRestClientAdapter(mode, FAKE_KEY, FAKE_SECRET, transport=httpx.MockTransport(f.handler), read_only=read_only,
                                      budget=RateLimitBudget(), sleep=lambda s: None)
    mtr.CLIENT_FACTORY = factory
    mtr._margin_ok.clear()
    saved = (settings.MODE, settings.LIVE_TRADING_ENABLED, settings.LIVE_API_KEY, settings.LIVE_API_SECRET,
             settings.TESTNET_API_KEY, settings.TESTNET_API_SECRET)
    c = TestClient(webapp.app)
    try:
        _api_body(c, live, tn)
    finally:
        mtr.CLIENT_FACTORY = None
        mtr._margin_ok.clear()
        settings.LIVE_TRADING_ENABLED = False  # before MODE: the config validator forbids live trading outside MODE=live
        (settings.MODE, settings.LIVE_TRADING_ENABLED, settings.LIVE_API_KEY, settings.LIVE_API_SECRET,
         settings.TESTNET_API_KEY, settings.TESTNET_API_SECRET) = saved


def _api_body(c: TestClient, live: FakeDelta9, tn: FakeDelta9) -> None:
    q = {"symbol": "ETHUSD", "include_candles": False}
    s0 = c.get("/api/manual/state", params=q).json()
    pv = c.post("/api/manual/preview", json={"symbol": "ETHUSD", "side": "LONG", "leverage": 10, "contracts": 2}).json()
    ticket = {"symbol": "ETHUSD", "side": "LONG", "leverage": 10, "contracts": 2,
              "expected_mode": pv.get("mode"), "mode_generation": pv.get("mode_generation")}
    settings.MODE, settings.LIVE_TRADING_ENABLED = "live", True
    settings.LIVE_API_KEY, settings.LIVE_API_SECRET = FAKE_KEY, FAKE_SECRET
    sw = c.post("/api/manual/mode", json={"mode": "live", "confirm": "LIVE"}).json()
    check("F01", "mode + monotonic mode_generation returned by GET /state, POST /preview and POST /mode",
          s0.get("mode") == "paper" and s0.get("mode_generation") == 0 and pv.get("mode_generation") == 0
          and sw.get("mode") == "live" and sw.get("mode_generation") == 1, f"state {s0.get('mode')}/{s0.get('mode_generation')}, "
                                                                           f"preview {pv.get('mode_generation')}, switch -> {sw.get('mode_generation')}")
    n0 = len(calls(live, "POST", "/v2/orders"))
    r_stale = c.post("/api/manual/order", json=ticket)
    r_close = c.post("/api/manual/position/ETHUSD/close", json={"expected_mode": "paper", "mode_generation": 0})
    r_prot = c.put("/api/manual/position/ETHUSD/protection", json={"sl_price": 2900, "expected_mode": "paper", "mode_generation": 0})
    r_canc = c.delete("/api/manual/order/abc123", params={"expected_mode": "paper", "mode_generation": 0})
    check("F01", "PAPER ticket submitted after another client switched to LIVE -> 409, no exchange mutation (also close / protection / cancel)",
          r_stale.status_code == 409 and r_close.status_code == 409 and r_prot.status_code == 409 and r_canc.status_code == 409
          and len(calls(live, "POST", "/v2/orders")) == n0 and not calls(live, "PUT", "/v2/orders") and not calls(live, "DELETE", "/v2/orders"),
          f"order {r_stale.status_code} · close {r_close.status_code} · protection {r_prot.status_code} · cancel {r_canc.status_code}: "
          f"{str(r_stale.json().get('detail'))[:80]}")
    r_none = c.post("/api/manual/order", json={k: v for k, v in ticket.items() if k not in ("expected_mode", "mode_generation")})
    r_noconf = c.post("/api/manual/order", json={**ticket, "expected_mode": "live", "mode_generation": 1})
    check("F01", "unbound ticket -> 422; LIVE ticket without confirm='LIVE' on the request -> 422; nothing sent",
          r_none.status_code == 422 and r_noconf.status_code == 422 and len(calls(live, "POST", "/v2/orders")) == n0,
          f"{r_none.status_code} / {r_noconf.status_code}: {str(r_noconf.json().get('detail'))[:70]}")

    live.margin_error = (400, "cannot_switch_to_isolated_with_open_positions")
    r_mm = c.post("/api/manual/order", json={**ticket, **mb()})
    check("F09", "LIVE order while Delta rejects isolated margin -> 409 'no order was placed', failure NOT cached, no POST /v2/orders",
          r_mm.status_code == 409 and "isolated" in str(r_mm.json().get("detail")) and not mtr._margin_ok.get("live")
          and len(calls(live, "POST", "/v2/orders")) == n0, str(r_mm.json().get("detail"))[:110])
    live.margin_error = None
    r_ok = c.post("/api/manual/order", json={**ticket, **mb()})
    live_texts = [h.get("text", "") for h in bot_ops.notifier("live").history] if hasattr(bot_ops, "_notifiers") else []
    paper_texts = [h.get("text", "") for h in bot_ops.notifier("paper").history]
    check("F01", "the same ticket re-bound to LIVE (generation 1, confirm='LIVE') -> exactly one order sent",
          r_ok.status_code == 200 and r_ok.json().get("result") == "opened" and len(calls(live, "POST", "/v2/orders")) == n0 + 1,
          f"HTTP {r_ok.status_code} {r_ok.json().get('result')}")
    check("#7", "manual LIVE trade alert carries [LIVE] (mode-aware notifier), the PAPER notifier did not send it",
          any(t.startswith("[LIVE]") and "ETHUSD" in t for t in live_texts) and not any("ETHUSD" in t and "ENTRY" in t.upper() for t in paper_texts),
          next((t.splitlines()[0] for t in live_texts if "ETHUSD" in t), "no live alert")[:90])

    settings.TESTNET_API_KEY, settings.TESTNET_API_SECRET = FAKE_KEY, FAKE_SECRET
    c.post("/api/manual/mode", json={"mode": "testnet"})
    tn.timeout_after_processing_next_post = True
    base = {"symbol": "ETHUSD", "side": "LONG", "leverage": 10, "contracts": 5}
    r1 = c.post("/api/manual/order", json={**base, **mb(), "client_request_id": "click-1"})
    r2 = c.post("/api/manual/order", json={**base, **mb(), "client_request_id": "click-1"})
    con = bot_ops.ro_conn("testnet")
    coid = con.execute("SELECT client_order_id FROM order_intents ORDER BY created_at_us DESC LIMIT 1").fetchone()[0]
    con.close()
    tn.hidden_coids.add(coid)
    tn.hidden_fill_orders.update(o["id"] for o in tn.orders.values() if o["client_order_id"] == coid)
    r3 = c.post("/api/manual/order", json={**base, **mb(), "client_request_id": "click-2"})
    tn.hidden_coids.clear()
    tn.hidden_fill_orders.clear()
    n_post = len(calls(tn, "POST", "/v2/orders"))
    rs = c.post("/api/manual/sync_exchange")
    acts = rs.json().get("actions", [])
    con = bot_ops.ro_conn("testnet")
    jpos = con.execute("SELECT contracts FROM open_positions WHERE symbol='ETHUSD'").fetchone()
    con.close()
    check("F02", "timeout-after-fill -> outcome_unknown; repeated click (same client_request_id) -> 409; new click while unresolved -> 409",
          r1.status_code == 200 and r1.json().get("result") == "outcome_unknown" and r2.status_code == 409 and r3.status_code == 409
          and n_post == 1, f"{r1.json().get('result')} · {str(r2.json().get('detail'))[:60]} · {str(r3.json().get('detail'))[:60]}")
    check("F02", "POST /api/manual/sync_exchange runs the shared unknown-intent recovery: journal 5 = exchange 5, one entry order",
          any(a.get("kind") == "unknown_intent" and a.get("result") == "FILLED" for a in acts) and jpos is not None and jpos[0] == 5
          and tn.positions[ETH]["size"] == 5 and len(calls(tn, "POST", "/v2/orders")) == 1, f"actions {[(a.get('kind'), a.get('result')) for a in acts][:3]}")

    ra = c.post("/api/manual/symbols", json={"symbol": "XRPUSD", "backfill": False})
    rb = c.post("/api/manual/symbols", json={"symbol": "DOGEUSD", "backfill": False, "enable_auto_bot": True})
    prof = ee.bot_profiles()
    entries = json.loads(ee.BOT_PROFILES_FILE.read_text(encoding="utf-8")) if ee.BOT_PROFILES_FILE.exists() else {}
    check("Section 5", "a newly added coin defaults to {'variant': 'C_costgate', 'enabled': false} (no auto-trading); "
          "enable_auto_bot=true opts in", ra.status_code == 200 and rb.status_code == 200 and entries.get("XRPUSD") == {"variant": "C_costgate", "enabled": False}
          and "XRPUSD" not in prof and prof.get("DOGEUSD") is not None and prof["DOGEUSD"].variant == "C_costgate", f"bot_profiles.json {entries}")


def test_docs() -> None:
    console.rule("[bold]Runbook")
    rb = (PROJECT_ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    need = ("Step 9", "mode_generation", "FLATTEN_INCOMPLETE", "missing_confirmed_protection", "enable_auto_bot", "AUTO_BOT_LIVE_ENABLED",
            "DEGRADED_REST_FALLBACK")
    check("docs", "RUNBOOK.md describes the Step 8B + Step 9 behaviour", all(k in rb for k in need), str([k for k in need if k not in rb]))


# --------------------------------------------------------------------------- #
# Regression
# --------------------------------------------------------------------------- #
def test_regression(skip: bool) -> None:
    console.rule("[bold]Regression: step8b_verify.py (-> step8 -> step7 -> step6 -> step5 -> step4) + Step 4B execution checks")
    results = PROJECT_ROOT / "data" / "results"
    before = {p.name: p.read_bytes() for p in results.iterdir() if p.is_file()}
    if skip:
        console.print("[yellow]suite chain skipped (--skip-regression)[/]")
    else:
        t0 = time.time()
        p = subprocess.run([sys.executable, "step8b_verify.py"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=PROJECT_ROOT, timeout=5400, env={**os.environ, "PYTHONIOENCODING": "utf-8", "COLUMNS": "200"})
        out = p.stdout
        console.print(out, markup=False, highlight=False)
        if p.stderr.strip():
            console.print(p.stderr[-3000:], markup=False, highlight=False)
        for step, n in (("Step 8B", 37), ("Step 8", 33), ("Step 7", 42), ("Step 6", 47), ("Step 5", 92), ("Step 4", 77)):
            lines = [ln.strip("─ ") for ln in out.splitlines() if f"{step} verification:" in ln and "──" in ln]
            check("regression", f"{step} verification PASS - {n} checks", bool(lines) and f"PASS - {n} checks" in lines[-1],
                  lines[-1] if lines else "verdict line not found")
        check("regression", "step8b_verify.py exit code 0", p.returncode == 0, f"{time.time() - t0:.0f}s")
    import step4b_research as s4b
    s4b.CHECKS.clear()
    s4b.test_engine_paths()
    eng = [c for c in s4b.CHECKS if c[0] == "engine"]
    check("regression", "step4b_research.py execution-path checks (synthetic 1m paths) all pass", len(eng) == 7 and all(c[2] for c in eng),
          f"{sum(c[2] for c in eng)}/{len(eng)}")
    after = {p.name: p.read_bytes() for p in results.iterdir() if p.is_file()}
    report = (results / "STEP4B_REPORT.md").read_text(encoding="utf-8")
    check("regression", "research artifacts untouched (data/results byte-identical); STEP4B_REPORT.md verdict still NO CANDIDATE",
          before == after and "NO CANDIDATE" in report.upper(), f"{len(after)} files")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-regression", action="store_true")
    args = ap.parse_args()
    console.print(f"test process: MODE={settings.MODE!r} LIVE_TRADING_ENABLED={settings.LIVE_TRADING_ENABLED} live key in settings="
                  f"{bool(settings.LIVE_API_KEY)} (testenv) · real bot lock held: {lb.InstanceLock.is_held(lb.LOCK_PATH)} (untouched)")
    if T.exists():
        shutil.rmtree(T)
    T.mkdir(parents=True)
    for section, fn in (("F03", test_f03_exit_recovery), ("F04", test_f04_partial_fills), ("F08", test_f08_unknown_outcome),
                        ("F09", test_f09_margin_mode), ("#1", test_src1_atomic), ("#3", test_src3_external), ("#4", test_src4_fees),
                        ("F02", test_f02_engine_gate), ("F05", test_f05_flatten), ("F06", test_f06_protection),
                        ("F07", test_f07_account_policy), ("F10", test_f10_subscriptions), ("#2", test_src2_private_stream),
                        ("#5", test_src5_day_anchor), ("#6", test_src6_operations), ("F11", test_f11_zero_trades),
                        ("F01", test_api), ("docs", test_docs)):
        guarded(section, fn)
    test_regression(args.skip_regression)
    console.rule("[bold]Summary")
    t = Table()
    for col in ("section", "check", "result", "detail"):
        t.add_column(col)
    for sec, name, ok, detail in CHECKS:
        t.add_row(sec, name, "[green]PASS" if ok else "[red]FAIL", detail[:100])
    console.print(t)
    failed = [x for x in CHECKS if not x[2]]
    console.rule(f"Step 9 verification: {'[green]PASS' if not failed else f'[red]FAIL ({len(failed)} checks)'} - {len(CHECKS)} checks")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
