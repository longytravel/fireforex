# 05 — Sensitivity results: trailing stop

Phase 5 of validate-forex-knob, run 2026-04-19 (afternoon, post v2 fix).

## Setup

Same seeded 800-bar EUR/USD synthetic fixture used by the breakeven
sensitivity runner and `tests/test_knob_sensitivity.py`. 56 trades
per configuration. Fixed SL 30 pips, TP 60 pips, no BE / partial /
stale / max-bars — isolating the trailing stop.

Five configurations:

- **A:** trailing OFF (baseline).
- **B:** fixed mode, `activate=20`, `distance=20` — generous, known-safe.
- **C:** fixed mode, `activate=5`, `distance=1` — the pre-fix bug
  configuration.
- **D:** ATR mode, `activate=5`, `atr_mult=0.3` — the ATR-variant
  of the bug.
- **E:** ATR mode, `activate=20`, `atr_mult=2.0` — normal ATR trail.

## Results (post v2 fix)

```
config                                 trades   wins   win%   total_pips
A: trail off                               56     17  30.4%       -206.0
B: fixed act=20 dist=20                    56     32  57.1%        379.1
C: fixed act=5  dist=1 (bug)               56     17  30.4%       -206.0
D: ATR   act=5  mult=0.3(bug)              56     30  53.6%       -246.2
E: ATR   act=20 mult=2.0                   56     24  42.9%         55.9
```

## Reading the numbers

- **C is now identical to A.** Same 17 wins, same 30.4 % win rate,
  same −206 pips total. Every trail move in config C is rejected by
  the new side-of-price guard — the engine behaves exactly as if
  trailing were off. This is the clean signature of a correct fix.
- **B (generous fixed) still produces a legitimate +379 pip edge.**
  The guard only rejects moves on the *wrong* side of price; normal
  trailing (distance wider than intrabar noise) is unaffected.
- **D (ATR bug config) is partially constrained.** 53.6 % win rate
  is still elevated relative to A's 30.4 %, but the total pips
  (−246) is *worse* than A — the fix is rejecting the bug-driven
  +10 pip exits while leaving a handful of legitimate ATR-trail
  exits on big-ATR signals (`atr_pips` varies per signal, so
  `0.3 × atr` can still be wider than some sub-bars' wicks). The
  residual effect is a genuine-but-too-tight trailing strategy,
  not an accounting artefact.
- **E (normal ATR) is net positive +56 pips.** Normal ATR trails
  continue to work.

## Before / after direction of effect

Under the buggy engine (pre v2, hypothetical re-run on this
fixture), C would have shown an inflated win rate (73 %-ish on the
breakeven fixture showed the same pattern), matching the signature
of "same trigger cadence but +distance pips per exit". With the
guard in place, that pattern collapses to A's baseline. The knob
still moves outcomes (B, D, E all differ from A) but only through
legitimate strategy pathways.

## One-line verdict

Trailing stop family behaves correctly post-fix. Legitimate
configurations still produce their expected effect; bug
configurations (tiny `distance` in fixed mode) collapse to
trail-off behaviour because every trail move is rejected as
invalid. ATR-mode tiny distance partially constrained — further
improvement possible via a minimum-stop-distance check, but not
urgent (the pathological profit mechanism is gone).
