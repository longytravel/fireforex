"""Dukascopy downloader for Fire Forex.

Wraps the ``dukascopy_python`` pip package. Writes parquet directly into
``DATA_ROOT`` (normally ``G:\\My Drive\\BackTestData``). Supports:

- Fresh download: build the full file from ``start`` → ``end``.
- Incremental append: detect ``max(timestamp)`` of the existing file and fetch
  only the tail window.

Writes are crash-safe: each file is written as ``<name>.partial`` then
atomically renamed. Inventory cache is invalidated at the end.

The module is intentionally standalone — no config files, no TOML. The user
can edit it here alongside the rest of the codebase. Rate-limit / retry
policy lives inline.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from ff import harness
from . import inventory


LogCallback = Callable[[str], None]

_TF_TO_DUKASCOPY = {
    "M1": "1min",
    "M5": "5min",
    "M15": "15min",
    "M30": "30min",
    "H1": "1h",
    "H4": "4h",
    "D": "1d",
    "W": "1w",
}

_CANONICAL_COLUMNS = ["open", "high", "low", "close", "volume", "spread"]


@dataclass
class DownloadRequest:
    pair: str
    tf: str
    start: date
    end: date
    append: bool = True


# ── Fetch ────────────────────────────────────────────────────────────────

def _emit(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb is None:
        return
    try:
        log_cb(msg)
    except Exception:
        pass


def _chunks(start: date, end: date, years: int = 1) -> list[tuple[date, date]]:
    """Split [start, end] into <=N-year chunks — the Dukascopy API throttles
    large single requests and yearly granularity makes retry cheap."""
    out: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        nxt = date(cur.year + years, 1, 1)
        stop = min(end, nxt - timedelta(days=1))
        out.append((cur, stop))
        cur = stop + timedelta(days=1)
    return out


def _fetch_chunk(pair: str, tf: str, start: date, end: date,
                 log_cb: LogCallback | None) -> pd.DataFrame:
    """Call ``dukascopy_python.fetch`` with retry/backoff. Returns an empty
    DataFrame if the API has nothing for the window (weekends etc)."""
    import dukascopy_python

    tf_key = _TF_TO_DUKASCOPY[tf]
    instrument = pair.replace("_", "").lower()  # EUR_USD → eurusd

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            raw = dukascopy_python.fetch(
                instrument=instrument,
                interval=tf_key,
                offer_side="bid",
                start=datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
                end=datetime.combine(end, datetime.max.time(), tzinfo=timezone.utc),
            )
            if raw is None:
                return pd.DataFrame(columns=_CANONICAL_COLUMNS)
            df = pd.DataFrame(raw)
            df.columns = [c.lower() for c in df.columns]
            if "timestamp" not in df.columns and isinstance(df.index, pd.DatetimeIndex):
                df = df.reset_index().rename(columns={df.index.name or "index": "timestamp"})
            return _normalise(df)
        except Exception as exc:  # network / timeout / payload issue
            last_err = exc
            delay = 2.0 ** attempt
            _emit(log_cb, f"    retry {attempt+1}/3 in {delay:.0f}s after error: {exc}")
            time.sleep(delay)
    assert last_err is not None
    raise last_err


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce whatever Dukascopy returned into our canonical schema."""
    if df.empty:
        return df
    # Ensure lowercase, UTC-aware timestamp column.
    df.columns = [c.lower() for c in df.columns]
    if "timestamp" not in df.columns:
        raise ValueError("dukascopy payload missing timestamp column")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp")

    # Fill optional columns so the schema is stable across every file on disk.
    if "spread" not in df.columns:
        df["spread"] = 0.0
    if "volume" not in df.columns:
        df["volume"] = 0.0

    keep = ["timestamp", "open", "high", "low", "close", "volume", "spread"]
    return df[[c for c in keep if c in df.columns]].reset_index(drop=True)


# ── Persist ──────────────────────────────────────────────────────────────

def _target_path(pair: str, tf: str) -> Path:
    return harness.DATA_ROOT / f"{pair}_{tf}.parquet"


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


def _write_atomic(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".partial")
    df.to_parquet(tmp, index=False)
    if path.exists():
        path.unlink()
    tmp.replace(path)


# ── Entry point ──────────────────────────────────────────────────────────

def download(pair: str, tf: str, start: date, end: date, *,
             append: bool = True, log_cb: LogCallback | None = None,
             cancel_cb: Callable[[], bool] | None = None) -> dict:
    """Download a window of bars and persist to ``DATA_ROOT``.

    Returns a summary dict: ``{path, appended, new_bars, total_bars,
    start_ts, end_ts}``.
    """
    if tf not in _TF_TO_DUKASCOPY:
        raise ValueError(f"unsupported timeframe: {tf}")
    path = _target_path(pair, tf)

    effective_start = start
    existing: pd.DataFrame | None = None
    if append and path.exists():
        last = _existing_max_ts(path)
        if last is not None:
            nxt = (last + timedelta(seconds=1)).date()
            effective_start = max(start, nxt)
            try:
                existing = pd.read_parquet(path)
                existing.columns = [c.lower() for c in existing.columns]
            except Exception:
                existing = None
            _emit(log_cb, f"append mode — existing max ts {last.isoformat()}; "
                          f"fetching from {effective_start.isoformat()}")
        else:
            _emit(log_cb, "append mode requested but existing file unreadable; "
                          "falling back to full fetch")

    if effective_start > end:
        _emit(log_cb, "nothing to fetch — existing file already covers the requested end")
        return {"path": str(path), "appended": True, "new_bars": 0,
                "total_bars": int(existing.shape[0]) if existing is not None else 0,
                "start_ts": None, "end_ts": None}

    _emit(log_cb, f"→ {pair} {tf}  {effective_start.isoformat()} → {end.isoformat()}")

    frames: list[pd.DataFrame] = []
    chunks = _chunks(effective_start, end)
    for i, (c_start, c_end) in enumerate(chunks, 1):
        if cancel_cb is not None and cancel_cb():
            _emit(log_cb, "cancelled by user")
            break
        _emit(log_cb, f"  chunk {i}/{len(chunks)}  {c_start} → {c_end}")
        df = _fetch_chunk(pair, tf, c_start, c_end, log_cb)
        if not df.empty:
            frames.append(df)
            _emit(log_cb, f"    got {len(df):,} bars")
        else:
            _emit(log_cb, "    empty")
        time.sleep(0.5)  # polite pacing

    new_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=_CANONICAL_COLUMNS)
    new_bars = int(len(new_df))

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
        final_df = new_df

    if not final_df.empty:
        _write_atomic(path, final_df)

    inventory.invalidate()

    return {
        "path": str(path),
        "appended": append and existing is not None,
        "new_bars": new_bars,
        "total_bars": int(len(final_df)),
        "start_ts": (final_df["timestamp"].iloc[0].isoformat()
                     if not final_df.empty else None),
        "end_ts": (final_df["timestamp"].iloc[-1].isoformat()
                   if not final_df.empty else None),
    }
