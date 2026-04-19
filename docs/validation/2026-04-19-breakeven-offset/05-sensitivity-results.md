# 05 — Sensitivity results: breakeven.offset

Phase 5 of validate-forex-knob, run 2026-04-19.

## Setup

Same synthetic fixture as `tests/test_knob_sensitivity.py` — 800 H1
bars of drifting EUR/USD random walk, 60 M1 sub-bars per H1, seeded
ema_cross(5, 20) signals. 56 trades per configuration. Fixed SL 30
pips, fixed TP 60 pips, no trailing, no partial, no stale.

Three configurations run side-by-side via the one-off
`_sensitivity_runner.py` in this folder:

- **A:** BE off.
- **B:** BE on, trigger=5, offset=2 (safe normal case).
- **C:** BE on, trigger=5, offset=10 (the bug configuration).

## Results

```
config                             trades   wins   win%   total_pips
A: BE off                              56     17  30.4%       -206.0
B: BE trig=5 offset=2                  56     41  73.2%       -173.0
C: BE trig=5 offset=10                 56     41  73.2%        -25.0
```

## Reading the numbers

- **A vs B/C:** win rate jumps from 30 % to 73 %. The knob clearly
  moves outcomes. *Silent no-op failure mode is ruled out.*
- **B vs C:** **same number of BE-triggered wins, but C produces 148
  more pips of total profit than B.** Same 41 wins in both, so the
  extra pips are not from extra winning trades — they are from
  *bigger* wins per BE fire.
- The per-BE-fire win is ~+2 pips in B and ~+10 pips in C, matching
  the expected arithmetic from the behaviour table.

## Direction-of-effect check

The mechanics brief predicted that once `offset > trigger`, the
engine would exit for `+offset` pips regardless of whether the trade
"deserved" the win. The sensitivity run confirms this at sweep
scale: the number of BE-triggered wins is fixed by the trigger
condition (same in B and C), but the size of each BE-triggered
exit scales with `offset` without any side-of-price guard.

**In plain English:** the larger you set `offset`, the more pips
the engine hands you per BE-triggered exit. Under the fixed fill
model (exact-stop with no side-of-price guard), the optimiser has
every incentive to push `offset` to the maximum allowed (+10 pips)
and pair it with a small `trigger` (+5 pips). That is exactly the
winning trial observed on 2026-04-19: `trigger = 5.024,
offset = 9.920`.

## One-line verdict

The knob moves outcomes, *and moves them unexpectedly*: BE-triggered
exits scale linearly with `offset` with no physical basis. This
matches Phase 1 prediction (2) — *accept and fire on same sub-bar*
— confirmed by the Phase 2 code trace and the Phase 4 micro-test.

## Why both B and C have the same win rate

Even the "safe" configuration (B, offset=2) is selling the trade's
optionality. Once BE fires, the SL sits just above entry, and the
next sub-bar's low almost always reaches it. Trades that would
otherwise have run to full TP are cut short at +2 pips. B is a
*suboptimal* BE strategy in this fixture, but **not an invalid
one** — the arithmetic is correct, spreads are being respected
(ish), and the +2 pip wins are real gains from the price path.

C shares the same trigger pattern, so it generates exits at the
same cadence, but each exit pays out +10 pips instead of +2.
Nothing about the market justifies the extra 8 pips per trade —
they are accounting artefacts created by the missing guard.
