"""MetaTrader 5 tick history downloader for Fire Forex.

Mirror of ``ff.data.mt5_m1_downloader`` for tick data. Pulls per-pair
tick history from a connected MT5 terminal and writes a
``[timestamp, bid, ask, bid_volume, ask_volume]`` parquet at
``MT5_DATA_ROOT / {pair}_TICK.parquet``.

Why this exists: MT5 M1 ``spread`` is a bar-close-tick snapshot, not a
time-weighted average. During quiet bars, the close tick lands at the
broker's 1-point quote-rounding floor, making per-session medians pin
to 0.1 pips on most pairs (issue #39). Real bid/ask sampled at every
quote change avoids the floor bias entirely — every tick is a genuine
quote, so per-session medians reflect typical execution cost.

Windows-only (the ``MetaTrader5`` pip package is a MetaQuotes binary).
On dev boxes without MT5 installed, import still succeeds; ``download()``
raises on first use.
"""

from __future__ import annotations

import os
import time as _time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from . import inventory
from .mt5_m1_downloader import (
    MT5_DATA_ROOT,
    _emit,
    _ensure_mt5_connected,
    _load_existing,
    _symbol_for,
    _write_atomic,
)

LogCallback = Callable[[str], None]
CancelCallback = Callable[[], bool]

_TICK_SCHEMA = ["timestamp", "bid", "ask", "bid_volume", "ask_volume"]


def _target_path(pair: str) -> Path:
    return MT5_DATA_ROOT / f"{pair}_TICK.parquet"


def _fetch_window(
    mt5: Any,
    symbol: str,
    start_utc: datetime,
    end_utc: datetime,
    *,
    log_cb: LogCallback | None = None,
    cancel_cb: CancelCallback | None = None,
) -> pd.DataFrame:
    """Pull ticks for [start_utc, end_utc] from MT5.

    Chunks by day to keep memory bounded — a busy major can produce 1M+
    ticks per day. Stops early if cancel_cb returns True.
    """
    chunks: list[pd.DataFrame] = []
    cursor = start_utc
    one_day = timedelta(days=1)
    while cursor < end_utc:
        if cancel_cb is not None and cancel_cb():
            _emit(log_cb, "cancelled mid-fetch")
            break
        chunk_end = min(cursor + one_day, end_utc)
        ticks = mt5.copy_ticks_range(
            symbol,
            cursor,
            chunk_end,
            mt5.COPY_TICKS_ALL,
        )
        if ticks is not None and len(ticks) > 0:
            chunks.append(pd.DataFrame(ticks))
        cursor = chunk_end

    if not chunks:
        return pd.DataFrame(columns=_TICK_SCHEMA)

    df = pd.concat(chunks, ignore_index=True)

    # MT5 ticks expose `time_msc` in broker-local milliseconds. Same UTC
    # offset probe as the M1 downloader.
    offset_sec = 0
    tick = mt5.symbol_info_tick(symbol)
    if tick is not None and tick.time:
        offset_sec = -(int(tick.time) - int(_time.time()))
    offset_ms = int(offset_sec) * 1000
    if "time_msc" in df.columns:
        df["timestamp"] = pd.to_datetime(
            df["time_msc"].astype("int64") + offset_ms,
            unit="ms",
            utc=True,
        )
    else:
        df["timestamp"] = pd.to_datetime(
            df["time"].astype("int64") + int(offset_sec),
            unit="s",
            utc=True,
        )

    if "volume_real" in df.columns:
        df["bid_volume"] = df["volume_real"].astype("float64")
        df["ask_volume"] = df["volume_real"].astype("float64")
    elif "volume" in df.columns:
        df["bid_volume"] = df["volume"].astype("float64")
        df["ask_volume"] = df["volume"].astype("float64")
    else:
        df["bid_volume"] = float("nan")
        df["ask_volume"] = float("nan")

    return df[_TICK_SCHEMA].copy()


def download(
    pair: str,
    start: date,
    end: date,
    *,
    append: bool = True,
    log_cb: LogCallback | None = None,
    cancel_cb: CancelCallback | None = None,
    symbol_map: dict[str, str] | None = None,
) -> dict:
    """Download MT5 ticks into ``MT5_DATA_ROOT / {pair}_TICK.parquet``.

    Append-only when ``append=True`` and an existing parquet is present:
    fetches from the last existing timestamp + 1ms onwards, so a partial
    failure does not require re-pulling years of history.
    """
    path = _target_path(pair)
    symbol = _symbol_for(pair, symbol_map)

    requested_start = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_utc = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)

    existing = _load_existing(path) if append else None

    effective_start = requested_start
    if existing is not None and not existing.empty:
        existing_ts = pd.to_datetime(existing["timestamp"], utc=True, errors="coerce").dropna()
        if not existing_ts.empty:
            last_ts = existing_ts.max().to_pydatetime()
            effective_start = max(requested_start, last_ts + timedelta(milliseconds=1))

    if cancel_cb is not None and cancel_cb():
        _emit(log_cb, "cancelled before MT5 fetch")
        return {
            "path": str(path),
            "appended": False,
            "new_ticks": 0,
            "total_ticks": int(len(existing)) if existing is not None else 0,
            "start_ts": None,
            "end_ts": None,
        }

    skip_env = os.environ.get("FF_MT5_SKIP_DOWNLOAD", "").lower() in ("1", "true", "yes")
    try:
        mt5 = None if skip_env else _ensure_mt5_connected()
    except RuntimeError as e:
        mt5 = None
        _emit(log_cb, f"MT5 terminal not reachable; using on-disk parquet only ({e})")

    if mt5 is None:
        new_df = pd.DataFrame(columns=_TICK_SCHEMA)
        if existing is None:
            raise FileNotFoundError(
                f"No MT5 terminal and no existing tick parquet at {path}. "
                "Open MT5 terminal on a Windows box and retry, or unset FF_MT5_SKIP_DOWNLOAD."
            )
    elif effective_start >= end_utc:
        _emit(log_cb, f"{pair} TICK already covers requested end — nothing to fetch")
        new_df = pd.DataFrame(columns=_TICK_SCHEMA)
    else:
        _emit(
            log_cb,
            f"-> MT5 {pair} ({symbol}) TICK  {effective_start.isoformat()} -> {end_utc.isoformat()}",
        )
        new_df = _fetch_window(
            mt5,
            symbol,
            effective_start,
            end_utc,
            log_cb=log_cb,
            cancel_cb=cancel_cb,
        )
        _emit(log_cb, f"  {len(new_df):,} new ticks from MT5")

    new_ticks = int(len(new_df))

    if existing is not None and not new_df.empty:
        existing["timestamp"] = pd.to_datetime(
            existing["timestamp"],
            utc=True,
            errors="coerce",
        )
        existing = existing.dropna(subset=["timestamp"])
        for c in new_df.columns:
            if c not in existing.columns:
                existing[c] = pd.NA
        existing = existing[new_df.columns]
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
        "new_ticks": new_ticks,
        "total_ticks": int(len(final_df)),
        "start_ts": (final_df["timestamp"].iloc[0].isoformat() if not final_df.empty else None),
        "end_ts": (final_df["timestamp"].iloc[-1].isoformat() if not final_df.empty else None),
    }
