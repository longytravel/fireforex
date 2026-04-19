# Phase 5 — Partial close on / off sensitivity

## Test

`tests/test_knob_sensitivity.py::test_partial_close_knob_moves_outcomes`
already existed (added 2026-04-19 alongside the EXEC_BASIC rescue work).
It runs the reference dataset twice — once with
`partial_enabled=0, pct=0, trigger=0`, once with
`partial_enabled=1, pct=50, trigger=5` — and asserts that the off-run and
on-run produce materially different PnL / trade counts.

## Result

```
tests/test_knob_sensitivity.py::test_partial_close_knob_moves_outcomes PASSED
```

The partial close knob **moves outcomes in the expected direction**. The
test passes cleanly against the current (pre-fix) engine, which rules out
the EXEC_BASIC-shape silent no-op failure mode for this knob.

## What the sensitivity test does NOT catch

The sensitivity test only asserts that flipping the knob changes
outcomes. It does **not** check that the direction or magnitude of that
change is physically correct. Both bugs identified in Phase 2 /
Phase 3 / Phase 4 —

- Bug A: realisation at sb_close rather than at the trigger price
- Bug B: partial firing before TP when trigger lies beyond tp

— pass the sensitivity check without objection. That is the core reason
the Phase 4 micro-test exists: it is the only artefact in the test suite
that asserts partial close produces the **correct** pnl for specific
hand-calculated scenarios.

## Verdict for Phase 5

Knob is wired and responsive. No silent no-op. All downstream verdict
weight rests on the Phase 4 micro-test evidence, which is that the
on-path arithmetic is wrong in two distinct ways.
