# 01 — Mechanics brief: trailing stop family

Phase 1 of validate-forex-knob, run 2026-04-19 (afternoon).

## The knob family

- **`trailing.test`** — on/off gate.
- **`trailing.mode`** — selector: `fixed` (fixed-pip distance) or `atr`
  (distance scales with current ATR).
- **`trailing.activate`** (pips) — the trade must be at least this
  many pips in profit before the trail starts to follow.
- **`trailing.distance`** (pips, fixed mode) — how far behind the
  favourable extreme the SL sits.
- **`trailing.atr_mult`** (ATR mode) — the SL sits
  `atr_mult × atr_pips` behind the favourable extreme.

Typical retail ranges:

- `activate`: 5 – 100 pips, scaled to volatility.
- `distance`: 5 – 50 pips (fixed mode).
- `atr_mult`: 0.3 – 4.0 (usually 1 – 2).

## What it means in standard forex practice

A **trailing stop** is a money-management pattern that lets winners
run while protecting accumulated profit. As the trade moves in the
favourable direction the stop is ratcheted forward; it is never
loosened. A retail EA typically:

1. Waits until the trade shows `activate` pips of float profit.
2. Once active, on every tick (or bar) computes a candidate SL:
   - Long: `SL_candidate = high_since_entry − distance`
   - Short: `SL_candidate = low_since_entry + distance`
3. Only accepts the candidate if it is *tighter* than the current
   SL. Never loosens.
4. Fires the SL like any other stop when price crosses it.

The "distance" can be literal pips (robust, simple) or scaled by
current ATR (adaptive — lets the stop widen during volatile periods
and tighten during quiet ones).

## Units

Entirely pip-denominated once ATR mode has multiplied. The
multiplication to price units is `distance_pips × pip_value` or
`atr_mult × atr_pips × pip_value`. Same pip-convention trap as
breakeven: pip_value must be pair-correct (0.0001 majors, 0.01
JPY).

## Interaction with spread

Exactly the same rules as a fixed SL or a BE-moved SL:

- Long SL fires when `bid ≤ SL`. The trail tracks price using the
  same series the engine uses for everything else — in our case
  single-series OHLC with a separate spread array.
- Short SL fires when `ask ≥ SL`.
- If the engine tracks the trailing high/low from `sb_high` /
  `sb_low` but uses `bid` for the hit-check, and `bid = close -
  spread / 2`, there is a ~spread-wide asymmetry: the SL candidate
  can sit *above* the current bid even when the engine's own
  high-water-mark looks fine. Same failure mode as
  breakeven.offset with a different trigger.

## Interaction with slippage

Same rules as any stop-market order: once triggered, the fill can
slip. Our engine uses exact-stop fills (see Phase 2 of the
breakeven validation). Gap-through is not modelled. That's a
standing concern, not a trailing-specific bug.

## Long vs short asymmetry

Symmetric with sign flips:

| Side | Candidate SL                | Monotonicity rule |
|------|-----------------------------|-------------------|
| Long | `sb_high − distance`         | `new > current`   |
| Short | `sb_low + distance`         | `new < current`   |

Activation uses `float_pnl_pips ≥ activate`. For a long,
`float_pnl_pips = (sb_high − actual_entry) / pip_value`. For a
short, `(actual_entry − sb_low) / pip_value`. So activation is
based on the *favourable extreme* — not close, not open. Same
intrabar-trigger pattern as breakeven.

## Edge cases — where the bug lives

Based on the breakeven investigation, the trailing stop almost
certainly has the same structural problem:

### Case A — `distance` smaller than (spread + one sub-bar's range)

Long at `entry = 1.10000`. `activate = 5`, `distance = 1` (both in
pips). Sub-bar N has `sb_high = 1.10006`, `sb_low = 1.10000`,
`sb_close = 1.10003`. Activation fires (float_pnl from sb_high = 6).
New candidate SL = `1.10006 − 1 pip = 1.10005`. This is **above**
`sb_close = 1.10003` — the trail is already deeper than where
price actually is.

A correct engine should reject this move (or the activation
itself) because the next sub-bar is almost certain to have
`sb_low ≤ 1.10005`, which will exit the trade at the new trailing
SL — paying out `+5 pips` as if the strategy "won" the move.
Incorrect engines accept and fire → unearned wins.

### Case B — ATR mode with tiny ATR

Same structure, different knob. If `atr_pips` is small (quiet
market) and `atr_mult ≤ 0.5`, the effective distance can drop
below the one-sub-bar range, producing the same above-price SL.
ATR mode has an additional concern: the ATR is calculated at entry
(fixed for the trade) or on-the-fly (refreshes every bar)? Phase 2
must check — refreshing ATR would let a volatility collapse push
the SL above price even during an otherwise-normal trade.

### Case C — activation on a spike

Sub-bar N has a one-tick spike to +10 pips then closes back at +2.
`activate = 5` → trail activates from the spike. Initial candidate
SL = `spike_high − distance`. If distance is also small, SL is set
well above current close. Next sub-bar almost certainly fires it.
Same pattern as breakeven.offset row 5.

### Case D — `activate` barely met then price retreats on the same sub-bar

Sub-bar N: `sb_high = entry + 5 pips = 1.10005`,
`sb_close = entry − 1 pip = 0.99999`. Float_pnl from sb_high = 5,
activate fires. Candidate SL = `1.10005 − 10 = 1.09995`. This one
is below sb_close (safe), but activation was triggered on a price
level the trade never actually held. Defensible (same logic as BE
spike-trigger) but still worth flagging.

## My prediction for Phase 2

The trailing block sits at `trade_full.rs:195 – 266`. From the
breakeven trace we already saw the guard at line 212 is
`if new_sl > effective_sl { pending_sl = new_sl; }` — a
monotonicity-only check identical in shape to the breakeven bug.
**I expect no side-of-price guard exists** on the trailing block.
If so, cases A, B, C above will reproduce as engine bugs in the
micro-test.

## What a correct engine should do

- Reject any trailing SL placement that is on the wrong side of
  the current confirmed price (`sb_close`).
- Or, equivalently, *never activate* the trail unless the
  activation threshold exceeds the distance by at least the
  bid-ask spread + one sub-bar's expected range.
- For ATR mode, enforce a minimum `distance = atr_mult × atr_pips`
  and reject configurations where distance ≤ 0 after rounding.

The cheapest fix is the same shape as the breakeven fix: a
`be_price < sb_close` check, applied here as
`new_sl < sb_close` for longs (and `new_sl > sb_close` for shorts)
before the candidate is accepted.

## Open questions for Codex / the trace

- Does the engine use sb_close or sb_low (for long) as the
  "current price" reference when the SL hit-check happens on the
  same sub-bar as the BE/trailing move?
- Is ATR recomputed per-bar during the trade or fixed at entry?
- Does the trailing block interact with the breakeven block — can
  both fire on the same sub-bar, and if so which wins?

---

## Codex independent brief

*Verbatim from Codex (gpt-5.4, reasoning-effort high, read-only
sandbox over the skill folder, no project files visible).*

> A retail forex trailing stop is a stop-loss that is repeatedly
> moved in the trade's favor as price moves favorably. It is still
> a stop order, not a profit target. Standard descriptions in
> MetaTrader docs, BabyPips, and Pardo's trading-system writing
> all treat it as a ratchet: for a long, the stop can rise; for a
> short, it can fall. It should not move away from price to give
> the trade more room.
>
> Your claimed semantics are broadly standard.
>
> For a long:
>
> ```text
> activate when floating profit >= activate pips
> favorable extreme = highest tradable close-out price since activation
> candidate_SL = favorable_extreme - trail_distance
> SL = max(existing_SL, candidate_SL)
> ```
>
> For a short:
>
> ```text
> activate when floating profit >= activate pips
> favorable extreme = lowest tradable close-out price since activation
> candidate_SL = favorable_extreme + trail_distance
> SL = min(existing_SL, candidate_SL)
> ```
>
> Typical retail units are pips, except ATR multiplier.
> `activate` is commonly maybe 5-50 pips for intraday FX, wider
> for swing systems. `distance` is often 5-100+ pips depending on
> timeframe, pair volatility, and spread. Values below spread or
> broker stop-level are usually pathological. `atr_mult` commonly
> lives around 1.0-4.0 ATR; below 0.5 is very tight, above 5 is
> loose.
>
> Spread matters. A long position is closed by selling at bid, so
> a long stop-loss fires when bid trades at or through the stop.
> A short is closed by buying at ask, so a short stop-loss fires
> when ask trades at or through the stop. A clean engine should
> track the favorable close-out side: long trails should use bid
> highs; short trails should use ask lows. If the engine only has
> bid OHLC plus spread, then short stop triggering must add
> spread to bid, and short favorable extremes should ideally be
> computed from ask, not raw bid. Many backtests get this wrong
> by triggering both long and short stops on bid candles.
>
> Slippage is not special because the stop moved. Once the
> trailing logic modifies the SL level, the stop behaves like any
> other stop-loss.
>
> Long/short asymmetry is directional but important. Long: profit
> rises as bid rises; stop sits below price; stop tightens
> upward; trigger is bid <= SL. Short: profit rises as ask falls;
> stop sits above price; stop tightens downward; trigger is
> ask >= SL. Formula sign errors are common.
>
> Edge cases:
>
> `distance` smaller than spread: mechanically possible but
> usually invalid in retail execution. Correct engines should
> respect broker minimum stop distance if modeling a broker.
>
> `distance` so small that the new long SL lands above current
> price: this is invalid as a live stop placement for a long,
> because a sell stop must be below the current bid. A correct
> engine should reject / clamp / defer that candidate until it is
> valid. It should NOT silently place an impossible stop above
> current bid and then claim an immediate favorable fill. Same
> issue applies in ATR mode when `atr_mult * atr_pips` is tiny.
>
> Activation barely met then retreat on the same bar is disputed
> because bar-only data cannot know sequence. If the high reached
> activation before the low hit the would-be trail, the trail
> could activate and stop out on the same bar. Correct bar
> engines need an explicit intrabar path assumption, lower-
> timeframe data, or conservative ambiguity handling.
>
> Should an engine allow a trailing SL placement that would fire
> immediately on the next tick? Once a VALID modified stop
> exists, yes, it can be hit on the next tick. But it should not
> allow an INVALID stop that is already beyond the current
> close-out price. Correct flow: compute candidate from favorable
> extreme, apply "tightens only," check side-validity and broker
> stop-level against current bid / ask, accept modification only
> if valid, then trigger using normal SL rules. Common incorrect
> engines compute from bar high/low, place the stop using
> hindsight, ignore spread / current-price validity, and stop out
> on the same bar at an impossible or overly favorable level.

---

## Diff — where my brief and Codex agree / disagree

### Load-bearing agreements

- **Units:** pips for activate / distance, ATR multiplier for
  atr_mult. Same typical retail ranges (Codex slightly tighter on
  `distance` — he says 5–100 pips, I said 5–50).
- **Long SL fires on bid, short SL fires on ask.** Long
  favorable extreme is bid-side; short favorable extreme is
  ask-side.
- **Slippage is not special for trailing** — same rules as any
  stop-market order.
- **The core prediction:** a trailing SL placed above current
  bid for a long (or below ask for a short) is invalid. A
  correct engine should reject, clamp, or defer. Incorrect
  engines place it anyway and accept an "immediate favorable
  fill" on the next bar. **Both briefs call out the same bug
  pattern, independently.**

### Codex-specific additions (worth carrying into Phase 2)

- **Bid-only candle feed on shorts.** Our engine uses a single
  OHLC series. For a short, the favorable extreme should
  technically be computed from ask, and the trigger from ask.
  The engine currently computes `float_pnl_pips` for short as
  `(entry − sb_low) / pip_value` and fires on `sb_high ≥ SL` —
  both using the single "mid" series with no ask adjustment.
  **Conclusion:** short trailing is doubly suspect — same
  side-of-price bug *and* structurally uses the wrong side of
  the book. Phase 2 must check whether `spread` is applied to
  the trigger comparison for shorts.
- **Broker minimum stop distance.** A concept I didn't name. If
  the engine has no such check, `distance` values below ~1 – 2
  pips produce valid-looking but pathological trails.
- **Activation-retreat intrabar ambiguity.** Codex flagged that
  bar-only data genuinely cannot resolve sequence — if `sb_high`
  reached activation before `sb_low` hit the would-be trail, the
  trail activates; if not, it shouldn't. Our engine reads
  `sb_high` first, so it has an implicit "favorable first"
  assumption for longs. Defensible but worth documenting.

### Disagreements

**None of substance.** Codex and I reach the same bug prediction
through the same reasoning.

### Load-bearing for Phase 2

The trace must answer:

1. Is there any side-of-price check on the candidate SL before it
   is accepted? (My prior from the earlier breakeven trace: no —
   only monotonicity.)
2. Does the engine compute short-side `float_pnl` and SL triggers
   using ask or bid? Is spread applied on the trigger comparison?
3. Is ATR computed at entry (fixed for trade) or refreshed per
   bar? Tiny ATR on refresh could push the SL above price mid-trade.
4. Does trailing interact with breakeven — can both fire on the
   same sub-bar, and if so which wins?
