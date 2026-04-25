"""Clip a timestamp-indexed DataFrame to a [start, end] UTC window.

Both bounds inclusive. Empty strings and ``None`` are treated as "no bound".
The caller is expected to pass already-loaded DataFrames from
``ff.harness.load_parquet`` (UTC-localized index).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Union

import pandas as pd

DateLike = Union[str, date, datetime, pd.Timestamp, None]


def _to_ts(value: DateLike, *, end_of_day: bool) -> pd.Timestamp | None:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        ts = pd.Timestamp(s)
    else:
        ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    # If user gave a bare date (midnight), treat end-of-day as 23:59:59.999999
    # so the end bound includes that full day's bars.
    if end_of_day and ts.hour == 0 and ts.minute == 0 and ts.second == 0 and ts.microsecond == 0:
        ts = ts + pd.Timedelta(hours=23, minutes=59, seconds=59, microseconds=999_999)
    return ts


def clip(df: pd.DataFrame, start: DateLike = None, end: DateLike = None) -> pd.DataFrame:
    """Return ``df.loc[start:end]`` with UTC normalisation of the bounds."""
    if df is None or df.empty:
        return df
    lo = _to_ts(start, end_of_day=False)
    hi = _to_ts(end, end_of_day=True)
    if lo is None and hi is None:
        return df
    return df.loc[lo:hi]
