# 03 — Expected-behaviour table: breakeven.offset

Phase 3 of validate-forex-knob, run 2026-04-19.

## Plain-English summary

We are going to invent six specific trading scenarios, work out **what
we think the engine will do** in each one based on the code trace, and
then (in Phase 4) actually run the engine to see if it matches.

Three of the scenarios exercise the main bug (the SL-above-price
problem). Two cover the secondary wrinkle (breakeven firing on a
brief price spike that immediately reverses). One is a sanity-check
scenario that should work correctly.

If the engine matches our predictions, we know exactly what is
broken. If the engine does *not* match our predictions, either we
read the code wrong or the engine has an even bigger surprise. Either
answer is useful.

## Fixture assumptions

- **Pair:** EURUSD, `pip_value = 0.0001`.
- **Single-series OHLC** (no bid / ask book), spread set to 0 for
  clarity — spread effects are tested separately.
- **Slippage = 0** for clarity.
- **Original SL (at entry):** 30 pips behind entry — far enough that
  no scenario stops out on the original SL before the BE logic runs.
- **Original TP (at entry):** 60 pips ahead — far enough that no
  scenario hits TP before BE logic runs.
- **Bar resolution:** H1 main bars, M1 sub-bars. The BE logic works on
  sub-bars with a one-sub-bar deferred apply.

## The scenarios

### Row 1 — Plain vanilla (sanity check, should behave correctly)

- **Setup:** long, entry = 1.10000. SL = 1.09970, TP = 1.10060.
- **Knobs:** `trigger = 5, offset = 2`.
- **Story:** Trade runs up to +5 pips, BE fires and moves SL to
  entry + 2. Price later drifts back, trade stops out at the new SL
  with a small profit.
- **Arithmetic:**
  - Sub-bar N has `sb_high = 1.10005` (float PnL = +5). BE fires.
    `be_price = entry + 2 × 0.0001 = 1.10002`. Guard passes
    (1.10002 > 1.09970). `pending_sl = 1.10002`.
  - Sub-bar N+1 applies pending. `current_sl = 1.10002`. On any
    sub-bar where `sb_low ≤ 1.10002`, SL fires. PnL = +2 pips.
- **Expected engine action:** BE fires once, SL moves to entry + 2,
  trade exits at +2 pips with `exit_reason = EXIT_BREAKEVEN`.
- **Verdict if this matches:** normal BE works as expected on small
  offsets. No bug here — this row is the *control*.

### Row 2 — The main bug on a long (`offset > trigger`)

- **Setup:** long, entry = 1.10000. SL = 1.09970, TP = 1.10060.
- **Knobs:** `trigger = 5, offset = 10`. Same sampleable pair that
  won the 2026-04-19 sweep.
- **Story:** Trade reaches +5 pips, BE fires, and the engine writes
  an SL at entry + 10 pips — *above* current price. Next sub-bar
  exits at that SL for an impossible +10 pip win.
- **Arithmetic:**
  - Sub-bar N has `sb_high = 1.10005` (float PnL = +5). BE fires.
    `be_price = entry + 10 × 0.0001 = 1.10010`. Guard passes
    (1.10010 > 1.09970 — *monotonicity only*). `pending_sl = 1.10010`.
  - Sub-bar N+1 applies pending. `current_sl = 1.10010`. On any
    sub-bar where `sb_low ≤ 1.10010`, SL fires. Because the sub-bar
    that just triggered had `sb_high = 1.10005`, continuation
    sub-bars almost certainly have `sb_low ≤ 1.10010`.
  - Exit PnL = `(1.10010 − 0 − 1.10000) / 0.0001 = +10 pips`.
- **Expected engine action:** BE fires, SL written above current
  price with no guard rejecting it, trade exits at +10 pips with
  `exit_reason = EXIT_BREAKEVEN`.
- **Verdict if this matches:** **confirmed bug.** The engine is
  gifting `+offset` pips whenever float PnL reaches `trigger`.
  Explains the 78 % win-rate.

### Row 3 — The main bug on a short (mirror of Row 2)

- **Setup:** short, entry = 1.10000. SL = 1.10030, TP = 1.09940.
- **Knobs:** `trigger = 5, offset = 10`.
- **Story:** Same bug reflected. Short reaches +5 pips of profit
  (price falls to 1.09995), BE moves SL to 1.09990 — *below* current
  price — and the next sub-bar exits at +10 pips.
- **Arithmetic:**
  - Sub-bar N has `sb_low = 1.09995` (float PnL for short = +5). BE
    fires. `be_price = entry − 10 × 0.0001 = 1.09990`. Guard passes
    (1.09990 < 1.10030 — monotonicity). `pending_sl = 1.09990`.
  - Sub-bar N+1 applies pending. `current_sl = 1.09990`. On any
    sub-bar where `sb_high ≥ 1.09990`, SL fires. Because the prior
    sub-bar had `sb_low = 1.09995`, continuation sub-bars almost
    certainly have `sb_high ≥ 1.09990`.
  - Exit PnL = `(1.10000 − 1.09990 − 0) / 0.0001 = +10 pips`.
- **Expected engine action:** mirror of Row 2. Trade exits at +10
  pips, `exit_reason = EXIT_BREAKEVEN`.
- **Verdict if this matches:** **confirmed bug is symmetric** —
  applies to both directions, not just longs.

### Row 4 — Trigger-on-spike with tight offset (probably not a bug)

- **Setup:** long, entry = 1.10000. SL = 1.09970, TP = 1.10060.
- **Knobs:** `trigger = 5, offset = 2`.
- **Story:** Within a single M1 sub-bar, price spikes up to +6 pips,
  then falls back to close at +2 pips. The BE trigger uses the
  sub-bar's *high*, so BE fires on the spike — even though price
  never actually held the trigger level. SL moves to entry + 2.
  Then the next sub-bar retraces below entry, and the trade exits
  at entry + 2.
- **Arithmetic:**
  - Sub-bar N: `sb_high = 1.10006`, `sb_low = 1.10001`,
    `sb_close = 1.10002`. `float_pnl_pips` from `sb_high = 6 ≥ 5`
    → BE fires. `be_price = 1.10002`. `pending_sl = 1.10002`.
  - Sub-bar N+1: `sb_low = 1.09998`, `sb_high = 1.10001`,
    `sb_close = 1.09999`. `current_sl = 1.10002`. SL check:
    `sb_low (1.09998) ≤ 1.10002` → fires. Exit at 1.10002,
    PnL = +2.
- **Expected engine action:** trade exits at +2 pips even though
  without BE the same price path would have lost (price closed at
  entry − 1 pip on sub-bar N+1).
- **Is this a bug?** **Defensible.** A tick-level BE trigger is
  standard retail behaviour — if the trade touched +5 at *any*
  point, the trigger has been met. But it does mean a single spike
  can "rescue" a losing trade. Worth knowing, probably not worth
  changing.

### Row 5 — Spike trigger combined with the main bug (the worst case)

- **Setup:** long, entry = 1.10000. SL = 1.09970, TP = 1.10060.
- **Knobs:** `trigger = 5, offset = 10`.
- **Story:** Price spikes briefly to +6 pips then falls back. BE
  still fires because of the spike. `offset = 10` places the SL
  above even the spike-high. Next sub-bar, price has retreated well
  below entry — but the SL at entry + 10 is waiting. It fires, and
  the trade exits for a full +10 pips, despite the fact that every
  realistic trade outcome after the spike was a loss.
- **Arithmetic:**
  - Sub-bar N: `sb_high = 1.10006`, `sb_low = 1.10001`,
    `sb_close = 1.10002`. `float_pnl_pips from sb_high = 6 ≥ 5` → BE
    fires. `be_price = 1.10010`. Guard passes. `pending_sl = 1.10010`.
  - Sub-bar N+1: price retraces hard. `sb_low = 1.09994`,
    `sb_high = 1.10001`, `sb_close = 1.09995`. `current_sl = 1.10010`.
    SL check: `sb_low (1.09994) ≤ 1.10010` → fires. Exit at 1.10010,
    PnL = +10 pips.
- **Expected engine action:** trade exits at +10 pips. In the real
  market this trade made money only for a single tick; in the
  backtest it banks the full fake offset.
- **Verdict if this matches:** this is how the 78 % win-rate result
  is generated in the wild — not just a bug, but a bug *amplified*
  by the intrabar-trigger behaviour. Fixing the main SL-above-price
  guard eliminates this entirely.

### Row 6 — Negative offset (safe path, should work normally)

- **Setup:** long, entry = 1.10000. SL = 1.09970, TP = 1.10060.
- **Knobs:** `trigger = 5, offset = −2`.
- **Story:** Trade reaches +5 pips, BE fires, SL moves to
  entry − 2 = 1.09998 (below entry, but tighter than the original
  1.09970). If price retraces, trade stops out at 1.09998 for a
  small controlled loss of −2 pips. If price runs, trade hits TP as
  normal. This is the "give the trade room, but limit the worst case"
  pattern.
- **Arithmetic:**
  - Sub-bar N: `sb_high = 1.10005`. BE fires.
    `be_price = 1.10000 − 2 × 0.0001 = 1.09998`. Guard passes
    (1.09998 > 1.09970). `pending_sl = 1.09998`.
  - Sub-bar N+1: `current_sl = 1.09998`. If `sb_low ≤ 1.09998`,
    SL fires at 1.09998. Exit PnL = −2 pips.
- **Expected engine action:** trade exits at −2 pips with
  `exit_reason = EXIT_BREAKEVEN`, or continues if price stays above
  1.09998.
- **Verdict if this matches:** the negative-offset path is safe
  against the main bug because the new SL is always on the correct
  side of current price. If this row passes in Phase 4, the fix
  only needs to constrain the *positive* offset case.

## Summary table

| # | Setup             | trigger | offset | Expected PnL | Expected exit_reason | Bug? |
|---|-------------------|---------|--------|--------------|----------------------|------|
| 1 | Long, normal      | 5       | +2     | +2 pips      | EXIT_BREAKEVEN       | No (control) |
| 2 | Long, offset>trig | 5       | +10    | +10 pips     | EXIT_BREAKEVEN       | **Main bug**  |
| 3 | Short, offset>trig| 5       | +10    | +10 pips     | EXIT_BREAKEVEN       | **Main bug (mirror)** |
| 4 | Long + spike      | 5       | +2     | +2 pips      | EXIT_BREAKEVEN       | Debatable — tick-level trigger |
| 5 | Long + spike + bug| 5       | +10    | +10 pips     | EXIT_BREAKEVEN       | **Main bug amplified**  |
| 6 | Long, negative    | 5       | −2     | −2 pips      | EXIT_BREAKEVEN       | No (safe path) |

## Scenarios where my brief and the trace disagree

None. The trace confirmed every prediction from the brief. Phase 4
will tell us if the *engine* matches, not just the reasoning.

## What Phase 4 must test

Each row becomes one `pytest` parametrize case with asserted PnL
(±0.01 pip tolerance) and asserted `exit_reason` (exact match). If
any row fails, either the prediction is wrong or the engine has
additional behaviour we have not yet traced — both answers are
useful. If every row matches, the bug is formally confirmed and
ready for Phase 6 verdict.
