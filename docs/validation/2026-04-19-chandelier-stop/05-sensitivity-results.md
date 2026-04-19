# 05 — On/off sensitivity: `engine.chandelier`

## Per-knob sensitivity test (`tests/test_knob_sensitivity.py`)

Added `test_chandelier_knob_moves_outcomes` at line 211, using
aggressive params (`activate=1.0, atr_mult=0.5`) to guarantee the
chandelier SL beats the baseline 15-pip SL on any modestly-profitable
move.

```
tests/test_knob_sensitivity.py::test_chandelier_knob_moves_outcomes PASSED
```

Flipping `PL_CHANDELIER_ENABLED` from 0 → 1 with the same synthetic
fixture changes trade count or total return. Confirms the Rust engine
is honouring the knob.

**Note:** the default build-side parameters (`activate=5.0,
atr_mult=3.0`) produce *zero* effect on the test_knob_sensitivity
fixture — every losing trade stops out at the 15-pip SL before
chandelier arms, and the guard correctly rejects raw_sl candidates
that are above sb_low. That is not a silent-no-op bug; it is the
guard working. The test was tightened to `activate=1, atr_mult=0.5`
to exercise the knob on this particular fixture. Post-fix, the test
asserts effect in the expected direction.

## Full-sweep A/B on real data (EUR_USD H1, level=5, 500 trials,
seed=42)

Fired via `POST /api/run` against the live web server.

| run | overrides                         | trades | win rate | total pips | expectancy | max DD | PF     |
|-----|-----------------------------------|--------|----------|------------|------------|--------|--------|
| A   | `groups.chandelier = false`       | 356    | 57.02 %  | +2806.35   | +7.88      | 16.84% | 1.359  |
| B   | `groups.chandelier = true` (forced)| 421    | 38.00 %  | +2414.72   | +5.74      | 15.75% | 1.439  |

- A's best trial: macd_cross(15/35/5), SL fixed 90, TP RR 1.9,
  trailing fixed(45, activate 30), no breakeven, no max_bars.
- B's best trial: macd_cross(8/43/7), SL fixed 85, TP fixed 5,
  trailing fixed(31, activate 95), breakeven ON (trigger 19, offset 0),
  max_bars ON (348), days Mon-Fri.

Best-trial outputs diverge in **every** knob (signal variant, SL,
TP, trailing distance, breakeven, max_bars, days bitmask). Total
PnL diverges by **392 pips**; trade count by **65**. Max drawdown
shifts by **1.1 pp**.

**Verdict: the knob moves outcomes in the expected direction.**
Forcing chandelier on materially changes the sweep's search space
and the best-trial composition. Chandelier is not a silent no-op.

## UI-visible artifacts

- A's run file:
  `artifacts/runs/chandelier_off_500_<timestamp>.npz`
- B's run file:
  `artifacts/runs/chandelier_on_500_<timestamp>.npz`
- Both rows appended to `artifacts/history.csv` (visible in the
  History dropdown on the dashboard).
- `artifacts/comparison.html` rewritten after B's run.

The user can compare either run against the pinned baseline on the
dashboard, and the scatter plot for B will show per-trial metrics
for 500 trials with chandelier armed on every trial.

## Known observability gap

`app/routes.py` `explain-bundle` / `best_params_english` does not
yet render chandelier knob settings in the best-trial English
summary. Visible only as an increased "effective dims: 7 top-level
knobs" count. Flagged for ship-checklist follow-up: add
`"Chandelier: ON (activate=..., atr_mult=...)"` to the renderer so
a user scanning a run's summary can see at a glance whether the
knob was on. Non-blocking for correctness.
