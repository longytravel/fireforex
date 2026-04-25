"""Dukascopy raw-bi5 M1 candle downloader for Fire Forex.

Fetches per-day ``BID_candles_min_1.bi5`` and ``ASK_candles_min_1.bi5``
files from the public Dukascopy datafeed, decompresses (LZMA), unpacks
the 24-byte records and writes a single ``{pair}_M1.parquet`` file per
pair with OHLCV + computed spread.

This module exists because ``dukascopy_python`` 3.x/4.x are both broken
upstream (iterator crash on None rows). Pattern mirrors
``tick_downloader.py`` and hits the same CDN.

Record format (24 bytes, big-endian, per candle):

    >IIIIIf
     ||||||
     |||||- float32 volume (millions of units)
     ||||-- uint32  high  (scaled)
     |||--- uint32  low   (scaled)
     ||---- uint32  close (scaled)
     |----- uint32  open  (scaled)
     uint32 seconds offset from day start (UTC)

Prices are uint32 scaled integers:
    JPY-quoted pairs → divide by 1e3
    Everything else  → divide by 1e5

Empirically verified against Dukascopy's GBP/USD 2024-06-03 file:
record 0 decodes to (0 s, 127439, 127429, 127428, 127439, 55.89) →
O=1.27439, C=1.27429, L=1.27428, H=1.27439 at 00:00 UTC.
"""

from __future__ import annotations

import lzma
import struct
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

import pandas as pd

from ff import harness

from . import inventory

LogCallback = Callable[[str], None]
CancelCallback = Callable[[], bool]


_BASE_URL = "https://datafeed.dukascopy.com/datafeed"
_REC_FMT = ">IIIIIf"
_REC_SIZE = struct.calcsize(_REC_FMT)  # 24 bytes
_UA = "Mozilla/5.0 (FireForex M1 downloader)"


def _emit(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb is None:
        return
    try:
        log_cb(msg)
    except Exception:
        pass


def _symbol(pair: str) -> str:
    return pair.replace("_", "").upper()  # EUR_USD → EURUSD


def _price_scale(pair: str) -> float:
    return 1e3 if "JPY" in pair.upper() else 1e5


def _target_path(pair: str) -> Path:
    return harness.DATA_ROOT / f"{pair}_M1.parquet"


def _day_url(pair: str, d: date, side: str) -> str:
    # side ∈ {"BID", "ASK"}. Months zero-based in URL.
    return f"{_BASE_URL}/{_symbol(pair)}/{d.year:04d}/{d.month - 1:02d}/{d.day:02d}/{side}_candles_min_1.bi5"


def _fetch_day(url: str, retries: int = 3, timeout: float = 45.0) -> bytes:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": _UA})
            with urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:
            msg = str(exc).lower()
            if "404" in msg or "not found" in msg:
                return b""
            last_err = exc
            time.sleep(2.0**attempt)
    assert last_err is not None
    raise last_err


def _decode_candles(raw: bytes, day_start: datetime, scale: float) -> pd.DataFrame:
    """Decompress + unpack one day of M1 candle records."""
    cols = ["timestamp", "open", "high", "low", "close", "volume"]
    if not raw:
        return pd.DataFrame(columns=cols)

    try:
        data = lzma.decompress(raw, format=lzma.FORMAT_ALONE)
    except lzma.LZMAError:
        data = lzma.decompress(raw)

    n = len(data) // _REC_SIZE
    if n == 0:
        return pd.DataFrame(columns=cols)

    t_list = [0] * n
    o_list = [0.0] * n
    c_list = [0.0] * n
    l_list = [0.0] * n
    h_list = [0.0] * n
    v_list = [0.0] * n

    unp = struct.Struct(_REC_FMT).unpack_from
    inv_scale = 1.0 / scale
    for i in range(n):
        t, o, c, l, h, v = unp(data, i * _REC_SIZE)
        t_list[i] = t
        o_list[i] = o * inv_scale
        c_list[i] = c * inv_scale
        l_list[i] = l * inv_scale
        h_list[i] = h * inv_scale
        v_list[i] = v

    base = pd.Timestamp(day_start)
    ts = base + pd.to_timedelta(t_list, unit="s")

    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": o_list,
            "high": h_list,
            "low": l_list,
            "close": c_list,
            "volume": v_list,
        }
    )


def _write_atomic(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".partial")
    df.to_parquet(tmp, index=False)
    if path.exists():
        path.unlink()
    tmp.replace(path)


def _existing_max_ts(path: Path) -> datetime | None:
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception:
        return None
    if df.empty:
        return None
    # Handle both legacy (DatetimeIndex) and new (timestamp column) shapes.
    if isinstance(df.index, pd.DatetimeIndex):
        return df.index.max().to_pydatetime()
    if "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce").dropna()
        return ts.max().to_pydatetime() if len(ts) else None
    return None


def _iter_days(start_d: date, end_d: date):
    cur = start_d
    while cur <= end_d:
        yield cur
        cur = cur + timedelta(days=1)


def _fetch_day_via_ticks(
    pair: str,
    d: date,
    side: str,
    scale: float,
    log_cb: LogCallback | None,
    cancel_cb: CancelCallback | None,
) -> pd.DataFrame:
    """Fallback for today's partial day: fetch per-hour tick bi5 files and
    aggregate to M1 OHLC for the requested side.

    Dukascopy publishes per-day candle bi5 only AFTER the UTC day closes
    (~24h lag). The per-hour tick bi5 files are published minutes after
    each hour, so for today we stitch them together and roll up to M1.
    Returns the same schema as ``_decode_candles``.
    """
    from . import tick_downloader as _td

    day_start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    now_utc = datetime.now(timezone.utc)
    last_hour = now_utc.hour if d == now_utc.date() else 23
    col = "bid" if side == "BID" else "ask"
    frames: list[pd.DataFrame] = []

    for h in range(0, last_hour + 1):
        if cancel_cb is not None and cancel_cb():
            break
        hour_start = day_start + timedelta(hours=h)
        url = _td._hour_url(pair, hour_start)
        try:
            raw = _td._fetch_hour(url)
        except Exception as exc:
            _emit(log_cb, f"    {side} tick fetch err @ {hour_start}: {exc}")
            continue
        ticks = _td._decode_ticks(raw, hour_start, scale)
        if ticks.empty:
            continue
        frames.append(ticks[["timestamp", col]])
        time.sleep(0.02)

    if not frames:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    ticks_df = pd.concat(frames, ignore_index=True)
    ticks_df = ticks_df.set_index("timestamp").sort_index()
    # Resample ticks → M1 OHLC. Dropna() skips empty minutes (weekends etc.).
    m1 = ticks_df[col].resample("1min").ohlc().dropna(how="any")
    m1 = m1.reset_index()
    m1["volume"] = 0.0  # tick files don't carry per-minute volume reliably
    _emit(
        log_cb,
        f"  {side} tick-fallback @ {d.isoformat()}: {len(m1)} M1 bars from {last_hour + 1} hours",
    )
    return m1[["timestamp", "open", "high", "low", "close", "volume"]]


def _fetch_side(
    pair: str,
    days: list[date],
    side: str,
    scale: float,
    log_cb: LogCallback | None,
    cancel_cb: CancelCallback | None,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    fetched = 0
    today = datetime.now(timezone.utc).date()
    for d in days:
        if cancel_cb is not None and cancel_cb():
            _emit(log_cb, f"cancelled by user ({side})")
            break
        # Skip Saturday — no data published.
        if d.weekday() == 5:
            continue
        day_start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        url = _day_url(pair, d, side)
        try:
            raw = _fetch_day(url)
        except Exception as exc:
            _emit(log_cb, f"    {side} fetch error @ {d.isoformat()}: {exc}")
            continue
        df = _decode_candles(raw, day_start, scale)
        if df.empty and d == today:
            # Per-day candle file not published yet — stitch from ticks.
            df = _fetch_day_via_ticks(pair, d, side, scale, log_cb, cancel_cb)
        if not df.empty:
            frames.append(df)
        fetched += 1
        if fetched % 30 == 0:
            _emit(log_cb, f"  {side} progress — {fetched}/{len(days)} days")
        time.sleep(0.05)

    if not frames:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    return pd.concat(frames, ignore_index=True)


def download(
    pair: str,
    start: date,
    end: date,
    *,
    append: bool = True,
    log_cb: LogCallback | None = None,
    cancel_cb: CancelCallback | None = None,
) -> dict:
    """Download a window of Dukascopy M1 candles to ``{pair}_M1.parquet``.

    Fetches BID + ASK sides, computes per-candle spread, writes
    indexed-by-timestamp parquet matching the existing Fire Forex schema
    ``[open, high, low, close, volume, spread]``.
    """
    scale = _price_scale(pair)
    path = _target_path(pair)

    start_d = start
    end_d = end

    existing: pd.DataFrame | None = None
    if append and path.exists():
        try:
            existing = pd.read_parquet(path)
        except Exception:
            existing = None
        if existing is not None:
            # Normalise to timestamp column for the existing-day-set check.
            if isinstance(existing.index, pd.DatetimeIndex):
                if existing.index.name is None or existing.index.name.lower() != "timestamp":
                    existing.index.name = "timestamp"
                existing = existing.reset_index()
            existing.columns = [c.lower() for c in existing.columns]
            ts = pd.to_datetime(existing["timestamp"], utc=True, errors="coerce").dropna()
            _emit(
                log_cb,
                f"append — existing M1 has {len(existing):,} bars "
                f"({ts.min().date()} → {ts.max().date()}). "
                f"Will fetch user window {start_d} → {end_d} and merge "
                f"(skip days already covered).",
            )

    # Always honour the user's window. Skip days already covered (with at least
    # 1 bar) by the existing file — supports both backfill and forward-append.
    # EXCEPTION: today's bars are always re-fetched — the per-hour tick
    # fallback grows during the trading day, so yesterday's "covered" flag
    # would otherwise freeze today at whatever partial state we first saw.
    today = datetime.now(timezone.utc).date()
    requested_days = [d for d in _iter_days(start_d, end_d) if d.weekday() != 5]
    have_days: set[date] = set()
    if existing is not None and not existing.empty:
        ts_existing = pd.to_datetime(existing["timestamp"], utc=True, errors="coerce").dropna()
        have_days = set(ts_existing.dt.date.unique())
    days = [d for d in requested_days if d == today or d not in have_days]

    if not days:
        _emit(log_cb, "nothing to fetch — every requested weekday already has bars")
        return {
            "path": str(path),
            "appended": True,
            "new_bars": 0,
            "total_bars": int(len(existing)) if existing is not None else 0,
            "start_ts": None,
            "end_ts": None,
        }

    _emit(log_cb, f"→ {pair} M1  {days[0].isoformat()} → {days[-1].isoformat()}")
    _emit(log_cb, f"  {len(days)} days × 2 sides (BID+ASK) to fetch")

    bid_df = _fetch_side(pair, days, "BID", scale, log_cb, cancel_cb)
    ask_df = _fetch_side(pair, days, "ASK", scale, log_cb, cancel_cb)

    # Merge BID + ASK, compute spread. All frames use timestamp as a COLUMN
    # (not an index) to match the new schema produced by ff.data.resample.
    cols = ["timestamp", "open", "high", "low", "close", "volume", "spread"]
    if bid_df.empty:
        _emit(log_cb, "no new BID data")
        new_df = pd.DataFrame(columns=cols)
    else:
        bid_df = bid_df.drop_duplicates(subset=["timestamp"], keep="last")
        bid_df = bid_df.sort_values("timestamp").reset_index(drop=True)

        if not ask_df.empty:
            ask_df = ask_df.drop_duplicates(subset=["timestamp"], keep="last")
            ask_df = ask_df.sort_values("timestamp").reset_index(drop=True)
            merged = bid_df.merge(
                ask_df[["timestamp", "open", "close"]].rename(columns={"open": "_ask_o", "close": "_ask_c"}),
                on="timestamp",
                how="left",
            )
            merged["spread"] = ((merged["_ask_o"] - merged["open"]) + (merged["_ask_c"] - merged["close"])) / 2.0
            bid_df = merged.drop(columns=["_ask_o", "_ask_c"])
        else:
            bid_df["spread"] = float("nan")

        new_df = bid_df[["timestamp", "open", "high", "low", "close", "volume", "spread"]]

    new_bars = int(len(new_df))

    if existing is not None and not new_df.empty:
        # Normalise existing to timestamp-as-column if it's a legacy
        # DatetimeIndex file.
        if isinstance(existing.index, pd.DatetimeIndex):
            if existing.index.name is None or existing.index.name.lower() != "timestamp":
                existing.index.name = "timestamp"
            existing = existing.reset_index()
        existing.columns = [c.lower() for c in existing.columns]
        if "spread" not in existing.columns:
            existing["spread"] = float("nan")
        existing = existing[[c for c in new_df.columns if c in existing.columns]]
        for c in new_df.columns:
            if c not in existing.columns:
                existing[c] = pd.NA
        existing = existing[new_df.columns]
        existing["timestamp"] = pd.to_datetime(existing["timestamp"], utc=True, errors="coerce")
        existing = existing.dropna(subset=["timestamp"])
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["timestamp"], keep="last")
        combined = combined.sort_values("timestamp").reset_index(drop=True)
        final_df = combined
    elif existing is not None:
        final_df = existing
    else:
        final_df = new_df

    if not final_df.empty:
        _write_atomic(path, final_df)

    inventory.invalidate()

    return {
        "path": str(path),
        "appended": append and existing is not None,
        "new_bars": new_bars,
        "total_bars": int(len(final_df)),
        "start_ts": (final_df["timestamp"].iloc[0].isoformat() if not final_df.empty else None),
        "end_ts": (final_df["timestamp"].iloc[-1].isoformat() if not final_df.empty else None),
    }
