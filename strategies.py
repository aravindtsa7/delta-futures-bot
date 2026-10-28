"""Past-only indicators and signal generators (shared by the backtester and the live bot).

Rules that keep this module free of lookahead:
  * Every indicator value at bar ``i`` uses only bars ``<= i`` (rolling windows, recursive
    smoothing, cumulative sums). Nothing is centred, back-filled or shifted backwards.
  * Strategies are evaluated on CLOSED higher-timeframe bars. A signal on bar ``[t, t+tf)``
    may only be acted on at ``t + tf`` (the backtester enforces this).

Smoothing convention (matches TA-Lib): EMA and Wilder RMA are seeded with the SMA of the
first ``n`` values and are NaN before that, so warm-up is handled by NaN propagation and
comparisons against NaN never produce a signal.

Signal column: ``1`` = long, ``-1`` = short, ``0`` = flat. Signals are EDGE-TRIGGERED: a
signal is emitted only on the bar where the long/short condition turns true, not on every
following bar while it stays true. (The post-exit cooldown depends on position state and is
enforced by the backtester / live executor.)

Optional ``exit_signal`` column: ``1`` = close longs, ``-1`` = close shorts (acted on at the
next bar open only when ``exit_on_opposite_signal`` is enabled).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #
def _seeded_ewm(s: pd.Series, n: int, alpha: float) -> pd.Series:
    arr = s.to_numpy(dtype="float64", copy=True)
    valid = np.flatnonzero(~np.isnan(arr))
    if valid.size < n:
        return pd.Series(np.nan, index=s.index)
    seed_idx = valid[0] + n - 1
    arr[seed_idx] = np.nanmean(arr[valid[0] : seed_idx + 1])
    arr[:seed_idx] = np.nan
    return pd.Series(arr, index=s.index).ewm(alpha=alpha, adjust=False).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return _seeded_ewm(s, n, 2.0 / (n + 1))


def rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's moving average (alpha = 1/n)."""
    return _seeded_ewm(s, n, 1.0 / n)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    tr.iloc[:1] = np.nan  # Wilder: TR needs a previous close
    return tr


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return rma(true_range(df), n)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    avg_gain = rma(delta.clip(lower=0.0), n)
    avg_loss = rma(-delta.clip(upper=0.0), n)
    out = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    out = out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    out = out.mask((avg_loss == 0) & (avg_gain == 0), 50.0)
    return out


def adx(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0).where(up.notna())
    minus_dm = down.where((down > up) & (down > 0), 0.0).where(down.notna())
    atr_ = rma(true_range(df), n)
    plus_di = 100.0 * rma(plus_dm, n) / atr_
    minus_di = 100.0 * rma(minus_dm, n) / atr_
    di_sum = plus_di + minus_di
    dx = (100.0 * (plus_di - minus_di).abs() / di_sum).where(di_sum != 0, 0.0).where(di_sum.notna())
    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx": rma(dx, n)}, index=df.index)


def supertrend(df: pd.DataFrame, n: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    """Classic Supertrend. ``direction`` is +1 (up) / -1 (down), NaN during warm-up."""
    high, low, close = (df[c].to_numpy("float64") for c in ("high", "low", "close"))
    atr_ = atr(df, n).to_numpy()
    hl2 = (high + low) / 2.0
    basic_upper = hl2 + multiplier * atr_
    basic_lower = hl2 - multiplier * atr_

    size = len(df)
    upper = np.full(size, np.nan)
    lower = np.full(size, np.nan)
    direction = np.full(size, np.nan)
    line = np.full(size, np.nan)

    started = False
    for i in range(size):
        if np.isnan(atr_[i]):
            continue
        if not started:
            upper[i], lower[i], direction[i] = basic_upper[i], basic_lower[i], 1.0
            line[i] = lower[i]
            started = True
            continue
        upper[i] = basic_upper[i] if (basic_upper[i] < upper[i - 1] or close[i - 1] > upper[i - 1]) else upper[i - 1]
        lower[i] = basic_lower[i] if (basic_lower[i] > lower[i - 1] or close[i - 1] < lower[i - 1]) else lower[i - 1]
        if direction[i - 1] == -1.0 and close[i] > upper[i]:
            direction[i] = 1.0
        elif direction[i - 1] == 1.0 and close[i] < lower[i]:
            direction[i] = -1.0
        else:
            direction[i] = direction[i - 1]
        line[i] = lower[i] if direction[i] == 1.0 else upper[i]

    return pd.DataFrame(
        {"supertrend": line, "st_direction": direction, "st_upper": upper, "st_lower": lower},
        index=df.index,
    )


def bollinger_bands(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    std = close.rolling(n, min_periods=n).std(ddof=0)
    upper = mid + k * std
    lower = mid - k * std
    return pd.DataFrame(
        {"bb_mid": mid, "bb_upper": upper, "bb_lower": lower, "bb_bandwidth": (upper - lower) / mid},
        index=close.index,
    )


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig}, index=close.index)


def donchian(df: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """Channel of the PRIOR ``n`` bars (excludes the current bar)."""
    return pd.DataFrame(
        {
            "dc_high": df["high"].rolling(n, min_periods=n).max().shift(1),
            "dc_low": df["low"].rolling(n, min_periods=n).min().shift(1),
        },
        index=df.index,
    )


def vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP anchored to each UTC day (index must be a tz-aware UTC DatetimeIndex)."""
    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError("vwap requires a DatetimeIndex")
    day = df.index.tz_convert("UTC").floor("D")
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = (typical * df["volume"]).groupby(day).cumsum()
    vol = df["volume"].groupby(day).cumsum()
    return (pv / vol).where(vol > 0)


def obv(df: pd.DataFrame, obv_span: int = 20) -> pd.DataFrame:
    """Zero-lookahead On-Balance Volume (OBV) and its exponential signal line (OBV_EMA)."""
    span = max(2, int(obv_span))
    direction = np.sign(df["close"].diff()).fillna(0.0)
    obv_s = (direction * df["volume"]).cumsum()
    obv_ema_s = obv_s.ewm(span=span, adjust=False, min_periods=span).mean()
    return pd.DataFrame({"obv": obv_s, "obv_ema": obv_ema_s}, index=df.index)


def rvol(df: pd.DataFrame, vol_ma_period: int = 20) -> pd.Series:
    """Zero-lookahead Relative Volume: volume / SMA(volume, vol_ma_period)."""
    n = max(2, int(vol_ma_period))
    v_ma = df["volume"].rolling(n, min_periods=n).mean().replace(0.0, np.nan)
    return df["volume"] / v_ma


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #
def _edge(cond: pd.Series) -> np.ndarray:
    """True only on bars where ``cond`` turns true (False -> True transition)."""
    c = cond.fillna(False).to_numpy(bool)
    prev = np.concatenate(([False], c[:-1]))
    return c & ~prev


def _signal(long: pd.Series, short: pd.Series) -> pd.Series:
    sig = np.select([_edge(long), _edge(short)], [1, -1], 0)
    return pd.Series(sig.astype("int8"), index=long.index, name="signal")


def _finish(out: pd.DataFrame, long: pd.Series, short: pd.Series) -> None:
    """Store the raw (level) entry conditions and the edge-triggered signal."""
    out["cond_long"] = long.fillna(False).astype(bool)
    out["cond_short"] = short.fillna(False).astype(bool)
    out["signal"] = _signal(out["cond_long"], out["cond_short"])


# --------------------------------------------------------------------------- #
# Entry filters (Step 4B). Applied to the raw conditions BEFORE the edge trigger, so a
# blocked bar never consumes the transition of a later, eligible bar.
# --------------------------------------------------------------------------- #
def trend_filter_4h(bars_htf: pd.DataFrame, bars_4h: pd.DataFrame, htf_minutes: int) -> pd.DataFrame:
    """Closed-4h EMA(50/200) regime, joined to signal bars by AVAILABILITY time.

    A 4h bar opening at t closes at t+4h and is visible to a signal bar only if that signal bar's
    close time >= t+4h. Long allowed: close_4h > EMA200 and EMA50 > EMA200; short: the mirror.
    """
    c4 = bars_4h["close"]
    f = pd.DataFrame({"close_4h": c4, "ema50_4h": ema(c4, 50), "ema200_4h": ema(c4, 200)}, index=bars_4h.index)
    f["src_4h_open"] = f.index
    f["avail_time"] = f.index + pd.Timedelta(hours=4)
    left = pd.DataFrame({"close_time": bars_htf.index + pd.Timedelta(minutes=htf_minutes)}, index=bars_htf.index)
    merged = pd.merge_asof(left.reset_index(), f.reset_index(drop=True).sort_values("avail_time"),
                           left_on="close_time", right_on="avail_time", direction="backward")
    merged.index = bars_htf.index
    above = (merged["close_4h"] > merged["ema200_4h"]) & (merged["ema50_4h"] > merged["ema200_4h"])
    below = (merged["close_4h"] < merged["ema200_4h"]) & (merged["ema50_4h"] < merged["ema200_4h"])
    merged["allow_long"] = above.fillna(False).astype(bool)
    merged["allow_short"] = below.fillna(False).astype(bool)
    return merged.drop(columns=["time"], errors="ignore")


def _closed_4h_regime(bars: pd.DataFrame, ema_custom: int = 100) -> pd.DataFrame:
    """Causal closed-4h regime computed directly from ``bars`` (zero lookahead)."""
    tf_map = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "45m": 45, "1h": 60, "2h": 120, "4h": 240}
    tf_attr = str(bars.attrs.get("timeframe", ""))
    if tf_attr in tf_map:
        htf_min = tf_map[tf_attr]
    elif len(bars) >= 2:
        diffs = pd.Series(bars.index).diff().dropna().dt.total_seconds() / 60.0
        htf_min = max(1, int(round(float(diffs.median()))))
    else:
        htf_min = 60
    if htf_min >= 240:
        b4 = bars
        last_close = b4.index + pd.Timedelta(minutes=htf_min)
    else:
        b4 = pd.DataFrame({
            "open": bars["open"].resample("4h", label="left", closed="left", origin="epoch").first(),
            "high": bars["high"].resample("4h", label="left", closed="left", origin="epoch").max(),
            "low": bars["low"].resample("4h", label="left", closed="left", origin="epoch").min(),
            "close": bars["close"].resample("4h", label="left", closed="left", origin="epoch").last(),
            "volume": bars["volume"].resample("4h", label="left", closed="left", origin="epoch").sum(),
        }).dropna(subset=["close"])
        t_open_s = pd.Series(bars.index, index=bars.index)
        last_open = t_open_s.resample("4h", label="left", closed="left", origin="epoch").max().reindex(b4.index)
        last_close = (last_open + pd.Timedelta(minutes=htf_min)).clip(lower=b4.index + pd.Timedelta(hours=4))
    c4 = b4["close"]
    f = pd.DataFrame({
        "close_4h": c4,
        "ema50_4h": ema(c4, 50),
        "ema100_4h": ema(c4, max(5, int(ema_custom))),
        "ema200_4h": ema(c4, 200),
    }, index=b4.index)
    f["avail_time"] = last_close
    left = pd.DataFrame({"close_time": bars.index + pd.Timedelta(minutes=htf_min)}, index=bars.index)
    merged = pd.merge_asof(
        left.reset_index(),
        f.reset_index(drop=True).sort_values("avail_time"),
        left_on="close_time",
        right_on="avail_time",
        direction="backward",
    )
    merged.index = bars.index
    merged["allow_long_200"] = ((merged["close_4h"] > merged["ema200_4h"]) & (merged["ema50_4h"] > merged["ema200_4h"])).fillna(False).astype(bool)
    merged["allow_short_200"] = ((merged["close_4h"] < merged["ema200_4h"]) & (merged["ema50_4h"] < merged["ema200_4h"])).fillna(False).astype(bool)
    merged["allow_long_100"] = (merged["close_4h"] > merged["ema100_4h"]).fillna(False).astype(bool)
    merged["allow_short_100"] = (merged["close_4h"] < merged["ema100_4h"]).fillna(False).astype(bool)
    return merged


def cost_gate(bars: pd.DataFrame, taker_fee: float, gst_rate: float, slippage_bps: float,
              multiplier: float = 2.0, atr_n: int = 14) -> pd.DataFrame:
    """Raw ATR/close (before any stop clamp) must be >= multiplier x estimated round-trip cost."""
    raw_ratio = atr(bars, atr_n) / bars["close"]
    rt_cost = 2.0 * (taker_fee * (1.0 + gst_rate)) + 2.0 * (slippage_bps / 10_000.0)
    allow = (raw_ratio >= multiplier * rt_cost).fillna(False).astype(bool)
    return pd.DataFrame({"raw_atr_ratio": raw_ratio, "rt_cost_frac": rt_cost, "allow": allow}, index=bars.index)


def apply_entry_filters(out: pd.DataFrame, filters: dict[str, tuple[pd.Series, pd.Series]],
                        count_mask: np.ndarray | None = None) -> tuple[pd.Series, dict[str, int]]:
    """Edge-trigger ``cond & all filters``; count what each filter rejected at raw transitions.

    ``count_mask`` (bool per bar) restricts only the COUNTS (e.g. to a backtest window); the
    signal itself is always computed on the full history so edges stay causal and identical.
    """
    cl, cs = out["cond_long"].to_numpy(bool), out["cond_short"].to_numpy(bool)
    ok_l = np.ones(len(out), bool)
    ok_s = np.ones(len(out), bool)
    per = {}
    for name, (al, ash) in filters.items():
        al, ash = al.reindex(out.index).fillna(False).to_numpy(bool), ash.reindex(out.index).fillna(False).to_numpy(bool)
        per[name] = (al, ash)
        ok_l &= al
        ok_s &= ash
    idx = out.index
    sig = _signal(pd.Series(cl & ok_l, index=idx), pd.Series(cs & ok_s, index=idx))

    m = np.ones(len(out), bool) if count_mask is None else np.asarray(count_mask, bool)
    raw_l, raw_s = _edge(pd.Series(cl, index=idx)) & m, _edge(pd.Series(cs, index=idx)) & m
    raw = raw_l | raw_s
    fired = (sig != 0).to_numpy() & m
    stats = {"raw_transitions": int(raw.sum()), "entries_fired": int(fired.sum())}
    blocked = {}
    for name, (al, ash) in per.items():
        blocked[name] = (raw_l & ~al) | (raw_s & ~ash)
    names = list(per)
    any_block = np.zeros(len(out), bool)
    if names:
        for name in names:
            others = np.zeros(len(out), bool)
            for o in names:
                if o != name:
                    others |= blocked[o]
            stats[f"rejected_{name}_only"] = int((blocked[name] & ~others).sum())
            stats[f"rejected_by_{name}"] = int(blocked[name].sum())
            any_block |= blocked[name]
        if len(names) > 1:
            all_block = np.ones(len(out), bool)
            for name in names:
                all_block &= blocked[name]
            stats["rejected_by_all"] = int(all_block.sum())
    stats["raw_transitions_passed"] = int((raw & ~any_block).sum())
    stats["entries_fired_not_at_raw_transition"] = int((fired & ~raw).sum())
    return sig, stats


def supertrend_ema_rsi(bars: pd.DataFrame) -> pd.DataFrame:
    c = bars["close"]
    out = pd.DataFrame(index=bars.index)
    out["ema21"] = ema(c, 21)
    out["ema200"] = ema(c, 200)
    out["rsi14"] = rsi(c, 14)
    out["adx14"] = adx(bars, 14)["adx"]
    out = out.join(supertrend(bars, 10, 3.0))

    d = out["st_direction"]
    flip_up = (d == 1) & (d.shift(1) == -1)
    flip_dn = (d == -1) & (d.shift(1) == 1)
    cross_up = (c > out["ema21"]) & (c.shift(1) <= out["ema21"].shift(1))
    cross_dn = (c < out["ema21"]) & (c.shift(1) >= out["ema21"].shift(1))
    trend_ok = out["adx14"] >= 18

    long = (c > out["ema200"]) & (d == 1) & (flip_up | cross_up) & out["rsi14"].between(50, 72) & trend_ok
    short = (c < out["ema200"]) & (d == -1) & (flip_dn | cross_dn) & out["rsi14"].between(28, 50) & trend_ok
    _finish(out, long, short)
    return out


def bollinger_volume_breakout(bars: pd.DataFrame) -> pd.DataFrame:
    c, v = bars["close"], bars["volume"]
    out = bollinger_bands(c, 20, 2.0)
    out["ema50"] = ema(c, 50)
    out["vol_sma20"] = sma(v, 20)
    out["bw_p60_50"] = out["bb_bandwidth"].rolling(50, min_periods=50).quantile(0.6)

    squeeze = out["bb_bandwidth"].shift(1) < out["bw_p60_50"].shift(1)
    vol_ok = v >= 1.3 * out["vol_sma20"]

    long = (c > out["bb_upper"]) & squeeze & vol_ok & (c > out["ema50"])
    short = (c < out["bb_lower"]) & squeeze & vol_ok & (c < out["ema50"])
    _finish(out, long, short)
    return out


def vwap_ema_pullback(bars: pd.DataFrame) -> pd.DataFrame:
    c = bars["close"]
    out = pd.DataFrame(index=bars.index)
    out["vwap"] = vwap(bars)
    out["ema9"] = ema(c, 9)
    out["ema21"] = ema(c, 21)
    out["ema50"] = ema(c, 50)
    out["rsi14"] = rsi(c, 14)

    e9, e21, e50 = out["ema9"], out["ema21"], out["ema50"]
    long = (
        (c > out["vwap"]) & (e9 > e21) & (e21 > e50)
        & (bars["low"] <= e21 * 1.002) & (c > e9) & (out["rsi14"] > 50)
    )
    short = (
        (c < out["vwap"]) & (e9 < e21) & (e21 < e50)
        & (bars["high"] >= e21 * 0.998) & (c < e9) & (out["rsi14"] < 50)
    )
    _finish(out, long, short)
    return out


def donchian_atr_breakout(bars: pd.DataFrame) -> pd.DataFrame:
    c, v = bars["close"], bars["volume"]
    out = donchian(bars, 20)
    out["ema200"] = ema(c, 200)
    out["adx14"] = adx(bars, 14)["adx"]
    out["atr14"] = atr(bars, 14)
    out["vol_sma20"] = sma(v, 20)

    common = (out["adx14"] >= 20) & (v > 1.2 * out["vol_sma20"])
    long = (c > out["dc_high"]) & (c > out["ema200"]) & common
    short = (c < out["dc_low"]) & (c < out["ema200"]) & common
    _finish(out, long, short)
    return out


def ema_macd_trend(bars: pd.DataFrame) -> pd.DataFrame:
    c = bars["close"]
    out = pd.DataFrame(index=bars.index)
    out["ema21"] = ema(c, 21)
    out["ema55"] = ema(c, 55)
    out["ema200"] = ema(c, 200)
    out["rsi14"] = rsi(c, 14)
    out = out.join(macd(c, 12, 26, 9))

    h, h1, h2 = out["macd_hist"], out["macd_hist"].shift(1), out["macd_hist"].shift(2)
    hist_up = ((h > 0) & (h1 <= 0)) | ((h > 0) & (h > h1) & (h1 <= h2))
    hist_dn = ((h < 0) & (h1 >= 0)) | ((h < 0) & (h < h1) & (h1 >= h2))

    long = (c > out["ema200"]) & (out["ema21"] > out["ema55"]) & hist_up & out["rsi14"].between(52, 70)
    short = (c < out["ema200"]) & (out["ema21"] < out["ema55"]) & hist_dn & out["rsi14"].between(30, 48)
    _finish(out, long, short)
    return out


def keltner_channels(df: pd.DataFrame, n: int = 20, atr_n: int = 14, k: float = 1.5) -> pd.DataFrame:
    mid = ema(df["close"], n)
    rng = k * atr(df, atr_n)
    return pd.DataFrame({"kc_mid": mid, "kc_upper": mid + rng, "kc_lower": mid - rng}, index=df.index)


def keltner_squeeze_momentum(bars: pd.DataFrame) -> pd.DataFrame:
    """BB(20,2) expanding out of KC(20, EMA, 1.5*ATR14) after a squeeze / low-vol regime."""
    c = bars["close"]
    out = bollinger_bands(c, 20, 2.0).join(keltner_channels(bars, 20, 14, 1.5))
    out["ema100"] = ema(c, 100)
    out["rsi14"] = rsi(c, 14)
    out["adx14"] = adx(bars, 14)["adx"]

    squeeze_on = (out["bb_upper"] < out["kc_upper"]) & (out["bb_lower"] > out["kc_lower"])
    recent_squeeze = squeeze_on.astype(float).shift(1).rolling(6, min_periods=6).max() > 0
    bw = out["bb_bandwidth"]
    low_vol = bw.shift(1) <= bw.rolling(100, min_periods=100).quantile(0.25).shift(1)
    out["squeeze_on"] = squeeze_on
    setup = recent_squeeze | low_vol
    trend_ok = out["adx14"] >= 20

    long = (
        (out["bb_upper"] > out["kc_upper"]) & setup & (c > out["ema100"])
        & (out["rsi14"] > 55) & (c > c.shift(3)) & trend_ok
    )
    short = (
        (out["bb_lower"] < out["kc_lower"]) & setup & (c < out["ema100"])
        & (out["rsi14"] < 45) & (c < c.shift(3)) & trend_ok
    )
    _finish(out, long, short)
    return out


def supertrend_donchian_trend(bars: pd.DataFrame) -> pd.DataFrame:
    """Supertrend(10,3) direction + prior-15-bar Donchian breakout + EMA100; exits on Supertrend flip."""
    c = bars["close"]
    out = supertrend(bars, 10, 3.0).join(donchian(bars, 15))
    out["ema100"] = ema(c, 100)

    d = out["st_direction"]
    long = (d == 1) & (c > out["dc_high"]) & (c > out["ema100"])
    short = (d == -1) & (c < out["dc_low"]) & (c < out["ema100"])
    _finish(out, long, short)
    flip_dn = (d == -1) & (d.shift(1) == 1)
    flip_up = (d == 1) & (d.shift(1) == -1)
    out["exit_signal"] = pd.Series(np.select([flip_dn, flip_up], [1, -1], 0).astype("int8"), index=bars.index)
    return out


@dataclass(frozen=True, slots=True)
class Strategy:
    name: str
    description: str
    compute: Callable[[pd.DataFrame], pd.DataFrame]

    def generate(self, bars: pd.DataFrame) -> pd.DataFrame:
        """Return indicator columns + ``signal`` aligned to ``bars.index`` (closed bars only)."""
        return self.compute(bars)


STRATEGIES: dict[str, Strategy] = {
    s.name: s
    for s in (
        Strategy("supertrend_ema_rsi", "Supertrend(10,3) + EMA200 trend + EMA21 trigger, RSI/ADX filters", supertrend_ema_rsi),
        Strategy("bollinger_volume_breakout", "BB(20,2) squeeze breakout with 1.3x volume and EMA50 filter", bollinger_volume_breakout),
        Strategy("vwap_ema_pullback", "EMA9>21>50 stack, pullback to EMA21, VWAP side + RSI filter", vwap_ema_pullback),
        Strategy("donchian_atr_breakout", "Prior-20-bar Donchian breakout, EMA200 regime, ADX>=20, 1.2x volume", donchian_atr_breakout),
        Strategy("ema_macd_trend", "EMA21/55/200 trend + MACD(12,26,9) histogram turn + RSI band", ema_macd_trend),
        Strategy("keltner_squeeze_momentum", "BB(20,2) breaking out of KC(20,1.5ATR) after squeeze, EMA100/RSI/ADX/momentum filters", keltner_squeeze_momentum),
        Strategy("supertrend_donchian_trend", "Supertrend(10,3) + prior-15-bar Donchian breakout + EMA100; exit on Supertrend flip", supertrend_donchian_trend),
    )
}


CUSTOM_RULE_TYPES = (
    "ema_cross",
    "supertrend",
    "rsi_reversal",
    "bollinger_breakout",
    "macd_cross",
    "bb_squeeze_adx",
    "supertrend_ema_pullback",
    "donchian_adx_breakout",
    "ema_swing_4h",
    "keltner_momentum",
    "bb_squeeze_quick_tp",
    "ema_pullback_scalp",
    "bb_rsi_snapback",
    "vwap_st_burst",
    "scalp_vwap_ema",
    "scalp_rsi4_bb",
    "scalp_vol_burst",
    "scalp_ema_ribbon",
    "meme_liq_snapback",
    "meme_pump_ignition",
    "alt_ema_wave",
    "vol_obv_breakout",
    "vol_climax_reversal",
    "vol_vwap_expansion",
    "vol_dryup_breakout",
)


def generate_signals(strategy: str, bars: pd.DataFrame, custom_def: dict | None = None) -> pd.DataFrame:
    if custom_def is not None and "rule_type" in custom_def:
        return evaluate_custom_strategy(custom_def["rule_type"], custom_def.get("params") or {}, bars)
    if strategy in STRATEGIES:
        return STRATEGIES[strategy].generate(bars)
    if strategy in CUSTOM_RULE_TYPES:
        return evaluate_custom_strategy(strategy, {}, bars)
    raise KeyError(f"unknown strategy {strategy!r}; available: {list(STRATEGIES)} + {list(CUSTOM_RULE_TYPES)}")


def evaluate_custom_strategy(rule_type: str, params: dict, bars: pd.DataFrame) -> pd.DataFrame:
    """Evaluate user-defined custom strategy rules with tunable indicator parameters (past-only, edge-triggered)."""
    c, v = bars["close"], bars["volume"]
    p = params or {}
    rt = (rule_type or "ema_cross").strip().lower()

    if rt in ("bb_squeeze_adx", "bb_squeeze_quick_tp"):
        bb_n = max(5, int(p.get("bb_period", 20)))
        bb_k = max(0.5, float(p.get("bb_std", 2.0)))
        adx_min = float(p.get("adx_min", 20.0))
        vol_m = max(0.1, float(p.get("vol_mult", 1.3)))
        body_ratio = float(p.get("body_ratio", 0.45))
        ema_n = int(p.get("ema_filter", 50))
        cost_mult = float(p.get("cost_gate_mult", 2.0))
        use_4h = bool(int(p.get("use_4h_trend", 1)))
        squeeze_q = float(p.get("squeeze_q", 0.60))
        sq_lb = max(1, int(p.get("squeeze_lookback", 4)))
        adx_rising_req = bool(int(p.get("adx_rising", 1)))

        out = bollinger_bands(c, bb_n, bb_k)
        out["ema50"] = ema(c, max(5, ema_n)) if ema_n > 0 else c
        out["adx14"] = adx(bars, 14)["adx"]
        out["vol_sma20"] = sma(v, 20)

        bbw = out["bb_bandwidth"]
        if squeeze_q < 1.0:
            bw_q = bbw.rolling(50, min_periods=50).quantile(squeeze_q)
            squeeze = bbw.shift(1).rolling(sq_lb, min_periods=1).min() < bw_q.shift(1)
        else:
            squeeze = pd.Series(True, index=bars.index)
        bbw_expanding = (bbw > bbw.shift(1)) if bool(int(p.get("bbw_expanding", 1))) else pd.Series(True, index=bars.index)
        adx_rise = (out["adx14"] > out["adx14"].shift(1)) if adx_rising_req else pd.Series(True, index=bars.index)
        adx_ok = (out["adx14"] >= adx_min) & adx_rise
        vol_ok = v >= vol_m * out["vol_sma20"]
        rng = (bars["high"] - bars["low"]).replace(0.0, np.nan)
        body_ok = ((c - bars["open"]).abs() >= body_ratio * rng).fillna(False) if body_ratio > 0 else pd.Series(True, index=bars.index)
        raw_atr_ratio = atr(bars, 14) / c
        rt_cost = float(p.get("rt_cost", 0.00158))
        cost_ok = (raw_atr_ratio >= cost_mult * rt_cost).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)

        if use_4h:
            reg4 = _closed_4h_regime(bars)
            t4_l, t4_s = reg4["allow_long_200"], reg4["allow_short_200"]
        else:
            t4_l = pd.Series(True, index=bars.index)
            t4_s = pd.Series(True, index=bars.index)

        ema_l = (c > out["ema50"]) if ema_n > 0 else pd.Series(True, index=bars.index)
        ema_s = (c < out["ema50"]) if ema_n > 0 else pd.Series(True, index=bars.index)

        long = (c > out["bb_upper"]) & squeeze & bbw_expanding & adx_ok & vol_ok & body_ok & ema_l & cost_ok & t4_l
        short = (c < out["bb_lower"]) & squeeze & bbw_expanding & adx_ok & vol_ok & body_ok & ema_s & cost_ok & t4_s
        _finish(out, long, short)
        return out

    if rt == "ema_pullback_scalp":
        fast_n = max(5, int(p.get("fast_ema", 20)))
        slow_n = max(fast_n + 1, int(p.get("slow_ema", 50)))
        adx_min = float(p.get("adx_min", 18.0))
        pb_tol = float(p.get("pullback_tol", 0.002))
        rsi_n = max(2, int(p.get("rsi_period", 14)))
        rsi_l_min = float(p.get("rsi_long_min", 42.0))
        rsi_l_max = float(p.get("rsi_long_max", 60.0))
        rsi_s_min = float(p.get("rsi_short_min", 40.0))
        rsi_s_max = float(p.get("rsi_short_max", 58.0))
        vol_m = float(p.get("vol_mult", 1.0))
        cost_mult = float(p.get("cost_gate_mult", 1.8))
        use_4h = bool(int(p.get("use_4h_trend", 1)))

        out = pd.DataFrame(index=bars.index)
        out["ema_fast"] = ema(c, fast_n)
        out["ema_slow"] = ema(c, slow_n)
        out["ema21"] = out["ema_fast"]
        out["ema50"] = out["ema_slow"]
        out["rsi14"] = rsi(c, rsi_n)
        out["adx14"] = adx(bars, 14)["adx"]
        out["vol_sma20"] = sma(v, 20)

        if use_4h:
            reg4 = _closed_4h_regime(bars)
            t4_l, t4_s = reg4["allow_long_200"], reg4["allow_short_200"]
        else:
            t4_l = pd.Series(True, index=bars.index)
            t4_s = pd.Series(True, index=bars.index)

        ef, es = out["ema_fast"], out["ema_slow"]
        pb_l = (bars["low"] <= ef * (1.0 + pb_tol)) & (c > ef) & (c > bars["open"]) & (bars["low"].shift(1) > ef.shift(1) * (1.0 - pb_tol))
        pb_s = (bars["high"] >= ef * (1.0 - pb_tol)) & (c < ef) & (c < bars["open"]) & (bars["high"].shift(1) < ef.shift(1) * (1.0 + pb_tol))
        adx_ok = out["adx14"] >= adx_min
        vol_ok = (v >= vol_m * out["vol_sma20"]) if vol_m > 0 else pd.Series(True, index=bars.index)
        raw_atr_ratio = atr(bars, 14) / c
        cost_ok = (raw_atr_ratio >= cost_mult * float(p.get("rt_cost", 0.00138))).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)

        long = t4_l & (ef > es) & pb_l & out["rsi14"].between(rsi_l_min, rsi_l_max) & adx_ok & vol_ok & cost_ok
        short = t4_s & (ef < es) & pb_s & out["rsi14"].between(rsi_s_min, rsi_s_max) & adx_ok & vol_ok & cost_ok
        _finish(out, long, short)
        return out

    if rt == "bb_rsi_snapback":
        bb_n = max(5, int(p.get("bb_period", 20)))
        bb_k = max(0.5, float(p.get("bb_std", 2.0)))
        rsi_n = max(2, int(p.get("rsi_period", 14)))
        rsi_os = float(p.get("rsi_os", 34.0))
        rsi_ob = float(p.get("rsi_ob", 66.0))
        cost_mult = float(p.get("cost_gate_mult", 1.8))
        use_4h = bool(int(p.get("use_4h_trend", 0)))

        out = bollinger_bands(c, bb_n, bb_k)
        out["rsi14"] = rsi(c, rsi_n)
        r = out["rsi14"]

        stretch_l = ((bars["low"].shift(1) <= out["bb_lower"].shift(1)) | (bars["low"].shift(2) <= out["bb_lower"].shift(2))) & (
            (r.shift(1) <= rsi_os) | (r.shift(2) <= rsi_os)
        )
        stretch_s = ((bars["high"].shift(1) >= out["bb_upper"].shift(1)) | (bars["high"].shift(2) >= out["bb_upper"].shift(2))) & (
            (r.shift(1) >= rsi_ob) | (r.shift(2) >= rsi_ob)
        )
        rev_l = (c > out["bb_lower"]) & (c > bars["open"]) & (c > c.shift(1))
        rev_s = (c < out["bb_upper"]) & (c < bars["open"]) & (c < c.shift(1))
        raw_atr_ratio = atr(bars, 14) / c
        cost_ok = (raw_atr_ratio >= cost_mult * float(p.get("rt_cost", 0.00138))).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)

        if use_4h:
            reg4 = _closed_4h_regime(bars)
            t4_l, t4_s = reg4["allow_long_200"], reg4["allow_short_200"]
        else:
            t4_l = pd.Series(True, index=bars.index)
            t4_s = pd.Series(True, index=bars.index)

        long = stretch_l & rev_l & cost_ok & t4_l
        short = stretch_s & rev_s & cost_ok & t4_s
        _finish(out, long, short)
        return out

    if rt == "vwap_st_burst":
        atr_n = max(2, int(p.get("atr_period", 10)))
        atr_m = max(0.5, float(p.get("atr_mult", 2.5)))
        ema_n = max(5, int(p.get("ema_period", 20)))
        vol_m = max(0.1, float(p.get("vol_mult", 1.25)))
        adx_min = float(p.get("adx_min", 18.0))
        body_ratio = float(p.get("body_ratio", 0.35))
        cost_mult = float(p.get("cost_gate_mult", 1.8))
        use_4h = bool(int(p.get("use_4h_trend", 1)))

        if len(bars) >= 2:
            htf_min = max(1, int(round((bars.index[1] - bars.index[0]).total_seconds() / 60.0)))
        else:
            htf_min = 60
        w_bars = max(4, int(round(1440.0 / htf_min)))
        typical = (bars["high"] + bars["low"] + c) / 3.0
        pv_sum = (typical * v).rolling(w_bars, min_periods=4).sum()
        v_sum = v.rolling(w_bars, min_periods=4).sum().replace(0.0, np.nan)

        out = supertrend(bars, atr_n, atr_m)
        out["vwap"] = pv_sum / v_sum
        out["ema21"] = ema(c, ema_n)
        out["adx14"] = adx(bars, 14)["adx"]
        out["vol_sma20"] = sma(v, 20)

        d = out["st_direction"]
        vw = out["vwap"]
        e20 = out["ema21"]
        cross_l = (c > vw) & (c > e20) & ((c.shift(1) <= vw.shift(1)) | (c.shift(1) <= e20.shift(1)) | (d.shift(1) == -1))
        cross_s = (c < vw) & (c < e20) & ((c.shift(1) >= vw.shift(1)) | (c.shift(1) >= e20.shift(1)) | (d.shift(1) == 1))
        rng = (bars["high"] - bars["low"]).replace(0.0, np.nan)
        body_ok = ((c - bars["open"]).abs() >= body_ratio * rng).fillna(False) if body_ratio > 0 else pd.Series(True, index=bars.index)
        vol_ok = v >= vol_m * out["vol_sma20"]
        adx_ok = out["adx14"] >= adx_min
        raw_atr_ratio = atr(bars, 14) / c
        cost_ok = (raw_atr_ratio >= cost_mult * float(p.get("rt_cost", 0.00138))).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)

        if use_4h:
            reg4 = _closed_4h_regime(bars)
            t4_l, t4_s = reg4["allow_long_200"], reg4["allow_short_200"]
        else:
            t4_l = pd.Series(True, index=bars.index)
            t4_s = pd.Series(True, index=bars.index)

        long = t4_l & (d == 1) & cross_l & (c > bars["open"]) & body_ok & vol_ok & adx_ok & cost_ok
        short = t4_s & (d == -1) & cross_s & (c < bars["open"]) & body_ok & vol_ok & adx_ok & cost_ok
        _finish(out, long, short)
        return out

    if rt == "supertrend_ema_pullback":
        atr_n = max(2, int(p.get("atr_period", 10)))
        atr_m = max(0.5, float(p.get("atr_mult", 3.0)))
        pb_ema_n = max(5, int(p.get("pullback_ema", 21)))
        ema_4h_n = max(20, int(p.get("ema_4h", 100)))
        pb_tol = float(p.get("pullback_tol", 0.003))
        rsi_n = max(2, int(p.get("rsi_period", 14)))
        rsi_l_min = float(p.get("rsi_long_min", 40.0))
        rsi_l_max = float(p.get("rsi_long_max", 62.0))
        rsi_s_min = float(p.get("rsi_short_min", 38.0))
        rsi_s_max = float(p.get("rsi_short_max", 60.0))
        adx_min = float(p.get("adx_min", 18.0))
        vol_m = float(p.get("vol_mult", 1.1))
        cost_mult = float(p.get("cost_gate_mult", 2.0))
        use_4h_200 = bool(int(p.get("use_4h_200", 1)))

        out = supertrend(bars, atr_n, atr_m)
        out["ema21"] = ema(c, pb_ema_n)
        out["ema200"] = ema(c, 200)
        out["rsi14"] = rsi(c, rsi_n)
        out["adx14"] = adx(bars, 14)["adx"]
        out["vol_sma20"] = sma(v, 20)
        reg4 = _closed_4h_regime(bars, ema_custom=ema_4h_n)

        d = out["st_direction"]
        e_pb = out["ema21"]
        pb_long = (bars["low"] <= e_pb * (1.0 + pb_tol)) & (c > e_pb) & (c > bars["open"]) & (c > bars["high"].shift(1))
        pb_short = (bars["high"] >= e_pb * (1.0 - pb_tol)) & (c < e_pb) & (c < bars["open"]) & (c < bars["low"].shift(1))
        adx_ok = (out["adx14"] >= adx_min) if adx_min > 0 else pd.Series(True, index=bars.index)
        vol_ok = (v >= vol_m * out["vol_sma20"]) if vol_m > 0 else pd.Series(True, index=bars.index)
        raw_atr_ratio = atr(bars, 14) / c
        cost_ok = (raw_atr_ratio >= cost_mult * float(p.get("rt_cost", 0.00158))).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)
        t4_l = (reg4["allow_long_100"] & reg4["allow_long_200"]) if use_4h_200 else reg4["allow_long_100"]
        t4_s = (reg4["allow_short_100"] & reg4["allow_short_200"]) if use_4h_200 else reg4["allow_short_100"]

        long = t4_l & (d == 1) & pb_long & out["rsi14"].between(rsi_l_min, rsi_l_max) & adx_ok & vol_ok & cost_ok
        short = t4_s & (d == -1) & pb_short & out["rsi14"].between(rsi_s_min, rsi_s_max) & adx_ok & vol_ok & cost_ok
        _finish(out, long, short)
        return out

    if rt == "donchian_adx_breakout":
        dc_n = max(5, int(p.get("dc_period", 20)))
        adx_min = float(p.get("adx_min", 20.0))
        vol_m = max(0.1, float(p.get("vol_mult", 1.2)))
        ema_n = int(p.get("ema_filter", 50))
        use_4h = bool(int(p.get("use_4h_trend", 1)))
        cost_mult = float(p.get("cost_gate_mult", 2.0))
        squeeze_q = float(p.get("squeeze_q", 0.65))
        body_ratio = float(p.get("body_ratio", 0.40))

        out = donchian(bars, dc_n)
        out["ema200"] = ema(c, max(10, ema_n)) if ema_n > 0 else ema(c, 200)
        out["adx14"] = adx(bars, 14)["adx"]
        out["vol_sma20"] = sma(v, 20)
        raw_atr_ratio = atr(bars, 14) / c
        cost_ok = (raw_atr_ratio >= cost_mult * float(p.get("rt_cost", 0.00158))).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)

        dc_w = (out["dc_high"] - out["dc_low"]) / c
        if squeeze_q < 1.0:
            dc_q = dc_w.rolling(50, min_periods=30).quantile(squeeze_q)
            comp_ok = dc_w.shift(1) < dc_q.shift(1)
        else:
            comp_ok = pd.Series(True, index=bars.index)

        rng = (bars["high"] - bars["low"]).replace(0.0, np.nan)
        body_ok = ((c - bars["open"]).abs() >= body_ratio * rng).fillna(False) if body_ratio > 0 else pd.Series(True, index=bars.index)
        common = (out["adx14"] >= adx_min) & (v > vol_m * out["vol_sma20"]) & comp_ok & body_ok & cost_ok
        if use_4h:
            reg4 = _closed_4h_regime(bars)
            t4_l, t4_s = reg4["allow_long_200"], reg4["allow_short_200"]
        else:
            t4_l = pd.Series(True, index=bars.index)
            t4_s = pd.Series(True, index=bars.index)

        ema_l = (c > out["ema200"]) if ema_n > 0 else pd.Series(True, index=bars.index)
        ema_s = (c < out["ema200"]) if ema_n > 0 else pd.Series(True, index=bars.index)

        long = (c > out["dc_high"]) & ema_l & common & t4_l
        short = (c < out["dc_low"]) & ema_s & common & t4_s
        _finish(out, long, short)
        return out

    if rt == "ema_swing_4h":
        fast_n = max(2, int(p.get("fast_ema", 21)))
        slow_n = max(fast_n + 1, int(p.get("slow_ema", 55)))
        trend_n = int(p.get("trend_ema", 100))
        adx_min = float(p.get("adx_min", 20.0))
        rsi_l = float(p.get("rsi_long", 52.0))
        rsi_s = float(p.get("rsi_short", 48.0))
        vol_m = float(p.get("vol_mult", 1.2))
        cost_mult = float(p.get("cost_gate_mult", 2.0))
        brk_n = int(p.get("breakout_bars", 10))

        out = pd.DataFrame(index=bars.index)
        out["ema_fast"] = ema(c, fast_n)
        out["ema_slow"] = ema(c, slow_n)
        out["rsi14"] = rsi(c, 14)
        out["adx14"] = adx(bars, 14)["adx"]
        out["vol_sma20"] = sma(v, 20)
        if trend_n > 0:
            out["ema_trend"] = ema(c, max(10, trend_n))
            tl, ts_ = (c > out["ema_trend"]), (c < out["ema_trend"])
        else:
            tl = pd.Series(True, index=bars.index)
            ts_ = pd.Series(True, index=bars.index)

        raw_atr_ratio = atr(bars, 14) / c
        rt_cost = float(p.get("rt_cost", 0.00158))
        cost_ok = (raw_atr_ratio >= cost_mult * rt_cost).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)
        adx_ok = out["adx14"] >= adx_min
        vol_ok = (v >= vol_m * out["vol_sma20"]) if vol_m > 0 else pd.Series(True, index=bars.index)
        if brk_n > 0:
            dc = donchian(bars, brk_n)
            trig_l = c > dc["dc_high"]
            trig_s = c < dc["dc_low"]
        else:
            trig_l = (out["ema_fast"] > out["ema_slow"]) & (out["ema_fast"].shift(1) <= out["ema_slow"].shift(1))
            trig_s = (out["ema_fast"] < out["ema_slow"]) & (out["ema_fast"].shift(1) >= out["ema_slow"].shift(1))

        long = (out["ema_fast"] > out["ema_slow"]) & trig_l & tl & adx_ok & vol_ok & (out["rsi14"].between(rsi_l, 72.0)) & cost_ok
        short = (out["ema_fast"] < out["ema_slow"]) & trig_s & ts_ & adx_ok & vol_ok & (out["rsi14"].between(28.0, rsi_s)) & cost_ok
        _finish(out, long, short)
        return out

    if rt == "keltner_momentum":
        kc_n = max(5, int(p.get("kc_period", 20)))
        kc_mult = max(0.5, float(p.get("kc_mult", 1.5)))
        atr_n = max(2, int(p.get("atr_period", 14)))
        adx_min = float(p.get("adx_min", 20.0))
        vol_m = float(p.get("vol_mult", 1.25))
        ema_n = int(p.get("ema_filter", 50))
        use_4h = bool(int(p.get("use_4h_trend", 1)))
        cost_mult = float(p.get("cost_gate_mult", 2.0))
        squeeze_q = float(p.get("squeeze_q", 0.60))
        body_ratio = float(p.get("body_ratio", 0.40))

        out = bollinger_bands(c, kc_n, 2.0).join(keltner_channels(bars, kc_n, atr_n, kc_mult)).join(macd(c, 12, 26, 9))
        out["ema100"] = ema(c, max(10, ema_n)) if ema_n > 0 else ema(c, 100)
        out["rsi14"] = rsi(c, 14)
        out["adx14"] = adx(bars, 14)["adx"]
        out["vol_sma20"] = sma(v, 20)

        bw = out["bb_bandwidth"]
        if squeeze_q < 1.0:
            bw_q = bw.rolling(50, min_periods=30).quantile(squeeze_q)
            setup = bw.shift(1).rolling(3, min_periods=1).min() < bw_q.shift(1)
        else:
            setup = pd.Series(True, index=bars.index)

        h = out["macd_hist"]
        hist_l = (h > 0) & (h > h.shift(1))
        hist_s = (h < 0) & (h < h.shift(1))
        adx_ok = out["adx14"] >= adx_min
        vol_ok = (v >= vol_m * out["vol_sma20"]) if vol_m > 0 else pd.Series(True, index=bars.index)
        rng = (bars["high"] - bars["low"]).replace(0.0, np.nan)
        body_ok = ((c - bars["open"]).abs() >= body_ratio * rng).fillna(False) if body_ratio > 0 else pd.Series(True, index=bars.index)
        raw_atr_ratio = atr(bars, 14) / c
        cost_ok = (raw_atr_ratio >= cost_mult * float(p.get("rt_cost", 0.00158))).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)

        if use_4h:
            reg4 = _closed_4h_regime(bars)
            t4_l, t4_s = reg4["allow_long_200"], reg4["allow_short_200"]
        else:
            t4_l = pd.Series(True, index=bars.index)
            t4_s = pd.Series(True, index=bars.index)

        ema_l = (c > out["ema100"]) if ema_n > 0 else pd.Series(True, index=bars.index)
        ema_s = (c < out["ema100"]) if ema_n > 0 else pd.Series(True, index=bars.index)

        long = (c > out["kc_upper"]) & (c.shift(1) <= out["kc_upper"].shift(1)) & setup & ema_l & hist_l & adx_ok & vol_ok & body_ok & cost_ok & t4_l
        short = (c < out["kc_lower"]) & (c.shift(1) >= out["kc_lower"].shift(1)) & setup & ema_s & hist_s & adx_ok & vol_ok & body_ok & cost_ok & t4_s
        _finish(out, long, short)
        return out

    if rt == "ema_cross":
        fast_n = max(2, int(p.get("fast_ema", 9)))
        slow_n = max(fast_n + 1, int(p.get("slow_ema", 21)))
        trend_n = int(p.get("trend_ema", 50))
        rsi_n = max(2, int(p.get("rsi_period", 14)))
        rsi_ob = float(p.get("rsi_ob", 75.0))
        rsi_os = float(p.get("rsi_os", 25.0))

        out = pd.DataFrame(index=bars.index)
        out["ema_fast"] = ema(c, fast_n)
        out["ema_slow"] = ema(c, slow_n)
        out["rsi14"] = rsi(c, rsi_n)
        if trend_n > 0:
            out["ema_trend"] = ema(c, max(5, trend_n))
            trend_l = c > out["ema_trend"]
            trend_s = c < out["ema_trend"]
        else:
            trend_l = pd.Series(True, index=bars.index)
            trend_s = pd.Series(True, index=bars.index)

        long = (out["ema_fast"] > out["ema_slow"]) & trend_l & (out["rsi14"] < rsi_ob)
        short = (out["ema_fast"] < out["ema_slow"]) & trend_s & (out["rsi14"] > rsi_os)
        _finish(out, long, short)
        return out

    if rt == "supertrend":
        atr_n = max(2, int(p.get("atr_period", 10)))
        atr_m = max(0.5, float(p.get("atr_mult", 3.0)))
        ema_n = max(5, int(p.get("ema_filter", 200)))
        rsi_n = max(2, int(p.get("rsi_period", 14)))
        adx_min = float(p.get("adx_min", 18.0))

        out = pd.DataFrame(index=bars.index)
        out["ema21"] = ema(c, 21)
        out["ema200"] = ema(c, ema_n)
        out["rsi14"] = rsi(c, rsi_n)
        out["adx14"] = adx(bars, 14)["adx"]
        out = out.join(supertrend(bars, atr_n, atr_m))

        d = out["st_direction"]
        flip_up = (d == 1) & (d.shift(1) == -1)
        flip_dn = (d == -1) & (d.shift(1) == 1)
        cross_up = (c > out["ema21"]) & (c.shift(1) <= out["ema21"].shift(1))
        cross_dn = (c < out["ema21"]) & (c.shift(1) >= out["ema21"].shift(1))
        trend_ok = out["adx14"] >= adx_min

        long = (c > out["ema200"]) & (d == 1) & (flip_up | cross_up) & out["rsi14"].between(50, 72) & trend_ok
        short = (c < out["ema200"]) & (d == -1) & (flip_dn | cross_dn) & out["rsi14"].between(28, 50) & trend_ok
        _finish(out, long, short)
        return out

    if rt == "rsi_reversal":
        rsi_n = max(2, int(p.get("rsi_period", 14)))
        rsi_os = float(p.get("rsi_os", 35.0))
        rsi_ob = float(p.get("rsi_ob", 65.0))
        ema_n = max(5, int(p.get("ema_filter", 50)))

        out = pd.DataFrame(index=bars.index)
        out["ema21"] = ema(c, 21)
        out["ema50"] = ema(c, ema_n)
        out["rsi14"] = rsi(c, rsi_n)

        r = out["rsi14"]
        long = (r > rsi_os) & (r.shift(1) <= rsi_os) & (c > out["ema50"])
        short = (r < rsi_ob) & (r.shift(1) >= rsi_ob) & (c < out["ema50"])
        _finish(out, long, short)
        return out

    if rt == "bollinger_breakout":
        bb_n = max(5, int(p.get("bb_period", 20)))
        bb_k = max(0.5, float(p.get("bb_std", 2.0)))
        vol_m = max(0.1, float(p.get("vol_mult", 1.3)))
        ema_n = max(5, int(p.get("ema_filter", 50)))

        out = bollinger_bands(c, bb_n, bb_k)
        out["ema50"] = ema(c, ema_n)
        out["vol_sma20"] = sma(v, 20)
        out["bw_p60_50"] = out["bb_bandwidth"].rolling(50, min_periods=50).quantile(0.6)

        squeeze = out["bb_bandwidth"].shift(1) < out["bw_p60_50"].shift(1)
        vol_ok = v >= vol_m * out["vol_sma20"]

        long = (c > out["bb_upper"]) & squeeze & vol_ok & (c > out["ema50"])
        short = (c < out["bb_lower"]) & squeeze & vol_ok & (c < out["ema50"])
        _finish(out, long, short)
        return out

    if rt == "macd_cross":
        fast_n = max(2, int(p.get("fast_ema", 12)))
        slow_n = max(fast_n + 1, int(p.get("slow_ema", 26)))
        sig_n = max(2, int(p.get("signal_ema", 9)))
        ema_n = max(5, int(p.get("ema_filter", 200)))

        out = pd.DataFrame(index=bars.index)
        out["ema21"] = ema(c, 21)
        out["ema55"] = ema(c, 55)
        out["ema200"] = ema(c, ema_n)
        out["rsi14"] = rsi(c, 14)
        out = out.join(macd(c, fast_n, slow_n, sig_n))

        h, h1, h2 = out["macd_hist"], out["macd_hist"].shift(1), out["macd_hist"].shift(2)
        hist_up = ((h > 0) & (h1 <= 0)) | ((h > 0) & (h > h1) & (h1 <= h2))
        hist_dn = ((h < 0) & (h1 >= 0)) | ((h < 0) & (h < h1) & (h1 >= h2))

        long = (c > out["ema200"]) & (out["ema21"] > out["ema55"]) & hist_up
        short = (c < out["ema200"]) & (out["ema21"] < out["ema55"]) & hist_dn
        _finish(out, long, short)
        return out

    if rt == "scalp_vwap_ema":
        fast_n = max(2, int(p.get("fast_ema", 9)))
        slow_n = max(fast_n + 1, int(p.get("slow_ema", 21)))
        trend_n = max(slow_n + 1, int(p.get("trend_ema", 50)))
        macro_n = int(p.get("macro_ema", 200))
        rsi_n = max(2, int(p.get("rsi_period", 7)))
        rsi_l_min = float(p.get("rsi_long_min", 45.0))
        rsi_l_max = float(p.get("rsi_long_max", 68.0))
        rsi_s_min = float(p.get("rsi_short_min", 32.0))
        rsi_s_max = float(p.get("rsi_short_max", 55.0))
        pb_tol = float(p.get("pullback_tol", 0.0015))
        adx_min = float(p.get("adx_min", 16.0))
        vol_m = float(p.get("vol_mult", 1.1))
        cost_mult = float(p.get("cost_gate_mult", 1.2))
        use_4h = bool(int(p.get("use_4h_trend", 0)))

        out = pd.DataFrame(index=bars.index)
        out["ema9"] = ema(c, fast_n)
        out["ema21"] = ema(c, slow_n)
        out["ema50"] = ema(c, trend_n)
        out["vwap"] = vwap(bars)
        out["rsi14"] = rsi(c, rsi_n)
        out["adx14"] = adx(bars, 14)["adx"]
        out["vol_sma20"] = sma(v, 20)
        if macro_n > 0:
            out["ema200"] = ema(c, macro_n)
            mac_l, mac_s = (c > out["ema200"]), (c < out["ema200"])
        else:
            mac_l = pd.Series(True, index=bars.index)
            mac_s = pd.Series(True, index=bars.index)

        if use_4h:
            reg4 = _closed_4h_regime(bars)
            t4_l, t4_s = reg4["allow_long_200"], reg4["allow_short_200"]
        else:
            t4_l = pd.Series(True, index=bars.index)
            t4_s = pd.Series(True, index=bars.index)

        raw_atr_ratio = atr(bars, 14) / c
        rt_cost = float(p.get("rt_cost", 0.0006))
        cost_ok = (raw_atr_ratio >= cost_mult * rt_cost).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)
        adx_ok = out["adx14"] >= adx_min
        vol_ok = (v >= vol_m * out["vol_sma20"]) if vol_m > 0 else pd.Series(True, index=bars.index)

        stack_l = (out["ema9"] > out["ema21"]) & (out["ema21"] > out["ema50"]) & (c > out["vwap"])
        stack_s = (out["ema9"] < out["ema21"]) & (out["ema21"] < out["ema50"]) & (c < out["vwap"])
        touch_l = ((bars["low"] <= out["ema9"] * (1.0 + pb_tol)) | (bars["low"] <= out["ema21"] * (1.0 + pb_tol))) & (c > out["ema9"]) & (c > bars["open"])
        touch_s = ((bars["high"] >= out["ema9"] * (1.0 - pb_tol)) | (bars["high"] >= out["ema21"] * (1.0 - pb_tol))) & (c < out["ema9"]) & (c < bars["open"])
        brk_n = int(p.get("breakout_bars", 0))
        if brk_n > 0:
            dc = donchian(bars, brk_n)
            touch_l = touch_l & (c > dc["dc_high"])
            touch_s = touch_s & (c < dc["dc_low"])

        long = stack_l & touch_l & out["rsi14"].between(rsi_l_min, rsi_l_max) & adx_ok & vol_ok & cost_ok & mac_l & t4_l
        short = stack_s & touch_s & out["rsi14"].between(rsi_s_min, rsi_s_max) & adx_ok & vol_ok & cost_ok & mac_s & t4_s
        _finish(out, long, short)
        return out

    if rt == "scalp_rsi4_bb":
        bb_n = max(5, int(p.get("bb_period", 20)))
        bb_k = max(0.5, float(p.get("bb_std", 2.0)))
        rsi_n = max(2, int(p.get("rsi_period", 4)))
        rsi_os = float(p.get("rsi_os", 20.0))
        rsi_ob = float(p.get("rsi_ob", 80.0))
        ema_n = int(p.get("ema_filter", 200))
        adx_max = float(p.get("adx_max", 45.0))
        bb_tol = float(p.get("bb_tol", 0.001))
        cost_mult = float(p.get("cost_gate_mult", 1.2))
        use_4h = bool(int(p.get("use_4h_trend", 0)))

        out = bollinger_bands(c, bb_n, bb_k)
        out["rsi14"] = rsi(c, rsi_n)
        out["adx14"] = adx(bars, 14)["adx"]
        if ema_n > 0:
            out["ema200"] = ema(c, max(10, ema_n))
            ema_l, ema_s = (c > out["ema200"]), (c < out["ema200"])
        else:
            ema_l = pd.Series(True, index=bars.index)
            ema_s = pd.Series(True, index=bars.index)

        if use_4h:
            reg4 = _closed_4h_regime(bars)
            t4_l, t4_s = reg4["allow_long_100"], reg4["allow_short_100"]
        else:
            t4_l = pd.Series(True, index=bars.index)
            t4_s = pd.Series(True, index=bars.index)

        raw_atr_ratio = atr(bars, 14) / c
        rt_cost = float(p.get("rt_cost", 0.0006))
        cost_ok = (raw_atr_ratio >= cost_mult * rt_cost).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)
        adx_ok = (out["adx14"] <= adx_max) if adx_max < 100 else pd.Series(True, index=bars.index)

        r = out["rsi14"]
        dip_l = ((bars["low"].shift(1) <= out["bb_lower"].shift(1) * (1.0 + bb_tol)) | (bars["low"] <= out["bb_lower"] * (1.0 + bb_tol)))
        dip_s = ((bars["high"].shift(1) >= out["bb_upper"].shift(1) * (1.0 - bb_tol)) | (bars["high"] >= out["bb_upper"] * (1.0 - bb_tol)))
        rsi_l = ((r.shift(1) <= rsi_os) | (r.shift(2) <= rsi_os)) & (r > r.shift(1))
        rsi_s = ((r.shift(1) >= rsi_ob) | (r.shift(2) >= rsi_ob)) & (r < r.shift(1))
        snap_l = (c > bars["open"]) & (c > out["bb_lower"])
        snap_s = (c < bars["open"]) & (c < out["bb_upper"])

        long = dip_l & rsi_l & snap_l & ema_l & adx_ok & cost_ok & t4_l
        short = dip_s & rsi_s & snap_s & ema_s & adx_ok & cost_ok & t4_s
        _finish(out, long, short)
        return out

    if rt == "scalp_vol_burst":
        dc_n = max(4, int(p.get("dc_period", 15)))
        vol_m = max(0.5, float(p.get("vol_mult", 2.0)))
        body_ratio = float(p.get("body_ratio", 0.55))
        adx_min = float(p.get("adx_min", 20.0))
        ema_n = int(p.get("ema_filter", 50))
        squeeze_q = float(p.get("squeeze_q", 0.70))
        cost_mult = float(p.get("cost_gate_mult", 1.5))
        use_4h = bool(int(p.get("use_4h_trend", 1)))

        out = donchian(bars, dc_n)
        out["ema50"] = ema(c, max(5, ema_n)) if ema_n > 0 else c
        out["adx14"] = adx(bars, 14)["adx"]
        out["vol_sma20"] = sma(v, 20)

        dc_w = (out["dc_high"] - out["dc_low"]) / c
        if squeeze_q < 1.0:
            dc_q = dc_w.rolling(50, min_periods=30).quantile(squeeze_q)
            comp_ok = dc_w.shift(1) < dc_q.shift(1)
        else:
            comp_ok = pd.Series(True, index=bars.index)

        rng = (bars["high"] - bars["low"]).replace(0.0, np.nan)
        body_ok = ((c - bars["open"]).abs() >= body_ratio * rng).fillna(False) if body_ratio > 0 else pd.Series(True, index=bars.index)
        vol_ok = v >= vol_m * out["vol_sma20"]
        adx_ok = (out["adx14"] >= adx_min) & (out["adx14"] >= out["adx14"].shift(1))
        raw_atr_ratio = atr(bars, 14) / c
        rt_cost = float(p.get("rt_cost", 0.0008))
        cost_ok = (raw_atr_ratio >= cost_mult * rt_cost).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)

        if use_4h:
            reg4 = _closed_4h_regime(bars)
            t4_l, t4_s = reg4["allow_long_200"], reg4["allow_short_200"]
        else:
            t4_l = pd.Series(True, index=bars.index)
            t4_s = pd.Series(True, index=bars.index)

        ema_l = (c > out["ema50"]) if ema_n > 0 else pd.Series(True, index=bars.index)
        ema_s = (c < out["ema50"]) if ema_n > 0 else pd.Series(True, index=bars.index)

        long = (c > out["dc_high"]) & vol_ok & body_ok & adx_ok & comp_ok & ema_l & cost_ok & t4_l
        short = (c < out["dc_low"]) & vol_ok & body_ok & adx_ok & comp_ok & ema_s & cost_ok & t4_s
        _finish(out, long, short)
        return out

    if rt == "scalp_ema_ribbon":
        e1_n = max(2, int(p.get("ema1", 8)))
        e2_n = max(e1_n + 1, int(p.get("ema2", 13)))
        e3_n = max(e2_n + 1, int(p.get("ema3", 21)))
        e4_n = max(e3_n + 1, int(p.get("ema4", 55)))
        macro_n = int(p.get("macro_ema", 200))
        adx_min = float(p.get("adx_min", 18.0))
        vol_m = float(p.get("vol_mult", 1.25))
        cost_mult = float(p.get("cost_gate_mult", 1.5))
        use_4h = bool(int(p.get("use_4h_trend", 1)))
        brk_n = int(p.get("breakout_bars", 0))

        out = pd.DataFrame(index=bars.index)
        out["ema_fast"] = ema(c, e1_n)
        out["ema_slow"] = ema(c, e2_n)
        out["ema21"] = ema(c, e3_n)
        out["ema55"] = ema(c, e4_n)
        out = out.join(macd(c, 8, 21, 5))
        out["rsi14"] = rsi(c, 14)
        out["adx14"] = adx(bars, 14)["adx"]
        out["vol_sma20"] = sma(v, 20)
        if macro_n > 0:
            out["ema200"] = ema(c, macro_n)
            mac_l, mac_s = (c > out["ema200"]), (c < out["ema200"])
        else:
            mac_l = pd.Series(True, index=bars.index)
            mac_s = pd.Series(True, index=bars.index)

        if use_4h:
            reg4 = _closed_4h_regime(bars)
            t4_l, t4_s = reg4["allow_long_200"], reg4["allow_short_200"]
        else:
            t4_l = pd.Series(True, index=bars.index)
            t4_s = pd.Series(True, index=bars.index)

        rib_l = (c > out["ema_fast"]) & (out["ema_fast"] > out["ema_slow"]) & (out["ema_slow"] > out["ema21"]) & (out["ema21"] > out["ema55"])
        rib_s = (c < out["ema_fast"]) & (out["ema_fast"] < out["ema_slow"]) & (out["ema_slow"] < out["ema21"]) & (out["ema21"] < out["ema55"])
        h = out["macd_hist"]
        hist_l = (h > 0) & (h > h.shift(1))
        hist_s = (h < 0) & (h < h.shift(1))
        if brk_n > 0:
            dc = donchian(bars, brk_n)
            trig_l = c > dc["dc_high"]
            trig_s = c < dc["dc_low"]
        else:
            trig_l = (~rib_l.shift(1).fillna(False)) | ((h > 0) & (h.shift(1) <= 0))
            trig_s = (~rib_s.shift(1).fillna(False)) | ((h < 0) & (h.shift(1) >= 0))

        vol_ok = (v >= vol_m * out["vol_sma20"]) if vol_m > 0 else pd.Series(True, index=bars.index)
        adx_ok = out["adx14"] >= adx_min
        raw_atr_ratio = atr(bars, 14) / c
        rt_cost = float(p.get("rt_cost", 0.0008))
        cost_ok = (raw_atr_ratio >= cost_mult * rt_cost).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)

        long = rib_l & trig_l & hist_l & out["rsi14"].between(50, 74) & vol_ok & adx_ok & cost_ok & mac_l & t4_l
        short = rib_s & trig_s & hist_s & out["rsi14"].between(26, 50) & vol_ok & adx_ok & cost_ok & mac_s & t4_s
        _finish(out, long, short)
        return out

    if rt == "meme_liq_snapback":
        bb_n = max(5, int(p.get("bb_period", 20)))
        bb_k = max(0.5, float(p.get("bb_std", 2.0)))
        dc_n = max(4, int(p.get("dc_period", 15)))
        rsi_n = max(2, int(p.get("rsi_period", 7)))
        rsi_os = float(p.get("rsi_os", 28.0))
        rsi_ob = float(p.get("rsi_ob", 72.0))
        wick_ratio = float(p.get("wick_ratio", 0.35))
        ema_n = int(p.get("ema_filter", 200))
        adx_max = float(p.get("adx_max", 45.0))
        cost_mult = float(p.get("cost_gate_mult", 1.5))
        use_4h = bool(int(p.get("use_4h_trend", 1)))

        out = bollinger_bands(c, bb_n, bb_k).join(donchian(bars, dc_n))
        out["rsi14"] = rsi(c, rsi_n)
        out["adx14"] = adx(bars, 14)["adx"]
        if ema_n > 0:
            out["ema200"] = ema(c, max(10, ema_n))
            ema_l, ema_s = (c > out["ema200"]), (c < out["ema200"])
        else:
            ema_l = pd.Series(True, index=bars.index)
            ema_s = pd.Series(True, index=bars.index)

        if use_4h:
            reg4 = _closed_4h_regime(bars)
            t4_l, t4_s = reg4["allow_long_100"], reg4["allow_short_100"]
        else:
            t4_l = pd.Series(True, index=bars.index)
            t4_s = pd.Series(True, index=bars.index)

        raw_atr_ratio = atr(bars, 14) / c
        rt_cost = float(p.get("rt_cost", 0.0010))
        cost_ok = (raw_atr_ratio >= cost_mult * rt_cost).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)
        adx_ok = (out["adx14"] <= adx_max) if adx_max < 100 else pd.Series(True, index=bars.index)

        rng = (bars["high"] - bars["low"]).replace(0.0, np.nan)
        lower_wick = (np.minimum(bars["open"], c) - bars["low"]) >= wick_ratio * rng
        upper_wick = (bars["high"] - np.maximum(bars["open"], c)) >= wick_ratio * rng
        lower_wick_prev = (np.minimum(bars["open"].shift(1), c.shift(1)) - bars["low"].shift(1)) >= wick_ratio * rng.shift(1)
        upper_wick_prev = (bars["high"].shift(1) - np.maximum(bars["open"].shift(1), c.shift(1))) >= wick_ratio * rng.shift(1)

        sweep_l = (
            (bars["low"] <= out["bb_lower"]) | (bars["low"] <= out["dc_low"])
            | (bars["low"].shift(1) <= out["bb_lower"].shift(1)) | (bars["low"].shift(1) <= out["dc_low"].shift(1))
        )
        sweep_s = (
            (bars["high"] >= out["bb_upper"]) | (bars["high"] >= out["dc_high"])
            | (bars["high"].shift(1) >= out["bb_upper"].shift(1)) | (bars["high"].shift(1) >= out["dc_high"].shift(1))
        )
        r = out["rsi14"]
        rsi_l = (r <= rsi_os) | (r.shift(1) <= rsi_os)
        rsi_s = (r >= rsi_ob) | (r.shift(1) >= rsi_ob)
        rej_l = ((c > bars["open"]) & (c > out["bb_lower"])) | (lower_wick.fillna(False) & (c > out["bb_lower"])) | (lower_wick_prev.fillna(False) & (c > bars["open"]))
        rej_s = ((c < bars["open"]) & (c < out["bb_upper"])) | (upper_wick.fillna(False) & (c < out["bb_upper"])) | (upper_wick_prev.fillna(False) & (c < bars["open"]))

        long = sweep_l & rsi_l & rej_l & ema_l & adx_ok & cost_ok & t4_l
        short = sweep_s & rsi_s & rej_s & ema_s & adx_ok & cost_ok & t4_s
        _finish(out, long, short)
        return out

    if rt == "meme_pump_ignition":
        bb_n = max(5, int(p.get("bb_period", 20)))
        bb_k = max(0.5, float(p.get("bb_std", 2.0)))
        dc_n = max(4, int(p.get("dc_period", 15)))
        vol_m = max(0.5, float(p.get("vol_mult", 2.0)))
        body_ratio = float(p.get("body_ratio", 0.50))
        adx_min = float(p.get("adx_min", 20.0))
        bbw_max = float(p.get("bbw_max", 0.08))
        squeeze_q = float(p.get("squeeze_q", 0.65))
        ema_n = int(p.get("ema_filter", 50))
        cost_mult = float(p.get("cost_gate_mult", 1.8))
        use_4h = bool(int(p.get("use_4h_trend", 1)))

        out = bollinger_bands(c, bb_n, bb_k).join(donchian(bars, dc_n))
        out["ema50"] = ema(c, max(5, ema_n)) if ema_n > 0 else c
        out["adx14"] = adx(bars, 14)["adx"]
        out["vol_sma20"] = sma(v, 20)

        bbw = out["bb_bandwidth"]
        min_bw15 = bbw.shift(1).rolling(15, min_periods=3).min()
        if squeeze_q < 1.0:
            bw_q = bbw.rolling(50, min_periods=25).quantile(squeeze_q)
            comp_ok = (min_bw15 < bbw_max) & (min_bw15 < bw_q.shift(1))
        else:
            comp_ok = min_bw15 < bbw_max

        rng = (bars["high"] - bars["low"]).replace(0.0, np.nan)
        body_ok = ((c - bars["open"]).abs() >= body_ratio * rng).fillna(False) if body_ratio > 0 else pd.Series(True, index=bars.index)
        vol_ok = v >= vol_m * out["vol_sma20"]
        adx_ok = out["adx14"] >= adx_min
        raw_atr_ratio = atr(bars, 14) / c
        rt_cost = float(p.get("rt_cost", 0.0012))
        cost_ok = (raw_atr_ratio >= cost_mult * rt_cost).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)

        if use_4h:
            reg4 = _closed_4h_regime(bars)
            t4_l, t4_s = reg4["allow_long_200"], reg4["allow_short_200"]
        else:
            t4_l = pd.Series(True, index=bars.index)
            t4_s = pd.Series(True, index=bars.index)

        ema_l = (c > out["ema50"]) if ema_n > 0 else pd.Series(True, index=bars.index)
        ema_s = (c < out["ema50"]) if ema_n > 0 else pd.Series(True, index=bars.index)

        brk_l = (c > out["bb_upper"]) | (c > out["dc_high"])
        brk_s = (c < out["bb_lower"]) | (c < out["dc_low"])

        long = brk_l & comp_ok & vol_ok & body_ok & adx_ok & ema_l & cost_ok & t4_l
        short = brk_s & comp_ok & vol_ok & body_ok & adx_ok & ema_s & cost_ok & t4_s
        _finish(out, long, short)
        return out

    if rt == "alt_ema_wave":
        fast_n = max(2, int(p.get("fast_ema", 9)))
        mid_n = max(fast_n + 1, int(p.get("mid_ema", 21)))
        slow_n = max(mid_n + 1, int(p.get("slow_ema", 50)))
        adx_min = float(p.get("adx_min", 18.0))
        pb_tol = float(p.get("pullback_tol", 0.003))
        rsi_l_min = float(p.get("rsi_long_min", 42.0))
        rsi_l_max = float(p.get("rsi_long_max", 65.0))
        rsi_s_min = float(p.get("rsi_short_min", 35.0))
        rsi_s_max = float(p.get("rsi_short_max", 58.0))
        vol_m = float(p.get("vol_mult", 1.0))
        cost_mult = float(p.get("cost_gate_mult", 1.5))
        use_4h = bool(int(p.get("use_4h_trend", 1)))

        out = pd.DataFrame(index=bars.index)
        out["ema9"] = ema(c, fast_n)
        out["ema21"] = ema(c, mid_n)
        out["ema50"] = ema(c, slow_n)
        out["vwap"] = vwap(bars)
        out["rsi14"] = rsi(c, 14)
        out["adx14"] = adx(bars, 14)["adx"]
        out["vol_sma20"] = sma(v, 20)

        if use_4h:
            reg4 = _closed_4h_regime(bars)
            t4_l, t4_s = reg4["allow_long_200"], reg4["allow_short_200"]
        else:
            t4_l = pd.Series(True, index=bars.index)
            t4_s = pd.Series(True, index=bars.index)

        raw_atr_ratio = atr(bars, 14) / c
        rt_cost = float(p.get("rt_cost", 0.0010))
        cost_ok = (raw_atr_ratio >= cost_mult * rt_cost).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)
        adx_ok = out["adx14"] >= adx_min
        vol_ok = (v >= vol_m * out["vol_sma20"]) if vol_m > 0 else pd.Series(True, index=bars.index)

        stack_l = (out["ema9"] > out["ema21"]) & (out["ema21"] > out["ema50"]) & (c > out["vwap"])
        stack_s = (out["ema9"] < out["ema21"]) & (out["ema21"] < out["ema50"]) & (c < out["vwap"])
        dip_l = (bars["low"] <= out["ema21"] * (1.0 + pb_tol)) & (c > out["ema9"]) & (c > bars["open"])
        dip_s = (bars["high"] >= out["ema21"] * (1.0 - pb_tol)) & (c < out["ema9"]) & (c < bars["open"])

        long = stack_l & dip_l & out["rsi14"].between(rsi_l_min, rsi_l_max) & adx_ok & vol_ok & cost_ok & t4_l
        short = stack_s & dip_s & out["rsi14"].between(rsi_s_min, rsi_s_max) & adx_ok & vol_ok & cost_ok & t4_s
        _finish(out, long, short)
        return out

    if rt == "vol_obv_breakout":
        obv_span = max(2, int(p.get("obv_span", 20)))
        vol_ma_n = max(2, int(p.get("vol_ma_period", 20)))
        rvol_min = max(0.5, float(p.get("rvol_min", 1.3)))
        ema_n = int(p.get("ema_filter", 50))
        dc_n = int(p.get("dc_period", 12))
        adx_min = float(p.get("adx_min", 18.0))
        body_ratio = float(p.get("body_ratio", 0.40))
        cost_mult = float(p.get("cost_gate_mult", 1.6))
        use_4h = bool(int(p.get("use_4h_trend", 1)))

        out = obv(bars, obv_span)
        out["rvol"] = rvol(bars, vol_ma_n)
        out["ema50"] = ema(c, max(5, ema_n)) if ema_n > 0 else c
        out["adx14"] = adx(bars, 14)["adx"]
        if dc_n > 0:
            dc = donchian(bars, dc_n)
            out["dc_high"] = dc["dc_high"]
            out["dc_low"] = dc["dc_low"]
            brk_l = (c > out["dc_high"]) | (out["obv"] > out["obv"].shift(1).rolling(dc_n, min_periods=3).max())
            brk_s = (c < out["dc_low"]) | (out["obv"] < out["obv"].shift(1).rolling(dc_n, min_periods=3).min())
        else:
            brk_l = (out["obv"] > out["obv_ema"]) & (out["obv"].shift(1) <= out["obv_ema"].shift(1))
            brk_s = (out["obv"] < out["obv_ema"]) & (out["obv"].shift(1) >= out["obv_ema"].shift(1))

        if use_4h:
            reg4 = _closed_4h_regime(bars)
            t4_l, t4_s = reg4["allow_long_200"], reg4["allow_short_200"]
        else:
            t4_l = pd.Series(True, index=bars.index)
            t4_s = pd.Series(True, index=bars.index)

        rng = (bars["high"] - bars["low"]).replace(0.0, np.nan)
        body_ok = ((c - bars["open"]).abs() >= body_ratio * rng).fillna(False) if body_ratio > 0 else pd.Series(True, index=bars.index)
        raw_atr_ratio = atr(bars, 14) / c
        rt_cost = float(p.get("rt_cost", 0.0010))
        cost_ok = (raw_atr_ratio >= cost_mult * rt_cost).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)
        rvol_ok = out["rvol"] >= rvol_min
        adx_ok = out["adx14"] >= adx_min
        ema_l = (c > out["ema50"]) if ema_n > 0 else pd.Series(True, index=bars.index)
        ema_s = (c < out["ema50"]) if ema_n > 0 else pd.Series(True, index=bars.index)
        obv_l = (out["obv"] > out["obv_ema"]) & (out["obv"] > out["obv"].shift(1)) & (c > bars["open"])
        obv_s = (out["obv"] < out["obv_ema"]) & (out["obv"] < out["obv"].shift(1)) & (c < bars["open"])

        long = obv_l & brk_l & rvol_ok & ema_l & adx_ok & body_ok & cost_ok & t4_l
        short = obv_s & brk_s & rvol_ok & ema_s & adx_ok & body_ok & cost_ok & t4_s
        _finish(out, long, short)
        return out

    if rt == "vol_climax_reversal":
        climax_mult = max(1.2, float(p.get("climax_mult", 2.8)))
        vol_ma_n = max(2, int(p.get("vol_ma_period", 20)))
        wick_ratio = float(p.get("wick_ratio", 0.40))
        bb_n = max(5, int(p.get("bb_period", 20)))
        bb_k = max(0.5, float(p.get("bb_std", 2.0)))
        rsi_n = max(2, int(p.get("rsi_period", 14)))
        rsi_os = float(p.get("rsi_os", 38.0))
        rsi_ob = float(p.get("rsi_ob", 62.0))
        ema_n = int(p.get("ema_filter", 200))
        adx_max = float(p.get("adx_max", 45.0))
        cost_mult = float(p.get("cost_gate_mult", 1.5))
        use_4h = bool(int(p.get("use_4h_trend", 1)))

        out = bollinger_bands(c, bb_n, bb_k)
        out["rvol"] = rvol(bars, vol_ma_n)
        out["rsi14"] = rsi(c, rsi_n)
        out["adx14"] = adx(bars, 14)["adx"]
        if ema_n > 0:
            out["ema200"] = ema(c, max(10, ema_n))
            ema_l, ema_s = (c > out["ema200"]), (c < out["ema200"])
        else:
            ema_l = pd.Series(True, index=bars.index)
            ema_s = pd.Series(True, index=bars.index)

        if use_4h:
            reg4 = _closed_4h_regime(bars)
            t4_l, t4_s = reg4["allow_long_100"], reg4["allow_short_100"]
        else:
            t4_l = pd.Series(True, index=bars.index)
            t4_s = pd.Series(True, index=bars.index)

        raw_atr_ratio = atr(bars, 14) / c
        rt_cost = float(p.get("rt_cost", 0.0010))
        cost_ok = (raw_atr_ratio >= cost_mult * rt_cost).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)
        adx_ok = (out["adx14"] <= adx_max) if adx_max < 100 else pd.Series(True, index=bars.index)

        rv = out["rvol"]
        climax_ok = (rv >= climax_mult) | (rv.shift(1) >= climax_mult)
        rng = (bars["high"] - bars["low"]).replace(0.0, np.nan)
        lower_wick = (np.minimum(bars["open"], c) - bars["low"]) >= wick_ratio * rng
        upper_wick = (bars["high"] - np.maximum(bars["open"], c)) >= wick_ratio * rng
        lower_wick_prev = (np.minimum(bars["open"].shift(1), c.shift(1)) - bars["low"].shift(1)) >= wick_ratio * rng.shift(1)
        upper_wick_prev = (bars["high"].shift(1) - np.maximum(bars["open"].shift(1), c.shift(1))) >= wick_ratio * rng.shift(1)

        ext_l = (bars["low"] <= out["bb_lower"] * 1.002) | (bars["low"].shift(1) <= out["bb_lower"].shift(1) * 1.002) | (out["rsi14"] <= rsi_os) | (out["rsi14"].shift(1) <= rsi_os)
        ext_s = (bars["high"] >= out["bb_upper"] * 0.998) | (bars["high"].shift(1) >= out["bb_upper"].shift(1) * 0.998) | (out["rsi14"] >= rsi_ob) | (out["rsi14"].shift(1) >= rsi_ob)

        abs_l = (lower_wick.fillna(False) & (c > out["bb_lower"])) | ((c > bars["open"]) & (c >= bars["low"] + 0.55 * rng).fillna(False)) | (lower_wick_prev.fillna(False) & (c > bars["open"]))
        abs_s = (upper_wick.fillna(False) & (c < out["bb_upper"])) | ((c < bars["open"]) & (c <= bars["high"] - 0.55 * rng).fillna(False)) | (upper_wick_prev.fillna(False) & (c < bars["open"]))

        long = climax_ok & ext_l & abs_l & ema_l & adx_ok & cost_ok & t4_l
        short = climax_ok & ext_s & abs_s & ema_s & adx_ok & cost_ok & t4_s
        _finish(out, long, short)
        return out

    if rt == "vol_vwap_expansion":
        rvol_min = max(1.0, float(p.get("rvol_min", 2.0)))
        vol_ma_n = max(2, int(p.get("vol_ma_period", 20)))
        ema_n = max(5, int(p.get("ema_period", 20)))
        body_ratio = float(p.get("body_ratio", 0.50))
        adx_min = float(p.get("adx_min", 20.0))
        dc_n = int(p.get("dc_period", 10))
        cost_mult = float(p.get("cost_gate_mult", 1.6))
        use_4h = bool(int(p.get("use_4h_trend", 1)))

        out = pd.DataFrame(index=bars.index)
        out["vwap"] = vwap(bars)
        out["ema21"] = ema(c, ema_n)
        out["rvol"] = rvol(bars, vol_ma_n)
        out["adx14"] = adx(bars, 14)["adx"]
        if dc_n > 0:
            dc = donchian(bars, dc_n)
            out["dc_high"] = dc["dc_high"]
            out["dc_low"] = dc["dc_low"]
            brk_l = (c > out["dc_high"]) | (c.shift(1) <= out["vwap"].shift(1)) | (c.shift(1) <= out["ema21"].shift(1))
            brk_s = (c < out["dc_low"]) | (c.shift(1) >= out["vwap"].shift(1)) | (c.shift(1) >= out["ema21"].shift(1))
        else:
            brk_l = (c.shift(1) <= out["vwap"].shift(1)) | (c.shift(1) <= out["ema21"].shift(1))
            brk_s = (c.shift(1) >= out["vwap"].shift(1)) | (c.shift(1) >= out["ema21"].shift(1))

        if use_4h:
            reg4 = _closed_4h_regime(bars)
            t4_l, t4_s = reg4["allow_long_200"], reg4["allow_short_200"]
        else:
            t4_l = pd.Series(True, index=bars.index)
            t4_s = pd.Series(True, index=bars.index)

        rng = (bars["high"] - bars["low"]).replace(0.0, np.nan)
        body_ok = ((c - bars["open"]).abs() >= body_ratio * rng).fillna(False) if body_ratio > 0 else pd.Series(True, index=bars.index)
        rvol_ok = out["rvol"] >= rvol_min
        adx_ok = out["adx14"] >= adx_min
        raw_atr_ratio = atr(bars, 14) / c
        rt_cost = float(p.get("rt_cost", 0.0010))
        cost_ok = (raw_atr_ratio >= cost_mult * rt_cost).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)

        long = (c > out["vwap"]) & (c > out["ema21"]) & (c > bars["open"]) & brk_l & rvol_ok & body_ok & adx_ok & cost_ok & t4_l
        short = (c < out["vwap"]) & (c < out["ema21"]) & (c < bars["open"]) & brk_s & rvol_ok & body_ok & adx_ok & cost_ok & t4_s
        _finish(out, long, short)
        return out

    if rt == "vol_dryup_breakout":
        dry_bars = max(2, int(p.get("dryup_bars", 3)))
        dry_ratio = float(p.get("dryup_max_ratio", 0.65))
        burst_mult = max(1.2, float(p.get("burst_mult", 2.4)))
        dc_n = max(4, int(p.get("dc_period", 15)))
        body_ratio = float(p.get("body_ratio", 0.45))
        adx_min = float(p.get("adx_min", 18.0))
        ema_n = int(p.get("ema_filter", 50))
        cost_mult = float(p.get("cost_gate_mult", 1.6))
        use_4h = bool(int(p.get("use_4h_trend", 1)))

        out = donchian(bars, dc_n)
        out["vol_sma20"] = sma(v, 20)
        out["rvol"] = rvol(bars, 20)
        out["ema50"] = ema(c, max(5, ema_n)) if ema_n > 0 else c
        out["adx14"] = adx(bars, 14)["adx"]

        if use_4h:
            reg4 = _closed_4h_regime(bars)
            t4_l, t4_s = reg4["allow_long_200"], reg4["allow_short_200"]
        else:
            t4_l = pd.Series(True, index=bars.index)
            t4_s = pd.Series(True, index=bars.index)

        # Dry-up: prior N-bar mean volume (or recent 3-bar window within last 6 bars) <= dry_ratio * SMA20
        v_mean_dry = v.shift(1).rolling(dry_bars, min_periods=2).mean()
        dry_recent = (v_mean_dry <= dry_ratio * out["vol_sma20"].shift(1)).rolling(4, min_periods=1).max().astype(bool)
        burst_ok = out["rvol"] >= burst_mult
        rng = (bars["high"] - bars["low"]).replace(0.0, np.nan)
        body_ok = ((c - bars["open"]).abs() >= body_ratio * rng).fillna(False) if body_ratio > 0 else pd.Series(True, index=bars.index)
        adx_ok = out["adx14"] >= adx_min
        raw_atr_ratio = atr(bars, 14) / c
        rt_cost = float(p.get("rt_cost", 0.0010))
        cost_ok = (raw_atr_ratio >= cost_mult * rt_cost).fillna(False) if cost_mult > 0 else pd.Series(True, index=bars.index)
        ema_l = (c > out["ema50"]) if ema_n > 0 else pd.Series(True, index=bars.index)
        ema_s = (c < out["ema50"]) if ema_n > 0 else pd.Series(True, index=bars.index)

        long = (c > out["dc_high"]) & dry_recent & burst_ok & body_ok & adx_ok & ema_l & cost_ok & t4_l
        short = (c < out["dc_low"]) & dry_recent & burst_ok & body_ok & adx_ok & ema_s & cost_ok & t4_s
        _finish(out, long, short)
        return out

    raise ValueError(f"unknown custom rule_type {rule_type!r}; choose from {CUSTOM_RULE_TYPES}")
