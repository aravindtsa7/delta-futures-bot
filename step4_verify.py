"""Step 4 verification: dashboard API endpoints + exact parity with the Step 3C CLI results.

Usage:
    python step4_verify.py            # full checks incl. a live candle sync
    python step4_verify.py --no-sync  # skip the network sync test
"""

from __future__ import annotations

import testenv  # noqa: F401,E402  (must run before config is imported: research mode, no live keys)

import argparse
import sys
import time
import warnings
from typing import Any

import pandas as pd

warnings.filterwarnings("ignore", message=".*httpx.*starlette.testclient.*")
from fastapi.testclient import TestClient  # noqa: E402
from rich.console import Console
from rich.table import Table

import app as webapp
import step3c_walkforward as s3c
from backtester import Backtester, BacktestConfig, make_leg
from config import settings

console = Console()
client = TestClient(webapp.app)
CHECKS: list[tuple[str, str, bool, str]] = []

# Portfolio totals printed by `step3c_walkforward.py` (trades, net ₹, fees+GST ₹) per window.
STEP3C_LOG = {
    "modeA": {"W1": (59, 28.26, 829.69), "W2": (65, 274.82, 1183.22), "W3": (67, 1306.44, 946.51), "full": (189, 2186.16, 3021.18)},
    "modeA_WF": {"W1": (28, 501.64, 238.60), "W2": (18, 531.88, 149.34), "W3": (23, -1257.94, 184.90), "full": (69, -597.92, 586.38)},
    "modeB": {"W1": (74, 1107.78, 1195.91), "W2": (67, 2759.66, 1166.19), "W3": (92, -3297.24, 1133.94), "full": (233, -1099.73, 4043.43)},
}
WINDOWS = ["W1", "W2", "W3", "full"]


def check(section: str, name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((section, name, bool(ok), detail))
    return bool(ok)


def paisa(x: float) -> float:
    return round(float(x) + 0.0, 2)


def preset_request(p: dict[str, Any]) -> dict[str, Any]:
    leg0 = p["legs"][0]
    body = {k: p[k] for k in ("days", "end", "history_start", "fee_mode", "initial_capital_inr", "leverage",
                              "risk_per_trade_pct", "max_daily_loss_pct", "trail_atr_mult", "trigger_source", "cooldown_bars")}
    body.update({k: p[k] for k in ("slippage_bps", "trend_filter_4h", "cost_gate", "strict_lot_cap") if k in p})
    body.update(symbols=[leg["symbol"] for leg in p["legs"]], strategy=leg0["strategy"], timeframe=leg0["timeframe"],
                stop_mode=leg0["stop_mode"], sl_value=leg0["sl_value"], tp_value=leg0["tp_value"], exit_mode=leg0["exit_mode"])
    keys = ("strategy", "timeframe", "stop_mode", "sl_value", "tp_value", "exit_mode")
    if any(any(leg[k] != leg0[k] for k in keys) for leg in p["legs"]):
        body["legs"] = p["legs"]
    return body


def api_windows(resp: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {w["name"]: w["summary"] for w in resp["windows"]}
    out["full"] = resp["summary"]
    return out


def run(body: dict[str, Any]) -> tuple[dict[str, Any], float]:
    t0 = time.perf_counter()
    r = client.post("/api/backtest/run", json=body)
    if r.status_code != 200:
        raise RuntimeError(f"backtest failed: {r.status_code} {r.text[:300]}")
    return r.json(), time.perf_counter() - t0


# --------------------------------------------------------------------------- #
def test_status() -> dict[str, Any]:
    console.rule("[bold]1. GET /api/status")
    r = client.get("/api/status")
    check("status", "HTTP 200", r.status_code == 200, str(r.status_code))
    s = r.json()
    check("status", "research mode, live trading disabled",
          s["mode"] == "research" and s["live_trading_enabled"] is False and s["is_live"] is False,
          f"mode={s['mode']} LIVE_TRADING_ENABLED={s['live_trading_enabled']}")
    check("status", "environment", s["environment"] in ("india_prod", "india_testnet"), s["environment"])
    check("status", "symbols include BTCUSD, ETHUSD", {"BTCUSD", "ETHUSD"} <= set(s["symbols"]), str(s["symbols"]))
    t = Table()
    for c in ("symbol", "product_id", "contract_value", "tick_size", "series", "rows", "start (UTC)", "end (UTC)", "gaps"):
        t.add_column(c)
    research = ["BTCUSD", "ETHUSD"]  # the Step 3C parity symbols; coins added later are shown, not checked
    for sym in s["symbols"]:
        p = s["products"][sym]
        if sym in research:
            check("status", f"{sym} product metadata", p and float(p["contract_value"]) > 0 and float(p["tick_size"]) > 0,
                  f"cv={p and p['contract_value']} tick={p and p['tick_size']}")
        for st, cov in (s["coverage"][sym]["series"] or {}).items():
            if not cov:
                continue
            if sym in research:
                check("status", f"{sym} {st} coverage >= 180 days, 0 gaps", cov["rows"] >= 259_200 and cov["gaps"] == 0,
                      f"rows={cov['rows']} gaps={cov['gaps']}")
            t.add_row(sym if sym in research else f"{sym} (added, info)", str(p["id"]) if p else "-",
                      str(p["contract_value"]) if p else "-", str(p["tick_size"]) if p else "-", st, f"{cov['rows']:,}",
                      cov["start"][:16], cov["end"][:16], str(cov["gaps"]))
    console.print(t)
    console.print(f"mode={s['mode']}  LIVE_TRADING_ENABLED={s['live_trading_enabled']}  env={s['environment']}  "
                  f"strategies={len(s['strategies'])}  timeframes={s['timeframes']}\n")
    return s


def test_presets() -> list[dict[str, Any]]:
    console.rule("[bold]2. GET /api/presets")
    r = client.get("/api/presets")
    check("presets", "HTTP 200", r.status_code == 200, str(r.status_code))
    body = r.json()
    presets = body["presets"]
    three = [p for p in presets if p["id"].startswith("3of3:")]
    ids = {p["id"] for p in presets}
    check("presets", "7 configurations that won 3/3 windows", len(three) == 7, str(len(three)))
    check("presets", "Mode A / Mode A-WF / Mode B portfolio presets", {"modeA", "modeA_WF", "modeB"} <= ids, str(sorted(ids - {p['id'] for p in three})))
    a4 = body.get("step4b_anchor") or {}
    check("presets", "pinned to their anchors (Step 3C / Step 4B)",
          all(p["end"] == (a4.get("end") if p.get("anchor") == "step4b" else body["anchor"]["end"]) for p in presets),
          f"3C {body['anchor']['end']} | 4B {a4.get('end')}")
    t = Table()
    for c in ("group", "label", "kind", "W1 ₹", "W2 ₹", "W3 ₹", "180d ₹"):
        t.add_column(c, justify="right" if "₹" in c else "left")
    for p in presets:
        s = p.get("stats")
        t.add_row(p["group"].split(" (")[0], p["label"], p["kind"],
                  *([f"{s['net_W1']:,.2f}", f"{s['net_W2']:,.2f}", f"{s['net_W3']:,.2f}", f"{s['net']:,.2f}"] if s else ["", "", "", ""]))
    console.print(t)
    console.print()
    return presets


def test_leaderboard() -> None:
    console.rule("[bold]3. GET /api/leaderboard")
    r = client.get("/api/leaderboard", params={"source": "step3c", "limit": 5000})
    b = r.json()
    check("leaderboard", "step3c: 1344 configurations", r.status_code == 200 and b["total"] == 1344, str(b.get("total")))
    nets = [x["net"] for x in b["rows"]]
    check("leaderboard", "step3c sorted by net desc", nets == sorted(nets, reverse=True))
    r = client.get("/api/leaderboard", params={"source": "step3c", "passing_only": True, "limit": 5000})
    check("leaderboard", "passing filter -> 69 rows (as Step 3C)", r.json()["total"] == 69, str(r.json()["total"]))
    r = client.get("/api/leaderboard", params={"source": "step3c", "symbol": "ETHUSD", "tf": "1h", "sort": "net_W3", "desc": False})
    rows = r.json()["rows"]
    check("leaderboard", "symbol/tf filter + ascending sort",
          all(x["symbol"] == "ETHUSD" and x["tf"] == "1h" for x in rows)
          and [x["net_W3"] for x in rows] == sorted(x["net_W3"] for x in rows), f"{len(rows)} rows")
    r = client.get("/api/leaderboard", params={"source": "step3b", "limit": 5000})
    check("leaderboard", "step3b: 360 configurations", r.status_code == 200 and r.json()["total"] == 360, str(r.json().get("total")))
    top = client.get("/api/leaderboard", params={"source": "step3c", "limit": 3}).json()["rows"]
    for x in top:
        console.print(f"  top: {x['symbol']} {x['strategy']} {x['tf']} {x['stops']} {x['exits']}  net ₹{x['net']:,.2f}  "
                      f"W1/W2/W3 {x['net_W1']:,.2f}/{x['net_W2']:,.2f}/{x['net_W3']:,.2f}")
    console.print()


def test_single_parity(presets: list[dict[str, Any]], label: str = "4") -> bool:
    console.rule(f"[bold]{label}. POST /api/backtest/run (single symbol) vs step3c_walkforward.csv - all 3/3 presets")
    csv = pd.read_csv(webapp.STEP3C_CSV).set_index(["symbol", "strategy", "tf", "stops", "exits", "window"])
    t = Table()
    for c in ("preset", "window", "CSV net ₹", "API net ₹", "Δ ₹", "trades CSV/API", "CSV fees ₹", "API fees ₹", "match"):
        t.add_column(c, justify="left" if c in ("preset", "window", "match") else "right")
    all_ok = True
    for p in [p for p in presets if p["id"].startswith("3of3:")]:
        leg = p["legs"][0]
        stops = next(k for k, v in s3c.STOPS.items() if webapp.stops_to_params(k) == {
            "stop_mode": leg["stop_mode"], "sl_value": leg["sl_value"], "tp_value": leg["tp_value"]})
        resp, secs = run(preset_request(p))
        got = api_windows(resp)
        for w in WINDOWS:
            row = csv.loc[(leg["symbol"], leg["strategy"], leg["timeframe"], stops, leg["exit_mode"], w)]
            a = got[w]
            ok = (paisa(a["net_pnl_inr"]) == paisa(row["net"]) and a["total_trades"] == row["trades"]
                  and paisa(a["fees_gst_inr"]) == paisa(row["fees_gst"]))
            all_ok &= ok
            t.add_row(p["label"] if w == "W1" else "", w, f"{row['net']:,.2f}", f"{a['net_pnl_inr']:,.2f}",
                      f"{a['net_pnl_inr'] - row['net']:+.10f}", f"{int(row['trades'])}/{a['total_trades']}",
                      f"{row['fees_gst']:,.2f}", f"{a['fees_gst_inr']:,.2f}", "[green]exact" if ok else "[red]MISMATCH")
        check("parity-single", f"{p['label']} (4 windows, {secs:.1f}s)", all(
            paisa(got[w]["net_pnl_inr"]) == paisa(csv.loc[(leg["symbol"], leg["strategy"], leg["timeframe"], stops, leg["exit_mode"], w)]["net"])
            for w in WINDOWS))
        check("parity-single", f"{p['label']} ledger audit", resp["audit"]["passed"] and all(w["audit_passed"] for w in resp["windows"]))
    console.print(t)
    console.print()
    return all_ok


def cli_portfolio(p: dict[str, Any], anchor: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Independent recompute with the CLI building blocks (no app.py code involved)."""
    base = BacktestConfig(**s3c.BASE)
    legs = []
    for leg in p["legs"]:
        stops = next(k for k, v in s3c.STOPS.items() if webapp.stops_to_params(k) == {
            "stop_mode": leg["stop_mode"], "sl_value": leg["sl_value"], "tp_value": leg["tp_value"]})
        df = webapp.get_history(leg["symbol"], pd.Timestamp(anchor["warmup_start"]))
        legs.append(make_leg(leg["symbol"], leg["strategy"], leg["timeframe"], df_1m=df, config=s3c.make_config(stops, leg["exit_mode"])))
    out = {}
    for w in WINDOWS:
        a, b = (pd.Timestamp(x) for x in anchor["windows"][w])
        out[w] = Backtester(base).run_portfolio(legs, a, b).summary
    return out


def test_portfolio_parity(presets: list[dict[str, Any]]) -> dict[str, Any]:
    console.rule("[bold]5. POST /api/backtest/run (shared-wallet portfolios) vs CLI recompute and Step 3C log")
    anchor = webapp.step3c_anchor()
    t = Table()
    for c in ("preset", "window", "log net ₹", "CLI net ₹", "API net ₹", "trades log/CLI/API", "log fees ₹", "API fees ₹", "match"):
        t.add_column(c, justify="left" if c in ("preset", "window", "match") else "right")
    single_resp = None
    for pid in ("modeA", "modeA_WF", "modeB"):
        p = next(x for x in presets if x["id"] == pid)
        resp, secs = run(preset_request(p))
        if pid == "modeA":
            single_resp = resp
        got = api_windows(resp)
        cli = cli_portfolio(p, anchor)
        ok_all = True
        for w in WINDOWS:
            lt, ln, lf = STEP3C_LOG[pid][w]
            a, c = got[w], cli[w]
            exact = a["net_pnl_inr"] == c["net_pnl_inr"] and a["fees_gst_inr"] == c["fees_gst_inr"] and a["total_trades"] == c["total_trades"]
            logged = paisa(a["net_pnl_inr"]) == ln and a["total_trades"] == lt and paisa(a["fees_gst_inr"]) == lf
            ok_all &= exact and logged
            t.add_row(pid if w == "W1" else "", w, f"{ln:,.2f}", f"{c['net_pnl_inr']:,.2f}", f"{a['net_pnl_inr']:,.2f}",
                      f"{lt}/{c['total_trades']}/{a['total_trades']}", f"{lf:,.2f}", f"{a['fees_gst_inr']:,.2f}",
                      "[green]exact" if exact and logged else "[red]MISMATCH")
        legs = " + ".join(f"{leg['symbol']} {leg['strategy']} {leg['timeframe']}" for leg in p["legs"])
        check("parity-portfolio", f"{pid}: {legs} ({secs:.1f}s)", ok_all)
        check("parity-portfolio", f"{pid} ledger audit", resp["audit"]["passed"] and all(w["audit_passed"] for w in resp["windows"]))
    console.print(t)
    console.print()
    return single_resp


def test_payload(presets: list[dict[str, Any]]) -> None:
    console.rule("[bold]6. Chart / equity / ledger payload")
    p = next(x for x in presets if x["label"] == "ETHUSD bollinger_volume_breakout 1h atr 2x/5x trail")
    resp, secs = run(preset_request(p))
    ch = resp["charts"]["ETHUSD"]
    n_tr = resp["summary"]["total_trades"]
    expect_bars = int((pd.Timestamp(resp["window"]["end"]) - pd.Timestamp(resp["window"]["start"])) / pd.Timedelta(hours=1))
    names = {o["name"] for o in ch["overlays"]}
    check("payload", "candles cover the window (1h)", abs(len(ch["candles"]) - expect_bars) <= 1, f"{len(ch['candles'])} vs {expect_bars}")
    check("payload", "entry + exit marker per trade", len(ch["markers"]) == 2 * n_tr, f"{len(ch['markers'])} markers / {n_tr} trades")
    check("payload", "markers sorted & on candle times",
          [m["time"] for m in ch["markers"]] == sorted(m["time"] for m in ch["markers"])
          and {m["time"] for m in ch["markers"]} <= {c["time"] for c in ch["candles"]})
    check("payload", "Bollinger + EMA overlays", {"bb_upper", "bb_mid", "bb_lower", "ema50"} <= names, ", ".join(sorted(names)))
    check("payload", "ledger rows == total_trades", len(resp["trades"]) == n_tr)
    check("payload", "ledger INR == USD x 85", all(abs(t["net_pnl_inr"] - t["net_pnl_usd"] * 85) < 1e-9 for t in resp["trades"]))
    eq = resp["equity"]
    check("payload", "equity & drawdown series (<= 4000 pts)", 0 < len(eq["equity"]) <= 4001 and len(eq["drawdown"]) == len(eq["equity"]),
          f"{len(eq['equity'])} points, bucket {eq['bucket_sec']}s")
    check("payload", "exit reason counts", sum(resp["summary"]["exit_reason_counts"].values()) == n_tr, str(resp["summary"]["exit_reason_counts"]))
    console.print(f"ETHUSD 1h: {len(ch['candles']):,} candles, {len(ch['overlays'])} overlays ({', '.join(sorted(names))}), "
                  f"{len(ch['markers'])} markers, {len(eq['equity']):,} equity points, response in {secs:.2f}s\n")


def test_validation_and_static() -> None:
    console.rule("[bold]7. Validation, static files")
    base = {"symbols": ["ETHUSD"], "strategy": "bollinger_volume_breakout", "timeframe": "1h", "days": 30}
    r = client.post("/api/backtest/run", json={**base, "strategy": "not_a_strategy"})
    check("validation", "unknown strategy -> 422", r.status_code == 422, str(r.status_code))
    r = client.post("/api/backtest/run", json={**base, "symbols": ["../../etc"]})
    check("validation", "path-like symbol -> 422", r.status_code == 422, str(r.status_code))
    r = client.post("/api/backtest/run", json={**base, "symbols": ["XRPUSD"]})
    check("validation", "symbol without local data -> 422", r.status_code == 422, r.json().get("detail", "")[:80])
    r = client.post("/api/backtest/run", json={**base, "stop_mode": "pct", "sl_value": 15, "tp_value": 30})
    check("validation", "SL beyond liquidation distance -> 422", r.status_code == 422, r.json().get("detail", "")[:80])
    r = client.post("/api/backtest/run", json={**base, "timeframe": "7m"})
    check("validation", "unsupported timeframe -> 422", r.status_code == 422, str(r.status_code))
    r = client.post("/api/data/sync", json={"add_symbol": "NOTACOIN123"})
    check("validation", "add unknown coin -> 404 (validated via /v2/products)", r.status_code == 404, r.json().get("detail", "")[:80])
    r = client.get("/")
    check("static", "GET / serves dashboard with TradingView attribution",
          r.status_code == 200 and "lightweight-charts@4.2.0" in r.text and "tradingview.com" in r.text)
    for f in ("app.js", "styles.css"):
        r = client.get(f"/static/{f}")
        check("static", f"/static/{f}", r.status_code == 200 and len(r.content) > 1000, f"{len(r.content):,} bytes")
    console.print()


def test_sync(presets: list[dict[str, Any]]) -> None:
    console.rule("[bold]8. POST /api/data/sync (background) + parity re-check on the grown dataset")
    before = {s: client.get("/api/status").json()["coverage"][s]["series"]["TRADE"]["rows"] for s in settings.SYMBOLS}
    r = client.post("/api/data/sync", json={})
    check("sync", "POST returns 202 immediately", r.status_code == 202, f"{r.status_code} state={r.json().get('state')}")
    t0 = time.time()
    st = r.json()
    while st.get("state") == "running" and time.time() - t0 < 300:
        time.sleep(1.0)
        st = client.get("/api/data/sync").json()
    check("sync", "job finished", st.get("state") == "done", f"{st.get('state')} in {time.time() - t0:.1f}s {st.get('error', '')}")
    after = {s: client.get("/api/status").json()["coverage"][s]["series"]["TRADE"]["rows"] for s in settings.SYMBOLS}
    fetched = sum(x["fetched"] for x in st.get("results", []))
    check("sync", "rows grew (or already current)", all(after[s] >= before[s] for s in before),
          ", ".join(f"{s} {before[s]:,} -> {after[s]:,}" for s in before) + f" (fetched {fetched:,} rows)")
    status = client.get("/api/status").json()
    gaps = {s: sum(v["gaps"] for v in status["coverage"][s]["series"].values()) for s in settings.SYMBOLS}
    check("sync", "still 0 gaps after sync", not any(gaps.values()), str(gaps))
    p = next(x for x in presets if x["label"] == "ETHUSD bollinger_volume_breakout 1h atr 2x/5x trail")
    resp, _ = run(preset_request(p))
    csv = pd.read_csv(webapp.STEP3C_CSV)
    row = csv[(csv.symbol == "ETHUSD") & (csv.strategy == "bollinger_volume_breakout") & (csv.tf == "1h")
              & (csv.stops == "atr 2x/5x") & (csv.exits == "trail") & (csv.window == "full")].iloc[0]
    check("sync", "pinned run still matches CSV after new candles", paisa(resp["summary"]["net_pnl_inr"]) == paisa(row["net"])
          and resp["summary"]["total_trades"] == row["trades"], f"API ₹{resp['summary']['net_pnl_inr']:,.2f} vs CSV ₹{row['net']:,.2f}")
    latest = dict(preset_request(p), end=None)
    resp2, _ = run(latest)
    shift = pd.Timestamp(resp2["window"]["end"]) - pd.Timestamp(p["end"])
    console.print(f"Same config on the LATEST 180 days (window end {resp2['window']['end'][:16]}, {shift} after the Step 3C anchor): "
                  f"net ₹{resp2['summary']['net_pnl_inr']:,.2f}, {resp2['summary']['total_trades']} trades\n")


def test_step4b(presets: list[dict[str, Any]]) -> None:
    console.rule("[bold]9. Step 4B API: ₹50,000 default, provenance tags, filters/slippage parity with step4b_comparison.csv")
    s = client.get("/api/status").json()
    check("step4b", "default capital ₹50,000 (config + API)", s["defaults"]["initial_capital_inr"] == 50_000.0,
          str(s["defaults"]["initial_capital_inr"]))
    r = client.post("/api/backtest/run", json={"symbols": ["ETHUSD"], "days": 30, "include_chart": False, "walk_forward_windows": 0})
    check("step4b", "request without capital runs at ₹50,000", r.status_code == 200 and r.json()["config"]["initial_capital_inr"] == 50_000.0)
    tags = {p.get("tag") for p in presets}
    check("step4b", "every preset carries a provenance tag", all(p.get("tag") for p in presets), " | ".join(sorted(t for t in tags if t)))
    legacy = [p for p in presets if p.get("anchor") == "step3c"]
    check("step4b", "legacy Step 3C presets keep ₹10,000 + warm-up pin", all(p["initial_capital_inr"] == 10_000.0 and p["history_start"] for p in legacy),
          f"{len(legacy)} presets")
    p4 = [p for p in presets if p["id"].startswith("4b:")]
    check("step4b", "8 Step 4B ablation presets (2 symbols x A-D)", len(p4) == 8, str(len(p4)))

    csv_path = webapp.STEP4B_CSV
    if not csv_path.exists():
        check("step4b", "step4b_comparison.csv present", False, "run step4b_research.py first")
        return
    csv = pd.read_csv(csv_path)
    prim = csv[(csv["kind"] == "variant") & (csv["window"] == "full_extended") & (csv["risk_pct"] == 1.0)
               & (csv["trigger"] == "trade") & (csv["slippage_bps"] == 5.0)].set_index(["symbol", "variant"])
    t = Table()
    for c in ("preset", "CSV net ₹", "API net ₹", "trades", "slippage ₹ CSV/API", "4h rej CSV/API", "gate rej CSV/API", "lot skips", "match"):
        t.add_column(c, justify="left" if c in ("preset", "match") else "right")
    for p in p4:
        sym, variant = p["legs"][0]["symbol"], p["id"].split(":")[2]
        body = preset_request(p)
        body.update(include_chart=False, walk_forward_windows=0)
        resp, secs = run(body)
        row = prim.loc[(sym, variant)]
        a = resp["summary"]
        st = resp["filters"]["stats"].get(sym, {})
        rej_t = st.get("rejected_by_trend4h", 0)
        rej_g = st.get("rejected_by_costgate", 0)
        csv_t = 0 if pd.isna(row.get("filter_rejected_by_trend4h")) else int(row["filter_rejected_by_trend4h"])
        csv_g = 0 if pd.isna(row.get("filter_rejected_by_costgate")) else int(row["filter_rejected_by_costgate"])
        ok = (paisa(a["net_pnl_inr"]) == paisa(row["net_pnl_inr"]) and a["total_trades"] == row["total_trades"]
              and paisa(a["fees_gst_inr"]) == paisa(row["fees_gst_inr"]) and paisa(a["slippage_inr"]) == paisa(row["slippage_inr"])
              and rej_t == csv_t and rej_g == csv_g and a["skipped_lot_budget"] == row["skipped_lot_budget"] and resp["audit"]["passed"])
        check("step4b", f"API == CLI: {sym} {variant} (5 bps, {secs:.1f}s)", ok)
        t.add_row(f"{sym} {variant}", f"{row['net_pnl_inr']:,.2f}", f"{a['net_pnl_inr']:,.2f}", f"{int(row['total_trades'])}/{a['total_trades']}",
                  f"{row['slippage_inr']:,.2f}/{a['slippage_inr']:,.2f}", f"{csv_t}/{rej_t}", f"{csv_g}/{rej_g}",
                  str(a["skipped_lot_budget"]), "[green]exact" if ok else "[red]MISMATCH")
    console.print(t)
    p = next(x for x in p4 if x["id"] == "4b:ETHUSD:A_baseline")
    body = dict(preset_request(p), include_chart=False, walk_forward_windows=0, slippage_bps=0.0)
    r0, _ = run(body)
    check("step4b", "0 bps -> slippage cost ₹0; 5 bps -> > ₹0", r0["summary"]["slippage_inr"] == 0.0
          and prim.loc[("ETHUSD", "A_baseline"), "slippage_inr"] > 0, f"0 bps ₹{r0['summary']['slippage_inr']:.2f}")
    console.print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-sync", action="store_true", help="skip the live candle sync test")
    args = ap.parse_args()

    test_status()
    presets = test_presets()
    test_leaderboard()
    test_single_parity(presets)
    test_portfolio_parity(presets)
    test_payload(presets)
    test_validation_and_static()
    test_step4b(presets)
    if not args.no_sync:
        test_sync(presets)

    console.rule("[bold]Summary")
    t = Table()
    for c in ("section", "check", "result", "detail"):
        t.add_column(c)
    for sec, name, ok, detail in CHECKS:
        t.add_row(sec, name, "[green]PASS" if ok else "[red]FAIL", detail)
    console.print(t)
    failed = [c for c in CHECKS if not c[2]]
    console.rule(f"Step 4 verification: {'[green]PASS' if not failed else f'[red]FAIL ({len(failed)} checks)'} - {len(CHECKS)} checks")
    console.print("\nLaunch the dashboard:\n  [bold].venv\\Scripts\\python.exe -m uvicorn app:app --reload[/]\n"
                  "then open [bold]http://127.0.0.1:8000[/] in your browser.\n")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
