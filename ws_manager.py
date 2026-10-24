"""Delta Exchange India WebSocket feed manager (Step 6).

Sockets
  public   paper/live: wss://public-socket.india.delta.exchange   testnet: wss://socket-ind-pub.testnet.deltaex.org
  private  live: wss://socket.india.delta.exchange                testnet: wss://socket-ind.testnet.deltaex.org
           (never opened in paper mode)

Connection generation (one integer per attempt, both sockets):
  CONNECTING -> enable_heartbeat on every socket -> AUTHENTICATING (private: key-auth, wait for
  {"type":"key-auth","success":true,"status":"authenticated"}) -> SUBSCRIBING (wait until every required
  channel / symbol is acknowledged or errored) -> SNAPSHOTTING (private: first orders/positions snapshot)
  -> READY, or DEGRADED_REST_FALLBACK when any required subscription errored / was never acknowledged
  (handler.on_ws_degraded gets the per-socket problems; the runner then reconciles over REST and, in
  testnet/live, blocks entries while private channels are missing). Then a read loop with a 35s watchdog
  (server heartbeat every 30s + 5s): silence closes both sockets and a new generation starts.
  Frames tagged with an older generation are dropped.

Live wire facts (probed 2026-10-20 on the India public socket):
  * candlestick_1m / ob_l1 / mark_price / spot_price / ticker arrive with compact keys
    (cst, o, h, l, c, v, sy, ts ...); cst is in microseconds.
  * "v2/ticker" and "v2/spot_price" are rejected ("subscription forbidden on this invalid channel");
    "ticker" (batched {"d": [...]}) and "spot_price" work.
  * symbols ["all"] IS accepted for candlestick_1m and floods every product -> refused here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from config import ENVIRONMENTS, settings
from exchange_client import sign_request

log = logging.getLogger(__name__)

WATCHDOG_SECONDS = 35.0
HANDSHAKE_TIMEOUT_SECONDS = 15.0
SNAPSHOT_TIMEOUT_SECONDS = 10.0
RECONNECT_BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)

SYMBOL_MANDATORY = {"orders", "positions", "v2/user_trades", "candlestick_1m", "v2/ticker", "ticker", "mark_price",
                    "ob_l1", "ob_l2", "ob_updates", "spot_price", "v2/spot_price"}
PRIVATE_CHANNELS = {"orders", "positions", "v2/user_trades", "margins"}
SNAPSHOT_CHANNELS = ("orders", "positions")

PHASES = ("CONNECTING", "AUTHENTICATING", "SUBSCRIBING", "SNAPSHOTTING", "READY", "DEGRADED_REST_FALLBACK")


class SubscriptionError(ValueError):
    pass


class AuthError(RuntimeError):
    pass


class WatchdogTimeout(RuntimeError):
    pass


class ConnectionLost(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Pure helpers (normalizers, validation)
# --------------------------------------------------------------------------- #
def ws_urls(mode: str) -> tuple[str, str | None]:
    if mode == "testnet":
        env = ENVIRONMENTS["india_testnet"]
        return env.public_ws_url, env.private_ws_url
    env = ENVIRONMENTS["india_prod"]
    return env.public_ws_url, (env.private_ws_url if mode == "live" else None)


def validate_channels(channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Refuse wildcard / missing symbols on symbol-mandatory and snapshot-bearing channels."""
    for ch in channels:
        name, syms = ch.get("name"), ch.get("symbols")
        if not name:
            raise SubscriptionError(f"channel without a name: {ch}")
        if name in SYMBOL_MANDATORY:
            if not syms:
                raise SubscriptionError(f"{name}: explicit symbols are mandatory")
            if any(str(s).lower() == "all" for s in syms):
                raise SubscriptionError(f"{name}: symbols ['all'] is refused (explicit symbols only)")
    return channels


def subscribe_message(channels: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "subscribe", "payload": {"channels": validate_channels(channels)}}


def key_auth_message(api_key: str, api_secret: str, timestamp: str) -> dict[str, Any]:
    """Current private-socket handshake: signature = HMAC_SHA256(secret, 'GET' + timestamp + '/live')."""
    return {"type": "key-auth", "payload": {"api-key": api_key, "timestamp": timestamp,
                                            "signature": sign_request(api_secret, "GET", timestamp, "/live")}}


def _f(x: Any) -> float | None:
    return None if x in (None, "") else float(x)


@dataclass(frozen=True)
class Candle:
    symbol: str
    start: int          # Unix seconds, candle open
    open: float
    high: float
    low: float
    close: float
    volume: float
    ts_us: int | None = None
    schema: str = "compact"   # compact | verbose | rest
    source: str = "ws"        # ws | rest_backfill | rest_poll


def normalize_candle(m: dict[str, Any]) -> Candle | None:
    """candlestick_1m in either compact wire keys (cst,o,h,l,c,v,sy,ts) or verbose doc keys."""
    if "cst" in m or "sy" in m:
        start, sym, schema = m.get("cst"), m.get("sy"), "compact"
        o, h, l, c, v, ts = m.get("o"), m.get("h"), m.get("l"), m.get("c"), m.get("v"), m.get("ts")
    else:
        start, sym, schema = m.get("candle_start_time"), m.get("symbol"), "verbose"
        o, h, l, c, v, ts = m.get("open"), m.get("high"), m.get("low"), m.get("close"), m.get("volume"), m.get("timestamp")
    if start is None or sym is None or o is None:
        return None
    start = int(start)
    if start > 10**14:      # microseconds
        start //= 1_000_000
    elif start > 10**11:    # milliseconds
        start //= 1_000
    return Candle(sym, start, float(o), float(h), float(l), float(c), float(v or 0.0),
                  int(ts) if ts is not None else None, schema)


def normalize_ticker(m: dict[str, Any]) -> list[dict[str, Any]]:
    """'ticker' batched compact frames ({'d': [...]}) or verbose v2/ticker frames -> common dicts."""
    out = []
    if isinstance(m.get("d"), list):
        for d in m["d"]:
            pb = d.get("pb") or [None, None]
            out.append({"symbol": d.get("s") or m.get("sy"), "product_id": d.get("i"), "mark_price": _f(d.get("m")),
                        "spot_price": _f(m.get("sp")), "band_lower": _f(pb[0]), "band_upper": _f(pb[1]),
                        "ts_us": m.get("ts"), "schema": "compact"})
        return out
    q, pb = m.get("quotes") or {}, m.get("price_band") or {}
    out.append({"symbol": m.get("symbol"), "product_id": m.get("product_id"), "mark_price": _f(m.get("mark_price")),
                "spot_price": _f(m.get("spot_price")), "best_bid": _f(q.get("best_bid")), "best_ask": _f(q.get("best_ask")),
                "band_lower": _f(pb.get("lower_limit")), "band_upper": _f(pb.get("upper_limit")),
                "ts_us": m.get("timestamp"), "schema": "verbose"})
    return out


def normalize_quote(m: dict[str, Any]) -> dict[str, Any]:
    """ob_l1: compact (bp, bs, ap, as, sy) or verbose (best_bid, best_ask, symbol)."""
    if "sy" in m:
        return {"symbol": m["sy"], "best_bid": _f(m.get("bp")), "bid_size": _f(m.get("bs")), "best_ask": _f(m.get("ap")),
                "ask_size": _f(m.get("as")), "ts_us": m.get("ts"), "schema": "compact"}
    return {"symbol": m.get("symbol"), "best_bid": _f(m.get("best_bid")), "best_ask": _f(m.get("best_ask")),
            "bid_size": _f(m.get("bid_qty")), "ask_size": _f(m.get("ask_qty")), "ts_us": m.get("timestamp"), "schema": "verbose"}


class CandleCloser:
    """Per-symbol: a candle is final once that symbol's candle_start_time advances. Skipped minutes are backfilled."""

    def __init__(self, backfill: Callable[[str, int, int], list[Candle]] | None = None):
        self.backfill = backfill
        self.current: dict[str, Candle] = {}
        self.stale_updates = 0
        self.backfilled_minutes = 0
        self.unfilled_minutes = 0

    def feed(self, c: Candle) -> list[Candle]:
        prev = self.current.get(c.symbol)
        if prev is None or c.start == prev.start:
            self.current[c.symbol] = c
            return []
        if c.start < prev.start:
            self.stale_updates += 1
            return []
        closed = [prev]
        if c.start > prev.start + 60:
            missing = list(range(prev.start + 60, c.start, 60))
            got: dict[int, Candle] = {}
            if self.backfill is not None:
                try:
                    got = {b.start: b for b in self.backfill(c.symbol, missing[0], missing[-1])}
                except Exception as exc:  # noqa: BLE001 - the bot's REST poller covers the hole
                    log.warning("%s backfill %s..%s failed: %s", c.symbol, missing[0], missing[-1], exc)
            for t in missing:
                if t in got:
                    closed.append(got[t])
                    self.backfilled_minutes += 1
                else:
                    self.unfilled_minutes += 1
        self.current[c.symbol] = c
        return closed


class SequenceTracker:
    """Per (channel, symbol) sequence numbers on private streams; gaps / resets are anomalies."""

    def __init__(self):
        self.last: dict[tuple[str, str], int] = {}

    def reset(self) -> None:
        self.last.clear()

    def feed(self, channel: str, symbol: str, seq: int | None, snapshot: bool) -> dict[str, Any] | None:
        if seq is None:
            return None
        key = (channel, symbol)
        prev = self.last.get(key)
        self.last[key] = seq
        if snapshot or prev is None:
            return None
        if seq == prev + 1:
            return None
        kind = "reset" if seq == 1 else ("gap" if seq > prev + 1 else "regression")
        return {"channel": channel, "symbol": symbol, "prev_seq": prev, "seq": seq, "kind": kind}


def message_seq(channel: str, m: dict[str, Any]) -> int | None:
    for k in (("se",) if channel == "v2/user_trades" else ("seq_no", "seq_id")):
        if m.get(k) is not None:
            return int(m[k])
    return None


def message_symbol(m: dict[str, Any]) -> str:
    return str(m.get("symbol") or m.get("product_symbol") or m.get("sy") or "*")


def normalize_private(channel: str, m: dict[str, Any]) -> list[dict[str, Any]]:
    """orders / positions / v2/user_trades frames -> one dict per item. Snapshot envelopes ({"action": "snapshot",
    "result": [...]}) are unpacked; order ids are exposed as both 'id' and 'order_id' (the docs' wire key is
    order_id), symbols as 'product_symbol', compact trade keys (sy, p, s/q, oid) as symbol / price / size / order_id."""
    snapshot = m.get("action") == "snapshot" or str(m.get("type", "")).endswith("snapshot")
    items = m["result"] if isinstance(m.get("result"), list) else [m]
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        x = {**it, "type": channel, "snapshot": snapshot}
        x["product_symbol"] = it.get("product_symbol") or it.get("symbol") or it.get("sy") or m.get("symbol") or m.get("sy")
        if channel == "orders":
            oid = it.get("order_id") if it.get("order_id") is not None else it.get("id")
            x["id"] = x["order_id"] = oid
        elif channel == "positions":
            x["size"] = int(it.get("size") or 0)
        else:  # v2/user_trades
            x["order_id"] = it.get("order_id") or it.get("oid") or it.get("o")
            x["price"] = _f(it.get("price") if it.get("price") is not None else it.get("p"))
            sz = next((it.get(k) for k in ("size", "s", "q") if it.get(k) is not None), None)
            x["size"] = None if sz is None else int(float(sz))
        out.append(x)
    return out


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
Connector = Callable[[str], Awaitable[Any]]


async def default_connector(url: str) -> Any:
    import websockets
    return await websockets.connect(url, user_agent_header=settings.USER_AGENT, open_timeout=HANDSHAKE_TIMEOUT_SECONDS,
                                    ping_interval=20, ping_timeout=20, max_size=2**23)


@dataclass
class WSStats:
    messages: dict[str, int] = field(default_factory=dict)
    wire_keys: dict[str, list[str]] = field(default_factory=dict)
    schemas: dict[str, int] = field(default_factory=dict)
    acked: dict[str, list[str]] = field(default_factory=dict)
    acked_symbols: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # socket -> channel -> symbols
    subscription_errors: list[dict[str, Any]] = field(default_factory=list)
    subscription_problems: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # current generation
    dropped_stale_generation: int = 0
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    heartbeats: int = 0
    closed_candles: int = 0
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #
class DeltaWebSocketManager:
    def __init__(
        self,
        mode: str,
        symbols: Iterable[str],
        handler: Any = None,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        connector: Connector = default_connector,
        backfill: Callable[[str, int, int], list[Candle]] | None = None,
        public_channels: list[dict[str, Any]] | None = None,
        private_channels: list[dict[str, Any]] | None = None,
        watchdog_s: float = WATCHDOG_SECONDS,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        offload: bool = False,
    ):
        self.mode = mode
        # offload=True: frame handling and handler callbacks run in a worker thread (asyncio.to_thread), so the
        # handler's synchronous REST / SQLite work never blocks the event loop (heartbeats, watchdog, pings).
        self.offload = offload
        self.degraded: dict[str, list[dict[str, Any]]] = {}
        self.symbols = list(symbols)
        self.handler = handler
        self.public_url, self.private_url = ws_urls(mode)
        self.use_private = mode in ("testnet", "live")
        if self.use_private and not (api_key and api_secret):
            raise AuthError(f"{mode} WebSocket needs API key + secret for the private socket")
        self._key, self._secret = api_key or "", api_secret or ""
        self.connector, self.clock, self.sleep = connector, clock, sleep
        self.watchdog_s = watchdog_s
        mark = [f"MARK:{s}" for s in self.symbols]
        self.public_channels = validate_channels(public_channels if public_channels is not None else [
            {"name": "candlestick_1m", "symbols": self.symbols + mark},
            {"name": "ticker", "symbols": self.symbols},
            {"name": "ob_l1", "symbols": self.symbols},
            {"name": "system_status"},
        ])
        self.private_channels = validate_channels(private_channels if private_channels is not None else [
            {"name": "orders", "symbols": self.symbols},
            {"name": "positions", "symbols": self.symbols},
            {"name": "v2/user_trades", "symbols": self.symbols},
        ]) if self.use_private else []
        self.closer = CandleCloser(backfill)
        self.seq = SequenceTracker()
        self.stats = WSStats()
        self.generation = 0
        self.phase = "IDLE"
        self.authenticated = False
        self.auth_response: dict[str, Any] | None = None
        self.snapshots: set[str] = set()
        self.last_quote: dict[str, dict[str, Any]] = {}
        self.last_ticker: dict[str, dict[str, Any]] = {}
        self._conns: dict[str, Any] = {}
        self._queue: asyncio.Queue | None = None
        self._readers: list[asyncio.Task] = []
        self._reconnect_reason: str | None = None
        self._last_rx = 0.0
        self.sent: list[tuple[int, str, dict[str, Any]]] = []  # (generation, socket, message) for audits

    # -- handler plumbing ---------------------------------------------------------- #
    def _emit(self, name: str, *args: Any) -> None:
        fn = getattr(self.handler, name, None)
        if fn is not None:
            fn(*args)

    async def _aemit(self, name: str, *args: Any) -> None:
        if self.offload:
            await asyncio.to_thread(self._emit, name, *args)
        else:
            self._emit(name, *args)

    async def _set_phase(self, phase: str) -> None:
        self.phase = phase
        await self._aemit("on_ws_phase", phase, self.generation)

    def invalidate(self, reason: str) -> int:
        """Make every frame of the current connection stale and request a fresh connection generation."""
        self.generation += 1
        self._reconnect_reason = reason
        self.seq.reset()
        log.warning("ws generation invalidated -> %d (%s)", self.generation, reason)
        return self.generation

    # -- message processing (synchronous, deterministic) ---------------------------- #
    def handle_message(self, m: dict[str, Any], generation: int, source: str = "public") -> None:
        if generation != self.generation:
            self.stats.dropped_stale_generation += 1
            return
        self._last_rx = self.clock()
        t = str(m.get("type", "?"))
        st = self.stats
        st.messages[t] = st.messages.get(t, 0) + 1
        if t not in st.wire_keys:
            st.wire_keys[t] = sorted(m.keys())
        if t == "heartbeat":
            st.heartbeats += 1
        elif t == "subscriptions":
            for ch in m.get("channels") or []:
                if ch.get("error"):
                    st.subscription_errors.append({"socket": source, "generation": generation, **ch})
                    self._emit("on_subscription_error", source, ch)
                else:
                    st.acked.setdefault(source, []).append(ch.get("name"))
                    got = st.acked_symbols.setdefault(source, {})
                    # an ack that does not list symbols acknowledges the whole channel ("*")
                    syms = ch.get("symbols") if ch.get("symbols") is not None else ["*"]
                    got[ch.get("name")] = sorted(set(got.get(ch.get("name"), [])) | set(syms))
        elif t == "key-auth":
            self.auth_response = m
            self.authenticated = bool(m.get("success")) and m.get("status") == "authenticated"
        elif t == "error":
            st.errors.append(json.dumps(m)[:300])
        elif t == "candlestick_1m":
            c = normalize_candle(m)
            if c is None:
                return
            st.schemas[c.schema] = st.schemas.get(c.schema, 0) + 1
            for closed in self.closer.feed(c):
                st.closed_candles += 1
                self._emit("on_closed_1m_candle", closed)
        elif t in ("ticker", "v2/ticker"):
            for tk in normalize_ticker(m):
                self.last_ticker[tk["symbol"]] = tk
                self._emit("on_ticker", tk)
        elif t == "ob_l1":
            q = normalize_quote(m)
            self.last_quote[q["symbol"]] = {**q, "rx": self._last_rx}
            self._emit("on_quote", q)
        elif t in PRIVATE_CHANNELS or t in ("v2/user_trades",):
            snapshot = m.get("action") == "snapshot" or m.get("type", "").endswith("snapshot")
            if snapshot:
                self.snapshots.add(t)
            anomaly = self.seq.feed(t, message_symbol(m), message_seq(t, m), snapshot)
            for item in normalize_private(t, m):
                self._emit({"orders": "on_order", "positions": "on_position"}.get(t, "on_user_trade"), item)
            if anomaly is not None:
                anomaly["generation"] = generation
                st.anomalies.append(anomaly)
                self._emit("on_sequence_anomaly", anomaly)
        elif t == "system_status":
            self._emit("on_system_status", m)

    # -- async transport -------------------------------------------------------------- #
    async def _send(self, socket: str, msg: dict[str, Any]) -> None:
        self.sent.append((self.generation, socket, msg))
        await self._conns[socket].send(json.dumps(msg))

    async def _reader(self, gen: int, socket: str, conn: Any, q: asyncio.Queue) -> None:
        try:
            while True:
                raw = await conn.recv()
                await q.put((gen, socket, raw))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any transport failure ends the generation
            await q.put((gen, socket, ConnectionLost(f"{socket}: {type(exc).__name__}: {exc}")))

    async def _pump(self, until: Callable[[], bool], timeout_s: float, what: str) -> None:
        """Dispatch incoming frames until ``until()`` holds (raise on timeout / lost connection)."""
        deadline = self.clock() + timeout_s
        while not until():
            remaining = min(deadline, self._last_rx + self.watchdog_s) - self.clock()
            if remaining <= 0:
                if self.clock() >= self._last_rx + self.watchdog_s:
                    raise WatchdogTimeout(f"no frame for {self.watchdog_s:.0f}s")
                raise TimeoutError(f"timed out waiting for {what}")
            try:
                gen, socket, raw = await asyncio.wait_for(self._queue.get(), timeout=min(remaining, 0.25))
            except asyncio.TimeoutError:
                continue
            if isinstance(raw, Exception):
                if gen == self.generation:
                    raise raw
                continue
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                self.stats.errors.append(f"non-JSON frame on {socket}")
                continue
            if self.offload:  # handler work (REST / SQLite) off the event loop; frames stay strictly ordered
                await asyncio.to_thread(self.handle_message, msg, gen, socket)
            else:
                self.handle_message(msg, gen, socket)
            if self._reconnect_reason is not None and gen != self.generation:
                return

    async def connect_once(self) -> int:
        """Open both sockets for a new generation and run the handshake up to READY."""
        await self.close_sockets()
        self.generation += 1
        gen = self.generation
        self._reconnect_reason = None
        self.seq.reset()
        self.closer.current.clear()  # a partial candle from the previous generation is not trusted
        self.snapshots.clear()
        self.authenticated = False
        self.auth_response = None
        self.stats.acked.clear()
        self.stats.acked_symbols.clear()
        self.stats.subscription_problems = {}
        self._queue = asyncio.Queue()
        await self._set_phase("CONNECTING")
        self._conns["public"] = await self.connector(self.public_url)
        if self.use_private:
            self._conns["private"] = await self.connector(self.private_url)
        self._last_rx = self.clock()
        self._readers = [asyncio.create_task(self._reader(gen, s, c, self._queue)) for s, c in self._conns.items()]
        for s in self._conns:
            await self._send(s, {"type": "enable_heartbeat"})
        await self._set_phase("AUTHENTICATING")
        if self.use_private:
            await self._send("private", key_auth_message(self._key, self._secret, str(int(self.clock()))))
            await self._pump(lambda: self.auth_response is not None, HANDSHAKE_TIMEOUT_SECONDS, "key-auth")
            if not self.authenticated:
                raise AuthError(f"private socket key-auth failed: {self.auth_response}")
        await self._set_phase("SUBSCRIBING")
        await self._send("public", subscribe_message(self.public_channels))
        if self.use_private:
            await self._send("private", subscribe_message(self.private_channels))
        try:  # every required channel / symbol acknowledged or errored (F10); a timeout leaves the rest missing
            await self._pump(lambda: not any(p.get("pending") for ps in self.subscription_problems(gen).values() for p in ps),
                             HANDSHAKE_TIMEOUT_SECONDS, "subscriptions ack")
        except TimeoutError:
            self.stats.errors.append("subscription acknowledgement timeout")
        problems = {s: ps for s, ps in self.subscription_problems(gen).items() if ps}
        self.stats.subscription_problems = problems
        await self._set_phase("SNAPSHOTTING")
        if self.use_private:
            try:
                await self._pump(lambda: all(c in self.snapshots for c in SNAPSHOT_CHANNELS), SNAPSHOT_TIMEOUT_SECONDS,
                                 "private snapshots")
            except TimeoutError:
                self.stats.errors.append("snapshot timeout (REST reconciliation covers it)")
        self.degraded = problems
        if problems:  # never report READY for streams that were not confirmed
            log.warning("ws generation %d subscriptions not confirmed: %s", gen, problems)
            await self._set_phase("DEGRADED_REST_FALLBACK")
            await self._aemit("on_ws_degraded", gen, problems)
        else:
            await self._set_phase("READY")
        await self._aemit("on_ws_ready", gen)
        return gen

    def required_channels(self, socket: str) -> list[dict[str, Any]]:
        return self.public_channels if socket == "public" else self.private_channels

    def subscription_problems(self, generation: int | None = None) -> dict[str, list[dict[str, Any]]]:
        """Per socket: required channels that errored, or still lack an acknowledgement for some symbols
        ('pending': True while the handshake may still deliver it)."""
        gen = self.generation if generation is None else generation
        out: dict[str, list[dict[str, Any]]] = {}
        for socket in self._conns or {"public": None, **({"private": None} if self.use_private else {})}:
            errs = {e.get("name"): e.get("error") for e in self.stats.subscription_errors
                    if e.get("socket") == socket and e.get("generation") == gen}
            got = self.stats.acked_symbols.get(socket, {})
            acked = set(self.stats.acked.get(socket, []))
            probs = []
            for ch in self.required_channels(socket):
                name = ch["name"]
                if name in errs:
                    probs.append({"channel": name, "error": errs[name]})
                    continue
                have = set(got.get(name, []))
                missing = [] if "*" in have else sorted(set(ch.get("symbols") or []) - have)
                if name not in acked or missing:
                    probs.append({"channel": name, "missing_symbols": missing if name in acked else ch.get("symbols") or [],
                                  "pending": True})
            out[socket] = probs
        return out

    async def read_loop(self, stop: asyncio.Event) -> str:
        """Dispatch frames until stop / reconnect request / watchdog / lost socket. Returns the reason."""
        gen = self.generation
        try:
            await self._pump(lambda: stop.is_set() or self._reconnect_reason is not None or gen != self.generation,
                             float("inf"), "frames")
            return "stop" if stop.is_set() else f"reconnect: {self._reconnect_reason}"
        except WatchdogTimeout as exc:
            return f"watchdog: {exc}"
        except ConnectionLost as exc:
            return f"connection lost: {exc}"

    async def close_sockets(self) -> None:
        for t in self._readers:
            t.cancel()
        for t in self._readers:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._readers = []
        for c in self._conns.values():
            try:
                await c.close()
            except Exception:  # noqa: BLE001
                pass
        self._conns = {}

    async def run(self, stop: asyncio.Event) -> None:
        """Connect, read, and reconnect with backoff (new generation each time) until ``stop`` is set."""
        failures = 0
        while not stop.is_set():
            reason = ""
            try:
                gen = await self.connect_once()
                failures = 0
                reason = await self.read_loop(stop)
            except (AuthError, TimeoutError, WatchdogTimeout, ConnectionLost, OSError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
                failures += 1
            except Exception as exc:  # noqa: BLE001 - e.g. websockets.InvalidStatus
                reason = f"{type(exc).__name__}: {exc}"
                failures += 1
            await self.close_sockets()
            if stop.is_set():
                break
            await self._aemit("on_ws_disconnect", self.generation, reason)
            delay = RECONNECT_BACKOFF[min(max(failures - 1, 0), len(RECONNECT_BACKOFF) - 1)] if failures else 0.5
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def snapshot(self) -> dict[str, Any]:
        return {"generation": self.generation, "phase": self.phase, "degraded": self.degraded, **asdict(self.stats)}


def rest_backfill(client: Any) -> Callable[[str, int, int], list[Candle]]:
    """REST GET /v2/history/candles backfill for minutes the socket skipped."""
    def fn(symbol: str, start_s: int, end_s: int) -> list[Candle]:
        rows = client.get_history_candles(symbol, start_s, end_s)
        return [Candle(symbol, int(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                       float(r.get("volume") or 0), None, "rest", "rest_backfill") for r in rows]
    return fn
