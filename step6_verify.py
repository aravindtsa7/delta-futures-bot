"""Step 6 verification: WebSocket feed manager + live bot runner.

  1. Live public WebSocket probe (wss://public-socket.india.delta.exchange)
  2. Deterministic drill (mock WS + mock REST, testnet mode): readiness sequence, 'all' refusal,
     hour-boundary signal -> entry -> trailing -> exit, sequence gap / socket drop / user_trades reset
     reconciliation, stale-generation drops, skipped-minute REST backfill, watchdog reconnects
  3. Safety: single-instance lock, 5% daily-loss breaker, 4-consecutive-loss lock (paper)
  4. CLI smoke test (live_bot.py --mode paper --once) + regression (step5_verify.py incl. step4_verify.py)

Usage:
    python step6_verify.py [--skip-regression]
"""

from __future__ import annotations

import testenv  # noqa: F401,E402  (must run before config is imported: research mode, no live keys)

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pandas as pd
from rich.console import Console
from rich.table import Table

import live_bot as lb
import ws_manager as wm
from config import PROJECT_ROOT, settings
from exchange_client import DeltaRestClientAdapter, sign_request
from step5_verify import FAKE_KEY, FAKE_SECRET, FakeClock, FakeDelta, fake_client, fresh_db, raises, us

console = Console(width=200)
CHECKS: list[tuple[str, str, bool, str]] = []
DB_TESTNET = settings.data_path / "test_live_bot_testnet.db"
DB_PAPER = settings.data_path / "test_live_bot_paper.db"
DB_LOCK = settings.data_path / "test_live_bot_lock.db"
# Test-only lock / journal / control paths: the drills never touch a real bot's data/live_bot.lock.
TEST_LOCK = settings.data_path / "test_live_bot.lock"
CLI_DB = settings.data_path / "test_live_bot_cli.db"
CLI_LOCK = settings.data_path / "test_live_bot_cli.lock"
CLI_CONTROL = settings.data_path / "test_bot_control"
SYMS = ["BTCUSD", "ETHUSD"]


def check(section: str, name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((section, name, bool(ok), detail))
    console.print(f"  {'[green]PASS[/]' if ok else '[red]FAIL[/]'} {name}" + (f"  [dim]{detail}[/]" if detail else ""))
    return bool(ok)


def ts(s: str) -> int:
    return int(pd.Timestamp(s, tz="UTC").value // 10**9)


# --------------------------------------------------------------------------- #
# 1. Live public WebSocket probe
# --------------------------------------------------------------------------- #
class ProbeHandler:
    def __init__(self):
        self.closed: list[wm.Candle] = []
        self.tickers: list[dict] = []
        self.quotes: list[dict] = []
        self.status: list[dict] = []
        self.sub_errors: list[dict] = []
        self.phases: list[str] = []

    def on_ws_phase(self, p, g): self.phases.append(p)
    def on_closed_1m_candle(self, c): self.closed.append(c)
    def on_ticker(self, t): self.tickers.append(t)
    def on_quote(self, q): self.quotes.append(q)
    def on_system_status(self, m): self.status.append(m)
    def on_subscription_error(self, s, ch): self.sub_errors.append(ch)


def test_live_probe() -> None:
    console.rule("[bold]1. Live public WebSocket probe (wss://public-socket.india.delta.exchange)")
    cs = ["BTCUSD", "ETHUSD", "MARK:BTCUSD", "MARK:ETHUSD"]
    channels = [{"name": "v2/ticker", "symbols": SYMS}, {"name": "ticker", "symbols": SYMS},
                {"name": "candlestick_1m", "symbols": cs}, {"name": "ob_l1", "symbols": SYMS}, {"name": "system_status"}]
    h = ProbeHandler()
    mgr = wm.DeltaWebSocketManager("paper", SYMS, h, public_channels=channels)
    t0 = time.time()

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(mgr.run(stop))
        while time.time() - t0 < 85:
            await asyncio.sleep(0.5)
            if time.time() - t0 >= 15 and set(cs) <= {c.symbol for c in h.closed}:
                break
        stop.set()
        await task
    asyncio.run(go())
    dur = time.time() - t0
    st = mgr.stats
    console.print(f"  connected {dur:.1f}s (15s minimum, extended until one closed candle per symbol), generation {mgr.generation}, "
                  f"phases {' -> '.join(h.phases)}")
    console.print(f"  messages by type: {st.messages}")
    check("1 live-ws", "enable_heartbeat sent first; server heartbeat received", mgr.sent[0][2] == {"type": "enable_heartbeat"}
          and st.heartbeats >= 1, f"{st.heartbeats} heartbeat(s)")
    acked = set(st.acked.get("public", []))
    check("1 live-ws", "subscriptions ack: ticker, candlestick_1m, ob_l1, system_status",
          {"ticker", "candlestick_1m", "ob_l1", "system_status"} <= acked, f"acked {sorted(acked)}")
    err = [e for e in st.subscription_errors if e.get("name") == "v2/ticker"]
    check("1 live-ws", "'v2/ticker' flagged by the ack's error field (India socket uses 'ticker')", bool(err),
          err[0]["error"] if err else "no error returned")
    wk = st.wire_keys.get("candlestick_1m", [])
    schema = "compact (cst,o,h,l,c,v,sy,ts)" if "cst" in wk else "verbose (candle_start_time,...)" if "candle_start_time" in wk else "?"
    check("1 live-ws", "candlestick_1m wire keys recorded", bool(wk), f"{wk} -> {schema}; normalized schemas {st.schemas}")
    check("1 live-ws", "ticker wire keys recorded + normalized (mark inside price band)",
          bool(h.tickers) and all(t["band_lower"] < t["mark_price"] < t["band_upper"] for t in h.tickers if t["mark_price"]),
          f"ticker keys {st.wire_keys.get('ticker')}; e.g. {({k: h.tickers[-1][k] for k in ('symbol', 'mark_price', 'spot_price', 'band_lower', 'band_upper')} if h.tickers else None)}")
    check("1 live-ws", "ob_l1 normalized (best bid < best ask)", bool(h.quotes) and all(q["best_bid"] < q["best_ask"] for q in h.quotes),
          f"keys {st.wire_keys.get('ob_l1')}")
    check("1 live-ws", "system_status received", bool(h.status), str(h.status[0].get("status")) if h.status else "")
    got = {c.symbol for c in h.closed}
    check("1 live-ws", "explicit symbols only: no other products on candlestick_1m", got <= set(cs), f"{sorted(got)}")
    check("1 live-ws", "on_closed_1m_candle fired for BTCUSD, ETHUSD, MARK:BTCUSD, MARK:ETHUSD", set(cs) <= got,
          f"{st.closed_candles} closed candles")
    ok_shape = all(c.start % 60 == 0 and c.high >= max(c.open, c.close) and c.low <= min(c.open, c.close) for c in h.closed)
    check("1 live-ws", "normalized candles: µs cst -> Unix seconds on the minute, OHLC consistent", ok_shape,
          f"e.g. {h.closed[0].symbol} {lb.to_utc(h.closed[0].start)} o{h.closed[0].open} h{h.closed[0].high} l{h.closed[0].low} c{h.closed[0].close}" if h.closed else "")
    rest = DeltaRestClientAdapter("paper")
    t = Table(title="Closed 1m candle: WebSocket vs REST /v2/history/candles")
    for col in ("symbol", "minute (UTC)", "WS o/h/l/c", "REST o/h/l/c", "max |diff| %", "WS vol", "REST vol"):
        t.add_column(col, justify="right")
    worst = 0.0
    for sym in cs:
        c = next((x for x in h.closed if x.symbol == sym), None)
        if c is None:
            continue
        rows = []
        for _ in range(6):
            rows = rest.get_history_candles(sym, c.start, c.start)
            if rows:
                break
            time.sleep(2)
        if not rows:
            t.add_row(sym, lb.to_utc(c.start), "", "not yet on REST", "", "", "")
            continue
        r = rows[0]
        diff = max(abs(a - float(r[k])) / float(r[k]) * 100 for a, k in ((c.open, "open"), (c.high, "high"), (c.low, "low"), (c.close, "close")))
        worst = max(worst, diff)
        t.add_row(sym, lb.to_utc(c.start), f"{c.open}/{c.high}/{c.low}/{c.close}", f"{r['open']}/{r['high']}/{r['low']}/{r['close']}",
                  f"{diff:.4f}", f"{c.volume:g}", f"{float(r.get('volume') or 0):g}")
    console.print(t)
    check("1 live-ws", "closed WS candle matches the REST candle (OHLC within 0.1%)", worst <= 0.1, f"worst {worst:.4f}%")


# --------------------------------------------------------------------------- #
# Fakes for the deterministic drills
# --------------------------------------------------------------------------- #
CLOSE = object()


class FakeConn:
    def __init__(self, server: "FakeWSServer", url: str):
        self.server, self.url = server, url
        self.private = "pub" not in url and "public" not in url
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        m = json.loads(raw)
        self.sent.append(m)
        self.server.on_message(self, m)

    async def recv(self) -> str:
        item = await self.inbox.get()
        if item is CLOSE:
            raise ConnectionError("socket closed by server")
        return item

    async def close(self) -> None:
        self.closed = True

    def push(self, m: dict) -> None:
        self.inbox.put_nowait(json.dumps(m))

    def drop(self) -> None:
        self.inbox.put_nowait(CLOSE)


class FakeWSServer:
    def __init__(self, secret: str = FAKE_SECRET, silent_after_ack: bool = False):
        self.secret, self.silent = secret, silent_after_ack
        self.conns: list[FakeConn] = []
        self.auth_ok = self.auth_bad = 0

    async def connect(self, url: str) -> FakeConn:
        c = FakeConn(self, url)
        self.conns.append(c)
        return c

    def latest(self, private: bool) -> FakeConn:
        return [c for c in self.conns if c.private == private][-1]

    def on_message(self, conn: FakeConn, m: dict) -> None:
        t = m.get("type")
        if t == "enable_heartbeat":
            conn.push({"type": "heartbeat", "ts_origin": 1})
        elif t == "key-auth":
            p = m["payload"]
            ok = sign_request(self.secret, "GET", p["timestamp"], "/live") == p["signature"]
            self.auth_ok += ok
            self.auth_bad += not ok
            conn.push({"type": "key-auth", "success": ok, "status": "authenticated" if ok else "unauthorized"})
        elif t == "subscribe":
            conn.push({"type": "subscriptions", "channels": m["payload"]["channels"]})
            if conn.private and not self.silent:
                conn.push({"type": "orders", "action": "snapshot", "result": [], "seq_no": 1})
                conn.push({"type": "positions", "action": "snapshot", "result": []})


class FakeDelta6(FakeDelta):
    """Step 5 fake exchange + public /v2/history/candles (newest-first, like the real API)."""

    def __init__(self, clock: FakeClock):
        super().__init__(clock)
        self.candles: dict[str, dict[int, dict]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/history/candles":
            p = dict(parse_qsl(request.url.query.decode()))
            self.calls.append({"method": "GET", "path": request.url.path, "params": p, "body": {}, "auth": False, "query": ""})
            rows = [r for t, r in self.candles.get(p["symbol"], {}).items() if int(p["start"]) <= t <= int(p["end"])]
            return self.ok(sorted(rows, key=lambda r: -r["time"]))
        return super().handler(request)


class FakeMarket:
    def __init__(self, clock_s, signals: dict[tuple[str, int], tuple[int, float]], prices: dict[str, float]):
        self.clock_s, self.signals, self.prices = clock_s, signals, prices
        self.evaluations: list[tuple[str, int | None]] = []
        self.syncs = 0

    def sync(self, symbols=None):
        self.syncs += 1
        t = int(self.clock_s())
        return {s: {"last_1m": t - t % 60 - 60, "fetched": 0, "gaps": {"TRADE": 0, "MARK": 0, "FUNDING": 0}} for s in symbols or SYMS}

    def evaluate(self, sym, hour_close_s=None):
        if hour_close_s is None:
            t = int(self.clock_s())
            hour_close_s = t - t % 3600
        self.evaluations.append((sym, hour_close_s))
        d, atr = self.signals.get((sym, hour_close_s), (0, 20.0 if sym == "ETHUSD" else 450.0))
        p = self.prices[sym]
        return {"symbol": sym, "variant": "C_costgate" if sym == "ETHUSD" else "D_both",
                "bar_open": pd.Timestamp(hour_close_s - 3600, unit="s", tz="UTC"),
                "bar_close": pd.Timestamp(hour_close_s, unit="s", tz="UTC"), "close": p, "bb_upper": p * 1.01,
                "bb_lower": p * 0.99, "ema50_4h": p, "ema200_4h": p * 0.99, "atr14": atr, "cost_gate_pass": True,
                "cond_long": d == 1, "cond_short": d == -1, "signal": d, "fresh_signal": d != 0,
                "signal_side": {1: "LONG", -1: "SHORT"}.get(d)}

    def minutes_after(self, sym, after_s):
        return []

    def funding_inputs(self, sym, bar_s):
        return 0.0001, self.prices[sym]

    def ticker(self, sym):
        p = self.prices[sym]
        return {"best_bid": p - (0.05 if sym == "ETHUSD" else 0.5), "best_ask": p, "mark_price": p}


def candle_msg(sym: str, start: int, o: float, h: float, l: float, c: float, verbose: bool = False) -> dict:
    if verbose:
        return {"type": "candlestick_1m", "symbol": sym, "candle_start_time": start * 1_000_000, "open": o, "high": h,
                "low": l, "close": c, "volume": 10, "timestamp": (start + 30) * 1_000_000, "resolution": "1m"}
    return {"type": "candlestick_1m", "sy": sym, "cst": start * 1_000_000, "o": o, "h": h, "l": l, "c": c, "v": 10.0,
            "ts": (start + 30) * 1_000_000, "res": "1m"}


# --------------------------------------------------------------------------- #
# 2. Deterministic drill (testnet mode, mock WS + mock REST)
# --------------------------------------------------------------------------- #
def test_units() -> None:
    console.rule("[bold]2a. Normalizers, subscription guard, sequence tracker")
    for name in ("orders", "positions", "v2/user_trades", "candlestick_1m", "v2/ticker", "mark_price", "ob_l1", "ob_l2",
                 "ob_updates", "spot_price", "v2/spot_price"):
        ok, _ = raises(wm.SubscriptionError, wm.subscribe_message, [{"name": name, "symbols": ["all"]}])
        if not ok:
            break
    check("2 drill", "symbols ['all'] refused on all 11 symbol-mandatory/snapshot channels", ok)
    ok2, det = raises(wm.SubscriptionError, wm.DeltaWebSocketManager, "paper", SYMS,
                      public_channels=[{"name": "candlestick_1m", "symbols": ["BTCUSD", "all"]}])
    check("2 drill", "manager constructor refuses ['all'] too; missing symbols refused", ok2
          and raises(wm.SubscriptionError, wm.subscribe_message, [{"name": "orders"}])[0]
          and wm.subscribe_message([{"name": "system_status"}])["type"] == "subscribe", det)
    a = wm.normalize_candle(candle_msg("ETHUSD", ts("2026-10-20 05:59"), 1, 2, 0.5, 1.5))
    b = wm.normalize_candle(candle_msg("ETHUSD", ts("2026-10-20 05:59"), 1, 2, 0.5, 1.5, verbose=True))
    check("2 drill", "compact and verbose candlestick schemas normalize identically (µs -> s)",
          (a.symbol, a.start, a.open, a.high, a.low, a.close) == (b.symbol, b.start, b.open, b.high, b.low, b.close)
          and a.start == ts("2026-10-20 05:59") and (a.schema, b.schema) == ("compact", "verbose"))
    v = wm.normalize_ticker({"type": "v2/ticker", "symbol": "BTCUSD", "mark_price": "100", "spot_price": "101",
                             "quotes": {"best_bid": "99.5", "best_ask": "100.5"}, "price_band": {"lower_limit": "95", "upper_limit": "105"}})[0]
    c = wm.normalize_ticker({"type": "ticker", "sy": "BTCUSD", "sp": "101", "d": [{"s": "BTCUSD", "m": "100", "pb": ["95", "105"]}]})[0]
    check("2 drill", "ticker: verbose v2/ticker and compact batched 'ticker' normalize to the same fields",
          (v["mark_price"], v["spot_price"], v["band_lower"], v["band_upper"]) == (c["mark_price"], c["spot_price"], c["band_lower"], c["band_upper"]))
    tr = wm.SequenceTracker()
    seq = [tr.feed("orders", "ETHUSD", s, snap) for s, snap in ((1, True), (2, False), (3, False), (6, False))]
    tr2 = wm.SequenceTracker()
    reset = [tr2.feed("v2/user_trades", "ETHUSD", s, False) for s in (7, 8, 1)]
    check("2 drill", "sequence tracker: snapshot baseline, gap 3->6, reset 8->1; seq_no/seq_id/se field names",
          seq[:3] == [None, None, None] and seq[3]["kind"] == "gap" and reset[2]["kind"] == "reset"
          and wm.message_seq("orders", {"seq_id": 4}) == 4 and wm.message_seq("orders", {"seq_no": 5}) == 5
          and wm.message_seq("v2/user_trades", {"se": 9}) == 9, f"{seq[3]} / {reset[2]}")
    m = wm.key_auth_message("k", "s", "1700000000")
    check("2 drill", "key-auth payload: signature = HMAC('GET' + timestamp + '/live'); legacy 'auth' never used",
          m["type"] == "key-auth" and m["payload"]["signature"] == sign_request("s", "GET", "1700000000", "/live"))


async def testnet_drill() -> None:
    console.rule("[bold]2b. Testnet drill: readiness, hour signal, trailing, anomalies, reconnects, exit")
    fclock = FakeClock(us("2026-10-20 05:00:30"))
    fake = FakeDelta6(fclock)
    fake.prices[3136] = 2400.0
    server = FakeWSServer()
    bot_clock = fclock.seconds
    prices = {"ETHUSD": 2400.0, "BTCUSD": 81_600.0}
    market = FakeMarket(bot_clock, {("ETHUSD", ts("2026-10-20 06:00")): (1, 20.0), ("BTCUSD", ts("2026-10-20 07:00")): (1, 450.0)}, prices)
    fresh_db(DB_TESTNET)
    bot = lb.LiveBotRunner("testnet", client=fake_client(fake, fclock), market=market, store_path=DB_TESTNET,
                           lock_path=TEST_LOCK, clock=bot_clock, ws_connector=server.connect,
                           ws_api_key=FAKE_KEY, ws_api_secret=FAKE_SECRET)
    ws = bot.ws
    try:
        await ws.connect_once()
        seq = [x["to"] for x in bot.transitions]
        want = ["BOOTING", "CONNECTING", "AUTHENTICATING", "SUBSCRIBING", "SNAPSHOTTING", "RECONCILING", "OBSERVED_READY"]
        check("2 drill", "startup: BOOTING -> CONNECTING -> AUTHENTICATING -> SUBSCRIBING -> SNAPSHOTTING -> RECONCILING -> OBSERVED_READY",
              seq == want, " -> ".join(seq))
        pub, prv = server.latest(False), server.latest(True)
        check("2 drill", "wire order: enable_heartbeat first on both sockets; private key-auth acknowledged before subscribe",
              pub.sent[0]["type"] == "enable_heartbeat" and [m["type"] for m in prv.sent[:3]] == ["enable_heartbeat", "key-auth", "subscribe"]
              and server.auth_ok == 1 and server.auth_bad == 0 and ws.authenticated,
              f"private sent {[m['type'] for m in prv.sent]}")
        check("2 drill", "private channels orders/positions/v2/user_trades subscribed with explicit symbols; snapshots seen",
              [c["name"] for c in prv.sent[2]["payload"]["channels"]] == ["orders", "positions", "v2/user_trades"]
              and all(sorted(c["symbols"]) == sorted(SYMS) for c in prv.sent[2]["payload"]["channels"]) and {"orders", "positions"} <= ws.snapshots)
        r0 = bot.reconciliations[-1]
        check("2 drill", "startup reconciliation via REST: user_id bound, wallet, positions, active orders, candle sync",
              r0["user_id"] == fake.user_id and abs(r0["wallet_usd_available"] - 50_000 / 85) < 1e-6 and r0["issues"] == []
              and fake.count("GET", "/v2/users/trading_preferences") >= 1 and fake.count("GET", "/v2/positions") == 2)

        def feed(sym: str, start: int, o: float, h: float, l: float, c: float, verbose: bool = False, gen: int | None = None):
            fclock.t = (start + 2) * 1_000_000
            prices[sym] = o
            ws.handle_message(candle_msg(sym, start, o, h, l, c, verbose), ws.generation if gen is None else gen)

        t = ts("2026-10-20 05:00")
        while t <= ts("2026-10-20 06:00"):          # 05:00..05:59 closed by the first 06:00 tick (BTC on the verbose schema)
            feed("ETHUSD", t, 2400, 2400.5, 2399.5, 2400)
            feed("BTCUSD", t, 81_600, 81_605, 81_595, 81_600, verbose=True)
            t += 60
        e = bot.entries[0] if bot.entries else {}
        it = bot.store.one("SELECT * FROM order_intents WHERE symbol='ETHUSD'")
        check("2 drill", "05:59 close completes the 1h bar -> frozen signal evaluated -> ETH entry with bracket legs 9001/9002",
              e.get("hour_close") == ts("2026-10-20 06:00") and e["result"]["action"] == "filled" and it["sl_leg_order_id"] == "9001"
              and it["tp_leg_order_id"] == "9002" and fake.count("POST", "/v2/orders") == 1,
              f"evaluations at {sorted({lb.to_utc(h) for _, h in market.evaluations})}, {e.get('result', {}).get('contracts')} lots")
        check("2 drill", "60 minutes per symbol processed, no minute gaps", bot.minute_gaps == 0
              and bot.last_minute == {"ETHUSD": ts("2026-10-20 05:59"), "BTCUSD": ts("2026-10-20 05:59")})

        # rising ETH -> trailing; skip 06:10/06:11 on the socket -> REST backfill
        for k, m in enumerate(range(0, 13)):
            start = ts("2026-10-20 06:00") + 60 * m
            px = 2400 + 3 * m
            fake.candles.setdefault("ETHUSD", {})[start] = {"time": start, "open": px, "high": px + 1, "low": px - 1, "close": px, "volume": 5}
            feed("BTCUSD", start, 81_600, 81_605, 81_595, 81_600, verbose=True)
            if m not in (10, 11):
                feed("ETHUSD", start, px, px + 1, px - 1, px)
        puts = [c for c in fake.calls if c["method"] == "PUT" and c["path"] == "/v2/orders"]
        check("2 drill", "1m closes ratchet the SL leg in place: PUT /v2/orders id 9001 (never PUT /v2/orders/bracket)",
              len(puts) >= 3 and all(c["body"]["id"] == 9001 for c in puts) and fake.count("PUT", "/v2/orders/bracket") == 0,
              f"{len(puts)} edits, SL now {fake.orders[9001]['stop_price']}")
        hc = [c for c in fake.calls if c["path"] == "/v2/history/candles"]
        check("2 drill", "socket skipped 06:10-06:11 -> REST /v2/history/candles backfill, minutes processed in order",
              ws.closer.backfilled_minutes == 2 and len(hc) == 1 and bot.minute_gaps == 0 and bot.last_minute["ETHUSD"] == ts("2026-10-20 06:11"),
              f"backfill {lb.to_utc(int(hc[0]['params']['start']))}..{lb.to_utc(int(hc[0]['params']['end']))}" if hc else "")

        # continue to 06:58 so the anomaly lands just before the BTC hour close
        for m in range(13, 60):
            start = ts("2026-10-20 06:00") + 60 * m
            feed("ETHUSD", start, 2436, 2437, 2435, 2436)
            feed("BTCUSD", start, 81_600, 81_605, 81_595, 81_600, verbose=True)
        n_post, sl_before = fake.count("POST", "/v2/orders"), fake.orders[9001]["stop_price"]
        gen_before = ws.generation
        for s in (1, 2, 5):
            ws.handle_message({"type": "orders", "symbol": "ETHUSD", "seq_no": s, "action": "update", "id": 9001, "state": "pending"}, ws.generation, "private")
        check("2 drill", "orders seq_no 2 -> 5 gap: state RECONCILING immediately, generation bumped",
              bot.state == "RECONCILING" and ws.generation == gen_before + 1 and ws.stats.anomalies[-1]["kind"] == "gap",
              f"{ws.stats.anomalies[-1]}")
        dropped = ws.stats.dropped_stale_generation
        ws.handle_message(candle_msg("ETHUSD", ts("2026-10-20 06:59"), 2436, 2600, 2435, 2590), gen_before)
        ws.handle_message(candle_msg("ETHUSD", ts("2026-10-20 07:00"), 2590, 2600, 2580, 2590), gen_before)
        check("2 drill", "late frames from the old generation dropped (no trailing from them)",
              ws.stats.dropped_stale_generation == dropped + 2 and fake.orders[9001]["stop_price"] == sl_before)
        fclock.t = us("2026-10-20 07:00:05")
        bot.on_minute_close("ETHUSD", ts("2026-10-20 06:59"), 2436, 2437, 2435, 2436, "rest_poll")
        bot.on_minute_close("BTCUSD", ts("2026-10-20 06:59"), 81_600, 81_605, 81_595, 81_600, "rest_poll")
        blk = [b for b in bot.blocked_signals if b["symbol"] == "BTCUSD"]
        check("2 drill", "BTC 1h signal at 07:00 while RECONCILING -> entry blocked, nothing sent",
              blk and blk[0]["state"] == "RECONCILING" and fake.count("POST", "/v2/orders") == n_post, f"{blk[0] if blk else ''}")
        await ws.connect_once()
        r = bot.reconciliations[-1]
        tail = [x["to"] for x in bot.transitions][-6:]
        check("2 drill", "reconnect (new generation) -> REST reconciliation -> OBSERVED_READY, no duplicate order",
              bot.state == "OBSERVED_READY" and tail == want[1:] and fake.count("POST", "/v2/orders") == n_post
              and r["positions"]["ETHUSD"]["sl_leg"] == 9001 and r["positions"]["ETHUSD"]["tp_leg"] == 9002 and not r["issues"],
              f"ETH exchange {r['positions']['ETHUSD']['exchange_size']} = journal {r['positions']['ETHUSD']['journal']}; legs 9001/9002")
        check("2 drill", "the missed BTC signal is not entered late after recovery", not any(x["symbol"] == "BTCUSD" for x in bot.entries))

        # socket drop handled by the real run() loop
        stop = asyncio.Event()
        n_rec = len(bot.reconciliations)
        task = asyncio.create_task(ws.run(stop))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if len(bot.reconciliations) > n_rec:
                break
        gen_run = ws.generation
        server.latest(True).drop()
        states_seen = []
        for _ in range(200):
            await asyncio.sleep(0.02)
            states_seen.append(bot.state)
            if len(bot.reconciliations) >= n_rec + 2 and bot.state == "OBSERVED_READY":
                break
        stop.set()
        await task
        drops = [x for x in bot.transitions if x["reason"].startswith("socket drop")]
        check("2 drill", "private socket drop during the open position -> RECONCILING -> new generation -> OBSERVED_READY",
              bool(drops) and bot.state == "OBSERVED_READY" and ws.generation > gen_run and fake.count("POST", "/v2/orders") == n_post
              and bot.store.position("testnet", "ETHUSD") is not None, f"{drops[0]['reason'][:80] if drops else ''}")

        await ws.connect_once()
        for s in (1, 2, 3, 1):
            ws.handle_message({"type": "v2/user_trades", "sy": "ETHUSD", "se": s, "p": "2436"}, ws.generation, "private")
        reset_state = bot.state
        await ws.connect_once()
        check("2 drill", "v2/user_trades se reset 3 -> 1 -> RECONCILING -> REST reconciliation -> OBSERVED_READY",
              reset_state == "RECONCILING" and ws.stats.anomalies[-1]["kind"] == "reset" and bot.state == "OBSERVED_READY"
              and fake.count("POST", "/v2/orders") == n_post, f"{ws.stats.anomalies[-1]}")

        # exit: the exchange fills SL leg 9001; the next minute close journals it
        stop_px = float(fake.orders[9001]["stop_price"])
        fake.trigger_leg(9001, stop_px)
        feed("ETHUSD", ts("2026-10-20 07:10"), stop_px, stop_px + 1, stop_px - 5, stop_px - 3)
        feed("ETHUSD", ts("2026-10-20 07:11"), stop_px - 3, stop_px, stop_px - 4, stop_px - 2)
        tr = bot.store.trades("testnet")
        check("2 drill", "SL leg fill -> trade journalled (TSL, exit order 9001), TP leg 9002 cancelled, position closed",
              len(tr) == 1 and tr[0]["exit_reason"] == "TSL" and tr[0]["exit_order_id"] == "9001"
              and fake.orders[9002]["state"] == "cancelled" and bot.store.position("testnet", "ETHUSD") is None,
              f"exit {tr[0]['exit_price']} net ₹{tr[0]['net_pnl_inr']:.2f}" if tr else "")
        check("2 drill", "whole drill: exactly 1 entry order, every REST signature valid", fake.count("POST", "/v2/orders") == 1
              and fake.sig_failures == 0 and fake.header_failures == 0, f"{fake.sig_checked} signed requests")
        t2 = Table(title="Drill state transitions")
        for col in ("#", "from", "to", "reason"):
            t2.add_column(col)
        for i, x in enumerate(bot.transitions):
            t2.add_row(str(i), x["from"] or "-", x["to"], x["reason"][:110])
        console.print(t2)
    finally:
        await ws.close_sockets()
        bot.close()


async def watchdog_drill() -> None:
    console.rule("[bold]2c. Watchdog: silent socket -> close + new generation")
    server = FakeWSServer(silent_after_ack=True)

    class H:
        def __init__(self):
            self.disc: list[str] = []

        def on_ws_disconnect(self, g, reason):
            self.disc.append(reason)
    h = H()
    mgr = wm.DeltaWebSocketManager("paper", SYMS, h, connector=server.connect, watchdog_s=0.4)
    stop = asyncio.Event()
    task = asyncio.create_task(mgr.run(stop))
    await asyncio.sleep(2.0)
    stop.set()
    await task
    wd = [r for r in h.disc if r.startswith("watchdog")]
    check("2 drill", "no frame for watchdog_s -> socket closed, reconnected as a new generation (35s in production)",
          len(wd) >= 2 and mgr.generation >= 3 and all(c.closed for c in server.conns[:-1]),
          f"{len(wd)} watchdog reconnects, generation {mgr.generation}")


# --------------------------------------------------------------------------- #
# 3. Safety locks (paper)
# --------------------------------------------------------------------------- #
def test_lock() -> None:
    console.rule("[bold]3a. Single-instance lock (test lock file; same mechanism as data/live_bot.lock)")
    fresh_db(DB_LOCK)
    a = lb.LiveBotRunner("paper", market=FakeMarket(time.time, {}, {"ETHUSD": 2400, "BTCUSD": 81_600}), store_path=DB_LOCK,
                         lock_path=TEST_LOCK)
    try:
        ok, det = raises(lb.BotAlreadyRunningError, lb.LiveBotRunner, "paper",
                         market=FakeMarket(time.time, {}, {"ETHUSD": 2400, "BTCUSD": 81_600}), store_path=DB_LOCK,
                         lock_path=TEST_LOCK)
        info = lb.InstanceLock.read_info(TEST_LOCK)
        check("3 safety", "second LiveBotRunner refused with BotAlreadyRunningError (holder pid/mode/epoch readable)",
              ok and info and info["pid"] == os.getpid() and info["mode"] == "paper", det)
    finally:
        a.close()
    b = lb.LiveBotRunner("paper", market=FakeMarket(time.time, {}, {"ETHUSD": 2400, "BTCUSD": 81_600}), store_path=DB_LOCK,
                         lock_path=TEST_LOCK)
    b.close()
    check("3 safety", "lock released on close -> a new runner can start", True)


def paper_bot(signals: dict, prices: dict) -> tuple[lb.LiveBotRunner, FakeClock, FakeMarket]:
    fresh_db(DB_PAPER)
    fclock = FakeClock(us("2026-10-20 05:00:30"))
    market = FakeMarket(fclock.seconds, signals, prices)
    bot = lb.LiveBotRunner("paper", market=market, store_path=DB_PAPER, clock=fclock.seconds, lock_path=TEST_LOCK)
    bot.run_reconciliation("drill start")
    return bot, fclock, market


def minute(bot, fclock, prices, sym, t, o, h, l, c):
    fclock.t = (t + 62) * 1_000_000
    prices[sym] = c
    bot.on_minute_close(sym, t, o, h, l, c, "drill")


def test_streak() -> None:
    console.rule("[bold]3b. 4-consecutive-loss lock (paper)")
    H = lambda s: ts(f"2026-10-20 {s}")  # noqa: E731
    prices = {"ETHUSD": 2400.0, "BTCUSD": 81_600.0}
    sig = {("ETHUSD", H("06:00")): (1, 20.0), ("ETHUSD", H("09:00")): (1, 20.0), ("ETHUSD", H("12:00")): (1, 20.0),
           ("ETHUSD", H("15:00")): (1, 20.0), ("ETHUSD", H("16:00")): (1, 20.0), ("BTCUSD", H("14:00")): (1, 450.0)}
    bot, fc, _ = paper_bot(sig, prices)
    try:
        for hh in ("05", "08", "11"):
            minute(bot, fc, prices, "ETHUSD", H(f"{hh}:59"), 2400, 2400.5, 2399.5, 2400)
            minute(bot, fc, prices, "ETHUSD", H(f"{int(hh) + 1:02d}:00"), 2399, 2400, 2340, 2345)
        minute(bot, fc, prices, "BTCUSD", H("13:59"), 81_600, 81_605, 81_595, 81_600)
        minute(bot, fc, prices, "BTCUSD", H("14:00"), 81_600, 81_650, 81_550, 81_580)
        minute(bot, fc, prices, "ETHUSD", H("14:59"), 2400, 2400.5, 2399.5, 2400)
        btc_open = bot.store.position("paper", "BTCUSD") is not None
        minute(bot, fc, prices, "ETHUSD", H("15:00"), 2399, 2400, 2340, 2345)
        tr = bot.store.trades("paper")
        eth_losses = [t for t in tr if t["symbol"] == "ETHUSD"]
        check("3 safety", "4 consecutive ETH stop-outs -> HALTED until next UTC midnight",
              len(eth_losses) == 4 and all(t["net_pnl_inr"] < 0 for t in eth_losses) and bot.state == "HALTED" and bot.daily_halted,
              f"losses ₹{', '.join(f'{t['net_pnl_inr']:.2f}' for t in eth_losses)}; {bot.engine.halted()[1]}")
        btc = [t for t in tr if t["symbol"] == "BTCUSD"]
        check("3 safety", "open BTC exposure flattened by the lock", btc_open and len(btc) == 1 and btc[0]["exit_reason"] == "LOSS_STREAK"
              and not bot.store.positions("paper"), f"BTC closed {btc[0]['exit_reason'] if btc else '-'} ₹{btc[0]['net_pnl_inr']:.2f}" if btc else "")
        minute(bot, fc, prices, "ETHUSD", H("15:59"), 2400, 2400.5, 2399.5, 2400)
        check("3 safety", "next 1h signal (16:00) blocked while halted", any(b["hour_close"] == H("16:00") for b in bot.blocked_signals)
              and len(bot.store.trades("paper")) == 5 and not bot.store.positions("paper"))
        minute(bot, fc, prices, "ETHUSD", ts("2026-10-21 00:00"), 2400, 2400.5, 2399.5, 2400)
        check("3 safety", "halt expires at 00:00 UTC -> reconciliation -> OBSERVED_READY, streak counter reset",
              bot.state == "OBSERVED_READY" and not bot.daily_halted and bot.loss_streak() == 0)
    finally:
        bot.close()


def test_daily() -> None:
    console.rule("[bold]3c. 5% daily-loss circuit breaker (paper)")
    H = lambda s: ts(f"2026-10-20 {s}")  # noqa: E731
    prices = {"ETHUSD": 2400.0, "BTCUSD": 81_600.0}
    sig = {("ETHUSD", H("06:00")): (1, 20.0), ("BTCUSD", H("07:00")): (1, 450.0), ("ETHUSD", H("09:00")): (1, 20.0),
           ("ETHUSD", H("11:00")): (1, 20.0)}
    bot, fc, _ = paper_bot(sig, prices)
    try:
        minute(bot, fc, prices, "ETHUSD", H("05:59"), 2400, 2400.5, 2399.5, 2400)
        minute(bot, fc, prices, "ETHUSD", H("06:00"), 2399, 2400, 2340, 2345)        # loss 1 (SL)
        minute(bot, fc, prices, "BTCUSD", H("06:59"), 81_600, 81_605, 81_595, 81_600)  # BTC entry at 07:00
        minute(bot, fc, prices, "ETHUSD", H("08:59"), 2400, 2400.5, 2399.5, 2400)      # ETH entry at 09:00
        minute(bot, fc, prices, "BTCUSD", H("09:00"), 81_000, 81_050, 80_780, 80_784)  # BTC -1.0%, above its 80,700 stop
        day_start = bot.day_start_equity
        minute(bot, fc, prices, "ETHUSD", H("09:00"), 2184, 2190, 2180, 2185)          # ETH gaps 9% through its stop
        tr = bot.store.trades("paper")
        eq = bot.engine.realized_equity_inr()
        eth = [t for t in tr if t["symbol"] == "ETHUSD"]
        btc = [t for t in tr if t["symbol"] == "BTCUSD"]
        check("3 safety", "equity <= 95% of UTC start-of-day -> HALTED (daily_halted for the rest of the day)",
              bot.state == "HALTED" and bot.daily_halted and "daily loss" in (bot.engine.halted()[1] or ""),
              f"day start ₹{day_start:,.2f} -> ₹{eq:,.2f} ({(eq / day_start - 1) * 100:.2f}%); ETH gap exit at {eth[-1]['exit_price']:.2f}" if eth else "")
        check("3 safety", "open BTC position flattened (reason DAILY), no exposure left",
              len(btc) == 1 and btc[0]["exit_reason"] == "DAILY" and not bot.store.positions("paper"))
        minute(bot, fc, prices, "ETHUSD", H("10:59"), 2185, 2186, 2184, 2185)
        check("3 safety", "new 1h signal (11:00) blocked by the daily halt", any(b["hour_close"] == H("11:00") for b in bot.blocked_signals)
              and not bot.store.positions("paper"))
    finally:
        bot.close()


# --------------------------------------------------------------------------- #
# 4. CLI smoke + regression
# --------------------------------------------------------------------------- #
def run_script(args: list[str], timeout: int) -> tuple[int, str]:
    p = subprocess.run([sys.executable, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=PROJECT_ROOT, timeout=timeout, env={**os.environ, "PYTHONIOENCODING": "utf-8", "COLUMNS": "200"})
    return p.returncode, p.stdout + (("\n[stderr]\n" + p.stderr[-4000:]) if p.stderr.strip() else "")


def test_cli(skip_regression: bool) -> None:
    console.rule("[bold]4a. CLI smoke test: live_bot.py --mode paper --once (live Delta India data)")
    fresh_db(CLI_DB)
    rc, out = run_script(["live_bot.py", "--mode", "paper", "--once", "--db-path", str(CLI_DB), "--lock-path", str(CLI_LOCK),
                          "--control-dir", str(CLI_CONTROL)], 600)
    console.print(out, markup=False, highlight=False)
    check("4 cli", "live_bot.py --mode paper --once exits 0 in OBSERVED_READY", rc == 0 and "state OBSERVED_READY" in out, f"exit {rc}")
    if skip_regression:
        console.print("[yellow]regression skipped (--skip-regression)[/]")
        return
    console.rule("[bold]4b. Regression: step5_verify.py (re-runs step4_verify.py inside)")
    t0 = time.time()
    rc, out = run_script(["step5_verify.py"], 1800)
    console.print(out, markup=False, highlight=False)
    v4 = [ln.strip() for ln in out.splitlines() if "Step 4 verification:" in ln and "──" in ln]
    v5 = [ln.strip() for ln in out.splitlines() if "Step 5 verification:" in ln]
    check("4 regression", "step4_verify.py PASS - 77 checks", bool(v4) and "PASS - 77 checks" in v4[-1], v4[-1].strip("─ ") if v4 else "")
    check("4 regression", "step5_verify.py PASS - 92 checks", rc == 0 and bool(v5) and "PASS - 92 checks" in v5[-1],
          f"{v5[-1].strip('─ ') if v5 else ''} ({time.time() - t0:.0f}s)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-regression", action="store_true")
    ap.add_argument("--skip-live", action="store_true", help="skip the live WebSocket probe and CLI smoke test")
    args = ap.parse_args()
    console.print(f"MODE={settings.MODE!r}  LIVE_TRADING_ENABLED={settings.LIVE_TRADING_ENABLED}  EXECUTION_MODE={settings.EXECUTION_MODE!r}")
    if not args.skip_live:
        test_live_probe()
    test_units()
    asyncio.run(testnet_drill())
    asyncio.run(watchdog_drill())
    test_lock()
    test_streak()
    test_daily()
    if not args.skip_live:
        test_cli(args.skip_regression)

    console.rule("[bold]Summary")
    t = Table()
    for col in ("section", "check", "result", "detail"):
        t.add_column(col)
    for sec, name, ok, detail in CHECKS:
        t.add_row(sec, name, "[green]PASS" if ok else "[red]FAIL", detail[:100])
    console.print(t)
    failed = [c for c in CHECKS if not c[2]]
    console.rule(f"Step 6 verification: {'[green]PASS' if not failed else f'[red]FAIL ({len(failed)} checks)'} - {len(CHECKS)} checks")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
