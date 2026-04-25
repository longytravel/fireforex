"""Golden baseline regression test.

Runs the pinned reference EA with a fixed seed and asserts the key metrics
match the committed golden file. If any number shifts, either:

  (a) you INTENDED a behaviour change — update the golden with a written
      note in its ``_meta.description`` explaining why, or
  (b) you have a regression. Go find it.

This is the single most important guardrail in the project. Without it every
refactor is flying blind. See `artifacts/system_audit_report_2026-04-19.md`
for context.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden" / "complex01_seed42_500trials.json"


def _load_ea(path: Path) -> dict:
    spec = importlib.util.spec_from_file_location("_golden_ea", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.EA


@pytest.mark.skipif(
    os.environ.get("FF_SKIP_GOLDEN") == "1" or not (ROOT / "eas" / "complex01.py").exists(),
    reason="golden baseline run skipped (FF_SKIP_GOLDEN=1 or missing EA)",
)
@pytest.mark.slow
def test_golden_complex01_seed42_500trials():
    """Re-run the pinned sweep and compare against the golden file.

    Slow test (~1.5s end-to-end on EUR_USD data). Gated with a marker so
    fast unit-test runs can skip it via ``pytest -m 'not slow'``.
    """
    if not GOLDEN.exists():
        pytest.skip(f"golden file not found: {GOLDEN}")

    # Importing ff.harness pulls numpy, pandas, the engine, etc. Keep it local
    # so other tests in this directory don't pay the cost.
    from ff import harness as hs

    golden = json.loads(GOLDEN.read_text())
    ea_path = ROOT / golden["_meta"]["ea_path"]
    ea = _load_ea(ea_path)

    result = hs.run(
        ea,
        layer_name=golden["_meta"]["layer_name"],
        optimizer=golden["_meta"]["optimizer"],
        seed=golden["_meta"]["seed"],
        n_trials=golden["_meta"]["n_trials"],
        open_browser=False,
    )

    expected = golden["metrics"]
    # Integer / coarse asserts first — these catch structural changes quickly.
    assert int(result["trades"]) == expected["trades_best"], f"trades_best: expected {expected['trades_best']}, got {result['trades']}"
    assert abs(float(result["win_rate_pct"]) - expected["win_rate_pct"]) < 0.05, (
        f"win_rate_pct: expected {expected['win_rate_pct']}, got {result['win_rate_pct']:.4f}"
    )
    assert abs(int(round(result["total_pips"])) - expected["total_pips"]) <= 2, (
        f"total_pips: expected {expected['total_pips']}, got {result['total_pips']:.2f}"
    )
    assert abs(float(result["expectancy_pips"]) - expected["expectancy_pips_per_trade"]) < 0.02, (
        f"expectancy_pips: expected {expected['expectancy_pips_per_trade']}, got {result['expectancy_pips']:.4f}"
    )
    assert abs(float(result["max_dd_pct"]) - expected["max_drawdown_pct"]) < 0.2, (
        f"max_drawdown_pct: expected {expected['max_drawdown_pct']}, got {result['max_dd_pct']:.4f}"
    )
    assert abs(float(result["profit_factor"]) - expected["profit_factor"]) < 0.005, (
        f"profit_factor: expected {expected['profit_factor']}, got {result['profit_factor']:.4f}"
    )
