"""Step 4B: ₹50,000 capital, closed-4h trend filter + volatility/cost gate ablation, slippage stress.

Usage:
    python step4b_research.py

Predeclared, frozen specification (no thresholds are tuned here):
  * Entry: existing 1h `bollinger_volume_breakout`; exits ATR SL 2.0x / TP 5.0x + 2.5x ATR trailing.
  * Variants: A_baseline, B_trend4h (closed-4h EMA50/200 regime), C_costgate (raw ATR/close >= 2.0 x
    estimated round-trip cost), D_both. Filters are applied BEFORE the edge trigger.
  * Primary screen: ₹50,000, 10x, 1% risk, all-taker + 18% GST, funding, 5 bps adverse slippage per fill,
    strict whole-lot risk cap, trade-price triggers, over the full extended history after a 45-day warm-up.

Outputs (data/results/): step4b_comparison.csv, step4b_expanding_windows.csv, step4b_windows.json, STEP4B_REPORT.md
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

import data_manager as dm
import strategies
from backtester import Backtester, BacktestConfig, BacktestResult, Leg, ProductSpec, audit_result
from config import PROJECT_ROOT, settings

console = Console()
RESULTS = settings.data_path / "results"

CAPITAL = 50_000.0
LEGACY_CAPITAL = 10_000.0
LEVERAGE = 10
STRATEGY, TF = "bollinger_volume_breakout", "1h"
VARIANTS: dict[str, tuple[bool, bool]] = {
    "A_baseline": (False, False), "B_trend4h": (True, False), "C_costgate": (False, True), "D_both": (True, True),
}
SLIPPAGE_BPS = [0.0, 2.0, 5.0]
RISKS = [1.0, 2.0]          # 1% primary, 2% secondary
TRIGGERS = ["trade", "mark"]
PRIMARY = {"risk": 1.0, "trigger": "trade", "slip": 5.0}
WARMUP_DAYS = 45
TARGET_DAYS = 365
GATE_MULT = 2.0
BOOT_BLOCK, BOOT_N, BOOT_SEED = 5, 1000, 20261020
RUBRIC = {"min_pf": 1.20, "max_dd": 15.0, "min_trades": 50, "min_window_share": 2 / 3}

UI_BASELINES = [  # (label, risk, fee_mode, trigger, expected net ₹, trades, PF, max DD %)
    ("1  ₹10k 2% taker_maker_tp trade", 2.0, "taker_maker_tp", "trade", 2660.98, 92, 1.34, 11.94),
    ("2  ₹10k 2% taker trade", 2.0, "taker", "trade", 2575.91, 92, 1.33, 12.20),
    ("3  ₹10k 2% taker mark", 2.0, "taker", "mark", 2439.95, 90, 1.31, 10.96),
    ("4  ₹10k 1% taker trade", 1.0, "taker", "trade", 893.68, 92, 1.26, 6.51),
]

CHECKS: list[tuple[str, str, bool, str]] = []
REPORT: list[str] = []


def check(section: str, name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((section, name, bool(ok), detail))
    return bool(ok)


def make_cfg(capital: float, risk: float, fee_mode: str, trigger: str, slip: float, strict: bool,
             mark_fill: str) -> BacktestConfig:
    return BacktestConfig(
        initial_capital_inr=capital, leverage=LEVERAGE, risk_per_trade_pct=risk, max_daily_loss_pct=settings.MAX_DAILY_LOSS_PCT,
        fee_mode=fee_mode, trigger_source=trigger, cooldown_bars=1, sl_mode="atr", sl_atr_mult=2.0, tp_atr_mult=5.0,
        trailing_atr_mult=2.5, slippage_bps=slip, allow_min_one_lot=not strict, mark_fill_mode=mark_fill,
    )


def step4b_cfg(risk: float, trigger: str, slip: float) -> BacktestConfig:
    return make_cfg(CAPITAL, risk, "taker", trigger, slip, strict=True, mark_fill="traded")


def sha256_file(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cfg_hash(cfg: BacktestConfig) -> str:
    return hashlib.sha256(json.dumps(asdict(cfg), sort_keys=True).encode()).hexdigest()[:16]


def data_fingerprint(df: pd.DataFrame, start, end) -> str:
    w = df[(df.index >= start) & (df.index < end)]
    cols = ["open", "high", "low", "close", "volume", "mark_close", "funding_rate"]
    h = hashlib.sha256(w.index.as_unit("s").asi8.tobytes())
    h.update(np.nan_to_num(w[cols].to_numpy("float64"), nan=-1.0).tobytes())
    return h.hexdigest()[:16]


def md_table(df: pd.DataFrame, floatfmt: str = "{:,.2f}") -> str:
    def f(v: Any) -> str:
        if isinstance(v, float):
            if math.isnan(v):
                return "–"
            if math.isinf(v):
                return "∞"
            return floatfmt.format(v)
        return str(v)
    head = "| " + " | ".join(map(str, df.columns)) + " |"
    sep = "| " + " | ".join("---" for _ in df.columns) + " |"
    rows = ["| " + " | ".join(f(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join([head, sep, *rows])


def rich_table(df: pd.DataFrame, title: str = "", floatfmt: str = "{:,.2f}") -> Table:
    t = Table(title=title)
    for c in df.columns:
        t.add_column(str(c), justify="left" if df[c].dtype == object else "right")
    for row in df.itertuples(index=False):
        cells = []
        for v in row:
            if isinstance(v, float):
                cells.append("–" if math.isnan(v) else ("∞" if math.isinf(v) else floatfmt.format(v)))
            else:
                cells.append(str(v))
        t.add_row(*cells)
    return t


# --------------------------------------------------------------------------- #
# Frames (bars, signals, filters) per symbol and history start
# --------------------------------------------------------------------------- #
class Frames:
    def __init__(self, symbol: str, hist: pd.DataFrame):
        self.symbol = symbol
        self.hist = hist
        self.product = ProductSpec.from_metadata(symbol)
        self.bars = dm.resample_candles(hist, TF)
        self.out = strategies.generate_signals(STRATEGY, self.bars)
        self.atr = strategies.atr(self.bars, 14)
        self.bars_4h = dm.resample_candles(hist, "4h")
        self.trend = strategies.trend_filter_4h(self.bars, self.bars_4h, dm.TIMEFRAME_MINUTES[TF])
        self._gates: dict[float, pd.DataFrame] = {}

    def gate(self, slip: float) -> pd.DataFrame:
        if slip not in self._gates:
            self._gates[slip] = strategies.cost_gate(self.bars, settings.TAKER_FEE, settings.GST_ON_FEES, slip, GATE_MULT)
        return self._gates[slip]

    def masks(self, variant: str, slip: float) -> dict[str, tuple[pd.Series, pd.Series]]:
        trend, gate = VARIANTS[variant]
        m: dict[str, tuple[pd.Series, pd.Series]] = {}
        if trend:
            m["trend4h"] = (self.trend["allow_long"], self.trend["allow_short"])
        if gate:
            g = self.gate(slip)["allow"]
            m["costgate"] = (g, g)
        return m

    def signal(self, variant: str, slip: float, start=None, end=None) -> tuple[pd.Series, dict[str, int]]:
        cm = None
        if start is not None:
            fill = self.bars.index + pd.Timedelta(minutes=dm.TIMEFRAME_MINUTES[TF])
            cm = (fill >= start) & (fill < end)
        return strategies.apply_entry_filters(self.out, self.masks(variant, slip), count_mask=cm)

    def run(self, cfg: BacktestConfig, variant: str, slip: float, start, end) -> tuple[BacktestResult, dict[str, int]]:
        sig, stats = self.signal(variant, slip, start, end)
        leg = Leg(self.product, self.hist, sig, TF, self.atr, STRATEGY, None, cfg)
        return Backtester(cfg).run_portfolio([leg], start, end), stats


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def stationary_bootstrap(x: np.ndarray, block: int = BOOT_BLOCK, n: int = BOOT_N, seed: int = BOOT_SEED) -> tuple[float, float, float]:
    """Politis-Romano stationary block bootstrap of the summed trade PnL (mean block length ``block``)."""
    x = np.asarray(x, float)
    N = len(x)
    if N == 0:
        return (0.0, 0.0, 0.0)
    rng = np.random.default_rng(seed)
    idx = np.empty((n, N), dtype=np.int64)
    idx[:, 0] = rng.integers(0, N, n)
    restart = rng.random((n, N)) < 1.0 / block
    fresh = rng.integers(0, N, (n, N))
    for t in range(1, N):
        idx[:, t] = np.where(restart[:, t], fresh[:, t], (idx[:, t - 1] + 1) % N)
    p5, p50, p95 = np.percentile(x[idx].sum(axis=1), [5, 50, 95])
    return float(p5), float(p50), float(p95)


def buy_and_hold(hist: pd.DataFrame, start, end, capital: float) -> dict[str, float]:
    """Unlevered 1x long from the first 1m open to the last 1m close, after round-trip taker fee + GST (no funding)."""
    w = hist[(hist.index >= start) & (hist.index < end)]
    f = settings.TAKER_FEE * (1 + settings.GST_ON_FEES)
    o, c = float(w["open"].iloc[0]), float(w["close"].iloc[-1])
    value = capital * (1 - f) * (c / o) * (1 - f)
    curve = capital * (1 - f) * w["close"] / o
    dd = float(((curve.cummax() - curve) / curve.cummax()).max() * 100)
    return {"bh_net_inr": value - capital, "bh_return_pct": (value / capital - 1) * 100, "bh_max_dd_pct": dd,
            "price_change_pct": (c / o - 1) * 100}


def metrics(r: BacktestResult) -> dict[str, Any]:
    s, t = r.summary, r.trades
    out = {k: s[k] for k in ("net_pnl_inr", "net_return_pct", "gross_pnl_inr", "fees_gst_inr", "funding_inr", "slippage_inr",
                             "profit_factor", "max_drawdown_pct", "total_trades", "win_rate_pct", "exposure_pct",
                             "skipped_lot_budget", "skipped_insufficient_capital", "min_lot_floor_used", "daily_halts",
                             "ambiguous_1m_bars", "risk_pct_min", "risk_pct_median", "risk_pct_max", "final_equity_inr")}
    out["exit_reasons"] = json.dumps(s["exit_reasons"], sort_keys=True)
    if len(t):
        longs, shorts = t[t["side"] == "LONG"], t[t["side"] == "SHORT"]
        top3 = t["net_pnl_inr"].nlargest(3).sum()
        net = t["net_pnl_inr"].sum()
        p5, p50, p95 = stationary_bootstrap(t["net_pnl_inr"].to_numpy())
        out.update(long_trades=len(longs), long_net_inr=float(longs["net_pnl_inr"].sum()), short_trades=len(shorts),
                   short_net_inr=float(shorts["net_pnl_inr"].sum()),
                   top3_concentration_pct=float(top3 / net * 100) if net > 0 else float("nan"),
                   boot_p5=p5, boot_p50=p50, boot_p95=p95,
                   contracts_min=int(t["contracts"].min()), contracts_median=float(t["contracts"].median()),
                   contracts_max=int(t["contracts"].max()))
    else:
        out.update(long_trades=0, long_net_inr=0.0, short_trades=0, short_net_inr=0.0, top3_concentration_pct=float("nan"),
                   boot_p5=0.0, boot_p50=0.0, boot_p95=0.0, contracts_min=0, contracts_median=0.0, contracts_max=0)
    errs = audit_result(r)
    ident = abs(r.config.initial_capital_inr + (t["net_pnl_inr"].sum() if len(t) else 0.0) - s["final_equity_inr"])
    out.update(audit_ok=not errs, audit_errors=len(errs), equity_identity_abs_diff=ident)
    return out


# --------------------------------------------------------------------------- #
# Synthetic engine tests (exact 1m paths)
# --------------------------------------------------------------------------- #
SYN_START = pd.Timestamp("2026-01-01 01:00", tz="UTC")


def synth_run(cfg: BacktestConfig, candles: list[tuple[float, float, float, float]],
              mark: dict[int, tuple[float, float, float, float]] | None = None, atr: float = 10.0) -> BacktestResult:
    idx = pd.date_range(SYN_START, periods=len(candles), freq="1min", tz="UTC")
    df = pd.DataFrame(candles, columns=["open", "high", "low", "close"], index=idx, dtype=float)
    df["volume"] = 1.0
    for k in ("open", "high", "low", "close"):
        df[f"mark_{k}"] = df[k]
    for i, (mo, mh, ml, mc) in (mark or {}).items():
        df.iloc[i, [df.columns.get_loc(c) for c in ("mark_open", "mark_high", "mark_low", "mark_close")]] = (mo, mh, ml, mc)
    df["funding_rate_pct"] = 0.0
    df["funding_rate"] = 0.0
    sig_idx = pd.DatetimeIndex([SYN_START - pd.Timedelta(hours=1)])
    sig = pd.Series(np.array([1], dtype="int8"), index=sig_idx)
    product = ProductSpec("SYNTH", 0, 0.01, 0.5)
    leg = Leg(product, df, sig, "1h", pd.Series([atr], index=sig_idx), "synthetic", None, cfg)
    return Backtester(cfg).run_portfolio([leg])


def synth_cfg(**kw: Any) -> BacktestConfig:
    base = dict(initial_capital_inr=CAPITAL, leverage=LEVERAGE, risk_per_trade_pct=1.0, sl_mode="pct", stop_loss_pct=2.0,
                take_profit_pct=10.0, max_daily_loss_pct=0.0, fee_mode="taker", allow_min_one_lot=False)
    return BacktestConfig(**{**base, **kw})


def test_engine_paths() -> None:
    console.rule("[bold]Invariant 2/3. Synthetic 1m paths: next-candle trailing & breakeven, gap-through, ambiguity, slippage")
    rows = []

    # Trailing: candle 1 makes a new high (1030 -> trail 1020) and dips to 1019 in the SAME candle -> must NOT exit.
    r = synth_run(synth_cfg(trailing_atr_mult=1.0), [(1000, 1001, 999, 1000), (1000, 1030, 1019, 1025),
                                                     (1025, 1026, 1021, 1022), (1022, 1023, 1019, 1020), (1020, 1021, 1019, 1020)])
    tr = r.trades.iloc[0]
    ok = tr.exit_time == SYN_START + pd.Timedelta(minutes=3) and tr.exit_reason == "TSL" and tr.raw_exit_price == 1020.0
    rows.append(("trailing stop set on candle t is active only from t+1", ok,
                 f"exit {tr.exit_time:%H:%M} {tr.exit_reason} @ {tr.raw_exit_price:g} (candle 01:01 low 1019 < 1020 ignored)"))

    # Breakeven: candle 1 reaches +1R (1020) and trades back to 1000 (< BE 1001.5) in the SAME candle -> no exit.
    r = synth_run(synth_cfg(breakeven_at_1r=True), [(1000, 1001, 999, 1000), (1000, 1021, 1000, 1010),
                                                    (1010, 1010, 1002, 1005), (1003, 1004, 1001, 1001), (1001, 1002, 1000, 1001)])
    tr = r.trades.iloc[0]
    ok = tr.exit_time == SYN_START + pd.Timedelta(minutes=3) and tr.exit_reason == "BE" and tr.raw_exit_price == tr.be_price == 1001.5
    rows.append(("breakeven armed on candle t is active only from t+1", ok,
                 f"exit {tr.exit_time:%H:%M} {tr.exit_reason} @ {tr.raw_exit_price:g} (BE {tr.be_price:g}; 01:01 low 1000 ignored)"))

    # Gap-through: candle 2 opens at 975, below the 980 stop -> fill at the open (worse than the stop).
    r = synth_run(synth_cfg(), [(1000, 1001, 999, 1000), (1000, 1001, 990, 995), (975, 976, 970, 972)])
    tr = r.trades.iloc[0]
    rows.append(("gap-through stop fills at the 1m open", tr.raw_exit_price == 975.0 and tr.exit_reason == "SL",
                 f"stop {tr.initial_sl:g}, filled {tr.raw_exit_price:g}"))

    # Ambiguous: one candle touches both SL (980) and TP (1020) -> SL first, counted.
    r = synth_run(synth_cfg(take_profit_pct=2.0), [(1000, 1001, 999, 1000), (1000, 1025, 975, 1000)])
    tr = r.trades.iloc[0]
    rows.append(("SL+TP in one 1m candle -> SL first, counted", tr.exit_reason == "SL" and r.summary["ambiguous_1m_bars"] == 1,
                 f"{tr.exit_reason} @ {tr.raw_exit_price:g}, ambiguous_1m_bars={r.summary['ambiguous_1m_bars']}"))

    # Slippage: 5 bps on both fills, TP exit.
    cfg = synth_cfg(slippage_bps=5.0)
    r = synth_run(cfg, [(1000, 1001, 999, 1000), (1000, 1101, 1000, 1100)])
    tr = r.trades.iloc[0]
    qty = tr.contracts * 0.01
    exp_slip = qty * (1000 * 0.0005 + tr.raw_exit_price * 0.0005)
    ok = (math.isclose(tr.entry_price, 1000.5) and math.isclose(tr.exit_price, tr.raw_exit_price * 0.9995)
          and math.isclose(tr.slippage_usd, exp_slip) and not audit_result(r))
    rows.append(("5 bps adverse slippage on entry & exit, audited", ok,
                 f"entry 1000 -> {tr.entry_price:g}, TP {tr.raw_exit_price:g} -> {tr.exit_price:.5f}, slippage ${tr.slippage_usd:.6f}"))

    # Mark trigger, traded fill: mark low hits the 980 stop but the traded low is 985 -> fill from traded range (985).
    cfg = synth_cfg(trigger_source="mark", mark_fill_mode="traded")
    r = synth_run(cfg, [(1000, 1001, 999, 1000), (1000, 1001, 985, 990)], mark={1: (1000, 1001, 979, 988)})
    tr = r.trades.iloc[0]
    rows.append(("mark-triggered stop fills from the traded 1m range", tr.raw_exit_price == 985.0 and tr.exit_reason == "SL",
                 f"mark low 979 <= stop {tr.initial_sl:g}; traded low 985 -> fill {tr.raw_exit_price:g}"))

    # Strict lot cap: 1 contract exceeds a tiny risk budget -> skipped, not rounded up.
    # ₹425 = $5: one lot's margin ($1) fits, but its SL loss (~$0.21) exceeds the 1% budget ($0.05).
    cfg = synth_cfg(initial_capital_inr=425.0, risk_per_trade_pct=1.0)
    r = synth_run(cfg, [(1000, 1001, 999, 1000), (1000, 1001, 999, 1000)])
    legacy = synth_run(synth_cfg(initial_capital_inr=425.0, allow_min_one_lot=True), [(1000, 1001, 999, 1000), (1000, 1001, 999, 1000)])
    rows.append(("strict lot cap skips instead of rounding up to 1 lot",
                 r.summary["total_trades"] == 0 and r.summary["skipped_lot_budget"] == 1
                 and legacy.summary["min_lot_floor_used"] == 1,
                 f"₹425 capital, 1% budget ₹4.25 < 1-lot SL loss: strict skipped_lot_budget={r.summary['skipped_lot_budget']}, "
                 f"legacy floor used={legacy.summary['min_lot_floor_used']}"))

    for name, ok, detail in rows:
        check("engine", name, ok, detail)
    console.print(rich_table(pd.DataFrame([(n, "PASS" if o else "FAIL", d) for n, o, d in rows], columns=["test", "result", "detail"])))
    console.print()


# --------------------------------------------------------------------------- #
def main() -> int:
    t_start = time.perf_counter()
    RESULTS.mkdir(parents=True, exist_ok=True)
    anchor3c = json.loads((RESULTS / "step3c_windows.json").read_text(encoding="utf-8"))
    pin_hist = pd.Timestamp(anchor3c["warmup_start"])
    pin_start, pin_end = pd.Timestamp(anchor3c["start"]), pd.Timestamp(anchor3c["end"])

    # ------------------------------------------------------------------ data
    console.rule("[bold]1. Dataset: bounded history extension toward 365 days (+45 warm-up)")
    dm.fetch_and_save_metadata(settings.SYMBOLS)
    dm.download_all(settings.SYMBOLS, TARGET_DAYS + WARMUP_DAYS)
    cov_rows = []
    for sym in settings.SYMBOLS:
        for st in dm.SERIES_TYPES:
            g = dm.find_gaps(dm.load_raw_1m(sym, st)["time"])
            cov_rows.append((sym, st, g.rows, str(pd.Timestamp(g.start, unit="s")), str(pd.Timestamp(g.end, unit="s")),
                             round(g.rows / 1440, 2), g.gap_count))
    cov = pd.DataFrame(cov_rows, columns=["symbol", "series", "rows", "earliest (UTC)", "latest (UTC)", "days", "gaps"])
    console.print(rich_table(cov))
    full = {s: dm.load_unified_1m_dataset(s) for s in settings.SYMBOLS}
    ext_hist = max(df.index[0] for df in full.values())
    ext_end = min(df.index[-1] for df in full.values()) + pd.Timedelta(minutes=1)
    ext_days = int((ext_end - (ext_hist + pd.Timedelta(days=WARMUP_DAYS))) / pd.Timedelta(days=1))
    ext_start = ext_end - pd.Timedelta(days=ext_days)
    anchor4b = {"warmup_start": ext_hist.isoformat(), "start": ext_start.isoformat(), "end": ext_end.isoformat(),
                "days": ext_days, "warmup_days_min": WARMUP_DAYS, "created": datetime.now(timezone.utc).isoformat(),
                "pinned_step3c": {"warmup_start": pin_hist.isoformat(), "start": pin_start.isoformat(), "end": pin_end.isoformat()}}
    (RESULTS / "step4b_windows.json").write_text(json.dumps(anchor4b, indent=2), encoding="utf-8")
    console.print(f"Pinned primary benchmark: {pin_start} -> {pin_end} (warm-up from {pin_hist}, 180 days)")
    console.print(f"Extended history:         {ext_start} -> {ext_end} ({ext_days} days, warm-up from {ext_hist})"
                  + ("" if ext_days >= TARGET_DAYS else f"  [yellow]short of {TARGET_DAYS} target[/]"))
    check("data", f"extended history covers >= {TARGET_DAYS} days after warm-up", ext_days >= TARGET_DAYS, f"{ext_days} days")
    check("data", "0 gaps in TRADE/MARK/FUNDING", int(cov["gaps"].sum()) == 0, f"{int(cov['gaps'].sum())} gaps")

    frames_pin = {s: Frames(s, df[df.index >= pin_hist]) for s, df in full.items()}
    frames_ext = {s: Frames(s, df[df.index >= ext_hist]) for s, df in full.items()}
    windows = {"pinned_180d": (frames_pin, pin_start, pin_end), "full_extended": (frames_ext, ext_start, ext_end)}

    src_files = ["config.py", "data_manager.py", "strategies.py", "backtester.py", "app.py", "step4b_research.py"]
    hashes = {f: sha256_file(PROJECT_ROOT / f)[:16] for f in src_files}
    fps = {(s, w): data_fingerprint(fr[s].hist, a, b) for w, (fr, a, b) in windows.items() for s in settings.SYMBOLS}
    primary_cfg = step4b_cfg(PRIMARY["risk"], PRIMARY["trigger"], PRIMARY["slip"])
    console.print(f"No git repository: SHA-256 (first 16 hex) of sources: {hashes}")
    console.print(f"Data fingerprints: { {f'{s}/{w}': v for (s, w), v in fps.items()} }")
    console.print(f"Primary config identity: {cfg_hash(primary_cfg)}\n")

    # ------------------------------------------------------------------ baseline reproduction
    console.rule("[bold]2. Baseline reproduction on the pinned 180-day window (legacy engine semantics)")
    base_rows = []
    eth = frames_pin["ETHUSD"]
    for label, risk, fee, trig, e_net, e_tr, e_pf, e_dd in UI_BASELINES:
        cfg = make_cfg(LEGACY_CAPITAL, risk, fee, trig, 0.0, strict=False, mark_fill="trigger")
        r, _ = eth.run(cfg, "A_baseline", 0.0, pin_start, pin_end)
        s = r.summary
        ok = (round(s["net_pnl_inr"], 2) == e_net and s["total_trades"] == e_tr and round(s["profit_factor"], 2) == e_pf
              and round(s["max_drawdown_pct"], 2) == e_dd and not audit_result(r))
        check("baseline", f"UI run {label}", ok, f"₹{s['net_pnl_inr']:,.2f} / {s['total_trades']} / PF {s['profit_factor']:.2f} / DD {s['max_drawdown_pct']:.2f}%")
        base_rows.append({"kind": "baseline_10k", "symbol": "ETHUSD", "label": label, "capital": LEGACY_CAPITAL, "risk_pct": risk,
                          "fee_mode": fee, "trigger": trig, "expected_net": e_net, "net_pnl_inr": s["net_pnl_inr"],
                          "expected_trades": e_tr, "total_trades": s["total_trades"], "expected_pf": e_pf,
                          "profit_factor": s["profit_factor"], "expected_dd": e_dd, "max_drawdown_pct": s["max_drawdown_pct"],
                          "match": ok, "config_id": cfg_hash(cfg)})
    for sym in settings.SYMBOLS:
        for risk in RISKS:
            for trig in TRIGGERS:
                legacy = make_cfg(CAPITAL, risk, "taker", trig, 0.0, strict=False, mark_fill="trigger")
                strict = step4b_cfg(risk, trig, 0.0)
                rl, _ = frames_pin[sym].run(legacy, "A_baseline", 0.0, pin_start, pin_end)
                rs, _ = frames_pin[sym].run(strict, "A_baseline", 0.0, pin_start, pin_end)
                base_rows.append({"kind": "baseline_50k", "symbol": sym, "label": f"₹50k {risk:g}% taker {trig}", "capital": CAPITAL,
                                  "risk_pct": risk, "fee_mode": "taker", "trigger": trig,
                                  "net_pnl_inr": rl.summary["net_pnl_inr"], "total_trades": rl.summary["total_trades"],
                                  "profit_factor": rl.summary["profit_factor"], "max_drawdown_pct": rl.summary["max_drawdown_pct"],
                                  "step4b_semantics_net": rs.summary["net_pnl_inr"], "step4b_semantics_trades": rs.summary["total_trades"],
                                  "step4b_semantics_pf": rs.summary["profit_factor"], "step4b_semantics_dd": rs.summary["max_drawdown_pct"],
                                  "skipped_lot_budget": rs.summary["skipped_lot_budget"], "min_lot_floor_used": rl.summary["min_lot_floor_used"],
                                  "match": not audit_result(rl) and not audit_result(rs), "config_id": cfg_hash(strict)})
    base = pd.DataFrame(base_rows)
    b10 = base[base["kind"] == "baseline_10k"][["label", "expected_net", "net_pnl_inr", "expected_trades", "total_trades",
                                                "expected_pf", "profit_factor", "expected_dd", "max_drawdown_pct", "match"]]
    console.print(rich_table(b10, "₹10,000 UI runs (ETHUSD, pinned window)"))
    b50 = base[base["kind"] == "baseline_50k"][["symbol", "label", "net_pnl_inr", "total_trades", "profit_factor", "max_drawdown_pct",
                                                "step4b_semantics_net", "step4b_semantics_trades", "skipped_lot_budget", "min_lot_floor_used"]]
    console.print(rich_table(b50, "₹50,000 baselines (pinned window). Left: legacy UI semantics; right: strict lot cap + traded mark fills"))
    console.print()

    # ------------------------------------------------------------------ invariant 1: 4h availability
    console.rule("[bold]Invariant 1. Closed-4h availability-time join: truncation parity + bar-level timing")
    for sym in settings.SYMBOLS:
        fr = frames_ext[sym]
        full_sig = {v: fr.signal(v, 5.0)[0] for v in VARIANTS}
        mism = comps = 0
        for frac in (0.40, 0.65, 0.85, 1.00):
            k = int(len(fr.hist) * frac)
            trunc = Frames(sym, fr.hist.iloc[:k])
            for v in VARIANTS:
                s_t = trunc.signal(v, 5.0)[0]
                s_f = full_sig[v].reindex(s_t.index)
                mism += int((s_t != s_f).sum())
                cols = ["allow_long", "allow_short"]
                mism += int((trunc.trend[cols] != fr.trend[cols].reindex(trunc.trend.index)).to_numpy().sum())
                comps += len(s_t)
        check("lookahead", f"{sym}: 4h-filtered 1h signals identical on 40/65/85/100% truncations", mism == 0,
              f"{comps:,} bar comparisons, {mism} mismatches")
        tr = fr.trend.dropna(subset=["src_4h_open"])
        lag = tr["close_time"] - tr["src_4h_open"]
        never_forming = bool((tr["src_4h_open"] + pd.Timedelta(hours=4) <= tr["close_time"]).all())
        latest = bool((lag < pd.Timedelta(hours=8)).mean() > 0.99)
        hrs = tr["close_time"].dt.hour % 4
        intra = tr[hrs.isin([1, 2, 3])]
        intra_ok = bool((intra["src_4h_open"] < intra["close_time"].dt.floor("4h")).all())
        check("lookahead", f"{sym}: 1h bars closing at 01/02/03h (mod 4h) never read the forming 4h bar", never_forming and intra_ok,
              f"{len(intra):,} such bars; all use a 4h bar closed <= their close")
        check("lookahead", f"{sym}: join uses the latest closed 4h bar (lag < 8h for >99% of bars)", latest,
              f"median lag {lag.median()}")
    sample = frames_ext["BTCUSD"].trend
    day = sample[(sample.index >= ext_end.floor("D") - pd.Timedelta(days=2)) & (sample.index < ext_end.floor("D") - pd.Timedelta(days=2) + pd.Timedelta(hours=6))]
    ex = pd.DataFrame({"1h bar open": day.index.strftime("%m-%d %H:%M"), "1h close": day["close_time"].dt.strftime("%H:%M"),
                       "4h bar used (open)": day["src_4h_open"].dt.strftime("%m-%d %H:%M"),
                       "4h bar available at": day["avail_time"].dt.strftime("%H:%M"),
                       "allow long": day["allow_long"], "allow short": day["allow_short"]})
    console.print(rich_table(ex, "BTCUSD sample: which closed 4h bar each 1h signal bar can see"))
    console.print()

    test_engine_paths()

    # ------------------------------------------------------------------ main grid
    console.rule("[bold]3. Variants A-D x slippage {0,2,5} bps x risk {1%,2%} x trigger {trade,mark} x symbol x window (₹50,000)")
    t0 = time.perf_counter()
    rows: list[dict[str, Any]] = []
    for wname, (frs, a, b) in windows.items():
        bh = {s: buy_and_hold(frs[s].hist, a, b, CAPITAL) for s in settings.SYMBOLS}
        for sym in settings.SYMBOLS:
            for variant in VARIANTS:
                for slip in SLIPPAGE_BPS:
                    for risk in RISKS:
                        for trig in TRIGGERS:
                            cfg = step4b_cfg(risk, trig, slip)
                            r, stats = frs[sym].run(cfg, variant, slip, a, b)
                            rows.append({"kind": "variant", "window": wname, "start": a.isoformat(), "end": b.isoformat(),
                                         "symbol": sym, "variant": variant, "slippage_bps": slip, "risk_pct": risk,
                                         "trigger": trig, "config_id": cfg_hash(cfg), "data_fingerprint": fps[(sym, wname)],
                                         **metrics(r), **{f"filter_{k}": v for k, v in stats.items()},
                                         "cash_net_inr": 0.0, **bh[sym]})
    comp = pd.DataFrame(rows)
    console.print(f"{len(comp)} end-to-end runs in {time.perf_counter() - t0:.1f}s; ledger audits passed: "
                  f"{int(comp['audit_ok'].sum())}/{len(comp)}; max |initial + Σnet − final| = ₹{comp['equity_identity_abs_diff'].max():.2e}")
    check("audit", "audit_result() passes on every Step 4B run", bool(comp["audit_ok"].all()), f"{int(comp['audit_ok'].sum())}/{len(comp)}")
    check("audit", "equity identity initial + Σnet = final (<= 1e-9 x capital)",
          bool((comp["equity_identity_abs_diff"] <= 1e-9 * CAPITAL).all()), f"max abs diff ₹{comp['equity_identity_abs_diff'].max():.2e}")

    mm = float(dm.load_metadata("BTCUSD").get("maintenance_margin", 0.25))
    worst = primary_cfg.atr_sl_max_pct + 2 * PRIMARY["slip"] / 100 + 2 * settings.TAKER_FEE * (1 + settings.GST_ON_FEES) * 100
    room = 100 / LEVERAGE - mm
    check("audit", "max stop distance (+fees+slippage) inside 10x liquidation room (IM 10% - MM)", worst < room,
          f"{worst:.3f}% < {room:.2f}% (MM {mm}% from product metadata); intra-trade liquidation approximated by this guard")

    prim = comp[(comp["risk_pct"] == PRIMARY["risk"]) & (comp["trigger"] == PRIMARY["trigger"])]
    for wname in windows:
        sub = prim[prim["window"] == wname]
        view = sub[["symbol", "variant", "slippage_bps", "total_trades", "net_pnl_inr", "net_return_pct", "gross_pnl_inr", "fees_gst_inr",
                    "funding_inr", "slippage_inr", "profit_factor", "max_drawdown_pct", "exposure_pct", "bh_return_pct"]]
        console.print(rich_table(view, f"{wname}: ₹50,000, 1% risk, all-taker, trade trigger (vs 1x buy & hold)"))
    filt = prim[(prim["window"] == "full_extended") & (prim["slippage_bps"] == 5.0)][
        ["symbol", "variant", "filter_raw_transitions", "filter_rejected_by_trend4h", "filter_rejected_by_costgate",
         "filter_rejected_by_all", "filter_raw_transitions_passed", "filter_entries_fired",
         "filter_entries_fired_not_at_raw_transition", "total_trades", "skipped_lot_budget"]].fillna(0)
    console.print(rich_table(filt, "Filter accounting (full extended, 5 bps): raw breakout transitions vs rejections vs entries", "{:,.0f}"))
    rk = prim[(prim["window"] == "full_extended") & (prim["slippage_bps"] == 5.0)][
        ["symbol", "variant", "risk_pct_min", "risk_pct_median", "risk_pct_max", "contracts_min", "contracts_median", "contracts_max",
         "skipped_lot_budget", "long_trades", "long_net_inr", "short_trades", "short_net_inr", "top3_concentration_pct",
         "boot_p5", "boot_p50", "boot_p95"]]
    console.print(rich_table(rk, "Sizing, direction split, concentration, block bootstrap (full extended, 5 bps)"))
    console.print()

    # ------------------------------------------------------------------ fixed-trade attribution
    console.rule("[bold]4. Fixed-trade cost attribution (Variant A trades at 0 bps, re-priced with 2 / 5 bps)")
    fx_rows = []
    fe = settings.TAKER_FEE * (1 + settings.GST_ON_FEES)
    for wname, (frs, a, b) in windows.items():
        for sym in settings.SYMBOLS:
            r, _ = frs[sym].run(step4b_cfg(1.0, "trade", 0.0), "A_baseline", 0.0, a, b)
            t = r.trades
            d = np.where(t["side"] == "LONG", 1.0, -1.0)
            qty = t["contracts"] * t["contract_value"]
            for slip in SLIPPAGE_BPS:
                s = slip / 10_000
                ent = t["raw_entry_price"] * (1 + d * s)
                ex = t["raw_exit_price"] * (1 - d * s)
                gross = d * qty * (ex - ent)
                fees = qty * ent * fe + qty * ex * fe
                net = (gross - fees + t["funding_usd"]) * settings.FIXED_USD_INR_RATE
                slip_cost = (qty * (t["raw_entry_price"] + t["raw_exit_price"]) * s).sum() * settings.FIXED_USD_INR_RATE
                fx_rows.append({"kind": "fixed_trade_attribution", "window": wname, "symbol": sym, "variant": "A_baseline",
                                "slippage_bps": slip, "risk_pct": 1.0, "trigger": "trade", "total_trades": len(t),
                                "net_pnl_inr": float(net.sum()), "slippage_inr": float(slip_cost),
                                "delta_vs_0bps_inr": float(net.sum() - r.summary["net_pnl_inr"])})
            check("attribution", f"{wname} {sym}: 0 bps re-pricing reproduces the engine net",
                  math.isclose(fx_rows[-3]["net_pnl_inr"], r.summary["net_pnl_inr"], rel_tol=1e-9, abs_tol=1e-6),
                  f"₹{fx_rows[-3]['net_pnl_inr']:,.4f} vs ₹{r.summary['net_pnl_inr']:,.4f}")
    fxa = pd.DataFrame(fx_rows)
    console.print(rich_table(fxa[["window", "symbol", "slippage_bps", "total_trades", "net_pnl_inr", "slippage_inr", "delta_vs_0bps_inr"]]))
    dyn = prim[prim["variant"] == "A_baseline"][["window", "symbol", "slippage_bps", "total_trades", "net_pnl_inr", "slippage_inr"]]
    console.print(rich_table(dyn, "End-to-end dynamic runs, same variant (sizing, compounding and gates evolve)"))
    console.print()

    # ------------------------------------------------------------------ diagnostic windows
    console.rule("[bold]5. Retrospective diagnostic windows on the extended history (no retuning; NOT untouched OOS)")
    diag_rows = []
    k30, k60 = ext_days // 30, ext_days // 60
    specs = ([("slice30", i, ext_start + pd.Timedelta(days=30 * i), ext_start + pd.Timedelta(days=30 * (i + 1))) for i in range(k30)]
             + [("slice60", i, ext_start + pd.Timedelta(days=60 * i), ext_start + pd.Timedelta(days=60 * (i + 1))) for i in range(k60)]
             + [("expanding30", i, ext_start, ext_start + pd.Timedelta(days=30 * (i + 1))) for i in range(k30)])
    t0 = time.perf_counter()
    for sym in settings.SYMBOLS:
        fr = frames_ext[sym]
        for wtype, i, a, b in specs:
            bh = buy_and_hold(fr.hist, a, b, CAPITAL)
            for variant in VARIANTS:
                for slip in SLIPPAGE_BPS:
                    r, stats = fr.run(step4b_cfg(1.0, "trade", slip), variant, slip, a, b)
                    s = r.summary
                    diag_rows.append({"window_type": wtype, "window_index": i + 1, "label": f"{'M' if wtype == 'slice30' else ('B' if wtype == 'slice60' else 'E')}{i + 1}",
                                      "start": a.isoformat(), "end": b.isoformat(), "symbol": sym, "variant": variant,
                                      "slippage_bps": slip, "total_trades": s["total_trades"], "net_pnl_inr": s["net_pnl_inr"],
                                      "net_return_pct": s["net_return_pct"], "profit_factor": s["profit_factor"],
                                      "max_drawdown_pct": s["max_drawdown_pct"], "fees_gst_inr": s["fees_gst_inr"],
                                      "slippage_inr": s["slippage_inr"], "funding_inr": s["funding_inr"],
                                      "audit_ok": not audit_result(r), "bh_return_pct": bh["bh_return_pct"]})
    diag = pd.DataFrame(diag_rows)
    console.print(f"{len(diag)} diagnostic runs in {time.perf_counter() - t0:.1f}s; audits passed {int(diag['audit_ok'].sum())}/{len(diag)}")
    check("audit", "audit_result() passes on every diagnostic-window run", bool(diag["audit_ok"].all()))
    d5 = diag[diag["slippage_bps"] == 5.0]
    piv = d5[d5["window_type"] == "slice30"].pivot_table(index=["symbol", "variant"], columns="label", values="net_pnl_inr", sort=False)
    piv = piv[[f"M{i + 1}" for i in range(k30)]].reset_index()
    console.print(rich_table(piv, "Consecutive 30-day slices, net ₹ at 5 bps (₹50,000, 1% risk)", "{:,.0f}"))
    piv60 = d5[d5["window_type"] == "slice60"].pivot_table(index=["symbol", "variant"], columns="label", values="net_pnl_inr", sort=False)
    piv60 = piv60[[f"B{i + 1}" for i in range(k60)]].reset_index()
    console.print(rich_table(piv60, "Consecutive 60-day slices, net ₹ at 5 bps", "{:,.0f}"))
    console.print()

    # ------------------------------------------------------------------ verdict
    console.rule("[bold]6. Predeclared screen -> CANDIDATE / REJECT (primary: ₹50,000, 1%, all-taker, 5 bps, full extended history)")
    verdict_rows = []
    for sym in settings.SYMBOLS:
        for variant in VARIANTS:
            row = prim[(prim["window"] == "full_extended") & (prim["symbol"] == sym) & (prim["variant"] == variant)
                       & (prim["slippage_bps"] == 5.0)].iloc[0]
            w60 = d5[(d5["window_type"] == "slice60") & (d5["symbol"] == sym) & (d5["variant"] == variant)]
            w30 = d5[(d5["window_type"] == "slice30") & (d5["symbol"] == sym) & (d5["variant"] == variant)]
            share60 = float((w60["net_pnl_inr"] > 0).mean())
            share30 = float((w30["net_pnl_inr"] > 0).mean())
            pf = row["profit_factor"]
            crit = {
                "net>0 @5bps": row["net_pnl_inr"] > 0,
                "PF>=1.20": (pf if isinstance(pf, float) else float("nan")) >= RUBRIC["min_pf"],
                "DD<=15%": row["max_drawdown_pct"] <= RUBRIC["max_dd"],
                "trades>=50": row["total_trades"] >= RUBRIC["min_trades"],
                ">=2/3 of 60d windows +": share60 >= RUBRIC["min_window_share"] - 1e-12,
            }
            verdict = "CANDIDATE" if all(crit.values()) else "REJECT"
            verdict_rows.append({"symbol": sym, "variant": variant, "net_pnl_inr": row["net_pnl_inr"], "profit_factor": pf,
                                 "max_drawdown_pct": row["max_drawdown_pct"], "total_trades": int(row["total_trades"]),
                                 "win60_share": share60, "win30_share": share30,
                                 **{k: "pass" if v else "FAIL" for k, v in crit.items()}, "verdict": verdict,
                                 "small_sample_warning": row["total_trades"] < RUBRIC["min_trades"]})
    ver = pd.DataFrame(verdict_rows)
    comp = comp.merge(ver[["symbol", "variant", "verdict"]], on=["symbol", "variant"], how="left")
    console.print(rich_table(ver.drop(columns=["small_sample_warning"])))

    port_rows = []
    both = [v for v in VARIANTS if (ver[(ver["variant"] == v)]["verdict"] == "CANDIDATE").all()]
    if both:
        for v in both:
            for wname, (frs, a, b) in windows.items():
                cfg = primary_cfg
                legs = []
                for sym in settings.SYMBOLS:
                    sig, _ = frs[sym].signal(v, 5.0)
                    legs.append(Leg(frs[sym].product, frs[sym].hist, sig, TF, frs[sym].atr, STRATEGY, None, cfg))
                r = Backtester(cfg).run_portfolio(legs, a, b)
                port_rows.append({"kind": "portfolio", "window": wname, "symbol": "+".join(settings.SYMBOLS), "variant": v,
                                  "slippage_bps": 5.0, "risk_pct": 1.0, "trigger": "trade", **metrics(r)})
        console.print(rich_table(pd.DataFrame(port_rows)[["window", "variant", "total_trades", "net_pnl_inr", "profit_factor",
                                                          "max_drawdown_pct", "audit_ok"]], "Shared ₹50,000 portfolio"))
    else:
        console.print("No variant passes the screen on BOTH symbols -> shared-wallet portfolio not run (per protocol).")

    # ------------------------------------------------------------------ outputs
    out_comp = pd.concat([base, comp, fxa, pd.DataFrame(port_rows)], ignore_index=True, sort=False)
    out_comp.to_csv(RESULTS / "step4b_comparison.csv", index=False)
    diag.to_csv(RESULTS / "step4b_expanding_windows.csv", index=False)
    write_report(cov, anchor4b, hashes, fps, primary_cfg, base, comp, prim, fxa, diag, piv, piv60, ver, port_rows, ext_days)

    console.rule("[bold]Checks")
    ct = pd.DataFrame([(s, n, "PASS" if o else "FAIL", d) for s, n, o, d in CHECKS], columns=["section", "check", "result", "detail"])
    console.print(rich_table(ct))
    failed = [c for c in CHECKS if not c[2]]
    cands = ver[ver["verdict"] == "CANDIDATE"]
    console.print(f"\nOutputs: {RESULTS / 'step4b_comparison.csv'}, {RESULTS / 'step4b_expanding_windows.csv'}, "
                  f"{RESULTS / 'step4b_windows.json'}, {RESULTS / 'STEP4B_REPORT.md'}  ({time.perf_counter() - t_start:.0f}s)")
    console.rule(f"Step 4B: {len(CHECKS) - len(failed)}/{len(CHECKS)} checks pass  |  verdict: "
                 + (", ".join(f"{r.symbol} {r.variant}" for r in cands.itertuples()) + " CANDIDATE" if len(cands) else "no CANDIDATE (all REJECT)"))
    return 0 if not failed else 1


# --------------------------------------------------------------------------- #
def write_report(cov, anchor4b, hashes, fps, primary_cfg, base, comp, prim, fxa, diag, piv, piv60, ver, port_rows, ext_days) -> None:
    L: list[str] = []
    cands = ver[ver["verdict"] == "CANDIDATE"]
    L += ["# Step 4B report — ₹50,000 capital, 4h trend filter, volatility/cost gate, slippage stress", "",
          f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} by `step4b_research.py`. Research only; live trading stays disabled.", ""]
    L += ["## Verdict", ""]
    if len(cands):
        L += ["Variants passing every predeclared criterion (frozen for unseen forward paper observation, no further tuning):", ""]
        for r in cands.itertuples():
            L.append(f"- **{r.symbol} {r.variant}** — net ₹{r.net_pnl_inr:,.2f}, PF {r.profit_factor:.2f}, max DD {r.max_drawdown_pct:.2f}%, "
                     f"{r.total_trades} trades, {r.win60_share:.0%} of 60-day windows profitable.")
        L += ["", "Frozen specification: `bollinger_volume_breakout` 1h, ATR SL 2.0x / TP 5.0x (stop clamp 0.8–4.0%), 2.5x ATR trailing, "
              "cooldown 1 bar, daily-loss stop 5%, ₹50,000, 10x, 1% risk with strict whole-lot cap, all-taker fees + 18% GST; "
              f"filters per variant name; cost-gate multiplier {GATE_MULT}. Config id `{cfg_hash(primary_cfg)}`.", ""]
    else:
        L += ["**No CANDIDATE.** Every variant on both symbols fails at least one predeclared criterion under the primary screen "
              "(₹50,000, 1% risk, all-taker + GST, funding, 5 bps adverse slippage per fill, full extended history). "
              "Per protocol no further grid search was run.", ""]
    L += [md_table(ver.drop(columns=["small_sample_warning"])), ""]
    L += ["Screen: net > 0 at 5 bps; profit factor ≥ 1.20; max drawdown ≤ 15%; ≥ 50 trades; net-profitable in ≥ 2/3 of the "
          "consecutive 60-day windows of the extended history (30-day share shown for information). "
          "These windows are retrospective diagnostics on data that informed earlier steps — not untouched out-of-sample data.", ""]

    L += ["## Dataset and identity", "",
          f"- Extended history: `{anchor4b['start']}` → `{anchor4b['end']}` ({ext_days} days) after indicator warm-up from `{anchor4b['warmup_start']}`.",
          f"- Pinned primary benchmark (Step 3C): `{anchor4b['pinned_step3c']['start']}` → `{anchor4b['pinned_step3c']['end']}`, warm-up from `{anchor4b['pinned_step3c']['warmup_start']}`.",
          "- No git repository; source SHA-256 (first 16 hex): " + ", ".join(f"`{k}` {v}" for k, v in hashes.items()) + ".",
          "- Window data fingerprints: " + ", ".join(f"{s}/{w} `{v}`" for (s, w), v in fps.items()) + ".",
          f"- Primary config identity `{cfg_hash(primary_cfg)}`: " + ", ".join(f"{k}={v}" for k, v in asdict(primary_cfg).items()) + ".", "",
          md_table(cov), "",
          "The FUNDING series had 33 one-minute holes per symbol at download-chunk starts before Oct 2025 (the API omits the "
          "first minute of a query that starts exactly on a funding boundary in older history). `data_manager` now re-fetches "
          "gaps from one funding interval earlier; all series have 0 gaps.", ""]

    b10 = base[base["kind"] == "baseline_10k"][["label", "expected_net", "net_pnl_inr", "expected_trades", "total_trades",
                                                "expected_pf", "profit_factor", "expected_dd", "max_drawdown_pct", "match"]]
    b50 = base[base["kind"] == "baseline_50k"][["symbol", "label", "net_pnl_inr", "total_trades", "profit_factor", "max_drawdown_pct",
                                                "step4b_semantics_net", "step4b_semantics_trades", "step4b_semantics_pf", "step4b_semantics_dd"]]
    L += ["## Baseline reproduction (pinned 180 days, ETHUSD bollinger 1h ATR 2x/5x trail 2.5x)", "", md_table(b10), "",
          "₹50,000 baselines. Left block: legacy UI semantics (min-1-lot floor allowed, mark-triggered stops fill at the level). "
          "Right block: Step 4B semantics (strict whole-lot risk cap, mark-triggered fills from the traded 1m range).", "", md_table(b50), ""]

    for wname in ("pinned_180d", "full_extended"):
        sub = prim[prim["window"] == wname][["symbol", "variant", "slippage_bps", "total_trades", "net_pnl_inr", "net_return_pct",
                                             "gross_pnl_inr", "fees_gst_inr", "funding_inr", "slippage_inr", "profit_factor",
                                             "max_drawdown_pct", "exposure_pct", "bh_return_pct"]]
        L += [f"## Variants — {wname} (₹50,000, 1% risk, all-taker, trade trigger)", "",
              "Benchmarks: cash = ₹0; `bh_return_pct` = unlevered 1x buy & hold after round-trip taker fee + GST (no funding).", "",
              md_table(sub), ""]
    sec = comp[(comp["kind"] == "variant") & (comp["window"] == "full_extended") & (comp["slippage_bps"] == 5.0)][
        ["symbol", "variant", "risk_pct", "trigger", "total_trades", "net_pnl_inr", "profit_factor", "max_drawdown_pct"]]
    L += ["## Secondary settings at 5 bps (full extended): 2% risk and mark-price triggers", "", md_table(sec), ""]
    filt = prim[(prim["window"] == "full_extended") & (prim["slippage_bps"] == 5.0)][
        ["symbol", "variant", "filter_raw_transitions", "filter_rejected_by_trend4h", "filter_rejected_by_costgate",
         "filter_rejected_by_all", "filter_entries_fired", "filter_entries_fired_not_at_raw_transition", "total_trades", "skipped_lot_budget"]].fillna(0)
    L += ["## Filter accounting (full extended, 5 bps)", "",
          "`raw_transitions` = bars where the unfiltered breakout condition turns true. Rejections are counted at those bars "
          "(`rejected_by_all` = blocked by both filters). Filters are applied before the edge trigger, so an entry can also fire "
          "later when a filter turns eligible while the breakout condition is still true (`fired_not_at_raw_transition`). "
          "Entries fired can exceed trades because signals arriving during an open position or cooldown are skipped.", "",
          md_table(filt, "{:,.0f}"), ""]
    rk = prim[(prim["window"] == "full_extended") & (prim["slippage_bps"] == 5.0)][
        ["symbol", "variant", "risk_pct_min", "risk_pct_median", "risk_pct_max", "contracts_min", "contracts_median", "contracts_max",
         "long_trades", "long_net_inr", "short_trades", "short_net_inr", "top3_concentration_pct", "boot_p5", "boot_p50", "boot_p95"]]
    L += ["## Sizing, direction, concentration and bootstrap (full extended, 5 bps)", "",
          f"Realized risk = contracts × SL loss per contract (price move + fees with GST + 2 × slippage) / equity at entry. "
          f"Top-3 concentration = sum of the 3 best trades / total net (> 100% means all other trades lost money net). "
          f"Bootstrap: stationary block bootstrap of trade net PnL, mean block {BOOT_BLOCK} trades, {BOOT_N} resamples "
          "(ignores compounding).", "", md_table(rk), ""]
    L += ["## Cost sensitivity", "", "Fixed-trade attribution: Variant A's exact 0-bps trades re-priced with adverse slippage held fixed.", "",
          md_table(fxa[["window", "symbol", "slippage_bps", "total_trades", "net_pnl_inr", "slippage_inr", "delta_vs_0bps_inr"]]), ""]
    L += ["## Retrospective diagnostic windows (extended history, 5 bps, ₹50,000, 1%)", "", "30-day slices (net ₹):", "",
          md_table(piv, "{:,.0f}"), "", "60-day slices (net ₹):", "", md_table(piv60, "{:,.0f}"), "",
          "All slices, expanding windows and 0/2 bps runs are in `step4b_expanding_windows.csv`.", ""]
    if port_rows:
        L += ["## Shared ₹50,000 portfolio", "", md_table(pd.DataFrame(port_rows)[["window", "variant", "total_trades", "net_pnl_inr",
                                                                                   "profit_factor", "max_drawdown_pct"]]), ""]
    L += ["## Invariant and execution tests", "", md_table(pd.DataFrame([(s, n, "PASS" if o else "FAIL", d) for s, n, o, d in CHECKS],
                                                                       columns=["section", "check", "result", "detail"])), "",
          "## Caveats", "",
          "- Exchange liquidation is not simulated; it is approximated by requiring the widest stop (+ fees + slippage) to sit inside "
          "the 10x initial-margin minus maintenance-margin distance. Gaps larger than that would be understated.",
          "- Slippage is a flat adverse bps per fill; real impact grows with size and volatility.",
          "- All results are historical simulations on 1m candles (intrabar order unknown; SL-first on ambiguity).",
          "- Variant specs and thresholds were fixed before this run; the extended history overlaps data used in earlier steps."]
    (RESULTS / "STEP4B_REPORT.md").write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
