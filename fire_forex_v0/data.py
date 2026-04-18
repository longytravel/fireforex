from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

CANDIDATE_PATHS = [
    r"G:\My Drive\BackTestData\EUR_USD_M1.parquet",
    r"G:\My Drive\ForexPipeline\parquet\EURUSD_2025_full\v1\market-data.parquet",
    r"G:\My Drive\ForexPipeline\EURUSD_M1_chunks\EURUSD_M1_2024.parquet",
]


def _find_default_path() -> str:
    for p in CANDIDATE_PATHS:
        if Path(p).exists():
            return p
    raise FileNotFoundError(
        "No EUR/USD M1 parquet found. Tried:\n  "
        + "\n  ".join(CANDIDATE_PATHS)
        + "\nPass an explicit --data <path>."
    )


# Probe the schema and return (timestamp_col, open_col, high_col, low_col, close_col, volume_col|None).
# Dukascopy-style files may store bid OHLC, ask OHLC, or mid. We prefer mid; fall back to bid.
def _resolve_columns(schema_names: list[str]) -> dict[str, str | None]:
    lower = {n.lower(): n for n in schema_names}

    # Timestamp
    for key in ("timestamp", "time", "datetime", "date", "t"):
        if key in lower:
            ts = lower[key]
            break
    else:
        ts = None  # will use the index if none found

    def pick(*keys: str) -> str | None:
        for k in keys:
            if k in lower:
                return lower[k]
        return None

    # Try mid first, then bid, then plain
    mapping = {
        "timestamp": ts,
        "open": pick("open", "mid_open", "bid_open", "o"),
        "high": pick("high", "mid_high", "bid_high", "h"),
        "low": pick("low", "mid_low", "bid_low", "l"),
        "close": pick("close", "mid_close", "bid_close", "c"),
        "volume": pick("volume", "vol", "tick_volume", "v"),
    }
    missing = [k for k in ("open", "high", "low", "close") if mapping[k] is None]
    if missing:
        raise ValueError(
            f"Parquet schema is missing OHLC columns: {missing}. "
            f"Available: {schema_names}"
        )
    return mapping


def load_ohlc(
    path: str | None = None,
    start: str | None = None,
    end: str | None = None,
    max_rows: int | None = None,
) -> pd.DataFrame:
    """Load M1 OHLC into a DatetimeIndex'd DataFrame with columns open/high/low/close[/volume].

    - `start`/`end` are ISO strings, inclusive.
    - `max_rows` caps row count (handy for quick smoke tests).
    """
    if path is None:
        path = _find_default_path()
    path = str(path)

    pf = pq.ParquetFile(path)
    schema_names = [f.name for f in pf.schema_arrow]
    cols = _resolve_columns(schema_names)

    read_cols = [c for c in cols.values() if c is not None]
    tbl = pq.read_table(path, columns=read_cols)
    df = tbl.to_pandas()

    # Build index
    ts_col = cols["timestamp"]
    if ts_col is not None and ts_col in df.columns:
        df[ts_col] = pd.to_datetime(df[ts_col], utc=True, errors="coerce")
        df = df.set_index(ts_col).sort_index()

    # Rename to canonical
    rename = {
        cols["open"]: "open",
        cols["high"]: "high",
        cols["low"]: "low",
        cols["close"]: "close",
    }
    if cols["volume"]:
        rename[cols["volume"]] = "volume"
    df = df.rename(columns=rename)

    keep = ["open", "high", "low", "close"] + (["volume"] if "volume" in df.columns else [])
    df = df[keep].dropna(subset=["open", "high", "low", "close"])

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")

    if start:
        df = df.loc[df.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df.loc[df.index <= pd.Timestamp(end, tz="UTC")]
    if max_rows:
        df = df.iloc[-max_rows:]

    return df


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else None
    df = load_ohlc(path=path, max_rows=5)
    print(f"loaded {len(df):,} rows (showing last 5)")
    print(df.tail())
    print("dtypes:", df.dtypes.to_dict())
    print("tz:", df.index.tz)
