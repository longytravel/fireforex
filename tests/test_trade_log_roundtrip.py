"""Trade-log round-trip test for the live-parity validator.

Runs a tiny backtest end-to-end and asserts the per-trade log written to
`artifacts/runs/*.npz` is internally consistent and matches the aggregate
metrics. This is the single guardrail that would catch a future refactor
silently desyncing `pnl_buffer` from `trade_records` in the Rust engine.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_ea(path: Path) -> dict:
    spec = importlib.util.spec_from_file_location("_tl_ea", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.EA


@pytest.mark.skipif(
    os.environ.get("FF_SKIP_GOLDEN") == "1"
    or not (ROOT / "eas" / "baseline.py").exists(),
    reason="trade-log test needs EUR_USD parquet + eas/baseline.py",
)
def test_trade_log_aggregate_parity_and_field_sanity():
    from ff import harness as hs

    ea_path = ROOT / "eas" / "baseline.py"
    ea = _load_ea(ea_path)

    hs.run(
        ea,
        layer_name="trade_log_parity",
        optimizer="random",
        seed=42,
        n_trials=5,
        open_browser=False,
    )

    run_dir = ROOT / "artifacts" / "runs"
    files = sorted(run_dir.glob("trade_log_parity_*.npz"),
                   key=lambda p: p.stat().st_mtime)
    assert files, "harness did not write an npz under trade_log_parity_*"
    npz_path = files[-1]
    z = np.load(npz_path, allow_pickle=True)

    assert "trades" in z.files, "npz missing the `trades` structured array"
    trades = z["trades"]

    # --- Structural assertions -------------------------------------------
    expected_fields = {
        "pnl_pips", "exit_reason", "direction",
        "entry_bar_index", "entry_sub_bar_index", "entry_price",
        "exit_bar_index", "exit_sub_bar_index", "exit_price",
        "entry_ts", "exit_ts",
    }
    assert set(trades.dtype.names) == expected_fields, (
        f"trade dtype fields mismatch: {trades.dtype.names}"
    )
    assert len(trades) > 0, "smoke-test produced zero trades — dataset or EA broken"

    # --- Aggregate-parity: trade pnl sum == pnl array sum ----------------
    pnl_aggregate = float(z["pnl"].sum())
    pnl_from_log = float(trades["pnl_pips"].sum())
    assert abs(pnl_aggregate - pnl_from_log) < 1e-6, (
        f"pnl drift: aggregate={pnl_aggregate!r}, trade-log={pnl_from_log!r}"
    )

    # --- Temporal sanity: exit_ts strictly after entry_ts ---------------
    entry_ts = pd.to_datetime(trades["entry_ts"])
    exit_ts = pd.to_datetime(trades["exit_ts"])
    assert (exit_ts >= entry_ts).all(), "exit_ts before entry_ts on some trade"

    # --- Direction values are ±1 ----------------------------------------
    directions = trades["direction"].astype(int)
    assert set(directions.tolist()).issubset({-1, 1}), (
        f"unexpected direction values: {set(directions.tolist())}"
    )

    # --- Exit reasons are in the documented code set --------------------
    # EXIT_NONE=0, EXIT_SL=1, EXIT_TP=2, EXIT_TRAILING=3, EXIT_BREAKEVEN=4,
    # EXIT_MAX_BARS=5, EXIT_STALE=6, EXIT_CHANDELIER=7
    valid_reasons = {0, 1, 2, 3, 4, 5, 6, 7}
    reasons = set(trades["exit_reason"].astype(int).tolist())
    assert reasons.issubset(valid_reasons), (
        f"unexpected exit_reason values: {reasons - valid_reasons}"
    )
