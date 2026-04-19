# The signal-filter bugs — 2026-04-19 evening

## What was broken

Fire Forex has three filter families that decide which raw signals become trades:

1. **Variant** (`PL_SIGNAL_VARIANT`) — picks one pooled signal family / param combo.
2. **Buy / sell value** (`PL_BUY_FILTER_MAX`, `PL_SELL_FILTER_MIN`) — direction-scoped equality gate.
3. **Generic Pk** (`PL_SIGNAL_P0..P9`) — ten exact-match gates.

Variant was fine. The other two each hid a different silent-aggressive-filter trap.

## Four defects, one fix pass

Found via the fifth `/validate-forex-knob` run. Full forensic trail at `docs/validation/2026-04-19-signal-filters/`.

| ID | Where | Symptom |
|----|-------|---------|
| D2 | `ff/encoding.py` | `ENGINE_DEFAULTS` omitted `PL_SIGNAL_P0..P9`. Any EA adding a Pk slot without explicitly registering it would leave the trial value at `0.0` — which the engine reads as "active filter for value zero" and silently rejects every signal with `sig_filters[f] > 0`. Dormant today (no EA registers Pk), but a trap for the next developer. |
| D4 | `core/src/lib.rs:260` | Pk trial value extracted via `params[col] as i64` — truncates `2.9 → 2`, `-0.9 → 0`. Any continuous sampler across a Pk slot silently buckets toward zero. |
| D5 | `core/src/lib.rs:278,283` | Buy/sell filter compared with raw `!=` on `f64`. `0.1 + 0.2 != 0.3` in IEEE 754, so any signal family computing `filter_value` arithmetically silently failed the equality. |
| D6 | `core/src/lib.rs:277-285` | Buy/sell guard rejected signals with `filter_value = -1` whenever the trial side was active, even though variant and Pk both honour `-1` as a bilateral opt-out. Asymmetric sentinel semantics. |

## The fix

**Python (`ff/encoding.py`):**

```python
ENGINE_DEFAULTS: dict[int, float] = {
    bc.PL_SIGNAL_VARIANT: -1.0,
    bc.PL_BUY_FILTER_MAX: -1.0,
    bc.PL_SELL_FILTER_MIN: -1.0,
    # Pk slots default to -1 (off) — closes D2.
    **{bc.PL_SIGNAL_P0 + i: -1.0 for i in range(bc.NUM_SIGNAL_PARAMS)},
}
```

**Rust (`core/src/lib.rs`):**

- D4 — `params[col as usize] as i64` → `params[col as usize].round() as i64`. Sampler drawing `2.9` now rounds to `3` (intuitive) instead of truncating to `2`.
- D5 — buy/sell compare changed from `!= buy_filter_max` to `(... - buy_filter_max).abs() >= 1e-9`. Absorbs f64 arithmetic drift. Integer-valued categoricals unaffected.
- D6 — buy/sell guard gains `&& sig_filter_value_s[si] >= 0.0` to honour signal-side `-1` as a bilateral opt-out. Now matches Pk family's sentinel shape.

## What changed for existing sweeps

Nothing. `complex01` registers only the variant filter; buy/sell/Pk stayed at the `-1.0` default in every historical sweep. D4/D5/D6 paths are not exercised. D2 was dormant.

**No golden baseline re-pin needed.** Historical metrics are byte-identical.

## Tests

Before fix:
- `tests/validation/test_signal_filters_mechanics.py` — 9 hand-calculated rows pinning the *buggy* behaviour.
- `tests/test_knob_sensitivity.py` — 3 new rows for variant / buy / sell filters.

After fix:
- Rows 4, 5, 8 of the micro-test had their `expected_trades` updated to post-fix behaviour.
- Row 9's comment updated — still exercises the engine-side semantic (trial `P0=0` treated as active), but the encoder-layer trap it was documenting is closed.
- New `test_encoding_defaults_for_pk_slots` pytest confirms `encode()` now writes `-1.0` into every unregistered Pk slot.

**66 / 66 tests pass**, including the full management-knob sensitivity guardrail.

## Engine version

Bumped `v4 scatter` → `v5 signal-filter-fix` in `ff/VERSION.py`. Web UI header will pick up the new label after the next server restart.

## How it was found

`/validate-forex-knob` session ran all six phases:

1. **Mechanics brief** — named the naming mismatch (D1), float brittleness (D5), truncation (D4), zero-init collision (D2 precursor), and asymmetric sentinel (D6 precursor).
2. **Code trace** — located every call site; Codex second-opinion surfaced the harness-level `-1` pre-fill for Pk (retracting a false-positive defect) and caught the `param_layout = np.arange()` identity layout (which made a Rust fallback path dead).
3. **Behaviour table** — 9 rows, hand-calculated.
4. **Micro-test** — 9 / 9 passed against buggy engine, confirming hypothesised failure modes.
5. **Sensitivity** — 3 new rows in `test_knob_sensitivity.py`; silent-no-op hypothesis rejected.
6. **Verdict** — (a) for variant; (b) for buy/sell and Pk. Defect list catalogued.

User greenlit "fix it all"; Phase 6.5 ship checklist executed end-to-end.

## Follow-ups

- Slot rename (D1) deferred — `PL_BUY_FILTER_MAX` → `PL_BUY_FILTER_MATCH` is cosmetic but breaks every downstream caller. Docstrings already carry the correction; rename can wait for an API-break pass.
- Chandelier stop remains an outstanding validation candidate when it lands.
