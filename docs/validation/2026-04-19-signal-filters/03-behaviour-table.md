# Phase 3 — Signal filters behaviour table

Nine hand-calculated scenarios covering the six defect candidates from Phase 2 (D1 naming — covered by D5/D6 in practice; D2 ENGINE_DEFAULTS hole; D4 truncation; D5 float equality brittleness; D6 buy/sell asymmetric sentinel) plus baseline positive-control paths for the three filter families.

All scenarios share the same fixture skeleton: synthetic H1 OHLC with no price movement (so SL/TP never fire); fixed SL = 100 pips, fixed TP_RR = 1.0 (effectively never); no trailing, no breakeven, no partial, no stale, no max_bars. Primary assertion is **how many of the planted signals are admitted and produce trades**, read from `metrics[bc.M_TRADES]`. Secondary assertion is on direction distribution (buy vs sell count) via the PnL buffer.

## Fixture invariants (shared across all rows)

```
ENTRY_PRICE = 1.10000
PIP         = 0.0001
N_BARS      = 30 H1 bars × 60 M1 = 1800 sub-bars
SPREAD      = 0.0 (disabled everywhere)
COMMISSION  = 0.0
SLIPPAGE    = 0.0
SL_MODE     = 0 (fixed)  SL_FIXED_PIPS = 100  (never reached)
TP_MODE     = 0 (rr)     TP_RR_RATIO   = 1.0  (never reached: all bars flat)
TRAILING / BE / PARTIAL / STALE / MAX_BARS = all off

Signals are planted on bars 5..14. Each test row plants 10 signals,
alternating long/short, each with configurable sig_variant /
sig_filter_value / sig_filters[f]. No two signals share a bar.

Since bars are flat and all management is off, every admitted signal
exits at the final bar close with PnL = 0.0 pips. Therefore:

  expected_trades     = count of admitted signals
  expected_pnl_total  = 0.0 (every trade is a break-even exit)

This lets us test the filter gate in isolation from trade mechanics.
```

## Scenarios

### Row 1 — `row_1_variant_positive_control`

Baseline: two variants (0 and 1), five signals each. Trial selects variant 0.

| Variable | Value |
|---|---|
| Signals | 5 with `sig_variant=0`, 5 with `sig_variant=1`, alternating long/short |
| Trial `PL_SIGNAL_VARIANT` | `0` |
| Trial buy/sell/Pk filters | all `-1` (off) |
| Expected admits | **5** (the variant-0 signals) |
| Why | `lib.rs:269-271` requires exact match when both sides `≥0`. |

### Row 2 — `row_2_variant_signal_side_opt_out`

Some signals are tagged `variant=-1` (explicit opt-out). Trial picks variant 1.

| Variable | Value |
|---|---|
| Signals | 3 `variant=0`, 3 `variant=1`, 4 `variant=-1` |
| Trial `PL_SIGNAL_VARIANT` | `1` |
| Expected admits | **3 + 4 = 7** (variant-1 match + variant-`-1` bilateral opt-out) |
| Why | `lib.rs:269` requires both sides `≥0` for the check to fire. Signal side `-1` skips the equality entirely → admits. |
| Settles | Phase 1 disagreement between my brief and Codex's brief. My reading wins. |

### Row 3 — `row_3_buy_filter_positive_control`

Buy filter set to `2.0`. Integer-valued filter_value on signals.

| Variable | Value |
|---|---|
| Signals | 4 long with `filter_value=2.0`, 2 long with `filter_value=3.0`, 4 short with `filter_value=5.0` |
| Trial `PL_BUY_FILTER_MAX` | `2.0` |
| Trial `PL_SELL_FILTER_MIN` | `-1.0` (off) |
| Expected admits | **4 (matching long) + 4 (short unfiltered) = 8** |
| Why | Long guard `lib.rs:277-280` active; rejects `filter_value != 2.0`. Short guard off → all shorts admit. |

### Row 4 — `row_4_float_equality_brittleness` (D5)

Signal `filter_value` computed arithmetically to produce `0.1 + 0.2`, trial sets `0.3`.

| Variable | Value |
|---|---|
| Signals | 5 long with `filter_value = np.float64(0.1) + np.float64(0.2)` (= `0.30000000000000004`), 5 short unfiltered |
| Trial `PL_BUY_FILTER_MAX` | `0.3` |
| Trial `PL_SELL_FILTER_MIN` | `-1.0` |
| Expected admits | **0 (longs) + 5 (shorts) = 5** |
| Why | Arithmetic drift: `0.1+0.2 != 0.3` in f64. `lib.rs:278` is exact `!=`, so every long is silently rejected. This is D5. |

### Row 5 — `row_5_buy_filter_signal_side_minus_one` (D6)

Some signals write `filter_value = -1` intending "opt out". Trial buy filter active.

| Variable | Value |
|---|---|
| Signals | 3 long with `filter_value=2.0`, 3 long with `filter_value=-1.0`, 4 short unfiltered |
| Trial `PL_BUY_FILTER_MAX` | `2.0` |
| Expected admits | **3 (matching) + 0 (-1 rejected) + 4 (shorts) = 7** |
| Why | `lib.rs:278` does `!=` on `f64`. `-1.0 != 2.0` → skip. The `-1` signal-side sentinel is NOT honoured for buy/sell. This differs from variant and Pk filters. D6. |

### Row 6 — `row_6_buy_sell_directional_asymmetry`

Both filters active with different values — each direction has its own gate.

| Variable | Value |
|---|---|
| Signals | 5 long with `filter_value=2.0`, 5 short with `filter_value=3.0` |
| Trial `PL_BUY_FILTER_MAX` | `2.0` |
| Trial `PL_SELL_FILTER_MIN` | `3.0` |
| Expected admits | **5 + 5 = 10** |
| Why | Both guards active but each matches its direction's signals. Demonstrates that the two knobs are independently direction-scoped. |

### Row 7 — `row_7_pk_bilateral_opt_out_positive_control`

Trial's Pk filter active; signals mix of matches, mismatches, and opt-outs.

| Variable | Value |
|---|---|
| Signals | 3 with `sig_filters[0]=5`, 3 with `sig_filters[0]=3`, 4 with `sig_filters[0]=-1` |
| Trial `PL_SIGNAL_P0` | `5.0` |
| Expected admits | **3 (matching) + 0 (mismatch) + 4 (-1 opt-out) = 7** |
| Why | `lib.rs:290-293` — bilateral opt-out. Trial `-1` off; signal `-1` admits; otherwise equality required. |

### Row 8 — `row_8_pk_trial_truncation` (D4)

Trial slot written as `2.9` (as would happen from a FloatRange sampler). `as i64` truncates to `2`.

| Variable | Value |
|---|---|
| Signals | 5 with `sig_filters[0]=2`, 5 with `sig_filters[0]=3` |
| Trial `PL_SIGNAL_P0` | `2.9` (written as float) |
| Expected admits | **5 (matching `2` after truncation) + 0 (the `3` signals mismatch) = 5** |
| Why | `lib.rs:260` — `params[col as usize] as i64` truncates toward zero. `2.9` becomes `2`. Continuous sampling across a Pk slot silently buckets. D4. |

### Row 9 — `row_9_engine_defaults_hole_p0_equals_zero` (D2)

Trial never registers `PL_SIGNAL_P0`. Encoded matrix defaults to `0.0`. Rust reads `0` as active filter. Signals split between `sig_filters[0]=0`, `=1`, `=-1`.

| Variable | Value |
|---|---|
| Signals | 3 with `sig_filters[0]=0`, 3 with `sig_filters[0]=1`, 4 with `sig_filters[0]=-1` |
| Trial `PL_SIGNAL_P0` | `0.0` (emulating un-registered encoding default) |
| Expected admits | **3 (0==0 match) + 0 (1 mismatch) + 4 (-1 bilateral opt-out) = 7** |
| Why | `lib.rs:290` treats trial value `0.0 → 0` as `≥0` → filter active. Signals with `sig_filters[0]=1` are silently rejected. Any EA that adds P0..P9 to its `engine_mapping` must explicitly include `-1.0` in `ENGINE_DEFAULTS`, or sweeps that pool multiple signal shapes will miss-fire. D2. |

## How these nine rows map to the Phase 2 defect list

| Defect | Covered by row(s) |
|-------|-------------------|
| D1 — naming mismatch | Documentation-only; demonstrated by D5/D6 which depend on *not* behaving as names suggest |
| D2 — ENGINE_DEFAULTS hole for P0..P9 | Row 9 |
| D4 — `as i64` truncation | Row 8 |
| D5 — float equality brittleness on buy/sell | Row 4 |
| D6 — buy/sell asymmetric sentinel | Row 5 |
| Baseline (all three filter families) | Rows 1, 3, 7 |
| Phase 1 disagreement about variant opt-out | Row 2 |
| Direction asymmetry positive control | Row 6 |

## What each row proves about the engine

- **Works correctly when used with integer-valued categorical filter_values and registered slots**: rows 1, 3, 6, 7 all pass as advertised.
- **Silently rejects intended matches under arithmetic drift**: row 4. This is a *correctness* bug for any family that computes `filter_value`, not a documentation issue.
- **Silently rejects signals that used `-1` as an opt-out on the buy/sell family**: row 5. Semantic asymmetry not documented anywhere.
- **Silently accepts/rejects the wrong bucket under continuous sampling**: row 8. Truncation-by-design but needs to be surfaced in `encoding.py` docs.
- **Silently treats zero-defaulted unregistered slots as an active filter for value 0**: row 9. This is the closest analogue in the current filter code to the historical `EXEC_BASIC` silent-no-op trap — it is precisely the shape of bug the skill exists to prevent.

## Next

Phase 4 encodes these nine rows as a pytest module under `tests/validation/test_signal_filters_mechanics.py`. All nine rows are expected to **pass** against the current engine — the "bugs" (D2, D4, D5, D6) are **present but asserted as such** in the tests so any future fix will flip the assertion and surface itself. This mirrors the pattern used for the partial-close validation earlier today (pre-fix assertions pin the bug so a fix reveals itself).
