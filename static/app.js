"use strict";

/* Delta Futures Lab - Step 10 Custom Strategy Builder, Save-Only Results Catalog & Modern Research Lab UI. */

const $ = (id) => document.getElementById(id);
const state = {
  status: null,
  customStrategies: [],
  savedResults: [],
  presets: [],
  anchor: null,
  result: null,
  resultSaved: false,
  indicatorsVisible: true,
  chartSymbol: null,
  legsOverride: null,
  selectedTrade: null,
  overlaySeries: [],
  tradeLines: [],
  startLine: null,
  syncTimer: null,
  lb: { source: "step3c", sort: "net", desc: true, columns: [], rows: [], days: 180 },
};

const COLORS = { up: "#10b981", down: "#f43f5e", accent: "#3b82f6", muted: "#94a3b8", grid: "#161f30", border: "#1e293b", panel: "#111827" };
const OVERLAY_STYLE = {
  ema9: { color: "#f5d76e" }, ema21: { color: "#f59e0b" }, ema50: { color: "#a78bfa" }, ema55: { color: "#c084fc" },
  ema100: { color: "#60a5fa" }, ema200: { color: "#e879f9", width: 2 },
  ema_fast: { color: "#38bdf8", width: 2 }, ema_slow: { color: "#f59e0b", width: 2 }, ema_trend: { color: "#a78bfa", style: 2 },
  vwap: { color: "#fbbf24", style: 2 },
  bb_upper: { color: "#38bdf8" }, bb_mid: { color: "#a78bfa", style: 2 }, bb_lower: { color: "#38bdf8" },
  kc_upper: { color: "#f472b6", style: 2 }, kc_lower: { color: "#f472b6", style: 2 },
  dc_high: { color: "#34d399", style: 1 }, dc_low: { color: "#fb7185", style: 1 },
  supertrend_up: { color: "#10b981", width: 2 }, supertrend_down: { color: "#f43f5e", width: 2 },
};

const RULE_PARAM_DEFS = {
  vol_obv_breakout: [
    { key: "obv_span", label: "OBV EMA Span", def: 20, step: 1, min: 5 },
    { key: "rvol_min", label: "Min RVOL ×", def: 1.3, step: 0.1, min: 0.8 },
    { key: "dc_period", label: "Breakout Bars (N)", def: 12, step: 1, min: 0 },
    { key: "ema_filter", label: "Trend EMA", def: 50, step: 5, min: 10 },
    { key: "adx_min", label: "Min ADX", def: 18, step: 1, min: 5 },
    { key: "use_4h_trend", label: "4h Trend (1=on,0=off)", def: 1, step: 1, min: 0 },
  ],
  vol_climax_reversal: [
    { key: "climax_mult", label: "Climax RVOL ×", def: 2.8, step: 0.1, min: 1.5 },
    { key: "wick_ratio", label: "Min Absorption Wick", def: 0.40, step: 0.05, min: 0.15 },
    { key: "bb_period", label: "BB Period", def: 20, step: 1, min: 5 },
    { key: "rsi_os", label: "RSI Oversold", def: 38, step: 1, min: 10 },
    { key: "rsi_ob", label: "RSI Overbought", def: 62, step: 1, min: 50 },
    { key: "use_4h_trend", label: "4h Trend (1=on,0=off)", def: 1, step: 1, min: 0 },
  ],
  vol_vwap_expansion: [
    { key: "rvol_min", label: "Min RVOL Burst ×", def: 2.0, step: 0.1, min: 1.0 },
    { key: "ema_period", label: "EMA Period", def: 20, step: 1, min: 5 },
    { key: "body_ratio", label: "Min Body Ratio", def: 0.50, step: 0.05, min: 0.2 },
    { key: "adx_min", label: "Min ADX", def: 20, step: 1, min: 5 },
    { key: "dc_period", label: "Breakout Bars (N)", def: 10, step: 1, min: 0 },
    { key: "use_4h_trend", label: "4h Trend (1=on,0=off)", def: 1, step: 1, min: 0 },
  ],
  vol_dryup_breakout: [
    { key: "dryup_bars", label: "Dry-Up Bars", def: 3, step: 1, min: 2 },
    { key: "dryup_max_ratio", label: "Max Dry-Up RVOL", def: 0.65, step: 0.05, min: 0.2 },
    { key: "burst_mult", label: "Impulse RVOL ×", def: 2.4, step: 0.1, min: 1.2 },
    { key: "dc_period", label: "Breakout Bars (N)", def: 15, step: 1, min: 4 },
    { key: "adx_min", label: "Min ADX", def: 18, step: 1, min: 5 },
    { key: "use_4h_trend", label: "4h Trend (1=on,0=off)", def: 1, step: 1, min: 0 },
  ],
  meme_liq_snapback: [
    { key: "bb_period", label: "BB Period", def: 20, step: 1, min: 5 },
    { key: "bb_std", label: "BB StdDev", def: 2.0, step: 0.1, min: 0.5 },
    { key: "rsi_period", label: "Fast RSI Period", def: 7, step: 1, min: 2 },
    { key: "rsi_os", label: "RSI Oversold", def: 28, step: 1, min: 5 },
    { key: "rsi_ob", label: "RSI Overbought", def: 72, step: 1, min: 55 },
    { key: "wick_ratio", label: "Min Wick Ratio", def: 0.35, step: 0.05, min: 0.1 },
  ],
  meme_pump_ignition: [
    { key: "bb_period", label: "BB Period", def: 20, step: 1, min: 5 },
    { key: "dc_period", label: "Breakout Bars (N)", def: 15, step: 1, min: 4 },
    { key: "vol_mult", label: "Volume Pump ×", def: 2.2, step: 0.1, min: 1.0 },
    { key: "body_ratio", label: "Min Body Ratio", def: 0.50, step: 0.05, min: 0.2 },
    { key: "adx_min", label: "Min ADX", def: 20, step: 1, min: 5 },
    { key: "use_4h_trend", label: "4h Trend (1=on,0=off)", def: 1, step: 1, min: 0 },
  ],
  alt_ema_wave: [
    { key: "fast_ema", label: "Fast EMA", def: 9, step: 1, min: 2 },
    { key: "mid_ema", label: "Pullback EMA", def: 21, step: 1, min: 5 },
    { key: "slow_ema", label: "Trend EMA", def: 50, step: 5, min: 10 },
    { key: "adx_min", label: "Min ADX", def: 18, step: 1, min: 5 },
    { key: "pullback_tol", label: "Pullback Tol", def: 0.003, step: 0.001, min: 0.0005 },
    { key: "use_4h_trend", label: "4h Trend (1=on,0=off)", def: 1, step: 1, min: 0 },
  ],
  scalp_vwap_ema: [
    { key: "fast_ema", label: "Fast EMA", def: 9, step: 1, min: 2 },
    { key: "slow_ema", label: "Slow EMA", def: 21, step: 1, min: 5 },
    { key: "trend_ema", label: "Trend EMA", def: 50, step: 5, min: 10 },
    { key: "rsi_period", label: "RSI Period", def: 7, step: 1, min: 2 },
    { key: "pullback_tol", label: "Pullback Tol", def: 0.0015, step: 0.0005, min: 0.0005 },
    { key: "adx_min", label: "Min ADX", def: 16, step: 1, min: 5 },
  ],
  scalp_rsi4_bb: [
    { key: "bb_period", label: "BB Period", def: 20, step: 1, min: 5 },
    { key: "bb_std", label: "BB StdDev", def: 2.0, step: 0.1, min: 0.5 },
    { key: "rsi_period", label: "Fast RSI Period", def: 4, step: 1, min: 2 },
    { key: "rsi_os", label: "RSI Oversold", def: 20, step: 1, min: 5 },
    { key: "rsi_ob", label: "RSI Overbought", def: 80, step: 1, min: 60 },
    { key: "ema_filter", label: "Trend EMA", def: 200, step: 10, min: 0 },
  ],
  scalp_vol_burst: [
    { key: "dc_period", label: "Micro Range (N)", def: 15, step: 1, min: 4 },
    { key: "vol_mult", label: "Volume Spike ×", def: 2.0, step: 0.1, min: 1.0 },
    { key: "body_ratio", label: "Min Body Ratio", def: 0.55, step: 0.05, min: 0.2 },
    { key: "adx_min", label: "Min ADX", def: 20, step: 1, min: 5 },
    { key: "cost_gate_mult", label: "Cost Gate ×", def: 1.5, step: 0.2, min: 0 },
    { key: "use_4h_trend", label: "4h Trend (1=on,0=off)", def: 1, step: 1, min: 0 },
  ],
  scalp_ema_ribbon: [
    { key: "ema1", label: "Ribbon EMA 1", def: 8, step: 1, min: 2 },
    { key: "ema2", label: "Ribbon EMA 2", def: 13, step: 1, min: 5 },
    { key: "ema3", label: "Ribbon EMA 3", def: 21, step: 1, min: 8 },
    { key: "ema4", label: "Ribbon EMA 4", def: 55, step: 5, min: 20 },
    { key: "adx_min", label: "Min ADX", def: 18, step: 1, min: 5 },
    { key: "vol_mult", label: "Volume Mult", def: 1.25, step: 0.05, min: 0.5 },
  ],
  bb_squeeze_quick_tp: [
    { key: "bb_period", label: "BB Period", def: 20, step: 1, min: 5 },
    { key: "bb_std", label: "BB StdDev", def: 2.0, step: 0.1, min: 0.5 },
    { key: "adx_min", label: "Min ADX", def: 18, step: 1, min: 5 },
    { key: "vol_mult", label: "Volume Mult", def: 1.25, step: 0.05, min: 0.5 },
    { key: "body_ratio", label: "Min Body Ratio", def: 0.40, step: 0.05, min: 0.1 },
    { key: "cost_gate_mult", label: "Cost Gate ×", def: 2.0, step: 0.2, min: 0 },
    { key: "use_4h_trend", label: "4h Trend (1=on,0=off)", def: 1, step: 1, min: 0 },
  ],
  ema_pullback_scalp: [
    { key: "fast_ema", label: "Pullback EMA", def: 20, step: 1, min: 5 },
    { key: "slow_ema", label: "Trend EMA", def: 50, step: 5, min: 10 },
    { key: "adx_min", label: "Min ADX", def: 18, step: 1, min: 5 },
    { key: "pullback_tol", label: "Pullback Tol", def: 0.002, step: 0.001, min: 0.001 },
    { key: "cost_gate_mult", label: "Cost Gate ×", def: 1.8, step: 0.2, min: 0 },
    { key: "use_4h_trend", label: "4h Trend (1=on,0=off)", def: 1, step: 1, min: 0 },
  ],
  bb_rsi_snapback: [
    { key: "bb_period", label: "BB Period", def: 20, step: 1, min: 5 },
    { key: "bb_std", label: "BB StdDev", def: 2.0, step: 0.1, min: 0.5 },
    { key: "rsi_period", label: "RSI Period", def: 14, step: 1, min: 2 },
    { key: "rsi_os", label: "RSI Oversold", def: 34, step: 1, min: 10 },
    { key: "rsi_ob", label: "RSI Overbought", def: 66, step: 1, min: 50 },
    { key: "cost_gate_mult", label: "Cost Gate ×", def: 1.8, step: 0.2, min: 0 },
  ],
  vwap_st_burst: [
    { key: "atr_period", label: "ST ATR Period", def: 10, step: 1, min: 2 },
    { key: "atr_mult", label: "ST Multiplier", def: 2.5, step: 0.1, min: 0.5 },
    { key: "ema_period", label: "EMA Period", def: 20, step: 1, min: 5 },
    { key: "vol_mult", label: "Volume Mult", def: 1.25, step: 0.05, min: 0.5 },
    { key: "adx_min", label: "Min ADX", def: 18, step: 1, min: 5 },
    { key: "use_4h_trend", label: "4h Trend (1=on,0=off)", def: 1, step: 1, min: 0 },
  ],
  bb_squeeze_adx: [
    { key: "bb_period", label: "BB Period", def: 20, step: 1, min: 5 },
    { key: "bb_std", label: "BB StdDev", def: 2.0, step: 0.1, min: 0.5 },
    { key: "adx_min", label: "Min ADX (Rising)", def: 20, step: 1, min: 5 },
    { key: "vol_mult", label: "Volume Mult", def: 1.25, step: 0.05, min: 0.5 },
    { key: "body_ratio", label: "Min Body Ratio", def: 0.45, step: 0.05, min: 0.1 },
    { key: "cost_gate_mult", label: "Cost Gate ×", def: 2.0, step: 0.2, min: 0 },
    { key: "use_4h_trend", label: "4h Trend (1=on,0=off)", def: 1, step: 1, min: 0 },
  ],
  supertrend_ema_pullback: [
    { key: "atr_period", label: "ST ATR Period", def: 10, step: 1, min: 2 },
    { key: "atr_mult", label: "ST Multiplier", def: 3.0, step: 0.1, min: 0.5 },
    { key: "pullback_ema", label: "Pullback EMA", def: 21, step: 1, min: 5 },
    { key: "ema_4h", label: "4h Macro EMA", def: 100, step: 10, min: 20 },
    { key: "adx_min", label: "Min ADX", def: 16, step: 1, min: 5 },
  ],
  donchian_adx_breakout: [
    { key: "dc_period", label: "Donchian Bars (N)", def: 20, step: 1, min: 5 },
    { key: "adx_min", label: "Min ADX", def: 20, step: 1, min: 5 },
    { key: "vol_mult", label: "Volume Mult", def: 1.2, step: 0.05, min: 0.5 },
    { key: "ema_filter", label: "Trend EMA", def: 200, step: 10, min: 10 },
    { key: "use_4h_trend", label: "4h Trend (1=on,0=off)", def: 1, step: 1, min: 0 },
  ],
  ema_swing_4h: [
    { key: "fast_ema", label: "Fast EMA", def: 21, step: 1, min: 2 },
    { key: "slow_ema", label: "Slow EMA", def: 55, step: 1, min: 5 },
    { key: "trend_ema", label: "Trend EMA (0=off)", def: 100, step: 10, min: 0 },
    { key: "adx_min", label: "Min ADX", def: 18, step: 1, min: 5 },
    { key: "cost_gate_mult", label: "Cost Gate ×", def: 2.0, step: 0.2, min: 0 },
  ],
  keltner_momentum: [
    { key: "kc_period", label: "KC EMA Period", def: 20, step: 1, min: 5 },
    { key: "kc_mult", label: "KC ATR Mult", def: 1.5, step: 0.1, min: 0.5 },
    { key: "adx_min", label: "Min ADX", def: 20, step: 1, min: 5 },
    { key: "ema_filter", label: "Trend EMA", def: 100, step: 10, min: 10 },
    { key: "use_4h_trend", label: "4h Trend (1=on,0=off)", def: 1, step: 1, min: 0 },
  ],
  ema_cross: [
    { key: "fast_ema", label: "Fast EMA", def: 9, step: 1, min: 2 },
    { key: "slow_ema", label: "Slow EMA", def: 21, step: 1, min: 3 },
    { key: "trend_ema", label: "Trend EMA (0=off)", def: 50, step: 1, min: 0 },
    { key: "rsi_period", label: "RSI Period", def: 14, step: 1, min: 2 },
    { key: "rsi_ob", label: "RSI Overbought", def: 70, step: 1, min: 50 },
    { key: "rsi_os", label: "RSI Oversold", def: 30, step: 1, min: 5 },
  ],
  supertrend: [
    { key: "atr_period", label: "ATR Period", def: 10, step: 1, min: 2 },
    { key: "atr_mult", label: "ATR Multiplier", def: 3.0, step: 0.1, min: 0.5 },
    { key: "ema_filter", label: "Trend EMA", def: 200, step: 5, min: 10 },
    { key: "rsi_period", label: "RSI Period", def: 14, step: 1, min: 2 },
    { key: "adx_min", label: "Min ADX", def: 18, step: 1, min: 5 },
  ],
  rsi_reversal: [
    { key: "rsi_period", label: "RSI Period", def: 14, step: 1, min: 2 },
    { key: "rsi_os", label: "RSI Oversold Cross", def: 35, step: 1, min: 5 },
    { key: "rsi_ob", label: "RSI Overbought Cross", def: 65, step: 1, min: 50 },
    { key: "ema_filter", label: "Trend EMA", def: 50, step: 5, min: 5 },
  ],
  bollinger_breakout: [
    { key: "bb_period", label: "BB Period", def: 20, step: 1, min: 5 },
    { key: "bb_std", label: "BB StdDev", def: 2.0, step: 0.1, min: 0.5 },
    { key: "vol_mult", label: "Volume Mult", def: 1.3, step: 0.1, min: 0.5 },
    { key: "ema_filter", label: "Trend EMA", def: 50, step: 5, min: 5 },
  ],
  macd_cross: [
    { key: "fast_ema", label: "Fast EMA", def: 12, step: 1, min: 2 },
    { key: "slow_ema", label: "Slow EMA", def: 26, step: 1, min: 5 },
    { key: "signal_ema", label: "Signal EMA", def: 9, step: 1, min: 2 },
    { key: "ema_filter", label: "Trend EMA", def: 200, step: 5, min: 10 },
  ],
};

/* ------------------------------------------------------------------ utils */
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  let body = null;
  try { body = await res.json(); } catch (_) { /* non-JSON */ }
  if (!res.ok) {
    const d = body && body.detail;
    const msg = typeof d === "string" ? d : Array.isArray(d) ? d.map((e) => `${(e.loc || []).slice(-1)[0]}: ${e.msg}`).join("; ") : res.statusText;
    const err = new Error(msg || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return body;
}

const nf2 = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const nf0 = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const isNum = (x) => typeof x === "number" && Number.isFinite(x);
function inr(x, signed = true) {
  if (!isNum(x)) return "–";
  const sign = x < 0 ? "−" : signed && x > 0 ? "+" : "";
  return `${sign}₹${nf2.format(Math.abs(x))}`;
}
function usd(x) { return isNum(x) ? `${x < 0 ? "−" : ""}$${Math.abs(x).toFixed(4)}` : "–"; }
function pct(x, signed = false, d = 2) {
  if (!isNum(x)) return "–";
  const sign = x < 0 ? "−" : signed && x > 0 ? "+" : "";
  return `${sign}${Math.abs(x).toFixed(d)}%`;
}
function pf(x) { return x === "inf" ? "∞" : isNum(x) ? x.toFixed(2) : "–"; }
function cls(x) { return isNum(x) ? (x > 0 ? "pos" : x < 0 ? "neg" : "") : ""; }
function fmtTime(x) {
  if (x == null) return "–";
  const d = typeof x === "number" ? new Date(x * 1000) : new Date(x);
  return d.toISOString().slice(0, 16).replace("T", " ");
}
function decimals(step) { const s = String(step); return s.includes(".") ? s.split(".")[1].length : 0; }
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

function toast(msg, isError = false) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("error", isError);
  t.classList.remove("hidden");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => t.classList.add("hidden"), isError ? 7000 : 4000);
}
function setOptions(sel, items, keep = true) {
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = items.map((it) => {
    const [v, l] = Array.isArray(it) ? it : [it, it];
    return `<option value="${esc(v)}">${esc(l)}</option>`;
  }).join("");
  if (keep && [...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

/* ------------------------------------------------------------------ charts */
let priceChart, equityChart, candleSeries, volumeSeries, equitySeries, ddSeries;

function baseChartOptions() {
  return {
    autoSize: true,
    layout: { background: { type: "solid", color: COLORS.panel }, textColor: "#94a3b8", fontFamily: "ui-monospace, Consolas, monospace", fontSize: 11, attributionLogo: true },
    grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
    rightPriceScale: { borderColor: COLORS.border },
    leftPriceScale: { borderColor: COLORS.border },
    timeScale: { borderColor: COLORS.border, timeVisible: true, secondsVisible: false, rightOffset: 3 },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  };
}

function initCharts() {
  priceChart = LightweightCharts.createChart($("priceChart"), baseChartOptions());
  candleSeries = priceChart.addCandlestickSeries({
    upColor: COLORS.up, downColor: COLORS.down, borderVisible: false, wickUpColor: COLORS.up, wickDownColor: COLORS.down,
  });
  volumeSeries = priceChart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
  priceChart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });

  equityChart = LightweightCharts.createChart($("equityChart"), { ...baseChartOptions(), leftPriceScale: { visible: true, borderColor: COLORS.border } });
  equitySeries = equityChart.addAreaSeries({
    lineColor: COLORS.accent, topColor: "rgba(59,130,246,0.32)", bottomColor: "rgba(59,130,246,0.02)", lineWidth: 2,
    priceFormat: { type: "custom", formatter: (v) => `₹${nf0.format(v)}`, minMove: 0.01 },
  });
  ddSeries = equityChart.addHistogramSeries({
    priceScaleId: "left", color: "rgba(244,63,94,0.45)", lastValueVisible: false, priceLineVisible: false,
    priceFormat: { type: "custom", formatter: (v) => `${v.toFixed(1)}%`, minMove: 0.01 },
  });

  let syncing = false;
  const link = (a, b) => a.timeScale().subscribeVisibleTimeRangeChange((r) => {
    if (!r || syncing) return;
    syncing = true;
    try { b.timeScale().setVisibleRange(r); } catch (_) { /* empty */ }
    syncing = false;
  });
  link(priceChart, equityChart);
  link(equityChart, priceChart);

  priceChart.subscribeClick((p) => {
    if (p && p.hoveredObjectId && state.result) {
      const tr = state.result.trades.find((t) => String(t.trade_id) === String(p.hoveredObjectId));
      if (tr) selectTrade(tr, { fromChart: true });
    }
  });
}

function renderChart(sym) {
  const r = state.result;
  const ch = r && r.charts[sym];
  if (!ch) return;
  state.chartSymbol = sym;
  $("priceChartSubtitle").textContent = `${sym} · ${ch.timeframe}`;
  const tick = state.status && state.status.products[sym] && state.status.products[sym].tick_size;
  if (tick) candleSeries.applyOptions({ priceFormat: { type: "price", precision: decimals(tick), minMove: Number(tick) } });
  candleSeries.setData(ch.candles);
  volumeSeries.setData(ch.volume.map((v) => ({ time: v.time, value: v.value, color: v.up ? "rgba(16,185,129,0.28)" : "rgba(244,63,94,0.28)" })));

  state.overlaySeries.forEach((s) => priceChart.removeSeries(s));
  state.overlaySeries = [];
  clearTradeLines();
  const legend = [];
  if (state.indicatorsVisible) {
    for (const ov of ch.overlays) {
      const st = OVERLAY_STYLE[ov.name] || { color: "#94a3b8" };
      const s = priceChart.addLineSeries({
        color: st.color, lineWidth: st.width || 1, lineStyle: st.style || 0,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      });
      s.setData(ov.data);
      state.overlaySeries.push(s);
      legend.push(`<span><i style="background:${st.color}"></i>${esc(ov.name)}</span>`);
    }
  }
  legend.push(`<span><i style="background:#2dd4bf"></i>BUY ▲</span><span><i style="background:#f87171"></i>SELL ▼</span>`);
  $("legend").innerHTML = legend.join("");
  candleSeries.setMarkers(ch.markers.map((m) => ({ time: m.time, position: m.position, shape: m.shape, color: m.color, text: m.text, id: String(m.trade_id) })));

  document.querySelectorAll("#chartTabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.sym === sym));
  priceChart.timeScale().fitContent();
}

function renderEquity() {
  const r = state.result;
  const eq = r.equity;
  equitySeries.setData(eq.equity);
  ddSeries.setData(eq.drawdown.map((d) => ({ time: d.time, value: d.value, color: d.value < -10 ? "rgba(244,63,94,0.75)" : "rgba(244,63,94,0.38)" })));
  if (state.startLine) equitySeries.removePriceLine(state.startLine);
  const startCap = r.config.capital_inr || r.config.initial_capital_inr;
  state.startLine = equitySeries.createPriceLine({
    price: startCap, color: COLORS.muted, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "start",
  });
  $("eqStartLabel").textContent = `Starting capital ₹${nf0.format(startCap)}`;
  $("eqEndValue").textContent = `₹${nf2.format(r.summary.final_equity_inr)}`;
  $("eqEndPct").innerHTML = `<span class="${cls(r.summary.net_return_pct)}">${pct(r.summary.net_return_pct, true)}</span>`;
  equityChart.timeScale().fitContent();
}

function clearTradeLines() {
  state.tradeLines.forEach((l) => candleSeries.removePriceLine(l));
  state.tradeLines = [];
}

/* ------------------------------------------------------------------ Custom Strategies & Saved Results */
async function loadCustomStrategies(symbol = null, selectId = null) {
  const sym = symbol || $("symbolSelect").value || "ETHUSD";
  const r = await api(`/api/strategies?symbol=${encodeURIComponent(sym)}`);
  state.customStrategies = r.strategies || [];
  const opts = state.customStrategies.map((s) => [s.id, `${s.name}${s.symbol && s.symbol !== "ALL" ? ` (${s.symbol})` : ""}`]);
  setOptions($("strategySelect"), opts, !selectId);
  if (selectId && [...$("strategySelect").options].some((o) => o.value === selectId)) {
    $("strategySelect").value = selectId;
  }
  applySelectedStrategyDefaults();
}

function currentStrategyObj() {
  const id = $("strategySelect").value;
  return state.customStrategies.find((s) => s.id === id) || state.customStrategies[0] || null;
}

function applySelectedStrategyDefaults() {
  const st = currentStrategyObj();
  if (!st) {
    $("strategyRuleHint").textContent = "No strategy for this coin — click + New to create one";
    return;
  }
  const pStr = Object.entries(st.params || {}).map(([k, v]) => `${k}=${v}`).join(", ");
  $("strategyRuleHint").textContent = `${st.rule_type} · ${pStr}`;
  setTimeframePill(st.timeframe || "1h");
  if (st.capital_inr) $("capital").value = st.capital_inr;
  if (st.leverage) $("leverage").value = st.leverage;
  if (st.risk_pct) setRiskValue(st.risk_pct);
  if (st.sl_type) $("slType").value = st.sl_type;
  if (st.sl_value) $("slValue").value = st.sl_value;
  if (st.tp_type) $("tpType").value = st.tp_type;
  if (st.tp_value) $("tpValue").value = st.tp_value;
  if (st.trail_mult !== undefined) $("trailAtr").value = st.trail_mult;
  if (st.fee_mode) $("feeMode").value = st.fee_mode;
  if (st.slippage_bps !== undefined) $("slippage").value = String(Math.round(st.slippage_bps));
  updateAdvSummary();
  updateOverviewHeaderPreview();
}

function setTimeframePill(tf) {
  $("tfSelect").value = tf;
  document.querySelectorAll("#tfPills .tf-pill").forEach((b) => b.classList.toggle("active", b.dataset.tf === tf));
}

function setRiskValue(val) {
  $("risk").value = val;
  document.querySelectorAll("[data-risk]").forEach((b) => b.classList.toggle("active", Number(b.dataset.risk) === Number(val)));
}

function updateAdvSummary() {
  const lev = $("leverage").value || 10;
  const slT = $("slType").value === "fixed_pct" ? `Fixed ${$("slValue").value}% SL` : `${$("slValue").value}x ATR SL`;
  const tpT = $("tpType").value === "rr_ratio" ? `${$("tpValue").value}R TP` : $("tpType").value === "fixed_pct" ? `${$("tpValue").value}% TP` : `${$("tpValue").value}x ATR TP`;
  $("advFootSummary").textContent = `${lev}x leverage · ${slT} / ${tpT} · Fees + GST`;
}

function updateOverviewHeaderPreview() {
  const sym = $("symbolSelect").value || "ETHUSD";
  const st = currentStrategyObj();
  const sname = st ? st.name : "Custom Strategy";
  const tf = $("tfSelect").value || "1h";
  const lev = $("leverage").value || 10;
  $("overviewSubtitle").textContent = `${sym} · ${sname} · ${tf} · ${lev}x`;
  $("priceChartSubtitle").textContent = `${sym} · ${tf}`;
}

function markPreviewStale() {
  updateAdvSummary();
  updateOverviewHeaderPreview();
  if (!state.result) {
    $("emptyRunOverlay").classList.remove("hidden");
    $("saveResultBtn").disabled = true;
    $("unsavedBadge").textContent = "Configure parameters & click Run backtest";
    $("unsavedBadge").className = "unsaved-badge";
  } else {
    state.resultSaved = false;
    $("unsavedBadge").textContent = "Parameters modified — click Run backtest to refresh";
    $("unsavedBadge").className = "unsaved-badge unsaved";
  }
}

function renderRuleParamInputs(ruleType, params = {}) {
  const defs = RULE_PARAM_DEFS[ruleType] || RULE_PARAM_DEFS.ema_cross;
  $("stratParamsContainer").innerHTML = defs.map((d) => {
    const val = params[d.key] !== undefined ? params[d.key] : d.def;
    return `<div class="field">
      <label for="param_${d.key}">${esc(d.label)}</label>
      <input id="param_${d.key}" data-param="${d.key}" type="number" min="${d.min}" step="${d.step}" value="${val}">
    </div>`;
  }).join("");
}

function openStrategyModal(isNew = false) {
  const st = isNew ? null : currentStrategyObj();
  $("stratDialogTitle").textContent = isNew ? "Create New Custom Strategy" : `Edit Strategy — ${st ? st.name : ""}`;
  $("stratIdInput").value = st ? st.id : "";
  $("stratNameInput").value = st ? st.name : "";
  const coinOpts = [["ALL", "ALL (Available on every coin)"], ...(state.status ? state.status.symbols : ["ETHUSD", "BTCUSD", "SOLUSD"]).map((s) => [s, `${s} only`])];
  setOptions($("stratSymbolInput"), coinOpts, false);
  $("stratSymbolInput").value = st ? (st.symbol || "ALL") : ($("symbolSelect").value || "ALL");
  $("stratRuleInput").value = st ? st.rule_type : "ema_cross";
  $("stratTfInput").value = st ? (st.timeframe || "1h") : ($("tfSelect").value || "1h");
  renderRuleParamInputs($("stratRuleInput").value, st ? (st.params || {}) : {});
  $("stratCapInput").value = st ? st.capital_inr : ($("capital").value || 50000);
  $("stratLevInput").value = st ? st.leverage : ($("leverage").value || 10);
  $("stratRiskInput").value = st ? st.risk_pct : ($("risk").value || 2.0);
  $("stratSlTypeInput").value = st ? st.sl_type : ($("slType").value || "atr_mult");
  $("stratSlValInput").value = st ? st.sl_value : ($("slValue").value || 2.0);
  $("stratTpTypeInput").value = st ? st.tp_type : ($("tpType").value || "atr_mult");
  $("stratTpValInput").value = st ? st.tp_value : ($("tpValue").value || 5.0);
  $("stratDeleteBtn").classList.toggle("hidden", isNew || !st);
  $("stratFormMsg").textContent = "";
  $("strategyDialog").showModal();
}

async function saveStrategyFromModal(ev) {
  ev.preventDefault();
  const id = $("stratIdInput").value.trim();
  const params = {};
  document.querySelectorAll("#stratParamsContainer [data-param]").forEach((inp) => {
    params[inp.dataset.param] = Number(inp.value);
  });
  const payload = {
    name: $("stratNameInput").value.trim(),
    symbol: $("stratSymbolInput").value,
    timeframe: $("stratTfInput").value,
    rule_type: $("stratRuleInput").value,
    params,
    capital_inr: Number($("stratCapInput").value) || 50000,
    leverage: Number($("stratLevInput").value) || 10,
    risk_pct: Number($("stratRiskInput").value) || 2.0,
    sl_type: $("stratSlTypeInput").value,
    sl_value: Number($("stratSlValInput").value) || 2.0,
    tp_type: $("stratTpTypeInput").value,
    tp_value: Number($("stratTpValInput").value) || 5.0,
    trail_mult: Number($("trailAtr").value) || 2.5,
  };
  try {
    let saved;
    if (id) {
      saved = await api(`/api/strategies/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(payload) });
      toast(`Updated strategy "${saved.name}"`);
    } else {
      saved = await api("/api/strategies", { method: "POST", body: JSON.stringify(payload) });
      toast(`Created strategy "${saved.name}"`);
    }
    $("strategyDialog").close();
    if (saved.symbol !== "ALL" && saved.symbol !== $("symbolSelect").value) {
      $("symbolSelect").value = saved.symbol;
    }
    await loadCustomStrategies($("symbolSelect").value, saved.id);
  } catch (e) {
    $("stratFormMsg").textContent = e.message;
  }
}

async function deleteStrategyFromModal() {
  const id = $("stratIdInput").value.trim();
  if (!id) return;
  if (!confirm("Delete this custom strategy?")) return;
  try {
    await api(`/api/strategies/${encodeURIComponent(id)}`, { method: "DELETE" });
    $("strategyDialog").close();
    toast("Strategy deleted");
    await loadCustomStrategies($("symbolSelect").value);
  } catch (e) {
    $("stratFormMsg").textContent = e.message;
  }
}

async function saveCurrentBacktestResult() {
  const r = state.result;
  if (!r) return;
  const st = currentStrategyObj();
  const leg0 = r.legs[0] || {};
  const payload = {
    strategy_id: (st && st.id) || r.config.strategy_id || leg0.strategy,
    strategy_name: (st && st.name) || r.config.strategy_name || leg0.strategy,
    symbol: leg0.symbol || $("symbolSelect").value,
    timeframe: leg0.timeframe || $("tfSelect").value,
    period_days: r.window.days,
    capital_inr: r.config.capital_inr || r.config.initial_capital_inr,
    leverage: r.config.leverage,
    risk_pct: r.config.risk_pct || r.config.risk_per_trade_pct,
    sl_type: r.config.sl_type || $("slType").value,
    sl_value: r.config.sl_value || Number($("slValue").value),
    tp_type: r.config.tp_type || $("tpType").value,
    tp_value: r.config.tp_value || Number($("tpValue").value),
    trail_mult: r.config.trail_atr_mult,
    rule_type: (st && st.rule_type) || r.config.rule_type,
    params: (st && st.params) || r.config.params || {},
    net_pnl_inr: r.summary.net_pnl_inr,
    return_pct: r.summary.net_return_pct,
    win_rate_pct: r.summary.win_rate_pct,
    profit_factor: r.summary.profit_factor,
    max_dd_pct: r.summary.max_drawdown_pct,
    total_trades: r.summary.total_trades,
    summary: r.summary,
    trades: r.trades,
  };
  $("saveResultBtn").disabled = true;
  try {
    await api("/api/backtest/save_result", { method: "POST", body: JSON.stringify(payload) });
    state.resultSaved = true;
    $("unsavedBadge").textContent = "✓ Saved to Backtest Results";
    $("unsavedBadge").className = "unsaved-badge saved";
    toast("Backtest result saved to catalog");
    await loadSavedResults();
  } catch (e) {
    $("saveResultBtn").disabled = false;
    toast(`Save failed: ${e.message}`, true);
  }
}

async function loadSavedResults() {
  const q = new URLSearchParams();
  if ($("resSymbolFilter").value) q.set("symbol", $("resSymbolFilter").value);
  if ($("resStratFilter").value.trim()) q.set("strategy_name", $("resStratFilter").value.trim());
  if ($("resTfFilter").value) q.set("timeframe", $("resTfFilter").value);
  if ($("resMinWrFilter").value !== "") q.set("min_win_rate", $("resMinWrFilter").value);
  if ($("resMinPfFilter").value !== "") q.set("min_pf", $("resMinPfFilter").value);
  try {
    const r = await api(`/api/backtest/results?${q}`);
    state.savedResults = r.results || [];
    $("savedCountBadge").textContent = String(state.savedResults.length);
    $("resCatalogCount").textContent = `${state.savedResults.length} saved result(s)`;
    renderSavedResultsTable();
  } catch (e) {
    toast(`Could not load saved results: ${e.message}`, true);
  }
}

function renderSavedResultsTable() {
  const rows = state.savedResults;
  const head = `<thead><tr>
    <th class="l">Date (UTC)</th><th class="l">Coin</th><th class="l">Strategy</th><th>TF</th>
    <th>Capital ₹</th><th>Lev</th><th>SL / TP</th><th>Net P&amp;L ₹</th><th>Win Rate</th>
    <th>PF</th><th>Max DD</th><th>Trades</th><th class="l">Actions</th>
  </tr></thead>`;
  if (!rows.length) {
    $("resultsTable").innerHTML = head + `<tbody><tr><td class="l muted" colspan="13">No saved backtest results match your filters. Run a backtest and click "🔖 Save result" to add one.</td></tr></tbody>`;
    return;
  }
  const body = rows.map((r, i) => {
    const slLbl = `${r.sl_value}${r.sl_type === "fixed_pct" ? "%" : "x ATR"}`;
    const tpLbl = `${r.tp_value}${r.tp_type === "rr_ratio" ? "R" : r.tp_type === "fixed_pct" ? "%" : "x ATR"}`;
    return `<tr data-i="${i}">
      <td class="l">${fmtTime(r.saved_at)}</td>
      <td class="l"><b>${esc(r.symbol)}</b></td>
      <td class="l">${esc(r.strategy_name)}</td>
      <td>${esc(r.timeframe)}</td>
      <td>₹${nf0.format(r.capital_inr)}</td>
      <td>${r.leverage}x</td>
      <td>${esc(slLbl)} / ${esc(tpLbl)}</td>
      <td class="${cls(r.net_pnl_inr)}"><b>${inr(r.net_pnl_inr)}</b> <span class="muted">(${pct(r.return_pct, true, 1)})</span></td>
      <td>${pct(r.win_rate_pct, false, 1)}</td>
      <td>${pf(r.profit_factor)}</td>
      <td class="neg">${pct(-Math.abs(r.max_dd_pct || 0), false, 2)}</td>
      <td>${r.total_trades}</td>
      <td class="l">
        <button type="button" class="btn btn-xs btn-primary" data-load-res="${esc(r.id)}">Load</button>
        <button type="button" class="btn btn-xs btn-danger" data-del-res="${esc(r.id)}">Delete</button>
      </td>
    </tr>`;
  }).join("");
  $("resultsTable").innerHTML = head + `<tbody>${body}</tbody>`;
  $("resultsTable").querySelectorAll("[data-load-res]").forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      const item = state.savedResults.find((x) => x.id === btn.dataset.loadRes);
      if (item) restoreSavedResult(item);
    };
  });
  $("resultsTable").querySelectorAll("[data-del-res]").forEach((btn) => {
    btn.onclick = async (ev) => {
      ev.stopPropagation();
      try {
        await api(`/api/backtest/results/${encodeURIComponent(btn.dataset.delRes)}`, { method: "DELETE" });
        toast("Saved result deleted");
        loadSavedResults();
      } catch (e) { toast(e.message, true); }
    };
  });
}

async function restoreSavedResult(item) {
  $("symbolSelect").value = item.symbol;
  await loadCustomStrategies(item.symbol, item.strategy_id);
  setTimeframePill(item.timeframe || "1h");
  $("capital").value = item.capital_inr || 50000;
  $("leverage").value = item.leverage || 10;
  if (item.risk_pct) setRiskValue(item.risk_pct);
  $("slType").value = item.sl_type || "atr_mult";
  $("slValue").value = item.sl_value || 2.0;
  $("tpType").value = item.tp_type || "atr_mult";
  $("tpValue").value = item.tp_value || 5.0;
  if (item.trail_mult !== undefined) $("trailAtr").value = item.trail_mult;
  if (item.fee_mode) $("feeMode").value = item.fee_mode;
  if (item.slippage_bps !== undefined) $("slippage").value = String(Math.round(item.slippage_bps));
  const d = String(Math.round(item.period_days || 180));
  if ([...$("daysSelect").options].some((o) => o.value === d)) $("daysSelect").value = d;
  updateAdvSummary();
  await runBacktest({ markSaved: true });
}

/* ------------------------------------------------------------------ status, presets */
async function loadStatus() {
  const s = await api("/api/status");
  state.status = s;
  state.anchor = s.step3c_anchor;
  state.anchor4b = s.step4b_anchor;
  $("endSelect").querySelector('option[value="step4b"]').disabled = !state.anchor4b;

  const live = s.is_live;
  const mb = $("modeBadge");
  mb.innerHTML = `<span class="dot"></span> ${live ? "Live trading enabled" : "Live trading disabled"}`;
  mb.className = `status-pill ${live ? "live" : ""}`;
  $("envBadge").textContent = `${s.environment} · ${s.rest_url.replace("https://", "")}`;

  $("coverage").innerHTML = s.symbols.map((sym) => {
    const c = s.coverage[sym] && s.coverage[sym].series.TRADE;
    if (!c) return `<span class="cov-chip"><b>${esc(sym)}</b> no data</span>`;
    const gaps = Object.values(s.coverage[sym].series).reduce((a, x) => a + (x ? x.gaps : 0), 0);
    return `<span class="cov-chip"><b>${esc(sym)}</b> ${c.days}d · <span class="${gaps ? "bad" : "ok"}">${gaps} gaps</span></span>`;
  }).join("");

  const symbolsWithData = s.symbols.filter((x) => s.coverage[x] && s.coverage[x].series.TRADE);
  setOptions($("symbolSelect"), symbolsWithData);
  setOptions($("resSymbolFilter"), [["", "All Coins"], ...symbolsWithData.map((x) => [x, x])]);
  setOptions($("tfSelect"), s.timeframes);
  setOptions($("exitMode"), s.exit_modes);
  setOptions($("feeMode"), [
    ["taker_maker_tp", "Taker Entry / Maker TP"],
    ["maker_maker", "Maker Entry + Maker TP (0.02%)"],
    ["taker", "All Taker (0.05%)"],
    ["maker", "All Maker (0.02%)"],
  ]);
  setOptions($("triggerSource"), s.trigger_sources);

  await loadCustomStrategies($("symbolSelect").value);
  await loadSavedResults();
  renderSyncStatus(s.sync);
  if (s.sync && s.sync.state === "running") pollSync();
}

function toggleCustomRange() {
  const custom = $("daysSelect").value === "custom";
  $("customRange").classList.toggle("hidden", !custom);
  $("endSelect").disabled = custom;
}

/* ------------------------------------------------------------------ backtest */
function readRequest() {
  const sym = $("symbolSelect").value;
  const st = currentStrategyObj();
  const trailOn = $("trailEnabled").checked;
  const req = {
    symbol: sym,
    symbols: [sym].filter(Boolean),
    strategy: (st && st.id) || $("strategySelect").value || "bollinger_volume_breakout",
    strategy_id: st ? st.id : $("strategySelect").value,
    strategy_name: st ? st.name : $("strategySelect").value,
    rule_type: st ? st.rule_type : null,
    params: st ? st.params : null,
    timeframe: $("tfSelect").value || "1h",
    sl_type: $("slType").value,
    stop_mode: $("slType").value === "fixed_pct" ? "pct" : "atr",
    sl_value: Number($("slValue").value),
    tp_type: $("tpType").value,
    tp_value: Number($("tpValue").value),
    trailing_stop: trailOn,
    exit_mode: trailOn ? "trail" : "fixed",
    trail_mult: Number($("trailAtr").value),
    trail_atr_mult: Number($("trailAtr").value),
    fee_mode: $("feeMode").value || "taker_maker_tp",
    trigger_source: $("triggerSource").value || "trade",
    capital_inr: Number($("capital").value),
    initial_capital_inr: Number($("capital").value),
    leverage: Number($("leverage").value),
    risk_pct: Number($("risk").value),
    risk_per_trade_pct: Number($("risk").value),
    max_daily_loss_pct: Number($("dailyLoss").value || 5),
    slippage_bps: Number($("slippage").value || 0),
    trend_filter_4h: $("trend4h").checked,
    cost_gate: $("costGate").checked,
    strict_lot_cap: $("strictLot").checked,
    walk_forward_windows: 3,
    include_chart: true,
  };
  if ($("daysSelect").value === "custom") {
    if (!$("startDate").value || !$("endDate").value) throw new Error("Pick a custom start and end date");
    req.days = null;
    req.start = `${$("startDate").value}T00:00:00Z`;
    req.end = `${$("endDate").value}T00:00:00Z`;
  } else {
    req.days = Number($("daysSelect").value);
    const a = $("endSelect").value === "step3c" ? state.anchor : $("endSelect").value === "step4b" ? state.anchor4b : null;
    if (a) { req.end = a.end; req.history_start = a.warmup_start; }
  }
  return req;
}

async function runBacktest(opts = {}) {
  let req;
  try { req = readRequest(); } catch (e) { toast(e.message, true); return; }
  if (!req.symbols.length) { toast("Select a coin", true); return; }
  $("runBtn").disabled = true;
  $("chartLoading").classList.remove("hidden");
  try {
    const r = await api("/api/backtest/run", { method: "POST", body: JSON.stringify(req) });
    state.result = r;
    state.resultSaved = !!opts.markSaved;
    state.selectedTrade = null;
    $("emptyRunOverlay").classList.add("hidden");
    renderResult();
    const w = r.window;
    $("runInfo").textContent = `${fmtTime(w.start)} → ${fmtTime(w.end)} UTC (${Math.round(w.days)}d) · ${r.elapsed_ms} ms`;
    if (state.resultSaved) {
      $("unsavedBadge").textContent = "Loaded from saved result";
      $("unsavedBadge").className = "unsaved-badge saved";
      $("saveResultBtn").disabled = false;
    } else {
      $("unsavedBadge").textContent = "Unsaved preview — click Save result to keep";
      $("unsavedBadge").className = "unsaved-badge unsaved";
      $("saveResultBtn").disabled = false;
    }
  } catch (e) {
    toast(`Backtest failed: ${e.message}`, true);
  } finally {
    $("runBtn").disabled = false;
    $("chartLoading").classList.add("hidden");
  }
}

function renderResult() {
  const r = state.result;
  const leg0 = r.legs[0] || {};
  $("overviewSubtitle").textContent = `${leg0.symbol} · ${r.config.strategy_name || leg0.strategy} · ${leg0.timeframe} · ${r.config.leverage}x`;
  renderKpis(r);
  const syms = Object.keys(r.charts);
  $("chartTabs").innerHTML = syms.length > 1 ? syms.map((s) => `<button class="tab" data-sym="${esc(s)}">${esc(s)}</button>`).join("") : "";
  document.querySelectorAll("#chartTabs .tab").forEach((b) => { b.onclick = () => renderChart(b.dataset.sym); });
  renderEquity();
  renderChart(syms.includes(state.chartSymbol) ? state.chartSymbol : syms[0]);
  renderLedger();
  renderWalkForward();
}

function kpi(label, value, sub = "", extra = "") {
  return `<div class="kpi ${extra}"><div class="k-label">${label}</div><div class="k-value">${value}</div><div class="k-sub">${sub}</div></div>`;
}

function renderKpis(r) {
  const s = r.summary;
  const wins = r.trades.filter((t) => t.net_pnl_usd > 0).length;
  $("kpis").innerHTML = [
    `<div class="kpi">
      <div class="kpi-top"><span class="kpi-ico ico-green">📊</span><span class="k-label">Net profit</span></div>
      <div class="k-value ${cls(s.net_pnl_inr)}">${inr(s.net_pnl_inr, false)}</div>
      <div class="k-sub ${cls(s.net_return_pct)}">${pct(s.net_return_pct, true)} return</div>
    </div>`,
    `<div class="kpi">
      <div class="kpi-top"><span class="kpi-ico ico-blue">%</span><span class="k-label">Win rate</span></div>
      <div class="k-value">${pct(s.win_rate_pct, false, 1)}</div>
      <div class="k-sub">${wins} of ${s.total_trades} trades</div>
    </div>`,
    `<div class="kpi">
      <div class="kpi-top"><span class="kpi-ico ico-blue">📈</span><span class="k-label">Profit factor</span></div>
      <div class="k-value">${pf(s.profit_factor)}</div>
      <div class="k-sub">gross wins / losses</div>
    </div>`,
    `<div class="kpi">
      <div class="kpi-top"><span class="kpi-ico ico-red">↘</span><span class="k-label">Max drawdown</span></div>
      <div class="k-value neg">${pct(-Math.abs(s.max_drawdown_pct || 0), false)}</div>
      <div class="k-sub">Fees+GST ${inr(-s.fees_gst_inr)}</div>
    </div>`,
    `<div class="kpi">
      <div class="kpi-top"><span class="kpi-ico ico-slate">≣</span><span class="k-label">Total trades</span></div>
      <div class="k-value">${s.total_trades}</div>
      <div class="k-sub">L ${s.long_trades} · S ${s.short_trades}</div>
    </div>`,
  ].join("");
}

/* ------------------------------------------------------------------ ledger */
const LEDGER_COLS = [
  ["symbol", "Symbol", "l"],
  ["side", "Side", "l"],
  ["entry_price", "Entry"],
  ["exit_price", "Exit"],
  ["net_pnl_inr", "Net P&L"],
  ["equity_after_inr", "Equity"],
  ["minutes_held", "Duration"],
];

function ledgerCell(t, key) {
  const v = t[key];
  switch (key) {
    case "side": return `<span class="tag ${v === "LONG" ? "tag-long" : "tag-short"}">${v}</span>`;
    case "entry_price": case "exit_price": return isNum(v) ? v.toFixed(2) : esc(v);
    case "net_pnl_inr": return `<span class="${cls(v)}"><b>${inr(v)}</b></span>`;
    case "equity_after_inr": return `₹${nf2.format(v)}`;
    case "minutes_held": return v >= 1440 ? `${(v / 1440).toFixed(1)}d` : v >= 60 ? `${(v / 60).toFixed(1)}h` : `${v}m`;
    default: return esc(v);
  }
}

function renderLedger() {
  const trades = state.result.trades;
  $("ledgerCount").textContent = `${trades.length} trades · Sample rows from current run`;
  $("exportCsv").disabled = !trades.length;
  const head = `<thead><tr>${LEDGER_COLS.map(([, l, a]) => `<th class="${a || ""}">${l}</th>`).join("")}</tr></thead>`;
  const body = trades.map((t, i) => `<tr data-i="${i}">${LEDGER_COLS.map(([k, , a]) => `<td class="${a || ""}">${ledgerCell(t, k)}</td>`).join("")}</tr>`).join("");
  $("ledgerTable").innerHTML = head + `<tbody>${body}</tbody>`;
  $("ledgerTable").querySelectorAll("tbody tr").forEach((tr) => {
    tr.onclick = () => selectTrade(trades[Number(tr.dataset.i)]);
  });
}

function selectTrade(tr, { fromChart = false } = {}) {
  state.selectedTrade = tr.trade_id;
  if (tr.symbol !== state.chartSymbol) renderChart(tr.symbol);
  const ch = state.result.charts[tr.symbol];
  const pad = Math.max(30 * ch.tf_sec, (tr.exit_time_ts - tr.entry_time_ts) * 0.6);
  priceChart.timeScale().setVisibleRange({ from: tr.entry_time_ts - pad, to: tr.exit_time_ts + pad });

  clearTradeLines();
  const long = tr.side === "LONG";
  const line = (price, color, title) => candleSeries.createPriceLine({ price, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title });
  state.tradeLines = [
    line(tr.entry_price, "#cbd5e1", `#${tr.trade_id} entry`),
    line(tr.initial_sl, COLORS.down, "SL"),
    line(tr.tp_price, COLORS.up, "TP"),
    line(tr.exit_price, long === tr.exit_price > tr.entry_price ? "#4ade80" : "#fb923c", `exit ${tr.exit_reason}`),
  ];
  if (tr.final_sl !== tr.initial_sl) state.tradeLines.push(line(tr.final_sl, "#f59e0b", "final SL"));

  showTab("ledger");
  const rows = $("ledgerTable").querySelectorAll("tbody tr");
  rows.forEach((row) => row.classList.toggle("selected", state.result.trades[Number(row.dataset.i)].trade_id === tr.trade_id));
  const sel = $("ledgerTable").querySelector("tbody tr.selected");
  if (sel && fromChart) sel.scrollIntoView({ block: "nearest", behavior: "smooth" });
  if (!fromChart) $("priceChart").scrollIntoView({ block: "center", behavior: "smooth" });
}

function exportCsv() {
  const trades = state.result && state.result.trades;
  if (!trades || !trades.length) return;
  const cols = ["trade_id", "symbol", "side", "signal_bar", "entry_time", "exit_time", "minutes_held", "contracts", "contract_value",
    "entry_price", "exit_price", "initial_sl", "final_sl", "tp_price", "exit_reason", "notional_entry_usd", "notional_exit_usd",
    "margin_usd", "gross_pnl_usd", "entry_fee_rate", "exit_fee_rate", "entry_fee_usd", "exit_fee_usd", "fees_usd", "gst_usd",
    "funding_usd", "funding_events", "net_pnl_usd", "gross_pnl_inr", "fees_inr", "gst_inr", "funding_inr", "net_pnl_inr", "equity_after_inr"];
  const q = (v) => { const s = v == null ? "" : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const csv = [cols.join(","), ...trades.map((t) => cols.map((c) => q(t[c])).join(","))].join("\n");
  const legs = state.result.legs.map((l) => `${l.symbol}-${l.strategy}-${l.timeframe}`).join("_");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = `ledger_${legs}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ------------------------------------------------------------------ walk-forward */
function renderWalkForward() {
  const r = state.result;
  const rows = [...r.windows.map((w) => ({ ...w, label: w.name })),
    { label: "Full", start: r.window.start, end: r.window.end, days: r.window.days, summary: r.summary, audit_passed: r.audit.passed }];
  const syms = r.legs.map((l) => l.symbol);
  $("wfNote").textContent = r.windows.length
    ? `Each window is an independent run with fresh ₹${nf0.format(r.config.initial_capital_inr)} capital.`
    : "Window breakdown disabled.";
  const head = ["Window", "Start (UTC)", "End (UTC)", "Days", "Trades", syms.length > 1 ? syms.join(" / ") : null, "Win %", "Gross ₹", "Fees+GST ₹", "Funding ₹", "Net ₹", "Return", "PF", "Max DD", "Audit"].filter(Boolean);
  const body = rows.map((w) => {
    const s = w.summary;
    const cells = [
      `<td class="l"><b>${esc(w.label)}</b></td>`, `<td class="l">${fmtTime(w.start)}</td>`, `<td class="l">${fmtTime(w.end)}</td>`,
      `<td>${w.days}</td>`, `<td>${s.total_trades}</td>`,
      syms.length > 1 ? `<td>${syms.map((x) => s.trades_by_symbol[x] || 0).join(" / ")}</td>` : null,
      `<td>${pct(s.win_rate_pct, false, 1)}</td>`, `<td class="${cls(s.gross_pnl_inr)}">${inr(s.gross_pnl_inr)}</td>`,
      `<td>${inr(-s.fees_gst_inr)}</td>`, `<td class="${cls(s.funding_inr)}">${inr(s.funding_inr)}</td>`,
      `<td class="${cls(s.net_pnl_inr)}"><b>${inr(s.net_pnl_inr)}</b></td>`, `<td class="${cls(s.net_return_pct)}">${pct(s.net_return_pct, true)}</td>`,
      `<td>${pf(s.profit_factor)}</td>`, `<td>${pct(s.max_drawdown_pct)}</td>`,
      `<td><span class="tag ${w.audit_passed ? "tag-ok" : "tag-bad"}">${w.audit_passed ? "OK" : "FAIL"}</span></td>`,
    ].filter(Boolean);
    return `<tr>${cells.join("")}</tr>`;
  }).join("");
  $("wfTable").innerHTML = `<thead><tr>${head.map((h, i) => `<th class="${i < 3 ? "l" : ""}">${h}</th>`).join("")}</tr></thead><tbody>${body}</tbody>`;
}

/* ------------------------------------------------------------------ sync */
function renderSyncStatus(s) {
  if (!s || s.state === "idle") { $("syncStatus").textContent = ""; return; }
  if (s.state === "running") $("syncStatus").textContent = `Syncing… ${s.message || ""}`;
  else if (s.state === "done") $("syncStatus").textContent = `Synced ${fmtTime(s.finished)} UTC`;
  else if (s.state === "error") $("syncStatus").textContent = `Sync error: ${s.error}`;
  $("syncBtn").disabled = s.state === "running";
}

async function startSync(body = {}) {
  const r = await api("/api/data/sync", { method: "POST", body: JSON.stringify(body) });
  renderSyncStatus(r);
  pollSync();
  return r;
}

function pollSync() {
  clearInterval(state.syncTimer);
  state.syncTimer = setInterval(async () => {
    let s;
    try { s = await api("/api/data/sync"); } catch (_) { return; }
    renderSyncStatus(s);
    if (s.state === "running") return;
    clearInterval(state.syncTimer);
    if (s.state === "done") {
      const fetched = (s.results || []).reduce((a, x) => a + x.fetched, 0);
      toast(`Sync complete: ${fetched.toLocaleString()} new 1m rows${s.add_symbol ? ` · added ${s.add_symbol}` : ""}`);
      await loadStatus();
    } else if (s.state === "error") {
      toast(`Sync failed: ${s.error}`, true);
    }
  }, 1500);
}

/* ------------------------------------------------------------------ wiring */
function showTab(name) {
  document.querySelectorAll(".bottom .tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  ["ledger", "results", "wf"].forEach((t) => {
    const el = $(`tab-${t}`);
    if (el) el.classList.toggle("hidden", t !== name);
  });
}

function bind() {
  $("runBtn").onclick = () => runBacktest();
  $("saveResultBtn").onclick = saveCurrentBacktestResult;

  $("symbolSelect").onchange = async () => {
    await loadCustomStrategies($("symbolSelect").value);
    markPreviewStale();
  };
  $("strategySelect").onchange = () => {
    applySelectedStrategyDefaults();
    markPreviewStale();
  };

  document.querySelectorAll("#tfPills .tf-pill").forEach((b) => {
    b.onclick = () => {
      setTimeframePill(b.dataset.tf);
      markPreviewStale();
    };
  });

  document.querySelectorAll("[data-risk]").forEach((b) => {
    b.onclick = () => {
      setRiskValue(b.dataset.risk);
      markPreviewStale();
    };
  });
  $("risk").oninput = () => {
    setRiskValue($("risk").value);
    markPreviewStale();
  };

  ["capital", "leverage", "slType", "slValue", "tpType", "tpValue", "trailEnabled", "trailAtr", "slippage", "feeMode", "daysSelect"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("change", () => {
      if (id === "daysSelect") toggleCustomRange();
      markPreviewStale();
    });
  });

  $("newStrategyBtn").onclick = () => openStrategyModal(true);
  $("editStrategyBtn").onclick = () => openStrategyModal(false);
  $("stratRuleInput").onchange = () => renderRuleParamInputs($("stratRuleInput").value, {});
  $("stratCancelBtn").onclick = () => $("strategyDialog").close();
  $("strategyForm").addEventListener("submit", saveStrategyFromModal);
  $("stratDeleteBtn").onclick = deleteStrategyFromModal;

  $("toggleIndicatorsBtn").onclick = () => {
    state.indicatorsVisible = !state.indicatorsVisible;
    $("toggleIndicatorsBtn").classList.toggle("active", state.indicatorsVisible);
    if (state.result && state.chartSymbol) renderChart(state.chartSymbol);
  };
  $("expandChartBtn").onclick = () => {
    $("priceChartCard").classList.toggle("expanded");
    if (priceChart) priceChart.timeScale().fitContent();
  };

  document.querySelectorAll(".bottom .tab").forEach((b) => {
    b.onclick = () => {
      showTab(b.dataset.tab);
      if (b.dataset.tab === "results") loadSavedResults();
    };
  });
  $("exportCsv").onclick = exportCsv;

  ["resSymbolFilter", "resTfFilter", "resMinWrFilter", "resMinPfFilter"].forEach((id) => {
    $(id).addEventListener("change", loadSavedResults);
  });
  $("resStratFilter").addEventListener("input", () => {
    clearTimeout(state._filterTimer);
    state._filterTimer = setTimeout(loadSavedResults, 220);
  });
  $("resClearFiltersBtn").onclick = () => {
    $("resSymbolFilter").value = "";
    $("resStratFilter").value = "";
    $("resTfFilter").value = "";
    $("resMinWrFilter").value = "";
    $("resMinPfFilter").value = "";
    loadSavedResults();
  };

  $("syncBtn").onclick = async () => {
    try { await startSync({}); } catch (e) { toast(e.message, true); }
  };
  $("addCoinBtn").onclick = () => { $("addCoinMsg").textContent = ""; $("addCoinDialog").showModal(); $("newSymbol").focus(); };
  $("addCoinForm").addEventListener("submit", async (ev) => {
    if (ev.submitter && ev.submitter.value === "cancel") return;
    ev.preventDefault();
    const sym = $("newSymbol").value.trim().toUpperCase();
    $("addCoinMsg").textContent = `Validating ${sym}…`;
    $("addCoinSubmit").disabled = true;
    try {
      await startSync({ add_symbol: sym, days: Number($("newDays").value) || 90 });
      $("addCoinDialog").close();
      toast(`${sym} validated — downloading history in the background`);
    } catch (e) {
      $("addCoinMsg").textContent = e.message;
    } finally {
      $("addCoinSubmit").disabled = false;
    }
  });
}

/* ------------------------------------------------------------------ Step 7: bot operations view */
const bot = { timer: null, status: null, busy: false };

function showView(name) {
  document.querySelectorAll(".vbtn").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $("labView").classList.toggle("hidden", name !== "lab");
  $("botView").classList.toggle("hidden", name !== "bot");
  $("manualView").classList.toggle("hidden", name !== "manual");
  if (history.replaceState) history.replaceState(null, "", name === "lab" ? "#" : `#${name}`);
  clearInterval(bot.timer);
  clearInterval(mt.timer);
  if (name === "bot") {
    loadBotStatus();
    bot.timer = setInterval(() => { if (!document.hidden) loadBotStatus(); }, 5000);
  } else if (name === "manual") {
    mtEnter();
  }
}

function botMsg(text, isError = false) {
  const m = $("botMsg");
  m.textContent = text;
  m.classList.toggle("error", isError);
}

function setBadge(id, text, klass) {
  const b = $(id);
  if (!b) return;
  b.textContent = text;
  b.className = `badge ${klass}`;
}

function modeBadge(s) {
  if (!s.runner.running) {
    return s.settings.live_allowed ? ["BOT STOPPED · LIVE ARMED", "badge-live"] : ["BOT STOPPED · LIVE (DISABLED)", "badge-bot-off"];
  }
  const m = (s.runner.mode || s.mode || "paper").toUpperCase();
  if (m === "LIVE") return ["LIVE BOT", "badge-live"];
  return [`${m} BOT · RUNNING`, m === "TESTNET" ? "badge-testnet" : "badge-paper"];
}

async function loadBotStatus() {
  try {
    const s = await api("/api/bot/status");
    bot.status = s;
    renderBot(s);
    $("botRefresh").textContent = `updated ${new Date().toISOString().slice(11, 19)} UTC · auto-refresh 5s`;
  } catch (e) {
    $("botRefresh").textContent = `status failed: ${e.message}`;
  }
}

function chip(ok, yes = "PASS", no = "block") { return `<span class="chip ${ok ? "ok" : "no"}">${ok ? yes : no}</span>`; }
function td(v, k = "") { return `<td class="${k}">${v}</td>`; }
function shortTime(x) { return x ? String(x).slice(5, 16).replace("T", " ") : "–"; }

function renderBot(s) {
  const [bt, bk] = modeBadge(s);
  setBadge("botBadge", bt, bk);
  setBadge("botModeBadge", `${(s.mode || "paper").toUpperCase()} JOURNAL${s.settings.live_allowed ? "" : " · LIVE (DISABLED)"}`,
    s.mode === "testnet" ? "badge-testnet" : s.mode === "live" ? "badge-live" : "badge-paper");
  const rs = s.readiness_state || "–";
  setBadge("botStateBadge", rs === "STOPPED" ? `STOPPED (last: ${s.last_known_state || "–"})` : rs,
    rs === "OBSERVED_READY" ? "badge-ok" : rs === "HALTED" || rs === "DEGRADED" ? "badge-bad" : "badge-env");
  $("botPausedBadge").classList.toggle("hidden", !s.entries_paused);
  $("botHaltBadge").classList.toggle("hidden", !s.daily_halted);
  $("botHaltBadge").textContent = s.daily_halted ? `HALTED · ${s.halt_reason || ""}`.slice(0, 80) : "HALTED";
  const running = s.runner.running;
  $("botStart").disabled = running;
  $("botStep").disabled = running;
  $("botStop").disabled = !running;
  $("botPause").disabled = !!s.entries_paused;
  $("botResume").disabled = !s.entries_paused;
  $("botMode").querySelector('option[value="live"]').disabled = !s.settings.live_allowed;
  $("botMode").querySelector('option[value="testnet"]').disabled = !s.settings.testnet_keys;
  $("botTelegram").title = s.settings.telegram_configured ? "send a test message" : "no Telegram credentials: dry run";

  const w = s.wallet || {};
  const pos = s.positions || [];
  const streak = `${s.loss_streak ?? 0} / ${s.max_consecutive_losses ?? 4}`;
  $("botKpis").innerHTML = [
    kpi("Runner", running ? `PID ${s.runner.pid}` : "stopped", running ? `${(s.runner.mode || "").toUpperCase()} · ${rs}` : `last ${s.last_known_state || "–"}`, "primary"),
    kpi("Total equity", inr(w.total_equity_inr, false), `realized ${inr(w.realized_equity_inr, false)}`),
    kpi("Today's PnL", `<span class="${cls(w.today_pnl_inr)}">${inr(w.today_pnl_inr)}</span>`, `${pct(w.today_pnl_pct, true)} · limit −${w.daily_loss_limit_pct ?? 5}% (${inr(-(w.daily_loss_limit_inr || 0))})`),
    kpi("Unrealized", `<span class="${cls(w.unrealized_pnl_inr)}">${inr(w.unrealized_pnl_inr)}</span>`, `available ${inr(w.available_margin_inr, false)}`),
    kpi("Open positions", String(pos.length), pos.map((p) => `${p.symbol} ${p.side}`).join(", ") || "flat"),
    kpi("Loss streak", streak, s.daily_halted ? "breaker TRIPPED" : "breaker armed"),
    kpi("Closed trades", String(w.closed_trades ?? 0), `net ${inr(w.closed_net_inr)}`),
    kpi("Unknown intents", String(s.outcome_unknown_intents ?? 0), (s.outcome_unknown_intents ? "entries blocked — reconcile" : "none")),
  ].join("");

  const sig = s.signals || {};
  $("botSignals").innerHTML = `<thead><tr><th class="l">Symbol</th><th class="l">Variant</th><th>1h bar (UTC)</th><th>Close</th><th>BB upper</th><th>BB lower</th>
    <th>4h EMA50</th><th>4h EMA200</th><th class="l">4h regime</th><th>ATR14</th><th>ATR/close</th><th>Gate ≥</th><th>Cost gate</th><th>Raw L/S</th><th>Signal</th></tr></thead><tbody>` +
    Object.entries(sig).map(([sym, x]) => x.error ? `<tr>${td(esc(sym), "l")}<td class="l" colspan="14">${esc(x.error)}</td></tr>` : `<tr>
      ${td(esc(sym), "l")}${td(esc(x.variant), "l")}${td(shortTime(x.bar_open))}${td(nf2.format(x.close))}${td(nf2.format(x.bb_upper))}${td(nf2.format(x.bb_lower))}
      ${td(nf2.format(x.ema50_4h))}${td(nf2.format(x.ema200_4h))}
      ${td(`${x.allow_long_4h ? "long-ok" : x.allow_short_4h ? "short-ok" : "neutral"}${x.trend_filter_used ? "" : " (unused)"}`, "l")}
      ${td(nf2.format(x.atr14))}${td(pct(x.atr_ratio * 100, false, 3))}${td(pct(x.gate_threshold * 100, false, 3))}
      ${td(x.cost_gate_used ? chip(x.cost_gate_pass) : "n/a")}${td(`${x.cond_long ? 1 : 0}/${x.cond_short ? 1 : 0}`)}
      ${td(x.signal_side ? `<span class="chip sig">${esc(x.signal_side)}</span>` : "none")}</tr>`).join("") + "</tbody>";

  $("botPosCount").textContent = pos.length ? `${pos.length} open` : "flat";
  $("botPositions").innerHTML = `<thead><tr><th class="l">Symbol</th><th class="l">Side</th><th>Lots</th><th>Entry</th><th>Mark</th><th>Unrealized ₹</th>
    <th>SL</th><th>TP</th><th>Trail</th><th class="l">SL / TP order</th><th>Opened</th></tr></thead><tbody>` +
    (pos.length ? pos.map((p) => `<tr>${td(esc(p.symbol), "l")}${td(esc(p.side), "l")}${td(p.contracts)}${td(nf2.format(p.entry_price))}
      ${td(isNum(p.mark_price) ? nf2.format(p.mark_price) : "–")}${td(`<span class="${cls(p.unrealized_pnl_inr)}">${inr(p.unrealized_pnl_inr)}</span>`)}
      ${td(nf2.format(p.sl_price))}${td(nf2.format(p.tp_price))}${td(nf2.format(p.trail_amount))}
      ${td(`${esc(p.sl_order_id || "paper")} / ${esc(p.tp_order_id || "paper")}`, "l")}${td(shortTime(p.opened_at))}</tr>`).join("")
      : `<tr><td class="l muted" colspan="11">No open positions</td></tr>`) + "</tbody>";

  const ints = s.intents || [];
  const unknown = ints.filter((i) => i.alert).length;
  $("botIntentNote").textContent = unknown ? `${unknown} OUTCOME_UNKNOWN — new entries on that symbol are blocked until reconciled` : `${ints.length} most recent`;
  $("botIntents").innerHTML = `<thead><tr><th>Created</th><th class="l">Symbol</th><th class="l">Role</th><th class="l">Side</th><th>Size</th><th class="l">State</th>
    <th>Fill</th><th>SL</th><th>TP</th><th class="l">Client order id</th><th class="l">Exch. id</th></tr></thead><tbody>` +
    (ints.length ? ints.map((i) => `<tr class="${i.alert ? "alert-row" : ""}">${td(shortTime(i.created_at))}${td(esc(i.symbol), "l")}${td(esc(i.role), "l")}
      ${td(esc(i.side), "l")}${td(i.size)}${td(esc(i.state), "l")}${td(isNum(i.avg_fill_price) ? nf2.format(i.avg_fill_price) : "–")}
      ${td(isNum(i.sl_price) ? nf2.format(i.sl_price) : "–")}${td(isNum(i.tp_price) ? nf2.format(i.tp_price) : "–")}
      ${td(esc(i.client_order_id), "l")}${td(esc(i.exchange_order_id || "–"), "l")}</tr>`).join("")
      : `<tr><td class="l muted" colspan="11">No order intents yet</td></tr>`) + "</tbody>";

  const trs = s.trades || [];
  $("botTradeCount").textContent = `${w.closed_trades ?? 0} closed`;
  $("botTrades").innerHTML = `<thead><tr><th>Closed</th><th class="l">Symbol</th><th class="l">Side</th><th>Lots</th><th>Entry</th><th>Exit</th><th class="l">Reason</th>
    <th>Gross ₹</th><th>Fees+GST ₹</th><th>Funding ₹</th><th>Net ₹</th><th>Equity ₹</th></tr></thead><tbody>` +
    (trs.length ? trs.map((t) => `<tr>${td(shortTime(t.closed_at))}${td(esc(t.symbol), "l")}${td(esc(t.side), "l")}${td(t.contracts)}
      ${td(nf2.format(t.entry_price))}${td(nf2.format(t.exit_price))}${td(esc(t.exit_reason), "l")}${td(inr(t.gross_pnl_inr))}${td(inr(-t.fees_gst_inr))}
      ${td(inr(t.funding_inr))}${td(`<span class="${cls(t.net_pnl_inr)}">${inr(t.net_pnl_inr)}</span>`)}${td(inr(t.equity_after_inr, false))}</tr>`).join("")
      : `<tr><td class="l muted" colspan="12">No closed bot trades yet</td></tr>`) + "</tbody>";

  $("botRecon").textContent = s.last_reconciled_at ? `last reconciled ${shortTime(s.last_reconciled_at)} UTC · ws gen ${s.ws_generation ?? "–"}` : "never reconciled";
  const rows = [
    ...(s.transitions || []).map((t) => ({ at: t.at, level: "STATE", kind: `${t.from || "–"} → ${t.to}`, detail: t.reason || "" })),
    ...(s.events || []).map((e) => ({ at: e.at, level: e.level, kind: e.kind + (e.symbol ? ` ${e.symbol}` : ""), detail: e.detail || "" })),
  ].sort((a, b) => (a.at < b.at ? 1 : -1)).slice(0, 60);
  $("botEvents").innerHTML = `<thead><tr><th>Time (UTC)</th><th class="l">Level</th><th class="l">Event</th><th class="l">Detail</th></tr></thead><tbody>` +
    (rows.length ? rows.map((r) => `<tr class="lvl-${esc(r.level)}">${td(shortTime(r.at))}${td(esc(r.level), "l")}${td(esc(r.kind), "l")}
      <td class="l" title="${esc(r.detail)}">${esc(String(r.detail).slice(0, 110))}</td></tr>`).join("")
      : `<tr><td class="l muted" colspan="4">No events yet</td></tr>`) + "</tbody>";
}

async function botAction(label, path, body = null, confirmText = null) {
  if (confirmText && !confirm(confirmText)) return;
  if (bot.busy) return;
  bot.busy = true;
  botMsg(`${label}…`);
  document.querySelectorAll(".bot-controls button").forEach((b) => { b.dataset.wasDisabled = b.disabled; b.disabled = true; });
  try {
    const r = await api(path, { method: "POST", body: body ? JSON.stringify(body) : null });
    botMsg(`${label}: ${botSummary(path, r)}`);
    toast(`${label} done`);
  } catch (e) {
    botMsg(`${label} failed: ${e.message}`, true);
    toast(`${label} failed: ${e.message}`, true);
  } finally {
    bot.busy = false;
    document.querySelectorAll(".bot-controls button").forEach((b) => { b.disabled = b.dataset.wasDisabled === "true"; });
    loadBotStatus();
  }
}

function botSummary(path, r) {
  if (path.endsWith("/step")) {
    const ev = Object.entries(r.evaluations || {}).map(([k, v]) => `${k} ${v.signal_side || "no signal"}`).join(", ");
    return `${r.state} in ${r.seconds}s · ${ev}${r.entries.length ? ` · ${r.entries.length} entry` : ""}${r.summary.issues.length ? ` · issues: ${r.summary.issues.join("; ")}` : ""}`;
  }
  if (path.endsWith("/start")) return `PID ${r.pid} (${r.mode}) · log ${r.log_file}`;
  if (path.endsWith("/stop")) return r.stopped ? `stopped (exit ${r.exit_code ?? "?"}${r.forced_terminate ? ", forced" : ""}) · lock released` : r.reason || "not stopped";
  if (path.endsWith("/flatten")) return `${r.status} · closed ${(r.closed || []).map((c) => `${c.symbol} @ ${c.exit_price ?? "?"}`).join(", ") || "nothing open"}${(r.residual || []).length ? ` · RESIDUAL ${JSON.stringify(r.residual)}` : ""} · entries paused`;
  if (path.endsWith("/backup")) return `${r.backup} · integrity ${r.verify.integrity.join(",")} · ${r.verify.bytes} bytes`;
  if (path.endsWith("/test-telegram")) return r.status + (r.error ? ` (${r.error})` : "");
  return JSON.stringify(r);
}

function bindBot() {
  document.querySelectorAll(".vbtn").forEach((b) => { b.onclick = () => showView(b.dataset.view); });
  $("botStep").onclick = () => botAction("Run 1 cycle", "/api/bot/step");
  $("botStart").onclick = () => {
    const mode = $("botMode").value;
    botAction(`Start ${mode} loop`, "/api/bot/start", { mode },
      `Start the 24/7 ${mode.toUpperCase()} bot loop as a background process?\n\nIt keeps running after you close this page; stop it with "Stop Bot Process".`);
  };
  $("botPause").onclick = () => botAction("Pause new entries", "/api/bot/pause", null, "Pause NEW entries? Trailing stops and exits stay active.");
  $("botResume").onclick = () => botAction("Resume entries", "/api/bot/resume");
  $("botStop").onclick = () => botAction("Stop bot", "/api/bot/stop", null,
    "Stop the bot process?\n\nOpen paper positions stop being managed until it is restarted; exchange-resident SL/TP orders (testnet/live) stay in place.");
  $("botFlatten").onclick = () => {
    const typed = prompt("EMERGENCY FLATTEN closes every open bot position at market and pauses new entries.\n\nType FLATTEN to confirm:");
    if (typed !== "FLATTEN") { botMsg("Flatten cancelled"); return; }
    botAction("Emergency flatten", "/api/bot/flatten");
  };
  $("botBackup").onclick = () => botAction("Backup state DB", "/api/bot/backup");
  $("botTelegram").onclick = () => botAction("Test Telegram alert", "/api/bot/test-telegram");
}

/* ------------------------------------------------------------------ Step 8: manual trade terminal */
const mt = { symbol: "BTCUSD", res: "5m", timer: null, chart: null, series: null, lines: [], lastBarTime: null, chartKey: null,
  catalog: null, product: null, state: null, previewTimer: null, preview: null, busy: false, protectSym: null, requestId: null };

function mtBinding(src, confirmText) {
  const b = { expected_mode: src.mode, mode_generation: src.mode_generation };
  if (src.mode === "live") b.confirm = confirmText;
  return b;
}
function mtNewRequestId() { return `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`; }
function mtLiveConfirm(question) {
  return prompt(`⚠ REAL MONEY — ${question}\n\nType LIVE to send it:`) === "LIVE" ? "LIVE" : null;
}
const LEV_CHIPS = [1, 2, 3, 5, 10, 20, 25, 50, 75, 100, 125, 150, 200];

function fmtPx(x) {
  if (!isNum(x)) return "–";
  const t = mt.product ? mt.product.tick_size : 0.01;
  const d = Math.min(8, Math.max(2, decimals(t)));
  return x.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function mtMsg(text, isError = false) {
  $("mtMsg").textContent = text;
  $("mtMsg").classList.toggle("error", isError);
  $("mtMsg").style.color = isError ? "var(--down)" : "";
}

async function mtLoadCatalog() {
  if (mt.catalog) return;
  try {
    const c = await api("/api/products/catalog");
    mt.catalog = Object.fromEntries(c.products.map((p) => [p.symbol, p]));
    $("mtCatalog").innerHTML = c.products.map((p) => `<option value="${esc(p.symbol)}">${esc(p.description || "")} · max ${p.max_leverage}x</option>`).join("");
  } catch (e) { mtMsg(`catalog failed: ${e.message}`, true); }
}

function mtInitChart() {
  if (mt.chart || !window.LightweightCharts) return;
  mt.chart = LightweightCharts.createChart($("mtChart"), baseChartOptions());
  mt.series = mt.chart.addCandlestickSeries({ upColor: COLORS.up, downColor: COLORS.down, borderVisible: false,
    wickUpColor: COLORS.up, wickDownColor: COLORS.down });
}

function mtSetLevBounds(maxLev) {
  const m = Math.max(1, Math.floor(maxLev || 100));
  $("mtLevRange").max = m;
  $("mtLev").max = m;
  $("mtLevMax").textContent = `(max ${maxLev}x)`;
  if (Number($("mtLev").value) > m) { $("mtLev").value = m; $("mtLevRange").value = m; }
  $("mtLevChips").innerHTML = LEV_CHIPS.filter((x) => x <= m).map((x) => `<button type="button" data-lev="${x}">${x}x</button>`).join("");
  $("mtLevChips").querySelectorAll("button").forEach((b) => { b.onclick = () => { mtSetLev(Number(b.dataset.lev)); }; });
  mtHighlightChip();
}

function mtSetLev(v) {
  const m = Number($("mtLev").max) || 100;
  v = Math.min(m, Math.max(1, Math.round(Number(v) || 1)));
  $("mtLev").value = v;
  $("mtLevRange").value = v;
  mtHighlightChip();
  mtSchedulePreview();
}
function mtHighlightChip() {
  const v = Number($("mtLev").value);
  $("mtLevChips").querySelectorAll("button").forEach((b) => b.classList.toggle("active", Number(b.dataset.lev) === v));
}

function mtReadOrder(side) {
  const type = document.querySelector('input[name="mtType"]:checked').value;
  const size = document.querySelector('input[name="mtSize"]:checked').value;
  const v = Number($("mtSizeVal").value);
  const body = { symbol: mt.symbol, side, order_type: type, leverage: Number($("mtLev").value) };
  if (type === "limit") body.limit_price = Number($("mtLimit").value) || null;
  if (size === "margin") body.margin_inr = v; else if (size === "contracts") body.contracts = Math.floor(v); else body.risk_pct = v;
  const sl = Number($("mtSl").value), tp = Number($("mtTp").value), tr = Number($("mtTrail").value);
  if (sl > 0) body[$("mtSlMode").value === "pct" ? "sl_pct" : "sl_price"] = sl;
  if (tp > 0) body[$("mtTpMode").value === "pct" ? "tp_pct" : "tp_price"] = tp;
  if (tr > 0) body.trail_amount = tr;
  return body;
}

function mtSchedulePreview() {
  clearTimeout(mt.previewTimer);
  mt.previewTimer = setTimeout(mtRunPreview, 350);
}

function kv(k, v, c = "") { return `<div class="kv"><span>${k}</span><span class="${c}">${v}</span></div>`; }

async function mtRunPreview() {
  const box = $("mtPreview");
  try {
    const [l, s] = await Promise.all([api("/api/manual/preview", { method: "POST", body: JSON.stringify(mtReadOrder("LONG")) }),
      api("/api/manual/preview", { method: "POST", body: JSON.stringify(mtReadOrder("SHORT")) })]);
    mt.preview = { LONG: l, SHORT: s };
    const p = l;
    box.innerHTML = kv("Contracts", `${p.contracts} × ${p.contract_value}`) +
      kv("Order value", `${inr(p.notional_inr, false)} · $${nf2.format(p.notional_usd)}`) +
      kv("Required margin", `${inr(p.required_margin_inr, false)} @ ${p.leverage}x`) +
      kv(`Est. fee + GST (${p.fee_type})`, inr(p.est_entry_fee_inr, false)) +
      kv("Est. entry LONG / SHORT", `${fmtPx(l.est_entry_price)} / ${fmtPx(s.est_entry_price)}`) +
      kv("Est. liq. LONG / SHORT", `<span class="neg">${fmtPx(l.est_liquidation_price)}</span> / <span class="neg">${fmtPx(s.est_liquidation_price)}</span>`) +
      (isNum(l.sl_loss_inr) || isNum(s.sl_loss_inr) ? kv("SL loss LONG / SHORT", `${inr(-(l.sl_loss_inr ?? NaN))} / ${inr(-(s.sl_loss_inr ?? NaN))}`) : "") +
      (isNum(l.tp_gain_inr) || isNum(s.tp_gain_inr) ? kv("TP gain LONG / SHORT", `${inr(l.tp_gain_inr ?? NaN)} / ${inr(s.tp_gain_inr ?? NaN)}`) : "") +
      kv("Available margin", inr(p.available_margin_inr, false)) +
      (p.real_money ? `<div class="real-note">⚠ LIVE — orders use REAL MONEY on Delta Exchange India</div>` : "") +
      [...new Set([...(l.problems || []).map((x) => `LONG: ${x}`), ...(s.problems || []).map((x) => `SHORT: ${x}`)])]
        .map((x) => `<div class="warn">⚠ ${esc(x)}</div>`).join("");
    mtUpdateButtons();
  } catch (e) {
    mt.preview = null;
    box.innerHTML = `<div class="err">${esc(e.message)}</div>`;
    mtUpdateButtons();
  }
}

function mtUpdateButtons() {
  const enabled = mt.state && mt.state.manual_enabled;
  $("mtBuy").disabled = !enabled || !mt.preview || !mt.preview.LONG.can_submit || mt.busy;
  $("mtSell").disabled = !enabled || !mt.preview || !mt.preview.SHORT.can_submit || mt.busy;
}

async function mtPlace(side) {
  const pv = mt.preview && mt.preview[side];
  if (!pv) return;
  if (pv.mode !== mt.state.mode || pv.mode_generation !== mt.state.mode_generation) {
    mtMsg("The trading mode changed after this ticket was priced — review the new preview first", true);
    mt.preview = null; mtUpdateButtons(); mtSchedulePreview();
    return;
  }
  const txt = `${side === "LONG" ? "BUY / LONG" : "SELL / SHORT"} ${pv.contracts} ${pv.symbol} @ ${pv.order_type.toUpperCase()}${pv.resting_limit ? " (resting limit)" : ""}\n` +
    `Leverage ${pv.leverage}x · margin ${inr(pv.required_margin_inr, false)} · value ${inr(pv.notional_inr, false)}\n` +
    `Est. entry ${fmtPx(pv.est_entry_price)} · est. liquidation ${fmtPx(pv.est_liquidation_price)}\n` +
    `SL ${pv.sl_price ?? "none"} · TP ${pv.tp_price ?? "none"}${pv.trail_amount ? ` · trail ${pv.trail_amount}` : ""}\n\nPlace this ${(pv.mode || "paper").toUpperCase()} order?`;
  let typed = null;
  if (pv.real_money) {
    typed = prompt(`⚠ REAL MONEY ORDER on Delta Exchange India\n\n${txt}\n\nType LIVE to send it:`);
    if (typed !== "LIVE") { mtMsg("Live order cancelled"); return; }
  } else if (!confirm(txt)) return;
  mt.busy = true; mtUpdateButtons();
  mt.requestId = mt.requestId || mtNewRequestId();
  try {
    const body = { ...mtReadOrder(side), ...mtBinding(pv, typed), client_request_id: mt.requestId };
    const r = await api("/api/manual/order", { method: "POST", body: JSON.stringify(body) });
    mt.requestId = null;
    const f = r.fill || {};
    const ex = r.exchange || {};
    const px = f.price || ex.price;
    mtMsg(`${side} ${pv.symbol} [${(r.mode || mt.state.mode || "paper").toUpperCase()}]: ${r.result}${px ? ` @ ${fmtPx(px)}` : ""}${r.result === "resting" ? ` (limit ${req_limit(pv)})` : ""}${ex.exchange_order_id ? ` · order ${ex.exchange_order_id}` : ""}`);
    toast(`Manual ${side} ${pv.symbol}: ${r.result}`);
  } catch (e) {
    if (e.status) mt.requestId = null;
    mtMsg(`Order failed: ${e.message}`, true);
    toast(`Order failed: ${e.message}`, true);
  } finally {
    mt.busy = false;
    mtLoadState(true);
  }
}

function mtRenderCandles(bars, force) {
  if (!mt.series || !bars) return;
  const key = `${mt.symbol}:${mt.res}`;
  if (force || mt.chartKey !== key || !mt.lastBarTime) {
    mt.series.setData(bars);
    mt.chart.timeScale().fitContent();
    mt.chartKey = key;
  } else {
    bars.filter((b) => b.time >= mt.lastBarTime).forEach((b) => mt.series.update(b));
  }
  if (bars.length) mt.lastBarTime = bars[bars.length - 1].time;
  mt.series.applyOptions({ priceFormat: { type: "price", precision: Math.min(8, decimals(mt.product ? mt.product.tick_size : 0.01)),
    minMove: mt.product ? mt.product.tick_size : 0.01 } });
}

function mtRenderLines(positions) {
  mt.lines.forEach((l) => mt.series.removePriceLine(l));
  mt.lines = [];
  const p = positions.find((x) => x.symbol === mt.symbol);
  if (!p || !mt.series) return;
  const add = (price, color, title, style = 0) => { if (isNum(price)) mt.lines.push(mt.series.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title })); };
  add(p.entry_price, COLORS.accent, `${p.side} ${p.contracts} @ ${p.leverage}x`);
  add(p.sl_price, COLORS.down, "SL", 2);
  add(p.tp_price, COLORS.up, "TP", 2);
  add(p.liq_price_est, "#f59e0b", "LIQ (est)", 1);
}

function mtRender(s, forceChart) {
  mt.state = s;
  mt.product = s.product;
  const w = s.wallet;
  const mode = (s.mode || s.journal.mode || "paper").toUpperCase();
  mtRenderModeBar(s);
  setBadge("mtModeBadge", mode === "PAPER" ? `PAPER WALLET ${inr(w.initial_capital_inr, false)}` : mode === "LIVE" ? "LIVE WALLET (Delta India)" : "TESTNET WALLET",
    mode === "LIVE" ? "badge-live-real" : mode === "TESTNET" ? "badge-testnet" : "badge-paper-blue");
  $("mtBalance").textContent = inr(w.realized_equity_inr, false);
  $("mtAvail").textContent = inr(w.available_margin_inr, false);
  $("mtUsed").textContent = inr(w.used_margin_inr, false);
  $("mtUpnl").innerHTML = `<span class="${cls(w.unrealized_pnl_inr)}">${inr(w.unrealized_pnl_inr)}</span>`;
  $("mtEquity").textContent = inr(w.total_equity_inr, false);
  const wt = s.watcher || {};
  $("mtWatch").textContent = `paper watcher: ${wt.status || "–"}${wt.last_error ? ` · ${wt.last_error}` : ""}`;
  $("mtBotBanner").classList.toggle("hidden", !s.bot.running);

  const syms = [...new Set([...(s.coins || []), ...s.positions.map((p) => p.symbol), mt.symbol])];
  const sel = $("mtSymbol");
  if (sel.options.length !== syms.length || [...sel.options].some((o, i) => o.value !== syms[i])) {
    sel.innerHTML = syms.map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
  }
  sel.value = mt.symbol;
  const pr = s.product;
  $("mtProduct").textContent = `${pr.symbol} · id ${pr.product_id} · 1 contract = ${pr.contract_value} ${pr.underlying || ""} · tick ${pr.tick_size} · max ${pr.max_leverage}x · maint. ${pr.maintenance_margin_pct}%`;
  if (Number($("mtLevRange").max) !== Math.floor(pr.max_leverage || 100)) mtSetLevBounds(pr.max_leverage);

  const t = s.ticker;
  $("mtTicker").innerHTML = `<span class="sym">${esc(s.symbol)}</span><span class="px ${cls(t.change_24h_pct)}">${fmtPx(t.last_price)}</span>
    <span><span class="lbl">24h</span><span class="${cls(t.change_24h_pct)}">${pct(t.change_24h_pct, true)}</span></span>
    <span><span class="lbl">mark</span>${fmtPx(t.mark_price)}</span><span><span class="lbl">spot</span>${fmtPx(t.spot_price)}</span>
    <span><span class="lbl">bid</span><span class="pos">${fmtPx(t.best_bid)}</span></span><span><span class="lbl">ask</span><span class="neg">${fmtPx(t.best_ask)}</span></span>
    <span><span class="lbl">funding/8h</span>${pct(t.funding_rate_pct, true, 4)}</span>
    <span><span class="lbl">24h H/L</span>${fmtPx(t.high_24h)} / ${fmtPx(t.low_24h)}</span>`;
  mtRenderCandles(s.candles.bars, forceChart);
  mtRenderLines(s.positions);

  const pos = s.positions;
  $("mtPosNote").textContent = pos.length ? `${pos.length} open · auto-refresh 2.5s` : "flat";
  $("mtPositions").innerHTML = `<thead><tr><th class="l">Symbol</th><th class="l">Side</th><th>Lev</th><th>Contracts</th><th>Entry</th><th>Mark</th>
    <th>Est. liq</th><th>Margin ₹</th><th>uPnL ₹</th><th>ROE</th><th>SL</th><th>TP</th><th>Trail</th><th class="l">Source</th><th class="l">Actions</th></tr></thead><tbody>` +
    (pos.length ? pos.map((p) => `<tr>${td(esc(p.symbol), "l")}${td(`<span class="${p.side === "LONG" ? "pos" : "neg"}">${p.side}</span>`, "l")}
      ${td(p.leverage ? `${p.leverage}x` : "–")}${td(p.contracts)}${td(fmtPx(p.entry_price))}${td(fmtPx(p.mark_price))}${td(`<span class="neg">${fmtPx(p.liq_price_est)}</span>${p.liq_source === "delta" ? ' <span class="muted">Δ</span>' : ""}`)}
      ${td(inr(p.margin_inr, false))}${td(`<span class="${cls(p.unrealized_pnl_inr)}">${inr(p.unrealized_pnl_inr)}</span>`)}
      ${td(`<span class="${cls(p.roe_pct)}">${pct(p.roe_pct, true)}</span>`)}${td(isNum(p.sl_price) ? fmtPx(p.sl_price) : "–")}
      ${td(isNum(p.tp_price) ? fmtPx(p.tp_price) : "–")}${td(p.trail_amount ? p.trail_amount : "–")}${td(esc(p.source || "bot"), "l")}
      <td class="l"><button class="btn btn-xs" data-edit="${esc(p.symbol)}">Edit SL/TP</button>
      <button class="btn btn-xs btn-danger" data-close="${esc(p.symbol)}">Close</button>
      <button class="btn btn-xs" data-part="${esc(p.symbol)}">Close part…</button></td></tr>`).join("")
      : `<tr><td class="l muted" colspan="15">No open positions</td></tr>`) + "</tbody>";
  $("mtPositions").querySelectorAll("[data-edit]").forEach((b) => { b.onclick = () => mtOpenProtect(b.dataset.edit); });
  $("mtPositions").querySelectorAll("[data-close]").forEach((b) => { b.onclick = () => mtClose(b.dataset.close, null); });
  $("mtPositions").querySelectorAll("[data-part]").forEach((b) => { b.onclick = () => {
    const p = pos.find((x) => x.symbol === b.dataset.part);
    const n = Number(prompt(`Close how many of ${p.contracts} ${p.symbol} contracts?`, String(Math.max(1, Math.floor(p.contracts / 2)))));
    if (n >= 1) mtClose(p.symbol, Math.floor(n));
  }; });
  const disabled = !s.manual_enabled;
  $("mtPositions").querySelectorAll("button").forEach((b) => { b.disabled = disabled; });

  const pend = s.pending_orders || [];
  $("mtPending").innerHTML = `<thead><tr><th class="l">Symbol</th><th class="l">Side</th><th>Size</th><th>Limit</th><th>Lev</th><th>SL</th><th>TP</th><th class="l"></th></tr></thead><tbody>` +
    (pend.length ? pend.map((o) => `<tr>${td(esc(o.symbol), "l")}${td(esc(o.side), "l")}${td(o.size)}${td(fmtPx(o.limit_price))}${td(`${o.leverage}x`)}
      ${td(isNum(o.sl_price) ? fmtPx(o.sl_price) : "–")}${td(isNum(o.tp_price) ? fmtPx(o.tp_price) : "–")}
      <td class="l"><button class="btn btn-xs" data-cancel="${esc(o.intent_id || o.exchange_order_id)}" ${disabled ? "disabled" : ""}>Cancel</button>${o.external ? ' <span class="muted">exchange</span>' : ""}</td></tr>`).join("")
      : `<tr><td class="l muted" colspan="8">No resting limit orders</td></tr>`) + "</tbody>";
  $("mtPending").querySelectorAll("[data-cancel]").forEach((b) => { b.onclick = async () => {
    let typed = null;
    if (mt.state.real_money && !(typed = mtLiveConfirm(`cancel LIVE order ${b.dataset.cancel} on Delta Exchange India?`))) return;
    const q = new URLSearchParams(Object.entries(mtBinding(mt.state, typed)).filter(([, v]) => v !== null && v !== undefined));
    try { await api(`/api/manual/order/${b.dataset.cancel}?${q}`, { method: "DELETE" }); toast("Order cancelled"); } catch (e) { toast(e.message, true); }
    mtLoadState(true);
  }; });

  const trs = s.trades || [];
  $("mtTradeNote").textContent = `${trs.length} shown`;
  $("mtTrades").innerHTML = `<thead><tr><th>Closed</th><th class="l">Symbol</th><th class="l">Side</th><th class="l">Source</th><th>Lev</th><th>Lots</th><th>Entry</th><th>Exit</th>
    <th class="l">Reason</th><th>Gross ₹</th><th>Fees+GST ₹</th><th>Funding ₹</th><th>Net ₹</th><th>Equity ₹</th></tr></thead><tbody>` +
    (trs.length ? trs.map((t) => `<tr>${td(shortTime(t.closed_at))}${td(esc(t.symbol), "l")}${td(esc(t.side), "l")}${td(esc(t.source || "bot"), "l")}
      ${td(t.leverage ? `${t.leverage}x` : "–")}${td(t.contracts)}${td(fmtPx(t.entry_price))}${td(fmtPx(t.exit_price))}${td(esc(t.exit_reason), "l")}
      ${td(inr(t.gross_pnl_inr))}${td(inr(-t.fees_gst_inr))}${td(inr(t.funding_inr))}${td(`<span class="${cls(t.net_pnl_inr)}">${inr(t.net_pnl_inr)}</span>`)}
      ${td(inr(t.equity_after_inr, false))}</tr>`).join("")
      : `<tr><td class="l muted" colspan="14">No closed trades yet</td></tr>`) + "</tbody>";
  mtUpdateButtons();
}

async function mtLoadState(forceChart = false) {
  try {
    const s = await api(`/api/manual/state?symbol=${encodeURIComponent(mt.symbol)}&resolution=${mt.res}`);
    mtRender(s, forceChart);
  } catch (e) {
    mtMsg(`state failed: ${e.message}`, true);
  }
}

async function mtSelect(sym, add = false) {
  sym = String(sym || "").trim().toUpperCase();
  if (!sym) return;
  if (mt.catalog && !mt.catalog[sym]) { mtMsg(`${sym} is not a live Delta India perpetual`, true); return; }
  try {
    if (add) {
      const r = await api("/api/manual/symbols", { method: "POST", body: JSON.stringify({ symbol: sym }) });
      toast(`${sym} added · auto-bot profile ${r.auto_bot_profile} · history ${r.backfill}`);
    }
    mt.symbol = sym;
    mt.lastBarTime = null;
    $("mtSearch").value = "";
    mtMsg(`${sym} loaded`);
    await mtLoadState(true);
    mtSchedulePreview();
  } catch (e) { mtMsg(e.message, true); }
}

function mtOpenProtect(sym) {
  const p = mt.state.positions.find((x) => x.symbol === sym);
  if (!p) return;
  mt.protectSym = sym;
  $("mtProtectTitle").textContent = `Edit protection — ${sym} ${p.side} ${p.contracts} @ ${fmtPx(p.entry_price)} (mark ${fmtPx(p.mark_price)}, est. liq ${fmtPx(p.liq_price_est)})`;
  $("mtPSl").value = isNum(p.sl_price) ? p.sl_price : "";
  $("mtPTp").value = isNum(p.tp_price) ? p.tp_price : "";
  $("mtPTrail").value = p.trail_amount || 0;
  $("mtPClearSl").checked = false;
  $("mtPClearTp").checked = false;
  $("mtProtectMsg").textContent = "";
  $("mtProtectDialog").showModal();
}

async function mtSaveProtect(ev) {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  const body = {};
  const sl = Number($("mtPSl").value), tp = Number($("mtPTp").value);
  if ($("mtPClearSl").checked) body.clear_sl = true; else if (sl > 0) body.sl_price = sl;
  if ($("mtPClearTp").checked) body.clear_tp = true; else if (tp > 0) body.tp_price = tp;
  if ($("mtPTrail").value !== "") body.trail_amount = Number($("mtPTrail").value);
  let typed = null;
  if (mt.state.real_money && !(typed = mtLiveConfirm(`change SL/TP of the LIVE ${mt.protectSym} position on Delta Exchange India?`))) return;
  Object.assign(body, mtBinding(mt.state, typed));
  try {
    await api(`/api/manual/position/${mt.protectSym}/protection`, { method: "PUT", body: JSON.stringify(body) });
    $("mtProtectDialog").close();
    toast(`${mt.protectSym} protection updated`);
    mtLoadState();
  } catch (e) { $("mtProtectMsg").textContent = e.message; }
}

async function mtClose(sym, contracts) {
  const p = mt.state.positions.find((x) => x.symbol === sym);
  const q = `Close ${contracts ?? p.contracts} of ${p.contracts} ${sym} ${p.side} at market (reduce-only)?`;
  let typed = null;
  if (mt.state.real_money) {
    if (!(typed = mtLiveConfirm(q))) return;
  } else if (!confirm(q)) return;
  const body = { ...mtBinding(mt.state, typed), client_request_id: mtNewRequestId() };
  if (contracts) body.contracts = contracts;
  try {
    const r = await api(`/api/manual/position/${sym}/close`, { method: "POST", body: JSON.stringify(body) });
    const t = r.trade || {};
    mtMsg(`${sym}: ${r.result} @ ${fmtPx(t.exit_price)} · net ${inr(t.net_pnl_inr)} · ${r.remaining} left`);
    toast(`${sym} closed: net ${inr(t.net_pnl_inr)}`);
  } catch (e) { mtMsg(`Close failed: ${e.message}`, true); toast(e.message, true); }
  mtLoadState(true);
}

function mtEnter() {
  mtInitChart();
  mtLoadCatalog();
  mtLoadState(true).then(mtSchedulePreview);
  clearInterval(mt.timer);
  mt.timer = setInterval(() => { if (!document.hidden) mtLoadState(); }, 2500);
}

function req_limit(pv) { return pv.order_type === "limit" ? pv.reference_price : ""; }

function mtRenderModeBar(s) {
  const mode = s.mode || "paper";
  document.querySelectorAll("#mtModeBar .mbtn").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("mtModeTestnet").classList.toggle("hidden", !(s.gates && s.gates.testnet_keys) && mode !== "testnet");
  const liveOk = s.gates && s.gates.live_gates_open && s.gates.live_keys;
  $("mtModeLive").disabled = !liveOk && mode !== "live";
  $("mtModeLive").title = liveOk ? "real-money manual orders on Delta Exchange India" :
    "needs MODE=live, LIVE_TRADING_ENABLED=true and DELTA_API_KEY / DELTA_API_SECRET in .env";
  const tag = mode === "live" ? ["LIVE — REAL MONEY (Delta India)", "badge-live-real"] :
    mode === "testnet" ? ["TESTNET — Delta India demo", "badge-testnet"] : [`PAPER — Virtual ${inr(ee_capital(s), false)}`, "badge-paper-blue"];
  setBadge("mtModeTag", tag[0], tag[1]);
  $("mtModeGen").textContent = `mode #${s.mode_generation ?? 0}`;
  $("manualView").classList.toggle("real-money", mode === "live");
  $("mtTrail").disabled = mode !== "paper";
  $("mtTrail").placeholder = mode === "paper" ? "e.g. 1.5" : "paper only";
  if (s.exchange && !s.exchange.connected) $("mtConn").innerHTML = `<span class="bad">✖ ${esc(s.exchange.error || "not connected")}</span>`;
}
function ee_capital(s) { return (s.wallet && s.wallet.initial_capital_inr) || 50000; }

async function mtSwitchMode(mode) {
  let body = { mode };
  if (mode === "live") {
    const typed = prompt("Type LIVE to enable real-money manual orders on Delta Exchange India");
    if (typed !== "LIVE") { mtMsg("Still in the previous mode (LIVE not confirmed)"); return; }
    body.confirm = "LIVE";
  }
  try {
    const r = await api("/api/manual/mode", { method: "POST", body: JSON.stringify(body) });
    toast(`Manual mode: ${r.mode.toUpperCase()} (journal ${r.journal.split(/[\\/]/).pop()})`);
    mt.preview = null;
    mt.requestId = null;
    await mtLoadState(true);
    mtSchedulePreview();
    if (mode !== "paper") mtVerify();
  } catch (e) { mtMsg(e.message, true); toast(e.message, true); }
}

async function mtVerify() {
  $("mtConn").textContent = "checking…";
  try {
    const c = await api("/api/manual/connection_status?verify=true");
    const mode = c.active_mode;
    const parts = [];
    for (const m of ["live", "testnet"]) {
      const x = c.modes[m];
      if (!x.key_present) { if (m === "live") parts.push(`<span class="bad">LIVE: no API key in .env</span>`); continue; }
      parts.push(x.connected ? `<span class="ok">${m.toUpperCase()} ${esc(x.api_key_masked)} Connected ✔</span> · equity ${inr(x.wallet_equity_inr, false)} · available ${inr(x.available_margin_inr, false)} · ${x.open_positions} open position(s)`
        : `<span class="bad">${m.toUpperCase()} ${esc(x.api_key_masked || "")} ✖ ${esc(x.message || "")}</span>`);
    }
    if (!c.live_gates_open) parts.push(`<span class="muted">live gates closed (MODE=${esc(c.MODE)}, LIVE_TRADING_ENABLED=${c.LIVE_TRADING_ENABLED})</span>`);
    if (mode !== "paper") {
      try { await api("/api/manual/sync_exchange", { method: "POST" }); } catch (e) { parts.push(`<span class="bad">sync: ${esc(e.message)}</span>`); }
    }
    $("mtConn").innerHTML = parts.join(" &nbsp;|&nbsp; ") || "paper mode — no exchange account";
    mtLoadState();
  } catch (e) { $("mtConn").innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
}

function bindManual() {
  document.querySelectorAll("#mtModeBar .mbtn").forEach((b) => { b.onclick = () => { if (!b.classList.contains("active")) mtSwitchMode(b.dataset.mode); }; });
  $("mtVerify").onclick = mtVerify;
  $("mtSymbol").onchange = () => mtSelect($("mtSymbol").value);
  $("mtLoad").onclick = () => mtSelect($("mtSearch").value);
  $("mtAdd").onclick = () => mtSelect($("mtSearch").value || mt.symbol, true);
  $("mtSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") mtSelect($("mtSearch").value); });
  $("mtLevRange").oninput = () => mtSetLev($("mtLevRange").value);
  $("mtLev").onchange = () => mtSetLev($("mtLev").value);
  document.querySelectorAll('input[name="mtType"]').forEach((r) => { r.onchange = () => {
    const lim = document.querySelector('input[name="mtType"]:checked').value === "limit";
    $("mtLimit").classList.toggle("hidden", !lim);
    if (lim && !$("mtLimit").value && mt.state) $("mtLimit").value = mt.state.ticker.best_bid;
    mtSchedulePreview();
  }; });
  document.querySelectorAll('input[name="mtSize"]').forEach((r) => { r.onchange = () => {
    const v = document.querySelector('input[name="mtSize"]:checked').value;
    $("mtSizeVal").value = v === "margin" ? 5000 : v === "contracts" ? 1 : 1;
    mtSchedulePreview();
  }; });
  ["mtSizeVal", "mtLimit", "mtSl", "mtTp", "mtTrail", "mtSlMode", "mtTpMode"].forEach((id) => $(id).addEventListener("input", mtSchedulePreview));
  ["mtSlMode", "mtTpMode"].forEach((id) => $(id).addEventListener("change", mtSchedulePreview));
  $("mtBuy").onclick = () => mtPlace("LONG");
  $("mtSell").onclick = () => mtPlace("SHORT");
  $("mtResTabs").querySelectorAll(".tab").forEach((b) => { b.onclick = () => {
    mt.res = b.dataset.res;
    $("mtResTabs").querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
    mt.lastBarTime = null;
    mtLoadState(true);
  }; });
  $("mtStopBot").onclick = async () => {
    if (!confirm("Stop the 24/7 auto-bot so you can trade manually?\n\nOpen positions are then protected by the dashboard's paper watcher (paper) or exchange orders (testnet).")) return;
    $("mtStopBot").disabled = true;
    try {
      const r = await api("/api/bot/stop", { method: "POST" });
      toast(r.stopped ? "Auto-bot stopped — manual trading enabled" : `Not stopped: ${r.reason || ""}`, !r.stopped);
    } catch (e) { toast(e.message, true); }
    $("mtStopBot").disabled = false;
    mtLoadState();
  };
  $("mtProtectForm").addEventListener("submit", mtSaveProtect);
  mtSetLevBounds(100);
}

async function main() {
  bindBot();
  bindManual();
  loadBotStatus();
  if (location.hash === "#bot") showView("bot");
  if (location.hash === "#manual") showView("manual");
  if (!window.LightweightCharts) {
    toast("Could not load TradingView Lightweight Charts from unpkg.com — check your internet connection.", true);
    return;
  }
  initCharts();
  bind();
  try {
    await loadStatus();
  } catch (e) {
    toast(`Status failed: ${e.message}`, true);
  }
}

main();
