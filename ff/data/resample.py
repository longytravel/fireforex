"""Roll-up utilities for the Claude-Backtester data model.

Two public functions:

- ``tick_to_m1(pair)`` — read ``{pair}_TICK.parquet``, aggregate to
  1-minute OHLCV+spread using mid-price ``= (bid + ask) / 2``.
- ``derive_higher_tfs(pair, source_tf, targets)`` — read
  ``{pair}_{source_tf}.parquet`` and resample upwards to every target TF.

Files are written atomically (``.partial`` + rename) using the same
pattern as :mod:`ff.data.downloader`.

Aggregation rules (forex-correct OHLCV):

    open   : first
    high   : max
    low    : min
    close  : last
    volume : sum (if column present)
    spread : mean (if column present)
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pandas as pd

from ff import harness

from . import inventory

# Map our internal TF tokens to the pandas offset alias used by Grouper.
_TF_TO_OFFSET: dict[str, str] = {
    "M1": "1min",
    "M5": "5min",
    "M15": "15min",
    "M30": "30min",
    "H1": "1h",
    "H4": "4h",
    "D": "1D",
    "W": "1W",
}

_DEFAULT_TARGETS: tuple[str, ...] = ("M5", "M15", "M30", "H1", "H4", "D", "W")


# ── helpers ────────────────────────────────────────────────────────────────


def _source_path(pair: str, tf: str, root: Path) -> Path:
    return root / f"{pair}_{tf}.parquet"


def _write_atomic(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".partial")
    df.to_parquet(tmp, index=False)
    if path.exists():
        path.unlink()
    tmp.replace(path)


def _merge_with_existing(target: Path, new_df: pd.DataFrame) -> pd.DataFrame:
    """Merge ``new_df`` into whatever already lives at ``target``.

    Critical for the "fetch a 1-yr tick window, derive M1, derive higher TFs"
    pipeline: without this, deriving from a 1-year M1 wipes out any prior
    19-year M1 history. New rows win on overlap; everything outside the new
    range is preserved.
    """
    if not target.exists():
        return new_df
    try:
        existing = pd.read_parquet(target)
    except Exception:
        return new_df  # corrupted target — let the new write replace it
    if existing.empty:
        return new_df

    # Legacy files store timestamp as a DatetimeIndex (downloader.py output);
    # new files store it as a column. Normalise to a column so the merge +
    # drop_duplicates path below works for both shapes.
    if isinstance(existing.index, pd.DatetimeIndex):
        if existing.index.name is None or existing.index.name.lower() != "timestamp":
            existing.index.name = "timestamp"
        existing = existing.reset_index()
    existing.columns = [c.lower() for c in existing.columns]
    if "timestamp" not in existing.columns:
        return new_df
    existing["timestamp"] = pd.to_datetime(existing["timestamp"], utc=True, errors="coerce")
    existing = existing.dropna(subset=["timestamp"])

    # Schema reconciliation — older M1 files might lack a 'spread' column.
    # Pad missing columns with NaN so the concat keeps shape.
    for col in new_df.columns:
        if col not in existing.columns:
            existing[col] = pd.NA
    for col in existing.columns:
        if col not in new_df.columns:
            new_df[col] = pd.NA

    combined = pd.concat([existing[new_df.columns], new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["timestamp"], keep="last")
    combined = combined.sort_values("timestamp").reset_index(drop=True)
    return combined


def _read_bars(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    # Legacy files store timestamp as a DatetimeIndex (downloader.py output);
    # new files store it as a column. Handle both shapes.
    if isinstance(df.index, pd.DatetimeIndex):
        if df.index.name is None or df.index.name.lower() != "timestamp":
            df.index.name = "timestamp"
        df = df.reset_index()
    df.columns = [c.lower() for c in df.columns]
    if "timestamp" not in df.columns:
        raise ValueError(f"{path.name}: missing 'timestamp' column")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    return df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def _resample_bars(df: pd.DataFrame, offset: str) -> pd.DataFrame:
    """OHLCV+spread resample. Left-labelled, left-closed, origin=start_day."""
    idx = df.set_index("timestamp")
    agg: dict[str, str] = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in idx.columns:
        agg["volume"] = "sum"
    if "spread" in idx.columns:
        agg["spread"] = "mean"
    out = idx.resample(offset, label="left", closed="left", origin="start_day").agg(agg).dropna(subset=["open"])
    return out.reset_index()


# ── public API ─────────────────────────────────────────────────────────────


def tick_to_m1(pair: str, root: Path | None = None) -> Path:
    """Aggregate ``{pair}_TICK.parquet`` to 1-minute OHLCV.

    Mid-price is ``(bid + ask) / 2``. Spread is ``ask - bid`` averaged
    over the minute. Volume sums bid+ask volume if those columns are
    present.

    Returns the written path (``{pair}_M1.parquet``).
    """
    if root is None:
        root = harness.DATA_ROOT
    src = root / f"{pair}_TICK.parquet"
    if not src.exists():
        raise FileNotFoundError(f"no tick file: {src}")

    df = pd.read_parquet(src)
    df.columns = [c.lower() for c in df.columns]
    if "timestamp" not in df.columns:
        raise ValueError(f"{src.name}: missing 'timestamp' column")
    for c in ("bid", "ask"):
        if c not in df.columns:
            raise ValueError(f"{src.name}: missing '{c}' column")

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp", "bid", "ask"])
    df["mid"] = (df["bid"] + df["ask"]) * 0.5
    df["spread"] = df["ask"] - df["bid"]

    idx = df.set_index("timestamp")
    agg: dict[str, str] = {"mid": ["first", "max", "min", "last"], "spread": "mean"}
    if "bid_volume" in idx.columns and "ask_volume" in idx.columns:
        idx["volume"] = idx["bid_volume"] + idx["ask_volume"]
        agg["volume"] = "sum"
    out = idx.resample("1min", label="left", closed="left", origin="start_day").agg(agg)
    out.columns = ["_".join([c for c in col if c]).strip("_") for col in out.columns.to_flat_index()]
    out = out.rename(
        columns={
            "mid_first": "open",
            "mid_max": "high",
            "mid_min": "low",
            "mid_last": "close",
            "spread_mean": "spread",
            "volume_sum": "volume",
        }
    )
    out = out.dropna(subset=["open"]).reset_index()
    keep = [c for c in ("timestamp", "open", "high", "low", "close", "volume", "spread") if c in out.columns]
    out = out[keep]

    target = root / f"{pair}_M1.parquet"
    merged = _merge_with_existing(target, out)
    _write_atomic(target, merged)
    inventory.invalidate()
    return target


def derive_higher_tfs(
    pair: str,
    source_tf: str = "M1",
    targets: Sequence[str] = _DEFAULT_TARGETS,
    root: Path | None = None,
) -> list[Path]:
    """Resample ``{pair}_{source_tf}.parquet`` up to each target TF.

    Each target writes ``{pair}_{tf}.parquet`` atomically. The source
    TF itself is silently skipped if it appears in ``targets`` (you
    don't derive M1 from M1).

    Returns the list of files written (in the order of ``targets``).
    """
    if root is None:
        root = harness.DATA_ROOT
    src_path = _source_path(pair, source_tf, root)
    if not src_path.exists():
        raise FileNotFoundError(f"no source bars: {src_path}")
    if source_tf not in _TF_TO_OFFSET:
        raise ValueError(f"unknown source_tf: {source_tf}")

    df = _read_bars(src_path)
    written: list[Path] = []
    for tf in targets:
        if tf == source_tf:
            continue
        if tf not in _TF_TO_OFFSET:
            raise ValueError(f"unknown target tf: {tf}")
        out = _resample_bars(df, _TF_TO_OFFSET[tf])
        if out.empty:
            continue
        target = root / f"{pair}_{tf}.parquet"
        merged = _merge_with_existing(target, out)
        _write_atomic(target, merged)
        written.append(target)
    if written:
        inventory.invalidate()
    return written
