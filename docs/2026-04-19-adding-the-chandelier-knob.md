# Adding the chandelier stop knob

**Date:** 2026-04-19 (evening)
**Engine version:** v6 chandelier-stop
**Build skill:** `add-forex-knob`
**Validation:** verdict (a) — works as advertised.

## What was added

A **peak-anchored ATR trailing stop** (classic Chuck LeBeau
Chandelier Exit) as a new independent Group at `engine.chandelier`
in the Fire Forex schema.

- For a long: `sl = max(sl_prev, highest_high_since_entry - atr_mult*ATR)`.
- For a short: `sl = min(sl_prev, lowest_low_since_entry + atr_mult*ATR)`.
- Ratchets one-way (stop only tightens in favour of the trade).
- Arms once `float_pnl_pips >= activate_pips`.
- Side-of-price guard mirrors the v2 trailing fix
  (`raw_sl < sb_low` long / `raw_sl > sb_high` short) — without it,
  arming on a spike bar would fire the stop on the same sub-bar for
  an unearned +`atr_mult*ATR` pip win.

Exposed in:

- `eas/complex01.json` — the JSON-backed EA — as a `chandelier`
  Group sibling of `trailing`, `breakeven`, `partial`, `stale`.
- `ff/defaults/complexity.py` — recipe-built EAs from **level 5
  onward** automatically include chandelier. Levels 1-4 still
  exclude it to keep the simple-management ladder intact.

## Why separate from the existing trailing stop?

`core/src/trade_full.rs` already had a `TRAIL_ATR_CHANDELIER = 2`
constant in `constants.rs`. **That mode is misnamed** — its
arithmetic is `sb_high - trail_atr_mult * ATR`, i.e. a
distance-from-current-high trail, not a distance-from-peak trail.
Classic chandelier requires tracking the highest-high since entry
across bars, which the pre-existing trailing code does not do.

Rather than fix the old mode in place (which would shift outcomes
for every sweep that has used it — a blast-radius concern two hours
before a working session wrap), the new knob ships as an independent
Group. Each trial can stack chandelier on top of trailing; the
engine picks the most protective SL. The old
`TRAIL_ATR_CHANDELIER` mode stays frozen and keeps its name for
now — a future session can deprecate or rename it.

## Why the whole skill, not "just throw it in quickly"?

Every other knob in Fire Forex's management family has shipped with
a silent-no-op or wrong-semantics bug that took weeks to catch.
This session alone fixed five of them — breakeven offset, trailing
side-of-price (v2), partial-close order of execution (v3), stale
exit, and five signal-filter defects (v5). The cost of each bug was
multiple days of contaminated optimisation results before anyone
noticed.

The seven-phase `add-forex-knob` workflow fronts-loads that cost:

1. **Mechanics brief** + Codex independent brief → caught one
   load-bearing disagreement (side-of-price guard vs
   fire-immediately) and resolved it consciously.
2. **Slot map** → explicit grid across seven layers, prevents the
   "forgot to add the parser line" bug.
3. **Reference scenarios** (hand-calculated) → writing the
   expected behaviour table before the code is the single best
   filter on under-specified mechanics.
4. **Minimal-diff implementation** → the only files touched are
   the ones the slot map named.
5. **Build + smoke** → 20-trial sweep and full pytest run.
6. **Auto-invoked validate-forex-knob** → six more artifacts;
   Codex independent code trace confirming every hop; five-scenario
   micro-test green; real-data A/B showing material sweep effect.
7. **Ship checklist** — this document.

Net: two Codex high-reasoning passes, five pass/fail pytest
scenarios, six narrative artifacts across two directories. Cost
about two hours; the historical alternative was three-to-five days
of invisible-bug contaminated sweeps.

## Observable behaviour

From the phase 5 A/B run (500 trials, EUR_USD H1, seed 42, level 5):

| metric         | chandelier OFF | chandelier ON |
|----------------|----------------|---------------|
| trade count    | 356            | 421           |
| win rate       | 57.0 %         | 38.0 %        |
| total pips     | +2806.35       | +2414.72      |
| expectancy     | +7.88 pips     | +5.74 pips    |
| max DD         | 16.84 %        | 15.75 %       |
| profit factor  | 1.359          | 1.439         |

Forcing chandelier on **reduces** total pips on this pair/timeframe
— an expected artifact of locking a tighter management rule on
every trial regardless of fit. The natural-sweep path (chandelier
sampler free to toggle) would pick chandelier only when it improves
the objective — neither forced configuration is the optimal one.

## Files changed

**Rust:**
- `core/src/constants.rs` — `EXIT_CHANDELIER=7`,
  `PL_CHANDELIER_ENABLED/ACTIVATE/ATR_MULT = 37/38/39`.
- `core/src/trade_full.rs` — three new signature params; per-trade
  peak/trough trackers; per-sub-bar chandelier block mirroring the
  trailing-v2 side-of-price guard; exit attribution priority.
- `core/src/lib.rs` — three slot reads, three call args, three
  pyo3 `m.add()` exports.

**Python:**
- `eas/complex01.json` — new `"chandelier"` Group.
- `eas/complex01.py` — three `ENGINE_MAPPING` tuples.
- `ff/defaults/complexity.py` — `_build_chandelier`,
  `_optional_keys_for_level` (level 5+), mapping block.
- `ff/VERSION.py` — `v6 chandelier-stop` + history entry.
- `tests/test_knob_sensitivity.py` — new
  `test_chandelier_knob_moves_outcomes` row.
- `tests/validation/test_chandelier_mechanics.py` — five-scenario
  micro-test.

**Docs:**
- `docs/builds/2026-04-19-chandelier-stop/` — 01 through 06,
  build-side.
- `docs/validation/2026-04-19-chandelier-stop/` — 01 through 06,
  validate-side.

## Known follow-ups

1. **`best_params_english` renderer.** `app/routes.py` does not
   yet mention chandelier settings in the English trial summary,
   so a user scanning the dashboard cannot tell at a glance
   whether the knob was on in the best trial. Non-blocking for
   correctness; fix in a future session.
2. **`ATR_RULES` volatility entry.** `ff/defaults/volatility.py`
   does not yet scale `chandelier.when_on.activate` per pair.
   Current 5-25 pip range is fixed across pairs. Defer until
   post-ship usage data justifies per-pair scaling.
3. **Rename the misnamed `TRAIL_ATR_CHANDELIER` trailing mode.**
   Consider renaming to `TRAIL_ATR_DISTANCE` to free the
   "chandelier" name and avoid confusion when reading the code.
   Separate PR, needs to update the trailing mechanics test and
   the behaviour table.

## References

- Build artifacts: `docs/builds/2026-04-19-chandelier-stop/`
- Validation artifacts: `docs/validation/2026-04-19-chandelier-stop/`
- Codex `gpt-5.4 high` independent brief + trace: verbatim in the
  artifact `01-mechanics-brief.md` (build) and `02-code-trace.md`
  (validate).
- Live engine version string: `ff.VERSION.VERSION == "v6 chandelier-stop"`.
