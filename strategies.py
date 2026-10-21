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


def generate_signals(strategy: str, bars: pd.DataFrame) -> pd.DataFrame:
    if strategy not in STRATEGIES:
        raise KeyError(f"unknown strategy {strategy!r}; available: {list(STRATEGIES)}")
    return STRATEGIES[strategy].generate(bars)
