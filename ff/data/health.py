"""Per-file parquet health checks for the Data tab.

Runs four checks on a loaded DataFrame:

1. NaN counts in OHLC and spread.
2. OHLC sanity: ``high >= max(open, close)`` / ``low <= min(open, close)``
   / ``high >= low``.
3. Timestamp ordering: strictly increasing, no duplicates.
4. Gap detection: flags bar-to-bar gaps > 2× the expected bar delta, *excluding*
   the FX weekend window (Fri 22:00 UTC → Sun 22:00 UTC).

Returns a structured report with a roll-up summary ``ok | warn | fail``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ff import harness


# Expected bar duration per timeframe, in minutes.
_TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30,
               "H1": 60, "H4": 240, "D": 1440, "W": 10080}


def _ns(index: pd.DatetimeIndex) -> np.ndarray:
    """Return a UTC-nanosecond int64 view regardless of pandas version.

    Pandas 3.0 changed ``asi8`` from ns to µs; this helper pins the unit.
    """
    if index.tz is not None:
        index = index.tz_convert("UTC").tz_localize(None)
    return index.to_numpy().astype("datetime64[ns]").view("i8")


def _weekend_mask(lo_ns: np.ndarray, hi_ns: np.ndarray) -> np.ndarray:
    """True where the interval (lo, hi] contains any Saturday (UTC).

    This is the pragmatic FX-weekend rule: any gap that straddles a Saturday
    is treated as the normal Fri-close → Sun-open window and suppressed.
    """
    lo = pd.DatetimeIndex(pd.to_datetime(lo_ns, utc=True, unit="ns"))
    hi = pd.DatetimeIndex(pd.to_datetime(hi_ns, utc=True, unit="ns"))
    days_ahead = (5 - np.asarray(lo.weekday)) % 7
    next_sat = lo.normalize() + pd.to_timedelta(days_ahead, unit="D")
    return np.asarray(next_sat <= hi)


def _ohlc_sanity(df: pd.DataFrame) -> dict[str, int]:
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    o = df["open"].to_numpy()
    c = df["close"].to_numpy()
    return {
        "high_lt_low": int((h < l).sum()),
        "high_lt_open": int((h < o).sum()),
        "high_lt_close": int((h < c).sum()),
        "low_gt_open": int((l > o).sum()),
        "low_gt_close": int((l > c).sum()),
    }


def _nan_counts(df: pd.DataFrame) -> dict[str, int]:
    cols = [c for c in ("open", "high", "low", "close", "volume", "spread") if c in df.columns]
    return {c: int(df[c].isna().sum()) for c in cols}


def _timestamp_issues(index: pd.DatetimeIndex) -> dict[str, int]:
    ns = _ns(index)
    deltas = np.diff(ns)
    return {
        "duplicates": int((deltas == 0).sum()),
        "non_monotonic": int((deltas < 0).sum()),
    }


def _gap_samples(index: pd.DatetimeIndex, tf: str, limit: int = 20) -> list[dict[str, Any]]:
    expected_min = _TF_MINUTES.get(tf, 0)
    if expected_min <= 0 or len(index) < 2:
        return []
    ns = _ns(index)
    expected_ns = int(expected_min * 60 * 1_000_000_000)
    deltas = np.diff(ns)
    # Gaps > 2× expected, excluding the weekend window.
    big = np.where(deltas > 2 * expected_ns)[0]
    if big.size == 0:
        return []
    weekend = _weekend_mask(ns[big], ns[big + 1])
    real = big[~weekend]
    out: list[dict[str, Any]] = []
    for i in real[:limit]:
        out.append({
            "from": pd.Timestamp(ns[i], tz="UTC").isoformat(),
            "to": pd.Timestamp(ns[i + 1], tz="UTC").isoformat(),
            "gap_minutes": int((ns[i + 1] - ns[i]) / 60_000_000_000),
        })
    return out + ([{"_more": int(real.size - limit)}] if real.size > limit else [])


def _spread_sanity(df: pd.DataFrame) -> dict[str, Any]:
    if "spread" not in df.columns:
        return {"present": False}
    s = df["spread"].to_numpy()
    return {
        "present": True,
        "negatives": int((s < 0).sum()),
        "nonzero_share": float(np.mean(s > 0)) if len(s) else 0.0,
    }


def _roll_up(counts: dict[str, int], ts: dict[str, int],
             gaps: list[dict[str, Any]], ohlc: dict[str, int],
             spread: dict[str, Any]) -> str:
    nan_total = sum(v for v in counts.values())
    ohlc_total = sum(v for v in ohlc.values())
    if ts["non_monotonic"] > 0 or ohlc_total > 0:
        return "fail"
    if nan_total > 0 or ts["duplicates"] > 0 or len(gaps) > 0 or \
       (spread.get("present") and spread.get("negatives", 0) > 0):
        return "warn"
    return "ok"


def check(pair: str, tf: str) -> dict[str, Any]:
    """Run health checks on the parquet file for ``pair`` / ``tf``."""
    path = harness.DATA_ROOT / f"{pair}_{tf}.parquet"
    if not path.exists():
        return {"pair": pair, "tf": tf, "summary": "missing",
                "error": f"file not found: {path}"}

    df = harness.load_parquet(path)
    nans = _nan_counts(df)
    ohlc = _ohlc_sanity(df)
    ts = _timestamp_issues(df.index)
    gaps = _gap_samples(df.index, tf)
    spread = _spread_sanity(df)

    summary = _roll_up(nans, ts, gaps, ohlc, spread)
    return {
        "pair": pair,
        "tf": tf,
        "summary": summary,
        "bars": int(len(df)),
        "range": {
            "start": df.index.min().isoformat() if len(df) else None,
            "end": df.index.max().isoformat() if len(df) else None,
        },
        "nan_counts": nans,
        "ohlc_violations": ohlc,
        "timestamp_issues": ts,
        "spread": spread,
        "gap_samples": gaps,
    }
