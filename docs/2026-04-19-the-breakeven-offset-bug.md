# The day the 78 % win-rate turned out to be free money

**Date:** 2026-04-19 (afternoon)
**Engine version after fix:** `v1 breakeven-fix`
**Authors:** You + Claude (claude-opus-4-7[1m]) + second opinions from
GPT-5.4 high via Codex.

## What kicked this off

The morning's work killed the EXEC_BASIC silent-no-op bug (see
`2026-04-19-the-exec-basic-bug.md`) and left one nagging thread: a
`breakeven` trial had won the 500-trial complex01 sweep with
`trigger ≈ 5, offset ≈ 10`, producing a 78 % win-rate and 1.88 %
max drawdown. On a long trade, that parameter pair reads as *"once
price is +5 pips above entry, move the stop-loss to +10 pips."*
But a stop-loss above current price should fire instantly — so
either the engine was rejecting the move (safe), interpreting
"offset" differently (semantic mismatch), or silently gifting us
+10 pips per trade (bug).

No existing test could tell those three apart. That's the gap this
session filled.

## The skill we built first

Instead of poking at the code directly we built a reusable tool:
`~/.claude/skills/validate-forex-knob/`. Its whole job is to turn
"is this knob actually working?" into a six-phase, repeatable
process. Phases are:

1. **Mechanics brief** — plain English explanation of what the knob
   means in standard retail forex, from first principles. Codex
   writes the same brief independently; we diff.
2. **Code trace** — follow the value from the UI schema to the
   Rust arithmetic, line by line. Codex reads the same files
   without seeing my trace.
3. **Behaviour table** — 3–6 hand-calculated scenarios predicting
   what the engine should do.
4. **Micro-test** — committed `pytest` file that runs the real
   engine on synthetic fixtures and asserts each scenario's PnL.
5. **Sensitivity** — run the knob on/off at sweep scale, confirm
   the direction of effect matches the brief.
6. **Verdict** — (a) works, (b) semantics mismatch → rename, (c)
   broken → engine bug.

Why the process matters: the EXEC_BASIC bug was caught by one
phase (code trace), but validation without the mechanics brief
would have caught it as "knob moves outcomes, fine" even though
the knob was moving them in the *wrong way*. The six phases
together catch both silent no-ops *and* wrong semantics.

## Applying the skill to `breakeven.offset`

The artifacts live at
`docs/validation/2026-04-19-breakeven-offset/`. Short summary:

- **Phase 1 brief, mine and Codex's.** Both independently conclude
  that a correct engine should reject any BE move that would place
  the SL on the wrong side of current price. Codex's phrase:
  *"common incorrect engines blindly assign the SL above market,
  then later mark it as an immediate profitable exit, creating
  impossible profits."* That is a *pre-registered* prediction of
  the bug we then went looking for.
- **Phase 2 trace, mine and Codex's.** Both find the same gap at
  `core/src/trade_full.rs:180` / `:185`. The guard at those lines
  only checks that the new SL is tighter than the old SL
  (monotonicity). It does not check that the new SL is on the
  correct side of current price. Codex's phrase: *"I found no
  check rejecting `new_sl > current price` for a long."*
- **Phase 3 behaviour table.** Six scenarios — three exercising
  the suspected bug, one control, one edge-case (negative
  offset), one compound worst-case (intrabar spike + bug).
  Hand-calculated PnL for each.
- **Phase 4 micro-test.** All six hand-calculations matched the
  live engine. Rows 2, 3, 5 produced exactly `+offset` pips each,
  confirming the bug is real, symmetric across long/short, and
  amplified by the intrabar-high trigger.
- **Phase 5 sensitivity.** 56 trades on a seeded synthetic
  fixture. BE off: 30 % win-rate. BE on with `offset=2`: 73 %
  win-rate, moderate pip loss. BE on with `offset=10`: *same* 73
  % win-rate but 148 more total pips. Same number of BE-triggered
  exits; each one bigger by the offset delta. No market
  justification — pure accounting artefact.
- **Phase 6 verdict.** (c) Broken. The fix is a side-of-price
  guard at the exact line numbers Codex and I both flagged.

## The fix, in five lines

In `core/src/trade_full.rs`, the original BE block accepted any
new SL that was tighter than the old one. The fix adds a second
condition: the new SL must also be on the correct side of
`sb_close` (the current sub-bar's confirmed price).

```rust
let accept = if is_buy {
    be_price > current_sl && be_price < sb_close
} else {
    be_price < current_sl && be_price > sb_close
};
if accept { /* set pending_sl, lock BE */ }
```

`sb_close` is the conservative reference point — more defensible
than `sb_low` / `sb_high`. When the guard rejects, BE re-arms
automatically on the next sub-bar. A configuration with
`offset > trigger` simply never fires BE, which is correct
behaviour, not a regression.

## Numbers, before and after

Same 500 trials, same seed (42), same complex01 EA, same data:

|              | Before fix (v0) | After fix (v1) |
|---|---|---|
| trades       | 3,015   | 894     |
| win rate     | 78.04 % | 17.79 % |
| total pips   | +9,450  | +1,617  |
| expectancy   | +3.13   | +1.81   |
| max DD       | 1.88 %  | 15.92 % |
| profit factor| 1.696   | 1.275   |
| best offset  | +9.92   | **−1.30** |

The story the numbers tell: the optimiser's "winning" strategy
yesterday was leaning hard on `offset > trigger` to generate fake
wins. Now that the bug is gone, the optimiser prefers a
*negative* offset (a legitimate "let the trade breathe below
entry before cutting it" pattern). The new numbers look much
worse on paper — 17 % win-rate, 16 % drawdown — but they are
earned, not gifted.

## Three things that landed with the fix

### 1. Live micro-test as guardrail
`tests/validation/test_breakeven_offset_mechanics.py` runs the
six scenarios against the real engine on every `pytest`
invocation. Safe configurations (rows 1, 4, 6) assert BE fires
and exits at the hand-calculated PnL. Bug-exercising
configurations (rows 2, 3, 5) assert the BE is *rejected* and the
trade completes at end-of-data — currently 0 pips. If the Rust
guard is ever removed or weakened, those three rows start
producing `+offset` pips and the test turns red loudly.

### 2. Engine version on the dashboard
The web UI header shows a small `engine v1 breakeven-fix` pill
next to the baseline / server pills. Powered by `ff/VERSION.py`
and a trivial `/api/version` endpoint. Bump the string and
redeploy after any future Rust change that shifts behaviour —
that way a sweep result taken today can be cross-referenced
against which engine version produced it.

### 3. History cleanup + selective delete
Because the pre-fix history was polluted by buggy-BE sweeps, all
old `artifacts/runs/*.npz`, `history.csv`, `baseline.json`, and
`comparison.html` were wiped. The golden baseline at
`tests/golden/complex01_seed42_500trials.json` was pinned with
the bug live and was also deleted — re-pin after the first v1
sweep when you are happy with the numbers.

The History tab now has a per-row checkbox, a "Delete selected"
button, a header "select all" checkbox, and a "Clear all" button.
Backed by `POST /api/history/delete` (which strips matching rows
from `history.csv` and deletes the corresponding `.npz` files)
and `POST /api/history/clear` (nuke everything). Delete paths are
validated — only plain `*.npz` filenames under `artifacts/runs/`
are touched.

## What is still open

From the Phase 6 verdict, unhandled:

- **Trailing stop.** Uses an almost-identical monotonicity-only
  guard pattern at `trade_full.rs:195 – 266`. Might have the same
  bug, might not. *Next validation target — run
  `validate-forex-knob` on it next session.*
- **Long-side SL fills do not apply spread.** Only `slippage_price`
  is subtracted (line 298). Entry for a long pays entry-side
  spread, but exit-side spread is never charged. For shorts,
  spread is applied at exit (line 361-371). Asymmetry is
  correctly accounted in aggregate (one spread crossing per
  trade), but the placement is inconsistent and worth a future
  validation run once we're confident the model is cleaner.
- **TP fills don't take slippage.** Limit orders in real retail
  can slip or not fill at all. Backtest is optimistic here.
- **Gap-through not modelled.** SL fills at the stop price even
  when price jumps past it. Optimistic.
- **Golden baseline needs re-pinning.** Currently deleted.
  Re-run the complex01 seed-42 500-trial sweep after the first
  v1 run you trust, then re-pin.
- **Consider `offset > trigger` at schema level.** Combinations
  like `trigger=5, offset=10` are now physically inert (BE never
  fires) but the sampler can still draw them. The optimiser will
  self-select away from them because they no longer print money,
  so no schema change is strictly required.

## Where to look for next session

- `docs/validation/2026-04-19-breakeven-offset/` — all six
  artifacts from this run.
- `tests/validation/test_breakeven_offset_mechanics.py` — live
  guardrail.
- `~/.claude/skills/validate-forex-knob/` — the skill itself.
  Invoke on `trailing.atr_mult` or `trailing.activate` next.
- `ff/VERSION.py` — bump when making engine changes.
- `core/src/trade_full.rs:172–195` — the fixed BE block.

## The bigger lesson

The EXEC_BASIC bug was caught by a code audit. This one was
caught by a *process*. The skill exists because "a strategy that
prints money is almost always a bug" is a truism in trading
engines, and catching that class of bug systematically needs a
mechanics brief *and* a code trace *and* a hand-calculated
expected value — any one of those alone is not enough. Every
future knob — Chandelier stop next, then whatever comes after —
goes through this six-phase sieve before its numbers get trusted.
