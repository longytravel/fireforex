"""Tests for ``pick_best``'s ``objective_array`` override (Option C).

The IC-realistic adjusted-P&L proxy lives outside the Rust-emitted
``metrics_out`` matrix (adding a column would break the
NUM_METRICS contract with ``ff_core``). ``pick_best`` instead accepts
a synthetic per-trial array. These tests pin its semantics:

- override path ranks by the synthetic array (higher is better);
- tie-breakers still come from ``metrics_out``;
- the profitability filter still gates losers out;
- ranking flips correctly as the per-trade charge grows.
"""

from __future__ import annotations

import numpy as np

from ff.harness import METRIC_COLUMNS, METRIC_INDEX, pick_best


def _zero_metrics(n_trials: int) -> np.ndarray:
    """Return an (n_trials, NUM_METRICS) array of zeros."""
    return np.zeros((n_trials, len(METRIC_COLUMNS)), dtype=np.float64)


def _set(metrics: np.ndarray, row: int, **kwargs) -> None:
    for key, val in kwargs.items():
        metrics[row, METRIC_INDEX[key]] = val


def test_objective_array_ranks_high_n_trades_when_charge_is_small() -> None:
    """Trial A: 200 trades × +2.5 pips/trade = +500 pips total.
    Trial B: 100 trades × +4.0 pips/trade = +400 pips total.
    With charge = -0.1 pips/trade, A's adjusted = 500 - 20 = 480, B's = 400 - 10 = 390.
    A still wins.
    """
    m = _zero_metrics(2)
    _set(m, 0, trades=200, expectancy_pips=2.5, return_pct=20.0)
    _set(m, 1, trades=100, expectancy_pips=4.0, return_pct=18.0)
    charge = -0.1
    proxy = m[:, METRIC_INDEX["trades"]] * (m[:, METRIC_INDEX["expectancy_pips"]] + charge)
    best = pick_best(m, objective_array=proxy)
    assert best == 0


def test_objective_array_ranks_low_n_trades_when_charge_is_large() -> None:
    """Same trials, but charge = -1.5 pips/trade.
    A's adjusted = 200*(2.5-1.5) = 200; B's = 100*(4.0-1.5) = 250. B wins.
    """
    m = _zero_metrics(2)
    _set(m, 0, trades=200, expectancy_pips=2.5, return_pct=20.0)
    _set(m, 1, trades=100, expectancy_pips=4.0, return_pct=18.0)
    charge = -1.5
    proxy = m[:, METRIC_INDEX["trades"]] * (m[:, METRIC_INDEX["expectancy_pips"]] + charge)
    best = pick_best(m, objective_array=proxy)
    assert best == 1


def test_objective_array_higher_is_always_better() -> None:
    """No ``LOWER_IS_BETTER`` lookup applies to the override path —
    the synthetic array is always ranked argmax."""
    m = _zero_metrics(3)
    _set(m, 0, trades=10, return_pct=5.0)
    _set(m, 1, trades=10, return_pct=5.0)
    _set(m, 2, trades=10, return_pct=5.0)
    proxy = np.array([1.0, 5.0, 3.0])
    best = pick_best(m, objective_array=proxy)
    assert best == 1


def test_objective_array_tie_break_uses_metrics_out() -> None:
    """When two trials tie on the synthetic objective, tie-breakers fall
    back to columns in ``metrics_out`` (here, return_pct then trades)."""
    m = _zero_metrics(3)
    _set(m, 0, trades=50, return_pct=5.0)
    _set(m, 1, trades=80, return_pct=8.0)  # higher return_pct → wins tie-break
    _set(m, 2, trades=80, return_pct=6.0)
    proxy = np.array([10.0, 100.0, 100.0])
    best = pick_best(m, objective_array=proxy, tie_break=("return_pct", "trades"))
    assert best == 1


def test_objective_array_profitability_filter_still_applies() -> None:
    """``require_profitable`` still drops trials with return_pct ≤ 0,
    even when the synthetic objective ranks them first."""
    m = _zero_metrics(2)
    _set(m, 0, trades=10, return_pct=-5.0, profit_factor=0.5)  # losing
    _set(m, 1, trades=10, return_pct=2.0, profit_factor=1.2)
    proxy = np.array([100.0, 1.0])  # losing trial has higher proxy
    best = pick_best(m, objective_array=proxy)
    assert best == 1


def test_objective_array_length_mismatch_raises() -> None:
    m = _zero_metrics(3)
    proxy = np.array([1.0, 2.0])
    try:
        pick_best(m, objective_array=proxy)
    except ValueError as exc:
        assert "objective_array length" in str(exc)
    else:
        raise AssertionError("expected ValueError on length mismatch")
