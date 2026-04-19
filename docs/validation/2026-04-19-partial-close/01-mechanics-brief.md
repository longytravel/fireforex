# Phase 1 — Partial close mechanics brief

## Definition

"Partial close" (a.k.a. scaling out, take-partial) closes a **fraction** of an open
position once the trade has moved a specified number of pips in the favourable
direction. The remainder of the position stays open and continues to run under
whatever exit logic is active (stop-loss, take-profit, trailing, breakeven).

It is a risk-management tool, not a directional signal. The theory:

- Locks in realised profit on part of the position before a reversal can take
  it back.
- Reduces emotional pressure on the runner, allowing the strategy to let
  winners run for the remaining fraction.
- Flattens the pnl distribution — fewer big winners and fewer scratches — at
  the cost of clipped upside.

## Parameters (standard retail conventions)

| Knob      | Fire Forex path                     | Units        | Typical retail range |
|-----------|-------------------------------------|--------------|----------------------|
| enabled   | `partial.test`                      | bool         | on / off             |
| pct       | `partial.when_on.pct`               | percent      | 20 – 75 %            |
| trigger   | `partial.when_on.trigger`           | pips         | 5 – (~SL × 0.8)      |

`pct` is the fraction of the position to close at the trigger. `trigger` is the
floating-pnl threshold, measured in pips **above entry for a long, below entry
for a short**, that gates the partial.

## How the exit price should be determined

In real retail trading a partial close is implemented as a **limit order**
sitting `trigger` pips above entry (for a long). When the market prints a
price at or beyond that level, the limit fills at **the limit price itself**
(possibly with a small positive slippage). It does *not* fill at some later
"end of bar" price.

That matters for a bar-based simulator. A naive implementation that only checks
`sb_close >= entry + trigger` will be conservative when the sub-bar briefly
spikes above trigger and closes lower — real trading would have filled at
the spike's first touch of trigger. Under-realisation is defensible as a
modelling choice; over-realisation (filling above trigger) is a bug.

## Unit checks

- `trigger` is in **pips**, not price units. Must be multiplied by `pip_value`
  (0.0001 on EURUSD, 0.01 on USDJPY) before comparing to price deltas.
- `pct` is a **percentage 20 – 75**, not a fraction 0.2 – 0.75. The engine
  must divide by 100 before use.
- `float_pnl_pips` should reflect the true floating pnl after entry spread and
  slippage are paid — otherwise a 5-pip trigger fires on a trade that is really
  break-even gross of spread.

## Spread and slippage

- **Longs** enter at the ask, so entry spread is paid at entry. A long partial
  exit happens at the bid; if the engine's price arrays are already bid, no
  additional spread cost is owed on exit.
- **Shorts** enter at the bid, so no entry spread is paid. A short partial
  exit happens at the ask; if arrays are bid, the engine must add spread to
  reach the ask on the closing fraction.
- Slippage on partial-close fills is typically modelled symmetrically with
  stop-loss fills — a fixed adverse pip cost applied to the exit price.

## Long / short asymmetry

| Concept                       | Long                            | Short                          |
|-------------------------------|---------------------------------|--------------------------------|
| Entry price                   | ask at entry bar                | bid at entry bar               |
| Trigger condition (floating)  | `high − entry ≥ trigger_pips`   | `entry − low  ≥ trigger_pips`  |
| Partial exit price reference  | bid (≈ sb_close if bid array)   | ask (= sb_close + spread)      |
| Remaining-position direction  | unchanged                       | unchanged                      |

A short-side sign flip is the canonical bug shape. Any engine implementation
must mirror the long formula with `actual_entry - exit` arithmetic, not copy
the long formula.

## Ordering against other exits within a bar

The single most important mechanics question: if a sub-bar hits **both** the
partial trigger **and** the final take-profit, which fires first?

- **Real trading**: limit orders sit in the book. If TP is placed inside the
  partial trigger distance (i.e. `tp < trigger`), the TP fills first — the
  partial never triggers because the trade is already closed. If
  `tp > trigger`, the partial fires at its limit first, then price continues
  to TP on the remainder.
- **Bar-based simulator**: the engine does not know the intra-sub-bar path.
  A common implementation checks one condition before the other in code
  order, which silently privileges whichever comes first. If partial is
  checked **before** TP and both conditions happen on the same sub-bar, the
  partial fires even if TP *should* have fired earlier. That over-realises
  profit on the partial and under-realises on the runner.

**A silent "partial fires after TP should have"** is the most likely hidden
bug for this knob, because the sampler can produce
`(trigger = 44, tp_rr = 0.21)` — i.e. trigger in pips well above the TP
distance in pips — and the combination is never flagged. The 78 % win rate
under this combination is exactly the shape a code-order bug would produce:
the partial realises profit at a price the trade should never have reached.

## Edge cases

- `pct = 100`: fully closes the trade at partial, equivalent to a synthetic
  TP at `trigger` pips. Remaining position is 0; any later exit check that
  uses `position_pct` should add nothing.
- `trigger < spread`: partial fires effectively immediately on a noisy entry.
  Typically clamped by the sampler's lower bound; verify.
- `trigger > sl_pips_total + spread`: partial can never fire because SL would
  close the whole trade before floating pnl could reach trigger. Waste of a
  sample, not a bug.
- `trigger > tp_pips`: partial can never fire *if* TP is checked first, or
  always fires if partial is checked first — this is the ordering question.
- Gap-through: if an H1 bar gaps through both trigger and TP at its open,
  the first sub-bar will satisfy both conditions; the same ordering question
  applies.

## What a correct partial-close implementation looks like

1. Schema slot exists, sampled, on/off group wired.
2. Encoding writes `pct` and `trigger` into the correct Rust param slots.
3. Rust reads the slots, interprets `pct` as a percentage (divides by 100)
   and `trigger` as pips (multiplies by `pip_value` only where necessary).
4. Trigger condition uses floating pnl in pips **after** entry costs.
5. Partial exit price uses the correct side (bid for long exits, ask for
   short exits) with slippage applied.
6. Ordering against TP is either: (a) TP checked first, or (b) the engine
   proves the partial-trigger price was reached before the TP price on the
   sub-bar path. Ordering by line number alone is **not** correct.
7. `position_pct` reduces by exactly `close_pct`. Subsequent exits scale by
   the reduced position. A once-only guard prevents double-firing.
8. Final reported pnl = realised partial + remainder-at-final-exit.

## Hypotheses to test

H1  (likeliest bug) — Partial is checked before TP within the sub-bar loop, so
     on a sub-bar that spans both trigger and tp, partial fires first and
     inflates realised pnl.
H2  (medium) — Trigger uses sb_high for the check but sb_close for the
     realisation. Conservative in isolation, but combined with H1 it
     asymmetrically favours the partial over the TP.
H3  (medium) — Sampler can produce `trigger > tp_rr × sl_pips`, which under
     H1 becomes the *source* of the 74.5 % win rate on tight-TP configs.
     Not a bug in partial itself but a sampling pathology that partial
     enables.
H4  (low) — Short-side spread cost deducted; long-side not. If arrays are
     bid, this is correct; if arrays are mid, both sides should be
     symmetric.

Phase 2 (code trace) answers H1 and H2 directly and narrows H3 and H4.
