"""Regression: signal-variant ints reshuffle across library builds; fingerprint
lookup by (family, params) must stay stable.

Context — docs/live/BUG-variant-id-not-stable-2026-04-22.md. Live runner
fired the wrong strategy because it stored bare `signal_variant=42` in the
deployed config and rebuilt the signal library with a different family
order at startup, mapping 42 to a different (family, params).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ff import schema as sc
from ff import signal_lib as sl


@pytest.fixture
def h1_df() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    n = 2000
    idx = pd.date_range("2020-01-01", periods=n, freq="1h", tz="UTC")
    close = 1.1 + np.cumsum(rng.normal(0, 0.0005, n))
    high = close + rng.uniform(0, 0.0008, n)
    low = close - rng.uniform(0, 0.0008, n)
    return pd.DataFrame({"high": high, "low": low, "close": close}, index=idx)


def _resolve(lib: sl.SignalLibrary, family: str, params: dict) -> int | None:
    for i, v in enumerate(lib.variant_map):
        if v.get("family") == family and v.get("params") == params:
            return i
    return None


def test_int_ids_differ_across_family_order(h1_df, monkeypatch):
    """Reversing the signals_cfg dict order changes the integer variant IDs
    for the same (family, params) fingerprint.
    """
    monkeypatch.setenv("FF_NO_CACHE", "1")

    cfg_a = {
        "ema_cross": {"fast": sc.IntRange(5, 15, step=5), "slow": sc.IntRange(20, 40, step=10)},
        "donchian": {"lookback": sc.IntRange(10, 30, step=10)},
    }
    cfg_b = {
        "donchian": {"lookback": sc.IntRange(10, 30, step=10)},
        "ema_cross": {"fast": sc.IntRange(5, 15, step=5), "slow": sc.IntRange(20, 40, step=10)},
    }

    lib_a = sl.build_signal_library(cfg_a, h1_df, pip_value=0.0001, atr_period=14, use_cache=False)
    lib_b = sl.build_signal_library(cfg_b, h1_df, pip_value=0.0001, atr_period=14, use_cache=False)

    target = ("donchian", {"lookback": 20})
    id_a = _resolve(lib_a, *target)
    id_b = _resolve(lib_b, *target)
    assert id_a is not None and id_b is not None, "donchian lookback=20 must exist in both libraries"
    assert id_a != id_b, (
        f"int IDs must differ across reversed family order — got {id_a}=={id_b}. "
        "If this ever passes it means the builder now sorts families, at which "
        "point the fingerprint-lookup workaround is no longer load-bearing."
    )


def test_fingerprint_resolves_to_same_family_across_builds(h1_df, monkeypatch):
    """Saving a fingerprint (signal_family, signal_params) and walking
    variant_map at replay time returns the variant pointing at the original
    (family, params) — regardless of how IDs were re-assigned.
    """
    monkeypatch.setenv("FF_NO_CACHE", "1")

    cfg_a = {
        "ema_cross": {"fast": sc.IntRange(5, 15, step=5), "slow": sc.IntRange(20, 40, step=10)},
        "donchian": {"lookback": sc.IntRange(10, 30, step=10)},
    }
    cfg_b = {
        "donchian": {"lookback": sc.IntRange(10, 30, step=10)},
        "ema_cross": {"fast": sc.IntRange(5, 15, step=5), "slow": sc.IntRange(20, 40, step=10)},
    }

    lib_a = sl.build_signal_library(cfg_a, h1_df, pip_value=0.0001, atr_period=14, use_cache=False)
    lib_b = sl.build_signal_library(cfg_b, h1_df, pip_value=0.0001, atr_period=14, use_cache=False)

    # Training picks donchian(20) under lib_a and saves the fingerprint.
    training_id = _resolve(lib_a, "donchian", {"lookback": 20})
    assert training_id is not None
    fingerprint = {
        "signal_family": lib_a.variant_map[training_id]["family"],
        "signal_params": lib_a.variant_map[training_id]["params"],
    }

    # Replay rebuilds the library (lib_b) with a different family order.
    # Resolving by fingerprint must still land on donchian(20).
    replay_id = _resolve(lib_b, fingerprint["signal_family"], fingerprint["signal_params"])
    assert replay_id is not None, "fingerprint must match under rebuilt library"
    assert lib_b.variant_map[replay_id]["family"] == "donchian"
    assert lib_b.variant_map[replay_id]["params"] == {"lookback": 20}


def test_fingerprint_mismatch_returns_none(h1_df, monkeypatch):
    """If the fingerprint references a (family, params) that isn't in the
    rebuilt library — because signals_cfg drifted — resolution returns None.
    Downstream is expected to raise / loudly log on None; this test pins the
    walker's contract.
    """
    monkeypatch.setenv("FF_NO_CACHE", "1")

    cfg = {"donchian": {"lookback": sc.IntRange(10, 30, step=10)}}
    lib = sl.build_signal_library(cfg, h1_df, pip_value=0.0001, atr_period=14, use_cache=False)

    # lookback=999 is not in the grid.
    assert _resolve(lib, "donchian", {"lookback": 999}) is None
    # family not in cfg at all.
    assert _resolve(lib, "ema_cross", {"fast": 5, "slow": 20}) is None
