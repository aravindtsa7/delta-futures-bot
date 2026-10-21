"""Delta Exchange India V2 REST adapter (Step 5).

Environment isolation
  paper    public endpoints of https://api.india.delta.exchange only; every authenticated call raises.
  testnet  https://cdn-ind.testnet.deltaex.org with DELTA_TESTNET_API_KEY / _SECRET.
  live     https://api.india.delta.exchange with DELTA_LIVE_API_KEY / _SECRET, and only when
           settings.MODE == "live" AND settings.LIVE_TRADING_ENABLED (checked at construction).

Wire rules
  * signature = HMAC_SHA256(secret, method + timestamp + path + query_string + body) where
    query_string includes the leading "?" and body is the exact JSON string sent on the wire.
    The request URL and body bytes are built from those same strings, so signed == sent.
  * Safe GETs retry with bounded exponential backoff, re-signing each attempt (5s validity).
  * POST / PUT / DELETE are sent exactly once. A timeout / network error / 5xx after sending
    raises AmbiguousOutcomeError: the order may or may not exist, so the caller must reconcile
    (by client_order_id) instead of resending.
  * One shared 20,000-weight / 5-minute budget per base URL; HTTP 429 blocks the budget until
    X-RATE-LIMIT-RESET (milliseconds) has elapsed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Iterable, Literal
from urllib.parse import urlencode

import httpx

from config import ENVIRONMENTS, settings

log = logging.getLogger(__name__)

ExecMode = Literal["paper", "testnet", "live"]

RATE_LIMIT_CAPACITY = 20_000
RATE_LIMIT_WINDOW_SECONDS = 300.0
MAX_GET_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 8.0
MAX_CLIENT_OID_LEN = 32
MAX_PAGES = 200
CLOSE_POLL_ATTEMPTS = 5
CLOSE_POLL_SECONDS = 1.0


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ExchangeError(Exception):
    """Base class for adapter errors."""


class LiveTradingDisabledError(ExchangeError):
    """Live mode requested without MODE='live' AND LIVE_TRADING_ENABLED=True."""


class ReadOnlyClientError(ExchangeError):
    """A mutation was attempted on a read-only client (refused before anything was sent)."""


class MissingCredentialsError(ExchangeError):
    """An authenticated endpoint was called without credentials for this environment."""


class DeltaAPIError(ExchangeError):
    """Definite rejection: the exchange answered with an error (4xx or success=false)."""

    def __init__(self, status: int, method: str, path: str, error: Any, body: Any = None):
        self.status, self.method, self.path, self.error, self.body = status, method, path, error, body
        code = error.get("code") if isinstance(error, dict) else error
        self.code = code
        super().__init__(f"HTTP {status} {method} {path}: {error}")


class RateLimitedError(ExchangeError):
    """HTTP 429. The request was rejected (not executed); retry_after_s says when the budget reopens."""

    def __init__(self, method: str, path: str, retry_after_s: float):
        self.retry_after_s = retry_after_s
        super().__init__(f"429 rate limited on {method} {path}; reset in {retry_after_s:.2f}s")


class TransientReadError(ExchangeError):
    """A safe GET kept failing after bounded retries."""


class AmbiguousOutcomeError(ExchangeError):
    """A mutation was sent but its outcome is unknown (timeout / network error / 5xx). Never auto-retried."""

    def __init__(self, method: str, path: str, body: str, cause: BaseException | str):
        self.method, self.path, self.body, self.cause = method, path, body, cause
        super().__init__(f"outcome unknown for {method} {path}: {cause!r}")


class OrderValidationError(ExchangeError):
    """Rejected client-side before anything was sent."""


class OrderPriceMismatchError(ExchangeError):
    """The exchange acknowledged an order whose price differs from the tick-snapped intended price."""

    def __init__(self, order: dict[str, Any], mismatches: dict[str, tuple[str, Any]]):
        self.order, self.mismatches = order, mismatches
        super().__init__(f"order {order.get('id')} price mismatch (field: intended, returned): {mismatches}")


class ProductNotTradableError(ExchangeError):
    pass


# --------------------------------------------------------------------------- #
# Signing / encoding helpers
# --------------------------------------------------------------------------- #
def sign_request(secret: str, method: str, timestamp: str, path: str, query_string: str = "", body: str = "") -> str:
    """Delta signature: hex HMAC-SHA256 of method + timestamp + path + query_string + body."""
    message = method + timestamp + path + query_string + body
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def encode_query(params: dict[str, Any] | None) -> str:
    """Query string WITH the leading '?', or '' — exactly the bytes signed and sent."""
    if not params:
        return ""
    items = []
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        items.append((k, str(v)))
    return "?" + urlencode(items, safe=",") if items else ""


def encode_body(body: dict[str, Any] | None) -> str:
    return "" if body is None else json.dumps(body, separators=(",", ":"))


def snap_to_tick(price: Any, tick_size: Any) -> Decimal:
    """Nearest multiple of tick_size (half-up), exact in Decimal."""
    p, t = Decimal(str(price)), Decimal(str(tick_size))
    if t <= 0:
        raise ValueError("tick_size must be > 0")
    return ((p / t).to_integral_value(rounding=ROUND_HALF_UP) * t).quantize(t)


def validate_client_oid(client_order_id: str | None) -> None:
    if client_order_id is None:
        return
    if not isinstance(client_order_id, str) or not client_order_id:
        raise OrderValidationError("client_order_id must be a non-empty string")
    if len(client_order_id) > MAX_CLIENT_OID_LEN:
        raise OrderValidationError(f"client_order_id {client_order_id!r} is {len(client_order_id)} chars (> {MAX_CLIENT_OID_LEN})")


def endpoint_weight(method: str, path: str) -> int:
    """Delta's documented REST weights (unlisted endpoints cost 1)."""
    p = path.split("?")[0]
    if method in ("POST", "PUT", "DELETE"):
        if p.startswith("/v2/orders/batch"):
            return 25
        if p in ("/v2/orders", "/v2/orders/bracket") or p.startswith("/v2/positions/change_margin"):
            return 5
        return 1
    if p.startswith(("/v2/orders/history", "/v2/fills", "/v2/wallet/transactions")):
        return 10
    if p.startswith(("/v2/products", "/v2/l2orderbook", "/v2/tickers", "/v2/orders", "/v2/positions",
                     "/v2/wallet/balances", "/v2/history/candles")):
        return 3
    return 1


# --------------------------------------------------------------------------- #
# Shared rate-limit budget
# --------------------------------------------------------------------------- #
class RateLimitBudget:
    """Fixed 5-minute window of 20,000 weight units, shared by every client on one base URL."""

    def __init__(self, capacity: int = RATE_LIMIT_CAPACITY, window_s: float = RATE_LIMIT_WINDOW_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.capacity, self.window_s, self.clock = capacity, window_s, clock
        self.window_start = clock()
        self.used = 0
        self.blocked_until = 0.0
        self.total_weight = 0
        self.n_429 = 0
        self._lock = threading.Lock()

    def wait_time(self, weight: int) -> float:
        """Seconds to wait before ``weight`` may be spent (0 = now); reserves it when 0."""
        with self._lock:
            now = self.clock()
            if now >= self.window_start + self.window_s:
                self.window_start, self.used = now, 0
            if self.blocked_until > now:
                return self.blocked_until - now
            if self.used + weight > self.capacity:
                return self.window_start + self.window_s - now
            self.used += weight
            self.total_weight += weight
            return 0.0

    def acquire(self, weight: int, sleep: Callable[[float], None]) -> None:
        while (w := self.wait_time(weight)) > 0:
            log.warning("rate budget: waiting %.2fs (used %d/%d)", w, self.used, self.capacity)
            sleep(w)

    def note_429(self, reset_header: str | None) -> float:
        """Block the budget until the server's reset time; returns seconds to wait."""
        wait = 1.0
        if reset_header:
            try:
                wait = max(float(reset_header) / 1000.0, 0.05)
            except ValueError:
                pass
        with self._lock:
            self.n_429 += 1
            self.blocked_until = max(self.blocked_until, self.clock() + wait)
        return wait

    def snapshot(self) -> dict[str, Any]:
        return {"used": self.used, "capacity": self.capacity, "total_weight": self.total_weight, "n_429": self.n_429,
                "blocked_for_s": max(0.0, self.blocked_until - self.clock())}


_BUDGETS: dict[str, RateLimitBudget] = {}
_BUDGETS_LOCK = threading.Lock()


def shared_budget(base_url: str) -> RateLimitBudget:
    with _BUDGETS_LOCK:
        if base_url not in _BUDGETS:
            _BUDGETS[base_url] = RateLimitBudget()
        return _BUDGETS[base_url]


# --------------------------------------------------------------------------- #
# Product info
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ProductInfo:
    id: int
    symbol: str
    contract_value: Decimal
    tick_size: Decimal
    price_band_pct: Decimal | None
    state: str
    trading_status: str
    raw: dict[str, Any] = field(repr=False, compare=False, default_factory=dict)

    @property
    def tradable(self) -> bool:
        return self.state == "live" and self.trading_status == "operational"

    @classmethod
    def from_api(cls, r: dict[str, Any]) -> "ProductInfo":
        band = r.get("price_band")
        return cls(int(r["id"]), r["symbol"], Decimal(str(r["contract_value"])), Decimal(str(r["tick_size"])),
                   Decimal(str(band)) if band not in (None, "") else None, r.get("state", ""),
                   r.get("trading_status", ""), r)


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class DeltaRestClientAdapter:
    def __init__(
        self,
        mode: ExecMode = "paper",
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        budget: RateLimitBudget | None = None,
        timeout: float | None = None,
        read_only: bool = False,
    ):
        if mode not in ("paper", "testnet", "live"):
            raise ValueError(f"unknown mode {mode!r}")
        self.read_only = read_only
        # A read-only client may authenticate against the live server (balances / positions diagnostics) even
        # while live trading is disabled: every POST / PUT / DELETE is refused locally before it is sent.
        if mode == "live" and not settings.is_live and not read_only:
            raise LiveTradingDisabledError(
                "live mode requires MODE='live' AND LIVE_TRADING_ENABLED=True "
                f"(now MODE={settings.MODE!r}, LIVE_TRADING_ENABLED={settings.LIVE_TRADING_ENABLED})")
        self.mode: ExecMode = mode
        self.base_url = ENVIRONMENTS["india_testnet" if mode == "testnet" else "india_prod"].rest_url
        if mode == "paper" and (api_key or api_secret):
            raise ExchangeError("paper mode never carries API credentials")
        self._key, self._secret = api_key or "", api_secret or ""
        self._now, self._sleep = now, sleep
        self.budget = budget or shared_budget(self.base_url)
        self._http = httpx.Client(transport=transport, timeout=timeout or settings.HTTP_TIMEOUT_SECONDS)
        self._products: dict[int, ProductInfo] = {}
        self.request_log: list[dict[str, Any]] = []  # method, path, attempt, status (no secrets)

    @classmethod
    def from_settings(cls, mode: ExecMode | None = None, **kw: Any) -> "DeltaRestClientAdapter":
        mode = mode or settings.EXECUTION_MODE
        if mode == "testnet":
            return cls("testnet", settings.TESTNET_API_KEY, settings.TESTNET_API_SECRET, **kw)
        if mode == "live":
            return cls("live", settings.LIVE_API_KEY, settings.LIVE_API_SECRET, **kw)
        return cls("paper", **kw)

    @property
    def has_credentials(self) -> bool:
        return bool(self._key and self._secret)

    def close(self) -> None:
        self._http.close()

    # -- transport ------------------------------------------------------------ #
    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                 body: dict[str, Any] | None = None, auth: bool = False) -> dict[str, Any]:
        """Return the full JSON payload ({success, result, meta})."""
        if auth:
            if self.mode == "paper":
                raise MissingCredentialsError(f"{method} {path} needs auth; paper mode is public-only")
            if not self.has_credentials:
                raise MissingCredentialsError(f"no {self.mode} API key/secret configured for {method} {path}")
        if self.read_only and method != "GET":
            raise ReadOnlyClientError(f"read-only client: {method} {path} refused (nothing sent)")
        query, body_str = encode_query(params), encode_body(body)
        mutation = method != "GET"
        attempts = 1 if mutation else MAX_GET_ATTEMPTS
        weight = endpoint_weight(method, path)
        last_exc: BaseException | str = ""
        for attempt in range(1, attempts + 1):
            self.budget.acquire(weight, self._sleep)
            headers = {"User-Agent": settings.USER_AGENT, "Accept": "application/json", "Content-Type": "application/json"}
            if auth:
                ts = str(int(self._now()))  # fresh timestamp + signature on every attempt
                headers.update({"api-key": self._key, "timestamp": ts,
                                "signature": sign_request(self._secret, method, ts, path, query, body_str)})
            entry = {"method": method, "path": path, "query": query, "attempt": attempt, "status": None}
            self.request_log.append(entry)
            try:
                resp = self._http.request(method, self.base_url + path + query, headers=headers,
                                          content=body_str.encode("utf-8") if body_str else None)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                entry["status"] = type(exc).__name__
                if mutation:
                    raise AmbiguousOutcomeError(method, path, body_str, exc) from exc
                last_exc = exc
                self._sleep(min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_MAX_SECONDS))
                continue
            entry["status"] = resp.status_code
            if resp.status_code == 429:
                wait = self.budget.note_429(resp.headers.get("X-RATE-LIMIT-RESET"))
                if mutation:
                    raise RateLimitedError(method, path, wait)
                last_exc = f"429 (reset {wait:.2f}s)"
                continue  # acquire() waits out the block before the next attempt
            if resp.status_code >= 500:
                if mutation:
                    raise AmbiguousOutcomeError(method, path, body_str, f"HTTP {resp.status_code}")
                last_exc = f"HTTP {resp.status_code}"
                self._sleep(min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_MAX_SECONDS))
                continue
            try:
                payload = resp.json()
            except ValueError:
                raise DeltaAPIError(resp.status_code, method, path, f"non-JSON body: {resp.text[:200]}")
            if resp.status_code >= 400 or not payload.get("success", False):
                raise DeltaAPIError(resp.status_code, method, path, payload.get("error", payload), payload)
            return payload
        raise TransientReadError(f"GET {path} failed after {attempts} attempts: {last_exc!r}")

    def _get(self, path: str, params: dict[str, Any] | None = None, auth: bool = False) -> Any:
        return self._request("GET", path, params=params, auth=auth)["result"]

    def _paginate(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        p = dict(params)
        for _ in range(MAX_PAGES):
            payload = self._request("GET", path, params=p, auth=True)
            rows.extend(payload.get("result") or [])
            after = (payload.get("meta") or {}).get("after")
            if not after:
                return rows
            p["after"] = after
        raise ExchangeError(f"{path}: more than {MAX_PAGES} pages")

    # -- public market ------------------------------------------------------- #
    def get_product(self, symbol: str, require_tradable: bool = True) -> ProductInfo:
        r = self._get(f"/v2/products/{symbol}")
        for k in ("contract_value", "tick_size", "price_band"):
            if r.get(k) in (None, ""):
                raise ProductNotTradableError(f"{symbol}: product has no {k}")
        info = ProductInfo.from_api(r)
        if require_tradable and not info.tradable:
            raise ProductNotTradableError(f"{symbol}: state={info.state!r} trading_status={info.trading_status!r}")
        self._products[info.id] = info
        return info

    def register_product(self, info: ProductInfo) -> None:
        self._products[info.id] = info

    def product(self, product_id: int) -> ProductInfo:
        if product_id not in self._products:
            raise ExchangeError(f"product {product_id} unknown; call get_product(symbol) first")
        return self._products[product_id]

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        r = self._get(f"/v2/tickers/{symbol}")
        q, band = r.get("quotes") or {}, r.get("price_band") or {}

        def f(x: Any) -> float | None:
            return None if x in (None, "") else float(x)

        return {"symbol": r.get("symbol"), "product_id": r.get("product_id"), "mark_price": f(r.get("mark_price")),
                "spot_price": f(r.get("spot_price")), "last_price": f(r.get("close")), "best_bid": f(q.get("best_bid")),
                "best_ask": f(q.get("best_ask")), "bid_size": f(q.get("bid_size")), "ask_size": f(q.get("ask_size")),
                "band_lower": f(band.get("lower_limit")), "band_upper": f(band.get("upper_limit")),
                "timestamp_us": r.get("timestamp"), "raw": r}

    def get_products(self, contract_types: str = "perpetual_futures", states: str = "live") -> list[dict[str, Any]]:
        """Product catalog (public). Follows meta.after if the exchange paginates."""
        rows: list[dict[str, Any]] = []
        params: dict[str, Any] = {"contract_types": contract_types, "states": states}
        for _ in range(MAX_PAGES):
            payload = self._request("GET", "/v2/products", params=params)
            rows.extend(payload.get("result") or [])
            after = (payload.get("meta") or {}).get("after")
            if not after:
                return rows
            params["after"] = after
        return rows

    def get_history_candles(self, symbol: str, start_s: int, end_s: int, resolution: str = "1m") -> list[dict[str, Any]]:
        """Closed candles with open time in [start_s, end_s] (inclusive), ascending. <= 2000 per call."""
        rows = self._get("/v2/history/candles", {"resolution": resolution, "symbol": symbol,
                                                 "start": int(start_s), "end": int(end_s)}) or []
        return sorted(rows, key=lambda r: int(r["time"]))

    def get_l2_orderbook(self, symbol: str, depth: int = 5) -> dict[str, Any]:
        r = self._get(f"/v2/l2orderbook/{symbol}", {"depth": depth})
        side = lambda rows: [(float(x["price"]), int(x["size"])) for x in rows or []]  # noqa: E731
        return {"symbol": r.get("symbol"), "bids": side(r.get("buy")), "asks": side(r.get("sell")),
                "last_updated_at_us": r.get("last_updated_at")}

    # -- authenticated reads -------------------------------------------------- #
    def get_trading_preferences(self) -> dict[str, Any]:
        return self._get("/v2/users/trading_preferences", auth=True)

    def get_wallet_balances(self) -> list[dict[str, Any]]:
        return self._get("/v2/wallet/balances", auth=True)

    def get_position(self, product_id: int) -> dict[str, Any]:
        """Real-time position: {'size': signed int, 'entry_price': str|None, ...}."""
        r = self._get("/v2/positions", {"product_id": product_id}, auth=True) or {}
        return {**r, "size": int(r.get("size") or 0)}

    def get_margined_positions(self, product_ids: Iterable[int] | None = None) -> list[dict[str, Any]]:
        """Full details (margin, liquidation_price, realized_funding, margin_mode). May lag up to ~10s."""
        params = {"product_ids": ",".join(map(str, product_ids))} if product_ids else None
        return self._get("/v2/positions/margined", params, auth=True) or []

    def get_active_orders(self, product_id: int | None = None, states: str = "open,pending") -> list[dict[str, Any]]:
        return self._paginate("/v2/orders", {"product_ids": product_id, "states": states})

    def get_order_by_id(self, order_id: int | str) -> dict[str, Any]:
        return self._get(f"/v2/orders/{order_id}", auth=True)

    def get_order_by_client_oid(self, client_oid: str) -> dict[str, Any] | None:
        """The order, or None if the exchange has no order with this client_order_id (404)."""
        validate_client_oid(client_oid)
        try:
            return self._get(f"/v2/orders/client_order_id/{client_oid}", auth=True)
        except DeltaAPIError as exc:
            if exc.status == 404:
                return None
            raise

    def get_order_history(self, product_id: int, start_time_us: int, end_time_us: int, page_size: int = 50) -> list[dict[str, Any]]:
        return self._paginate("/v2/orders/history", {"product_ids": product_id, "start_time": int(start_time_us),
                                                     "end_time": int(end_time_us), "page_size": page_size})

    def get_fills(self, product_id: int, start_time_us: int, end_time_us: int, page_size: int = 50) -> list[dict[str, Any]]:
        """Explicit microsecond start/end on every call (without them Delta silently limits the range)."""
        return self._paginate("/v2/fills", {"product_ids": product_id, "start_time": int(start_time_us),
                                            "end_time": int(end_time_us), "page_size": page_size})

    # -- account / risk mutations ------------------------------------------- #
    def set_margin_mode(self, margin_mode: str = "isolated", subaccount_user_id: int | str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"margin_mode": margin_mode}
        if subaccount_user_id is not None:
            body["subaccount_user_id"] = str(subaccount_user_id)
        return self._request("PUT", "/v2/users/margin_mode", body=body, auth=True)["result"]

    def get_order_leverage(self, product_id: int) -> dict[str, Any]:
        return self._get(f"/v2/products/{product_id}/orders/leverage", auth=True)

    def set_order_leverage(self, product_id: int, leverage: int | float | str) -> dict[str, Any]:
        r = self._request("POST", f"/v2/products/{product_id}/orders/leverage", body={"leverage": str(leverage)}, auth=True)["result"]
        if Decimal(str(r.get("leverage"))) != Decimal(str(leverage)):
            raise ExchangeError(f"leverage not applied: asked {leverage}, exchange reports {r.get('leverage')}")
        return r

    # -- orders ---------------------------------------------------------------- #
    def _snap(self, product_id: int, value: Any) -> str | None:
        if value is None:
            return None
        return str(snap_to_tick(value, self.product(product_id).tick_size))

    @staticmethod
    def _check_prices(order: dict[str, Any], intended: dict[str, str]) -> None:
        bad = {}
        for k, want in intended.items():
            got = order.get(k)
            if got in (None, ""):
                continue  # field not echoed (e.g. bracket legs live on child orders)
            if Decimal(str(got)) != Decimal(want):
                bad[k] = (want, got)
        if bad:
            raise OrderPriceMismatchError(order, bad)

    def place_order(
        self,
        *,
        product_id: int,
        side: Literal["buy", "sell"],
        size: int,
        order_type: Literal["market_order", "limit_order"] = "market_order",
        client_order_id: str | None = None,
        limit_price: Any = None,
        stop_price: Any = None,
        stop_order_type: str | None = None,
        stop_trigger_method: str | None = None,
        trail_amount: Any = None,
        reduce_only: bool = False,
        post_only: bool | None = None,
        time_in_force: str | None = None,
        bracket_stop_loss_price: Any = None,
        bracket_stop_loss_limit_price: Any = None,
        bracket_take_profit_price: Any = None,
        bracket_take_profit_limit_price: Any = None,
        bracket_trail_amount: Any = None,
        bracket_stop_trigger_method: str | None = None,
    ) -> dict[str, Any]:
        validate_client_oid(client_order_id)
        if side not in ("buy", "sell"):
            raise OrderValidationError(f"side must be buy/sell, got {side!r}")
        if int(size) != size or size < 1:
            raise OrderValidationError(f"size must be a positive integer number of contracts, got {size!r}")
        if order_type == "limit_order" and limit_price is None:
            raise OrderValidationError("limit_order needs limit_price")
        prices = {k: self._snap(product_id, v) for k, v in {
            "limit_price": limit_price, "stop_price": stop_price, "trail_amount": trail_amount,
            "bracket_stop_loss_price": bracket_stop_loss_price,
            "bracket_stop_loss_limit_price": bracket_stop_loss_limit_price,
            "bracket_take_profit_price": bracket_take_profit_price,
            "bracket_take_profit_limit_price": bracket_take_profit_limit_price,
            "bracket_trail_amount": bracket_trail_amount}.items()}
        body: dict[str, Any] = {"product_id": product_id, "size": int(size), "side": side, "order_type": order_type}
        body.update({k: v for k, v in prices.items() if v is not None})
        for k, v in (("client_order_id", client_order_id), ("stop_order_type", stop_order_type),
                     ("stop_trigger_method", stop_trigger_method), ("post_only", post_only),
                     ("time_in_force", time_in_force), ("bracket_stop_trigger_method", bracket_stop_trigger_method)):
            if v is not None:
                body[k] = v
        if reduce_only:
            body["reduce_only"] = True
        order = self._request("POST", "/v2/orders", body=body, auth=True)["result"]
        intended = {k: prices[k] for k in ("stop_price", "bracket_stop_loss_price", "bracket_take_profit_price")
                    if prices[k] is not None}
        if order_type == "limit_order":
            intended["limit_price"] = prices["limit_price"]
        self._check_prices(order, intended)
        return order

    def edit_order(self, order_id: int | str, product_id: int, size: int, limit_price: Any = None,
                   stop_price: Any = None, trail_amount: Any = None) -> dict[str, Any]:
        body: dict[str, Any] = {"id": int(order_id), "product_id": product_id, "size": int(size)}
        intended = {}
        for k, v in (("limit_price", limit_price), ("stop_price", stop_price), ("trail_amount", trail_amount)):
            s = self._snap(product_id, v)
            if s is not None:
                body[k] = s
                if k != "trail_amount":
                    intended[k] = s
        order = self._request("PUT", "/v2/orders", body=body, auth=True)["result"]
        self._check_prices(order, intended)
        return order

    def cancel_order(self, product_id: int, order_id: int | str | None = None, client_order_id: str | None = None) -> dict[str, Any]:
        if (order_id is None) == (client_order_id is None):
            raise OrderValidationError("pass exactly one of order_id / client_order_id")
        validate_client_oid(client_order_id)
        body: dict[str, Any] = {"product_id": product_id}
        if order_id is not None:
            body["id"] = int(order_id)
        else:
            body["client_order_id"] = client_order_id
        return self._request("DELETE", "/v2/orders", body=body, auth=True)["result"]

    def _leg(self, product_id: int, leg: dict[str, Any] | None) -> dict[str, Any] | None:
        if leg is None:
            return None
        out = dict(leg)
        for k in ("stop_price", "limit_price", "trail_amount"):
            if out.get(k) is not None:
                out[k] = self._snap(product_id, out[k])
        return out

    def place_position_bracket(self, product_id: int, stop_loss_order: dict[str, Any] | None,
                               take_profit_order: dict[str, Any] | None,
                               trigger_method: str = "last_traded_price") -> dict[str, Any]:
        """Attach SL/TP to an EXISTING position. Legs: {'order_type': 'market_order', 'stop_price': ...}."""
        body: dict[str, Any] = {"product_id": product_id, "bracket_stop_trigger_method": trigger_method}
        if stop_loss_order is not None:
            body["stop_loss_order"] = self._leg(product_id, stop_loss_order)
        if take_profit_order is not None:
            body["take_profit_order"] = self._leg(product_id, take_profit_order)
        return self._request("POST", "/v2/orders/bracket", body=body, auth=True)["result"]

    def edit_entry_bracket(self, entry_order_id: int | str, product_id: int, *, stop_loss_price: Any = None,
                           take_profit_price: Any = None, trail_amount: Any = None,
                           trigger_method: str | None = None) -> dict[str, Any]:
        """Edit bracket params of a still-OPEN entry order. Filled entries: edit the child leg via edit_order."""
        parent = self.get_order_by_id(entry_order_id)
        if parent.get("state") != "open":
            raise OrderValidationError(f"entry order {entry_order_id} is {parent.get('state')!r}; "
                                       "edit_entry_bracket only applies to open entry orders")
        body: dict[str, Any] = {"id": int(entry_order_id), "product_id": product_id}
        for k, v in (("bracket_stop_loss_price", stop_loss_price), ("bracket_take_profit_price", take_profit_price),
                     ("bracket_trail_amount", trail_amount)):
            s = self._snap(product_id, v)
            if s is not None:
                body[k] = s
        if trigger_method:
            body["bracket_stop_trigger_method"] = trigger_method
        return self._request("PUT", "/v2/orders/bracket", body=body, auth=True)["result"]

    # -- flattening ------------------------------------------------------------ #
    def close_position_reduce_only(self, product_id: int, current_size: int) -> dict[str, Any]:
        """Cancel resting entry orders, send ONE reduce-only market order, then poll the real-time position."""
        cancelled, notes = [], []
        for o in self.get_active_orders(product_id):
            if not o.get("reduce_only") and not o.get("stop_order_type"):
                self.cancel_order(product_id, order_id=o["id"])
                cancelled.append(o["id"])
        order = None
        if current_size:
            side = "sell" if current_size > 0 else "buy"
            try:
                order = self.place_order(product_id=product_id, side=side, size=abs(int(current_size)),
                                         order_type="market_order", reduce_only=True)
            except AmbiguousOutcomeError as exc:
                notes.append(f"close order outcome unknown ({exc.cause!r}); not resent, verifying position")
        residual = None
        for i in range(CLOSE_POLL_ATTEMPTS):
            residual = self.get_position(product_id)["size"]
            if residual == 0:
                break
            if i < CLOSE_POLL_ATTEMPTS - 1:
                self._sleep(CLOSE_POLL_SECONDS)
        return {"product_id": product_id, "flat": residual == 0, "residual_size": residual,
                "cancelled_entry_orders": cancelled, "close_order": order, "notes": notes}

    def emergency_close_all(self, user_id: int) -> dict[str, Any]:
        """POST /v2/positions/close_all, then verify every product with the REAL-TIME position endpoint."""
        before = self.get_margined_positions()
        error = None
        skipped: list[dict[str, Any]] = []
        try:
            r = self._request("POST", "/v2/positions/close_all",
                              body={"close_all_portfolio": True, "close_all_isolated": True, "user_id": int(user_id)},
                              auth=True)["result"] or {}
            skipped = list(r.get("skipped_products") or []) if isinstance(r, dict) else []
        except (DeltaAPIError, AmbiguousOutcomeError, RateLimitedError) as exc:
            error = str(exc)
        ids = {int(p["product_id"]) for p in before if int(p.get("size") or 0) != 0}
        ids |= {int(s["product_id"]) for s in skipped if s.get("product_id") is not None}
        residual = []
        for pid in sorted(ids):
            pos = self.get_position(pid)
            if pos["size"] != 0:
                residual.append({"product_id": pid, "product_symbol": pos.get("product_symbol"), "size": pos["size"]})
        status = "FLAT" if not residual and not skipped and error is None else "RESIDUAL_EXPOSURE"
        return {"status": status, "skipped_products": skipped, "residual": residual, "error": error,
                "checked_product_ids": sorted(ids)}
