# Phase 5 — Signal filter sensitivity results

Goal: confirm each filter family moves outcomes in the direction the brief predicts. Rule out silent no-op (the 2026-04-19 EXEC_BASIC failure mode).

## Two layers of evidence

### Layer 1 — micro-test (`tests/validation/test_signal_filters_mechanics.py`)

All **9 / 9** rows pass. Positive-control rows 1, 3, 6, 7 require *active* filtering to match their expected admit counts. If any filter family were a silent no-op, at least one of those rows would fail with a trade count equal to 10 (the total planted signals) instead of the expected admit count (5, 8, 10, 7 respectively).

Defect rows (4, 5, 8, 9) exercise specific failure modes (D5, D6, D4, D2) and are pinned with the current buggy expectations — they would become failing if the engine were fixed, which is the desired regression-guard behaviour.

```
tests/validation/test_signal_filters_mechanics.py::test_signal_filter_scenario[row_1_variant_positive_control]        PASSED
tests/validation/test_signal_filters_mechanics.py::test_signal_filter_scenario[row_2_variant_signal_side_opt_out]     PASSED
tests/validation/test_signal_filters_mechanics.py::test_signal_filter_scenario[row_3_buy_filter_positive_control]     PASSED
tests/validation/test_signal_filters_mechanics.py::test_signal_filter_scenario[row_4_float_equality_brittleness_D5]   PASSED
tests/validation/test_signal_filters_mechanics.py::test_signal_filter_scenario[row_5_buy_filter_signal_side_minus_one_D6] PASSED
tests/validation/test_signal_filters_mechanics.py::test_signal_filter_scenario[row_6_buy_sell_directional_asymmetry]  PASSED
tests/validation/test_signal_filters_mechanics.py::test_signal_filter_scenario[row_7_pk_bilateral_opt_out_positive_control] PASSED
tests/validation/test_signal_filters_mechanics.py::test_signal_filter_scenario[row_8_pk_trial_truncation_D4]          PASSED
tests/validation/test_signal_filters_mechanics.py::test_signal_filter_scenario[row_9_engine_defaults_hole_P0_zero_D2] PASSED
9 passed in 0.06s
```

### Layer 2 — regression-guard sensitivity (`tests/test_knob_sensitivity.py`)

Added three new rows that flip each filter family on a real-data ema_cross fixture:

- `test_signal_variant_filter_knob_moves_outcomes` — flip trial variant to a nonexistent id → must change trade count or return.
- `test_buy_filter_knob_moves_outcomes` — set `PL_BUY_FILTER_MAX = 7.0` against signals with `filter_value = 0.0` → must change trade count.
- `test_sell_filter_knob_moves_outcomes` — symmetric on short side.

All three pass. The Pk family is covered by the Phase 4 micro-test rows 7 – 9; adding a sweep-scale Pk sensitivity to `test_knob_sensitivity.py` would require a fixture with non-`-1` `sig_filters` cells, which is out of scope for this session (the existing fixture uses `-1` everywhere because the ema_cross library does not populate Pk).

```
tests/test_knob_sensitivity.py::test_max_bars_knob_moves_outcomes            PASSED
tests/test_knob_sensitivity.py::test_trailing_stop_knob_moves_outcomes       PASSED
tests/test_knob_sensitivity.py::test_breakeven_knob_moves_outcomes           PASSED
tests/test_knob_sensitivity.py::test_partial_close_knob_moves_outcomes       PASSED
tests/test_knob_sensitivity.py::test_stale_exit_knob_moves_outcomes          PASSED
tests/test_knob_sensitivity.py::test_session_filter_knob_moves_outcomes      PASSED
tests/test_knob_sensitivity.py::test_signal_variant_filter_knob_moves_outcomes  PASSED  ← new
tests/test_knob_sensitivity.py::test_buy_filter_knob_moves_outcomes             PASSED  ← new
tests/test_knob_sensitivity.py::test_sell_filter_knob_moves_outcomes            PASSED  ← new
9 passed in 0.33s
```

## Verdict on silent no-op hypothesis

**Rejected.** All three filter families materially move outcomes in the direction the mechanics brief predicts. The historical EXEC_BASIC failure mode (silent slot read / no effect) does not apply to any signal-filter knob.

## Verdict on semantic-drift hypotheses

**Confirmed.** Four separate semantic gaps exist (D2, D4, D5, D6 — see Phase 2 defect table), all of which produce silent **filter-too-aggressive** behaviour in specific situations:

- **D5** — float equality brittleness on buy/sell when `filter_value` is computed arithmetically: silently drops intended matches (micro-test row 4).
- **D6** — buy/sell does not honour signal-side `-1` opt-out, unlike variant and Pk: silently drops any signal that used `-1` to mean "I don't participate" (micro-test row 5).
- **D4** — `as i64` truncation on Pk trial slot: continuous sampling collapses to coarse buckets silently (row 8).
- **D2** — `ENGINE_DEFAULTS` omits P0..P9, so unregistered Pk slots default to `0.0` and are treated as *active* filter for value zero (row 9).

Codex's Phase 1 prediction of the "broken-aggressive" shape (trade count collapses, not flat) is the correct model for what users would see if they hit any of these defects at sweep level.

## What this means for historical sweeps

Because `complex01` (the flagship EA) registers **only** the variant filter and leaves buy/sell/Pk at their engine-side defaults (`-1.0`, `-1.0`, and — per D2 — `0.0`), the four defects only impact sweeps that **extend** an EA to use the disputed filter families. D2 in particular is a landmine the next developer to add P0..P9 to an EA will step on.

Existing sweep outputs (complex01 runs to date) are **unaffected** by D4, D5, D6. Variant filter has always worked correctly. D2 is dormant until someone registers Pk.

## Next

Phase 6 writes the verdict: confirm variant filter is (a) works as advertised; buy/sell and Pk filters are (b) works with important name/semantics gaps. Document the four defects; propose either docstring fixes, renames, or the single `ENGINE_DEFAULTS` one-liner that closes D2. **Do not ship any fix until the user returns and explicitly greenlights it** — that is the explicit out-of-scope boundary set at session start.
