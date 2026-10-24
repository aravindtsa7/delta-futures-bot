"""Historical data layer: product metadata, 1m candle downloads, unified dataset, resampling.

Storage layout (under ``settings.DATA_DIR``)::

    metadata/{symbol}.json
    raw_1m/{symbol}_{series_type}_1m.parquet     columns: time(int64 s), open, high, low, close, volume

API facts this module relies on (verified in step 1/2 probes):
  * ``/v2/history/candles`` returns rows newest-first; ``start`` and ``end`` are both inclusive.
  * ``FUNDING:{symbol}`` returns nothing unless ``start`` is at/before an 8h boundary
    (00/08/16 UTC), so every chunk start is snapped down to a 28,800 s boundary.
  * Funding values are in percentage points (0.0083 == 0.0083 %).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

import numpy as np
import pandas as pd
import requests

from config import settings

log = logging.getLogger(__name__)

SeriesType = Literal["TRADE", "MARK", "FUNDING"]
SERIES_TYPES: tuple[SeriesType, ...] = ("TRADE", "MARK", "FUNDING")

MINUTE = 60
FUNDING_INTERVAL_SECONDS = 8 * 3600  # 28,800
CHUNK_MINUTES = 1920  # 32h: <= 2000-candle limit and an exact multiple of 8h
CHUNK_SECONDS = CHUNK_MINUTES * MINUTE

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]
RAW_COLUMNS = ["time", *OHLCV_COLUMNS]

TIMEFRAME_MINUTES: dict[str, int] = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "1h": 60, "2h": 120, "4h": 240}

# HTTP pacing / retry policy
MIN_REQUEST_INTERVAL_SECONDS = 0.25
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
APP_SYMBOLS_FILE = settings.data_path / "app_symbols.json"
_SYMBOL_OK = __import__("re").compile(r"^[A-Z0-9]{2,20}$")


def app_symbols() -> list[str]:
    """config SYMBOLS + coins added from the dashboard (data/app_symbols.json), de-duplicated, in order."""
    extra: list[str] = []
    if APP_SYMBOLS_FILE.exists():
        extra = [s for s in json.loads(APP_SYMBOLS_FILE.read_text(encoding="utf-8")) if _SYMBOL_OK.match(s)]
    return list(dict.fromkeys([*settings.SYMBOLS, *extra]))


def add_app_symbol(symbol: str) -> None:
    syms = [s for s in app_symbols() if s not in settings.SYMBOLS]
    if symbol not in syms and symbol not in settings.SYMBOLS:
        syms.append(symbol)
    APP_SYMBOLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(APP_SYMBOLS_FILE, json.dumps(syms).encode("utf-8"))


def metadata_dir() -> Path:
    return settings.data_path / "metadata"


def metadata_path(symbol: str) -> Path:
    return metadata_dir() / f"{symbol}.json"


def raw_1m_dir() -> Path:
    return settings.data_path / "raw_1m"


def raw_1m_path(symbol: str, series_type: SeriesType) -> Path:
    return raw_1m_dir() / f"{symbol}_{series_type}_1m.parquet"


def api_symbol(symbol: str, series_type: SeriesType) -> str:
    if series_type == "TRADE":
        return symbol
    if series_type in ("MARK", "FUNDING"):
        return f"{series_type}:{symbol}"
    raise ValueError(f"series_type must be one of {SERIES_TYPES}, got {series_type!r}")


# --------------------------------------------------------------------------- #
# HTTP client with pacing + bounded retries
# --------------------------------------------------------------------------- #
class _PublicClient:
    def __init__(self) -> None:
        self.base_url = settings.env.rest_url
        self.session = requests.Session()
        self.session.headers.update(settings.http_headers())
        self._last_request = 0.0

    def _pace(self) -> None:
        wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    @staticmethod
    def _retry_delay(resp: requests.Response | None, attempt: int) -> float:
        if resp is not None:
            # Delta sends X-RATE-LIMIT-RESET in milliseconds; also honour Retry-After.
            reset_ms = resp.headers.get("X-RATE-LIMIT-RESET")
            if reset_ms and reset_ms.isdigit():
                return min(int(reset_ms) / 1000 + 0.5, BACKOFF_MAX_SECONDS)
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                return min(float(retry_after), BACKOFF_MAX_SECONDS)
        return min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_MAX_SECONDS)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        for attempt in range(1, MAX_RETRIES + 1):
            self._pace()
            resp: requests.Response | None = None
            try:
                resp = self.session.get(url, params=params, timeout=settings.HTTP_TIMEOUT_SECONDS)
                if resp.status_code in RETRYABLE_STATUS:
                    raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
                resp.raise_for_status()
                body = resp.json()
                if not body.get("success", False):
                    raise RuntimeError(f"API error on {path} {params}: {body}")
                return body["result"]
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
                retryable = resp is None or resp.status_code in RETRYABLE_STATUS
                if not retryable or attempt == MAX_RETRIES:
                    raise
                delay = self._retry_delay(resp, attempt)
                log.warning("GET %s failed (%s), retry %d/%d in %.1fs", path, exc, attempt, MAX_RETRIES - 1, delay)
                time.sleep(delay)
        raise AssertionError("unreachable")


_client: _PublicClient | None = None


def client() -> _PublicClient:
    global _client
    if _client is None:
        _client = _PublicClient()
    return _client


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #
def fetch_and_save_metadata(symbols: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    """Fetch ``GET /v2/products/{symbol}`` for each symbol and save to ``metadata/{symbol}.json``."""
    out: dict[str, dict[str, Any]] = {}
    metadata_dir().mkdir(parents=True, exist_ok=True)
    for sym in symbols or settings.SYMBOLS:
        product = client().get(f"/v2/products/{sym}")
        path = metadata_dir() / f"{sym}.json"
        _atomic_write_bytes(path, json.dumps(product, indent=2, sort_keys=True).encode("utf-8"))
        out[sym] = product
        log.info("metadata %s -> %s (product_id=%s)", sym, path, product.get("id"))
    return out


def load_metadata(symbol: str) -> dict[str, Any]:
    """Read cached product metadata, fetching it first if not on disk."""
    path = metadata_dir() / f"{symbol}.json"
    if not path.exists():
        return fetch_and_save_metadata([symbol])[symbol]
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 1m downloads
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DownloadResult:
    symbol: str
    series_type: SeriesType
    path: Path
    rows_before: int
    rows_after: int
    rows_fetched: int
    requests: int


def _last_closed_minute(now: float | None = None) -> int:
    """Open time of the most recent fully closed 1m candle."""
    now_s = int(time.time() if now is None else now)
    return now_s - now_s % MINUTE - MINUTE


def _fetch_range(symbol: str, series_type: SeriesType, start: int, end: int) -> tuple[pd.DataFrame, int]:
    """Fetch 1m candles with open time in [start, end] (inclusive) using 32h, 8h-aligned chunks."""
    sym = api_symbol(symbol, series_type)
    frames: list[pd.DataFrame] = []
    n_requests = 0
    chunk_start = start - start % FUNDING_INTERVAL_SECONDS
    while chunk_start <= end:
        chunk_end = chunk_start + CHUNK_SECONDS - MINUTE  # inclusive -> exactly 1920 candles
        rows = client().get(
            "/v2/history/candles",
            {"resolution": "1m", "symbol": sym, "start": chunk_start, "end": chunk_end},
        )
        n_requests += 1
        log.debug("%s %s..%s -> %d rows", sym, chunk_start, chunk_end, len(rows))
        if rows:
            frames.append(pd.DataFrame.from_records(rows))
        chunk_start += CHUNK_SECONDS

    if not frames:
        return _empty_raw(), n_requests
    df = _normalise_raw(pd.concat(frames, ignore_index=True))
    df = df[(df["time"] >= start) & (df["time"] <= end)]
    return df.reset_index(drop=True), n_requests


def _empty_raw() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="int64" if c == "time" else "float64") for c in RAW_COLUMNS})


def _normalise_raw(df: pd.DataFrame) -> pd.DataFrame:
    for col in RAW_COLUMNS:
        if col not in df.columns:
            df[col] = np.nan
    df = df[RAW_COLUMNS].copy()
    df["time"] = df["time"].astype("int64")
    for col in OHLCV_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    df["volume"] = df["volume"].fillna(0.0)
    return df


def _merge(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    # New rows win on duplicate `time` (refreshes candles re-fetched by the 8h snap).
    merged = pd.concat([existing, new], ignore_index=True)
    merged = merged.drop_duplicates(subset="time", keep="last")
    return merged.sort_values("time", kind="stable").reset_index(drop=True)


def _tmp_path(path: Path) -> Path:
    """A temp file unique per process and call, so concurrent writers (dashboard sync + bot) never share one."""
    return path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    try:
        df.to_parquet(tmp, engine="pyarrow", index=False, compression="zstd")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_raw_1m(symbol: str, series_type: SeriesType) -> pd.DataFrame:
    path = raw_1m_path(symbol, series_type)
    if not path.exists():
        return _empty_raw()
    return pd.read_parquet(path, engine="pyarrow")


def download_1m_series(symbol: str, series_type: SeriesType, days: int | None = None) -> DownloadResult:
    """Download (or incrementally extend) the 1m series and persist it as Parquet.

    The requested range is the last ``days`` days up to the last fully closed minute.
    If a Parquet file exists, only the missing older and/or newer ranges are fetched.
    Existing data outside the requested range is kept, never trimmed.
    """
    api_symbol(symbol, series_type)  # validates series_type
    days = days or settings.DEFAULT_LOOKBACK_DAYS
    req_end = _last_closed_minute()
    req_start = req_end + MINUTE - days * 86_400

    path = raw_1m_path(symbol, series_type)
    existing = load_raw_1m(symbol, series_type)
    rows_before = len(existing)

    ranges: list[tuple[int, int]] = []
    if existing.empty:
        ranges.append((req_start, req_end))
    else:
        have_min, have_max = int(existing["time"].min()), int(existing["time"].max())
        if req_start < have_min:
            ranges.append((req_start, have_min - MINUTE))
        if have_max < req_end:
            ranges.append((have_max + MINUTE, req_end))

    fetched: list[pd.DataFrame] = []
    n_requests = 0
    for a, b in ranges:
        df, n = _fetch_range(symbol, series_type, a, b)
        fetched.append(df)
        n_requests += n

    rows_fetched = sum(len(f) for f in fetched)
    merged = _merge(existing, pd.concat(fetched, ignore_index=True)) if fetched else existing
    if ranges:
        merged, n_rep = _repair_gaps(symbol, series_type, merged)
        n_requests += n_rep
        _atomic_write_parquet(merged, path)

    log.info(
        "%s %s: %d -> %d rows (fetched %d in %d requests)",
        symbol, series_type, rows_before, len(merged), rows_fetched, n_requests,
    )
    return DownloadResult(symbol, series_type, path, rows_before, len(merged), rows_fetched, n_requests)


MAX_GAP_REPAIRS = 200


def _repair_gaps(symbol: str, series_type: SeriesType, df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Re-fetch internal gaps with a query that starts one funding interval earlier.

    Older Delta history omits the first minute of a query whose start sits exactly on an 8h
    boundary (seen on FUNDING:* before Oct 2025), which left 1-minute holes at chunk starts.
    """
    gaps = find_gaps(df["time"]).gaps[:MAX_GAP_REPAIRS]
    if not gaps:
        return df, 0
    sym = api_symbol(symbol, series_type)
    frames = []
    for a, b in gaps:
        q_start = a - a % FUNDING_INTERVAL_SECONDS - FUNDING_INTERVAL_SECONDS
        rows = client().get("/v2/history/candles", {"resolution": "1m", "symbol": sym, "start": q_start, "end": b})
        if rows:
            part = _normalise_raw(pd.DataFrame.from_records(rows))
            frames.append(part[(part["time"] >= a) & (part["time"] <= b)])
    repaired = _merge(df, pd.concat(frames, ignore_index=True)) if frames else df
    log.info("%s %s: repaired %d of %d gap(s) with %d request(s)", symbol, series_type,
             len(gaps) - find_gaps(repaired["time"]).gap_count, len(gaps), len(gaps))
    return repaired, len(gaps)


def repair_series(symbol: str, series_type: SeriesType) -> int:
    """Repair gaps in an existing Parquet file; returns the remaining gap count."""
    path = raw_1m_path(symbol, series_type)
    df = load_raw_1m(symbol, series_type)
    fixed, n = _repair_gaps(symbol, series_type, df)
    if n:
        _atomic_write_parquet(fixed, path)
    return find_gaps(fixed["time"]).gap_count


def download_all(symbols: Iterable[str] | None = None, days: int | None = None) -> list[DownloadResult]:
    return [
        download_1m_series(sym, st, days)
        for sym in (symbols or settings.SYMBOLS)
        for st in SERIES_TYPES
    ]


# --------------------------------------------------------------------------- #
# Gap analysis
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class GapReport:
    rows: int
    start: int | None
    end: int | None
    expected_rows: int
    missing_minutes: int
    gap_count: int  # number of contiguous runs of missing minutes
    gaps: list[tuple[int, int]]  # (first_missing_time, last_missing_time)


def find_gaps(times: pd.Series | np.ndarray) -> GapReport:
    t = np.sort(np.asarray(times, dtype="int64"))
    if t.size == 0:
        return GapReport(0, None, None, 0, 0, 0, [])
    diffs = np.diff(t)
    idx = np.flatnonzero(diffs > MINUTE)
    gaps = [(int(t[i] + MINUTE), int(t[i + 1] - MINUTE)) for i in idx]
    expected = int((t[-1] - t[0]) // MINUTE + 1)
    return GapReport(
        rows=int(t.size),
        start=int(t[0]),
        end=int(t[-1]),
        expected_rows=expected,
        missing_minutes=expected - int(t.size),
        gap_count=len(gaps),
        gaps=gaps,
    )


# --------------------------------------------------------------------------- #
# Unified dataset
# --------------------------------------------------------------------------- #
def load_unified_1m_dataset(symbol: str, days: int | None = None) -> pd.DataFrame:
    """Join TRADE, MARK and FUNDING 1m series on the trade candles' UTC open time.

    Index: tz-aware UTC DatetimeIndex ``time`` (candle open), ascending, 60s grid.
    Columns:
      open, high, low, close, volume            trade OHLCV (only real trade candles; gaps are NOT filled)
      mark_open, mark_high, mark_low, mark_close
      funding_rate_pct                          Delta's funding value as published, in percent
      funding_rate                              same value as a decimal fraction (= pct / 100)

    Funding is forward-filled only within its own 8h funding window, never across a boundary
    and never backwards. Gap info is attached as ``df.attrs["gaps"]`` (GapReport per series).
    """
    trade = load_raw_1m(symbol, "TRADE")
    if trade.empty:
        raise FileNotFoundError(f"No TRADE data for {symbol}; run download_1m_series first")
    mark = load_raw_1m(symbol, "MARK")
    funding = load_raw_1m(symbol, "FUNDING")

    if days is not None:
        cutoff = int(trade["time"].max()) + MINUTE - days * 86_400
        trade, mark, funding = (d[d["time"] >= cutoff] for d in (trade, mark, funding))

    df = trade.set_index("time")[OHLCV_COLUMNS]
    m = mark.set_index("time")[["open", "high", "low", "close"]].add_prefix("mark_")
    f = funding.set_index("time")[["close"]].rename(columns={"close": "funding_rate_pct"})
    df = df.join(m, how="left").join(f, how="left")

    window = df.index.to_numpy() // FUNDING_INTERVAL_SECONDS
    df["funding_rate_pct"] = df["funding_rate_pct"].groupby(window).ffill()
    df["funding_rate"] = df["funding_rate_pct"] / 100.0

    gaps = {
        "TRADE": find_gaps(trade["time"]),
        "MARK": find_gaps(mark["time"]),
        "FUNDING": find_gaps(funding["time"]),
    }
    missing_mark = int(df["mark_close"].isna().sum())
    missing_funding = int(df["funding_rate_pct"].isna().sum())
    if gaps["TRADE"].gap_count:
        log.warning(
            "%s TRADE: %d gap(s), %d missing 1m candles (left as gaps, not filled)",
            symbol, gaps["TRADE"].gap_count, gaps["TRADE"].missing_minutes,
        )
    if missing_mark:
        log.warning("%s: %d trade rows have no MARK candle", symbol, missing_mark)
    if missing_funding:
        log.warning("%s: %d trade rows have no funding value in their 8h window", symbol, missing_funding)

    df.index = pd.to_datetime(df.index, unit="s", utc=True)
    df.index.name = "time"
    df.attrs["symbol"] = symbol
    df.attrs["gaps"] = gaps
    df.attrs["missing_mark_rows"] = missing_mark
    df.attrs["missing_funding_rows"] = missing_funding
    return df


# --------------------------------------------------------------------------- #
# Resampling
# --------------------------------------------------------------------------- #
_AGG: dict[str, str] = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
    "mark_open": "first",
    "mark_high": "max",
    "mark_low": "min",
    "mark_close": "last",
    "funding_rate_pct": "last",
    "funding_rate": "last",
}


def resample_candles(df_1m: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Resample the unified 1m frame into complete, gap-free ``timeframe`` bars.

    Buckets are UTC, left-closed and left-labelled (bar time = bucket open), anchored to
    the Unix epoch. Buckets with fewer than the expected number of 1m trade candles
    (gaps, range edges, or the still-forming bar) are dropped.
    """
    if timeframe not in settings.SUPPORTED_RESAMPLE_TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {settings.SUPPORTED_RESAMPLE_TIMEFRAMES}, got {timeframe!r}")
    minutes = TIMEFRAME_MINUTES[timeframe]
    if not isinstance(df_1m.index, pd.DatetimeIndex):
        raise TypeError("df_1m must have a DatetimeIndex (use load_unified_1m_dataset)")

    agg = {c: a for c, a in _AGG.items() if c in df_1m.columns}
    r = df_1m.resample(f"{minutes}min", closed="left", label="left", origin="epoch")
    out = r.agg(agg)
    counts = r["close"].count()
    out = out[counts == minutes]
    out.attrs = {
        "symbol": df_1m.attrs.get("symbol"),
        "timeframe": timeframe,
        "dropped_partial_buckets": int(((counts > 0) & (counts < minutes)).sum()),
        "dropped_empty_buckets": int((counts == 0).sum()),
    }
    return out
