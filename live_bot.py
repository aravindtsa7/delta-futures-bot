"""Step 6: 24/7 event-driven bot runner with a readiness state machine.

States
  BOOTING -> CONNECTING -> AUTHENTICATING -> SUBSCRIBING -> SNAPSHOTTING -> RECONCILING -> OBSERVED_READY
  DEGRADED  data or exchange state could not be verified (entries blocked, protection allowed)
  HALTED    daily-loss (5% of UTC start-of-day equity) or 4-consecutive-loss lock; flat until next UTC day

New entries only when state == OBSERVED_READY and no halt is active. Protective actions (in-place SL
trailing, reduce-only closes, emergency flatten) run in every state.

Step 9: reconciliation also applies the account policy (isolated margin + 10x, verified; a product holding a
position at another leverage stays blocked for bot entries), confirms each position's SL/TP legs strictly (else
missing_confirmed_protection -> DEGRADED), books partial / resting fills, and treats missing private streams as
DEGRADED. Emergency flatten cancels entry orders before closing and reports FLAT only when nothing can reopen
exposure. Socket frames, the REST poller and control commands run in worker threads, serialised by one lock.

Usage
  .venv\\Scripts\\python.exe live_bot.py --mode paper --once           # one reconciliation + evaluation cycle
  .venv\\Scripts\\python.exe live_bot.py --mode paper --duration 3600  # WebSocket + REST-poll runner for 1h
  .venv\\Scripts\\python.exe live_bot.py --mode paper --loop           # run until Ctrl+C
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from rich.console import Console
from rich.table import Table

import data_manager as dm
import execution_engine as ee
from config import get_mode_db_path, settings
from exchange_client import DeltaRestClientAdapter, ExchangeError, LiveTradingDisabledError
from ws_manager import DeltaWebSocketManager, default_connector, rest_backfill

log = logging.getLogger(__name__)

LOCK_PATH = settings.data_path / "live_bot.lock"
LOCK_BYTE_OFFSET = 1 << 20      # lock a byte far from the info header so other processes can read the header
LOCK_INFO_BYTES = 512
MAX_CONSECUTIVE_LOSSES = 4
ENTRY_MAX_DELAY_S = 300         # act on a 1h signal only within 5 min of the bar close (backtest enters at next open)
QUOTE_MAX_AGE_S = 30
POLL_INTERVAL_S = 15
POLL_GRACE_S = 75               # REST-poll a minute the socket has not delivered 75 s after it closed
BRACKET_RETRY_S = 300           # re-attach missing protection at most every 5 min; in between only read it back

STATES = ("BOOTING", "CONNECTING", "AUTHENTICATING", "SUBSCRIBING", "SNAPSHOTTING", "RECONCILING",
          "OBSERVED_READY", "DEGRADED", "HALTED")


class BotAlreadyRunningError(RuntimeError):
    pass


def _locked(fn: Callable) -> Callable:
    """Serialise a runner entry point: socket frames, the REST poller and control commands run in worker threads."""
    @functools.wraps(fn)
    def wrapper(self: "LiveBotRunner", *a: Any, **kw: Any) -> Any:
        with self._work:
            return fn(self, *a, **kw)
    return wrapper


def auto_bot_live_refusal(mode: str) -> str | None:
    """The 24/7 auto-bot needs a third, explicit switch to trade real money (manual live trading does not)."""
    if mode != "live":
        return None
    if not settings.is_live:
        return "live mode requires MODE='live' AND LIVE_TRADING_ENABLED=True"
    if not settings.AUTO_BOT_LIVE_ENABLED:
        return ("the auto-bot will not trade real money unless AUTO_BOT_LIVE_ENABLED=true is also set in .env "
                "(its strategies failed the Step 4B screen); manual live trading is available in the Manual Trade tab")
    return None


# --------------------------------------------------------------------------- #
# Single-instance lock
# --------------------------------------------------------------------------- #
class InstanceLock:
    def __init__(self, path, mode: str):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
        try:
            if sys.platform == "win32":
                import msvcrt
                os.lseek(self.fd, LOCK_BYTE_OFFSET, 0)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = self.read_info(path)
            os.close(self.fd)
            self.fd = None
            raise BotAlreadyRunningError(f"{path} is held by {holder or 'another process'}") from exc
        self.info = {"pid": os.getpid(), "mode": mode, "epoch": int(time.time()),
                     "started": pd.Timestamp.now(tz="UTC").isoformat()}
        os.lseek(self.fd, 0, 0)
        os.write(self.fd, json.dumps(self.info).ljust(LOCK_INFO_BYTES).encode())

    @staticmethod
    def is_held(path) -> bool:
        """True if another process holds the lock (probe only: never writes the info header)."""
        if not path.exists():
            return False
        fd = os.open(str(path), os.O_RDWR)
        try:
            if sys.platform == "win32":
                import msvcrt
                os.lseek(fd, LOCK_BYTE_OFFSET, 0)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True
                os.lseek(fd, LOCK_BYTE_OFFSET, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                return False
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    @staticmethod
    def read_info(path) -> dict[str, Any] | None:
        try:
            with open(path, "rb") as f:
                return json.loads(f.read(LOCK_INFO_BYTES).decode().strip() or "null")
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                os.lseek(self.fd, LOCK_BYTE_OFFSET, 0)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(self.fd)
            self.fd = None


# --------------------------------------------------------------------------- #
# Control channel: the dashboard drops command files; the running bot (sole DB writer) executes them
# --------------------------------------------------------------------------- #
CONTROL_DIR = settings.data_path / "bot_control"
CONTROL_COMMANDS = ("pause", "resume", "flatten", "stop")


class ControlChannel:
    def __init__(self, directory=CONTROL_DIR):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def submit(self, cmd: str, args: dict[str, Any] | None = None) -> str:
        if cmd not in CONTROL_COMMANDS:
            raise ValueError(f"unknown command {cmd!r}")
        cid = f"{int(time.time() * 1000)}-{os.getpid()}-{cmd}"
        tmp = self.dir / f"{cid}.tmp"
        tmp.write_text(json.dumps({"id": cid, "cmd": cmd, "args": args or {}, "at": time.time()}), encoding="utf-8")
        os.replace(tmp, self.dir / f"{cid}.cmd.json")
        return cid

    def pending(self) -> list[dict[str, Any]]:
        out = []
        for f in sorted(self.dir.glob("*.cmd.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out

    def complete(self, cid: str, result: dict[str, Any]) -> None:
        tmp = self.dir / f"{cid}.res.tmp"
        tmp.write_text(json.dumps(result, default=str), encoding="utf-8")
        os.replace(tmp, self.dir / f"{cid}.result.json")
        (self.dir / f"{cid}.cmd.json").unlink(missing_ok=True)

    def cancel(self, cid: str) -> None:
        (self.dir / f"{cid}.cmd.json").unlink(missing_ok=True)

    def wait_result(self, cid: str, timeout: float = 20.0) -> dict[str, Any] | None:
        f = self.dir / f"{cid}.result.json"
        end = time.time() + timeout
        while time.time() < end:
            if f.exists():
                try:
                    r = json.loads(f.read_text(encoding="utf-8"))
                    f.unlink(missing_ok=True)
                    return r
                except (OSError, ValueError):
                    pass
            time.sleep(0.1)
        return None


# --------------------------------------------------------------------------- #
# Market data (REST, parquet history) used for reconciliation, signals and the poll fallback
# --------------------------------------------------------------------------- #
class RestMarketData:
    def __init__(self, symbols: list[str], profiles: dict[str, ee.StrategyProfile] = ee.FROZEN_PROFILES):
        self.symbols, self.profiles = symbols, profiles
        self._hist: dict[str, pd.DataFrame] = {}
        self._public = DeltaRestClientAdapter("paper")

    def sync(self, symbols: list[str] | None = None) -> dict[str, dict[str, Any]]:
        out = {}
        for sym in symbols or self.symbols:
            days = 90 if sym in self.profiles else 2  # managed-only symbols (manual positions) need recent minutes only
            fetched = sum(dm.download_1m_series(sym, st, days=days).rows_fetched for st in dm.SERIES_TYPES)
            hist = ee.load_history(sym)
            self._hist[sym] = hist
            g = hist.attrs.get("gaps", {})
            out[sym] = {"last_1m": int(hist.index[-1].value // 10**9), "fetched": fetched,
                        "gaps": {k: v.gap_count for k, v in g.items()}}
        return out

    def history(self, sym: str) -> pd.DataFrame:
        if sym not in self._hist:
            self.sync([sym])
        return self._hist[sym]

    def evaluate(self, sym: str, hour_close_s: int | None = None) -> dict[str, Any] | None:
        self.sync([sym])
        s = ee.evaluate_symbol(self.profiles[sym], self._hist[sym])
        if hour_close_s is not None and int(s["bar_close"].value // 10**9) != hour_close_s:
            return None  # REST has not published that hour yet
        return s

    def minutes_after(self, sym: str, after_s: int) -> list[tuple]:
        h = self.history(sym)
        prev = h[["funding_rate", "mark_close"]].shift(1)
        new = h[h.index > pd.Timestamp(after_s, unit="s", tz="UTC")]
        return [(int(ts.value // 10**9), r["open"], r["high"], r["low"], r["close"], prev.at[ts, "funding_rate"],
                 prev.at[ts, "mark_close"]) for ts, r in new.iterrows()]

    def funding_inputs(self, sym: str, bar_s: int) -> tuple[float | None, float | None]:
        t = pd.Timestamp(bar_s - 60, unit="s", tz="UTC")
        h = self.history(sym)
        if t not in h.index:
            self.sync([sym])
            h = self._hist[sym]
        if t not in h.index:
            return None, None
        return float(h.at[t, "funding_rate"]), float(h.at[t, "mark_close"])

    def ticker(self, sym: str) -> dict[str, Any]:
        return self._public.get_ticker(sym)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
class LiveBotRunner:
    def __init__(self, mode: str = "paper", *, client: DeltaRestClientAdapter | None = None, market: Any = None,
                 store_path=None, lock_path=LOCK_PATH, clock: Callable[[], float] = time.time,
                 profiles: dict[str, ee.StrategyProfile] = ee.FROZEN_PROFILES, ws_connector=default_connector,
                 ws_api_key: str | None = None, ws_api_secret: str | None = None, watchdog_s: float = 35.0,
                 max_consecutive_losses: int = MAX_CONSECUTIVE_LOSSES, control_dir=None, notifier: Any = None):
        if mode == "live" and not settings.is_live:
            raise LiveTradingDisabledError("live bot needs MODE='live' AND LIVE_TRADING_ENABLED=True")
        self._work = threading.RLock()
        self._bracket_attempts: dict[str, float] = {}
        self.lock = InstanceLock(lock_path, mode)
        self.store = None
        try:
            self.mode, self.clock, self.profiles = mode, clock, profiles
            self.symbols = list(profiles)  # extended below with symbols that only need protective management
            self.client = client if client is not None else (DeltaRestClientAdapter.from_settings(mode) if mode != "paper" else None)
            self.store = ee.StateStore(store_path or get_mode_db_path(mode),
                                       clock=lambda: int(self.clock() * 1_000_000))
            self.engine = ee.ExecutionEngine(self.store, mode, self.client, clock=lambda: int(self.clock() * 1_000_000),
                                             profiles=profiles, notifier=notifier)
            self.notifier = self.engine.notifier
            self.control = ControlChannel(control_dir or CONTROL_DIR)
            managed = [p["symbol"] for p in self.store.positions(mode)] + [i["symbol"] for i in self.engine.pending_limits()]
            for sym in managed:
                if sym not in self.symbols:
                    self.symbols.append(sym)
                    self.engine.ensure_product(sym)
            self.market = market if market is not None else RestMarketData(self.symbols, profiles)
            if mode != "paper":
                ws_api_key = ws_api_key or (settings.TESTNET_API_KEY if mode == "testnet" else settings.LIVE_API_KEY)
                ws_api_secret = ws_api_secret or (settings.TESTNET_API_SECRET if mode == "testnet" else settings.LIVE_API_SECRET)
            backfill_client = self.client if mode != "paper" else DeltaRestClientAdapter("paper")
            self.ws = DeltaWebSocketManager(mode, self.symbols, self, api_key=ws_api_key, api_secret=ws_api_secret,
                                            connector=ws_connector, backfill=rest_backfill(backfill_client),
                                            watchdog_s=watchdog_s, offload=True)
        except BaseException:
            if self.store is not None:
                self.store.close()
            self.lock.release()
            raise
        self.max_consecutive_losses = max_consecutive_losses
        self.state = "BOOTING"
        self.transitions: list[dict[str, Any]] = [{"t": clock(), "from": None, "to": "BOOTING", "reason": "init"}]
        self.last_minute: dict[str, int] = {}
        self.last_close: dict[str, float] = {}
        self.pending_hour: dict[str, int] = {}
        self.last_eval: dict[str, dict[str, Any]] = {}
        self.entries: list[dict[str, Any]] = []
        self.blocked_signals: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.minute_gaps = 0
        self.reconciliations: list[dict[str, Any]] = []
        self.disconnects_since_ready = 0
        self.day: int | None = None
        self.day_start_equity: float | None = None
        ev = self.store.one("SELECT max(at_us) AS t FROM events WHERE kind='loss_streak_halt' AND mode=?", (mode,))
        self.streak_floor_us = int(ev["t"]) if ev and ev["t"] else 0

    def close(self) -> None:
        if self.store is not None:
            self.store.close()
            self.store = None
        self.lock.release()

    # -- state machine --------------------------------------------------------------- #
    @_locked
    def transition(self, new: str, reason: str = "") -> None:
        if new not in STATES:
            raise ValueError(new)
        if new == self.state:
            return
        self.transitions.append({"t": self.clock(), "from": self.state, "to": new, "reason": reason})
        log.info("state %s -> %s (%s)", self.state, new, reason)
        self.store.event("INFO" if new != "DEGRADED" else "WARN", "state", None, {"from": self.state, "to": new, "reason": reason}, self.mode)
        old, self.state = self.state, new
        first_ready = new == "OBSERVED_READY" and old == "RECONCILING" and len(self.reconciliations) <= 1
        if new in ("RECONCILING", "HALTED", "DEGRADED") or first_ready:
            label = {"RECONCILING": "Reconciling", "HALTED": "Halted", "DEGRADED": "Degraded", "OBSERVED_READY": "Ready"}[new]
            self.notifier.notify_bot_lifecycle(label, self.mode, new, reason)

    @property
    def daily_halted(self) -> bool:
        return self.engine.halted()[0]

    def can_enter(self) -> bool:
        return self.state == "OBSERVED_READY" and not self.daily_halted and not self.engine.entries_paused()

    # -- WebSocket handler callbacks (worker threads; one lock) ------------------------ #
    @_locked
    def on_ws_phase(self, phase: str, generation: int) -> None:
        if phase in ("CONNECTING", "AUTHENTICATING", "SUBSCRIBING", "SNAPSHOTTING"):
            self.transition(phase, f"ws generation {generation}")

    @_locked
    def on_ws_ready(self, generation: int) -> None:
        self.run_reconciliation(f"ws generation {generation} ready")

    @_locked
    def on_ws_degraded(self, generation: int, problems: dict[str, list[dict[str, Any]]]) -> None:
        """F10: some required subscriptions were rejected / never acknowledged. Public -> REST fallback (recorded);
        private (testnet/live) -> the next reconciliation reports private_stream_unavailable -> DEGRADED."""
        self.engine._event("CRITICAL" if problems.get("private") and self.mode != "paper" else "WARN", "ws_rest_fallback", None,
                           {"generation": generation, **problems}, notify=True)

    @_locked
    def on_ws_disconnect(self, generation: int, reason: str) -> None:
        self.disconnects_since_ready += 1
        if self.disconnects_since_ready >= 3:
            self.transition("DEGRADED", f"{self.disconnects_since_ready} disconnects without recovery: {reason}")
        else:
            self.transition("RECONCILING", f"socket drop (generation {generation}): {reason}")

    @_locked
    def on_sequence_anomaly(self, a: dict[str, Any]) -> None:
        self.engine._event("WARN", "sequence_anomaly", a.get("symbol"), a, notify=True)
        self.transition("RECONCILING", f"{a['kind']} on {a['channel']} {a['symbol']}: {a['prev_seq']} -> {a['seq']}")
        self.ws.invalidate(f"sequence {a['kind']} on {a['channel']}")

    @_locked
    def on_subscription_error(self, socket: str, ch: dict[str, Any]) -> None:
        self.store.event("WARN", "subscription_error", None, {"socket": socket, **ch}, self.mode)

    def on_closed_1m_candle(self, c: Any) -> None:
        if c.symbol in self.symbols:
            self.on_minute_close(c.symbol, c.start, c.open, c.high, c.low, c.close, c.source)

    def _private_update(self, sym: str, oid: str, closed: bool, source: str) -> None:
        """An order / fill event: book fills of our own resting or partially filled orders now (F04), and the exit
        when one of a position's SL/TP legs is done."""
        if oid and self.store.one("SELECT 1 FROM order_intents WHERE mode=? AND exchange_order_id=? AND state IN "
                                  "('ACKNOWLEDGED','PARTIALLY_FILLED')", (self.mode, oid)):
            for r in self.engine.refresh_resting_orders():
                self.actions.append({"symbol": sym, "source": source, **r})
        p = self.store.position(self.mode, sym) if sym else None
        if p is not None and oid in (p["sl_order_id"], p["tp_order_id"]) and closed:
            r = self.engine.sync_exchange_position(sym)
            if r:
                self.actions.append({"symbol": sym, "source": source, **r})

    @_locked
    def on_order(self, m: dict[str, Any]) -> None:
        """Private 'orders' item, normalized by ws_manager (id == order_id; snapshot envelopes unpacked)."""
        if self.mode == "paper":
            return
        self._private_update(str(m.get("product_symbol") or ""), str(m.get("id") or m.get("order_id") or ""),
                             m.get("state") == "closed", "ws_orders")

    @_locked
    def on_user_trade(self, m: dict[str, Any]) -> None:
        """Private 'v2/user_trades' item: a fill on one of our orders."""
        if self.mode == "paper":
            return
        self._private_update(str(m.get("product_symbol") or ""), str(m.get("order_id") or ""), True, "ws_user_trades")

    @_locked
    def on_position(self, m: dict[str, Any]) -> None:
        """Private 'positions' item: a size that differs from the journal is reconciled at once (Source #2/#3)."""
        if self.mode == "paper" or m.get("snapshot"):
            return  # snapshots arrive during the handshake; the REST reconciliation covers them
        sym = str(m.get("product_symbol") or "")
        p = self.store.position(self.mode, sym) if sym else None
        if p is not None and int(m.get("size") or 0) != (1 if p["side"] == "LONG" else -1) * int(p["contracts"]):
            r = self.engine.sync_exchange_position(sym)
            if r:
                self.actions.append({"symbol": sym, "source": "ws_positions", **r})

    # -- reconciliation ---------------------------------------------------------------- #
    @_locked
    def run_reconciliation(self, reason: str) -> dict[str, Any]:
        self.transition("RECONCILING", reason)
        now = int(self.clock())
        summary: dict[str, Any] = {"reason": reason, "ws_generation": self.ws.generation, "at": now, "issues": []}
        issues = summary["issues"]
        summary["unknown_intents"] = self.engine.reconcile_unknown_intents()
        if any(r.get("result") == "STILL_UNKNOWN" for r in summary["unknown_intents"]):
            issues.append("OUTCOME_UNKNOWN intent unresolved")
        issues.extend(f"unresolved_position_delta:{r.get('symbol')}" for r in summary["unknown_intents"]
                      if r.get("result") == "UNRESOLVED_POSITION_DELTA")
        degraded = self.ws.degraded or {}
        summary["ws_feed"] = "REST_FALLBACK" if degraded.get("public") else "WEBSOCKET"
        if self.mode != "paper":
            if degraded.get("private"):  # F10: account updates are not streaming -> no new entries
                issues.append("private_stream_unavailable: " + ", ".join(p["channel"] for p in degraded["private"]))
            summary["user_id"] = self.engine.bind_account()
            summary["resting_orders"] = self.engine.refresh_resting_orders()
            bals = self.client.get_wallet_balances()
            usd = next((b for b in bals if b.get("asset_symbol") == "USD"), {})
            summary["wallet_usd_available"] = float(usd.get("available_balance") or 0)
            orders = self.client.get_active_orders()
            summary["open_orders"] = len(orders)
            summary["positions"] = {}
            sizes: dict[str, int] = {}
            for sym, spec in self.engine.products.items():
                pos = self.client.get_position(spec.product_id)
                sizes[sym] = pos["size"]
                local = self.store.position(self.mode, sym)
                info = {"exchange_size": pos["size"], "journal": None if local is None else
                        (1 if local["side"] == "LONG" else -1) * local["contracts"]}
                if pos["size"] == 0 and local is not None:
                    info["closed_while_away"] = bool(self.engine.sync_exchange_position(sym))
                elif pos["size"] != 0 and local is None:
                    issues.append(f"{sym}: untracked exchange position {pos['size']}")
                elif pos["size"] != 0:
                    if pos["size"] != info["journal"]:  # Source #3: adopt / book the external change first
                        info["synced"] = (self.engine.sync_exchange_position(sym) or {}).get("action")
                        local = self.store.position(self.mode, sym)
                        j = None if local is None else (1 if local["side"] == "LONG" else -1) * local["contracts"]
                        if j != pos["size"]:
                            issues.append(f"{sym}: exchange size {pos['size']} != journal {j}")
                    if local is not None:
                        info.update(self._ensure_protection(sym, spec.product_id, local, orders, issues))
                summary["positions"][sym] = info
            try:  # F07: isolated margin + 10x verified before bot entries; open positions keep their leverage
                pol = self.engine.prepare_account(sizes)
                summary["account_policy"] = {"margin_mode": pol["margin_mode"], "blocked": pol["blocked"]}
            except ExchangeError as exc:
                issues.append(f"account_policy_failed: {exc}")
        cov = self.market.sync(self.symbols)
        summary["data"] = cov
        latest_closed = now - now % 60 - 60
        self.data_blocked = {}
        summary["data_warnings"] = []
        for sym, c in cov.items():
            if any(c["gaps"].values()):  # thin coins have minutes without trades: warn, do not block
                summary["data_warnings"].append(f"{sym}: historical 1m gaps {c['gaps']}")
            if c["last_1m"] < latest_closed - 60:
                why = f"{sym}: history ends {to_utc(c['last_1m'])}, expected >= {to_utc(latest_closed - 60)}"
                if sym in ee.FROZEN_PROFILES:
                    issues.append(why)
                else:  # an added coin with stale data blocks only its own entries
                    self.data_blocked[sym] = why
                    summary["data_warnings"].append(why + " (entries on this symbol blocked)")
        self._catch_up(cov)
        self.reconciliations.append(summary)
        if issues:
            self.transition("DEGRADED", "; ".join(issues))
        elif self.daily_halted:
            self.transition("HALTED", self.engine.halted()[1] or "halt active")
        else:
            self.disconnects_since_ready = 0
            self.transition("OBSERVED_READY", reason)
            for sym in list(self.pending_hour):
                self._try_hour_signal(sym)
        summary["state"] = self.state
        self.store.event("INFO", "readiness", None, {k: v for k, v in summary.items() if k != "data"} | {
            "data": {s: {"last_1m": to_utc(c["last_1m"]), "gaps": c["gaps"]} for s, c in cov.items()}}, self.mode)
        return summary

    def _ref_mark(self, sym: str) -> float | None:
        t = self.ws.last_ticker.get(sym) or {}
        return t.get("mark_price") or self.last_close.get(sym)

    def _ensure_protection(self, sym: str, pid: int, local: Any, orders: list[dict[str, Any]],
                           issues: list[str]) -> dict[str, Any]:
        """F06: a leg counts only if it matches the position: product, opposite side, reduce-only, size == contracts,
        stop on the correct side of the price. A missing leg is attached at most every BRACKET_RETRY_S (an ACK is
        never proof: the readback decides); a leg differing only in size is resized. Unconfirmed protection adds
        'missing_confirmed_protection:{sym}' so the runner stays DEGRADED (no entries)."""
        n = int(local["contracts"])
        ref = self._ref_mark(sym) or float(local["entry_price"])
        want_sl, want_tp = local["sl_price"] is not None, local["tp_price"] is not None

        def match(rows: list[dict[str, Any]]) -> tuple[dict | None, dict | None]:
            return self.engine._match_legs([o for o in rows if int(o.get("product_id") or 0) == int(pid)], pid, local["side"], ref)
        sl, tp = match(orders)
        out: dict[str, Any] = {"sl_leg": sl["id"] if sl else None, "tp_leg": tp["id"] if tp else None}
        reread = False
        if (want_sl and sl is None) or (want_tp and tp is None):
            self.store.event("CRITICAL", "unprotected_position", sym, out, self.mode)
            key = f"{sym}:{local['opened_at_us']}"
            last = self._bracket_attempts.get(key)
            if last is None or self.clock() - last >= BRACKET_RETRY_S:
                self._bracket_attempts[key] = self.clock()
                try:
                    self.client.place_position_bracket(
                        pid, {"order_type": "market_order", "stop_price": local["sl_price"]} if want_sl and sl is None else None,
                        {"order_type": "market_order", "stop_price": local["tp_price"]} if want_tp and tp is None else None,
                        ee.TRIGGER_METHOD)
                    out["bracket_attached"] = True
                except ExchangeError as exc:
                    out["bracket_error"] = str(exc)
            else:
                out["bracket_attach"] = "awaiting readback (not resent)"
            reread = True
        legs = {"sl_order_id": str(sl["id"]) if sl else local["sl_order_id"], "tp_order_id": str(tp["id"]) if tp else local["tp_order_id"]}
        if legs != {"sl_order_id": local["sl_order_id"], "tp_order_id": local["tp_order_id"]}:
            self.store.update_position(self.mode, sym, **legs)
        stale = {str(o["id"]) for o in (sl, tp) if o is not None and int(o.get("size") or 0) != n}
        if stale:
            out["legs_resized"] = self.engine._resize_legs(sym, only=stale)
            reread = True
        if reread:
            sl, tp = match(self.client.get_active_orders(pid))
            out.update(sl_leg=sl["id"] if sl else None, tp_leg=tp["id"] if tp else None)
            found = {k: v for k, v in (("sl_order_id", sl), ("tp_order_id", tp)) if v is not None}
            if found:
                self.store.update_position(self.mode, sym, **{k: str(v["id"]) for k, v in found.items()})
        ok = all(o is not None and int(o.get("size") or 0) == n for want, o in ((want_sl, sl), (want_tp, tp)) if want)
        out["protection_confirmed"] = ok
        if not ok:
            issues.append(f"missing_confirmed_protection:{sym}")
        return out

    def _catch_up(self, cov: dict[str, dict[str, Any]]) -> None:
        """Replay closed minutes missed while away over open positions; queue a missed hour close."""
        for sym, c in cov.items():
            last = self.last_minute.get(sym)
            p = self.store.position(self.mode, sym)
            start = None
            if p is not None:
                start = int(p["last_bar_us"] // 1_000_000)
            if last is not None:
                start = max(start or last, last)
            if start is not None and c["last_1m"] > start:
                for bar_s, o, h, l, cl, fr, mk in self.market.minutes_after(sym, start):
                    if self.store.position(self.mode, sym) is not None:
                        a = self.engine.on_bar(sym, bar_s * 1_000_000, o, h, l, cl, fr, mk)
                        if a:
                            self.actions.append({"symbol": sym, "catch_up": True, **a})
                    self.last_close[sym] = cl
                    if (bar_s + 60) % 3600 == 0 and sym in self.profiles:
                        self.pending_hour[sym] = bar_s + 60
            self.last_minute[sym] = max(self.last_minute.get(sym, 0), c["last_1m"])

    # -- minute / hour processing --------------------------------------------------------- #
    @_locked
    def on_minute_close(self, symbol: str, bar_s: int, o: float, h: float, l: float, c: float, source: str = "ws") -> None:
        if symbol not in self.symbols:
            return
        last = self.last_minute.get(symbol)
        if last is not None and bar_s <= last:
            return
        if last is not None and bar_s > last + 60:
            self.minute_gaps += 1
            self.store.event("WARN", "minute_gap", symbol, {"after": to_utc(last), "got": to_utc(bar_s), "source": source}, self.mode)
        self.last_minute[symbol] = bar_s
        self.last_close[symbol] = c
        self._roll_day(bar_s)
        if self.mode != "paper" and self.store.position(self.mode, symbol) is not None:
            x = self.engine.sync_exchange_position(symbol)  # exits first: never trail a leg that already filled
            if x:
                self.actions.append({"symbol": symbol, "bar": bar_s, "source": source, **x})
        if self.store.position(self.mode, symbol) is not None:
            fr = mk = None
            if bar_s % dm.FUNDING_INTERVAL_SECONDS == 0:
                fr, mk = self.market.funding_inputs(symbol, bar_s)
            a = self.engine.on_bar(symbol, bar_s * 1_000_000, o, h, l, c, fr, mk)
            if a:
                self.actions.append({"symbol": symbol, "bar": bar_s, "source": source, **a})
        if self.mode == "paper" and self.engine.pending_limits(symbol):
            for f in self.engine.check_pending_limits(symbol, bar_s * 1_000_000 + 59_000_000, l, h):
                self.actions.append({"symbol": symbol, "bar": bar_s, "source": source, "limit_fill": True, **f})
        self._risk_checks(bar_s)
        if self.state == "HALTED" and not self.daily_halted:
            self.run_reconciliation("halt expired")
        if (bar_s + 60) % 3600 == 0 and symbol in self.profiles:
            self.pending_hour[symbol] = bar_s + 60
        if symbol in self.pending_hour:
            self._try_hour_signal(symbol)

    def _try_hour_signal(self, symbol: str) -> None:
        hour_close = self.pending_hour[symbol]
        if self.clock() - hour_close > ENTRY_MAX_DELAY_S:
            self.pending_hour.pop(symbol)
            self.store.event("WARN", "signal_window_missed", symbol, {"hour_close": to_utc(hour_close)}, self.mode)
            return
        s = self.market.evaluate(symbol, hour_close)
        if s is None:
            return  # hour not published by REST yet; retried on the next closed minute
        self.pending_hour.pop(symbol)
        self.last_eval[symbol] = s
        if not s["fresh_signal"]:
            return
        rec = {"symbol": symbol, "hour_close": hour_close, "side": s["signal_side"], "state": self.state,
               "halted": self.daily_halted}
        if symbol in getattr(self, "data_blocked", {}):
            rec["reason"] = self.data_blocked[symbol]
            self.blocked_signals.append(rec)
            self.store.event("WARN", "entry_blocked_data", symbol, rec, self.mode)
            return
        if not self.can_enter():
            self.blocked_signals.append(rec)
            self.store.event("WARN", "entry_blocked_state", symbol, rec, self.mode)
            return
        bar_us = int(pd.Timestamp(s["bar_open"]).value // 1000)
        res = self.engine.on_signal(symbol, s["signal"], bar_us, s["atr14"], self._ref_price(symbol, s["signal"]))
        self.entries.append({**rec, "result": res})

    def _ref_price(self, symbol: str, direction: int) -> float:
        q = self.ws.last_quote.get(symbol)
        if q and time.time() - q.get("rx", 0) <= QUOTE_MAX_AGE_S and q.get("best_ask") and q.get("best_bid"):  # rx is wall time
            return q["best_ask"] if direction == 1 else q["best_bid"]
        tk = self.market.ticker(symbol)
        return (tk.get("best_ask") if direction == 1 else tk.get("best_bid")) or tk["mark_price"]

    # -- risk ------------------------------------------------------------------------------ #
    def _day_start_from_journal(self, day: int) -> float:
        midnight_us = day * 86_400 * 1_000_000
        today = sum(float(t["net_pnl_inr"]) for t in self.store.trades(self.mode) if t["closed_at_us"] >= midnight_us)
        return self.engine.realized_equity_inr() - today

    def equity_inr(self) -> float:
        return self.engine.realized_equity_inr() + self.engine.unrealized_inr(self.last_close)

    def _roll_day(self, bar_s: int) -> None:
        """UTC day anchor of the 5% breaker, persisted in system_meta (Source #5): a restart later the same day keeps
        the 00:00 baseline, including the mark-to-market of positions held across midnight."""
        day = bar_s // 86_400
        if day == self.day:
            return
        date = to_utc(day * 86_400)[:10]
        if self.day is None:
            saved_date = self.store.meta_get("utc_day_anchor_date")
            saved_usd = self.store.meta_get("utc_day_start_equity_usd")
            if saved_date == date and saved_usd is not None:
                self.day, self.day_start_equity = day, float(saved_usd) * self.engine.rate
                return
            # no anchor for today: a previous day's anchor means we were down at 00:00 -> mark to market now;
            # no anchor at all (first run) -> realized equity at midnight from the journal
            start = self.equity_inr() if saved_date is not None else self._day_start_from_journal(day)
        else:
            start = self.equity_inr()
        self.day, self.day_start_equity = day, start
        self.store.meta_set(utc_day_anchor_date=date, utc_day_start_equity_usd=start / self.engine.rate)
        self.store.event("INFO", "day_start", None, {"day": to_utc(day * 86_400), "equity_inr": start}, self.mode)

    def loss_streak(self) -> int:
        n = 0
        for t in reversed(self.store.trades(self.mode)):
            if t["closed_at_us"] <= self.streak_floor_us or t["net_pnl_inr"] >= 0:
                break
            n += 1
        return n

    def _risk_checks(self, bar_s: int) -> None:
        if self.daily_halted:
            return
        eq = self.equity_inr()
        limit = self.day_start_equity * (1 - ee.DAILY_LOSS_LIMIT_PCT / 100)
        reason = kind = None
        if eq <= limit:
            kind, reason = "DAILY", f"daily loss limit: equity ₹{eq:,.2f} <= ₹{limit:,.2f} (5% below day start ₹{self.day_start_equity:,.2f})"
        elif (n := self.loss_streak()) >= self.max_consecutive_losses:
            kind, reason = "LOSS_STREAK", f"{n} consecutive losing trades"
        if reason:
            self._trip(kind, reason, bar_s)

    def _trip(self, kind: str, reason: str, bar_s: int) -> None:
        until_us = (bar_s // 86_400 + 1) * 86_400 * 1_000_000
        self.store.set_halt(True, reason, until_us)
        now_us = int(self.clock() * 1_000_000)
        self.engine._event("CRITICAL", "loss_streak_halt" if kind == "LOSS_STREAK" else "daily_loss_halt", None, {"reason": reason})
        if kind == "LOSS_STREAK":
            self.streak_floor_us = now_us
        cancels: dict[str, Any] = {"cancelled_orders": [], "cancel_errors": []}
        self._cancel_entry_orders(cancels)  # F05: nothing resting may reopen exposure after the halt
        flattened = []
        for p in self.store.positions(self.mode):
            sym = p["symbol"]
            r = self.engine.close_position(sym, kind, price=self.last_close.get(sym), bar_us=bar_s * 1_000_000)
            flattened.append({"symbol": sym, "result": (r or {}).get("action")})
        if self.mode != "paper" and self.store.positions(self.mode):
            self.engine.emergency_flatten()
        self.actions.append({"action": "halt", "kind": kind, "reason": reason, "flattened": flattened, **cancels})
        self.transition("HALTED", reason)

    # -- runners ------------------------------------------------------------------------------ #
    def run_once(self) -> dict[str, Any]:
        """One REST reconciliation + signal evaluation (acts only within 5 min of the latest 1h close)."""
        if (why := auto_bot_live_refusal(self.mode)) is not None:
            raise LiveTradingDisabledError(why)
        summary = self.run_reconciliation("cli --once")
        for sym in self.profiles:
            try:
                s = self.market.evaluate(sym)
            except Exception as exc:  # noqa: BLE001 - e.g. an added coin whose history is still downloading
                self.store.event("WARN", "evaluate_failed", sym, f"{type(exc).__name__}: {exc}", self.mode)
                continue
            self.last_eval[sym] = s
            close_s = int(pd.Timestamp(s["bar_close"]).value // 10**9)
            if s["fresh_signal"] and self.clock() - close_s <= ENTRY_MAX_DELAY_S:
                self.pending_hour[sym] = close_s
                self._try_hour_signal(sym)
        return summary

    async def _poller(self, stop: asyncio.Event) -> None:
        """REST fallback: deliver minutes the socket has not delivered within POLL_GRACE_S of their close."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_S)
                break
            except asyncio.TimeoutError:
                pass
            if not self.reconciliations:
                continue
            await asyncio.to_thread(self._poll_once)  # Source #6: REST work off the event loop

    def _poll_once(self) -> None:
        """One REST-poll pass (worker thread): downloads run outside the runner lock, minute processing inside it."""
        now = self.clock()
        latest = int(now) - int(now) % 60 - 60
        for sym in list(self.symbols):
            last = self.last_minute.get(sym)
            if last is None or last >= latest or now - (last + 120) < POLL_GRACE_S:
                continue
            try:
                self.market.sync([sym])
                rows = self.market.minutes_after(sym, last)
                with self._work:
                    for bar_s, o, h, l, c, _, _ in rows:
                        self.on_minute_close(sym, bar_s, o, h, l, c, "rest_poll")
            except Exception as exc:  # noqa: BLE001 - keep polling
                log.warning("REST poll %s failed: %s", sym, exc)

    async def run_async(self, duration: float | None = None) -> None:
        if (why := auto_bot_live_refusal(self.mode)) is not None:
            raise LiveTradingDisabledError(why)
        stop = asyncio.Event()
        self.store.event("INFO", "runner_started", None, {"pid": os.getpid(), "duration": duration}, self.mode)
        self.notifier.notify_bot_lifecycle("Start", self.mode, self.state, {"pid": os.getpid()})
        tasks = [asyncio.create_task(self.ws.run(stop)), asyncio.create_task(self._poller(stop)),
                 asyncio.create_task(self._control_loop(stop))]
        try:
            if duration is not None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=duration)
                except asyncio.TimeoutError:
                    pass
            else:
                await stop.wait()
        finally:
            stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.ws.close_sockets()
            self.store.event("INFO", "runner_stopped", None, {"pid": os.getpid()}, self.mode)
            self.notifier.notify_bot_lifecycle("Stop", self.mode, self.state, "protective exchange orders (if any) stay in place")
            self.notifier.flush(5)

    async def _control_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if await asyncio.to_thread(self._process_commands):  # e.g. a flatten never blocks the socket loop
                stop.set()
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    def _process_commands(self) -> bool:
        """Execute pending control files (worker thread). True when a stop was requested."""
        stop = False
        for cmd in self.control.pending():
            try:
                res = self.handle_command(cmd["cmd"], cmd.get("args") or {})
            except Exception as exc:  # noqa: BLE001 - report, keep running
                res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self.control.complete(cmd["id"], res)
            stop |= cmd["cmd"] == "stop"
        return stop

    @_locked
    def handle_command(self, cmd: str, args: dict[str, Any]) -> dict[str, Any]:
        """Operator commands (dashboard / control files). Protective logic keeps running in every case."""
        if cmd == "pause":
            self.store.set_entries_paused(True)
            self.store.event("INFO", "entries_paused", None, args, self.mode)
            self.notifier.notify_bot_lifecycle("Pause", self.mode, self.state, "new entries paused; stops/exits stay active")
            return {"ok": True, "entries_paused": True}
        if cmd == "resume":
            self.store.set_entries_paused(False)
            self.store.event("INFO", "entries_resumed", None, args, self.mode)
            self.notifier.notify_bot_lifecycle("Resume", self.mode, self.state, "new entries allowed")
            return {"ok": True, "entries_paused": False}
        if cmd == "flatten":
            return {"ok": True, **self.emergency_flatten_all(args.get("reason", "MANUAL_FLATTEN"))}
        if cmd == "stop":
            self.store.event("INFO", "stop_requested", None, args, self.mode)
            return {"ok": True, "stopping": True, "pid": os.getpid()}
        raise ValueError(f"unknown command {cmd!r}")

    def _cancel_entry_orders(self, report: dict[str, Any]) -> None:
        """Cancel every order that could open exposure: resting / partially filled ENTRY intents (paper and exchange)
        and, on the exchange, any other open order that is neither reduce-only nor an SL/TP leg. Fills reported by a
        cancel are booked at once; a refused cancel (already filled) is booked by the reconciliation that follows."""
        for it in self.store.q("SELECT * FROM order_intents WHERE mode=? AND role='ENTRY' AND reduce_only=0 AND state IN "
                               "('ACKNOWLEDGED','PARTIALLY_FILLED') ORDER BY created_at_us", (self.mode,)):
            if self.mode == "paper" or not it["exchange_order_id"]:
                self.store.transition(it["intent_id"], "CANCELLED", "emergency flatten / risk halt")
                report["cancelled_orders"].append(it["intent_id"])
                continue
            try:
                r = self.client.cancel_order(it["product_id"], order_id=it["exchange_order_id"])
                self.engine._apply_order(it["intent_id"], {**r, "state": r.get("state") or "cancelled"})
                report["cancelled_orders"].append(it["intent_id"])
            except ExchangeError as exc:
                report["cancel_errors"].append(f"{it['symbol']} order {it['exchange_order_id']}: {type(exc).__name__}: {exc}")
        if self.mode != "paper":
            for o in self.client.get_active_orders():
                if o.get("reduce_only") or o.get("stop_order_type"):
                    continue
                try:
                    self.client.cancel_order(int(o["product_id"]), order_id=o["id"])
                    report["cancelled_orders"].append(str(o["id"]))
                except ExchangeError as exc:
                    report["cancel_errors"].append(f"{o.get('product_symbol')} order {o['id']}: {type(exc).__name__}: {exc}")

    def _open_entry_orders(self) -> list[dict[str, Any]]:
        out = [{"symbol": i["symbol"], "intent_id": i["intent_id"], "state": i["state"]} for i in self.store.q(
            "SELECT symbol, intent_id, state FROM order_intents WHERE mode=? AND role='ENTRY' AND reduce_only=0 AND state IN "
            "('ACKNOWLEDGED','PARTIALLY_FILLED','DISPATCHING','OUTCOME_UNKNOWN')", (self.mode,))]
        if self.mode != "paper":
            out += [{"symbol": o.get("product_symbol"), "order_id": o.get("id"), "side": o.get("side"),
                     "size": o.get("unfilled_size") or o.get("size")}
                    for o in self.client.get_active_orders() if not o.get("reduce_only") and not o.get("stop_order_type")]
        return out

    @_locked
    def emergency_flatten_all(self, reason: str = "MANUAL_FLATTEN") -> dict[str, Any]:
        """F05: 1) pause new entries, 2) cancel every order that could open exposure, 3) reconcile fills that landed
        before the cancels, 4) close journalled and unjournalled positions (reduce-only, escalating to close_all),
        5) report FLAT only when open positions AND open entry orders are verifiably zero, else FLATTEN_INCOMPLETE."""
        report: dict[str, Any] = {"reason": reason, "closed": [], "errors": [], "residual": [], "close_all": None,
                                  "cancelled_orders": [], "cancel_errors": [], "open_entry_orders": []}
        now_us = int(self.clock() * 1_000_000)
        self.store.set_entries_paused(True)                                   # 1
        report["entries_paused"] = True
        self._cancel_entry_orders(report)                                     # 2
        if self.mode != "paper":                                              # 3
            try:
                report["reconciled"] = [r.get("result") or r.get("action") for r in
                                        self.engine.reconcile_unknown_intents() + self.engine.refresh_resting_orders()]
            except ExchangeError as exc:
                report["errors"].append(f"reconcile: {type(exc).__name__}: {exc}")
        for p in self.store.positions(self.mode):                             # 4
            sym = p["symbol"]
            try:
                price = None
                if self.mode == "paper":
                    tk = self.market.ticker(sym)
                    price = tk.get("best_bid") if p["side"] == "LONG" else tk.get("best_ask")
                    price = price or tk.get("mark_price") or self.last_close.get(sym)
                r = self.engine.close_position(sym, reason, price=price, bar_us=now_us) or {}
                keep = ("side", "contracts", "exit_price", "exit_reason", "net_pnl_inr")
                report["closed"].append({"symbol": sym, **{k: r.get(k) for k in keep}})
            except Exception as exc:  # noqa: BLE001 - keep closing the others
                report["errors"].append(f"{sym}: {type(exc).__name__}: {exc}")
        if self.mode != "paper":
            try:  # unjournalled exchange positions count too
                open_ex = [x for x in self.client.get_margined_positions() if int(x.get("size") or 0)]
            except ExchangeError as exc:
                open_ex = []
                report["errors"].append(f"positions: {type(exc).__name__}: {exc}")
            if open_ex or self.store.positions(self.mode) or report["errors"]:
                r = self.engine.emergency_flatten()
                report["close_all"] = r
                if r.get("status") != "FLAT":
                    report["residual"].extend(r.get("residual") or [])
                for p in self.store.positions(self.mode):  # book what close_all did into the journal
                    self.engine.sync_exchange_position(p["symbol"])
        report["residual"].extend({"symbol": p["symbol"], "contracts": p["contracts"]} for p in self.store.positions(self.mode))
        try:                                                                  # 5
            report["open_entry_orders"] = self._open_entry_orders()
        except ExchangeError as exc:
            report["errors"].append(f"open orders: {type(exc).__name__}: {exc}")
        done = not report["residual"] and not report["open_entry_orders"] and not report["errors"]
        report["status"] = "FLAT" if done else "FLATTEN_INCOMPLETE"
        if not done:
            self.engine._event("CRITICAL", "flatten_incomplete", None,
                               {"reason": reason, "residual": report["residual"], "open_entry_orders": report["open_entry_orders"],
                                "errors": report["errors"]})
        else:
            self.engine._event("WARN", "manual_flatten", None, {"closed": len(report["closed"]),
                                                                 "cancelled_orders": len(report["cancelled_orders"])}, notify=True)
        return report

    def status(self) -> dict[str, Any]:
        eq, avail = self.engine.account_equity_inr() if self.mode == "paper" else (None, None)
        return {"state": self.state, "halted": self.engine.halted(), "equity_inr": eq, "available_inr": avail,
                "positions": [dict(p) for p in self.store.positions(self.mode)], "trades": len(self.store.trades(self.mode)),
                "loss_streak": self.loss_streak(), "ws": self.ws.snapshot()}


def to_utc(s: int | float) -> str:
    return pd.Timestamp(int(s), unit="s", tz="UTC").strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def print_report(bot: LiveBotRunner, console: Console) -> None:
    t = Table(title="State transitions")
    for c in ("time (UTC)", "from", "to", "reason"):
        t.add_column(c)
    for x in bot.transitions:
        t.add_row(pd.Timestamp(x["t"], unit="s", tz="UTC").strftime("%H:%M:%S"), x["from"] or "-", x["to"], x["reason"][:90])
    console.print(t)
    r = bot.reconciliations[-1] if bot.reconciliations else {}
    d = Table(title="Readiness summary")
    for c in ("symbol", "last closed 1m (UTC)", "gaps TRADE/MARK/FUNDING", "rows fetched"):
        d.add_column(c)
    for sym, c in (r.get("data") or {}).items():
        d.add_row(sym, to_utc(c["last_1m"]), "/".join(str(c["gaps"].get(k, "?")) for k in ("TRADE", "MARK", "FUNDING")), str(c["fetched"]))
    console.print(d)
    console.print(f"issues: {r.get('issues') or 'none'}   unknown intents resolved: {len(r.get('unknown_intents') or [])}")
    st = bot.status()
    halted, why = st["halted"]
    console.print(f"[bold]state {st['state']}[/]  halted={halted}{f' ({why})' if why else ''}  mode={bot.mode}")
    if st["equity_inr"] is not None:
        console.print(f"paper wallet: equity ₹{st['equity_inr']:,.2f}  available ₹{st['available_inr']:,.2f}  "
                      f"open positions {len(st['positions'])}  closed trades {st['trades']}  loss streak {st['loss_streak']}")
    s = Table(title="Signal evaluation (latest CLOSED 1h bar)")
    for c in ("symbol", "variant", "1h bar (UTC)", "close", "BB upper", "BB lower", "4h EMA50", "4h EMA200", "ATR14",
              "cost gate", "raw L/S", "signal", "action"):
        s.add_column(c, justify="right")
    for sym, e in bot.last_eval.items():
        act = next((x["result"].get("action") for x in bot.entries if x["symbol"] == sym), None)
        blk = next((f"blocked ({x['state']})" for x in bot.blocked_signals if x["symbol"] == sym), None)
        s.add_row(sym, e["variant"], pd.Timestamp(e["bar_open"]).strftime("%m-%d %H:%M"), f"{e['close']:,.2f}",
                  f"{e['bb_upper']:,.2f}", f"{e['bb_lower']:,.2f}", f"{e['ema50_4h']:,.2f}", f"{e['ema200_4h']:,.2f}",
                  f"{e['atr14']:,.2f}", "PASS" if e["cost_gate_pass"] else "block",
                  f"{int(e['cond_long'])}/{int(e['cond_short'])}", e["signal_side"] or "none", act or blk or "-")
    console.print(s)
    for p in st["positions"]:
        console.print(f"open {p['symbol']} {p['side']} {p['contracts']} @ {p['entry_price']:.4f}  SL {p['sl_price']}  TP {p['tp_price']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Step 6 live bot runner")
    ap.add_argument("--mode", choices=["paper", "testnet", "live"], default=settings.EXECUTION_MODE)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="one reconciliation + evaluation cycle")
    g.add_argument("--duration", type=float, help="run the WebSocket + REST-poll loop for N seconds")
    g.add_argument("--loop", action="store_true", help="run until Ctrl+C or a dashboard stop command")
    ap.add_argument("--db-path", help=argparse.SUPPRESS)
    ap.add_argument("--lock-path", help=argparse.SUPPRESS)
    ap.add_argument("--control-dir", help=argparse.SUPPRESS)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = Console(width=200)
    console.print(f"live_bot: mode={args.mode} MODE={settings.MODE!r} LIVE_TRADING_ENABLED={settings.LIVE_TRADING_ENABLED}")
    if (why := auto_bot_live_refusal(args.mode)) is not None:
        console.print(f"[red]refused: {why}[/]")
        return 3
    if args.mode == "testnet" and not (settings.TESTNET_API_KEY and settings.TESTNET_API_SECRET):
        console.print("[red]testnet mode needs DELTA_TESTNET_API_KEY / DELTA_TESTNET_API_SECRET in .env[/]")
        return 2
    try:
        bot = LiveBotRunner(args.mode, profiles=ee.bot_profiles(), store_path=Path(args.db_path) if args.db_path else None,
                            lock_path=Path(args.lock_path) if args.lock_path else LOCK_PATH,
                            control_dir=Path(args.control_dir) if args.control_dir else None)
    except (BotAlreadyRunningError, LiveTradingDisabledError) as exc:
        console.print(f"[red]{type(exc).__name__}: {exc}[/]")
        return 3
    try:
        if args.once or (args.duration is None and not args.loop):
            bot.run_once()
        else:
            try:
                asyncio.run(bot.run_async(None if args.loop else args.duration))
            except KeyboardInterrupt:
                console.print("stopping (Ctrl+C)")
        print_report(bot, console)
        return 0 if bot.state in ("OBSERVED_READY", "HALTED") else 1
    finally:
        bot.close()


if __name__ == "__main__":
    sys.exit(main())
