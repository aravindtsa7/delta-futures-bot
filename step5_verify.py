"""Step 5 verification: REST adapter, SQLite journal, lot sizer, paper/testnet executor.

  1. Signing & wire fixtures (official Delta test vector, signed bytes == sent bytes, client_order_id,
     tick snapping + returned-price assertion, GET retry / mutation no-retry, 429 + budget, live gate)
  2. Live public market data + ₹50,000 sizing + frozen-strategy signal status (api.india.delta.exchange)
  3. Paper lifecycle in data/test_bot_state.db (+ exact parity with backtester.py on the same 1m path)
  4. Mocked testnet adapter + fault injection (httpx.MockTransport fake exchange, signature-checked)
  5. Re-run step4_verify.py

Usage:
    python step5_verify.py              # everything
    python step5_verify.py --no-sync    # skip the candle sync before signal evaluation
    python step5_verify.py --skip-step4 # do not re-run step4_verify.py
"""

from __future__ import annotations

import testenv  # noqa: F401,E402  (must run before config is imported: research mode, no live keys)

import argparse
import json
import math
import subprocess
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

import data_manager as dm
import execution_engine as ee
from backtester import Backtester, Leg, ProductSpec, audit_result
from config import PROJECT_ROOT, settings
from exchange_client import (
    AmbiguousOutcomeError,
    DeltaRestClientAdapter,
    LiveTradingDisabledError,
    MissingCredentialsError,
    OrderPriceMismatchError,
    OrderValidationError,
    RateLimitBudget,
    RateLimitedError,
    encode_query,
    endpoint_weight,
    sign_request,
    snap_to_tick,
)

console = Console(width=210)
CHECKS: list[tuple[str, str, bool, str]] = []
RATE = settings.FIXED_USD_INR_RATE

DOC_SECRET = "7b6f39dcf660ec1c7c664f612c60410a2bd0c258416b498bf0311f94228f"
DOC_SIGNATURE = "ad767fead0bdbe91ba1e4feb142079245fecd66aa5e47a70b40ba1a4c9b4e3db"
V2_SIGNATURE = "4e38dda3e6477092f360ba70399266d8145630b22bcc34c0ec7f804d5746877a"  # same vector on /v2/orders (openssl)
FAKE_KEY, FAKE_SECRET = "fake-testnet-key", "fake-testnet-secret"
TEST_DB = settings.data_path / "test_bot_state.db"
TEST_DB_TESTNET = settings.data_path / "test_bot_state_testnet.db"


def check(section: str, name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((section, name, bool(ok), detail))
    mark = "[green]PASS[/]" if ok else "[red]FAIL[/]"
    console.print(f"  {mark} {name}" + (f"  [dim]{detail}[/]" if detail else ""))
    return bool(ok)


def raises(exc_type: type[BaseException], fn, *a, **kw) -> tuple[bool, str]:
    try:
        fn(*a, **kw)
    except exc_type as exc:
        return True, f"{type(exc).__name__}: {str(exc)[:110]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"wrong exception {type(exc).__name__}: {exc}"
    return False, "no exception"


def fresh_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", ".lock"):
        p = path.with_name(path.name + suffix)
        if p.exists():
            p.unlink()


def us(ts: str | pd.Timestamp) -> int:
    return int(pd.Timestamp(ts).value // 1000)


class FakeClock:
    def __init__(self, t_us: int):
        self.t = t_us

    def __call__(self) -> int:
        return self.t

    def seconds(self) -> float:
        return self.t / 1e6


# --------------------------------------------------------------------------- #
# Fake Delta exchange (MockTransport handler). Verifies every signature on the received bytes.
# --------------------------------------------------------------------------- #
class FakeDelta:
    def __init__(self, clock: FakeClock, user_id: int = 1_234_567, prices: dict[int, float] | None = None):
        self.clock = clock
        self.user_id = user_id
        self.meta = {s: dm.load_metadata(s) for s in ("BTCUSD", "ETHUSD")}
        self.by_id = {int(m["id"]): m for m in self.meta.values()}
        self.prices = prices or {27: 81_600.0, 3136: 3_000.0}
        self.orders: dict[int, dict[str, Any]] = {}
        self.fills: list[dict[str, Any]] = []
        self.positions: dict[int, dict[str, float]] = {}
        self.leverage: dict[int, str] = {}
        self.calls: list[dict[str, Any]] = []
        self.sig_checked = 0
        self.sig_failures = 0
        self.header_failures = 0
        self.next_order_id, self.next_leg_id, self.next_fill_id = 8001, 9001, 7001
        self.timeout_after_processing_next_post = False
        self.timeout_before_processing_next_post = False
        self.disrupted: set[int] = set()
        self.wallet_usd = 50_000.0 / RATE

    # -- helpers --------------------------------------------------------------- #
    @staticmethod
    def ok(result: Any, meta: dict | None = None, status: int = 200) -> httpx.Response:
        body = {"success": True, "result": result}
        if meta is not None:
            body["meta"] = meta
        return httpx.Response(status, json=body)

    @staticmethod
    def err(status: int, code: str) -> httpx.Response:
        return httpx.Response(status, json={"success": False, "error": {"code": code}})

    def cv(self, pid: int) -> float:
        return float(self.by_id[pid]["contract_value"])

    def _fill(self, o: dict[str, Any], price: float) -> None:
        pid, size = o["product_id"], o["size"]
        d = 1 if o["side"] == "buy" else -1
        pos = self.positions.setdefault(pid, {"size": 0, "entry_price": 0.0})
        if o.get("reduce_only"):
            size = min(size, abs(int(pos["size"])))
        new = pos["size"] + d * size
        if pos["size"] == 0 or (pos["size"] > 0) == (d > 0):
            tot = abs(pos["size"]) + size
            pos["entry_price"] = (abs(pos["size"]) * pos["entry_price"] + size * price) / tot
        pos["size"] = new
        if new == 0:
            pos["entry_price"] = 0.0
        o.update(unfilled_size=0, state="closed", average_fill_price=str(price))
        self.fills.append({"id": self.next_fill_id, "order_id": o["id"], "product_id": pid, "side": o["side"],
                           "size": size, "price": str(price), "role": "taker", "fill_type": "normal",
                           "commission": str(size * self.cv(pid) * price * settings.TAKER_FEE * (1 + settings.GST_ON_FEES)),
                           "created_at_us": self.clock()})
        self.next_fill_id += 1

    def _new_order(self, b: dict[str, Any], **extra: Any) -> dict[str, Any]:
        oid = extra.pop("id", None) or self.next_order_id
        if oid == self.next_order_id:
            self.next_order_id += 1
        pid = int(b["product_id"])
        o = {"id": oid, "product_id": pid, "product_symbol": self.by_id[pid]["symbol"], "size": int(b["size"]),
             "unfilled_size": int(b["size"]), "side": b["side"], "order_type": b.get("order_type", "market_order"),
             "limit_price": b.get("limit_price"), "stop_price": b.get("stop_price"), "stop_order_type": b.get("stop_order_type"),
             "reduce_only": bool(b.get("reduce_only", False)), "client_order_id": b.get("client_order_id"),
             "state": "open", "average_fill_price": None, "bracket_order": False,
             "bracket_stop_loss_price": b.get("bracket_stop_loss_price"),
             "bracket_take_profit_price": b.get("bracket_take_profit_price"), "created_at_us": self.clock(), **extra}
        self.orders[oid] = o
        return o

    def _legs(self, pid: int, side: str, size: int, sl: str | None, tp: str | None) -> None:
        exit_side = "sell" if side == "buy" else "buy"
        for kind, px in (("stop_loss_order", sl), ("take_profit_order", tp)):
            if px is None:
                continue
            lid = self.next_leg_id
            self.next_leg_id += 1
            self._new_order({"product_id": pid, "size": size, "side": exit_side, "order_type": "market_order",
                             "stop_price": px, "stop_order_type": kind, "reduce_only": True},
                            id=lid, state="pending", bracket_order=True)

    def trigger_leg(self, leg_id: int, price: float) -> None:
        """Exchange-side stop/TP trigger: the leg fills at ``price``."""
        self._fill(self.orders[leg_id], price)

    # -- router ----------------------------------------------------------------- #
    def handler(self, request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path.decode()
        path, _, q = raw.partition("?")
        query = "?" + q if q else ""
        body_str = request.content.decode() if request.content else ""
        if request.headers.get("user-agent") != settings.USER_AGENT or request.headers.get("content-type") != "application/json":
            self.header_failures += 1
        if "api-key" in request.headers:
            if not {"timestamp", "signature"} <= set(request.headers.keys()):
                self.header_failures += 1
            self.sig_checked += 1
            want = sign_request(FAKE_SECRET, request.method, request.headers["timestamp"], path, query, body_str)
            if want != request.headers.get("signature") or request.headers["api-key"] != FAKE_KEY:
                self.sig_failures += 1
                return self.err(401, "invalid_signature")
        params = dict(parse_qsl(q))
        b = json.loads(body_str) if body_str else {}
        self.calls.append({"method": request.method, "path": path, "query": query, "params": params, "body": b,
                           "body_str": body_str, "auth": "api-key" in request.headers})
        m = request.method
        if m == "GET" and path.startswith("/v2/products/") and path.count("/") == 3:
            sym = path.rsplit("/", 1)[1]
            return self.ok(self.meta[sym]) if sym in self.meta else self.err(404, "not_found")
        if path == "/v2/users/trading_preferences":
            return self.ok({"user_id": self.user_id, "vip_level": 0})
        if path == "/v2/wallet/balances":
            return self.ok([{"asset_symbol": "USD", "asset_id": 14, "available_balance": f"{self.wallet_usd:.8f}",
                             "balance": f"{self.wallet_usd:.8f}"}], meta={"net_equity": f"{self.wallet_usd:.8f}"})
        if path == "/v2/users/margin_mode" and m == "PUT":
            return self.ok({"id": str(self.user_id), "margin_mode": b["margin_mode"]})
        if path.endswith("/orders/leverage"):
            pid = int(path.split("/")[3])
            if m == "POST":
                self.leverage[pid] = b["leverage"]
            return self.ok({"leverage": self.leverage.get(pid, "200"), "order_margin": "0", "product_id": pid})
        if path == "/v2/orders" and m == "POST":
            if self.timeout_before_processing_next_post:
                self.timeout_before_processing_next_post = False
                raise httpx.ConnectTimeout("simulated connect timeout", request=request)
            coid = b.get("client_order_id")
            if coid and any(o["client_order_id"] == coid and o["state"] in ("open", "pending") for o in self.orders.values()):
                return self.err(400, "duplicate_client_order_id")
            o = self._new_order(b)
            if o["order_type"] == "market_order" and not o["stop_order_type"]:
                self._fill(o, self.prices[o["product_id"]])
                if b.get("bracket_stop_loss_price") or b.get("bracket_take_profit_price"):
                    self._legs(o["product_id"], o["side"], o["size"], b.get("bracket_stop_loss_price"),
                               b.get("bracket_take_profit_price"))
            if self.timeout_after_processing_next_post:
                self.timeout_after_processing_next_post = False
                raise httpx.ReadTimeout("simulated read timeout (order WAS processed)", request=request)
            return self.ok(o)
        if path == "/v2/orders" and m == "PUT":
            o = self.orders.get(int(b["id"]))
            if o is None or o["state"] not in ("open", "pending"):
                return self.err(400, "order_not_open")
            for k in ("stop_price", "limit_price"):
                if k in b:
                    o[k] = b[k]
            o["size"] = int(b["size"])
            return self.ok(o)
        if path == "/v2/orders" and m == "DELETE":
            o = self.orders.get(int(b["id"])) if "id" in b else next(
                (x for x in self.orders.values() if x["client_order_id"] == b.get("client_order_id")), None)
            if o is None:
                return self.err(404, "not_found")
            o["state"] = "cancelled"
            return self.ok(o)
        if path == "/v2/orders/bracket" and m == "POST":
            pid = int(b["product_id"])
            pos = self.positions.get(pid, {"size": 0})
            side = "buy" if pos["size"] > 0 else "sell"
            sl = (b.get("stop_loss_order") or {}).get("stop_price")
            tp = (b.get("take_profit_order") or {}).get("stop_price")
            self._legs(pid, side, abs(int(pos["size"])), sl, tp)
            return self.ok({"product_id": pid})
        if path == "/v2/orders/bracket" and m == "PUT":
            return self.ok({"id": b["id"]})
        if path == "/v2/orders" and m == "GET":
            states = params.get("states", "open,pending").split(",")
            rows = [o for o in self.orders.values() if o["state"] in states
                    and ("product_ids" not in params or str(o["product_id"]) in params["product_ids"].split(","))]
            return self.ok(rows, meta={"after": None})
        if path.startswith("/v2/orders/client_order_id/"):
            coid = path.rsplit("/", 1)[1]
            o = next((x for x in self.orders.values() if x["client_order_id"] == coid), None)
            return self.ok(o) if o else self.err(404, "not_found")
        if path == "/v2/orders/history":
            lo, hi = int(params["start_time"]), int(params["end_time"])
            rows = [o for o in self.orders.values() if o["state"] in ("closed", "cancelled") and lo <= o["created_at_us"] <= hi]
            return self.ok(rows, meta={"after": None})
        if path.startswith("/v2/orders/"):
            o = self.orders.get(int(path.rsplit("/", 1)[1]))
            return self.ok(o) if o else self.err(404, "not_found")
        if path == "/v2/fills":
            lo, hi = int(params["start_time"]), int(params["end_time"])
            rows = [f for f in self.fills if lo <= f["created_at_us"] <= hi
                    and str(f["product_id"]) in params.get("product_ids", str(f["product_id"]))]
            half = len(rows) // 2  # two pages, to exercise meta.after pagination
            if "after" not in params and half:
                return self.ok(rows[:half], meta={"after": "cursor-2"})
            return self.ok(rows[half:] if "after" in params else rows, meta={"after": None})
        if path == "/v2/positions" and m == "GET":
            pid = int(params["product_id"])
            pos = self.positions.get(pid, {"size": 0, "entry_price": 0.0})
            return self.ok({"product_id": pid, "product_symbol": self.by_id[pid]["symbol"], "size": int(pos["size"]),
                            "entry_price": str(pos["entry_price"]) if pos["size"] else None})
        if path == "/v2/positions/margined":
            return self.ok([{"product_id": pid, "product_symbol": self.by_id[pid]["symbol"], "size": int(p["size"]),
                             "entry_price": str(p["entry_price"]), "margin_mode": "isolated", "realized_funding": "0"}
                            for pid, p in self.positions.items() if p["size"]])
        if path == "/v2/positions/close_all":
            skipped = []
            for pid, p in self.positions.items():
                if not p["size"]:
                    continue
                if pid in self.disrupted:
                    skipped.append({"product_id": pid, "product_symbol": self.by_id[pid]["symbol"],
                                    "reason": "market_disrupted_cancel_only_mode"})
                    continue
                o = self._new_order({"product_id": pid, "size": abs(int(p["size"])), "side": "sell" if p["size"] > 0 else "buy",
                                     "reduce_only": True})
                self._fill(o, self.prices[pid])
            return self.ok({"skipped_products": skipped})
        return self.err(404, f"no_route {m} {path}")

    def count(self, method: str, path: str) -> int:
        return sum(1 for c in self.calls if c["method"] == method and c["path"] == path)


def fake_client(fake: FakeDelta, clock: FakeClock, sleeps: list[float] | None = None) -> DeltaRestClientAdapter:
    sl = sleeps if sleeps is not None else []
    return DeltaRestClientAdapter("testnet", FAKE_KEY, FAKE_SECRET, transport=httpx.MockTransport(fake.handler),
                                  now=clock.seconds, sleep=sl.append, budget=RateLimitBudget(clock=clock.seconds))


# --------------------------------------------------------------------------- #
# 1. Signing & wire fixtures
# --------------------------------------------------------------------------- #
def test_signing() -> None:
    console.rule("[bold]1. Signing & wire fixtures")
    q = encode_query({"product_id": 1, "state": "open"})
    check("1 signing", "query string encodes with leading '?'", q == "?product_id=1&state=open", q)
    s_doc = sign_request(DOC_SECRET, "GET", "1542110948", "/orders", q, "")
    check("1 signing", "official Delta doc vector reproduced (HMAC-SHA256)", s_doc == DOC_SIGNATURE,
          f"{s_doc[:16]}… (doc prints the message with /v2, but its signature was computed over /orders)")
    s_v2 = sign_request(DOC_SECRET, "GET", "1542110948", "/v2/orders", q, "")
    check("1 signing", "same vector on the real /v2 path (openssl cross-check)", s_v2 == V2_SIGNATURE, f"{s_v2[:16]}…")
    body = '{"product_id":27,"size":1,"side":"buy","order_type":"market_order"}'
    s_body = sign_request(DOC_SECRET, "POST", "1542110948", "/v2/orders", "", body)
    check("1 signing", "body bytes are part of the signature", s_body != sign_request(DOC_SECRET, "POST", "1542110948", "/v2/orders", "", ""))

    clock = FakeClock(us("2026-10-20 06:00:00"))
    fake = FakeDelta(clock)
    sleeps: list[float] = []
    c = fake_client(fake, clock, sleeps)
    c.get_product("BTCUSD")
    c.get_product("ETHUSD")
    c.get_active_orders(27)
    c.get_fills(27, us("2026-10-19"), us("2026-10-20"))
    call = fake.calls[-1]
    check("1 signing", "signature verified by fake server on the exact wire bytes", fake.sig_checked >= 2 and fake.sig_failures == 0,
          f"{fake.sig_checked} signed requests, {fake.sig_failures} failures")
    check("1 signing", "headers: api-key/timestamp/signature + User-Agent/Content-Type on every request",
          fake.header_failures == 0 and len(fake.calls) >= 4, f"{len(fake.calls)} requests, {fake.header_failures} header failures")
    check("1 signing", "GET /v2/fills always carries explicit µs start_time/end_time",
          all("start_time" in x["params"] and "end_time" in x["params"] for x in fake.calls if x["path"] == "/v2/fills"),
          call["query"])

    # client_order_id <= 32
    n_before = len(fake.calls)
    ok, det = raises(OrderValidationError, c.place_order, product_id=27, side="buy", size=1, client_order_id="x" * 33)
    check("1 signing", "client_order_id of 33 chars rejected before sending", ok and len(fake.calls) == n_before, det)
    o = c.place_order(product_id=27, side="buy", size=1, client_order_id="y" * 32)
    check("1 signing", "client_order_id of exactly 32 chars accepted", o["client_order_id"] == "y" * 32)

    # tick snapping
    cases = [("2500.03", "0.05", "2500.05"), ("2500.024", "0.05", "2500.00"), ("2500.025", "0.05", "2500.05"),
             ("81607.26", "0.5", "81607.5"), ("81607.24", "0.5", "81607.0"), ("81607.25", "0.5", "81607.5")]
    res = [(str(snap_to_tick(p, t)), want) for p, t, want in cases]
    check("1 signing", "Decimal tick snapping (half-up) ETH 0.05 / BTC 0.5", all(a == b for a, b in res),
          ", ".join(f"{p}->{a}" for (p, _, _), (a, _) in zip(cases, res)))
    o = c.place_order(product_id=3136, side="buy", size=1, order_type="limit_order", limit_price=2500.03, client_order_id="snap-1")
    sent = fake.calls[-1]["body"]
    check("1 signing", "limit_price snapped before dispatch and echoed price asserted", sent["limit_price"] == "2500.05"
          and o["limit_price"] == "2500.05", f"sent {sent['limit_price']!r}")
    o = c.place_order(product_id=27, side="sell", size=1, order_type="limit_order", limit_price=90000.2,
                      stop_price=89999.8, stop_order_type="stop_loss_order", client_order_id="snap-2")
    check("1 signing", "stop_price snapped (BTC 89999.8 -> 90000.0)", fake.calls[-1]["body"]["stop_price"] == "90000.0")

    orig = fake.handler

    def tamper(request: httpx.Request) -> httpx.Response:
        r = orig(request)
        if request.method == "POST" and request.url.path == "/v2/orders":
            body = json.loads(r.content)
            body["result"]["limit_price"] = "2500.10"
            return httpx.Response(r.status_code, json=body)
        return r
    ct = DeltaRestClientAdapter("testnet", FAKE_KEY, FAKE_SECRET, transport=httpx.MockTransport(tamper),
                                now=clock.seconds, sleep=lambda s: None, budget=RateLimitBudget(clock=clock.seconds))
    ct.get_product("ETHUSD")
    ok, det = raises(OrderPriceMismatchError, ct.place_order, product_id=3136, side="buy", size=1,
                     order_type="limit_order", limit_price=2500.03, client_order_id="snap-3")
    check("1 signing", "returned limit_price != intended tick price -> OrderPriceMismatchError", ok, det)

    # GET retry with fresh signature; mutation never retried
    seq = iter([503, 200])
    stamps: list[str] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        stamps.append(request.headers.get("timestamp", ""))
        clock.t += 2_000_000
        code = next(seq)
        return fake.handler(request) if code == 200 else httpx.Response(503, json={"success": False})
    cf = DeltaRestClientAdapter("testnet", FAKE_KEY, FAKE_SECRET, transport=httpx.MockTransport(flaky), now=clock.seconds,
                                sleep=lambda s: None, budget=RateLimitBudget(clock=clock.seconds))
    before = fake.sig_failures
    cf.get_wallet_balances()
    check("1 signing", "GET retried after 503 with a fresh timestamp + valid signature",
          len(stamps) == 2 and stamps[0] != stamps[1] and fake.sig_failures == before, f"timestamps {stamps}")

    hits: list[str] = []

    def timeout(request: httpx.Request) -> httpx.Response:
        hits.append(request.method)
        raise httpx.ReadTimeout("simulated", request=request)
    cto = DeltaRestClientAdapter("testnet", FAKE_KEY, FAKE_SECRET, transport=httpx.MockTransport(timeout), now=clock.seconds,
                                 sleep=lambda s: None, budget=RateLimitBudget(clock=clock.seconds))
    cto.register_product(c.product(27))
    ok, det = raises(AmbiguousOutcomeError, cto.place_order, product_id=27, side="buy", size=1, client_order_id="to-1")
    check("1 signing", "POST timeout -> AmbiguousOutcomeError, sent exactly once", ok and hits == ["POST"], det)
    hits.clear()
    ok, det = raises(AmbiguousOutcomeError, cto.cancel_order, 27, order_id=1)
    check("1 signing", "DELETE timeout -> AmbiguousOutcomeError, sent exactly once", ok and hits == ["DELETE"], det)
    hits.clear()
    ok, _ = raises(Exception, cto.get_wallet_balances)
    check("1 signing", "GET timeout retried with bounded backoff (4 attempts)", ok and hits == ["GET"] * 4, f"{len(hits)} attempts")
    c5 = DeltaRestClientAdapter("testnet", FAKE_KEY, FAKE_SECRET, now=clock.seconds, sleep=lambda s: None,
                                budget=RateLimitBudget(clock=clock.seconds),
                                transport=httpx.MockTransport(lambda r: (hits.append(r.method), httpx.Response(502))[1]))
    c5.register_product(c.product(27))
    hits.clear()
    ok, det = raises(AmbiguousOutcomeError, c5.place_order, product_id=27, side="buy", size=1, client_order_id="to-2")
    check("1 signing", "POST answered 502 -> AmbiguousOutcomeError (no retry)", ok and hits == ["POST"], det)

    # 429 handling + budget
    budget = RateLimitBudget(clock=clock.seconds)
    waits: list[float] = []

    def sleeper(s: float) -> None:
        waits.append(s)
        clock.t += int(s * 1e6)
    n429 = iter([True, False])

    def limited(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and next(n429, False):
            return httpx.Response(429, headers={"X-RATE-LIMIT-RESET": "1500"}, json={"success": False})
        if request.method == "POST":
            return httpx.Response(429, headers={"X-RATE-LIMIT-RESET": "800"}, json={"success": False})
        return fake.handler(request)
    cl = DeltaRestClientAdapter("testnet", FAKE_KEY, FAKE_SECRET, transport=httpx.MockTransport(limited), now=clock.seconds,
                                sleep=sleeper, budget=budget)
    cl.register_product(c.product(27))
    cl.get_wallet_balances()
    check("1 signing", "GET 429 -> waits X-RATE-LIMIT-RESET (ms) then succeeds", waits and abs(waits[0] - 1.5) < 1e-6,
          f"waited {waits}")
    ok, det = raises(RateLimitedError, cl.place_order, product_id=27, side="buy", size=1, client_order_id="rl-1")
    check("1 signing", "POST 429 -> RateLimitedError, not retried", ok, det)
    small = RateLimitBudget(capacity=10, window_s=300, clock=clock.seconds)
    w1, w2, w3 = small.wait_time(5), small.wait_time(5), small.wait_time(3)
    check("1 signing", "shared 5-min weight budget blocks when exhausted", w1 == 0 and w2 == 0 and 299 < w3 <= 300, f"wait {w3:.1f}s")
    weights = {("POST", "/v2/orders"): 5, ("GET", "/v2/fills"): 10, ("GET", "/v2/tickers/BTCUSD"): 3,
               ("GET", "/v2/orders/history"): 10, ("GET", "/v2/users/trading_preferences"): 1}
    check("1 signing", "documented endpoint weights", all(endpoint_weight(m, p) == w for (m, p), w in weights.items()))

    # environment isolation + live gate
    ok, det = raises(LiveTradingDisabledError, DeltaRestClientAdapter, "live", "k", "s")
    check("1 signing", "live adapter refused (MODE='research', LIVE_TRADING_ENABLED=False)", ok, det)
    ok, det = raises(LiveTradingDisabledError, DeltaRestClientAdapter.from_settings, "live")
    check("1 signing", "from_settings('live') also refused", ok)
    paper = DeltaRestClientAdapter("paper")
    ok, det = raises(MissingCredentialsError, paper.get_wallet_balances)
    check("1 signing", "paper adapter refuses authenticated calls", ok, det)
    ok, _ = raises(Exception, DeltaRestClientAdapter, "paper", "k", "s")
    check("1 signing", "paper adapter refuses credentials", ok)
    tn = DeltaRestClientAdapter("testnet")
    check("1 signing", "testnet adapter -> cdn-ind.testnet.deltaex.org, paper -> api.india.delta.exchange",
          tn.base_url == "https://cdn-ind.testnet.deltaex.org" and paper.base_url == "https://api.india.delta.exchange")
    ok, det = raises(MissingCredentialsError, tn.get_wallet_balances)
    check("1 signing", "testnet adapter without keys refuses auth calls (no request sent)", ok, det)


# --------------------------------------------------------------------------- #
# 2. Live public market data, sizing, signals
# --------------------------------------------------------------------------- #
def test_live_public(sync: bool) -> dict[str, Any]:
    console.rule("[bold]2. Live public market (api.india.delta.exchange) + ₹50,000 sizing + signals")
    pub = DeltaRestClientAdapter("paper")
    t = Table(title="Live tickers / L2 orderbook")
    for col in ("symbol", "id", "state", "status", "mark", "spot", "best bid", "best ask", "spread bps",
                "band lower", "band upper", "L2 bids (px x size)", "L2 asks (px x size)"):
        t.add_column(col, justify="right")
    tickers, infos = {}, {}
    for sym in ("BTCUSD", "ETHUSD"):
        info = pub.get_product(sym)
        tk = pub.get_ticker(sym)
        ob = pub.get_l2_orderbook(sym, depth=5)
        infos[sym], tickers[sym] = info, tk
        spread = (tk["best_ask"] - tk["best_bid"]) / tk["mark_price"] * 1e4
        t.add_row(sym, str(info.id), info.state, info.trading_status, f"{tk['mark_price']:,.2f}", f"{tk['spot_price']:,.2f}",
                  f"{tk['best_bid']:,.2f}", f"{tk['best_ask']:,.2f}", f"{spread:.2f}", f"{tk['band_lower']:,.2f}",
                  f"{tk['band_upper']:,.2f}", " ".join(f"{p:,.1f}x{s}" for p, s in ob["bids"][:3]),
                  " ".join(f"{p:,.1f}x{s}" for p, s in ob["asks"][:3]))
        check("2 live", f"{sym} product live+operational with contract_value/tick_size/price_band", info.tradable
              and info.contract_value > 0 and info.tick_size > 0 and info.price_band_pct is not None,
              f"cv {info.contract_value} tick {info.tick_size} band {info.price_band_pct}%")
        check("2 live", f"{sym} ticker quotes sane (bid < ask, band brackets mark)",
              tk["best_bid"] < tk["best_ask"] and tk["band_lower"] < tk["mark_price"] < tk["band_upper"])
        check("2 live", f"{sym} L2 book depth 5 (bids desc, asks asc, best bid < best ask)",
              len(ob["bids"]) >= 1 and len(ob["asks"]) >= 1 and ob["bids"][0][0] < ob["asks"][0][0]
              and all(a[0] > b[0] for a, b in zip(ob["bids"], ob["bids"][1:]))
              and all(a[0] < b[0] for a, b in zip(ob["asks"], ob["asks"][1:])))
        meta = ProductSpec.from_metadata(sym)
        check("2 live", f"{sym} live product matches saved metadata", meta.product_id == info.id
              and Decimal(str(meta.tick_size)) == info.tick_size and Decimal(str(meta.contract_value)) == info.contract_value)
    console.print(t)

    t0 = time.perf_counter()
    sig = ee.evaluate_latest_signals(sync=sync)
    console.print(f"  evaluate_latest_signals(sync={sync}) took {time.perf_counter() - t0:.1f}s; "
                  f"history pinned from Step 4B warm-up start {ee.step4b_history_start()}")
    st = Table(title="Live signal / indicator status on the latest CLOSED 1h bar (frozen Step 4B variants)")
    for col in ("symbol", "variant", "1h bar (UTC)", "closes", "close", "BB upper", "BB lower", "4h EMA50", "4h EMA200",
                "4h regime", "ATR14", "ATR/close", "gate thr", "cost gate", "raw long", "raw short", "SIGNAL"):
        st.add_column(col, justify="right")
    for sym, s in sig.items():
        regime = "long-ok" if s["allow_long_4h"] else "short-ok" if s["allow_short_4h"] else "neutral"
        if not ee.FROZEN_PROFILES[sym].trend_filter_4h:
            regime += " (unused)"
        st.add_row(sym, s["variant"], s["bar_open"].strftime("%m-%d %H:%M"), s["bar_close"].strftime("%H:%M"),
                   f"{s['close']:,.2f}", f"{s['bb_upper']:,.2f}", f"{s['bb_lower']:,.2f}", f"{s['ema50_4h']:,.2f}",
                   f"{s['ema200_4h']:,.2f}", regime, f"{s['atr14']:,.2f}", f"{s['atr_ratio'] * 100:.3f}%",
                   f"{s['gate_threshold'] * 100:.3f}%", "PASS" if s["cost_gate_pass"] else "block",
                   str(s["cond_long"]), str(s["cond_short"]), s["signal_side"] or "none")
        check("2 live", f"{sym} latest 1h bar is closed and fresh (closes <= last 1m candle + 1m, staleness < 125 min)",
              s["bar_close"] <= s["last_1m_open"] + pd.Timedelta(minutes=1) and s["staleness_min"] < 125,
              f"bar closes {s['bar_close']:%H:%M}, last 1m {s['last_1m_open']:%H:%M}, {s['staleness_min']:.0f} min old")
        check("2 live", f"{sym} 4h regime uses a CLOSED 4h bar (src_4h_open + 4h <= 1h bar close)",
              s["src_4h_open"] + pd.Timedelta(hours=4) <= s["bar_close"], f"4h bar {s['src_4h_open']:%m-%d %H:%M}")
    console.print(st)

    sz = Table(title="Live position sizing — ₹50,000 equity, 10x isolated, 1% risk, ATR(14) of latest closed 1h bar, 2 bps buffer")
    for col in ("symbol", "side", "price", "ATR14", "SL dist", "contracts", "risk lots", "margin cap", "notional $",
                "notional ₹", "margin $", "margin ₹", "risk $ (≤ budget)", "SL", "TP", "trail", "note"):
        sz.add_column(col, justify="right")
    sizing = {}
    for sym, s in sig.items():
        spec = ProductSpec.from_metadata(sym)
        for side in ("LONG", "SHORT"):
            px = tickers[sym]["best_ask"] if side == "LONG" else tickers[sym]["best_bid"]
            lots = ee.calculate_order_lots(spec, px, s["atr14"], side)
            sizing[(sym, side)] = lots
            sz.add_row(sym, side, f"{px:,.2f}", f"{s['atr14']:,.2f}", f"{lots.sl_distance / px * 100:.2f}%", str(lots.contracts),
                       str(lots.risk_contracts), str(lots.margin_cap_contracts), f"{lots.notional_usd:,.2f}",
                       f"{lots.notional_inr:,.0f}", f"{lots.margin_usd:,.2f}", f"{lots.margin_inr:,.0f}",
                       f"{lots.risk_usd:.2f} / {lots.risk_budget_usd:.2f}", f"{lots.sl_price:,.2f}", f"{lots.tp_price:,.2f}",
                       f"{lots.trail_amount:,.2f}", lots.reason)
            d = 1 if side == "LONG" else -1
            check("2 live", f"{sym} {side}: risk ≤ 1% budget, margin ≤ 95% available, SL/TP on tick + correct side",
                  lots.risk_usd <= lots.risk_budget_usd + 1e-9 and lots.margin_usd <= 0.95 * 50_000 / RATE
                  and d * (lots.tp_price - px) > 0 > d * (lots.sl_price - px)
                  and snap_to_tick(lots.sl_price, spec.tick_size) == Decimal(str(lots.sl_price))
                  and abs(lots.tp_distance / lots.sl_distance - 2.5) < 1e-12,
                  f"{lots.contracts} lots, risk {lots.risk_usd / (50_000 / RATE) * 100:.3f}% of equity")
            bt_n = Backtester(ee.sizing_config(50_000)).size_position(50_000 / RATE, px, lots.sl_price, spec)[0]
            check("2 live", f"{sym} {side}: sizer == backtester.size_position (strict lots)", bt_n == lots.contracts, f"{bt_n}")
    console.print(sz)
    tiny = ee.calculate_order_lots(ProductSpec.from_metadata("BTCUSD"), tickers["BTCUSD"]["mark_price"],
                                   sig["BTCUSD"]["atr14"], "LONG", equity_inr=500.0)
    check("2 live", "₹500 equity: 1 BTC lot exceeds 1% risk -> 0 contracts (no min-lot floor)", tiny.contracts == 0
          and tiny.risk_contracts == 0, tiny.reason)
    return {"signals": sig, "tickers": tickers, "sizing": sizing}


# --------------------------------------------------------------------------- #
# 3. Paper lifecycle
# --------------------------------------------------------------------------- #
@dataclass
class PathBar:
    t: pd.Timestamp
    o: float
    h: float
    l: float
    c: float


def synthetic_path(p0: float, a: float, start: pd.Timestamp) -> list[PathBar]:
    """06:00-06:59 flat, 07:00 entry, grind up 2 ATR by 09:00, 09:00 spike to +3 ATR with a deep wick,
    09:01 drops through the trailed stop, then flat."""
    bars: list[PathBar] = []
    t = start
    for _ in range(60):
        bars.append(PathBar(t, p0, p0 + 0.05 * a, p0 - 0.05 * a, p0))
        t += pd.Timedelta(minutes=1)
    c = p0
    for k in range(120):
        nc = p0 + 2.0 * a * (k + 1) / 120
        bars.append(PathBar(t, c, max(c, nc) + 0.05 * a, min(c, nc) - 0.05 * a, nc))
        c = nc
        t += pd.Timedelta(minutes=1)
    bars.append(PathBar(t, c, p0 + 3.0 * a, p0 + 0.0 * a, p0 + 2.5 * a))  # 09:00 new high; low under the NEW stop
    t += pd.Timedelta(minutes=1)
    bars.append(PathBar(t, p0 + 1.0 * a, p0 + 1.2 * a, p0 + 0.3 * a, p0 + 0.4 * a))  # 09:01 hits the trailed stop
    t += pd.Timedelta(minutes=1)
    for _ in range(5):
        bars.append(PathBar(t, p0 + 0.4 * a, p0 + 0.45 * a, p0 + 0.35 * a, p0 + 0.4 * a))
        t += pd.Timedelta(minutes=1)
    return bars


def test_paper_lifecycle(live: dict[str, Any]) -> None:
    console.rule("[bold]3. Paper lifecycle (data/test_bot_state.db) + parity with backtester.py")
    sym = "ETHUSD"
    spec = ProductSpec.from_metadata(sym)
    p_live = round(live["tickers"][sym]["best_ask"] / spec.tick_size) * spec.tick_size
    p0 = round(p_live, 2)
    a_live = live["signals"][sym]["atr14"]
    a = round(min(max(a_live, 0.005 * p0), 0.015 * p0) / 0.02) * 0.02  # 2.5*ATR on the 0.05 tick grid
    console.print(f"  ETHUSD live best ask {p0:,.2f}; live ATR14 {a_live:.2f} -> synthetic path ATR {a:.2f}")
    start = pd.Timestamp("2026-10-20 06:00", tz="UTC")
    path = synthetic_path(p0, a, start)
    funding_rate = 0.0001  # 0.01% per 8h, long pays

    fresh_db(TEST_DB)
    clock = FakeClock(us("2026-10-20 07:00:00"))
    store = ee.StateStore(TEST_DB, clock=clock)
    check("3 paper", "SQLite journal in WAL mode", store.journal_mode.lower() == "wal", store.journal_mode)
    ok, det = raises(ee.StateStoreLockedError, ee.StateStore, TEST_DB)
    check("3 paper", "single writer: a second writer on the same DB is refused", ok, det)
    eng = ee.ExecutionEngine(store, "paper", clock=clock)
    cfgrow = store.config()
    check("3 paper", "bot_config_state: paper, ₹50,000, 10x, 1%, 5% daily limit, frozen profiles",
          cfgrow["mode"] == "paper" and cfgrow["capital_inr"] == 50_000 and cfgrow["leverage"] == 10 and cfgrow["risk_pct"] == 1.0
          and cfgrow["daily_loss_limit_pct"] == 5.0 and json.loads(cfgrow["profiles_json"])["BTCUSD"]["variant"] == "D_both"
          and json.loads(cfgrow["profiles_json"])["ETHUSD"]["variant"] == "C_costgate")

    sig_bar = us(start)
    res = eng.on_signal(sym, 1, sig_bar, a, path[60].o)
    it = store.intent(res["intent_id"])
    check("3 paper", "signal -> intent CREATED -> FILLED (paper fill)", store.intent_path(res["intent_id"]) == ["CREATED", "FILLED"]
          and it["state"] == "FILLED", " -> ".join(store.intent_path(res["intent_id"])))
    check("3 paper", "client_order_id <= 32 chars", len(it["client_order_id"]) <= 32, it["client_order_id"])
    pos = store.position("paper", sym)
    exp_fill = p0 * (1 + settings.SIZING_SLIPPAGE_BPS / 1e4)
    check("3 paper", "open position: entry = raw × (1 + 2 bps), SL 2×ATR, TP 5×ATR, trail 2.5×ATR on tick",
          abs(pos["entry_price"] - exp_fill) < 1e-9 and abs(pos["entry_price"] - pos["initial_sl"] - 2 * a) < spec.tick_size
          and abs(pos["tp_price"] - pos["entry_price"] - 5 * a) < spec.tick_size and abs(pos["trail_amount"] - 2.5 * a) < 1e-9,
          f"{pos['contracts']} lots @ {pos['entry_price']:.4f}  SL {pos['sl_price']}  TP {pos['tp_price']}  trail {pos['trail_amount']}")
    r2 = eng.on_signal(sym, 1, sig_bar + 3_600_000_000, a, p0)
    check("3 paper", "second signal while in position is skipped", r2["action"] == "skip", r2["reason"])

    trail_log: list[dict[str, Any]] = []
    exit_res = None
    prev_close = None
    for b in path[60:]:
        clock.t = us(b.t) + 60_000_000
        r = eng.on_bar(sym, us(b.t), b.o, b.h, b.l, b.c, funding_rate, prev_close)
        prev_close = b.c
        if r and r["action"] == "trail":
            trail_log.append({"bar": b.t, **r})
        if r and r["action"] == "exit":
            exit_res = (b, r)
            break
    check("3 paper", "trailing SL ratcheted up during the move", len(trail_log) > 5,
          f"{len(trail_log)} updates, last {trail_log[-1]['old_sl']} -> {trail_log[-1]['new_sl']}" if trail_log else "")
    spike = next((x for x in trail_log if x["bar"] == pd.Timestamp("2026-10-20 09:00", tz="UTC")), None)
    check("3 paper", "stop raised by the 09:00 spike is NOT applied to that candle (its low is below the new stop)",
          spike is not None and path[180].l < spike["new_sl"] and exit_res is not None and exit_res[0].t > path[180].t,
          f"09:00 low {path[180].l:.2f} < new SL {spike['new_sl'] if spike else '?'}; exit on {exit_res[0].t:%H:%M}" if exit_res else "")
    fe = store.q("SELECT * FROM funding_events WHERE mode='paper'")
    check("3 paper", "08:00 UTC funding settled once (long pays)", len(fe) == 1 and fe[0]["cashflow_usd"] < 0,
          f"{fe[0]['cashflow_usd']:.6f} USD" if fe else "none")
    tr = store.trades("paper")
    check("3 paper", "exit at trailing stop (TSL), fill = stop × (1 − 2 bps)", exit_res is not None and len(tr) == 1
          and tr[0]["exit_reason"] == "TSL" and abs(tr[0]["exit_price"] - tr[0]["final_sl"] * (1 - 2e-4)) < 1e-9,
          f"stop {tr[0]['final_sl']} -> fill {tr[0]['exit_price']:.4f}" if tr else "")
    check("3 paper", "position removed after exit", store.position("paper", sym) is None)

    t = tr[0]
    qty = t["contracts"] * t["contract_value"]
    gross = qty * (t["exit_price"] - t["entry_price"])
    fees = qty * (t["entry_price"] + t["exit_price"]) * settings.TAKER_FEE * (1 + settings.GST_ON_FEES)
    funding = fe[0]["cashflow_usd"]
    net = gross - fees + funding
    final_eq = eng.realized_equity_inr()
    check("3 paper", "ledger: gross, fees+GST, funding recomputed independently",
          abs(gross - t["gross_pnl_usd"]) < 1e-9 and abs(fees - t["fees_gst_usd"]) < 1e-9 and abs(funding - t["funding_usd"]) < 1e-12,
          f"gross ${gross:.6f} fees ${fees:.6f} funding ${funding:.6f}")
    check("3 paper", "ledger: net = gross − fees + funding; INR = USD × 85",
          abs(net - t["net_pnl_usd"]) < 1e-9 and abs(t["net_pnl_inr"] - t["net_pnl_usd"] * RATE) < 1e-9
          and abs(t["gross_pnl_inr"] - gross * RATE) < 1e-9 and abs(t["fees_gst_inr"] - fees * RATE) < 1e-9)
    check("3 paper", "audit: ₹50,000 + net_pnl == final equity == equity_after_inr",
          abs(50_000 + t["net_pnl_inr"] - final_eq) < 1e-9 and abs(t["equity_after_inr"] - final_eq) < 1e-9,
          f"50,000 + {t['net_pnl_inr']:.6f} = {final_eq:.6f}")

    # cooldown + persistence
    clock.t = us("2026-10-20 09:10")
    r3 = eng.on_signal(sym, 1, us("2026-10-20 09:00"), a, p0)
    check("3 paper", "signal on the bar of the exit -> skipped by 1-bar cooldown", r3["action"] == "skip" and r3["reason"] == "cooldown",
          r3["reason"])
    store.close()
    store2 = ee.StateStore(TEST_DB, clock=clock)
    eng2 = ee.ExecutionEngine(store2, "paper", clock=clock)
    check("3 paper", "state survives a restart (trade + equity reloaded from SQLite)",
          len(store2.trades("paper")) == 1 and abs(eng2.realized_equity_inr() - final_eq) < 1e-9)
    ok, det = raises(RuntimeError, store2.init_config, "testnet", ee.FROZEN_PROFILES)
    check("3 paper", "a paper DB refuses to run as another mode", ok, det)
    store2.close()

    # parity with backtester.py on the identical 1m path
    idx = pd.DatetimeIndex([b.t for b in path], name="time")
    df = pd.DataFrame({"open": [b.o for b in path], "high": [b.h for b in path], "low": [b.l for b in path],
                       "close": [b.c for b in path], "volume": 1.0}, index=idx)
    for k in ("open", "high", "low", "close"):
        df[f"mark_{k}"] = df[k]
    df["funding_rate"] = funding_rate
    df["funding_rate_pct"] = funding_rate * 100
    h1 = pd.DatetimeIndex([start], name="time")
    leg = Leg(spec, df, pd.Series([1], index=h1, dtype="int8"), "1h", pd.Series([a], index=h1), "synthetic", None,
              ee.sizing_config(50_000))
    r = Backtester(ee.sizing_config(50_000)).run_portfolio([leg])
    bt = r.trades.iloc[0]
    pairs = {"contracts": (t["contracts"], bt["contracts"]), "entry": (t["entry_price"], bt["entry_price"]),
             "initial SL": (t["initial_sl"], bt["initial_sl"]), "final SL": (t["final_sl"], bt["final_sl"]),
             "TP": (t["tp_price"], bt["tp_price"]), "exit": (t["exit_price"], bt["exit_price"]),
             "funding $": (t["funding_usd"], bt["funding_usd"]), "net ₹": (t["net_pnl_inr"], bt["net_pnl_inr"])}
    tb = Table(title="Paper engine vs backtester.py on the same synthetic 1m path")
    for col in ("field", "paper (SQLite)", "backtester", "diff"):
        tb.add_column(col, justify="right")
    for k, (x, y) in pairs.items():
        tb.add_row(k, f"{x:,.6f}", f"{y:,.6f}", f"{x - y:.2e}")
    tb.add_row("exit reason", t["exit_reason"], bt["exit_reason"], "")
    console.print(tb)
    check("3 paper", "paper fill/exit/funding/net == backtester to 1e-9", len(r.trades) == 1 and bt["exit_reason"] == t["exit_reason"]
          and all(abs(x - y) < 1e-9 for x, y in pairs.values()) and not audit_result(r))

    lt = Table(title="Paper trade_history row")
    for col in ("side", "lots", "entry", "exit", "reason", "gross ₹", "fees+GST ₹", "funding ₹", "net ₹", "equity after ₹"):
        lt.add_column(col, justify="right")
    lt.add_row(t["side"], str(t["contracts"]), f"{t['entry_price']:,.4f}", f"{t['exit_price']:,.4f}", t["exit_reason"],
               f"{t['gross_pnl_inr']:,.2f}", f"{t['fees_gst_inr']:,.2f}", f"{t['funding_inr']:,.4f}", f"{t['net_pnl_inr']:,.2f}",
               f"{t['equity_after_inr']:,.2f}")
    console.print(lt)


# --------------------------------------------------------------------------- #
# 4. Mocked testnet + fault injection
# --------------------------------------------------------------------------- #
def testnet_engine(fake: FakeDelta, clock: FakeClock) -> tuple[ee.ExecutionEngine, ee.StateStore, DeltaRestClientAdapter]:
    fresh_db(TEST_DB_TESTNET)
    store = ee.StateStore(TEST_DB_TESTNET, clock=clock)
    client = fake_client(fake, clock)
    return ee.ExecutionEngine(store, "testnet", client, clock=clock), store, client


def test_mocked_testnet() -> None:
    console.rule("[bold]4. Mocked testnet adapter + fault injection (httpx.MockTransport, signatures verified)")
    clock = FakeClock(us("2026-10-20 07:00:05"))

    # a) margin mode + leverage
    fake = FakeDelta(clock)
    eng, store, client = testnet_engine(fake, clock)
    r = client.set_margin_mode("isolated")
    c = fake.calls[-1]
    check("4 testnet", "set_margin_mode('isolated') -> PUT /v2/users/margin_mode", c["method"] == "PUT"
          and c["path"] == "/v2/users/margin_mode" and c["body"] == {"margin_mode": "isolated"} and r["margin_mode"] == "isolated",
          c["body_str"])
    r = client.set_order_leverage(27, 10)
    c = fake.calls[-1]
    check("4 testnet", "set_order_leverage(27, 10) -> POST /v2/products/27/orders/leverage", c["method"] == "POST"
          and c["path"] == "/v2/products/27/orders/leverage" and c["body"] == {"leverage": "10"} and str(r["leverage"]) == "10",
          c["body_str"])
    prep = eng.prepare_account()
    check("4 testnet", "prepare_account binds user_id, isolated margin, 10x on both products",
          prep["user_id"] == fake.user_id and store.config()["bound_user_id"] == fake.user_id
          and fake.leverage == {27: "10", 3136: "10"})

    # b) entry with bracket -> fill -> discover legs 9001/9002 -> trail SL in place
    atr = 450.0
    res = eng.on_signal("BTCUSD", 1, us("2026-10-20 06:00"), atr, fake.prices[27])
    post = [x for x in fake.calls if x["method"] == "POST" and x["path"] == "/v2/orders"][-1]["body"]
    check("4 testnet", "entry POST /v2/orders carries client_order_id + tick-snapped bracket SL/TP",
          {"client_order_id", "bracket_stop_loss_price", "bracket_take_profit_price", "bracket_stop_trigger_method"} <= set(post)
          and Decimal(post["bracket_stop_loss_price"]) % Decimal("0.5") == 0,
          f"size {post['size']} SL {post['bracket_stop_loss_price']} TP {post['bracket_take_profit_price']}")
    it = store.intent(res["intent_id"])
    check("4 testnet", "fill -> legs discovered via GET /v2/orders: SL 9001, TP 9002",
          it["state"] == "FILLED" and it["sl_leg_order_id"] == "9001" and it["tp_leg_order_id"] == "9002",
          " -> ".join(store.intent_path(res["intent_id"])))
    pos = store.position("testnet", "BTCUSD")
    check("4 testnet", "open_positions row bound to sl_order_id 9001 / tp_order_id 9002",
          pos is not None and pos["sl_order_id"] == "9001" and pos["tp_order_id"] == "9002")
    n_put_bracket = fake.count("PUT", "/v2/orders/bracket")
    clock.t = us("2026-10-20 07:01:00")
    tr = eng.on_bar("BTCUSD", us("2026-10-20 07:00"), 81_600, 83_100, 81_550, 83_000)
    edit = [x for x in fake.calls if x["method"] == "PUT" and x["path"] == "/v2/orders"]
    want_sl = str(snap_to_tick(83_100 - float(snap_to_tick(2.5 * atr, "0.5")), "0.5"))
    check("4 testnet", "trailing SL edits leg 9001 in place via PUT /v2/orders (never PUT /v2/orders/bracket)",
          tr is not None and edit and edit[-1]["body"]["id"] == 9001 and edit[-1]["body"]["stop_price"] == want_sl
          and edit[-1]["body"]["size"] == pos["contracts"] and fake.count("PUT", "/v2/orders/bracket") == n_put_bracket == 0
          and fake.orders[9001]["stop_price"] == want_sl and store.position("testnet", "BTCUSD")["sl_price"] == float(want_sl),
          f"PUT body {edit[-1]['body_str'] if edit else None}")
    clock.t = us("2026-10-20 07:05:00")
    fake.trigger_leg(9001, float(want_sl))
    ex = eng.sync_exchange_position("BTCUSD")
    th = store.trades("testnet")
    t = th[0] if th else None
    gross = (t["contracts"] * t["contract_value"] * (t["exit_price"] - t["entry_price"])) if t else 0
    check("4 testnet", "SL leg fill detected -> trade journalled (TSL), TP leg 9002 cancelled",
          ex is not None and ex["reason"] == "TSL" and fake.orders[9002]["state"] == "cancelled" and t is not None
          and abs(t["gross_pnl_usd"] - gross) < 1e-9 and abs(t["net_pnl_usd"] - (t["gross_pnl_usd"] - t["fees_gst_usd"] + t["funding_usd"])) < 1e-9,
          f"exit {t['exit_price']} net ₹{t['net_pnl_inr']:.2f}" if t else "")
    store.close()

    # c) timeout on POST /v2/orders where the order DID fill
    clock = FakeClock(us("2026-10-20 07:00:05"))
    fake = FakeDelta(clock)
    eng, store, client = testnet_engine(fake, clock)
    eng.prepare_account()
    fake.timeout_after_processing_next_post = True
    res = eng.on_signal("ETHUSD", 1, us("2026-10-20 06:00"), 20.0, fake.prices[3136])
    it = store.intent(res["intent_id"])
    check("4 testnet", "POST timeout (order filled server-side) -> intent OUTCOME_UNKNOWN",
          res["action"] == "outcome_unknown" and it["state"] == "OUTCOME_UNKNOWN" and fake.positions[3136]["size"] > 0,
          f"exchange position {fake.positions[3136]['size']} lots, journal {it['state']}")
    clock.t = us("2026-10-20 08:00:05")
    blocked = eng.on_signal("ETHUSD", 1, us("2026-10-20 07:00"), 20.0, fake.prices[3136])
    check("4 testnet", "new ETHUSD entries blocked while OUTCOME_UNKNOWN", blocked["action"] == "skip" and "blocked" in blocked["reason"],
          blocked["reason"])
    rec = eng.reconcile_unknown_intents()
    it = store.intent(res["intent_id"])
    n_post = fake.count("POST", "/v2/orders")
    seq = [x["path"] for x in fake.calls if x["method"] == "GET"][-6:]
    check("4 testnet", "reconcile via GET /v2/orders/client_order_id/{oid} + GET /v2/fills -> FILLED",
          it["state"] == "FILLED" and any(p.startswith("/v2/orders/client_order_id/") for p in seq) and "/v2/fills" in seq,
          f"{rec[0]['steps'] if rec else ''}; path {' -> '.join(store.intent_path(res['intent_id']))}")
    check("4 testnet", "order NOT resubmitted (exactly 1 POST /v2/orders, same client_order_id)", n_post == 1, f"{n_post} POST")
    pos = store.position("testnet", "ETHUSD")
    check("4 testnet", "protective legs bound after recovery (SL 9001 / TP 9002)",
          it["sl_leg_order_id"] == "9001" and it["tp_leg_order_id"] == "9002" and pos is not None
          and pos["contracts"] == fake.positions[3136]["size"] and abs(pos["entry_price"] - fake.prices[3136]) < 1e-9)
    check("4 testnet", "symbol unblocked after reconciliation", eng.symbol_blocked("ETHUSD") is None)
    store.close()

    # c2) timeout where the order never reached the exchange
    clock = FakeClock(us("2026-10-20 07:00:05"))
    fake = FakeDelta(clock)
    eng, store, client = testnet_engine(fake, clock)
    fake.timeout_before_processing_next_post = True
    res = eng.on_signal("BTCUSD", -1, us("2026-10-20 06:00"), 450.0, fake.prices[27])
    first = eng.reconcile_unknown_intents()
    clock.t += ee.UNKNOWN_GRACE_US + 1_000_000
    second = eng.reconcile_unknown_intents()
    check("4 testnet", "order never reached exchange: STILL_UNKNOWN within grace, REJECTED after, never resent",
          first[0]["result"] == "STILL_UNKNOWN" and second[0]["result"] == "REJECTED"
          and fake.count("POST", "/v2/orders") == 1 and fake.positions.get(27, {"size": 0})["size"] == 0,
          f"{first[0]['steps']} -> {second[0]['result']}")
    store.close()

    # d) emergency_close_all with a disrupted product
    clock = FakeClock(us("2026-10-20 07:00:05"))
    fake = FakeDelta(clock)
    fake.positions = {27: {"size": 5, "entry_price": 81_000.0}, 3136: {"size": -10, "entry_price": 3_050.0}}
    fake.disrupted = {3136}
    eng, store, client = testnet_engine(fake, clock)
    eng.bind_account()
    r = client.emergency_close_all(fake.user_id)
    check("4 testnet", "close_all body {close_all_portfolio, close_all_isolated, user_id}",
          [x for x in fake.calls if x["path"] == "/v2/positions/close_all"][-1]["body"]
          == {"close_all_portfolio": True, "close_all_isolated": True, "user_id": fake.user_id})
    check("4 testnet", "skipped product surfaces as RESIDUAL_EXPOSURE (not FLAT)",
          r["status"] == "RESIDUAL_EXPOSURE" and r["residual"] == [{"product_id": 3136, "product_symbol": "ETHUSD", "size": -10}]
          and r["skipped_products"][0]["reason"] == "market_disrupted_cancel_only_mode" and fake.positions[27]["size"] == 0,
          f"skipped {r['skipped_products']}; residual {r['residual']}")
    fake.positions[27] = {"size": 2, "entry_price": 81_000.0}
    rf = eng.emergency_flatten()
    check("4 testnet", "engine.emergency_flatten halts the bot on residual exposure",
          rf["status"] == "RESIDUAL_EXPOSURE" and eng.halted()[0], eng.halted()[1] or "")
    fake.disrupted = set()
    r_ok = client.emergency_close_all(fake.user_id)
    check("4 testnet", "after disruption clears: close_all verified FLAT via real-time positions", r_ok["status"] == "FLAT"
          and all(p["size"] == 0 for p in fake.positions.values()))

    # e) reduce-only close of one product
    fake.positions[27] = {"size": 3, "entry_price": 81_000.0}
    entry = fake._new_order({"product_id": 27, "size": 2, "side": "buy", "order_type": "limit_order", "limit_price": "80000.0"})
    rc = client.close_position_reduce_only(27, 3)
    last_post = [x for x in fake.calls if x["method"] == "POST" and x["path"] == "/v2/orders"][-1]["body"]
    check("4 testnet", "close_position_reduce_only: cancels resting entry, reduce-only market sell 3, verifies flat",
          rc["flat"] and entry["state"] == "cancelled" and last_post["reduce_only"] is True and last_post["side"] == "sell"
          and last_post["size"] == 3, f"cancelled {rc['cancelled_entry_orders']}, residual {rc['residual_size']}")
    ok, det = raises(OrderValidationError, client.edit_entry_bracket, entry["id"], 27, stop_loss_price=79_000)
    check("4 testnet", "edit_entry_bracket refused on a non-open (cancelled/filled) entry order", ok, det)
    check("4 testnet", "every authenticated request carried a valid signature + required headers",
          fake.sig_failures == 0 and fake.header_failures == 0, f"{fake.sig_checked} verified in this fake")
    ok, det = raises(LiveTradingDisabledError, lambda: ee.ExecutionEngine(store, "live", DeltaRestClientAdapter("live", "k", "s")))
    check("4 testnet", "live engine cannot be constructed while MODE='research'", ok, det)
    store.close()

    # f) optional real testnet read-only
    console.rule("[bold]4f. Real Delta India testnet (read-only)")
    if settings.TESTNET_API_KEY and settings.TESTNET_API_SECRET:
        tn = DeltaRestClientAdapter.from_settings("testnet")
        prefs = tn.get_trading_preferences()
        bals = tn.get_wallet_balances()
        info = tn.get_product("BTCUSD", require_tradable=False)
        orders = tn.get_active_orders(info.id)
        usd = next((b for b in bals if b.get("asset_symbol") == "USD"), {})
        check("4 testnet", "real testnet read-only: trading_preferences / wallet / active orders", "user_id" in prefs,
              f"user_id {prefs.get('user_id')}, USD available {usd.get('available_balance')}, {len(orders)} open orders")
    else:
        console.print("  [yellow]SKIPPED (no Testnet key in .env)[/] — to enable, create "
                      f"{PROJECT_ROOT / '.env'} with:\n"
                      "    DELTA_TESTNET_API_KEY=<key from https://demo.india.delta.exchange → API keys>\n"
                      "    DELTA_TESTNET_API_SECRET=<secret>\n"
                      "  (read-only + trading permission on the DEMO account only; never put live keys there)")


# --------------------------------------------------------------------------- #
def test_step4(skip: bool) -> None:
    console.rule("[bold]5. Re-run step4_verify.py")
    if skip:
        console.print("  [yellow]skipped (--skip-step4)[/]")
        return
    t0 = time.perf_counter()
    p = subprocess.run([sys.executable, str(PROJECT_ROOT / "step4_verify.py")], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=PROJECT_ROOT, env={**__import__("os").environ,
                                                                                    "PYTHONIOENCODING": "utf-8", "COLUMNS": "180"})
    out = p.stdout
    console.print(out, markup=False, highlight=False)
    if p.stderr.strip():
        console.print(p.stderr[-3000:], markup=False, highlight=False)
    last = [ln for ln in out.splitlines() if "Step 4 verification" in ln]
    check("5 step4", "step4_verify.py exit code 0 and verdict PASS", p.returncode == 0 and bool(last) and "PASS" in last[-1],
          f"{last[-1].strip() if last else 'no verdict line'} ({time.perf_counter() - t0:.0f}s)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-sync", action="store_true")
    ap.add_argument("--skip-step4", action="store_true")
    args = ap.parse_args()
    console.print(f"MODE={settings.MODE!r}  LIVE_TRADING_ENABLED={settings.LIVE_TRADING_ENABLED}  "
                  f"EXECUTION_MODE={settings.EXECUTION_MODE!r}  sizing slippage {settings.SIZING_SLIPPAGE_BPS} bps  "
                  f"cost gate at {ee.GATE_SLIPPAGE_BPS} bps")
    test_signing()
    live = test_live_public(sync=not args.no_sync)
    test_paper_lifecycle(live)
    test_mocked_testnet()
    test_step4(args.skip_step4)

    console.rule("[bold]Summary")
    t = Table()
    for col in ("section", "check", "result", "detail"):
        t.add_column(col)
    for sec, name, ok, detail in CHECKS:
        t.add_row(sec, name, "[green]PASS" if ok else "[red]FAIL", detail[:90])
    console.print(t)
    failed = [x for x in CHECKS if not x[2]]
    console.rule(f"Step 5 verification: {'[green]PASS' if not failed else f'[red]FAIL ({len(failed)} checks)'} - {len(CHECKS)} checks")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
