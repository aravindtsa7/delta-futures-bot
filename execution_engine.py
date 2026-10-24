"""Step 5 execution engine: durable SQLite journal, lot sizer, paper / testnet / live executor.

Modes (stored in bot_config_state.mode; a DB is bound to one mode for life):
  paper    public market data only; fills are simulated and journalled in SQLite.
  testnet  Delta India demo account via DeltaRestClientAdapter("testnet").
  live     real account; DeltaRestClientAdapter refuses to build unless MODE="live" AND
           LIVE_TRADING_ENABLED=True.

Frozen per-symbol profiles (Step 4B; NOT validated — every variant was REJECTED by the Step 4B
screen, so these are forward paper-trading hypotheses):
  ETHUSD  Variant C  bollinger_volume_breakout 1h + cost gate
  BTCUSD  Variant D  bollinger_volume_breakout 1h + closed-4h EMA50/200 trend + cost gate
  both    ATR(14) stop 2x (clamped 0.8-4%), TP 5x, 2.5x ATR trail from the next 1m candle,
          ₹50,000, 10x isolated, 1% risk, 5% daily loss limit, strict whole-lot sizing.

Safety rules
  * Every order is journalled as an intent BEFORE dispatch (CREATED -> DISPATCHING -> ...).
  * A dispatch that times out becomes OUTCOME_UNKNOWN; new entries on that symbol are blocked
    until reconcile_unknown_intents() resolves it from the exchange. Nothing is resent.
  * Stop-loss trailing on a filled position edits the SL child order in place (PUT /v2/orders).
  * One writer per DB file (OS file lock), WAL journal, synchronous=FULL.
  * Step 9: every exchange fill is booked through one path (_apply_order -> _book_fill_delta), by intent role
    (ENTRY opens/adds, EXIT/reduce-only books a trade), in ONE SQLite transaction with the intent's
    filled_size_booked and the fill ids (booked_fills), so partial fills, timeouts and restarts book exactly once.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np
import pandas as pd

import data_manager as dm
import strategies
from backtester import Backtester, BacktestConfig, ProductSpec
from config import settings
from notifier import TelegramNotifier
from exchange_client import (
    AmbiguousOutcomeError,
    DeltaAPIError,
    DeltaRestClientAdapter,
    ExchangeError,
    OrderPriceMismatchError,
    ProductInfo,
    RateLimitedError,
    snap_to_tick,
)

log = logging.getLogger(__name__)

CAPITAL_INR = 50_000.0
LEVERAGE = 10
RISK_PCT = 1.0
DAILY_LOSS_LIMIT_PCT = 5.0
SL_ATR_MULT, TP_ATR_MULT, TRAIL_ATR_MULT = 2.0, 5.0, 2.5
SL_MIN_PCT, SL_MAX_PCT = 0.8, 4.0
MARGIN_BUFFER = 0.95
GATE_MULT = 2.0
GATE_SLIPPAGE_BPS = 5.0      # cost-gate threshold exactly as screened in Step 4B (primary 5 bps)
TIMEFRAME, HTF = "1h", "4h"
STRATEGY = "bollinger_volume_breakout"
COOLDOWN_BARS = 1
TRIGGER_METHOD = "last_traded_price"
UNKNOWN_GRACE_US = 60_000_000  # an intent unseen by the exchange this long after dispatch is REJECTED
STEP4B_ANCHOR = settings.data_path / "results" / "step4b_windows.json"

STATES = ("CREATED", "DISPATCHING", "ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED", "OUTCOME_UNKNOWN")
TRANSITIONS: dict[str, set[str]] = {
    "CREATED": {"DISPATCHING", "FILLED", "REJECTED", "CANCELLED", "ACKNOWLEDGED"},  # paper fills / resting paper limits
    "DISPATCHING": {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED", "OUTCOME_UNKNOWN"},
    "ACKNOWLEDGED": {"PARTIALLY_FILLED", "FILLED", "CANCELLED"},
    "PARTIALLY_FILLED": {"FILLED", "CANCELLED"},
    "OUTCOME_UNKNOWN": {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED"},
    "FILLED": set(), "CANCELLED": set(), "REJECTED": set(),
}
BLOCKING_STATES = ("DISPATCHING", "OUTCOME_UNKNOWN", "ACKNOWLEDGED", "PARTIALLY_FILLED")
UNRESOLVED_STATES = ("DISPATCHING", "OUTCOME_UNKNOWN")
# Only these margin-mode rejections are benign, and only after a readback shows isolated (F09).
BENIGN_MARGIN_CODES = ("margin_mode_already_set", "already_in_isolated_margin_mode", "already_isolated")


class EntryBlockedError(ExchangeError):
    """Refused by the shared order gate: an unresolved intent on the symbol, or a duplicate client_request_id."""


@dataclass(frozen=True)
class StrategyProfile:
    symbol: str
    variant: str
    strategy: str
    timeframe: str
    trend_filter_4h: bool
    cost_gate: bool


FROZEN_PROFILES: dict[str, StrategyProfile] = {
    "ETHUSD": StrategyProfile("ETHUSD", "C_costgate", STRATEGY, TIMEFRAME, False, True),
    "BTCUSD": StrategyProfile("BTCUSD", "D_both", STRATEGY, TIMEFRAME, True, True),
}


VARIANT_FLAGS = {"A_baseline": (False, False), "B_trend4h": (True, False), "C_costgate": (False, True), "D_both": (True, True)}
DEFAULT_VARIANT = "C_costgate"
BOT_PROFILES_FILE = settings.data_path / "bot_profiles.json"


def make_profile(symbol: str, variant: str = DEFAULT_VARIANT) -> StrategyProfile:
    trend, gate = VARIANT_FLAGS[variant]
    return StrategyProfile(symbol, variant, STRATEGY, TIMEFRAME, trend, gate)


def bot_profiles() -> dict[str, StrategyProfile]:
    """Auto-bot universe: BTCUSD (D) + ETHUSD (C) + every coin added in the dashboard (default C_costgate).
    data/bot_profiles.json overrides per symbol: {"SOLUSD": {"enabled": false}} or {"XRPUSD": {"variant": "D_both"}}.
    Since Step 9, POST /api/manual/symbols writes {"variant": "C_costgate", "enabled": false} for a new coin
    (see set_bot_profile_default), so added coins only auto-trade after an explicit opt-in."""
    out = dict(FROZEN_PROFILES)
    for sym in dm.app_symbols():
        if sym not in out and dm.metadata_path(sym).exists():
            out[sym] = make_profile(sym)
    if BOT_PROFILES_FILE.exists():
        for sym, ov in json.loads(BOT_PROFILES_FILE.read_text(encoding="utf-8")).items():
            if ov.get("enabled") is False:
                out.pop(sym, None)
            elif ov.get("variant") in VARIANT_FLAGS and (sym in out or dm.metadata_path(sym).exists()):
                out[sym] = make_profile(sym, ov["variant"])
    return out


def set_bot_profile_default(symbol: str, enable: bool = False, variant: str = DEFAULT_VARIANT) -> dict[str, Any]:
    """Section 5 guard: a coin added from the dashboard gets {"variant": "C_costgate", "enabled": false} in
    data/bot_profiles.json, so it never auto-trades unless explicitly enabled. An existing entry is kept (only
    ``enable=True`` switches it on)."""
    data = json.loads(BOT_PROFILES_FILE.read_text(encoding="utf-8")) if BOT_PROFILES_FILE.exists() else {}
    if symbol in FROZEN_PROFILES:
        return data.get(symbol, {})
    entry = data.get(symbol)
    if entry is None:
        entry = data[symbol] = {"variant": variant, "enabled": bool(enable)}
    elif enable:
        entry["enabled"] = True
    else:
        return entry
    BOT_PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BOT_PROFILES_FILE.with_name(BOT_PROFILES_FILE.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, BOT_PROFILES_FILE)
    return entry


def liquidation_price(entry: float, side: str, leverage: float, mm_pct: float) -> float:
    """Isolated-margin estimate: long entry*(1 - 1/lev + mm), short entry*(1 + 1/lev - mm). Fees/insurance ignored."""
    d = 1 if side in ("LONG", "buy") else -1
    return entry * (1 - d * (1.0 / leverage - mm_pct / 100.0))


def bankruptcy_price(entry: float, side: str, leverage: float) -> float:
    d = 1 if side in ("LONG", "buy") else -1
    return entry * (1 - d / leverage)


def now_us() -> int:
    return int(time.time() * 1_000_000)


def to_ts(us: int | None) -> str:
    return "-" if us is None else pd.Timestamp(int(us), unit="us", tz="UTC").strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# Single-writer SQLite store
# --------------------------------------------------------------------------- #
class StateStoreLockedError(RuntimeError):
    pass


class _Row(dict):
    """dict with sqlite3.Row-style keys() so position dicts and rows are interchangeable."""


class _ProcessLock:
    """Exclusive OS lock on '<db>.lock'; released automatically if the process dies."""

    def __init__(self, path: Path):
        self.path = path
        self.fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
        try:
            if sys.platform == "win32":
                import msvcrt
                os.lseek(self.fd, 0, 0)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.fd)
            raise StateStoreLockedError(f"{path} is held by another writer") from exc

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                os.lseek(self.fd, 0, 0)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(self.fd)
            self.fd = None


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS bot_config_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL CHECK (mode IN ('paper','testnet','live')),
    profiles_json TEXT NOT NULL,
    capital_inr REAL NOT NULL,
    leverage REAL NOT NULL,
    risk_pct REAL NOT NULL,
    daily_loss_limit_pct REAL NOT NULL,
    usd_inr_rate REAL NOT NULL,
    sizing_slippage_bps REAL NOT NULL,
    halted INTEGER NOT NULL DEFAULT 0,
    entries_paused INTEGER NOT NULL DEFAULT 0,
    halt_reason TEXT,
    halt_until_us INTEGER,
    bound_user_id INTEGER,
    updated_at_us INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS order_intents (
    intent_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE CHECK (length(client_order_id) BETWEEN 1 AND 32),
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('ENTRY','EXIT')),
    side TEXT NOT NULL CHECK (side IN ('buy','sell')),
    size INTEGER NOT NULL CHECK (size > 0),
    order_type TEXT NOT NULL,
    reduce_only INTEGER NOT NULL DEFAULT 0,
    entry_price REAL,
    sl_price REAL,
    tp_price REAL,
    trail_amount REAL,
    state TEXT NOT NULL CHECK (state IN ({",".join(f"'{s}'" for s in STATES)})),
    exchange_order_id TEXT,
    sl_leg_order_id TEXT,
    tp_leg_order_id TEXT,
    filled_size INTEGER NOT NULL DEFAULT 0,
    avg_fill_price REAL,
    signal_bar_us INTEGER,
    atr REAL,
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    raw_response TEXT,
    error TEXT,
    leverage REAL,
    source TEXT DEFAULT 'bot'
);
CREATE INDEX IF NOT EXISTS ix_intents_state ON order_intents(state, symbol);
CREATE TABLE IF NOT EXISTS intent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
    from_state TEXT, to_state TEXT NOT NULL, at_us INTEGER NOT NULL, note TEXT
);
CREATE TABLE IF NOT EXISTS open_positions (
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('LONG','SHORT')),
    contracts INTEGER NOT NULL CHECK (contracts > 0),
    contract_value REAL NOT NULL,
    entry_price REAL NOT NULL,
    raw_entry_price REAL NOT NULL,
    initial_sl REAL,
    sl_price REAL,
    tp_price REAL,
    trail_amount REAL NOT NULL DEFAULT 0,
    sl_order_id TEXT,
    tp_order_id TEXT,
    highest_fav_price REAL NOT NULL,
    opened_at_us INTEGER NOT NULL,
    last_bar_us INTEGER NOT NULL,
    realized_funding_usd REAL NOT NULL DEFAULT 0,
    entry_fee_usd REAL NOT NULL,
    margin_usd REAL NOT NULL,
    atr_at_signal REAL NOT NULL DEFAULT 0,
    intent_id TEXT NOT NULL,
    leverage REAL NOT NULL DEFAULT 10,
    source TEXT NOT NULL DEFAULT 'bot',
    PRIMARY KEY (mode, symbol)
);
CREATE TABLE IF NOT EXISTS trade_history (
    trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    contracts INTEGER NOT NULL,
    contract_value REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    raw_entry_price REAL,
    raw_exit_price REAL,
    initial_sl REAL,
    final_sl REAL,
    tp_price REAL,
    exit_reason TEXT NOT NULL,
    opened_at_us INTEGER NOT NULL,
    closed_at_us INTEGER NOT NULL,
    gross_pnl_usd REAL NOT NULL,
    fees_gst_usd REAL NOT NULL,
    funding_usd REAL NOT NULL,
    slippage_usd REAL NOT NULL,
    net_pnl_usd REAL NOT NULL,
    usd_inr_rate REAL NOT NULL,
    gross_pnl_inr REAL NOT NULL,
    fees_gst_inr REAL NOT NULL,
    funding_inr REAL NOT NULL,
    net_pnl_inr REAL NOT NULL,
    equity_after_inr REAL NOT NULL,
    entry_intent_id TEXT,
    exit_order_id TEXT,
    source TEXT DEFAULT 'bot',
    leverage REAL DEFAULT 10
);
CREATE TABLE IF NOT EXISTS funding_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL, symbol TEXT NOT NULL, at_us INTEGER NOT NULL,
    rate REAL NOT NULL, mark_price REAL NOT NULL, contracts INTEGER NOT NULL, cashflow_usd REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS symbol_state (
    mode TEXT NOT NULL, symbol TEXT NOT NULL,
    last_signal_bar_us INTEGER, cooldown_until_us INTEGER,
    PRIMARY KEY (mode, symbol)
);
CREATE TABLE IF NOT EXISTS booked_fills (
    fill_id TEXT PRIMARY KEY, intent_id TEXT, order_id TEXT, symbol TEXT, role TEXT, side TEXT,
    size INTEGER, price REAL, fee_usd REAL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS system_meta (key TEXT PRIMARY KEY, value TEXT, updated_at_us INTEGER);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at_us INTEGER NOT NULL, mode TEXT, level TEXT NOT NULL, kind TEXT NOT NULL, symbol TEXT, detail TEXT
);
"""


class StateStore:
    def __init__(self, path: Path | str, clock: Callable[[], int] = now_us):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = _ProcessLock(self.path.with_name(self.path.name + ".lock"))
        self._tx_depth = 0
        try:
            # check_same_thread=False: the runner offloads REST work to worker threads (it serialises access itself)
            self.conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=10, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.journal_mode = self.conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self._migrate()
        except Exception:
            self._lock.release()
            raise

    def _cols(self, table: str) -> dict[str, sqlite3.Row]:
        return {r["name"]: r for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        """Bring journals created by earlier steps up to the current schema (idempotent)."""
        c = self.conn
        if "entries_paused" not in self._cols("bot_config_state"):  # Step 7
            c.execute("ALTER TABLE bot_config_state ADD COLUMN entries_paused INTEGER NOT NULL DEFAULT 0")
        op = self._cols("open_positions")
        if op["sl_price"]["notnull"]:  # Step 8: SL/TP optional for manual positions -> rebuild the table
            old = [n for n in op]
            ddl = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS open_positions"):]
            ddl = ddl[:ddl.index(");") + 2]
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute("ALTER TABLE open_positions RENAME TO open_positions_v7")
                c.execute(ddl)
                cols = ",".join(old)
                c.execute(f"INSERT INTO open_positions ({cols}) SELECT {cols} FROM open_positions_v7")
                c.execute("DROP TABLE open_positions_v7")
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        for table, col, decl in (("open_positions", "leverage", "REAL NOT NULL DEFAULT 10"),
                                 ("open_positions", "source", "TEXT NOT NULL DEFAULT 'bot'"),
                                 ("trade_history", "source", "TEXT DEFAULT 'bot'"),
                                 ("trade_history", "leverage", "REAL DEFAULT 10"),
                                 ("order_intents", "leverage", "REAL"),
                                 ("order_intents", "source", "TEXT DEFAULT 'bot'"),
                                 ("order_intents", "client_request_id", "TEXT"),
                                 ("order_intents", "exit_reason", "TEXT")):
            if col not in self._cols(table):
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        if "filled_size_booked" not in self._cols("order_intents"):  # Step 9
            with self.tx():
                c.execute("ALTER TABLE order_intents ADD COLUMN filled_size_booked INTEGER NOT NULL DEFAULT 0")
                # earlier code booked FILLED intents in full; PARTIALLY_FILLED ones were never booked (F04)
                c.execute("UPDATE order_intents SET filled_size_booked = filled_size WHERE state = 'FILLED'")

    def close(self) -> None:
        if getattr(self, "conn", None) is not None:
            self.conn.close()
            self.conn = None
        self._lock.release()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT. Re-entrant: a nested tx() joins the outer transaction, so composite
        bookings (intent + fills + position + trade) commit or roll back as one unit."""
        if self._tx_depth:
            self._tx_depth += 1
            try:
                yield self.conn
            finally:
                self._tx_depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._tx_depth = 1
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self._tx_depth = 0

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    # -- config ---------------------------------------------------------------- #
    def init_config(self, mode: str, profiles: dict[str, StrategyProfile]) -> sqlite3.Row:
        row = self.config()
        if row is not None:
            if row["mode"] != mode:
                raise RuntimeError(f"{self.path} is bound to mode {row['mode']!r}; refusing to run it as {mode!r}")
            return row
        with self.tx() as c:
            c.execute(
                "INSERT INTO bot_config_state (id, mode, profiles_json, capital_inr, leverage, risk_pct, daily_loss_limit_pct,"
                " usd_inr_rate, sizing_slippage_bps, halted, updated_at_us) VALUES (1,?,?,?,?,?,?,?,?,0,?)",
                (mode, json.dumps({k: asdict(v) for k, v in profiles.items()}, sort_keys=True), CAPITAL_INR, LEVERAGE,
                 RISK_PCT, DAILY_LOSS_LIMIT_PCT, settings.FIXED_USD_INR_RATE, settings.SIZING_SLIPPAGE_BPS, self.clock()))
        return self.config()

    def config(self) -> sqlite3.Row | None:
        return self.one("SELECT * FROM bot_config_state WHERE id = 1")

    def set_halt(self, halted: bool, reason: str | None = None, until_us: int | None = None) -> None:
        with self.tx() as c:
            c.execute("UPDATE bot_config_state SET halted=?, halt_reason=?, halt_until_us=?, updated_at_us=? WHERE id=1",
                      (int(halted), reason, until_us, self.clock()))

    def set_bound_user(self, user_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE bot_config_state SET bound_user_id=?, updated_at_us=? WHERE id=1", (user_id, self.clock()))

    def set_entries_paused(self, paused: bool) -> None:
        with self.tx() as c:
            c.execute("UPDATE bot_config_state SET entries_paused=?, updated_at_us=? WHERE id=1", (int(paused), self.clock()))

    def event(self, level: str, kind: str, symbol: str | None = None, detail: Any = None, mode: str | None = None) -> int:
        d = detail if isinstance(detail, str) or detail is None else json.dumps(detail, default=str)
        with self.tx() as c:
            cur = c.execute("INSERT INTO events (at_us, mode, level, kind, symbol, detail) VALUES (?,?,?,?,?,?)",
                            (self.clock(), mode, level, kind, symbol, d))
            return int(cur.lastrowid)

    # -- intents ----------------------------------------------------------------- #
    def create_intent(self, **f: Any) -> sqlite3.Row:
        t = self.clock()
        f = {**f, "state": "CREATED", "created_at_us": t, "updated_at_us": t}
        cols = ",".join(f)
        with self.tx() as c:
            c.execute(f"INSERT INTO order_intents ({cols}) VALUES ({','.join('?' * len(f))})", tuple(f.values()))
            c.execute("INSERT INTO intent_events (intent_id, from_state, to_state, at_us, note) VALUES (?,?,?,?,?)",
                      (f["intent_id"], None, "CREATED", t, None))
        return self.intent(f["intent_id"])

    def intent(self, intent_id: str) -> sqlite3.Row:
        return self.one("SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,))

    def transition(self, intent_id: str, new_state: str, note: str | None = None, **fields: Any) -> sqlite3.Row:
        t = self.clock()
        with self.tx() as c:
            cur = c.execute("SELECT state FROM order_intents WHERE intent_id = ?", (intent_id,)).fetchone()
            if cur is None:
                raise KeyError(intent_id)
            old = cur["state"]
            if new_state != old and new_state not in TRANSITIONS[old]:
                raise RuntimeError(f"illegal intent transition {old} -> {new_state} ({intent_id})")
            sets = {"state": new_state, "updated_at_us": t, **fields}
            c.execute(f"UPDATE order_intents SET {', '.join(f'{k}=?' for k in sets)} WHERE intent_id = ?",
                      (*sets.values(), intent_id))
            if new_state != old:
                c.execute("INSERT INTO intent_events (intent_id, from_state, to_state, at_us, note) VALUES (?,?,?,?,?)",
                          (intent_id, old, new_state, t, note))
        return self.intent(intent_id)

    def update_intent(self, intent_id: str, **fields: Any) -> None:
        with self.tx() as c:
            c.execute(f"UPDATE order_intents SET {', '.join(f'{k}=?' for k in fields)}, updated_at_us=? WHERE intent_id=?",
                      (*fields.values(), self.clock(), intent_id))

    def intent_path(self, intent_id: str) -> list[str]:
        return [r["to_state"] for r in self.q("SELECT to_state FROM intent_events WHERE intent_id=? ORDER BY id", (intent_id,))]

    # -- positions / trades -------------------------------------------------- #
    def position(self, mode: str, symbol: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM open_positions WHERE mode=? AND symbol=?", (mode, symbol))

    def positions(self, mode: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM open_positions WHERE mode=? ORDER BY symbol", (mode,))

    def insert_position(self, **f: Any) -> None:
        with self.tx() as c:
            c.execute(f"INSERT INTO open_positions ({','.join(f)}) VALUES ({','.join('?' * len(f))})", tuple(f.values()))

    def update_position(self, mode: str, symbol: str, **f: Any) -> None:
        with self.tx() as c:
            c.execute(f"UPDATE open_positions SET {', '.join(f'{k}=?' for k in f)} WHERE mode=? AND symbol=?",
                      (*f.values(), mode, symbol))

    def close_position(self, mode: str, symbol: str, trade: dict[str, Any], cooldown_until_us: int) -> int:
        """Atomically: insert the closed trade, delete the open position, set the cooldown."""
        with self.tx() as c:
            cur = c.execute(f"INSERT INTO trade_history ({','.join(trade)}) VALUES ({','.join('?' * len(trade))})",
                            tuple(trade.values()))
            c.execute("DELETE FROM open_positions WHERE mode=? AND symbol=?", (mode, symbol))
            c.execute("INSERT INTO symbol_state (mode, symbol, cooldown_until_us) VALUES (?,?,?) "
                      "ON CONFLICT(mode, symbol) DO UPDATE SET cooldown_until_us=excluded.cooldown_until_us",
                      (mode, symbol, cooldown_until_us))
            return int(cur.lastrowid)

    def reduce_position(self, mode: str, symbol: str, trade: dict[str, Any], remaining: dict[str, Any]) -> int:
        """Atomically book a partial close and shrink the open position."""
        with self.tx() as c:
            cur = c.execute(f"INSERT INTO trade_history ({','.join(trade)}) VALUES ({','.join('?' * len(trade))})",
                            tuple(trade.values()))
            c.execute(f"UPDATE open_positions SET {', '.join(f'{k}=?' for k in remaining)} WHERE mode=? AND symbol=?",
                      (*remaining.values(), mode, symbol))
            return int(cur.lastrowid)

    def trades(self, mode: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM trade_history WHERE mode=? ORDER BY trade_id", (mode,))

    def symbol_state(self, mode: str, symbol: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM symbol_state WHERE mode=? AND symbol=?", (mode, symbol))

    def mark_signal_seen(self, mode: str, symbol: str, bar_us: int) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO symbol_state (mode, symbol, last_signal_bar_us) VALUES (?,?,?) "
                      "ON CONFLICT(mode, symbol) DO UPDATE SET last_signal_bar_us=excluded.last_signal_bar_us",
                      (mode, symbol, bar_us))

    # -- booked fills / metadata (Step 9) --------------------------------------- #
    def record_fills(self, rows: list[dict[str, Any]]) -> None:
        with self.tx() as c:
            for r in rows:
                c.execute("INSERT INTO booked_fills (fill_id, intent_id, order_id, symbol, role, side, size, price, fee_usd, created_at)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?)", (r["fill_id"], r.get("intent_id"), r.get("order_id"), r["symbol"],
                                                            r["role"], r.get("side"), int(r["size"]), float(r["price"]),
                                                            float(r["fee_usd"]), to_ts(self.clock())))

    def booked_fill_ids(self, symbol: str | None = None) -> set[str]:
        sql, args = "SELECT fill_id FROM booked_fills", ()
        if symbol:
            sql, args = sql + " WHERE symbol=?", (symbol,)
        return {r["fill_id"] for r in self.q(sql, args)}

    def meta_get(self, key: str) -> str | None:
        r = self.one("SELECT value FROM system_meta WHERE key=?", (key,))
        return None if r is None else r["value"]

    def meta_set(self, **kv: Any) -> None:
        with self.tx() as c:
            for k, v in kv.items():
                c.execute("INSERT INTO system_meta (key, value, updated_at_us) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                          "value=excluded.value, updated_at_us=excluded.updated_at_us", (k, None if v is None else str(v), self.clock()))


# --------------------------------------------------------------------------- #
# Lot sizing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LotSizing:
    symbol: str
    side: str
    price: float
    atr: float
    contracts: int
    risk_contracts: int
    margin_cap_contracts: int
    sl_distance: float
    tp_distance: float
    sl_price: float
    tp_price: float
    trail_amount: float
    loss_per_contract_usd: float
    risk_budget_usd: float
    risk_usd: float
    notional_usd: float
    notional_inr: float
    margin_usd: float
    margin_inr: float
    equity_inr: float
    available_inr: float
    reason: str


def sizing_config(equity_inr: float, risk_pct: float = RISK_PCT, leverage: float = LEVERAGE,
                  slippage_bps: float | None = None) -> BacktestConfig:
    """The Step 4B backtest config the live sizer shares (taker fees + GST, strict lots)."""
    return BacktestConfig(
        initial_capital_inr=equity_inr, leverage=leverage, risk_per_trade_pct=risk_pct,
        max_daily_loss_pct=DAILY_LOSS_LIMIT_PCT, fee_mode="taker", trigger_source="trade", cooldown_bars=COOLDOWN_BARS,
        sl_mode="atr", sl_atr_mult=SL_ATR_MULT, tp_atr_mult=TP_ATR_MULT, atr_sl_min_pct=SL_MIN_PCT,
        atr_sl_max_pct=SL_MAX_PCT, trailing_atr_mult=TRAIL_ATR_MULT, margin_buffer=MARGIN_BUFFER,
        allow_min_one_lot=False, mark_fill_mode="traded",
        slippage_bps=settings.SIZING_SLIPPAGE_BPS if slippage_bps is None else slippage_bps,
    )


def calculate_order_lots(product: ProductSpec, price: float, atr_value: float, side: str = "LONG",
                         equity_inr: float = CAPITAL_INR, available_inr: float | None = None,
                         risk_pct: float = RISK_PCT, leverage: float = LEVERAGE,
                         slippage_bps: float | None = None) -> LotSizing:
    """Whole contracts for one entry: 1% risk at the ATR stop incl. round-trip taker+GST and slippage,
    capped by 95% of available margin at 10x. 0 contracts if even one lot exceeds the risk budget."""
    if not (price > 0 and atr_value > 0):
        raise ValueError("price and ATR must be > 0")
    d = 1 if side.upper() in ("LONG", "BUY") else -1
    cfg = sizing_config(equity_inr, risk_pct, leverage, slippage_bps)
    bt = Backtester(cfg)
    rate = settings.FIXED_USD_INR_RATE
    # sl_distance = clamp(2 x ATR, 0.8%, 4%) of price; tp_distance = sl_distance x 5/2 (backtester's own math).
    sl_pct, tp_pct = cfg.stop_pcts(price, atr_value)
    sl_distance, tp_distance = price * sl_pct / 100, price * tp_pct / 100
    sl, tp = bt.levels(price, d, sl_pct, tp_pct, product)
    trail = float(snap_to_tick(TRAIL_ATR_MULT * atr_value, product.tick_size))
    available_inr = equity_inr if available_inr is None else available_inr
    n, n_risk, n_cap, _ = bt.size_position(equity_inr / rate, price, sl, product, available_inr / rate)
    lpc = bt.loss_per_contract(price, sl, product)
    if n >= 1:
        reason = "ok"
    elif n_risk == 0:
        reason = "1 lot exceeds the risk budget"
    else:
        reason = "insufficient available margin"
    notional = n * product.contract_value * price
    return LotSizing(product.symbol, "LONG" if d == 1 else "SHORT", price, atr_value, n, n_risk, n_cap, sl_distance,
                     tp_distance, sl, tp, trail, lpc, equity_inr / rate * risk_pct / 100, n * lpc, notional,
                     notional * rate, notional / leverage, notional / leverage * rate, equity_inr, available_inr, reason)


# --------------------------------------------------------------------------- #
# Live signal evaluation (frozen Step 4B strategies)
# --------------------------------------------------------------------------- #
def step4b_history_start() -> pd.Timestamp | None:
    """Warm-up start pinned by Step 4B, so EMA seeds match the backtest exactly."""
    if STEP4B_ANCHOR.exists():
        return pd.Timestamp(json.loads(STEP4B_ANCHOR.read_text())["warmup_start"])
    return None


def sync_market_data(symbols: list[str], days: int = 90) -> dict[str, int]:
    """Incrementally append the newest closed 1m TRADE/MARK/FUNDING candles (existing history is kept)."""
    fetched = {}
    for sym in symbols:
        for st in dm.SERIES_TYPES:
            r = dm.download_1m_series(sym, st, days=days)
            fetched[f"{sym}:{st}"] = r.rows_fetched
    return fetched


def load_history(symbol: str, history_start: pd.Timestamp | None = None) -> pd.DataFrame:
    hist = dm.load_unified_1m_dataset(symbol)
    start = history_start if history_start is not None else step4b_history_start()
    if start is not None:
        hist = hist[hist.index >= start]
    return hist


def evaluate_symbol(profile: StrategyProfile, hist: pd.DataFrame, at: pd.Timestamp | None = None) -> dict[str, Any]:
    """Frozen-variant state on the latest CLOSED 1h bar (optionally only using data before ``at``)."""
    if at is not None:
        hist = hist[hist.index < at]
    tf_min = dm.TIMEFRAME_MINUTES[profile.timeframe]
    bars = dm.resample_candles(hist, profile.timeframe)
    out = strategies.generate_signals(profile.strategy, bars)
    atr14 = strategies.atr(bars, 14)
    filters: dict[str, tuple[pd.Series, pd.Series]] = {}
    trend = None
    if profile.trend_filter_4h:
        bars_4h = dm.resample_candles(hist, HTF)
        trend = strategies.trend_filter_4h(bars, bars_4h, tf_min)
        filters["trend4h"] = (trend["allow_long"], trend["allow_short"])
    gate = strategies.cost_gate(bars, settings.TAKER_FEE, settings.GST_ON_FEES, GATE_SLIPPAGE_BPS, GATE_MULT)
    if profile.cost_gate:
        filters["costgate"] = (gate["allow"], gate["allow"])
    sig, _ = strategies.apply_entry_filters(out, filters)
    t = bars.index[-1]
    last_1m = hist.index[-1]
    if trend is None:  # report the regime even when the variant does not filter on it
        trend = strategies.trend_filter_4h(bars.iloc[-1:], dm.resample_candles(hist, HTF), tf_min)
    tr = trend.iloc[-1]
    s = int(sig.iloc[-1])
    return {
        "symbol": profile.symbol, "variant": profile.variant, "bar_open": t, "bar_close": t + pd.Timedelta(minutes=tf_min),
        "last_1m_open": last_1m, "close": float(bars["close"].iloc[-1]), "bb_upper": float(out["bb_upper"].iloc[-1]),
        "bb_lower": float(out["bb_lower"].iloc[-1]), "ema50_4h": float(tr["ema50_4h"]), "ema200_4h": float(tr["ema200_4h"]),
        "src_4h_open": tr["src_4h_open"], "allow_long_4h": bool(tr["allow_long"]), "allow_short_4h": bool(tr["allow_short"]),
        "atr14": float(atr14.iloc[-1]), "atr_ratio": float(gate["raw_atr_ratio"].iloc[-1]),
        "gate_threshold": float(GATE_MULT * gate["rt_cost_frac"].iloc[-1]), "cost_gate_pass": bool(gate["allow"].iloc[-1]),
        "cond_long": bool(out["cond_long"].iloc[-1]), "cond_short": bool(out["cond_short"].iloc[-1]),
        "signal": s, "fresh_signal": s != 0, "signal_side": {1: "LONG", -1: "SHORT"}.get(s), "n_bars": len(bars),
        "signals_total": int((sig != 0).sum()),
    }


def evaluate_latest_signals(symbols: list[str] | None = None, sync: bool = True, *,
                            profiles: dict[str, StrategyProfile] = FROZEN_PROFILES) -> dict[str, dict[str, Any]]:
    symbols = symbols or list(profiles)
    if sync:
        sync_market_data(symbols)
    res = {}
    for sym in symbols:
        hist = load_history(sym)
        r = evaluate_symbol(profiles[sym], hist)
        r["staleness_min"] = (pd.Timestamp.now(tz="UTC") - r["bar_close"]).total_seconds() / 60
        r["history"] = hist
        res[sym] = r
    return res


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class ExecutionEngine:
    def __init__(self, store: StateStore, mode: str = "paper", client: DeltaRestClientAdapter | None = None,
                 products: dict[str, ProductSpec] | None = None, clock: Callable[[], int] = now_us,
                 profiles: dict[str, StrategyProfile] = FROZEN_PROFILES,
                 sleep: Callable[[float], None] = time.sleep, notifier: TelegramNotifier | None = None):
        if mode not in ("paper", "testnet", "live"):
            raise ValueError(mode)
        if mode != "paper":
            if client is None or client.mode != mode:
                raise ExchangeError(f"{mode} engine needs a DeltaRestClientAdapter in {mode} mode")
        self.store, self.mode, self.client, self.clock, self.profiles, self.sleep = store, mode, client, clock, profiles, sleep
        self.notifier = notifier if notifier is not None else TelegramNotifier.from_settings(mode)
        self.cfg_row = store.init_config(mode, profiles)
        self.rate = float(self.cfg_row["usd_inr_rate"])
        if mode == "paper":
            self.products = products or {s: ProductSpec.from_metadata(s) for s in profiles}
        elif products is not None:
            self.products = dict(products)
        else:
            # Product ids differ between testnet and production: always take them from the venue traded.
            self.products = {}
            for s in profiles:
                info = client.get_product(s)
                self.products[s] = ProductSpec(s, info.id, float(info.contract_value), float(info.tick_size))
        self._mm: dict[str, float] = {}
        self.account_policy: dict[str, Any] | None = None  # set by prepare_account() (F07); exchange bot entries need it
        self.tf_us = dm.TIMEFRAME_MINUTES[TIMEFRAME] * 60_000_000
        self.paper_slip = settings.SIZING_SLIPPAGE_BPS / 10_000
        self.fee = settings.TAKER_FEE * (1 + settings.GST_ON_FEES)
        # A DISPATCHING intent left by a crash may or may not have reached the exchange.
        for r in store.q("SELECT intent_id FROM order_intents WHERE mode=? AND state='DISPATCHING'", (mode,)):
            store.transition(r["intent_id"], "OUTCOME_UNKNOWN", "found DISPATCHING at startup")

    # -- helpers ---------------------------------------------------------------- #
    def ensure_product(self, symbol: str) -> ProductSpec:
        """Product spec for any symbol (manual trades on coins outside the bot profiles)."""
        if symbol not in self.products:
            if self.mode == "paper":
                if not dm.metadata_path(symbol).exists():
                    dm.fetch_and_save_metadata([symbol])
                self.products[symbol] = ProductSpec.from_metadata(symbol)
            else:
                info = self.client.get_product(symbol)
                self.products[symbol] = ProductSpec(symbol, info.id, float(info.contract_value), float(info.tick_size))
        return self.products[symbol]

    def maintenance_pct(self, symbol: str) -> float:
        if symbol not in self._mm:
            try:
                self._mm[symbol] = float(dm.load_metadata(symbol).get("maintenance_margin") or 0.5)
            except FileNotFoundError:
                self._mm[symbol] = 0.5
        return self._mm[symbol]

    def position_liq(self, p: Any) -> float:
        return liquidation_price(float(p["entry_price"]), p["side"], float(p["leverage"] or LEVERAGE), self.maintenance_pct(p["symbol"]))

    def _event(self, level: str, kind: str, symbol: str | None = None, detail: Any = None, notify: bool = False) -> int:
        eid = self.store.event(level, kind, symbol, detail, self.mode)
        getattr(log, "warning" if level in ("WARN", "CRITICAL") else "info")("%s %s %s %s", level, kind, symbol or "", detail or "")
        if level == "CRITICAL" or notify:
            payload = {"symbol": symbol, **detail} if isinstance(detail, dict) else {"symbol": symbol, "detail": detail}
            self.notifier.notify_safety_incident(f"INC-{eid:06d}", level, kind, payload)
        return eid

    def entries_paused(self) -> bool:
        return bool(self.store.config()["entries_paused"])

    def new_client_oid(self, symbol: str, role: str = "E") -> str:
        oid = f"s5{self.mode[0]}{role}{symbol[:3]}{np.base_repr(self.clock(), 36).lower()}{secrets.token_hex(3)}"
        assert len(oid) <= 32, oid
        return oid

    def halted(self) -> tuple[bool, str | None]:
        c = self.store.config()
        if not c["halted"]:
            return False, None
        if c["halt_until_us"] is not None and self.clock() >= c["halt_until_us"]:
            self.store.set_halt(False)
            self._event("INFO", "halt_expired", detail=c["halt_reason"])
            return False, None
        return True, c["halt_reason"]

    def symbol_blocked(self, symbol: str) -> str | None:
        r = self.store.one(f"SELECT intent_id, state FROM order_intents WHERE mode=? AND symbol=? AND state IN "
                           f"({','.join('?' * len(BLOCKING_STATES))}) LIMIT 1", (self.mode, symbol, *BLOCKING_STATES))
        return None if r is None else f"intent {r['intent_id']} is {r['state']}"

    def realized_equity_inr(self) -> float:
        """Capital + closed net PnL + open positions' (funding - entry fee); the paper wallet + margins."""
        cap = float(self.cfg_row["capital_inr"])
        closed = sum(float(t["net_pnl_inr"]) for t in self.store.trades(self.mode))
        opened = sum((float(p["realized_funding_usd"]) - float(p["entry_fee_usd"])) * self.rate for p in self.store.positions(self.mode))
        return cap + closed + opened

    def account_equity_inr(self) -> tuple[float, float]:
        """(equity, available) in INR used for sizing."""
        if self.mode == "paper":
            eq = self.realized_equity_inr()
            used = sum(float(p["margin_usd"]) for p in self.store.positions(self.mode)) * self.rate
            return eq, eq - used
        bals = self.client.get_wallet_balances()
        usd = next((b for b in bals if b.get("asset_symbol") == "USD"), None)
        if usd is None:
            raise ExchangeError("no USD wallet balance returned")
        avail = float(usd["available_balance"]) * self.rate
        return avail, avail

    # -- entries ---------------------------------------------------------------- #
    def on_signal(self, symbol: str, direction: int, signal_bar_us: int, atr_value: float, ref_price: float) -> dict[str, Any]:
        """Act on a fresh signal from the latest closed bar. Returns what happened (never raises on a skip)."""
        side_name = "LONG" if direction == 1 else "SHORT"
        h, why = self.halted()
        if h:
            return {"action": "skip", "reason": f"halted: {why}"}
        if self.entries_paused():
            self._event("INFO", "entry_paused", symbol, {"signal_bar": to_ts(signal_bar_us)})
            return {"action": "skip", "reason": "entries paused"}
        if (b := self.symbol_blocked(symbol)) is not None:
            self._event("WARN", "entry_blocked", symbol, b)
            return {"action": "skip", "reason": f"blocked: {b}"}
        if self.store.position(self.mode, symbol) is not None:
            return {"action": "skip", "reason": "already in position"}
        st = self.store.symbol_state(self.mode, symbol)
        if st is not None and st["last_signal_bar_us"] is not None and signal_bar_us <= st["last_signal_bar_us"]:
            return {"action": "skip", "reason": "signal bar already processed"}
        self.store.mark_signal_seen(self.mode, symbol, signal_bar_us)
        entry_us = signal_bar_us + self.tf_us
        if st is not None and st["cooldown_until_us"] is not None and entry_us < st["cooldown_until_us"]:
            return {"action": "skip", "reason": "cooldown"}
        if self.mode != "paper" and (why := self.account_policy_refusal(symbol)) is not None:
            self._event("WARN", "entry_blocked_policy", symbol, why)
            return {"action": "skip", "reason": f"account policy: {why}"}
        product = self.products[symbol]
        equity, available = self.account_equity_inr()
        fill_ref = ref_price * (1 + direction * self.paper_slip) if self.mode == "paper" else ref_price
        lots = calculate_order_lots(product, fill_ref, atr_value, side_name, equity, available)
        if lots.contracts < 1:
            self._event("INFO", "entry_skipped", symbol, lots.reason)
            return {"action": "skip", "reason": lots.reason, "sizing": lots}
        intent = self.store.create_intent(
            intent_id=f"in-{secrets.token_hex(8)}", client_order_id=self.new_client_oid(symbol), mode=self.mode,
            symbol=symbol, product_id=product.product_id, role="ENTRY", side="buy" if direction == 1 else "sell",
            size=lots.contracts, order_type="market_order", entry_price=fill_ref, sl_price=lots.sl_price,
            tp_price=lots.tp_price, trail_amount=lots.trail_amount, signal_bar_us=signal_bar_us, atr=atr_value)
        if self.mode == "paper":
            return self._paper_fill_entry(intent, ref_price, lots)
        return self._dispatch_entry(intent)

    def _paper_fill_entry(self, intent: sqlite3.Row, raw_price: float, lots: LotSizing) -> dict[str, Any]:
        sym, d = intent["symbol"], 1 if intent["side"] == "buy" else -1
        product = self.products[sym]
        px = raw_price * (1 + d * self.paper_slip)
        # Levels from the executed price (as the backtester does).
        cfg = sizing_config(lots.equity_inr)
        sl_pct, tp_pct = cfg.stop_pcts(px, lots.atr)
        sl, tp = Backtester(cfg).levels(px, d, sl_pct, tp_pct, product)
        qty = intent["size"] * product.contract_value
        t = self.clock()
        with self.store.tx():  # intent FILLED + position: one transaction
            self.store.transition(intent["intent_id"], "FILLED", "paper fill", filled_size=intent["size"], avg_fill_price=px,
                                  exchange_order_id=f"paper-{intent['intent_id']}", sl_price=sl, tp_price=tp,
                                  raw_response=json.dumps({"paper": True, "raw_price": raw_price, "fill_price": px}),
                                  filled_size_booked=intent["size"])
            self.store.insert_position(
                mode=self.mode, symbol=sym, product_id=product.product_id, side="LONG" if d == 1 else "SHORT",
                contracts=intent["size"], contract_value=product.contract_value, entry_price=px, raw_entry_price=raw_price,
                initial_sl=sl, sl_price=sl, tp_price=tp, trail_amount=lots.trail_amount, sl_order_id=None, tp_order_id=None,
                highest_fav_price=px, opened_at_us=t, last_bar_us=t - t % 60_000_000 - 60_000_000,
                realized_funding_usd=0.0, entry_fee_usd=qty * px * self.fee, margin_usd=qty * px / LEVERAGE,
                atr_at_signal=lots.atr, intent_id=intent["intent_id"])
        self._event("INFO", "paper_entry", sym, {"side": "LONG" if d == 1 else "SHORT", "contracts": intent["size"],
                                                  "price": px, "sl": sl, "tp": tp})
        self.notifier.notify_trade_entry(self.store.intent(intent["intent_id"]), self.store.position(self.mode, sym))
        return {"action": "filled", "intent_id": intent["intent_id"], "price": px, "contracts": intent["size"],
                "sl": sl, "tp": tp, "sizing": lots}

    def _dispatch_entry(self, intent: sqlite3.Row) -> dict[str, Any]:
        iid = intent["intent_id"]
        self.store.transition(iid, "DISPATCHING")  # durable BEFORE anything leaves the process
        try:
            order = self.client.place_order(
                product_id=intent["product_id"], side=intent["side"], size=intent["size"], order_type="market_order",
                client_order_id=intent["client_order_id"], bracket_stop_loss_price=intent["sl_price"],
                bracket_take_profit_price=intent["tp_price"], bracket_stop_trigger_method=TRIGGER_METHOD)
        except AmbiguousOutcomeError as exc:
            self.store.transition(iid, "OUTCOME_UNKNOWN", str(exc), error=str(exc))
            self._event("CRITICAL", "dispatch_outcome_unknown", intent["symbol"], {"client_order_id": intent["client_order_id"]})
            return {"action": "outcome_unknown", "intent_id": iid}
        except OrderPriceMismatchError as exc:
            self.store.transition(iid, "ACKNOWLEDGED", "price mismatch", exchange_order_id=str(exc.order.get("id")),
                                  raw_response=json.dumps(exc.order), error=str(exc))
            self.store.set_halt(True, f"price mismatch on {intent['client_order_id']}: {exc.mismatches}")
            self._event("CRITICAL", "price_mismatch", intent["symbol"], exc.mismatches)
            return {"action": "halted", "intent_id": iid, "reason": str(exc)}
        except (DeltaAPIError, RateLimitedError) as exc:
            self.store.transition(iid, "REJECTED", str(exc), error=str(exc))
            self._event("WARN", "entry_rejected", intent["symbol"], str(exc))
            return {"action": "rejected", "intent_id": iid, "reason": str(exc)}
        return self._apply_order(iid, order)

    # -- fill booking (Step 9: one path for responses, recovery, partial fills and resting orders) ---------------- #
    def _order_fills(self, it: Any, order_id: Any) -> list[dict[str, Any]]:
        """GET /v2/fills for one order (window: intent creation - 5 s .. now + 1 s)."""
        start, end = int(it["created_at_us"]) - 5_000_000, self.clock() + 1_000_000
        return [f for f in self.client.get_fills(it["product_id"], start, end) if str(f.get("order_id")) == str(order_id)]

    @staticmethod
    def _venue_fee(f: dict[str, Any]) -> float | None:
        """The exchange's own commission for a fill (paid_commission / commission), when it reports one."""
        for k in ("paid_commission", "commission"):
            if f.get(k) not in (None, ""):
                return float(f[k])
        return None

    def _apply_order(self, intent_id: str, order: dict[str, Any], fills: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Map an exchange order snapshot onto the intent and book every filled contract not booked yet, by role."""
        it = self.store.intent(intent_id)
        size = int(order.get("size") or it["size"])
        booked = int(it["filled_size_booked"] or 0)
        filled = size - int(order.get("unfilled_size") or 0)
        st = order.get("state")
        if fills is None and self.mode != "paper" and filled > booked:
            fills = self._order_fills(it, order.get("id"))
        fills = fills or []
        qty = sum(int(f["size"]) for f in fills)
        filled = max(filled, qty, booked)  # a lagging order view never un-books contracts
        avg = order.get("average_fill_price")
        if qty:
            avg = sum(float(f["price"]) * int(f["size"]) for f in fills) / qty
        common = dict(exchange_order_id=str(order.get("id")), raw_response=json.dumps(order), filled_size=filled,
                      avg_fill_price=float(avg) if avg not in (None, "") else None)
        if st in ("open", "pending"):
            new = "PARTIALLY_FILLED" if filled > 0 else "ACKNOWLEDGED"
        elif st in ("closed", "cancelled"):
            if filled == 0:
                self.store.transition(intent_id, "CANCELLED", order.get("cancellation_reason") or f"exchange state {st}", **common)
                return {"action": "cancelled", "intent_id": intent_id}
            new = "FILLED"
        else:
            raise ExchangeError(f"unexpected order state {st!r} for {intent_id}")
        if filled > booked:
            if common["avg_fill_price"] is None:
                raise ExchangeError(f"order {order.get('id')} filled without a fill price")
            return self._book_fill_delta(it, order, fills, new, common)
        self.store.transition(intent_id, new, f"exchange state {st}", **common)
        return {"action": new.lower(), "intent_id": intent_id, "contracts": 0}

    def _book_fill_delta(self, it: Any, order: dict[str, Any], fills: list[dict[str, Any]], new_state: str,
                         common: dict[str, Any]) -> dict[str, Any]:
        """Book filled_size - filled_size_booked contracts: priced from fill records that cover exactly that slice
        (venue commission when reported), else from the order's cumulative average (taker + GST estimate)."""
        iid, sym, oid = it["intent_id"], it["symbol"], str(order.get("id"))
        product = self.ensure_product(sym)
        filled, booked = int(common["filled_size"]), int(it["filled_size_booked"] or 0)
        delta = filled - booked
        known = self.store.q("SELECT * FROM booked_fills WHERE order_id=?", (oid,))
        known_ids = {r["fill_id"] for r in known}
        fresh = [f for f in fills if str(f.get("id")) not in known_ids]
        if fresh and sum(int(f["size"]) for f in fresh) == delta:
            rows = [{"fill_id": str(f["id"]), "size": int(f["size"]), "price": float(f["price"]), "fee_usd": self._venue_fee(f)}
                    for f in fresh]
        else:  # fill records lag the order view: price this slice from the cumulative average
            done_qty = sum(int(r["size"]) for r in known)
            done_val = sum(float(r["price"]) * int(r["size"]) for r in known)
            avg = float(common["avg_fill_price"])
            px = (avg * filled - done_val) / delta if done_qty and done_qty == booked else avg
            rows = [{"fill_id": f"ord-{oid}-cum{filled}", "size": delta, "price": px, "fee_usd": None}]
        for r in rows:
            if r["fee_usd"] is None:
                r["fee_usd"] = r["size"] * product.contract_value * r["price"] * self.fee
            r.update(intent_id=iid, order_id=oid, symbol=sym, side=it["side"], role=it["role"])
        px = sum(r["price"] * r["size"] for r in rows) / delta
        fee = sum(r["fee_usd"] for r in rows)
        upd = {**common, "filled_size_booked": filled}
        if it["role"] == "EXIT" or it["reduce_only"]:  # F03: an exit is never booked as an entry / scale-in
            return self._book_exit(it, rows, delta, px, fee, oid, new_state, upd)
        return self._book_entry(it, rows, delta, px, fee, new_state, upd)

    def _book_entry(self, it: Any, rows: list[dict[str, Any]], delta: int, px: float, fee: float, new_state: str,
                    upd: dict[str, Any]) -> dict[str, Any]:
        """Open the position or add to it by ``delta`` contracts, in one transaction with the intent and its fills."""
        iid, sym = it["intent_id"], it["symbol"]
        cv = self.ensure_product(sym).contract_value
        lev = float(it["leverage"] or LEVERAGE)
        side = "LONG" if it["side"] == "buy" else "SHORT"
        t = self.clock()
        with self.store.tx():
            p = self.store.position(self.mode, sym)
            if p is None:
                sl = None if it["sl_price"] is None else float(it["sl_price"])
                tp = None if it["tp_price"] is None else float(it["tp_price"])
                self.store.insert_position(
                    mode=self.mode, symbol=sym, product_id=it["product_id"], side=side, contracts=delta, contract_value=cv,
                    entry_price=px, raw_entry_price=px, initial_sl=sl, sl_price=sl, tp_price=tp,
                    trail_amount=float(it["trail_amount"] or 0), sl_order_id=None, tp_order_id=None, highest_fav_price=px,
                    opened_at_us=t, last_bar_us=t - t % 60_000_000 - 60_000_000, realized_funding_usd=0.0, entry_fee_usd=fee,
                    margin_usd=delta * cv * px / lev, atr_at_signal=float(it["atr"] or 0), intent_id=iid, leverage=lev,
                    source=it["source"] or "bot")
                opened, n = True, delta
            elif p["side"] == side:
                n0 = int(p["contracts"])
                n = n0 + delta
                avg = (n0 * float(p["entry_price"]) + delta * px) / n
                raw = (n0 * float(p["raw_entry_price"]) + delta * px) / n
                self.store.update_position(self.mode, sym, contracts=n, entry_price=avg, raw_entry_price=raw,
                                           entry_fee_usd=float(p["entry_fee_usd"]) + fee,
                                           margin_usd=n * cv * avg / float(p["leverage"] or lev))
                opened = False
            else:
                raise ExchangeError(f"{sym}: {side} entry fill against the open {p['side']} position; not booked")
            self.store.record_fills(rows)
            self.store.transition(iid, new_state, f"booked {delta} contract(s) @ {px:g}", **upd)
        legs = self._protect(sym, iid, opened=opened)
        pos = self.store.position(self.mode, sym)
        self._event("INFO", "entry_filled" if opened else "entry_added", sym,
                    {"intent_id": iid, "price": px, "contracts": delta, "position": n, "fee_usd": fee, "state": new_state, **legs})
        self.notifier.notify_trade_entry(self.store.intent(iid), pos)
        action = "partially_filled" if new_state == "PARTIALLY_FILLED" else ("filled" if opened else "added")
        return {"action": action, "intent_id": iid, "price": px, "contracts": delta, "position_size": n,
                "avg_entry": float(pos["entry_price"]) if pos is not None else px, "state": new_state, **legs}

    def _book_exit(self, it: Any, rows: list[dict[str, Any]], delta: int, px: float, fee: float, oid: str, new_state: str,
                   upd: dict[str, Any]) -> dict[str, Any]:
        """Book a reduce-only fill as a closed trade (fees, realized PnL) and shrink / remove the position."""
        iid, sym = it["intent_id"], it["symbol"]
        reason = it["exit_reason"] or "MANUAL_CLOSE"
        t = self.clock()
        row, tid, n_all, q = None, None, 0, 0
        with self.store.tx():
            p = self.store.position(self.mode, sym)
            if p is not None:
                n_all = int(p["contracts"])
                q = min(delta, n_all)
                if q < n_all:
                    f = q / n_all
                    part = {**dict(p), "contracts": q, "entry_fee_usd": p["entry_fee_usd"] * f,
                            "realized_funding_usd": p["realized_funding_usd"] * f}
                    row = self._trade_row(_Row(part), px, px, reason, t, fee, oid)
                    tid = self.store.reduce_position(self.mode, sym, row, {
                        "contracts": n_all - q, "entry_fee_usd": p["entry_fee_usd"] * (1 - f),
                        "realized_funding_usd": p["realized_funding_usd"] * (1 - f), "margin_usd": p["margin_usd"] * (1 - f)})
                else:
                    row = self._trade_row(p, px, px, reason, t, fee, oid)
                    tid = self.store.close_position(self.mode, sym, row, self._cooldown_until(t))
            self.store.record_fills(rows)
            self.store.transition(iid, new_state, reason if p is not None else "exit fill without a journal position", **upd)
        if row is None:
            self._event("CRITICAL", "exit_fill_without_position", sym, {"intent_id": iid, "contracts": delta, "price": px})
            return {"action": "exit_unmatched", "intent_id": iid, "contracts": delta}
        legs = self._resize_legs(sym) if q < n_all else self._cancel_legs(int(p["product_id"]))
        action = "partial_exit" if q < n_all else "exit"
        self._event("INFO", f"exchange_{action}", sym, {"reason": reason, "contracts": q, "price": px,
                                                         "net_inr": row["net_pnl_inr"], "intent_id": iid})
        self.notifier.notify_trade_exit({**row, "trade_id": tid})
        return {"action": action, "intent_id": iid, "trade_id": tid, "contracts": q, "remaining": n_all - q,
                "exit_order_id": oid, "legs": legs, **row}

    # -- protective legs ------------------------------------------------------------------- #
    @staticmethod
    def _match_legs(orders: list[dict[str, Any]], product_id: int, pos_side: str,
                    ref_price: float | None = None) -> tuple[dict | None, dict | None]:
        """Newest SL / TP child orders that can protect a ``pos_side`` position: same product, opposite side,
        reduce-only, open/pending, stop on the correct side of ``ref_price`` (if given). Size is checked by callers."""
        exit_side = "sell" if pos_side == "LONG" else "buy"
        d = 1 if pos_side == "LONG" else -1

        def ok(o: dict[str, Any], kind: str) -> bool:
            if o.get("stop_order_type") != kind or o.get("side") != exit_side or not o.get("reduce_only"):
                return False
            if int(o.get("product_id") or product_id) != int(product_id) or o.get("state") not in (None, "open", "pending"):
                return False
            if ref_price is not None and o.get("stop_price") not in (None, ""):
                sp = float(o["stop_price"])
                return (sp - ref_price) * d < 0 if kind == "stop_loss_order" else (sp - ref_price) * d > 0
            return True
        sl = [o for o in orders if ok(o, "stop_loss_order")]
        tp = [o for o in orders if ok(o, "take_profit_order")]
        key = lambda o: int(o["id"])  # noqa: E731
        return (max(sl, key=key) if sl else None), (max(tp, key=key) if tp else None)

    def _protect(self, symbol: str, intent_id: str | None = None, opened: bool = False) -> dict[str, Any]:
        """Bind the position's SL/TP child orders (attach a position bracket if one is missing; never resend the
        entry) and size them to the filled contracts. Missing protection after the readback halts new entries."""
        p = self.store.position(self.mode, symbol)
        if p is None or self.mode == "paper":
            return {}
        want_sl, want_tp = p["sl_price"] is not None, p["tp_price"] is not None
        if not (want_sl or want_tp):
            return {"sl_leg_order_id": None, "tp_leg_order_id": None, "legs_resized": []}
        pid, n = int(p["product_id"]), int(p["contracts"])
        ref = float(p["entry_price"]) if opened else None  # an edited / trailed stop may sit beyond the average entry

        def legs() -> tuple[dict | None, dict | None]:
            return self._match_legs(self.client.get_active_orders(pid, states="open,pending"), pid, p["side"], ref)
        sl_o, tp_o = legs()
        if (want_sl and sl_o is None) or (want_tp and tp_o is None):
            self._event("WARN", "bracket_legs_missing", symbol, {"sl": bool(sl_o), "tp": bool(tp_o)})
            try:
                self.client.place_position_bracket(
                    pid, {"order_type": "market_order", "stop_price": p["sl_price"]} if want_sl and sl_o is None else None,
                    {"order_type": "market_order", "stop_price": p["tp_price"]} if want_tp and tp_o is None else None,
                    TRIGGER_METHOD)
            except ExchangeError as exc:
                self._event("CRITICAL", "bracket_attach_failed", symbol, str(exc))
            sl_o, tp_o = legs()
        if (want_sl and sl_o is None) or (want_tp and tp_o is None):
            self.store.set_halt(True, f"{symbol}: filled position without protective legs")
            self._event("CRITICAL", "unprotected_position", symbol, intent_id)
        upd: dict[str, Any] = {"sl_order_id": str(sl_o["id"]) if sl_o else p["sl_order_id"],
                               "tp_order_id": str(tp_o["id"]) if tp_o else p["tp_order_id"]}
        if sl_o is not None and sl_o.get("stop_price") not in (None, ""):
            upd["sl_price"] = float(sl_o["stop_price"])
            if opened:
                upd["initial_sl"] = upd["sl_price"]
        if tp_o is not None and tp_o.get("stop_price") not in (None, ""):
            upd["tp_price"] = float(tp_o["stop_price"])
        self.store.update_position(self.mode, symbol, **upd)
        if intent_id is not None:
            self.store.update_intent(intent_id, sl_leg_order_id=upd["sl_order_id"], tp_leg_order_id=upd["tp_order_id"])
        stale = {str(o["id"]) for o in (sl_o, tp_o) if o is not None and int(o.get("size") or 0) != n}
        return {"sl_leg_order_id": upd["sl_order_id"], "tp_leg_order_id": upd["tp_order_id"],
                "legs_resized": self._resize_legs(symbol, only=stale) if stale else []}

    # -- ambiguous-outcome recovery ----------------------------------------------- #
    def _known_order_ids(self) -> set[str]:
        """Exchange order ids this journal already accounts for: intent orders, SL/TP legs, booked fills."""
        ids: set[str] = set()
        for r in self.store.q("SELECT exchange_order_id, sl_leg_order_id, tp_leg_order_id FROM order_intents WHERE mode=?", (self.mode,)):
            ids |= {str(x) for x in r if x}
        for r in self.store.q("SELECT sl_order_id, tp_order_id FROM open_positions WHERE mode=?", (self.mode,)):
            ids |= {str(x) for x in r if x}
        ids |= {r[0] for r in self.store.q("SELECT DISTINCT order_id FROM booked_fills WHERE order_id IS NOT NULL")}
        return ids

    def _orphan_fills_for(self, it: Any, fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fills of one order nothing in the journal owns, on the intent's side, fitting its size (F08 evidence).
        A fill carrying this client_order_id wins; otherwise exactly one candidate order is required."""
        known, booked, coid = self._known_order_ids(), self.store.booked_fill_ids(), it["client_order_id"]
        by_order: dict[str, list[dict[str, Any]]] = {}
        for f in fills:
            if (str(f.get("order_id")) not in known and str(f.get("id")) not in booked and f.get("side") == it["side"]
                    and f.get("client_order_id") in (None, "", coid)):
                by_order.setdefault(str(f.get("order_id")), []).append(f)
        fits = [fs for fs in by_order.values() if sum(int(f["size"]) for f in fs) <= int(it["size"])]
        exact = [fs for fs in fits if any(f.get("client_order_id") == coid for f in fs)]
        return exact[0] if exact else (fits[0] if len(fits) == 1 else [])

    @staticmethod
    def _order_from_fills(it: Any, order_id: str, fills: list[dict[str, Any]]) -> dict[str, Any]:
        qty = sum(int(f["size"]) for f in fills)
        vwap = sum(float(f["price"]) * int(f["size"]) for f in fills) / qty
        market = it["order_type"] == "market_order"
        return {"id": order_id, "size": int(it["size"]), "unfilled_size": max(int(it["size"]) - qty, 0),
                "client_order_id": it["client_order_id"], "state": "closed" if market or qty >= int(it["size"]) else "open",
                "average_fill_price": str(vwap), "synthesized_from_fills": True}

    def _exchange_vs_journal(self, symbol: str, product_id: int) -> tuple[int, int]:
        """Signed contracts: Delta's position (GET /v2/positions/margined) vs the journal's."""
        ex = sum(int(x.get("size") or 0) for x in self.client.get_margined_positions([product_id])
                 if int(x.get("product_id") or 0) == int(product_id))
        p = self.store.position(self.mode, symbol)
        return ex, 0 if p is None else (1 if p["side"] == "LONG" else -1) * int(p["contracts"])

    def reconcile_unknown_intents(self) -> list[dict[str, Any]]:
        """Resolve OUTCOME_UNKNOWN intents from exchange evidence only; never resends anything. Order lookups
        (client_order_id, active, history), then fills and the real position: negative order lookups alone never
        clear the blocker (F08). Finally books fills a crash left unbooked (filled_size > filled_size_booked)."""
        out = []
        for it in self.store.q("SELECT * FROM order_intents WHERE mode=? AND state='OUTCOME_UNKNOWN' ORDER BY created_at_us",
                               (self.mode,)):
            iid, coid, pid = it["intent_id"], it["client_order_id"], it["product_id"]
            if self.mode == "paper":
                self.store.transition(iid, "REJECTED", "paper intent never dispatched")
                continue
            steps = []
            order = self.client.get_order_by_client_oid(coid)                      # 1) by client_order_id
            steps.append(f"client_oid:{'found' if order else 'none'}")
            if order is None:                                                     # 2) active orders
                order = next((o for o in self.client.get_active_orders(pid) if o.get("client_order_id") == coid), None)
                steps.append(f"active:{'found' if order else 'none'}")
            start, end = int(it["created_at_us"]) - 5_000_000, self.clock() + 1_000_000
            if order is None:                                                     # 3) history
                order = next((o for o in self.client.get_order_history(pid, start, end) if o.get("client_order_id") == coid), None)
                steps.append(f"history:{'found' if order else 'none'}")
            all_fills = self.client.get_fills(pid, start, end)                    # 4) fills
            if order is None and (mine := self._orphan_fills_for(it, all_fills)):
                oid = str(mine[0]["order_id"])
                steps.append(f"fills:{len(mine)} for unowned order {oid}")
                try:
                    order = self.client.get_order_by_id(oid)
                except ExchangeError:
                    order = None
                if not order or order.get("client_order_id") not in (None, "", coid):
                    order = self._order_from_fills(it, oid, mine)
            if order is None:
                if self.clock() - int(it["updated_at_us"]) < UNKNOWN_GRACE_US:
                    out.append({"intent_id": iid, "symbol": it["symbol"], "result": "STILL_UNKNOWN", "steps": steps})
                    continue
                ex, j = self._exchange_vs_journal(it["symbol"], pid)                # 5) real position
                steps.append(f"position:exchange {ex} journal {j}")
                if ex != j:  # something moved the position: keep the symbol blocked, never call it REJECTED
                    if not str(it["error"] or "").startswith("unresolved_position_delta"):
                        self.store.update_intent(iid, error=f"unresolved_position_delta: exchange {ex} != journal {j}")
                        self._event("CRITICAL", "unresolved_position_delta", it["symbol"],
                                    {"intent_id": iid, "exchange": ex, "journal": j, "steps": steps})
                    out.append({"intent_id": iid, "symbol": it["symbol"], "result": "UNRESOLVED_POSITION_DELTA", "steps": steps})
                    continue
                self.store.transition(iid, "REJECTED", "not found at exchange after grace period; " + ",".join(steps))
                out.append({"intent_id": iid, "symbol": it["symbol"], "result": "REJECTED", "steps": steps})
                continue
            fills = [f for f in all_fills if str(f.get("order_id")) == str(order.get("id"))]
            steps.append(f"fills:{len(fills)}")
            res = self._apply_order(iid, order, fills)
            self._event("WARN", "intent_reconciled", it["symbol"], {"intent_id": iid, "steps": steps, "result": res["action"]})
            out.append({**res, "intent_id": iid, "symbol": it["symbol"], "result": self.store.intent(iid)["state"], "steps": steps})
        return out + self.book_unbooked_fills()

    def book_unbooked_fills(self) -> list[dict[str, Any]]:
        """Crash recovery: FILLED / PARTIALLY_FILLED intents whose fills were not (fully) booked."""
        if self.mode == "paper":
            return []
        out = []
        for it in self.store.q("SELECT * FROM order_intents WHERE mode=? AND state IN ('FILLED','PARTIALLY_FILLED') "
                               "AND filled_size > filled_size_booked AND exchange_order_id IS NOT NULL", (self.mode,)):
            try:
                order = self.client.get_order_by_id(it["exchange_order_id"])
            except ExchangeError:
                order = None
            if not order:
                order = {"id": it["exchange_order_id"], "size": it["size"], "unfilled_size": it["size"] - it["filled_size"],
                         "state": "closed" if it["state"] == "FILLED" else "open", "average_fill_price": it["avg_fill_price"]}
            res = self._apply_order(it["intent_id"], order)
            self._event("WARN", "fill_booking_recovered", it["symbol"], {"intent_id": it["intent_id"], "result": res.get("action")})
            out.append({**res, "intent_id": it["intent_id"], "symbol": it["symbol"], "result": "BOOKED_RECOVERY"})
        return out

    # -- position management ---------------------------------------------------- #
    def update_stop(self, symbol: str, new_sl: float) -> dict[str, Any]:
        p = self.store.position(self.mode, symbol)
        if p is None:
            raise ExchangeError(f"no open {symbol} position")
        if self.mode == "paper":
            self.store.update_position(self.mode, symbol, sl_price=new_sl)
            return {"sl_price": new_sl}
        try:
            o = self.client.edit_order(order_id=p["sl_order_id"], product_id=p["product_id"], size=p["contracts"],
                                       stop_price=new_sl)
            confirmed = float(o.get("stop_price") or new_sl)
        except DeltaAPIError as exc:  # e.g. the leg already triggered/filled: keep the journal, let sync detect the exit
            self._event("WARN", "sl_edit_rejected", symbol, {"wanted": new_sl, "error": str(exc)})
            return {"sl_price": float(p["sl_price"]), "error": str(exc)}
        except AmbiguousOutcomeError:
            o = self.client.get_order_by_id(p["sl_order_id"])  # read back; never resend the edit blindly
            confirmed = float(o["stop_price"])
            self._event("WARN", "sl_edit_outcome_unknown", symbol, {"exchange_stop": confirmed, "wanted": new_sl})
        self.store.update_position(self.mode, symbol, sl_price=confirmed)
        return {"sl_price": confirmed, "order": o}

    def on_bar(self, symbol: str, bar_us: int, o: float, h: float, l: float, c: float,
               funding_rate: float | None = None, mark_close: float | None = None,
               mark: float | None = None) -> dict[str, Any] | None:
        """Process one CLOSED 1m trade candle for an open position (backtester semantics):
        funding at 8h boundaries, SL-first exits (paper only), then trail from the NEXT candle on.
        ``funding_rate`` / ``mark_close`` are the settlement inputs: the values of the minute BEFORE bar_us."""
        p = self.store.position(self.mode, symbol)
        if p is None or bar_us <= p["last_bar_us"]:
            return None
        d = 1 if p["side"] == "LONG" else -1
        product = self.ensure_product(symbol)
        t_s = bar_us // 1_000_000
        have_funding = funding_rate is not None and mark_close is not None and not (math.isnan(funding_rate) or math.isnan(mark_close))
        if t_s % dm.FUNDING_INTERVAL_SECONDS == 0 and bar_us > p["opened_at_us"] and have_funding:
            cash = -d * p["contracts"] * p["contract_value"] * mark_close * funding_rate
            with self.store.tx() as cx:
                cx.execute("UPDATE open_positions SET realized_funding_usd = realized_funding_usd + ? WHERE mode=? AND symbol=?",
                           (cash, self.mode, symbol))
                cx.execute("INSERT INTO funding_events (mode, symbol, at_us, rate, mark_price, contracts, cashflow_usd) "
                           "VALUES (?,?,?,?,?,?,?)", (self.mode, symbol, bar_us, funding_rate, mark_close, p["contracts"], cash))
            p = self.store.position(self.mode, symbol)
        sl = None if p["sl_price"] is None else float(p["sl_price"])
        tp = None if p["tp_price"] is None else float(p["tp_price"])
        if self.mode == "paper":
            liq = self.position_liq(p)
            gap_liq = o <= liq if d == 1 else o >= liq
            probe = mark if mark is not None else (l if d == 1 else h)
            liq_hit = probe <= liq if d == 1 else probe >= liq
            sl_first = sl is not None and (sl > liq if d == 1 else sl < liq)
            if gap_liq or (liq_hit and not sl_first):
                return self._liquidate(symbol, bar_us)
            sl_hit = sl is not None and (l <= sl if d == 1 else h >= sl)
            tp_hit = tp is not None and (h >= tp if d == 1 else l <= tp)
            if sl_hit:
                gapped = o <= sl if d == 1 else o >= sl
                raw = (min(o, sl) if d == 1 else max(o, sl)) if gapped else sl
                reason = "TSL" if p["initial_sl"] is None or sl != float(p["initial_sl"]) else "SL"
                return self._paper_close(symbol, raw, reason, bar_us)
            if tp_hit:
                return self._paper_close(symbol, tp, "TP", bar_us)
        ext = max(float(p["highest_fav_price"]), h) if d == 1 else min(float(p["highest_fav_price"]), l)
        trail = float(p["trail_amount"] or 0)
        better, level = False, None
        if trail > 0:
            level = product.round_price(ext - d * trail)
            better = sl is None or (level > sl if d == 1 else level < sl)
        self.store.update_position(self.mode, symbol, highest_fav_price=ext, last_bar_us=bar_us)
        if better:
            r = self.update_stop(symbol, level)
            return {"action": "trail", "old_sl": sl, "new_sl": r["sl_price"], "extreme": ext}
        return None

    def _trade_row(self, p: Any, exit_price: float, raw_exit: float, reason: str, closed_us: int,
                   exit_fee_usd: float, exit_order_id: str | None) -> dict[str, Any]:
        d = 1 if p["side"] == "LONG" else -1
        qty = p["contracts"] * p["contract_value"]
        gross = d * qty * (exit_price - p["entry_price"])
        fees = float(p["entry_fee_usd"]) + exit_fee_usd
        funding = float(p["realized_funding_usd"])
        slip = qty * (abs(p["entry_price"] - p["raw_entry_price"]) + abs(raw_exit - exit_price))
        net = gross - fees + funding
        eq_after = self.realized_equity_inr() + (net - (funding - float(p["entry_fee_usd"]))) * self.rate
        return dict(mode=self.mode, symbol=p["symbol"], side=p["side"], contracts=p["contracts"],
                    contract_value=p["contract_value"], entry_price=p["entry_price"], exit_price=exit_price,
                    raw_entry_price=p["raw_entry_price"], raw_exit_price=raw_exit, initial_sl=p["initial_sl"],
                    final_sl=p["sl_price"], tp_price=p["tp_price"], exit_reason=reason, opened_at_us=p["opened_at_us"],
                    closed_at_us=closed_us, gross_pnl_usd=gross, fees_gst_usd=fees, funding_usd=funding,
                    slippage_usd=slip, net_pnl_usd=net, usd_inr_rate=self.rate, gross_pnl_inr=gross * self.rate,
                    fees_gst_inr=fees * self.rate, funding_inr=funding * self.rate, net_pnl_inr=net * self.rate,
                    equity_after_inr=eq_after, entry_intent_id=p["intent_id"], exit_order_id=exit_order_id,
                    source=p["source"] if "source" in p.keys() else "bot",
                    leverage=p["leverage"] if "leverage" in p.keys() else LEVERAGE)

    def _cooldown_until(self, closed_bar_us: int) -> int:
        return (closed_bar_us // self.tf_us + 1 + COOLDOWN_BARS) * self.tf_us

    def _paper_close(self, symbol: str, raw_price: float, reason: str, bar_us: int, contracts: int | None = None,
                     slippage: bool = True) -> dict[str, Any]:
        """Close all (or ``contracts`` of) a paper position at ``raw_price`` (adverse slippage unless disabled)."""
        p = self.store.position(self.mode, symbol)
        d = 1 if p["side"] == "LONG" else -1
        px = raw_price * (1 - d * self.paper_slip) if slippage else raw_price
        n_all = int(p["contracts"])
        n = n_all if contracts is None else int(contracts)
        if not 1 <= n <= n_all:
            raise ValueError(f"cannot close {n} of {n_all} contracts")
        if n < n_all:
            f = n / n_all
            part = {**dict(p), "contracts": n, "entry_fee_usd": p["entry_fee_usd"] * f,
                    "realized_funding_usd": p["realized_funding_usd"] * f}
            qty = n * p["contract_value"]
            row = self._trade_row(_Row(part), px, raw_price, reason, bar_us, qty * px * self.fee, None)
            tid = self.store.reduce_position(self.mode, symbol, row, {
                "contracts": n_all - n, "entry_fee_usd": p["entry_fee_usd"] * (1 - f),
                "realized_funding_usd": p["realized_funding_usd"] * (1 - f), "margin_usd": p["margin_usd"] * (1 - f)})
            self._event("INFO", "paper_partial_exit", symbol, {"reason": reason, "contracts": n, "left": n_all - n, "price": px})
            self.notifier.notify_trade_exit({**row, "trade_id": tid})
            return {"action": "partial_exit", "trade_id": tid, "reason": reason, "price": px, "remaining": n_all - n, **row}
        qty = p["contracts"] * p["contract_value"]
        row = self._trade_row(p, px, raw_price, reason, bar_us, qty * px * self.fee, None)
        tid = self.store.close_position(self.mode, symbol, row, self._cooldown_until(bar_us))
        self._event("INFO", "paper_exit", symbol, {"reason": reason, "price": px, "net_inr": row["net_pnl_inr"]})
        self.notifier.notify_trade_exit({**row, "trade_id": tid})
        return {"action": "exit", "trade_id": tid, "reason": reason, "price": px, **row}

    def _liquidate(self, symbol: str, bar_us: int) -> dict[str, Any]:
        """Paper isolated-margin liquidation: exit at the bankruptcy price (the position margin is lost)."""
        p = self.store.position(self.mode, symbol)
        bk = bankruptcy_price(float(p["entry_price"]), p["side"], float(p["leverage"] or LEVERAGE))
        self._event("CRITICAL", "liquidation", symbol, {"side": p["side"], "leverage": p["leverage"], "entry": p["entry_price"],
                                                        "liq_est": self.position_liq(p), "exit": bk})
        return self._paper_close(symbol, bk, "LIQUIDATION", bar_us, slippage=False)

    def on_tick(self, symbol: str, t_us: int, last: float, mark: float | None = None,
                funding_rate: float | None = None) -> dict[str, Any] | None:
        """Paper tick watcher (bot stopped): funding at an 8h boundary crossed since the last tick, liquidation on
        mark, SL/TP/trailing on the last traded price (a tick is treated as a one-price bar)."""
        p = self.store.position(self.mode, symbol)
        if p is None or t_us <= p["last_bar_us"]:
            return None
        b_us = (t_us // 1_000_000 // dm.FUNDING_INTERVAL_SECONDS) * dm.FUNDING_INTERVAL_SECONDS * 1_000_000
        if b_us > p["last_bar_us"] and b_us > p["opened_at_us"] and funding_rate is not None and mark:
            d = 1 if p["side"] == "LONG" else -1
            cash = -d * p["contracts"] * p["contract_value"] * mark * funding_rate
            with self.store.tx() as cx:
                cx.execute("UPDATE open_positions SET realized_funding_usd = realized_funding_usd + ? WHERE mode=? AND symbol=?",
                           (cash, self.mode, symbol))
                cx.execute("INSERT INTO funding_events (mode, symbol, at_us, rate, mark_price, contracts, cashflow_usd) "
                           "VALUES (?,?,?,?,?,?,?)", (self.mode, symbol, b_us, funding_rate, mark, p["contracts"], cash))
        return self.on_bar(symbol, t_us, last, last, last, last, None, None, mark=mark)

    # -- manual trading (Step 8) ----------------------------------------------------------- #
    def manual_fill(self, symbol: str, direction: int, contracts: int, raw_price: float, leverage: float, *,
                    sl: float | None = None, tp: float | None = None, trail: float | None = None,
                    maker: bool = False, intent_id: str | None = None) -> dict[str, Any]:
        """Paper manual execution: open, add (same side, same leverage), reduce, close or flip."""
        if self.mode != "paper":
            raise ExchangeError("manual_fill is the paper executor; use manual_open_exchange in testnet/live")
        product = self.ensure_product(symbol)
        t = self.clock()
        slip = 0.0 if maker else self.paper_slip
        fee_rate = (settings.MAKER_FEE if maker else settings.TAKER_FEE) * (1 + settings.GST_ON_FEES)
        p = self.store.position(self.mode, symbol)
        side = "LONG" if direction == 1 else "SHORT"
        if p is not None and p["side"] != side:  # opposite side: reduce / close / flip
            n_pos = int(p["contracts"])
            r = self._paper_close(symbol, raw_price, "MANUAL_CLOSE", t, contracts=min(contracts, n_pos), slippage=not maker)
            if contracts <= n_pos:
                return {"action": "reduced" if contracts < n_pos else "closed", "close": r}
            o = self.manual_fill(symbol, direction, contracts - n_pos, raw_price, leverage, sl=sl, tp=tp, trail=trail,
                                 maker=maker, intent_id=intent_id)
            return {"action": "flipped", "close": r, "open": o}
        px = raw_price * (1 + direction * slip)
        qty = contracts * product.contract_value
        margin, fee = qty * px / leverage, qty * px * fee_rate
        _, available = self.account_equity_inr()
        if (margin + fee) * self.rate > available + 1e-9:
            raise ValueError(f"insufficient margin: need ₹{(margin + fee) * self.rate:,.2f}, available ₹{available:,.2f}")
        iid = intent_id or f"mn-{secrets.token_hex(8)}"
        if p is None:
            self.store.insert_position(
                mode=self.mode, symbol=symbol, product_id=product.product_id, side=side, contracts=contracts,
                contract_value=product.contract_value, entry_price=px, raw_entry_price=raw_price, initial_sl=sl, sl_price=sl,
                tp_price=tp, trail_amount=float(trail or 0), sl_order_id=None, tp_order_id=None, highest_fav_price=px,
                opened_at_us=t, last_bar_us=t - t % 60_000_000 - 60_000_000, realized_funding_usd=0.0, entry_fee_usd=fee,
                margin_usd=margin, atr_at_signal=0.0, intent_id=iid, leverage=float(leverage), source="manual")
            action = "opened"
        else:
            if abs(float(p["leverage"]) - float(leverage)) > 1e-9:
                raise ValueError(f"{symbol} position is {float(p['leverage']):g}x; add at the same leverage or close it first")
            n0 = int(p["contracts"])
            avg = (n0 * p["entry_price"] + contracts * px) / (n0 + contracts)
            raw_avg = (n0 * p["raw_entry_price"] + contracts * raw_price) / (n0 + contracts)
            upd = {"contracts": n0 + contracts, "entry_price": avg, "raw_entry_price": raw_avg,
                   "entry_fee_usd": p["entry_fee_usd"] + fee, "margin_usd": p["margin_usd"] + margin}
            for k, v in (("sl_price", sl), ("tp_price", tp), ("trail_amount", trail)):
                if v is not None:
                    upd[k] = v
            if sl is not None:
                upd["initial_sl"] = sl
            self.store.update_position(self.mode, symbol, **upd)
            action = "added"
        pos = self.store.position(self.mode, symbol)
        self._event("INFO", f"manual_{action}", symbol, {"side": side, "contracts": contracts, "price": px, "leverage": leverage,
                                                         "maker": maker})
        self.notifier.notify_trade_entry({"intent_id": iid}, pos)
        return {"action": action, "symbol": symbol, "side": side, "contracts": contracts, "price": px, "fee_usd": fee,
                "margin_usd": margin, "position": dict(pos)}

    def set_protection(self, symbol: str, **fields: float | None) -> dict[str, Any]:
        """Change sl_price / tp_price / trail_amount (None removes SL/TP; trail 0 disables trailing)."""
        p = self.store.position(self.mode, symbol)
        if p is None:
            raise ExchangeError(f"no open {symbol} position")
        upd = {k: v for k, v in fields.items() if k in ("sl_price", "tp_price", "trail_amount")}
        if self.mode != "paper":
            self.ensure_product(symbol)
        if "trail_amount" in upd:
            upd["trail_amount"] = float(upd["trail_amount"] or 0)
        if "sl_price" in upd:
            upd["initial_sl"] = upd["sl_price"]  # a manual SL edit is a new initial stop (exit reason SL, not TSL)
        if self.mode != "paper":
            pid, n = p["product_id"], p["contracts"]
            for key, leg, kind in (("sl_price", "sl_order_id", "stop_loss_order"), ("tp_price", "tp_order_id", "take_profit_order")):
                if key not in upd:
                    continue
                if upd[key] is None:  # remove protection: cancel the live leg
                    if p[leg]:
                        try:
                            self.client.cancel_order(pid, order_id=p[leg])
                        except DeltaAPIError as exc:
                            self._event("WARN", "leg_cancel_failed", symbol, str(exc))
                    upd[leg] = None
                    continue
                if p[leg]:
                    self.client.edit_order(order_id=p[leg], product_id=pid, size=n, stop_price=upd[key])
                else:
                    self.client.place_position_bracket(pid, {"order_type": "market_order", "stop_price": upd[key]} if kind == "stop_loss_order" else None,
                                                       {"order_type": "market_order", "stop_price": upd[key]} if kind == "take_profit_order" else None,
                                                       TRIGGER_METHOD)
                    sl_o, tp_o = self._match_legs(self.client.get_active_orders(pid, states="open,pending"), pid, p["side"])
                    upd["sl_order_id"] = str(sl_o["id"]) if sl_o else p["sl_order_id"]
                    upd["tp_order_id"] = str(tp_o["id"]) if tp_o else p["tp_order_id"]
        self.store.update_position(self.mode, symbol, **upd)
        self._event("INFO", "protection_updated", symbol, upd)
        return dict(self.store.position(self.mode, symbol))

    def pending_limits(self, symbol: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM order_intents WHERE mode=? AND state='ACKNOWLEDGED' AND order_type='limit_order' AND source='manual'"
        args: tuple = (self.mode,)
        if symbol:
            sql += " AND symbol=?"
            args += (symbol,)
        return self.store.q(sql + " ORDER BY created_at_us", args)

    def check_pending_limits(self, symbol: str, t_us: int, low: float, high: float) -> list[dict[str, Any]]:
        """Fill resting paper limit orders whose price was touched (maker fee, fill at the limit)."""
        out = []
        for it in self.pending_limits(symbol):
            d = 1 if it["side"] == "buy" else -1
            lim = float(it["entry_price"])
            if (d == 1 and low <= lim) or (d == -1 and high >= lim):
                try:
                    with self.store.tx():  # position + intent FILLED: one transaction
                        r = self.manual_fill(symbol, d, int(it["size"]), lim, float(it["leverage"] or LEVERAGE), sl=it["sl_price"],
                                             tp=it["tp_price"], trail=it["trail_amount"], maker=True, intent_id=it["intent_id"])
                        self.store.transition(it["intent_id"], "FILLED", "paper limit touched", filled_size=it["size"],
                                              avg_fill_price=lim, filled_size_booked=it["size"])
                    out.append({"intent_id": it["intent_id"], **r})
                except (ValueError, ExchangeError) as exc:
                    self.store.transition(it["intent_id"], "CANCELLED", f"fill refused: {exc}", error=str(exc))
                    out.append({"intent_id": it["intent_id"], "action": "cancelled", "reason": str(exc)})
        return out

    def manual_open_exchange(self, symbol: str, direction: int, contracts: int, leverage: float, ref_price: float, *,
                             sl: float | None = None, tp: float | None = None, trail: float | None = None) -> dict[str, Any]:
        """testnet/live manual market entry: set order leverage, journal the intent, dispatch with an optional bracket."""
        if self.store.position(self.mode, symbol) is not None:
            raise ExchangeError(f"{symbol}: adding to / reducing an exchange position is not supported from the manual "
                                "terminal yet; close it first")
        product = self.ensure_product(symbol)
        self.client.set_order_leverage(product.product_id, int(leverage) if float(leverage).is_integer() else leverage)
        intent = self.store.create_intent(
            intent_id=f"mn-{secrets.token_hex(8)}", client_order_id=self.new_client_oid(symbol, "M"), mode=self.mode,
            symbol=symbol, product_id=product.product_id, role="ENTRY", side="buy" if direction == 1 else "sell",
            size=int(contracts), order_type="market_order", entry_price=ref_price, sl_price=sl, tp_price=tp,
            trail_amount=float(trail or 0), leverage=float(leverage), source="manual")
        return self._dispatch_entry(intent)

    # -- exchange-backed manual trading (Step 8B; Step 9 gates) ---------------------------------------- #
    def read_margin_mode(self) -> str | None:
        """Account margin mode from GET /v2/users/trading_preferences (None when the venue does not report it)."""
        mm = (self.client.get_trading_preferences() or {}).get("margin_mode")
        return None if mm in (None, "") else str(mm)

    def ensure_isolated_margin(self) -> dict[str, Any]:
        """PUT /v2/users/margin_mode isolated. Only an 'already isolated' rejection that a readback confirms is benign;
        every other rejection propagates (F09), so callers never cache a failed switch as success."""
        try:
            r = self.client.set_margin_mode("isolated")
        except DeltaAPIError as exc:
            text = json.dumps(exc.error).lower()
            if not (str(exc.code or "").lower() in BENIGN_MARGIN_CODES or "already in isolated" in text
                    or any(c in text for c in BENIGN_MARGIN_CODES)):
                raise
            mode = self.read_margin_mode()
            if mode != "isolated":
                raise ExchangeError(f"margin mode switch refused ({exc.error}) and the readback shows {mode!r}, not isolated") from exc
            return {"margin_mode": "isolated", "changed": False, "verified_by": "readback", "note": str(exc.error)}
        got = (r or {}).get("margin_mode")
        if got not in (None, "", "isolated"):
            raise ExchangeError(f"margin mode switch answered {got!r}, not isolated")
        return {"margin_mode": "isolated", "changed": True}

    def _duplicate_request(self, client_request_id: str | None) -> sqlite3.Row | None:
        if not client_request_id:
            return None
        return self.store.one("SELECT intent_id, state, symbol FROM order_intents WHERE mode=? AND client_request_id=? "
                              "AND state != 'REJECTED' ORDER BY created_at_us LIMIT 1", (self.mode, client_request_id))

    def manual_entry_gate(self, symbol: str, client_request_id: str | None = None) -> None:
        """Shared manual entry gate (F02): a repeated client_request_id never creates a second intent; on the exchange,
        unknown outcomes are reconciled first and no entry is sent while the symbol has an unresolved/resting intent."""
        if (dup := self._duplicate_request(client_request_id)) is not None:
            raise EntryBlockedError(f"duplicate request {client_request_id}: already submitted as intent {dup['intent_id']} "
                                    f"({dup['state']}); nothing was sent")
        if self.mode == "paper":
            return
        self.reconcile_unknown_intents()
        if (b := self.symbol_blocked(symbol)) is not None:
            raise EntryBlockedError(f"{symbol}: {b} — resolve it first (Sync Exchange, or cancel the resting order); nothing was sent")

    def manual_exchange_entry(self, symbol: str, direction: int, contracts: int, leverage: float, *, order_type: str = "market",
                              ref_price: float, limit_price: float | None = None, sl: float | None = None,
                              tp: float | None = None, trigger: str = TRIGGER_METHOD, client_request_id: str | None = None,
                              prepare: Callable[[], Any] | None = None) -> dict[str, Any]:
        """Open or scale in on the exchange (market or limit) behind the shared entry gate. ``prepare`` (isolated
        margin) runs after the gate and before any order. SL/TP ride along as a bracket on a NEW position."""
        if self.mode == "paper":
            raise ExchangeError("paper mode uses manual_fill")
        self.manual_entry_gate(symbol, client_request_id)
        product = self.ensure_product(symbol)
        p = self.store.position(self.mode, symbol)
        side = "LONG" if direction == 1 else "SHORT"
        if p is not None and p["side"] != side:
            raise ExchangeError(f"{symbol}: an opposite-side order would reduce the {p['side']} position; use Close / Close part")
        if p is not None and abs(float(p["leverage"]) - float(leverage)) > 1e-9:
            raise ValueError(f"{symbol} position is {float(p['leverage']):g}x; scale in at the same leverage")
        if prepare is not None:
            prepare()
        lev = int(leverage) if float(leverage).is_integer() else leverage
        self.client.set_order_leverage(product.product_id, lev)
        bracket = p is None and (sl is not None or tp is not None)
        ot = "limit_order" if order_type == "limit" else "market_order"
        intent = self.store.create_intent(
            intent_id=f"mx-{secrets.token_hex(8)}", client_order_id=self.new_client_oid(symbol, "X"), mode=self.mode,
            symbol=symbol, product_id=product.product_id, role="ENTRY", side="buy" if direction == 1 else "sell",
            size=int(contracts), order_type=ot, entry_price=limit_price if ot == "limit_order" else ref_price,
            sl_price=sl if bracket else None, tp_price=tp if bracket else None, trail_amount=0.0,
            leverage=float(leverage), source="manual", client_request_id=client_request_id)
        iid = intent["intent_id"]
        self.store.transition(iid, "DISPATCHING")
        try:
            order = self.client.place_order(
                product_id=product.product_id, side=intent["side"], size=int(contracts), order_type=ot,
                client_order_id=intent["client_order_id"], limit_price=limit_price if ot == "limit_order" else None,
                bracket_stop_loss_price=sl if bracket else None, bracket_take_profit_price=tp if bracket else None,
                bracket_stop_trigger_method=trigger if bracket else None)
        except AmbiguousOutcomeError as exc:
            self.store.transition(iid, "OUTCOME_UNKNOWN", str(exc), error=str(exc))
            self._event("CRITICAL", "dispatch_outcome_unknown", symbol, {"client_order_id": intent["client_order_id"]})
            return {"action": "outcome_unknown", "intent_id": iid}
        except (DeltaAPIError, RateLimitedError) as exc:
            self.store.transition(iid, "REJECTED", str(exc), error=str(exc))
            raise
        r = self._apply_order(iid, order)
        r.setdefault("intent_id", iid)
        r["exchange_order_id"] = str(order.get("id"))
        return r

    def _resize_legs(self, symbol: str, only: set[str] | None = None) -> list[str]:
        """Resize live SL/TP child orders (all, or the ids in ``only``) to the journal's contracts (PUT /v2/orders)."""
        p = self.store.position(self.mode, symbol)
        self.ensure_product(symbol)
        done = []
        for leg, price in (("sl_order_id", "sl_price"), ("tp_order_id", "tp_price")):
            if p[leg] and p[price] is not None and (only is None or str(p[leg]) in only):
                try:
                    self.client.edit_order(order_id=p[leg], product_id=p["product_id"], size=int(p["contracts"]),
                                           stop_price=p[price])
                    done.append(str(p[leg]))
                except ExchangeError as exc:  # the close/scale-in itself already succeeded: report, never mask it
                    self._event("CRITICAL", "leg_resize_failed", symbol, {"leg": p[leg], "error": str(exc)})
        return done

    def _cancel_legs(self, product_id: int) -> list[str]:
        out = []
        for o in self.client.get_active_orders(product_id):
            if o.get("stop_order_type") in ("stop_loss_order", "take_profit_order"):
                try:
                    self.client.cancel_order(product_id, order_id=o["id"])
                    out.append(str(o["id"]))
                except ExchangeError as exc:
                    self._event("WARN", "leg_cancel_failed", None, str(exc))
        return out

    def _unresolved_exit(self, symbol: str) -> str | None:
        r = self.store.one("SELECT intent_id, state FROM order_intents WHERE mode=? AND symbol=? AND (role='EXIT' OR reduce_only=1) "
                           f"AND state IN ({','.join('?' * len(BLOCKING_STATES))}) LIMIT 1", (self.mode, symbol, *BLOCKING_STATES))
        return None if r is None else f"close intent {r['intent_id']} is {r['state']}"

    def exchange_reduce(self, symbol: str, contracts: int | None = None, reason: str = "MANUAL_CLOSE",
                        client_request_id: str | None = None) -> dict[str, Any]:
        """Reduce-only market order for part or all of a journalled exchange position, booked by the unified exit
        path (F03). Unknown outcomes are reconciled first; no second close is sent while one is unresolved."""
        if self.store.position(self.mode, symbol) is None:
            raise ExchangeError(f"no open {symbol} position in the {self.mode} journal")
        if (dup := self._duplicate_request(client_request_id)) is not None:
            raise EntryBlockedError(f"duplicate request {client_request_id}: already submitted as intent {dup['intent_id']} "
                                    f"({dup['state']}); nothing was sent")
        self.ensure_product(symbol)  # tick size registered in the client before any leg edit
        self.reconcile_unknown_intents()
        if (b := self._unresolved_exit(symbol)) is not None:
            raise EntryBlockedError(f"{symbol}: {b}; not resent — press Sync Exchange to reconcile it")
        p = self.store.position(self.mode, symbol)
        if p is None:
            raise ExchangeError(f"{symbol}: the position was closed while reconciling; nothing was sent")
        n_all = int(p["contracts"])
        n = n_all if contracts is None else int(contracts)
        if not 1 <= n <= n_all:
            raise ValueError(f"cannot close {n} of {n_all} contracts")
        d = 1 if p["side"] == "LONG" else -1
        intent = self.store.create_intent(
            intent_id=f"mx-{secrets.token_hex(8)}", client_order_id=self.new_client_oid(symbol, "C"), mode=self.mode,
            symbol=symbol, product_id=p["product_id"], role="EXIT", side="sell" if d == 1 else "buy", size=n,
            order_type="market_order", reduce_only=1, leverage=p["leverage"], source="manual", exit_reason=reason,
            client_request_id=client_request_id)
        iid = intent["intent_id"]
        self.store.transition(iid, "DISPATCHING")
        try:
            order = self.client.place_order(product_id=p["product_id"], side=intent["side"], size=n, order_type="market_order",
                                            reduce_only=True, client_order_id=intent["client_order_id"])
        except AmbiguousOutcomeError as exc:
            self.store.transition(iid, "OUTCOME_UNKNOWN", str(exc), error=str(exc))
            self._event("CRITICAL", "dispatch_outcome_unknown", symbol, {"client_order_id": intent["client_order_id"], "reduce_only": True})
            raise ExchangeError("close order outcome unknown: not resent; press Sync Exchange to reconcile") from exc
        except (DeltaAPIError, RateLimitedError) as exc:
            self.store.transition(iid, "REJECTED", str(exc), error=str(exc))
            raise
        r = self._apply_order(iid, order)
        if r["action"] == "cancelled":
            raise ExchangeError(f"{symbol}: reduce-only close did not fill (state {order.get('state')})")
        return r

    def refresh_resting_orders(self) -> list[dict[str, Any]]:
        """Advance ACKNOWLEDGED / PARTIALLY_FILLED exchange intents (manual limits and asynchronously acknowledged bot
        entries) from Delta's order state, booking each new partial fill as soon as it is seen (F04)."""
        if self.mode == "paper":
            return []
        out = []
        for it in self.store.q("SELECT * FROM order_intents WHERE mode=? AND state IN ('ACKNOWLEDGED','PARTIALLY_FILLED') "
                               "AND exchange_order_id IS NOT NULL ORDER BY created_at_us", (self.mode,)):
            o = self.client.get_order_by_id(it["exchange_order_id"])
            filled = int(o.get("size") or it["size"]) - int(o.get("unfilled_size") or 0)
            if o.get("state") in ("open", "pending") and filled <= int(it["filled_size_booked"] or 0):
                continue
            try:
                out.append({"intent_id": it["intent_id"], **self._apply_order(it["intent_id"], o)})
            except (RuntimeError, ExchangeError) as exc:
                out.append({"intent_id": it["intent_id"], "error": str(exc)})
        return out

    def sync_exchange_position(self, symbol: str) -> dict[str, Any] | None:
        """testnet/live: bring the journal position in line with Delta's real-time position (Source #3).
        flat -> book the exit (SL/TP leg or external fills); smaller -> book the external partial close; larger -> adopt
        the external add (size, average entry); side flipped -> close the journal position (the new side shows as an
        unjournalled exchange position). The journal's own orders and already-booked fills are never counted twice."""
        p = self.store.position(self.mode, symbol)
        if p is None or self.mode == "paper":
            return None
        if self.symbol_blocked(symbol) is not None:  # an in-flight / resting intent may explain a difference: book it first
            self.reconcile_unknown_intents()
            self.refresh_resting_orders()
            p = self.store.position(self.mode, symbol)
            if p is None:
                return None
            if self.store.one(f"SELECT 1 FROM order_intents WHERE mode=? AND symbol=? AND state IN "
                              f"({','.join('?' * len(UNRESOLVED_STATES))})", (self.mode, symbol, *UNRESOLVED_STATES)):
                return {"action": "deferred", "reason": "an unresolved intent may explain the exchange position"}
        pos = self.client.get_position(p["product_id"])
        d = 1 if p["side"] == "LONG" else -1
        ex, j = int(pos["size"]), d * int(p["contracts"])
        if ex == j:
            return None
        start, end = int(p["opened_at_us"]) - 5_000_000, self.clock() + 1_000_000
        own = {str(r[0]) for r in self.store.q("SELECT exchange_order_id FROM order_intents WHERE mode=? AND symbol=? AND "
                                               "exchange_order_id IS NOT NULL", (self.mode, symbol))}
        own |= {r[0] for r in self.store.q("SELECT exit_order_id FROM trade_history WHERE mode=? AND symbol=? AND opened_at_us=? "
                                           "AND exit_order_id IS NOT NULL", (self.mode, symbol, p["opened_at_us"]))}
        booked = self.store.booked_fill_ids(symbol)
        fills = [f for f in self.client.get_fills(p["product_id"], start, end)
                 if str(f.get("id")) not in booked and str(f.get("order_id")) not in own]
        exit_side = "sell" if d == 1 else "buy"
        if ex * j <= 0 or abs(ex) < abs(j):
            q = abs(j) if ex * j <= 0 else abs(j) - abs(ex)
            return self._book_external_exit(p, [f for f in fills if f.get("side") == exit_side], q, end, flipped=ex * j < 0)
        return self._book_external_add(p, [f for f in fills if f.get("side") != exit_side], abs(ex) - abs(j), pos)

    def _fill_rows(self, fills: list[dict[str, Any]], symbol: str, role: str, cv: float) -> list[dict[str, Any]]:
        return [{"fill_id": str(f["id"]), "intent_id": None, "order_id": str(f.get("order_id")), "symbol": symbol, "role": role,
                 "side": f.get("side"), "size": int(f["size"]), "price": float(f["price"]),
                 "fee_usd": self._venue_fee(f) if self._venue_fee(f) is not None
                 else int(f["size"]) * cv * float(f["price"]) * self.fee} for f in fills]

    def _book_external_exit(self, p: Any, xf: list[dict[str, Any]], q: int, end: int, flipped: bool) -> dict[str, Any]:
        sym, n_all = p["symbol"], int(p["contracts"])
        qty = sum(int(f["size"]) for f in xf)
        if qty == 0:
            self._event("CRITICAL", "flat_without_exit_fill" if q == n_all else "reduced_without_exit_fill", symbol=sym,
                        detail={"journal": n_all, "closed_outside": q})
            self.store.set_halt(True, f"{sym}: exchange position changed by {q} contracts but no exit fill was found")
            return {"action": "halted"}
        if qty != q:
            self._event("WARN", "exit_fills_mismatch", sym, {"exit_fill_contracts": qty, "booked_contracts": q, "flipped": flipped})
        rows = self._fill_rows(xf, sym, "EXT_EXIT", float(p["contract_value"]))
        px = sum(r["price"] * r["size"] for r in rows) / qty
        fee = sum(r["fee_usd"] for r in rows) * min(1.0, q / qty)
        oid = str(xf[-1].get("order_id"))
        reason = {p["sl_order_id"]: "TSL" if p["sl_price"] != p["initial_sl"] else "SL", p["tp_order_id"]: "TP"}.get(oid, "EXT")
        with self.store.tx():
            if q < n_all:
                f = q / n_all
                part = {**dict(p), "contracts": q, "entry_fee_usd": p["entry_fee_usd"] * f,
                        "realized_funding_usd": p["realized_funding_usd"] * f}
                row = self._trade_row(_Row(part), px, px, reason, end, fee, oid)
                tid = self.store.reduce_position(self.mode, sym, row, {
                    "contracts": n_all - q, "entry_fee_usd": p["entry_fee_usd"] * (1 - f),
                    "realized_funding_usd": p["realized_funding_usd"] * (1 - f), "margin_usd": p["margin_usd"] * (1 - f)})
            else:
                row = self._trade_row(p, px, px, reason, end, fee, oid)
                tid = self.store.close_position(self.mode, sym, row, self._cooldown_until(end))
            self.store.record_fills(rows)
        legs = self._resize_legs(sym) if q < n_all else self._cancel_legs(int(p["product_id"]))  # surviving legs
        action = "partial_exit" if q < n_all else "exit"
        self._event("INFO" if reason != "EXT" else "WARN", f"exchange_{action}", sym,
                    {"reason": reason, "contracts": q, "price": px, "net_inr": row["net_pnl_inr"], "flipped": flipped, "legs": legs})
        self.notifier.notify_trade_exit({**row, "trade_id": tid})
        return {"action": action, "trade_id": tid, "reason": reason, "contracts": q, "remaining": n_all - q,
                "flipped": flipped, **row}

    def _book_external_add(self, p: Any, ef: list[dict[str, Any]], q: int, pos: dict[str, Any]) -> dict[str, Any]:
        sym, n0 = p["symbol"], int(p["contracts"])
        cv, n = float(p["contract_value"]), n0 + q
        rows = self._fill_rows(ef, sym, "EXT_ENTRY", cv)
        qty = sum(r["size"] for r in rows)
        add_px = sum(r["price"] * r["size"] for r in rows) / qty if qty else float(p["entry_price"])
        avg = float(pos.get("entry_price") or 0) or (n0 * float(p["entry_price"]) + q * add_px) / n
        fee = sum(r["fee_usd"] for r in rows) if rows else q * cv * add_px * self.fee
        with self.store.tx():
            self.store.update_position(self.mode, sym, contracts=n, entry_price=avg, raw_entry_price=avg,
                                       entry_fee_usd=float(p["entry_fee_usd"]) + fee, margin_usd=n * cv * avg / float(p["leverage"]))
            self.store.record_fills(rows)
        legs = self._resize_legs(sym)
        self._event("WARN", "external_add", sym, {"added": q, "position": n, "avg_entry": avg, "fills": qty, "legs_resized": legs})
        return {"action": "external_add", "contracts": q, "position_size": n, "avg_entry": avg, "legs_resized": legs}

    def close_position(self, symbol: str, reason: str, price: float | None = None, bar_us: int | None = None) -> dict[str, Any] | None:
        """Protective close of one tracked position: paper at ``price``; testnet/live via a reduce-only market order."""
        p = self.store.position(self.mode, symbol)
        if p is None:
            return None
        if self.mode == "paper":
            if price is None:
                raise ValueError("paper close needs a price")
            return self._paper_close(symbol, price, reason, bar_us if bar_us is not None else self.clock())
        d = 1 if p["side"] == "LONG" else -1
        r = self.client.close_position_reduce_only(p["product_id"], d * int(p["contracts"]))
        if not r["flat"]:
            self.store.set_halt(True, f"{symbol}: reduce-only close left {r['residual_size']} contracts")
            self._event("CRITICAL", "close_incomplete", symbol, r)
        return self.sync_exchange_position(symbol) or r

    def manual_close(self, symbol: str, contracts: int | None = None, raw_price: float | None = None) -> dict[str, Any]:
        """Close all or part of a position at market (MANUAL_CLOSE)."""
        p = self.store.position(self.mode, symbol)
        if p is None:
            raise ExchangeError(f"no open {symbol} position")
        if self.mode == "paper":
            if raw_price is None:
                raise ValueError("paper close needs a price")
            return self._paper_close(symbol, raw_price, "MANUAL_CLOSE", self.clock(), contracts=contracts)
        if contracts is not None and int(contracts) != int(p["contracts"]):
            raise ExchangeError("partial closes of exchange positions are not supported from the manual terminal yet")
        return self.close_position(symbol, "MANUAL_CLOSE") or {}

    def unrealized_inr(self, prices: dict[str, float]) -> float:
        """Mark-to-last-price gross PnL of open positions (INR)."""
        tot = 0.0
        for p in self.store.positions(self.mode):
            px = prices.get(p["symbol"])
            if px is not None:
                d = 1 if p["side"] == "LONG" else -1
                tot += d * p["contracts"] * p["contract_value"] * (px - p["entry_price"])
        return tot * self.rate

    def check_daily_loss(self, day_start_equity_inr: float, unrealized_inr: float = 0.0) -> bool:
        """Halt until next UTC midnight when realized+unrealized equity fell 5% below the day start."""
        eq = self.realized_equity_inr() + unrealized_inr
        if eq > day_start_equity_inr * (1 - DAILY_LOSS_LIMIT_PCT / 100):
            return False
        t = self.clock()
        self.store.set_halt(True, f"daily loss limit {DAILY_LOSS_LIMIT_PCT}% hit", t - t % 86_400_000_000 + 86_400_000_000)
        self._event("CRITICAL", "daily_loss_halt", detail={"equity_inr": eq, "day_start_inr": day_start_equity_inr})
        return True

    def emergency_flatten(self) -> dict[str, Any]:
        """testnet/live: close everything; halts unless the exchange is verifiably flat."""
        if self.mode == "paper":
            return {"status": "FLAT", "note": "paper positions are closed by on_bar"}
        uid = self.store.config()["bound_user_id"] or int(self.client.get_trading_preferences()["user_id"])
        r = self.client.emergency_close_all(uid)
        if r["status"] != "FLAT":
            self.store.set_halt(True, f"emergency close incomplete: {r['residual']} skipped={r['skipped_products']}")
            self._event("CRITICAL", "residual_exposure", detail=r)
        return r

    def bind_account(self) -> int:
        """Record the account's immutable user_id; refuse to run if the credentials now point elsewhere."""
        uid = int(self.client.get_trading_preferences()["user_id"])
        bound = self.store.config()["bound_user_id"]
        if bound is None:
            self.store.set_bound_user(uid)
        elif int(bound) != uid:
            raise ExchangeError(f"credentials belong to user {uid}, but this DB is bound to user {bound}")
        return uid

    def prepare_account(self, exchange_sizes: dict[str, int] | None = None) -> dict[str, Any]:
        """testnet/live (F07): bind the account, isolated margin (strict, F09) and 10x order leverage confirmed by
        Delta's answer on every flat product. A product holding a position at another leverage is NOT changed: it
        stays blocked for bot entries until flat. ``exchange_sizes`` (signed contracts per symbol) avoids re-reading."""
        self.account_policy = None
        uid = self.bind_account()
        mm = self.ensure_isolated_margin()
        lev: dict[str, Any] = {}
        blocked: dict[str, str] = {}
        for s, prod in self.products.items():
            size = (exchange_sizes or {}).get(s)
            try:
                if size is None:
                    size = self.client.get_position(prod.product_id)["size"]
                if size:
                    have = (self.client.get_order_leverage(prod.product_id) or {}).get("leverage")
                    lev[s] = {"leverage": have, "changed": False, "open_size": size}
                    if have in (None, "") or float(have) != float(LEVERAGE):
                        blocked[s] = (f"open exchange position ({size}) at {have}x, policy {LEVERAGE}x: left unchanged; "
                                      "bot entries blocked until it is flat")
                else:
                    lev[s] = self.client.set_order_leverage(prod.product_id, LEVERAGE)  # raises unless Delta echoes 10x
            except ExchangeError as exc:
                blocked[s] = f"leverage policy failed: {exc}"
        self.account_policy = {"user_id": uid, "margin_mode": mm, "leverage": lev, "blocked": blocked,
                               "verified_at_us": self.clock()}
        if blocked:
            self._event("WARN", "account_policy_blocked", None, blocked)
        return self.account_policy

    def account_policy_refusal(self, symbol: str) -> str | None:
        """Why a testnet/live bot entry on ``symbol`` must not be sent under the margin/leverage policy (F07)."""
        if self.account_policy is None:
            try:
                self.prepare_account()
            except ExchangeError as exc:
                return f"isolated margin / {LEVERAGE}x leverage could not be verified: {exc}"
        return self.account_policy["blocked"].get(symbol)

    # -- paper runner ------------------------------------------------------------- #
    def run_paper_cycle(self, sync: bool = True) -> dict[str, Any]:
        """One cycle: sync candles, replay new closed 1m bars over open positions, act on fresh 1h signals."""
        if self.mode != "paper":
            raise ExchangeError("run_paper_cycle is paper-only")
        sig = evaluate_latest_signals(list(self.profiles), sync=sync, profiles=self.profiles)
        report: dict[str, Any] = {}
        for sym, s in sig.items():
            acts = []
            hist = s.pop("history")
            p = self.store.position(self.mode, sym)
            if p is not None:
                prev = hist[["funding_rate", "mark_close"]].shift(1)
                new = hist[hist.index > pd.Timestamp(int(p["last_bar_us"]), unit="us", tz="UTC")]
                for ts, r in new.iterrows():
                    a = self.on_bar(sym, int(ts.value // 1000), r["open"], r["high"], r["low"], r["close"],
                                    prev.at[ts, "funding_rate"], prev.at[ts, "mark_close"])
                    if a:
                        acts.append(a)
                    if self.store.position(self.mode, sym) is None:
                        break
            if s["fresh_signal"]:
                tk = DeltaRestClientAdapter("paper").get_ticker(sym)
                ref = tk["best_ask"] if s["signal"] == 1 else tk["best_bid"]
                acts.append(self.on_signal(sym, s["signal"], int(s["bar_open"].value // 1000), s["atr14"],
                                           ref or tk["mark_price"]))
            report[sym] = {"signal": s, "actions": acts}
        return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Step 5 execution engine (paper by default)")
    ap.add_argument("--mode", choices=["paper", "testnet", "live"], default=settings.EXECUTION_MODE)
    ap.add_argument("--loop", action="store_true", help="run a cycle every --interval seconds")
    ap.add_argument("--interval", type=float, default=60.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.mode != "paper":
        raise SystemExit("Only the paper runner is wired to the CLI in Step 5; testnet/live are exercised by step5_verify.py.")
    store = StateStore(settings.data_path / settings.BOT_DB_FILE)
    try:
        eng = ExecutionEngine(store, "paper")
        while True:
            rep = eng.run_paper_cycle()
            for sym, r in rep.items():
                s = r["signal"]
                print(f"{to_ts(int(s['bar_open'].value // 1000))} {sym} {s['variant']} close={s['close']:.2f} "
                      f"signal={s['signal_side'] or '-'} actions={[a.get('action') for a in r['actions']]}")
            print(f"paper equity ₹{eng.realized_equity_inr():,.2f}")
            if not args.loop:
                break
            time.sleep(args.interval)
    finally:
        store.close()


if __name__ == "__main__":
    main()
