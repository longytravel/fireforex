"""Dukascopy tick-level downloader for Fire Forex.

Fetches hourly ``.bi5`` (LZMA-compressed) tick files from the public
Dukascopy datafeed, decompresses them, unpacks the fixed-struct records
and writes a single ``{pair}_TICK.parquet`` file per pair. Append mode
writes only rows strictly newer than the existing max timestamp.

Record format (one row = 20 bytes, big-endian):

    >IIIff
     |||||
     |||||- float32 bid volume  (millions)
     ||||-- float32 ask volume  (millions)
     |||--- uint32  bid price   (scaled integer)
     ||---- uint32  ask price   (scaled integer)
     |----- uint32  ms offset from the file's hour start

Prices use a pair-dependent scale:

    JPY-quoted pairs (e.g. USD_JPY)  → divide by 1e3
    Everything else  (e.g. EUR_USD)  → divide by 1e5

The module is self-contained — no third-party tick-specific libraries
are required beyond the stdlib + pandas.
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
_REC_FMT = ">IIIff"
_REC_SIZE = struct.calcsize(_REC_FMT)  # 20 bytes
_UA = "Mozilla/5.0 (FireForex tick downloader)"


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
    """JPY pairs quote 3 decimals (scale 1e3); all others 5 decimals (1e5)."""
    return 1e3 if "JPY" in pair.upper() else 1e5


def _target_path(pair: str) -> Path:
    return harness.DATA_ROOT / f"{pair}_TICK.parquet"


def _hour_url(pair: str, ts: datetime) -> str:
    # Dukascopy months are zero-based in the URL.
    return f"{_BASE_URL}/{_symbol(pair)}/{ts.year:04d}/{ts.month - 1:02d}/{ts.day:02d}/{ts.hour:02d}h_ticks.bi5"


def _fetch_hour(url: str, retries: int = 3, timeout: float = 30.0) -> bytes:
    """Return the raw compressed bytes for one hour file.

    Returns b"" if the server returns a 404 (common on weekends / gaps).
    Retries on transient errors with exponential backoff.
    """
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": _UA})
            with urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # HTTPError / URLError / socket.timeout
            msg = str(exc).lower()
            if "404" in msg or "not found" in msg:
                return b""
            last_err = exc
            time.sleep(2.0**attempt)
    assert last_err is not None
    raise last_err


def _decode_ticks(raw: bytes, hour_start: datetime, scale: float) -> pd.DataFrame:
    """Decompress + unpack one hour of tick records. Empty payload → empty frame."""
    cols = ["timestamp", "bid", "ask", "bid_volume", "ask_volume"]
    if not raw:
        return pd.DataFrame(columns=cols)

    # Dukascopy ships raw LZMA (legacy ``.lzma`` / ``FORMAT_ALONE`` stream).
    try:
        data = lzma.decompress(raw, format=lzma.FORMAT_ALONE)
    except lzma.LZMAError:
        # Some newer hours use the full xz container.
        data = lzma.decompress(raw)

    n = len(data) // _REC_SIZE
    if n == 0:
        return pd.DataFrame(columns=cols)

    ms_list: list[int] = [0] * n
    bid_list: list[float] = [0.0] * n
    ask_list: list[float] = [0.0] * n
    bid_vol_list: list[float] = [0.0] * n
    ask_vol_list: list[float] = [0.0] * n

    unp = struct.Struct(_REC_FMT).unpack_from
    for i in range(n):
        ms, ask_i, bid_i, ask_v, bid_v = unp(data, i * _REC_SIZE)
        ms_list[i] = ms
        ask_list[i] = ask_i / scale
        bid_list[i] = bid_i / scale
        ask_vol_list[i] = ask_v
        bid_vol_list[i] = bid_v

    ts = pd.to_datetime(ms_list, unit="ms", utc=True) + pd.Timedelta(hour_start.timestamp(), unit="s")
    # Safer reconstruction: build from hour_start directly to avoid float drift.
    base = pd.Timestamp(hour_start)
    ts = base + pd.to_timedelta(ms_list, unit="ms")

    return pd.DataFrame(
        {
            "timestamp": ts,
            "bid": bid_list,
            "ask": ask_list,
            "bid_volume": bid_vol_list,
            "ask_volume": ask_vol_list,
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
        df = pd.read_parquet(path, columns=["timestamp"])
    except Exception:
        return None
    if df.empty:
        return None
    ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce").dropna()
    return ts.max().to_pydatetime() if len(ts) else None


def _iter_hours(start_dt: datetime, end_dt: datetime):
    cur = start_dt.replace(minute=0, second=0, microsecond=0)
    stop = end_dt.replace(minute=0, second=0, microsecond=0)
    while cur <= stop:
        yield cur
        cur = cur + timedelta(hours=1)


# ── entry point ────────────────────────────────────────────────────────────


def download(
    pair: str,
    start: date,
    end: date,
    *,
    append: bool = True,
    log_cb: LogCallback | None = None,
    cancel_cb: CancelCallback | None = None,
) -> dict:
    """Download a window of Dukascopy ticks to ``{pair}_TICK.parquet``.

    Returns a summary dict: ``{path, appended, new_rows, total_rows,
    start_ts, end_ts}``.
    """
    scale = _price_scale(pair)
    path = _target_path(pair)

    start_dt = datetime(start.year, start.month, start.day, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(end.year, end.month, end.day, 23, 0, 0, tzinfo=timezone.utc)

    existing: pd.DataFrame | None = None
    effective_start = start_dt
    if append and path.exists():
        last = _existing_max_ts(path)
        if last is not None:
            effective_start = max(start_dt, (last + timedelta(seconds=1)))
            try:
                existing = pd.read_parquet(path)
                existing.columns = [c.lower() for c in existing.columns]
            except Exception:
                existing = None
            _emit(
                log_cb,
                f"append — existing max ts {last.isoformat()}; fetching from {effective_start.isoformat()}",
            )

    if effective_start > end_dt:
        _emit(log_cb, "nothing to fetch — existing ticks already cover the requested end")
        return {
            "path": str(path),
            "appended": True,
            "new_rows": 0,
            "total_rows": int(len(existing)) if existing is not None else 0,
            "start_ts": None,
            "end_ts": None,
        }

    _emit(log_cb, f"→ {pair} TICK  {effective_start.isoformat()} → {end_dt.isoformat()}")

    hours = list(_iter_hours(effective_start, end_dt))
    _emit(log_cb, f"  {len(hours)} hour files to fetch")
    frames: list[pd.DataFrame] = []
    fetched = 0
    for h in hours:
        if cancel_cb is not None and cancel_cb():
            _emit(log_cb, "cancelled by user")
            break
        # Skip weekend hours — Dukascopy publishes an empty file anyway but this
        # saves a network round-trip per hour.
        if h.weekday() == 5:  # Saturday
            continue
        if h.weekday() == 6 and h.hour < 22:  # Sunday before 22:00 UTC
            continue
        if h.weekday() == 4 and h.hour >= 22:  # Friday after 22:00 UTC
            continue

        url = _hour_url(pair, h)
        try:
            raw = _fetch_hour(url)
        except Exception as exc:
            _emit(log_cb, f"    fetch error @ {h.isoformat()}: {exc}")
            continue
        df = _decode_ticks(raw, h, scale)
        if not df.empty:
            frames.append(df)
        fetched += 1
        if fetched % 24 == 0:
            _emit(log_cb, f"  progress — {fetched}/{len(hours)} hours")
        # Polite pacing so we don't hammer the feed.
        time.sleep(0.05)

    new_df = (
        pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["timestamp", "bid", "ask", "bid_volume", "ask_volume"])
    )
    new_rows = int(len(new_df))

    if existing is not None and not new_df.empty:
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined["timestamp"] = pd.to_datetime(combined["timestamp"], utc=True, errors="coerce")
        combined = combined.dropna(subset=["timestamp"])
        combined = combined.drop_duplicates(subset=["timestamp"], keep="last")
        combined = combined.sort_values("timestamp").reset_index(drop=True)
        final_df = combined
    elif existing is not None:
        final_df = existing
    else:
        new_df["timestamp"] = pd.to_datetime(new_df["timestamp"], utc=True, errors="coerce")
        final_df = new_df.sort_values("timestamp").reset_index(drop=True)

    if not final_df.empty:
        _write_atomic(path, final_df)

    inventory.invalidate()

    return {
        "path": str(path),
        "appended": append and existing is not None,
        "new_rows": new_rows,
        "total_rows": int(len(final_df)),
        "start_ts": (final_df["timestamp"].iloc[0].isoformat() if not final_df.empty else None),
        "end_ts": (final_df["timestamp"].iloc[-1].isoformat() if not final_df.empty else None),
    }
