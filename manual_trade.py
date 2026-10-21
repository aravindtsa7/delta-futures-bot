"""Manual trading terminal API (Steps 8 / 8B): /api/products/catalog and /api/manual/*.

Modes (POST /api/manual/mode, persisted in data/manual_mode.json) each use their own journal:
  paper    data/bot_state.db          simulated fills against live public prices (virtual ₹50,000)
  testnet  data/bot_state_testnet.db  Delta India demo account
  live     data/bot_state_live.db     REAL MONEY on Delta Exchange India — needs MODE=live, LIVE_TRADING_ENABLED=true,
                                      the live API key/secret and an explicit {"mode": "live", "confirm": "LIVE"} switch

* Single-writer rule: manual mutations are refused (409) while the 24/7 auto-bot holds data/live_bot.lock.
* Paper fills: market BUY at best ask / SELL at best bid + slippage, taker fee + 18% GST; a marketable limit fills like
  a market order; otherwise it rests and fills at its limit (maker fee) when the last price touches it. A 2 s
  watcher in the dashboard process runs SL / TP / trailing / liquidation / funding / limit fills while the bot is stopped.
* Exchange modes: isolated margin + the chosen leverage are set on Delta before every entry; market or limit entries
  carry SL/TP as a bracket; scale-ins resync size/average entry from the exchange and resize the SL/TP legs; partial and
  full closes are reduce-only market orders booked from their own fills; SL/TP edits modify the live legs (or create
  them); resting limits can be cancelled. Wallet and positions shown are Delta's (USD x 85 when no INR figure is given).
"""

from __future__ import annotations

import math
import re
import threading
import time
from typing import Any, Callable, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

import bot_ops
import data_manager as dm
import execution_engine as ee
from config import mask_secret, settings
from exchange_client import DeltaAPIError, DeltaRestClientAdapter, ExchangeError, ReadOnlyClientError

router = APIRouter(tags=["manual"])

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")
CATALOG_TTL_S = 300
SNAPSHOT_TTL_S = 3.0
WATCH_INTERVAL_S = 2.0
RESOLUTIONS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
CHART_BARS = 300
EXCHANGE_MODES = ("testnet", "live")
ZERO_BALANCE_MSG = ("Your Delta India {what} wallet balance is ₹0.00 — please add funds on Delta Exchange India "
                    "or switch to PAPER mode to test")
_cache: dict[str, tuple[float, Any]] = {}
_margin_ok: dict[str, bool] = {}
_watch: dict[str, Any] = {"thread": None, "runs": 0, "last_run": None, "last_actions": [], "last_error": None, "status": "not started"}
# Tests inject a fake exchange here: fn(mode, read_only) -> DeltaRestClientAdapter
CLIENT_FACTORY: Callable[[str, bool], DeltaRestClientAdapter] | None = None


def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def _pub() -> DeltaRestClientAdapter:
    return DeltaRestClientAdapter("paper")


def check_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    if not SYMBOL_RE.match(s):
        raise HTTPException(422, f"invalid symbol {symbol!r}")
    return s


def keys_for(mode: str) -> tuple[str, str]:
    if mode == "live":
        return settings.LIVE_API_KEY, settings.LIVE_API_SECRET
    if mode == "testnet":
        return settings.TESTNET_API_KEY, settings.TESTNET_API_SECRET
    return "", ""


def client_for(mode: str, read_only: bool = False) -> DeltaRestClientAdapter:
    if CLIENT_FACTORY is not None:
        return CLIENT_FACTORY(mode, read_only)
    key, secret = keys_for(mode)
    return DeltaRestClientAdapter(mode, key, secret, read_only=read_only)


# --------------------------------------------------------------------------- #
# Catalog + coin list
# --------------------------------------------------------------------------- #
def catalog() -> dict[str, dict[str, Any]]:
    def build() -> dict[str, dict[str, Any]]:
        out = {}
        for p in _pub().get_products("perpetual_futures", "live"):
            im = float(p.get("initial_margin") or 0) or None
            out[p["symbol"]] = {
                "symbol": p["symbol"], "product_id": p["id"], "description": p.get("description"),
                "contract_value": float(p["contract_value"]), "tick_size": float(p["tick_size"]),
                "initial_margin_pct": im, "maintenance_margin_pct": float(p.get("maintenance_margin") or 0),
                "max_leverage": round(100.0 / im, 4) if im else None, "trading_status": p.get("trading_status"),
                "underlying": (p.get("underlying_asset") or {}).get("symbol"),
                "settling_asset": (p.get("settling_asset") or {}).get("symbol"),
                "position_size_limit": p.get("position_size_limit"),
            }
        return out
    return _cached("catalog", CATALOG_TTL_S, build)


def product_info(symbol: str) -> dict[str, Any]:
    cat = catalog()
    if symbol not in cat:
        raise HTTPException(404, f"{symbol} is not a live Delta Exchange India perpetual future")
    if not dm.metadata_path(symbol).exists():
        dm.fetch_and_save_metadata([symbol])  # on the fly: no history backfill needed to trade it
    return cat[symbol]


@router.get("/api/products/catalog")
def products_catalog(q: str | None = None) -> dict[str, Any]:
    cat = catalog()
    added = set(dm.app_symbols())
    rows = sorted(cat.values(), key=lambda r: (r["symbol"] not in ("BTCUSD", "ETHUSD"), r["symbol"] not in added, r["symbol"]))
    if q:
        qq = q.strip().upper()
        rows = [r for r in rows if qq in r["symbol"] or qq in (r.get("description") or "").upper()]
    return {"count": len(rows), "total_live_perpetuals": len(cat), "cache_ttl_s": CATALOG_TTL_S,
            "products": [{**r, "added": r["symbol"] in added} for r in rows]}


class AddSymbolRequest(BaseModel):
    symbol: str
    backfill: bool = True
    days: int = Field(90, ge=1, le=400)


@router.post("/api/manual/symbols")
def add_symbol(req: AddSymbolRequest) -> dict[str, Any]:
    """Add a coin everywhere: metadata now, coin list now, 1m history for the Backtest Lab in the background."""
    sym = check_symbol(req.symbol)
    info = product_info(sym)
    dm.add_app_symbol(sym)
    backfill = None
    if req.backfill:
        import app as webapp  # lazy: app.py mounts this router
        try:
            webapp.start_sync(webapp.SyncRequest(add_symbol=sym, days=req.days))
            backfill = f"started ({req.days} days of TRADE/MARK/FUNDING 1m in the background)"
        except HTTPException as exc:
            backfill = f"not started: {exc.detail}"
    prof = ee.bot_profiles().get(sym)
    return {"symbol": sym, "product": info, "app_symbols": dm.app_symbols(), "backfill": backfill,
            "auto_bot_profile": prof.variant if prof else "disabled (data/bot_profiles.json)"}


# --------------------------------------------------------------------------- #
# Market data for the ticket / chart (public production data in every mode)
# --------------------------------------------------------------------------- #
def ticker(symbol: str, ttl: float = 1.0) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        t = _pub().get_ticker(symbol)
        raw = t.pop("raw", {}) or {}
        t.update(change_24h_pct=_f(raw.get("ltp_change_24h")), mark_change_24h_pct=_f(raw.get("mark_change_24h")),
                 high_24h=_f(raw.get("high")), low_24h=_f(raw.get("low")), open_24h=_f(raw.get("open")),
                 funding_rate_pct=_f(raw.get("funding_rate")), volume_24h=_f(raw.get("volume")),
                 turnover_usd_24h=_f(raw.get("turnover_usd")), at=time.time())
        return t
    return _cached(f"tk:{symbol}", ttl, build)


def _f(x: Any) -> float | None:
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def candles(symbol: str, resolution: str) -> list[dict[str, Any]]:
    step = RESOLUTIONS[resolution]

    def build() -> list[dict[str, Any]]:
        end = int(time.time())
        rows = _pub().get_history_candles(symbol, end - step * CHART_BARS, end, resolution)
        return [{"time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
                 "close": float(r["close"]), "volume": float(r.get("volume") or 0)} for r in rows]
    return _cached(f"c:{symbol}:{resolution}", 5.0, build)


# --------------------------------------------------------------------------- #
# Mode, gates, sessions
# --------------------------------------------------------------------------- #
def journal_mode() -> str:
    return bot_ops.active_mode()


def mode_gate(mode: str) -> None:
    """Raise unless ``mode`` may be used for orders right now (bot lock checked separately)."""
    if mode == "live":
        if not settings.is_live:
            raise HTTPException(403, "LIVE manual trading needs MODE=live and LIVE_TRADING_ENABLED=true in .env "
                                     f"(now MODE={settings.MODE!r}, LIVE_TRADING_ENABLED={settings.LIVE_TRADING_ENABLED})")
        if not all(keys_for("live")):
            raise HTTPException(400, "LIVE mode needs DELTA_API_KEY / DELTA_API_SECRET (or DELTA_LIVE_API_KEY/SECRET) in .env")
    if mode == "testnet" and not all(keys_for("testnet")):
        raise HTTPException(400, "TESTNET mode needs DELTA_TESTNET_API_KEY / DELTA_TESTNET_API_SECRET in .env")


def guard_writable() -> str:
    run, info = bot_ops.runner()
    if run:
        raise HTTPException(409, f"the 24/7 auto-bot is running (pid {(info or {}).get('pid')}, mode {(info or {}).get('mode')}); "
                                 "stop it to trade manually: POST /api/bot/stop")
    mode = journal_mode()
    mode_gate(mode)
    return mode


class _Session:
    """StateStore + engine (+ exchange client) for one manual operation; caller holds bot_ops._op_lock."""

    def __init__(self, mode: str):
        self.mode = mode
        self.store = ee.StateStore(bot_ops.mode_db(mode))
        try:
            if mode == "paper":
                self.client = None
                self.engine = ee.ExecutionEngine(self.store, mode, None, profiles=ee.bot_profiles(), notifier=bot_ops.notifier())
            else:
                self.client = client_for(mode)
                self.engine = ee.ExecutionEngine(self.store, mode, self.client, profiles={}, products={},
                                                 notifier=bot_ops.notifier())
        except BaseException:
            self.store.close()
            raise

    def ensure_margin(self) -> None:
        if not _margin_ok.get(self.mode):
            try:
                self.engine.ensure_isolated_margin()
            except DeltaAPIError as exc:
                raise HTTPException(409, f"could not set isolated margin on Delta ({exc.status}): {exc.error}; "
                                         "no order was placed") from exc
            _margin_ok[self.mode] = True

    def __enter__(self) -> "_Session":
        return self

    def __exit__(self, *exc: Any) -> None:
        _cache.pop(f"snap:{self.mode}", None)
        self.store.close()


# --------------------------------------------------------------------------- #
# Exchange wallet / positions snapshot
# --------------------------------------------------------------------------- #
def _usd_row(bals: list[dict[str, Any]]) -> dict[str, Any]:
    return next((b for b in bals if b.get("asset_symbol") == "USD"), {}) or {}


def wallet_from_balances(bals: list[dict[str, Any]]) -> dict[str, Any]:
    """Delta India wallet in INR: Delta's own *_inr figures when present, else USD x USD_INR_RATE."""
    rate = settings.FIXED_USD_INR_RATE
    u = _usd_row(bals)
    bal_usd, avail_usd = float(u.get("balance") or 0), float(u.get("available_balance") or 0)
    used_usd = float(u.get("position_margin") or 0) + float(u.get("order_margin") or 0)
    bal_inr = _f(u.get("balance_inr"))
    avail_inr = _f(u.get("available_balance_inr"))
    return {"wallet_equity_usd": bal_usd, "available_margin_usd": avail_usd, "used_margin_usd": used_usd,
            "wallet_equity_inr": bal_inr if bal_inr is not None else bal_usd * rate,
            "available_margin_inr": avail_inr if avail_inr is not None else avail_usd * rate,
            "used_margin_inr": used_usd * rate, "inr_source": "delta *_inr fields" if bal_inr is not None else f"USD x {rate:g}",
            "assets": [{k: b.get(k) for k in ("asset_symbol", "balance", "available_balance", "balance_inr")}
                       for b in bals if float(b.get("balance") or 0) or b.get("asset_symbol") in ("USD", "INR")]}


def exchange_snapshot(mode: str, ttl: float = SNAPSHOT_TTL_S) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        c = client_for(mode, read_only=True)
        bals = c.get_wallet_balances()
        poss = [p for p in c.get_margined_positions() if int(p.get("size") or 0) != 0]
        orders = c.get_active_orders()
        return {"wallet": wallet_from_balances(bals), "positions": poss, "orders": orders, "at": time.time()}
    return _cached(f"snap:{mode}", ttl, build)


def sync_journal(mode: str) -> dict[str, Any]:
    """Advance resting limit orders and book exits the exchange made (SL/TP legs) into the mode's journal."""
    actions: list[dict[str, Any]] = []
    with _Session(mode) as s:
        for r in s.engine.refresh_resting_orders():
            actions.append({"kind": "resting_order", **r})
        for p in s.store.positions(mode):
            r = s.engine.sync_exchange_position(p["symbol"])
            if r:
                actions.append({"kind": "exchange_exit", "symbol": p["symbol"], **{k: r.get(k) for k in ("action", "reason", "net_pnl_inr")}})
    return {"actions": actions}


@router.post("/api/manual/sync_exchange")
def sync_exchange() -> dict[str, Any]:
    mode = journal_mode()
    if mode not in EXCHANGE_MODES:
        raise HTTPException(409, "PAPER mode has no exchange account to sync; switch to LIVE or TESTNET")
    mode_gate(mode)
    out: dict[str, Any] = {"mode": mode}
    with bot_ops._op_lock:
        if not bot_ops.runner()[0]:
            out.update(sync_journal(mode))
        else:
            out["note"] = "auto-bot running: it reconciles its own journal; read-only snapshot only"
    _cache.pop(f"snap:{mode}", None)
    snap = exchange_snapshot(mode)
    out.update(wallet=snap["wallet"], exchange_positions=snap["positions"],
               open_orders=[o for o in snap["orders"] if not o.get("stop_order_type")],
               protective_orders=[o for o in snap["orders"] if o.get("stop_order_type")])
    return bot_ops._clean(out)


# --------------------------------------------------------------------------- #
# Mode switching + connection diagnostics
# --------------------------------------------------------------------------- #
class ModeRequest(BaseModel):
    mode: Literal["paper", "testnet", "live"]
    confirm: str | None = None


@router.post("/api/manual/mode")
def set_mode(req: ModeRequest) -> dict[str, Any]:
    run, info = bot_ops.runner()
    if run:
        raise HTTPException(409, f"the 24/7 auto-bot is running (pid {(info or {}).get('pid')}); stop it before switching modes")
    if req.mode != "paper":
        mode_gate(req.mode)
    if req.mode == "live" and req.confirm != "LIVE":
        raise HTTPException(422, 'switching to LIVE needs {"mode": "live", "confirm": "LIVE"} — real money on Delta Exchange India')
    bot_ops.set_active_mode(req.mode)
    _cache.pop(f"snap:{req.mode}", None)
    bot_ops.notifier().notify_bot_lifecycle(f"Manual mode {req.mode.upper()}", req.mode, None,
                                            "REAL MONEY manual orders enabled" if req.mode == "live" else None)
    return {"mode": req.mode, "journal": str(bot_ops.mode_db(req.mode))}


def _diagnose(exc: Exception) -> str:
    if isinstance(exc, DeltaAPIError):
        code = str(exc.code or "")
        hint = {"ip_not_whitelisted_for_api_key": "this machine's public IP is not whitelisted for the key",
                "invalid_api_key": "the API key is not recognised (wrong key, or a testnet key used on live)",
                "Signature Mismatch": "the API secret does not match the key", "SignatureExpired": "PC clock is off by >5 s",
                "unauthorized": "key lacks permission or does not exist"}.get(code, "")
        return f"HTTP {exc.status} {code}{' — ' + hint if hint else ''}"
    return f"{type(exc).__name__}: {str(exc)[:200]}"


@router.get("/api/manual/connection_status")
def connection_status(verify: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"active_mode": journal_mode(), "MODE": settings.MODE, "LIVE_TRADING_ENABLED": settings.LIVE_TRADING_ENABLED,
                           "live_gates_open": settings.is_live, "auto_bot_live_enabled": settings.AUTO_BOT_LIVE_ENABLED,
                           "usd_inr_rate": settings.FIXED_USD_INR_RATE, "modes": {}}
    for mode in ("paper", "testnet", "live"):
        key, secret = keys_for(mode)
        m: dict[str, Any] = {"journal": str(bot_ops.mode_db(mode)), "journal_exists": bot_ops.mode_db(mode).exists()}
        if mode == "paper":
            m.update(available=True, virtual_capital_inr=ee.CAPITAL_INR)
        else:
            m.update(key_present=bool(key), secret_present=bool(secret), api_key_masked=mask_secret(key),
                     rest_url=client_for(mode, read_only=True).base_url if (key or CLIENT_FACTORY) else None)
            m["available"] = bool(key and secret) and (mode != "live" or settings.is_live)
            if verify and key and secret:
                try:
                    c = client_for(mode, read_only=True)
                    w = wallet_from_balances(c.get_wallet_balances())
                    poss = [p for p in c.get_margined_positions() if int(p.get("size") or 0) != 0]
                    m.update(connected=True, **{k: w[k] for k in ("wallet_equity_inr", "available_margin_inr", "wallet_equity_usd",
                                                                  "available_margin_usd", "inr_source")},
                             open_positions=len(poss), message="Connected ✔ (read-only check: wallet + positions)")
                except (ExchangeError, ReadOnlyClientError) as exc:
                    m.update(connected=False, message=_diagnose(exc))
            elif verify:
                m.update(connected=False, message="no API key/secret in .env")
        out["modes"][mode] = m
    return bot_ops._clean(out)


# --------------------------------------------------------------------------- #
# Sizing / preview
# --------------------------------------------------------------------------- #
class ManualOrder(BaseModel):
    symbol: str
    side: Literal["LONG", "SHORT"]
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = Field(None, gt=0)
    leverage: float = Field(10, ge=1)
    margin_inr: float | None = Field(None, gt=0)
    contracts: int | None = Field(None, ge=1)
    risk_pct: float | None = Field(None, gt=0, le=100)
    sl_price: float | None = Field(None, gt=0)
    sl_pct: float | None = Field(None, gt=0, lt=100)
    tp_price: float | None = Field(None, gt=0)
    tp_pct: float | None = Field(None, gt=0)
    trail_amount: float | None = Field(None, ge=0)
    trigger_source: Literal["last_traded_price", "mark_price"] = "last_traded_price"

    @model_validator(mode="after")
    def _check(self) -> "ManualOrder":
        n = sum(x is not None for x in (self.margin_inr, self.contracts, self.risk_pct))
        if n != 1:
            raise ValueError("give exactly one sizing input: margin_inr, contracts or risk_pct")
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limit orders need limit_price")
        if self.sl_price is not None and self.sl_pct is not None or self.tp_price is not None and self.tp_pct is not None:
            raise ValueError("give SL/TP as a price or a percent, not both")
        return self


def account_view(mode: str) -> dict[str, Any]:
    """Wallet + journal positions for sizing: virtual paper wallet, or Delta's real wallet in exchange modes."""
    st = bot_ops.read_status(with_market=True, with_signals=False, mode=mode)
    if mode in EXCHANGE_MODES:
        w = exchange_snapshot(mode)["wallet"]
        st["wallet"] = {**st["wallet"], "realized_equity_inr": w["wallet_equity_inr"], "available_margin_inr": w["available_margin_inr"],
                        "used_margin_inr": w["used_margin_inr"], "exchange": w}
    return st


def build_preview(req: ManualOrder) -> dict[str, Any]:
    mode = journal_mode()
    sym = check_symbol(req.symbol)
    info = product_info(sym)
    if info["trading_status"] != "operational":
        raise HTTPException(409, f"{sym} trading_status is {info['trading_status']}")
    max_lev = info["max_leverage"] or 1
    if req.leverage > max_lev + 1e-9:
        raise HTTPException(422, f"{sym}: leverage {req.leverage:g}x exceeds the product maximum {max_lev:g}x")
    tk = ticker(sym)
    bid, ask, mark = tk["best_bid"], tk["best_ask"], tk["mark_price"]
    if not (bid and ask):
        raise HTTPException(503, f"{sym}: no live quotes")
    d = 1 if req.side == "LONG" else -1
    touch = ask if d == 1 else bid
    exchange = mode in EXCHANGE_MODES
    slip = 0.0 if exchange else settings.SIZING_SLIPPAGE_BPS / 10_000
    resting = False
    if req.order_type == "limit":
        marketable = req.limit_price >= ask if d == 1 else req.limit_price <= bid
        if marketable:
            raw = min(req.limit_price, ask) if d == 1 else max(req.limit_price, bid)
            entry = raw * (1 + d * slip)
        else:
            raw = entry = req.limit_price
            resting = True
    else:
        raw = touch
        entry = raw * (1 + d * slip)
    fee_rate = (settings.MAKER_FEE if resting else settings.TAKER_FEE) * (1 + settings.GST_ON_FEES)
    exit_fee_rate = settings.TAKER_FEE * (1 + settings.GST_ON_FEES)
    sl = req.sl_price if req.sl_price is not None else (entry * (1 - d * req.sl_pct / 100) if req.sl_pct else None)
    tp = req.tp_price if req.tp_price is not None else (entry * (1 + d * req.tp_pct / 100) if req.tp_pct else None)
    tick = info["tick_size"]
    snap = lambda x: None if x is None else round(round(x / tick) * tick, 10)  # noqa: E731
    sl, tp = snap(sl), snap(tp)
    problems: list[str] = []
    if sl is not None and (sl >= entry if d == 1 else sl <= entry):
        problems.append(f"stop-loss {sl} must be {'below' if d == 1 else 'above'} the entry {entry:.6g} for a {req.side}")
    if tp is not None and (tp <= entry if d == 1 else tp >= entry):
        problems.append(f"take-profit {tp} must be {'above' if d == 1 else 'below'} the entry {entry:.6g} for a {req.side}")
    if exchange and req.trail_amount:
        problems.append("trailing stops are paper-only for manual exchange positions (use SL/TP; edit the SL as price moves)")
    rate, cv = settings.FIXED_USD_INR_RATE, info["contract_value"]
    st = account_view(mode)
    w = st["wallet"]
    if req.contracts is not None:
        n = int(req.contracts)
    elif req.margin_inr is not None:
        n = math.floor(req.margin_inr / rate * req.leverage / (cv * entry) + 1e-9)
    else:
        if sl is None:
            raise HTTPException(422, "risk_pct sizing needs a stop-loss (sl_price or sl_pct)")
        per = cv * (abs(entry - sl) + entry * fee_rate + sl * exit_fee_rate)
        n = math.floor(max(w["realized_equity_inr"], 0) * req.risk_pct / 100 / rate / per + 1e-9)
    if n < 1:
        if exchange and w["available_margin_inr"] <= 0:
            raise HTTPException(422, ZERO_BALANCE_MSG.format(what=mode))
        raise HTTPException(422, f"sizing gives {n} contracts: one {sym} contract is {cv:g} x {entry:.6g} = "
                                 f"${cv * entry:,.4f} notional (₹{cv * entry / req.leverage * rate:,.2f} margin at {req.leverage:g}x)")
    notional = n * cv * entry
    margin = notional / req.leverage
    fee = notional * fee_rate
    mm = info["maintenance_margin_pct"] or 0.5
    liq = ee.liquidation_price(entry, req.side, req.leverage, mm)
    if sl is not None and (sl <= liq if d == 1 else sl >= liq):
        problems.append(f"stop-loss {sl} is beyond the estimated liquidation price {liq:.6g}: liquidation would hit first")
    pos = next((p for p in st["positions"] if p["symbol"] == sym), None)
    adds = pos is None or pos["side"] == req.side
    if pos is not None and adds and abs(float(pos["leverage"]) - req.leverage) > 1e-9:
        problems.append(f"open {sym} position is {float(pos['leverage']):g}x: add at the same leverage")
    effect = "open" if pos is None else ("add" if adds else ("reduce" if n < pos["contracts"] else
                                                           "close" if n == pos["contracts"] else "flip"))
    if exchange and effect == "flip":
        problems.append(f"this would flip the {sym} {pos['side']} position; on the exchange close it first, then open the new side")
    if exchange and not adds and req.order_type == "limit":
        problems.append("reducing an exchange position uses a reduce-only MARKET order; choose Market or use Close part")
    need = (margin + fee) * rate
    if adds:
        if exchange and w["available_margin_inr"] <= 0:
            problems.append(ZERO_BALANCE_MSG.format(what=mode))
        elif need > w["available_margin_inr"] + 1e-6:
            problems.append(f"insufficient margin: need ₹{need:,.2f}, available ₹{w['available_margin_inr']:,.2f}")
    sl_loss = None if sl is None else n * cv * (abs(entry - sl) * rate) + (fee + n * cv * sl * exit_fee_rate) * rate
    tp_gain = None if tp is None else n * cv * abs(tp - entry) * rate - (fee + n * cv * tp * exit_fee_rate) * rate
    eq = w["realized_equity_inr"]
    return {
        "mode": mode, "real_money": mode == "live",
        "symbol": sym, "side": req.side, "order_type": req.order_type, "resting_limit": resting, "effect": effect,
        "leverage": req.leverage, "max_leverage": max_lev, "contracts": n, "contract_value": cv, "tick_size": tick,
        "reference_price": raw, "est_entry_price": entry, "best_bid": bid, "best_ask": ask, "mark_price": mark,
        "notional_usd": notional, "notional_inr": notional * rate, "required_margin_usd": margin,
        "required_margin_inr": margin * rate, "est_entry_fee_inr": fee * rate, "fee_type": "maker" if resting else "taker",
        "est_liquidation_price": liq, "maintenance_margin_pct": mm, "sl_price": sl, "tp_price": tp,
        "trail_amount": req.trail_amount, "trigger_source": req.trigger_source, "sl_loss_inr": sl_loss, "tp_gain_inr": tp_gain,
        "sl_loss_pct_of_equity": (sl_loss / eq * 100) if (sl_loss is not None and eq > 0) else None,
        "available_margin_inr": w["available_margin_inr"], "journal_mode": mode,
        "problems": problems, "can_submit": not problems and not st["runner"]["running"],
        "bot_running": st["runner"]["running"],
    }


@router.post("/api/manual/preview")
def preview(req: ManualOrder) -> dict[str, Any]:
    return bot_ops._clean(build_preview(req))


@router.post("/api/manual/order")
def place_order(req: ManualOrder) -> dict[str, Any]:
    with bot_ops._op_lock:
        mode = guard_writable()
        pv = build_preview(req)
        if pv["problems"]:
            raise HTTPException(422, "; ".join(pv["problems"]))
        d = 1 if req.side == "LONG" else -1
        if mode in EXCHANGE_MODES:
            return bot_ops._clean(_place_exchange(mode, req, pv, d))
        with _Session(mode) as s:
            eng = s.engine
            if pv["resting_limit"]:
                it = s.store.create_intent(
                    intent_id=f"mn-{ee.secrets.token_hex(8)}", client_order_id=eng.new_client_oid(pv["symbol"], "L"),
                    mode=mode, symbol=pv["symbol"], product_id=eng.ensure_product(pv["symbol"]).product_id, role="ENTRY",
                    side="buy" if d == 1 else "sell", size=pv["contracts"], order_type="limit_order",
                    entry_price=req.limit_price, sl_price=pv["sl_price"], tp_price=pv["tp_price"],
                    trail_amount=float(req.trail_amount or 0), leverage=req.leverage, source="manual")
                s.store.transition(it["intent_id"], "ACKNOWLEDGED", "paper limit resting")
                _ensure_watcher()
                return bot_ops._clean({"result": "resting", "intent_id": it["intent_id"], "preview": pv})
            it = s.store.create_intent(
                intent_id=f"mn-{ee.secrets.token_hex(8)}", client_order_id=eng.new_client_oid(pv["symbol"], "M"),
                mode=mode, symbol=pv["symbol"], product_id=eng.ensure_product(pv["symbol"]).product_id,
                role="ENTRY" if pv["effect"] in ("open", "add", "flip") else "EXIT",
                side="buy" if d == 1 else "sell", size=pv["contracts"], order_type="market_order",
                entry_price=pv["est_entry_price"], sl_price=pv["sl_price"], tp_price=pv["tp_price"],
                trail_amount=float(req.trail_amount or 0), leverage=req.leverage, source="manual",
                reduce_only=int(pv["effect"] in ("reduce", "close")))
            try:
                r = eng.manual_fill(pv["symbol"], d, pv["contracts"], pv["reference_price"], req.leverage,
                                    sl=pv["sl_price"], tp=pv["tp_price"], trail=req.trail_amount, intent_id=it["intent_id"])
            except ValueError as exc:
                s.store.transition(it["intent_id"], "REJECTED", str(exc), error=str(exc))
                raise HTTPException(422, str(exc)) from exc
            fill = r.get("price") or (r.get("close") or {}).get("exit_price")
            s.store.transition(it["intent_id"], "FILLED", "paper manual fill", filled_size=pv["contracts"], avg_fill_price=fill,
                               exchange_order_id=f"paper-{it['intent_id']}")
            _ensure_watcher()
            return bot_ops._clean({"result": r["action"], "intent_id": it["intent_id"], "fill": r, "preview": pv})


def _place_exchange(mode: str, req: ManualOrder, pv: dict[str, Any], d: int) -> dict[str, Any]:
    with _Session(mode) as s:
        eng = s.engine
        try:
            if pv["effect"] in ("reduce", "close"):
                r = eng.exchange_reduce(pv["symbol"], pv["contracts"])
                return {"result": r["action"], "mode": mode, "fill": r, "preview": pv}
            s.ensure_margin()
            r = eng.manual_exchange_entry(pv["symbol"], d, pv["contracts"], req.leverage, order_type=req.order_type,
                                          ref_price=pv["reference_price"], limit_price=req.limit_price,
                                          sl=pv["sl_price"], tp=pv["tp_price"], trigger=req.trigger_source)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except DeltaAPIError as exc:
            raise HTTPException(422 if exc.status < 500 else 502, f"Delta rejected the order: {_diagnose(exc)} — {exc.error}") from exc
        except ExchangeError as exc:
            raise HTTPException(409, str(exc)) from exc
        result = {"filled": "opened" if pv["effect"] == "open" else "added", "acknowledged": "resting",
                  "partially_filled": "partially_filled", "added": "added"}.get(r.get("action"), r.get("action"))
        return {"result": result, "mode": mode, "intent_id": r.get("intent_id"), "exchange": r, "preview": pv}


@router.delete("/api/manual/order/{order_id}")
def cancel_order(order_id: str) -> dict[str, Any]:
    """Cancel a resting limit: a journal intent id (paper or exchange) or a raw Delta order id (exchange)."""
    with bot_ops._op_lock:
        mode = guard_writable()
        with _Session(mode) as s:
            it = s.store.intent(order_id) or s.store.one(
                "SELECT * FROM order_intents WHERE mode=? AND exchange_order_id=?", (mode, str(order_id)))
            if mode == "paper":
                if it is None or it["state"] != "ACKNOWLEDGED" or it["source"] != "manual":
                    raise HTTPException(404, f"no resting manual order {order_id}")
                s.store.transition(it["intent_id"], "CANCELLED", "cancelled from the manual terminal")
                return {"cancelled": it["intent_id"], "mode": mode}
            if it is not None and it["state"] in ("ACKNOWLEDGED", "PARTIALLY_FILLED"):
                pid, oid = it["product_id"], it["exchange_order_id"]
            else:
                o = next((x for x in exchange_snapshot(mode, ttl=0)["orders"] if str(x.get("id")) == str(order_id)), None)
                if o is None or o.get("stop_order_type"):
                    raise HTTPException(404, f"no open limit order {order_id} on the {mode} exchange")
                pid, oid = o["product_id"], o["id"]
            try:
                r = s.client.cancel_order(pid, order_id=oid)
            except DeltaAPIError as exc:
                raise HTTPException(409, f"Delta refused the cancel: {_diagnose(exc)}") from exc
            if it is not None and it["state"] in ("ACKNOWLEDGED", "PARTIALLY_FILLED"):
                filled = int(r.get("size") or it["size"]) - int(r.get("unfilled_size") or 0)
                if filled > 0:
                    s.engine._apply_order(it["intent_id"], r)
                else:
                    s.store.transition(it["intent_id"], "CANCELLED", "cancelled from the manual terminal", raw_response=str(r)[:2000])
            return {"cancelled": str(oid), "intent_id": it["intent_id"] if it else None, "mode": mode, "exchange_state": r.get("state")}


class ProtectionRequest(BaseModel):
    sl_price: float | None = Field(None, gt=0)
    tp_price: float | None = Field(None, gt=0)
    trail_amount: float | None = Field(None, ge=0)
    sl_pct: float | None = Field(None, gt=0, lt=100)
    tp_pct: float | None = Field(None, gt=0)
    clear_sl: bool = False
    clear_tp: bool = False


@router.put("/api/manual/position/{symbol}/protection")
def set_protection(symbol: str, req: ProtectionRequest) -> dict[str, Any]:
    sym = check_symbol(symbol)
    with bot_ops._op_lock:
        mode = guard_writable()
        if mode in EXCHANGE_MODES and req.trail_amount:
            raise HTTPException(422, "trailing stops are paper-only for manual exchange positions")
        with _Session(mode) as s:
            p = s.store.position(mode, sym)
            if p is None:
                raise HTTPException(404, f"no open {sym} position in the {mode} journal")
            d = 1 if p["side"] == "LONG" else -1
            e = float(p["entry_price"])
            mark = ticker(sym)["mark_price"] or e
            fields: dict[str, Any] = {}
            sl = req.sl_price if req.sl_price is not None else (e * (1 - d * req.sl_pct / 100) if req.sl_pct else None)
            tp = req.tp_price if req.tp_price is not None else (e * (1 + d * req.tp_pct / 100) if req.tp_pct else None)
            tick = s.engine.ensure_product(sym).tick_size
            if sl is not None:
                sl = round(round(sl / tick) * tick, 10)
                if sl >= mark if d == 1 else sl <= mark:
                    raise HTTPException(422, f"stop-loss {sl} would trigger immediately (mark {mark})")
                fields["sl_price"] = sl
            if tp is not None:
                tp = round(round(tp / tick) * tick, 10)
                if tp <= mark if d == 1 else tp >= mark:
                    raise HTTPException(422, f"take-profit {tp} would trigger immediately (mark {mark})")
                fields["tp_price"] = tp
            if req.clear_sl:
                fields["sl_price"] = None
            if req.clear_tp:
                fields["tp_price"] = None
            if req.trail_amount is not None and mode == "paper":
                fields["trail_amount"] = req.trail_amount
            if not fields:
                raise HTTPException(422, "nothing to change")
            try:
                pos = s.engine.set_protection(sym, **fields)
            except DeltaAPIError as exc:
                raise HTTPException(409, f"Delta refused the protection change: {_diagnose(exc)} — {exc.error}") from exc
            return bot_ops._clean({"symbol": sym, "mode": mode, "position": pos, "changed": fields})


class CloseRequest(BaseModel):
    contracts: int | None = Field(None, ge=1)


@router.post("/api/manual/position/{symbol}/close")
def close_position(symbol: str, req: CloseRequest | None = None) -> dict[str, Any]:
    sym = check_symbol(symbol)
    with bot_ops._op_lock:
        mode = guard_writable()
        n = req.contracts if req else None
        with _Session(mode) as s:
            p = s.store.position(mode, sym)
            if p is None and mode in EXCHANGE_MODES:
                # exchange position the journal does not know (opened elsewhere): flatten it reduce-only, not journalled
                ext = next((x for x in exchange_snapshot(mode, ttl=0)["positions"] if x.get("product_symbol") == sym), None)
                if ext is None:
                    raise HTTPException(404, f"no open {sym} position on the {mode} exchange")
                size = int(ext["size"])
                if n is not None and n < abs(size):
                    size = n if size > 0 else -n
                try:
                    r = s.client.close_position_reduce_only(int(ext["product_id"]), size) if n is None else \
                        s.client.place_order(product_id=int(ext["product_id"]), side="sell" if size > 0 else "buy",
                                             size=abs(size), order_type="market_order", reduce_only=True)
                except DeltaAPIError as exc:
                    raise HTTPException(409, f"Delta refused the close: {_diagnose(exc)}") from exc
                return bot_ops._clean({"symbol": sym, "mode": mode, "result": "external_close", "exchange": r,
                                       "note": "position was not opened from this terminal; not journalled"})
            if p is None:
                raise HTTPException(404, f"no open {sym} position")
            if n is not None and n > p["contracts"]:
                raise HTTPException(422, f"position has {p['contracts']} contracts")
            try:
                if mode in EXCHANGE_MODES:
                    r = s.engine.exchange_reduce(sym, n)
                else:
                    tk = ticker(sym, ttl=0)
                    raw = tk["best_bid"] if p["side"] == "LONG" else tk["best_ask"]
                    r = s.engine.manual_close(sym, n, raw)
            except (ValueError, ExchangeError) as exc:
                raise HTTPException(409, str(exc)) from exc
            return bot_ops._clean({"symbol": sym, "mode": mode, "result": r.get("action"), "trade": r,
                                   "remaining": (s.store.position(mode, sym) or {"contracts": 0})["contracts"]})


# --------------------------------------------------------------------------- #
# State for the terminal
# --------------------------------------------------------------------------- #
def _exchange_positions_view(mode: str, journal_positions: list[dict[str, Any]], snap: dict[str, Any]) -> list[dict[str, Any]]:
    """Delta's positions are the truth; the journal adds SL/TP legs and source."""
    rate = settings.FIXED_USD_INR_RATE
    by_sym = {p["symbol"]: p for p in journal_positions}
    rows = []
    for x in snap["positions"]:
        sym = x.get("product_symbol")
        size = int(x["size"])
        j = by_sym.pop(sym, None)
        try:
            cv = float((x.get("product") or {}).get("contract_value") or catalog().get(sym, {}).get("contract_value") or 1)
        except Exception:  # noqa: BLE001
            cv = 1.0
        mark = _f(x.get("mark_price")) or (ticker(sym)["mark_price"] if sym else None)
        entry = float(x.get("entry_price") or 0)
        u = (1 if size > 0 else -1) * abs(size) * cv * (mark - entry) * rate if mark and entry else None
        margin_inr = float(x.get("margin") or 0) * rate
        rows.append({**(j or {}), "symbol": sym, "side": "LONG" if size > 0 else "SHORT", "contracts": abs(size),
                     "contract_value": cv, "entry_price": entry, "mark_price": mark, "unrealized_pnl_inr": u,
                     "roe_pct": (u / margin_inr * 100) if (u is not None and margin_inr) else None, "margin_inr": margin_inr,
                     "liq_price_est": _f(x.get("liquidation_price")), "liq_source": "delta",
                     "leverage": (j or {}).get("leverage") or (round(abs(size) * cv * entry / float(x["margin"]), 2) if x.get("margin") and float(x["margin"]) else None),
                     "source": (j or {}).get("source", "exchange (not journalled)"), "journalled": j is not None,
                     "margin_mode": x.get("margin_mode"), "realized_funding_inr": (_f(x.get("realized_funding")) or 0) * rate})
    for sym, j in by_sym.items():  # journal says open, exchange says flat -> pending sync
        rows.append({**j, "journalled": True, "stale": True, "source": f"{j.get('source', 'manual')} (flat on exchange: Sync)"})
    return rows


@router.get("/api/manual/state")
def state(symbol: str = Query("BTCUSD"), resolution: Literal["1m", "5m", "15m", "1h"] = "5m",
          include_candles: bool = True) -> dict[str, Any]:
    sym = check_symbol(symbol)
    info = product_info(sym)
    mode = journal_mode()
    if mode == "paper":
        _ensure_watcher()
    st = bot_ops.read_status(with_market=True, with_signals=False, mode=mode)
    tk = ticker(sym)
    try:
        ob = _cached(f"ob:{sym}", 2.0, lambda: _pub().get_l2_orderbook(sym, depth=5))
    except Exception:  # noqa: BLE001
        ob = None
    con = bot_ops.ro_conn(mode)
    pending: list[dict[str, Any]] = []
    if con is not None:
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(order_intents)")}
            if "source" in cols:
                pending = [dict(r) for r in con.execute(
                    "SELECT intent_id, symbol, side, size, entry_price AS limit_price, sl_price, tp_price, leverage, created_at_us, "
                    "exchange_order_id, state FROM order_intents WHERE mode=? AND state IN ('ACKNOWLEDGED','PARTIALLY_FILLED') "
                    "AND source='manual' ORDER BY created_at_us DESC", (mode,))]
        finally:
            con.close()
    w = st["wallet"]
    positions = st["positions"]
    exchange: dict[str, Any] | None = None
    if mode in EXCHANGE_MODES:
        try:
            mode_gate(mode)
            if not st["runner"]["running"]:
                with bot_ops._op_lock:
                    sync_journal(mode)
                st = bot_ops.read_status(with_market=True, with_signals=False, mode=mode)
            snap = exchange_snapshot(mode)
            ew = snap["wallet"]
            positions = _exchange_positions_view(mode, st["positions"], snap)
            unreal = sum(p.get("unrealized_pnl_inr") or 0 for p in positions)
            w = {**st["wallet"], "realized_equity_inr": ew["wallet_equity_inr"], "available_margin_inr": ew["available_margin_inr"],
                 "used_margin_inr": ew["used_margin_inr"], "unrealized_pnl_inr": unreal,
                 "total_equity_inr": ew["wallet_equity_inr"] + unreal, "initial_capital_inr": None}
            known = {str(p.get("exchange_order_id")) for p in pending}
            for o in snap["orders"]:
                if o.get("stop_order_type") or str(o.get("id")) in known:
                    continue
                pending.append({"intent_id": None, "exchange_order_id": o["id"], "symbol": o.get("product_symbol"),
                                "side": o.get("side"), "size": o.get("unfilled_size") or o.get("size"),
                                "limit_price": _f(o.get("limit_price")), "sl_price": _f(o.get("bracket_stop_loss_price")),
                                "tp_price": _f(o.get("bracket_take_profit_price")), "leverage": None, "state": o.get("state"),
                                "external": True})
            exchange = {"connected": True, "wallet": ew, "snapshot_age_s": round(time.time() - snap["at"], 1),
                        "protective_orders": len([o for o in snap["orders"] if o.get("stop_order_type")])}
        except HTTPException as exc:
            exchange = {"connected": False, "error": exc.detail}
        except (ExchangeError, ReadOnlyClientError) as exc:
            exchange = {"connected": False, "error": _diagnose(exc)}
        else:
            if not st["runner"]["running"]:
                pass
        if exchange and not exchange.get("connected"):
            w = {**w, "realized_equity_inr": None, "available_margin_inr": None, "used_margin_inr": None, "unrealized_pnl_inr": None,
                 "total_equity_inr": None}
    else:
        w = {**w, "used_margin_inr": sum(p["margin_inr"] for p in positions)}
    return bot_ops._clean({
        "symbol": sym, "product": info, "ticker": tk, "orderbook": ob,
        "candles": {"resolution": resolution, "bars": candles(sym, resolution) if include_candles else []},
        "mode": mode, "real_money": mode == "live", "exchange": exchange,
        "journal": {"mode": mode, "exists": st["db_exists"], "path": st["db"]},
        "bot": st["runner"],
        "manual_enabled": (not st["runner"]["running"]) and (mode == "paper" or bool(exchange and exchange.get("connected"))),
        "wallet": w, "positions": positions, "pending_orders": pending, "trades": st["trades"],
        "watcher": {k: v for k, v in _watch.items() if k != "thread"}, "coins": dm.app_symbols(),
        "slippage_bps": settings.SIZING_SLIPPAGE_BPS if mode == "paper" else 0.0,
        "gates": {"live_gates_open": settings.is_live, "live_keys": all(keys_for("live")), "testnet_keys": all(keys_for("testnet"))},
    })


# --------------------------------------------------------------------------- #
# Paper tick watcher (bot stopped)
# --------------------------------------------------------------------------- #
def watch_once(prices: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
    """One protection pass over PAPER positions / resting limits. ``prices`` injects {sym: {last, mark, funding_rate_pct}}."""
    if bot_ops.runner()[0]:
        return {"status": "auto-bot running (it manages positions)"}
    con = bot_ops.ro_conn("paper")
    if con is None:
        return {"status": "no paper journal"}
    try:
        cfg = con.execute("SELECT mode FROM bot_config_state WHERE id=1").fetchone()
        if cfg is None or cfg["mode"] != "paper":
            return {"status": f"journal mode {cfg['mode'] if cfg else '-'}: exchange-resident orders protect positions"}
        syms = {r[0] for r in con.execute("SELECT symbol FROM open_positions WHERE mode='paper'")}
        cols = {r[1] for r in con.execute("PRAGMA table_info(order_intents)")}
        if "source" in cols:
            syms |= {r[0] for r in con.execute("SELECT symbol FROM order_intents WHERE mode='paper' AND state='ACKNOWLEDGED' "
                                               "AND source='manual'")}
    finally:
        con.close()
    if not syms:
        return {"status": "idle (no paper positions or resting orders)"}
    ticks = {}
    for sym in sorted(syms):
        if prices and sym in prices:
            ticks[sym] = prices[sym]
            continue
        tk = ticker(sym, ttl=0.5)
        ticks[sym] = {"last": tk["last_price"] or tk["mark_price"], "mark": tk["mark_price"], "funding_rate_pct": tk["funding_rate_pct"]}
    actions = []
    with bot_ops._op_lock:
        if bot_ops.runner()[0]:
            return {"status": "auto-bot started"}
        with _Session("paper") as s:
            t_us = s.engine.clock()
            for sym, tk in ticks.items():
                last, mark = tk["last"], tk.get("mark")
                fr = tk.get("funding_rate_pct")
                for f in s.engine.check_pending_limits(sym, t_us, last, last):
                    actions.append({"symbol": sym, "kind": "limit_fill", **f})
                a = s.engine.on_tick(sym, t_us, last, mark, None if fr is None else fr / 100.0)
                if a:
                    actions.append({"symbol": sym, "kind": a.get("action"), **{k: a.get(k) for k in ("reason", "price", "new_sl", "net_pnl_inr")}})
    return {"status": "ran", "symbols": sorted(syms), "actions": actions, "ticks": ticks}


def _watch_loop() -> None:
    while True:
        try:
            r = watch_once()
            _watch.update(status=r["status"], last_run=time.time(), runs=_watch["runs"] + 1, last_error=None)
            if r.get("actions"):
                _watch["last_actions"] = (r["actions"] + _watch["last_actions"])[:20]
        except Exception as exc:  # noqa: BLE001 - the watcher must survive network / lock hiccups
            _watch.update(last_error=f"{type(exc).__name__}: {exc}", last_run=time.time())
        time.sleep(WATCH_INTERVAL_S)


def _ensure_watcher() -> None:
    t = _watch.get("thread")
    if t is None or not t.is_alive():
        t = threading.Thread(target=_watch_loop, name="manual-paper-watcher", daemon=True)
        _watch["thread"] = t
        _watch["status"] = "starting"
        t.start()
