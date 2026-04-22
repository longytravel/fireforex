"""MetaTrader 5 M1 history downloader for Fire Forex.

Mirror of ``ff.data.m1_bi5_downloader``: pulls per-pair M1 bars from a
connected MT5 terminal and writes the same
``[timestamp, open, high, low, close, volume, spread]`` parquet shape, so
``harness.run(..., data_source="mt5")`` can read MT5 bars interchangeably
with Dukascopy bars through ``ff.data.resample.derive_higher_tfs(...,
root=MT5_DATA_ROOT)``.

Separate root dir (``MT5_DATA_ROOT``) so the two sources never overwrite
each other — the whole point of the three-way reconcile is comparing them
side-by-side.

Windows-only (the ``MetaTrader5`` pip package is a MetaQuotes binary). On
dev boxes without MT5 installed, import still succeeds; ``download()``
raises on first use.

Spread handling: MT5 exposes ``rates.spread`` in *points* (broker-defined
smallest increment). On modern 5-digit and 3-digit-JPY brokers 1 pip = 10
points universally — same convention ``ff/live/runner.py:659`` applies to
live spread telemetry, so we divide by 10 here too.
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ff import harness
from . import inventory
from ..live import broker_mt5 as _bmt5


LogCallback = Callable[[str], None]
CancelCallback = Callable[[], bool]


# Sibling root to ``harness.DATA_ROOT``. Override via FF_MT5_DATA_ROOT.
# Defaulting to a sibling of the Dukascopy root keeps Google-Drive-synced
# users from mingling the two sources in one tree.
MT5_DATA_ROOT: Path = Path(
    os.environ.get(
        "FF_MT5_DATA_ROOT",
        str(harness.DATA_ROOT.parent / "BackTestData_MT5"),
    )
)


def _emit(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb is None:
        return
    try:
        log_cb(msg)
    except Exception:
        pass


def _target_path(pair: str) -> Path:
    return MT5_DATA_ROOT / f"{pair}_M1.parquet"


def _symbol_for(pair: str, symbol_map: dict[str, str] | None) -> str:
    """Broker-symbol lookup. IC Markets uses ``EURUSD.a`` for some pairs;
    a user-supplied ``symbol_map`` wins, else fall back to stripping the
    underscore (``EUR_USD`` → ``EURUSD``).
    """
    if symbol_map and pair in symbol_map:
        return symbol_map[pair]
    return pair.replace("_", "")


def _ensure_mt5_connected() -> Any:
    """Attach to a running MT5 terminal. Raises if the package isn't
    installed or no terminal is running.

    We don't pass login/password here — this is a history fetch, not a
    live broker connection. ``mt5.initialize()`` with no credentials
    attaches to an already-running terminal (the one the user has open
    for trading).
    """
    mt5 = _bmt5._require_mt5()
    if not mt5.initialize():
        err = mt5.last_error()
        raise RuntimeError(
            f"MT5 initialize() failed: {err}. "
            "Open MetaTrader 5 terminal and ensure it's logged in, then retry."
        )
    return mt5


def _fetch_window(
    mt5: Any, symbol: str, start_utc: datetime, end_utc: datetime,
) -> pd.DataFrame:
    """Pull M1 rates for [start_utc, end_utc] from MT5 and normalise to our
    parquet schema.

    ``copy_rates_range`` expects broker-local datetimes (seconds since
    broker epoch). In practice passing naive UTC datetimes yields results
    indexed in broker time, which we then shift to UTC via the same probe
    pattern ``MT5Broker.connect()`` uses.
    """
    rates = mt5.copy_rates_range(
        symbol, mt5.TIMEFRAME_M1, start_utc, end_utc,
    )
    if rates is None or len(rates) == 0:
        return pd.DataFrame(columns=[
            "timestamp", "open", "high", "low", "close", "volume", "spread",
        ])

    df = pd.DataFrame(rates)

    # Detect broker-UTC offset from any live tick on the same symbol, so
    # broker-local ``time`` becomes true UTC. The returned historical
    # ``time`` column is seconds since broker epoch — same convention.
    offset_sec = 0
    tick = mt5.symbol_info_tick(symbol)
    if tick is not None and tick.time:
        offset_sec = -(int(tick.time) - int(time.time()))

    df["timestamp"] = pd.to_datetime(
        df["time"].astype("int64") + int(offset_sec),
        unit="s", utc=True,
    )

    # Volume field is "tick_volume" in MT5 historical; fall back gracefully.
    if "tick_volume" in df.columns:
        df["volume"] = df["tick_volume"].astype("float64")
    elif "real_volume" in df.columns:
        df["volume"] = df["real_volume"].astype("float64")
    else:
        df["volume"] = 0.0

    # Spread: MT5 gives points → /10 = pips on 5-digit / 3-digit-JPY brokers.
    if "spread" in df.columns:
        df["spread"] = df["spread"].astype("float64") / 10.0
    else:
        df["spread"] = float("nan")

    return df[[
        "timestamp", "open", "high", "low", "close", "volume", "spread",
    ]].copy()


def _write_atomic(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".partial")
    df.to_parquet(tmp, index=False)
    if path.exists():
        path.unlink()
    tmp.replace(path)


def _load_existing(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception:
        return None
    if df.empty:
        return None
    # Normalise legacy DatetimeIndex shapes to timestamp-as-column.
    if isinstance(df.index, pd.DatetimeIndex):
        if df.index.name is None or df.index.name.lower() != "timestamp":
            df.index.name = "timestamp"
        df = df.reset_index()
    df.columns = [c.lower() for c in df.columns]
    return df


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
    """Download MT5 M1 candles into ``MT5_DATA_ROOT / {pair}_M1.parquet``.

    Matches ``ff.data.m1_bi5_downloader.download`` signature so
    ``ff.replay`` / ``harness.run`` can swap between Dukascopy and MT5
    with a single ``data_source`` flag.
    """
    path = _target_path(pair)
    symbol = _symbol_for(pair, symbol_map)

    start_utc = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    # Exclusive end of day so we don't double-count midnight.
    end_utc = (datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
               + timedelta(days=1))

    existing = _load_existing(path) if append else None

    if cancel_cb is not None and cancel_cb():
        _emit(log_cb, "cancelled before MT5 fetch")
        return {"path": str(path), "appended": False, "new_bars": 0,
                "total_bars": int(len(existing)) if existing is not None else 0,
                "start_ts": None, "end_ts": None}

    mt5 = _ensure_mt5_connected()
    _emit(log_cb, f"→ MT5 {pair} ({symbol}) M1  "
                  f"{start.isoformat()} → {end.isoformat()}")

    new_df = _fetch_window(mt5, symbol, start_utc, end_utc)
    _emit(log_cb, f"  {len(new_df):,} new M1 bars from MT5")

    new_bars = int(len(new_df))

    if existing is not None and not new_df.empty:
        if "spread" not in existing.columns:
            existing["spread"] = float("nan")
        # Keep the column set the new data produces.
        existing = existing[[c for c in new_df.columns if c in existing.columns]]
        for c in new_df.columns:
            if c not in existing.columns:
                existing[c] = pd.NA
        existing = existing[new_df.columns]
        existing["timestamp"] = pd.to_datetime(
            existing["timestamp"], utc=True, errors="coerce",
        )
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

    # Dukascopy path invalidates the inventory cache; mirror for the MT5
    # root if/when inventory gets taught about it. Currently no-op because
    # inventory scans DATA_ROOT only — safe to call, won't touch MT5 dir.
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
