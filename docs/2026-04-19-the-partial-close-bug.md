# The partial-close bug (2026-04-19)

## Summary

The third `validate-forex-knob` run of the day, on the `partial` knob,
surfaced **two distinct bugs** in `core/src/trade_full.rs:285-310`.
Both were fixed in the same patch and shipped as engine version
`v3 partial-fix`.

Prior runs in the sequence:
- morning: BE `offset`-above-trigger bug → v1 breakeven-fix.
- early afternoon: trailing-stop side-of-price bug → v2 trailing-fix.
- this run: partial close realisation / ordering bugs → v3 partial-fix.

## The investigation

After the trailing fix shipped, the dashboard showed a `v2 trailing-fix`
pill, confirmed engine-level improvements, but a trial in the sweep
caught the eye: **74.5 % win rate, +3.5 pip expectancy on a tight
(12.5 / 58.75 pip) configuration** with partial close doing most of the
work. The arithmetic did not reconcile if every exit were at TP or SL.
The obvious suspect: partial close had never been forensically verified.

## What the skill found

### Bug A — realise-at-sb_close (over-realisation, production-reachable)

```rust
// Pre-fix (wrong)
let partial_pnl = if is_buy {
    (sb_close - slippage_price - actual_entry) / pip_value * close_pct
} else {
    (actual_entry - sb_close - slippage_price) / pip_value * close_pct
};
```

The partial realised at `sb_close`, the end-of-sub-bar price. In real
trading, a partial-close limit order fills **at the trigger price on
the first touch**, not at the sub-bar close. The bug over-stated pnl on
every partial that fired while the sub-bar was trending in the
favourable direction:

```
over-realisation = (sb_close - partial_trigger_price) * pct/100
```

On the user's suspicious trial (trigger=44, pct=72.75): 1 – 3 pips of
sub-bar over-shoot adds 0.7 – 2.2 pips per partial, which compounds to
a ~2 pp win-rate lift and a ~1 pip expectancy lift across a 500-trial
sweep.

The fix replaces `sb_close - actual_entry` arithmetic with a direct
`(partial_trigger_pips - slippage_pips) * close_pct` — the same shape
SL and TP fills already use (price the limit, not the bar close).

### Bug B — trigger-over-tp ordering (latent, not currently reachable)

Within the sub-bar loop, the code order was `partial → SL → TP`. On a
sub-bar where `sb_high` crossed both `partial_trigger` and `tp_price`
with `trigger > tp`:

1. Partial fires first (at the wrong price — see Bug A),
2. TP fires second on the remainder,

which over-reports pnl. In reality a TP limit order sitting closer to
entry fills before the partial limit can even see the price.

This is latent in production because `sl_tp::compute_sl_tp:67-69`
clamps `tp_distance >= sl_distance`, and the sampler caps
`partial_trigger ≤ 0.8 × sl_max`, so the condition
`partial_trigger > tp_distance` never arises from real schema samples.
The fix is defence-in-depth — if that clamp is ever removed or
bypassed, the ordering guard catches the bug.

```rust
// Post-fix guard (Bug B)
let tp_pips_from_entry = if is_buy {
    (tp_price - actual_entry) / pip_value
} else {
    (actual_entry - tp_price) / pip_value
};
let tp_reachable_this_sub = if is_buy {
    sb_high >= tp_price
} else {
    sb_low <= tp_price
};
let tp_has_priority = tp_reachable_this_sub
    && tp_pips_from_entry < partial_trigger_pips;
if !tp_has_priority {
    // ... fire partial
}
```

## Before / after, on the six-row micro-test

| # | Scenario                                  | Pre-fix | Post-fix | Δ      | Bug |
|---|-------------------------------------------|---------|----------|--------|-----|
| 1 | Partial OFF, long → TP                    | +60.0   | +60.0    | 0      | —   |
| 2 | trigger=10 < TP=60, long                  | +36.0   | +35.0    | −1.0   | A   |
| 3 | trigger=10 < TP=60, short                 | +36.0   | +35.0    | −1.0   | A   |
| 4 | trigger=44 > TP=12, long (SL=12 override) | +26.0   | +12.0    | −14.0  | B   |
| 5 | trigger=44 > TP=12, short                 | +26.0   | +12.0    | −14.0  | B   |
| 6 | partial rescues to win                    | +6.6    | +4.5     | −2.1   | A   |

All six rows now pass. `tests/test_knob_sensitivity.py::
test_partial_close_knob_moves_outcomes` still passes. All 46 other
Python tests pass.

## Impact on downstream work

- **Any dashboard run from before the 1:10pm restart is stale.** The
  history tab will keep them as historical records, but compare-to-
  baseline numbers that span v2 → v3 are not meaningful.
- **Golden baseline re-pin deferred.** The pinned golden at
  `tests/golden/complex01_seed42_500trials.json` is `@pytest.mark.slow`
  and skipped by default. When the user next runs the slow suite, the
  pin should be regenerated so the baseline reflects v3 numbers.
- **Chandelier stop validation and new baselines should start on v3.**

## Files touched

- `core/src/trade_full.rs` — partial block guards + realisation price.
- `ff/VERSION.py` — bumped to `v3 partial-fix` with history entry.
- `tests/validation/test_partial_close_mechanics.py` — new, six
  scenarios with post-fix expected pnls.
- `docs/validation/2026-04-19-partial-close/` — six-artefact skill output:
  mechanics brief, code trace, behaviour table, micro-test copy,
  sensitivity results, verdict.
- `docs/2026-04-19-the-partial-close-bug.md` — this document.
