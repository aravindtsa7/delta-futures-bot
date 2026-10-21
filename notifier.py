"""Non-blocking Telegram alerts (Step 7).

Rules
  * Never raises into the caller and never waits on the network: messages go to a bounded queue drained
    by one daemon thread; a send failure (no credentials, HTTP 4xx/5xx, timeout) is logged and recorded.
  * Everything is redacted BEFORE it is queued or logged: configured API keys/secrets and the bot token,
    sensitive dict keys (api_key, secret, signature, token, password ...), "secret=..." style pairs and
    long hex strings (HMAC signatures).
  * Without TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID every message is recorded as dry_run_no_credentials.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections import deque
from typing import Any, Iterable

import httpx

from config import settings

log = logging.getLogger(__name__)

REDACTED = "[REDACTED]"
SENSITIVE_KEYS = re.compile(r"(api[-_ ]?key|api[-_ ]?secret|secret|signature|token|password|passphrase|private[-_ ]?key)", re.I)
PAIR_RE = re.compile(r"(?i)\b(api[-_ ]?key|api[-_ ]?secret|secret|signature|token|password)(\s*[:=]\s*|\s+)([\"']?)([^\s\"',}]+)")
HEX_RE = re.compile(r"\b[0-9a-fA-F]{40,}\b")
BOT_URL_RE = re.compile(r"/bot[0-9]+:[A-Za-z0-9_-]+")
QUEUE_MAX = 1000
TELEGRAM_API = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None, *, mode: str = "paper",
                 transport: httpx.BaseTransport | None = None, timeout: float = 5.0,
                 extra_secrets: Iterable[str] = (), background: bool = True):
        self.token = token or ""
        self.chat_id = str(chat_id or "")
        self.mode = mode
        self.timeout = timeout
        self.background = background
        self._transport = transport
        self._secrets = sorted({s for s in (self.token, settings.TESTNET_API_KEY, settings.TESTNET_API_SECRET,
                                            settings.LIVE_API_KEY, settings.LIVE_API_SECRET, *extra_secrets)
                                if s and len(s) >= 6}, key=len, reverse=True)
        self.history: deque[dict[str, Any]] = deque(maxlen=500)
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.dropped = 0

    @classmethod
    def from_settings(cls, mode: str = "paper", **kw: Any) -> "TelegramNotifier":
        return cls(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID, mode=mode, **kw)

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    # -- redaction ------------------------------------------------------------------- #
    def redact(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: (REDACTED if isinstance(k, str) and SENSITIVE_KEYS.search(k) else self.redact(v)) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return type(obj)(self.redact(v) for v in obj)
        if not isinstance(obj, str):
            return obj
        s = obj
        for secret in self._secrets:
            s = s.replace(secret, REDACTED)
        s = BOT_URL_RE.sub("/bot" + REDACTED, s)
        s = PAIR_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}", s)
        return HEX_RE.sub(REDACTED, s)

    # -- core ------------------------------------------------------------------------------ #
    def _submit(self, kind: str, ref: str, text: str) -> dict[str, Any]:
        """Record + enqueue; returns the record. Never raises."""
        rec: dict[str, Any] = {"kind": kind, "ref": ref, "at": time.time(), "status": "pending", "error": None}
        try:
            rec["text"] = self.redact(text)
            if not self.configured:
                rec["status"] = "dry_run_no_credentials"
                log.info("telegram (dry run) %s %s: %s", kind, ref, rec["text"].replace("\n", " | "))
            elif not self.background:
                self._send(rec)
            else:
                self._ensure_thread()
                try:
                    self._q.put_nowait(rec)
                    rec["status"] = "queued"
                except queue.Full:
                    self.dropped += 1
                    rec["status"] = "dropped_queue_full"
                    log.warning("telegram queue full; dropped %s %s", kind, ref)
        except Exception as exc:  # noqa: BLE001 - alerts must never break trading
            rec["status"], rec["error"] = "failed", self.redact(f"{type(exc).__name__}: {exc}")
            log.warning("telegram submit failed: %s", rec["error"])
        self.history.append(rec)
        return rec

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, name="telegram-notifier", daemon=True)
                self._thread.start()

    def _worker(self) -> None:
        while True:
            rec = self._q.get()
            try:
                self._send(rec)
            finally:
                self._q.task_done()

    def _send(self, rec: dict[str, Any]) -> None:
        try:
            with httpx.Client(transport=self._transport, timeout=self.timeout) as c:
                r = c.post(f"{TELEGRAM_API}/bot{self.token}/sendMessage",
                           json={"chat_id": self.chat_id, "text": rec["text"], "disable_web_page_preview": True})
            if r.status_code == 200 and (r.json() or {}).get("ok", False):
                rec["status"] = "sent"
            else:
                rec["status"], rec["error"] = "failed", f"HTTP {r.status_code}: {self.redact(r.text[:200])}"
        except Exception as exc:  # noqa: BLE001 - timeouts, DNS, TLS, bad JSON ...
            rec["status"], rec["error"] = "failed", self.redact(f"{type(exc).__name__}: {exc}")
        if rec["status"] != "sent":
            log.warning("telegram %s %s not sent: %s", rec["kind"], rec["ref"], rec["error"])

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait (tests / shutdown only) until queued messages are processed."""
        end = time.time() + timeout
        while time.time() < end:
            if self._q.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return False

    # -- message builders -------------------------------------------------------------- #
    def _tag(self) -> str:
        return f"[{self.mode.upper()}]"

    def notify_bot_lifecycle(self, event: str, mode: str | None = None, state: str | None = None, detail: Any = None) -> dict[str, Any]:
        try:
            m = (mode or self.mode).upper()
            ref = f"BOT-{event.upper()}-{int(time.time())}"
            text = f"[{m}] BOT {event.upper()}  #{ref}\nState: {state or '-'}" + (f"\n{_fmt_detail(detail)}" if detail else "")
            return self._submit("lifecycle", ref, text)
        except Exception as exc:  # noqa: BLE001
            return self._fail("lifecycle", exc)

    def notify_trade_entry(self, trade_intent: Any, position: Any) -> dict[str, Any]:
        try:
            it, p = dict(trade_intent), dict(position)
            qty = p["contracts"] * p["contract_value"]
            e = float(p["entry_price"])
            sl = None if p.get("sl_price") is None else float(p["sl_price"])
            tp = None if p.get("tp_price") is None else float(p["tp_price"])
            fee = settings.TAKER_FEE * (1 + settings.GST_ON_FEES)
            risk = (f"₹{qty * (abs(e - sl) + (e + sl) * fee) * settings.FIXED_USD_INR_RATE:,.2f}" if sl is not None
                    else "unbounded (no stop-loss)")
            ref = it.get("intent_id") or it.get("client_order_id")
            lev = f" {float(p['leverage']):g}x" if p.get("leverage") else ""
            src = " (manual)" if p.get("source") == "manual" else ""
            text = (f"{self._tag()} ENTRY {p['symbol']} {p['side']}{lev}{src}  #{ref}\n"
                    f"Contracts: {p['contracts']} @ {e:,.4f}\n"
                    f"SL {'-' if sl is None else f'{sl:,.2f}'} | TP {'-' if tp is None else f'{tp:,.2f}'} | "
                    f"Trail {float(p.get('trail_amount') or 0):,.2f}\n"
                    f"Risk: {risk}" + (f"\nSL leg {p['sl_order_id']} / TP leg {p['tp_order_id']}" if p.get("sl_order_id") else ""))
            return self._submit("entry", str(ref), text)
        except Exception as exc:  # noqa: BLE001
            return self._fail("entry", exc)

    def notify_trade_exit(self, closed_trade: Any) -> dict[str, Any]:
        try:
            t = dict(closed_trade)
            ref = f"T{t.get('trade_id', '?')}"
            text = (f"{self._tag()} EXIT {t['symbol']} {t['side']} ({t['exit_reason']})  #{ref}\n"
                    f"{t['contracts']} @ {float(t['entry_price']):,.4f} -> {float(t['exit_price']):,.4f}\n"
                    f"Gross ₹{t['gross_pnl_inr']:,.2f} | Fees+GST ₹{t['fees_gst_inr']:,.2f} | Funding ₹{t['funding_inr']:,.2f}\n"
                    f"Net ₹{t['net_pnl_inr']:,.2f} | Equity ₹{t['equity_after_inr']:,.2f}")
            return self._submit("exit", ref, text)
        except Exception as exc:  # noqa: BLE001
            return self._fail("exit", exc)

    def notify_safety_incident(self, incident_id: str, level: str, kind: str, detail: Any = None) -> dict[str, Any]:
        try:
            label = INCIDENT_LABELS.get(kind, kind)
            text = f"{self._tag()} {level} {label}  #{incident_id}" + (f"\n{_fmt_detail(detail)}" if detail else "")
            return self._submit("incident", str(incident_id), text)
        except Exception as exc:  # noqa: BLE001
            return self._fail("incident", exc)

    def send_test(self) -> dict[str, Any]:
        """Synchronous test message (dashboard button); still never raises."""
        rec: dict[str, Any] = {"kind": "test", "ref": f"TEST-{int(time.time())}", "at": time.time(), "error": None,
                               "text": f"{self._tag()} Delta futures bot: Telegram alerts are working."}
        if not self.configured:
            rec["status"] = "dry_run_no_credentials"
        else:
            self._send(rec)
        self.history.append(rec)
        return rec

    def _fail(self, kind: str, exc: Exception) -> dict[str, Any]:
        rec = {"kind": kind, "ref": None, "at": time.time(), "status": "failed",
               "error": self.redact(f"format error {type(exc).__name__}: {exc}")}
        log.warning("telegram %s: %s", kind, rec["error"])
        self.history.append(rec)
        return rec


INCIDENT_LABELS = {
    "dispatch_outcome_unknown": "OUTCOME_UNKNOWN order",
    "residual_exposure": "RESIDUAL_EXPOSURE after flatten",
    "daily_loss_halt": "5% Daily Loss Breaker",
    "loss_streak_halt": "4-Loss Streak Halt",
    "sequence_anomaly": "Sequence Gap/Reset",
    "unprotected_position": "Unprotected position (bracket missing)",
    "price_mismatch": "Order price mismatch",
    "close_incomplete": "Reduce-only close incomplete",
    "flat_without_exit_fill": "Flat without exit fill",
    "bracket_attach_failed": "Bracket attach failed",
}


def _fmt_detail(detail: Any) -> str:
    if isinstance(detail, dict):
        return "\n".join(f"{k}: {v}" for k, v in detail.items())
    return str(detail)


_default: TelegramNotifier | None = None


def default_notifier(mode: str = "paper") -> TelegramNotifier:
    global _default
    if _default is None:
        _default = TelegramNotifier.from_settings(mode)
    return _default
