"""Smoke test that harness.run() emits the cost-realism columns."""

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("ff_core")

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_harness_npz_carries_cost_realism_keys(tmp_path):
    cost_table_path = REPO_ROOT / "artifacts" / "cost_table.json"
    if not cost_table_path.exists():
        pytest.skip("artifacts/cost_table.json missing — run scripts/build_cost_table.py first")

    ea_path = REPO_ROOT / "eas" / "complex01.py"
    if not ea_path.exists():
        pytest.skip("eas/complex01.py fixture missing")

    spec = importlib.util.spec_from_file_location("complex01_test_ea", ea_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ea = mod.EA

    from ff.harness import run as harness_run

    layer_name = f"costrealism_smoke_{tmp_path.name}"
    res = harness_run(
        ea,
        layer_name=layer_name,
        optimizer="random",
        seed=42,
        n_trials=20,
        open_browser=False,
    )
    assert res is not None
    assert res.get("run_file"), "harness did not return run_file path"

    run_file = Path(res["run_file"])
    assert run_file.exists(), f"NPZ not found at {run_file}"

    # Load without allow_pickle — uint8 / float64 / int64 arrays don't need it.
    # The structured array 'trades' does need it, so we use allow_pickle=True.
    npz = np.load(run_file, allow_pickle=True)

    assert "cost_realism_trades_json" in npz.files, "missing cost_realism_trades_json"
    assert "adjusted_pnl_total_pips" in npz.files, "missing adjusted_pnl_total_pips"
    assert "n_gated_trades" in npz.files, "missing n_gated_trades"

    assert np.isfinite(float(npz["adjusted_pnl_total_pips"])), "adjusted_pnl_total_pips is not finite"

    # Verify the JSON round-trips to a list of records.
    raw_bytes = bytes(npz["cost_realism_trades_json"].tobytes())
    records = json.loads(raw_bytes)
    assert isinstance(records, list), "cost_realism_trades_json did not decode to a list"
