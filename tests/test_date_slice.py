"""Date-range clip helper + harness wiring."""

from __future__ import annotations

import pandas as pd

from ff.data.date_slice import clip


def _make_df(n: int = 10) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"close": list(range(n))}, index=idx)


def test_clip_both_bounds_inclusive():
    df = _make_df(10)
    out = clip(df, "2024-01-03", "2024-01-05")
    # End-of-day expansion means 01-05 includes the 05 bar.
    assert list(out["close"]) == [2, 3, 4]


def test_clip_none_returns_full():
    df = _make_df(5)
    out = clip(df, None, None)
    assert len(out) == 5


def test_clip_only_start():
    df = _make_df(5)
    out = clip(df, "2024-01-03", None)
    assert list(out["close"]) == [2, 3, 4]


def test_clip_only_end():
    df = _make_df(5)
    out = clip(df, None, "2024-01-02")
    assert list(out["close"]) == [0, 1]


def test_clip_empty_dataframe():
    df = pd.DataFrame()
    out = clip(df, "2024-01-01", "2024-01-02")
    assert len(out) == 0


def test_clip_preserves_timezone():
    df = _make_df(3)
    out = clip(df, "2024-01-02", "2024-01-02")
    assert str(out.index.tz) == "UTC"
    assert len(out) == 1


def test_clip_naive_string_treated_as_utc():
    df = _make_df(3)
    # Passing a bare date should align to UTC and match the first UTC bar.
    out = clip(df, "2024-01-02", "2024-01-02")
    assert len(out) == 1
