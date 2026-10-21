"""Step 7 verification: alerts, backups, bot operations API, full-system regression.

  1. TelegramNotifier: message formats, secret redaction, non-blocking survival (timeout / HTTP 500),
     wiring into execution_engine.py and live_bot.py
  2. backup_state.py: online WAL backup of a live-writer journal, integrity check, restore refused while
     the bot lock / writer lock is held, clean restore afterwards
  3. /api/bot/* via FastAPI TestClient (isolated files under data/step7_test/), incl. a real
     live_bot.py --loop subprocess start -> pause/resume/flatten via control channel -> stop
  4. Regression: step6_verify.py (which re-runs step5_verify.py and step4_verify.py)

Usage:
    python step7_verify.py [--skip-regression]
"""

from __future__ import annotations

import testenv  # noqa: F401,E402  (must run before config is imported: research mode, no live keys)

import argparse
import json
import os
import shutil
import sqlite3
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
import backup_state  # noqa: E402
import bot_ops  # noqa: E402
import execution_engine as ee  # noqa: E402
import live_bot as lb  # noqa: E402
from config import PROJECT_ROOT, settings  # noqa: E402
from notifier import REDACTED, TelegramNotifier  # noqa: E402
from step5_verify import FakeClock, raises, us  # noqa: E402
from step6_verify import FakeMarket, ts  # noqa: E402

console = Console(width=200)
CHECKS: list[tuple[str, str, bool, str]] = []
T = settings.data_path / "step7_test"


def check(section: str, name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((section, name, bool(ok), detail))
    console.print(f"  {'[green]PASS[/]' if ok else '[red]FAIL[/]'} {name}" + (f"  [dim]{detail}[/]" if detail else ""))
    return bool(ok)


def reset_dir() -> None:
    if T.exists():
        shutil.rmtree(T)
    T.mkdir(parents=True)


ENTRY_INTENT = {"intent_id": "in-0123456789abcdef", "client_order_id": "s5pEETHabc"}
POSITION = {"symbol": "ETHUSD", "side": "LONG", "contracts": 15, "contract_value": 0.01, "entry_price": 2392.8785,
            "sl_price": 2359.45, "tp_price": 2476.5, "trail_amount": 41.8, "sl_order_id": None, "tp_order_id": None}
CLOSED = {"trade_id": 7, "symbol": "ETHUSD", "side": "LONG", "contracts": 15, "entry_price": 2392.8785, "exit_price": 2400.27,
          "exit_reason": "TSL", "gross_pnl_inr": 94.24, "fees_gst_inr": 36.06, "funding_inr": -3.07, "net_pnl_inr": 55.11,
          "equity_after_inr": 50055.11}


# --------------------------------------------------------------------------- #
# 1. Notifier
# --------------------------------------------------------------------------- #
def test_notifier() -> None:
    console.rule("[bold]1. TelegramNotifier: formats, redaction, non-blocking, wiring")
    n = TelegramNotifier(None, None, mode="paper")
    e = n.notify_trade_entry(ENTRY_INTENT, POSITION)
    x = n.notify_trade_exit(CLOSED)
    i = n.notify_safety_incident("INC-000042", "CRITICAL", "daily_loss_halt", {"reason": "equity -5.2%"})
    life = n.notify_bot_lifecycle("Start", "paper", "BOOTING", {"pid": 1234})
    console.print("  [dim]" + "\n  ".join((r["text"] or "").replace("\n", " | ") for r in (e, x, i, life)) + "[/]")
    check("1 notifier", "entry message: symbol, side, contracts, entry, SL, TP, trail, risk ₹, stable intent id",
          all(s in e["text"] for s in ("ENTRY ETHUSD LONG", "#in-0123456789abcdef", "Contracts: 15 @ 2,392.8785", "SL 2,359.45",
                                       "TP 2,476.50", "Trail 41.80", "Risk: ₹")) and e["status"] == "dry_run_no_credentials")
    check("1 notifier", "exit message: reason, gross, fees+GST, funding, net, equity, stable trade id",
          all(s in x["text"] for s in ("EXIT ETHUSD LONG (TSL)", "#T7", "Gross ₹94.24", "Fees+GST ₹36.06", "Funding ₹-3.07",
                                       "Net ₹55.11", "Equity ₹50,055.11")))
    check("1 notifier", "incident + lifecycle messages (5% Daily Loss Breaker, incident id)",
          "5% Daily Loss Breaker" in i["text"] and "#INC-000042" in i["text"] and "BOT START" in life["text"])
    secret, key, sig = "Sup3rS3cretValue_9f8e7d", "AKEYabc123456", "ad767fead0bdbe91ba1e4feb142079245fecd66aa5e47a70b40ba1a4c9b4e3db"
    nr = TelegramNotifier(None, None, extra_secrets=[key, secret])
    r = nr.notify_safety_incident("INC-1", "CRITICAL", "dispatch_outcome_unknown",
                                  {"api_secret": secret, "api-key": key, "note": f"signed with key {key} sig={sig}", "signature": sig})
    leaked = [s for s in (secret, key, sig) if s in r["text"]]
    check("1 notifier", "secrets in a detail dict / free text are masked as [REDACTED] before sending or logging",
          not leaked and r["text"].count(REDACTED) >= 4, r["text"].replace("\n", " | "))
    free = TelegramNotifier(None, None).redact(f"error calling https://api.telegram.org/bot123456:ABCdefGHI_jkl/sendMessage api_secret={secret}")
    check("1 notifier", "bot token in URLs and key=value secrets redacted from error strings",
          "ABCdefGHI" not in free and secret not in free, free)

    calls: list[httpx.Request] = []

    def slow500(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        time.sleep(1.0)
        return httpx.Response(500, json={"ok": False, "description": "boom"})

    def timeout(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated telegram timeout", request=req)

    def ok(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    tok = "123456789:FAKE_token-abcdefghijklmnop"
    n500 = TelegramNotifier(tok, "42", transport=httpx.MockTransport(slow500))
    t0 = time.perf_counter()
    r1 = n500.notify_trade_exit(CLOSED)
    r2 = n500.notify_safety_incident("INC-2", "CRITICAL", "residual_exposure", {"residual": [{"product_id": 3136, "size": -10}]})
    dt = (time.perf_counter() - t0) * 1000
    check("1 notifier", "configured notifier returns immediately even when Telegram takes 1s and answers HTTP 500",
          dt < 50 and r1["status"] == "queued", f"2 calls in {dt:.1f} ms")
    n500.flush(10)
    check("1 notifier", "HTTP 500 recorded as failed, logged locally, no exception", r1["status"] == "failed" and r2["status"] == "failed"
          and "HTTP 500" in (r1["error"] or "") and tok not in json.dumps(list(n500.history)), r1["error"])
    nto = TelegramNotifier(tok, "42", transport=httpx.MockTransport(timeout))
    r3 = nto.notify_bot_lifecycle("Halted", "paper", "HALTED", "daily loss")
    nto.flush(10)
    check("1 notifier", "network timeout recorded as failed, no exception, token not in the error",
          r3["status"] == "failed" and "ReadTimeout" in (r3["error"] or "") and "FAKE_token" not in (r3["error"] or ""), r3["error"])
    nok = TelegramNotifier(tok, "42", transport=httpx.MockTransport(ok))
    r4 = nok.notify_trade_entry(ENTRY_INTENT, POSITION)
    nok.flush(10)
    body = json.loads(calls[-1].content) if calls else {}
    check("1 notifier", "successful send: POST /bot<token>/sendMessage with chat_id + text", r4["status"] == "sent"
          and calls[-1].url.path == f"/bot{tok}/sendMessage" and body.get("chat_id") == "42" and "ENTRY ETHUSD" in body.get("text", ""))
    bad = nok.notify_trade_entry({}, {"symbol": "X"})
    check("1 notifier", "malformed input -> failed record, never an exception", bad["status"] == "failed", bad["error"])

    # wiring: engine + bot with a dry-run capturing notifier
    db = T / "notify_engine.db"
    clock = FakeClock(us("2026-10-20 07:00:00"))
    cap = TelegramNotifier(None, None, mode="paper")
    store = ee.StateStore(db, clock=clock)
    eng = ee.ExecutionEngine(store, "paper", clock=clock, notifier=cap)
    eng.on_signal("ETHUSD", 1, us("2026-10-20 06:00"), 20.0, 2400.0)
    clock.t = us("2026-10-20 07:01:00")
    eng.on_bar("ETHUSD", us("2026-10-20 07:00"), 2399, 2400, 2340, 2345)
    eng._event("CRITICAL", "dispatch_outcome_unknown", "ETHUSD", {"client_order_id": "abc"})
    kinds = [h["kind"] for h in cap.history]
    check("1 notifier", "execution_engine wiring: paper entry, SL exit and CRITICAL incident each produce an alert",
          kinds == ["entry", "exit", "incident"] and "(SL)" in cap.history[1]["text"] and "OUTCOME_UNKNOWN" in cap.history[2]["text"],
          f"{kinds}")
    store.close()
    capb = TelegramNotifier(None, None, mode="paper")
    fc = FakeClock(us("2026-10-20 05:00:30"))
    bot = lb.LiveBotRunner("paper", market=FakeMarket(fc.seconds, {}, {"ETHUSD": 2400.0, "BTCUSD": 81_600.0}),
                           store_path=T / "notify_bot.db", lock_path=T / "notify.lock", control_dir=T / "notify_ctl",
                           clock=fc.seconds, notifier=capb)
    try:
        bot.run_reconciliation("test")
        bot.on_sequence_anomaly({"channel": "orders", "symbol": "ETHUSD", "prev_seq": 2, "seq": 5, "kind": "gap"})
        bot.handle_command("pause", {})
        bot.handle_command("resume", {})
        txt = [h["text"].split("\n")[0] for h in capb.history]
        check("1 notifier", "live_bot wiring: Reconciling / Ready / Sequence Gap / Pause / Resume alerts",
              any("RECONCILING" in t for t in txt) and any("READY" in t for t in txt) and any("Sequence Gap/Reset" in t for t in txt)
              and any("PAUSE" in t for t in txt) and any("RESUME" in t for t in txt), " · ".join(txt))
    finally:
        bot.close()


# --------------------------------------------------------------------------- #
# 2. Backups
# --------------------------------------------------------------------------- #
def test_backup() -> None:
    console.rule("[bold]2. backup_state.py: online WAL backup, verify, guarded restore")
    src = T / "live_state.db"
    clock = FakeClock(us("2026-10-20 05:00:00"))
    store = ee.StateStore(src, clock=clock)
    eng = ee.ExecutionEngine(store, "paper", clock=clock, notifier=TelegramNotifier(None, None))
    for k in range(3):
        h = 6 + 3 * k
        clock.t = us(f"2026-10-20 {h:02d}:00:02")
        eng.on_signal("ETHUSD", 1, us(f"2026-10-20 {h - 1:02d}:00"), 20.0, 2400.0)
        clock.t = us(f"2026-10-20 {h:02d}:01:02")
        eng.on_bar("ETHUSD", us(f"2026-10-20 {h:02d}:00"), 2399, 2400, 2340, 2345)
    n_trades = len(store.trades("paper"))
    wal = src.with_name(src.name + "-wal")
    wal_bytes = wal.stat().st_size if wal.exists() else 0
    naive = T / "naive_copy.db"
    shutil.copy(src, naive)  # what NOT to do: main file only, WAL ignored
    nv = backup_state.verify(naive)
    check("2 backup", "writer still open, WAL holds the recent commits; a naive .db-only copy loses them",
          wal_bytes > 0 and n_trades == 3 and (not nv["ok"] or nv["row_counts"].get("trade_history", 0) < n_trades),
          f"WAL {wal_bytes:,} bytes; naive copy: {nv.get('missing_tables') or nv.get('row_counts')}")
    man = backup_state.backup(src, T / "backups")
    bfile = Path(man["backup"])
    check("2 backup", "online backup while the writer is active: PRAGMA integrity_check ok, all rows present",
          man["verify"]["ok"] and man["verify"]["integrity"] == ["ok"] and man["verify"]["row_counts"]["trade_history"] == 3
          and man["verify"]["row_counts"]["order_intents"] == 3 and bfile.exists() and Path(man["manifest"]).exists()
          and bfile.name.startswith("live_state_") and bfile.name.endswith("Z.db"),
          f"{bfile.name} {man['verify']['row_counts']} sha256 {man['sha256'][:12]}…")
    p = subprocess.run([sys.executable, "backup_state.py", "verify", str(bfile)], capture_output=True, text=True, cwd=PROJECT_ROOT)
    out = json.loads(p.stdout or "{}")
    check("2 backup", "CLI 'verify <file>': integrity ok + expected tables present", p.returncode == 0 and out.get("ok")
          and not out.get("missing_tables"), f"exit {p.returncode}, tables {sorted(out.get('row_counts', {}))}")
    ok, det = raises(backup_state.RestoreRefusedError, backup_state.restore, bfile, src, T / "unused.lock")
    check("2 backup", "restore onto a journal whose writer is open is refused", ok, det)
    store.close()
    lock_path = T / "live_bot.lock"
    holder = lb.InstanceLock(lock_path, "paper")
    target = T / "restored_state.db"
    try:
        ok, det = raises(backup_state.RestoreRefusedError, backup_state.restore, bfile, target, lock_path)
        check("2 backup", "restore refused while live_bot.lock is held by an active runner", ok and not target.exists(), det)
    finally:
        holder.release()
    r1 = backup_state.restore(bfile, target, lock_path)
    r2 = backup_state.restore(bfile, target, lock_path)
    check("2 backup", "after lock release: atomic restore, integrity ok, rows intact, previous copy kept",
          r1["verify"]["ok"] and r1["verify"]["row_counts"]["trade_history"] == 3 and r2["previous_saved_as"]
          and Path(r2["previous_saved_as"]).exists(), f"{target.name} {r1['verify']['row_counts']}")
    s2 = ee.StateStore(target)
    try:
        check("2 backup", "restored journal opens as a normal writer (WAL re-enabled, paper equity reloads)",
              s2.journal_mode.lower() == "wal" and len(s2.trades("paper")) == 3)
    finally:
        s2.close()


# --------------------------------------------------------------------------- #
# 3. API
# --------------------------------------------------------------------------- #
def test_api() -> None:
    console.rule("[bold]3. /api/bot/* endpoints (isolated files in data/step7_test/)")
    api_db = T / "api_bot_state.db"
    bot_ops.configure(db=api_db, lock=T / "api_live_bot.lock", control=T / "api_control", backups=T / "api_backups",
                      logs=T / "api_logs")
    c = TestClient(webapp.app)
    r = c.get("/api/bot/status")
    s = r.json()
    check("3 api", "GET /api/bot/status before any run: 200, no journal, signals for BTCUSD (D) and ETHUSD (C)",
          r.status_code == 200 and not s["db_exists"] and s["readiness_state"] == "NO_JOURNAL"
          and s["signals"]["BTCUSD"]["variant"] == "D_both" and s["signals"]["ETHUSD"]["variant"] == "C_costgate")
    t0 = time.time()
    r = c.post("/api/bot/step")
    st = r.json()
    check("3 api", "POST /api/bot/step: one reconciliation + evaluation cycle -> OBSERVED_READY", r.status_code == 200
          and st["state"] == "OBSERVED_READY" and {"BTCUSD", "ETHUSD"} <= set(st["evaluations"]) and not st["summary"]["issues"],
          f"{time.time() - t0:.1f}s; {', '.join(f'{k} {v['signal_side'] or 'no signal'}' for k, v in st['evaluations'].items())}")
    s = c.get("/api/bot/status").json()
    w = s["wallet"]
    check("3 api", "status after the cycle: journal, wallet telemetry, last reconcile, runner stopped",
          s["db_exists"] and s["readiness_state"] == "STOPPED" and s["last_known_state"] == "OBSERVED_READY"
          and s["last_reconciled_at"] and w["initial_capital_inr"] == 50_000 and w["daily_loss_limit_pct"] == 5.0
          and abs(w["total_equity_inr"] - 50_000) < 1e-9 and not s["runner"]["running"],
          f"equity ₹{w['total_equity_inr']:,.2f}, today {w['today_pnl_pct']:.2f}%")
    r = c.post("/api/bot/pause")
    p1 = c.get("/api/bot/status").json()["entries_paused"]
    r2 = c.post("/api/bot/resume")
    p2 = c.get("/api/bot/status").json()["entries_paused"]
    check("3 api", "POST /api/bot/pause and /resume toggle entries_paused in bot_config_state",
          r.status_code == 200 and r2.status_code == 200 and p1 is True and p2 is False and r.json()["via"] == "journal")

    # simulated open paper position, then flatten
    sig = s["signals"]["ETHUSD"]
    store = ee.StateStore(api_db)
    eng = ee.ExecutionEngine(store, "paper", notifier=bot_ops.notifier())
    tk = bot_ops._ticker("ETHUSD")
    now_us = int(time.time() * 1e6)
    res = eng.on_signal("ETHUSD", 1, now_us - now_us % 3_600_000_000 - 3_600_000_000, sig["atr14"], tk["best_ask"])
    store.close()
    s = c.get("/api/bot/status").json()
    pos = s["positions"][0] if s["positions"] else {}
    check("3 api", "status shows the simulated paper position with mark, unrealized ₹, SL/TP/trail",
          res["action"] == "filled" and len(s["positions"]) == 1 and pos.get("mark_price") and pos.get("unrealized_pnl_inr") is not None
          and pos["sl_price"] < pos["entry_price"] < pos["tp_price"],
          f"{pos.get('contracts')} lots @ {pos.get('entry_price', 0):.2f}, mark {pos.get('mark_price')}, uPnL ₹{pos.get('unrealized_pnl_inr') or 0:.2f}")
    n_hist = len(bot_ops.notifier().history)
    r = c.post("/api/bot/flatten")
    f = r.json()
    s = c.get("/api/bot/status").json()
    new_alerts = [h["kind"] for h in list(bot_ops.notifier().history)[n_hist:]]
    check("3 api", "POST /api/bot/flatten: position closed (MANUAL_FLATTEN), FLAT report, entries paused",
          r.status_code == 200 and f["status"] == "FLAT" and f["closed"][0]["symbol"] == "ETHUSD"
          and f["closed"][0]["exit_reason"] == "MANUAL_FLATTEN" and f["entries_paused"] and not s["positions"]
          and s["entries_paused"] and s["trades"][0]["exit_reason"] == "MANUAL_FLATTEN",
          f"exit {f['closed'][0].get('exit_price')} net ₹{f['closed'][0].get('net_pnl_inr') or 0:.2f}; alerts {new_alerts}")
    c.post("/api/bot/resume")
    r = c.post("/api/bot/backup")
    b = r.json()
    check("3 api", "POST /api/bot/backup: verified snapshot of the journal", r.status_code == 200 and b["verify"]["ok"]
          and b["verify"]["row_counts"]["trade_history"] == 1 and Path(b["backup"]).exists(), Path(b["backup"]).name)
    r = c.post("/api/bot/test-telegram")
    tg = r.json()
    expect = "sent" if (settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID) else "dry_run_no_credentials"
    check("3 api", "POST /api/bot/test-telegram", r.status_code == 200 and tg["status"] == expect, tg["status"])
    r = c.post("/api/bot/start", json={"mode": "live"})
    check("3 api", "POST /api/bot/start mode=live refused (403) while MODE='research'", r.status_code == 403, r.json()["detail"])
    if not settings.TESTNET_API_KEY:
        r = c.post("/api/bot/start", json={"mode": "testnet"})
        check("3 api", "POST /api/bot/start mode=testnet without keys refused (400)", r.status_code == 400, r.json()["detail"])

    # managed subprocess lifecycle
    r = c.post("/api/bot/start", json={"mode": "paper"})
    sr = r.json()
    check("3 api", "POST /api/bot/start: live_bot.py --mode paper --loop spawned, lock acquired", r.status_code == 200
          and sr["started"] and sr["pid"], f"pid {sr.get('pid')} · {sr.get('command')}")
    ready = None
    t0 = time.time()
    while time.time() - t0 < 120:
        s = c.get("/api/bot/status").json()
        if s["runner"]["running"] and s["readiness_state"] == "OBSERVED_READY" and (s["ws_generation"] or 0) >= 1:
            ready = s
            break
        time.sleep(1)
    check("3 api", "running loop reaches OBSERVED_READY over the live public WebSocket (status: pid, mode, ws generation)",
          ready is not None and ready["runner"]["pid"] == sr["pid"] and ready["runner"]["mode"] == "paper"
          and (ready["ws_generation"] or 0) >= 1, f"{time.time() - t0:.0f}s, ws generation {ready and ready['ws_generation']}")
    r1, r2 = c.post("/api/bot/start", json={"mode": "paper"}), c.post("/api/bot/step")
    check("3 api", "while running: second start and run-1-cycle refused (409, lock held)", r1.status_code == 409 and r2.status_code == 409)
    rp = c.post("/api/bot/pause").json()
    paused = c.get("/api/bot/status").json()["entries_paused"]
    rr = c.post("/api/bot/resume").json()
    check("3 api", "pause/resume while running go through the control channel and the bot applies them",
          rp.get("via") == "control_channel" and rp.get("entries_paused") is True and paused is True
          and rr.get("entries_paused") is False and not c.get("/api/bot/status").json()["entries_paused"])
    rf = c.post("/api/bot/flatten").json()
    c.post("/api/bot/resume")
    check("3 api", "flatten while running executes inside the bot process (nothing open -> FLAT)",
          rf.get("via") == "control_channel" and rf.get("status") == "FLAT")
    ok, det = raises(backup_state.RestoreRefusedError, backup_state.restore, Path(b["backup"]), api_db, T / "api_live_bot.lock")
    check("3 api", "restore refused while the dashboard-started bot holds the lock", ok, det)
    r = c.post("/api/bot/stop")
    so = r.json()
    s = c.get("/api/bot/status").json()
    check("3 api", "POST /api/bot/stop: clean shutdown via control channel, exit 0, lock released",
          r.status_code == 200 and so["stopped"] and so["lock_released"] and not so["forced_terminate"] and so["exit_code"] == 0
          and not s["runner"]["running"], json.dumps({k: so[k] for k in ("pid", "exit_code", "forced_terminate", "lock_released")}))
    log_tail = Path(sr["log_file"]).read_text(encoding="utf-8", errors="replace").splitlines()[-4:]
    console.print("  [dim]bot log tail: " + " | ".join(log_tail)[:600] + "[/]")
    ev = [e["kind"] for e in s["events"]]
    check("3 api", "journal records runner_started, stop_requested, runner_stopped", {"runner_started", "stop_requested", "runner_stopped"} <= set(ev))
    r = c.get("/")
    check("3 api", "dashboard HTML has the Bot Operations view + controls; app.js has the bot module",
          r.status_code == 200 and all(x in r.text for x in ('id="botView"', "Run 1 Cycle", "Start 24/7 Loop", "Emergency Flatten All Positions",
                                                          "Backup State DB", "Test Telegram Alert", 'data-view="bot"'))
          and "loadBotStatus" in c.get("/static/app.js").text and ".bot-grid" in c.get("/static/styles.css").text)

    t = Table(title="Bot status snapshot (after the API drill)")
    for col in ("field", "value"):
        t.add_column(col)
    for k in ("readiness_state", "last_known_state", "mode", "entries_paused", "daily_halted", "loss_streak", "ws_generation",
              "last_reconciled_at", "outcome_unknown_intents"):
        t.add_row(k, str(s.get(k)))
    for k, v in s["wallet"].items():
        t.add_row(f"wallet.{k}", f"{v:,.2f}" if isinstance(v, float) else str(v))
    console.print(t)


# --------------------------------------------------------------------------- #
def test_regression(skip: bool) -> None:
    console.rule("[bold]4. Regression: step6_verify.py (runs step5_verify.py, which runs step4_verify.py)")
    if skip:
        console.print("[yellow]skipped (--skip-regression)[/]")
        return
    t0 = time.time()
    p = subprocess.run([sys.executable, "step6_verify.py"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=PROJECT_ROOT, timeout=2400, env={**os.environ, "PYTHONIOENCODING": "utf-8", "COLUMNS": "200"})
    out = p.stdout
    console.print(out, markup=False, highlight=False)
    if p.stderr.strip():
        console.print(p.stderr[-3000:], markup=False, highlight=False)
    for step, n in (("Step 4", 77), ("Step 5", 92), ("Step 6", 47)):
        lines = [ln.strip("─ ") for ln in out.splitlines() if f"{step} verification:" in ln and "──" in ln]
        check("4 regression", f"{step} verification PASS - {n} checks", bool(lines) and f"PASS - {n} checks" in lines[-1],
              lines[-1] if lines else "verdict line not found")
    check("4 regression", "step6_verify.py exit code 0", p.returncode == 0, f"{time.time() - t0:.0f}s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-regression", action="store_true")
    args = ap.parse_args()
    console.print(f"MODE={settings.MODE!r}  LIVE_TRADING_ENABLED={settings.LIVE_TRADING_ENABLED}  EXECUTION_MODE={settings.EXECUTION_MODE!r}  "
                  f"telegram configured={bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)}")
    reset_dir()
    test_notifier()
    test_backup()
    test_api()
    test_regression(args.skip_regression)

    console.rule("[bold]Summary")
    t = Table()
    for col in ("section", "check", "result", "detail"):
        t.add_column(col)
    for sec, name, ok, detail in CHECKS:
        t.add_row(sec, name, "[green]PASS" if ok else "[red]FAIL", detail[:100])
    console.print(t)
    failed = [x for x in CHECKS if not x[2]]
    console.rule(f"Step 7 verification: {'[green]PASS' if not failed else f'[red]FAIL ({len(failed)} checks)'} - {len(CHECKS)} checks")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
