# The day we found out half the knobs didn't work

**Date:** 2026-04-19
**Authors:** You + Claude (claude-opus-4-7[1m]) + second opinions from
GPT-5.4 via Codex and six internal audit agents.

## What we set out to do

This session started as a speed-roadmap execution: disk cache for the
signal library, parallel variant build, parquet keep-warm. Three phases
from the plan. All three shipped behind green tests.

Then you asked a harder question: *how do we know the whole system is
actually working? And how do we keep knowing, once we start adding
things like a Chandelier stop?*

That pulled on a thread.

## What the team audit turned up

Six parallel audit agents covered the signal library, Rust engine,
harness, schema/overrides, web UI, and test coverage. Codex did an
independent knob-by-knob audit. Between them they flagged seven
concrete issues and one smell. Most of the issues were real but
minor — concurrency races in `app/jobs.py`, an append race on
`history.csv`, an unbounded parquet cache, misleading filter-slot
names (`PL_BUY_FILTER_MAX` that actually does exact equality).

The smell was the serious one. Codex said: *"the harness always
passes `EXEC_BASIC`. The basic trade path has no trailing, no
break-even, no partial, no stale, no max-bars. All those knobs are
silently ignored in every sweep."*

## Verification

Two call sites in `ff/harness.py` (lines 314 and 355), both hard-coded
to `bc.EXEC_BASIC`. A `grep` of `core/src/trade_basic.rs` for
`trail|breakeven|partial|stale|max_bars` returned zero matches. So
the claim was not a theoretical concern — it was literal behaviour.

Every complex01 backtest we'd run this session (and, presumably, every
run before) produced numbers that didn't reflect the EA's declared
behaviour. The sampler was rolling trailing-stop distances and BE
triggers and partial-close percentages, the display was printing them
as "winning parameters", but the Rust engine never opened those slots.

## The fix — simplify, don't switch

First draft of the plan was *"make the harness smart about picking
the right mode."* You stopped it: Fire Forex forked from
ClaudeBackTester to escape legacy complexity, and a two-mode switch
is exactly that. The real fix was to delete the option entirely.

Before deleting, we proved the full path behaves identically to the
basic path when all management knobs are off. We ran `baseline.py`
(no management knobs) under both modes. Bit-for-bit identical
metrics. That made it safe to:

1. Delete `core/src/trade_basic.rs`.
2. Strip the `exec_mode` argument from `batch_evaluate`.
3. Remove the `EXEC_BASIC` / `EXEC_FULL` constants.
4. Remove the two harness call sites' mode argument.
5. Move the `TradeResult` struct into `trade_full.rs` (its old home
   is gone).

Then we rebuilt with maturin, re-ran tests, re-pinned the golden.

## The numbers, before and after

Same EA (`complex01`), same seed (42), same trials (500):

|              | Before (basic, broken) | After (full, real) |
|---|---|---|
| trades       | 616 | 3,015 |
| win rate     | 6.49% | 78.04% |
| total pips   | +822 | +9,450 |
| expectancy   | +1.34 | +3.13 |
| max DD       | 46.52% | 1.88% |
| profit factor | 1.227 | 1.696 |

## The 78% win rate is worth a second look

The new winning trial has `breakeven.test=True` with
`trigger=5.024` and `offset=9.920`. That reads as *"once the trade
is 5 pips ahead, move stop-loss to entry +10 pips."* Which is
physically impossible for a long trade: if price is currently 5
pips above entry, a stop at entry+10 is above current price and
should stop the trade instantly. Either the engine interprets
`offset` differently than the name suggests, or the simulation
isn't accounting for slippage when the SL jumps ahead of current
price.

**This is an open question, not a confirmed fix.** Worth checking
before trusting the headline numbers. The per-knob sensitivity
tests we added today (`tests/test_knob_sensitivity.py`) prove
every knob moves outcomes — they don't prove each knob moves
outcomes *correctly*. Correctness of the BE math is the next
thing to interrogate.

## The safety net that landed today

1. **Six knob-sensitivity tests.** For each management knob
   (trailing, BE, partial, stale, max-bars, plus a session sanity
   check), two trial rows that differ only in that knob. If the
   outcomes match, the knob is silently dead — test fails loudly.
   This is the guardrail that would have caught the original bug
   the day it was introduced.

2. **A golden baseline.** `tests/golden/complex01_seed42_500trials.json`
   now pins the actual numbers from an `EXEC_FULL` sweep. Any
   future refactor that shifts them fails the test. The old broken
   numbers are kept in the file's `_meta.prior_broken_numbers`
   block as a historical cautionary tale.

3. **Hand-calculated math tests.** `tests/test_math_correctness.py`
   verifies EMA, ATR, RSI, Donchian breakout, and cross-detection
   formulas on tiny synthetic fixtures with expected answers
   computed in the test's docstring. One of those tests caught a
   mistake I made in my own hand-calculation on Donchian — which
   is exactly the point of writing them.

## What's still broken

From the audits, unfixed today:

- `app/jobs.py` lock released in the wrong thread — two concurrent
  `/api/run` calls can race past the 409 guard.
- `history.csv` append race — two runs finishing simultaneously
  can clobber each other.
- `_PARQUET_CACHE` in `ff/harness.py` has no LRU — long web
  sessions pin unbounded GB.
- Rust `catch_unwind` silently zeros metrics on panic, which looks
  like a bad strategy.
- NaN/Inf from OHLC, signals, or param matrix can poison ranking —
  no finite checks at the boundary.
- `layer_name` from the web API has no path-traversal sanitisation.
- Two filter slots (`PL_BUY_FILTER_MAX`, `PL_SELL_FILTER_MIN`) do
  exact equality despite names implying range. Rename or re-
  semantics.
- The `breakeven.offset` behaviour noted above.

## Test state at end of session

- Rust: 12 / 12 pass.
- Python: 30 / 30 pass (6 complexity + 6 cache + 11 math + 6 knob
  sensitivity + 1 golden baseline).
- Maturin rebuild: clean (6 warnings, none fatal).

## What to do next

Before anything else: **work out what `breakeven.offset` actually
does in the Rust engine.** If it's physically impossible to set an
SL above current price for a long, the engine is silently clamping
or no-op'ing, and the 78% win rate is partly fake. Everything else
depends on that being true.

After that, the concurrency fixes are small and self-contained.
Then we can think about the Chandelier stop with an actual safety
net in place.
