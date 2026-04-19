# 02 — Slot map: `chandelier_stop`

> Where the new value lives at every layer, *before* any edits.
> One row per layer; missing rows are silent-no-op bugs in waiting.

## Design decision from phase 1

**Option 2 adopted.** Chandelier is an **independent Group** sibling
of `trailing`, `breakeven`, `partial`. It reuses none of the existing
`trail_*` slots. The existing (misnamed) `TRAIL_ATR_CHANDELIER`
trailing mode stays as-is; chandelier does not touch the trailing
struct fields.

This means the schema path is `engine.chandelier.*`, mirroring the
breakeven / partial shape:

```
engine.chandelier.test              # bool gate
engine.chandelier.when_on.activate  # float, pips
engine.chandelier.when_on.atr_mult  # float, dimensionless
```

Three new PL slots, three new struct fields, one new EXIT code, one
per-bar arithmetic block, one per-trial peak/trough tracker pair.

## Layer table

| # | Layer                | File                                         | New addition                                                                                         | Notes |
|---|----------------------|----------------------------------------------|------------------------------------------------------------------------------------------------------|-------|
| 1 | Schema (JSON)        | `eas/complex01.json`                         | New `"chandelier"` Group at `engine.chandelier`: `test` Choice[true,false]; `when_on.activate` FloatRange 5-25 log; `when_on.atr_mult` FloatRange 2.0-4.0 linear step 0.25 | Mirror `breakeven` / `partial` shape |
| 2 | Engine mapping       | `eas/complex01.py`                           | Three new tuples appended to `ENGINE_MAPPING`: `PL_CHANDELIER_ENABLED` / `PL_CHANDELIER_ACTIVATE` / `PL_CHANDELIER_ATR_MULT` | Use existing `slot_bool_to_int` + `slot_if_on` helpers |
| 3 | Volatility defaults  | `ff/defaults/volatility.py` `ATR_RULES`       | `"chandelier.when_on.activate": (0.5, 2.5)` — multiples of daily ATR per pair, mirrors breakeven.trigger | `atr_mult` is scale-free — NO entry (the whole point is ATR-scaling) |
| 4 | Complexity recipe    | `ff/defaults/complexity.py`                   | Expose from level **3** onward (matches breakeven / partial / stale). Level 1-2 keep simple management only | |
| 5 | Encoding constants   | `core/src/constants.rs`                       | `PL_CHANDELIER_ENABLED: usize = 37`, `PL_CHANDELIER_ACTIVATE = 38`, `PL_CHANDELIER_ATR_MULT = 39`. Also `EXIT_CHANDELIER: i64 = 7`. | Slots 37-39 are first in the 37-63 reserved block. `NUM_PL = 64` stays — no resize needed. |
| 6 | Python-side exports  | `core/src/lib.rs` (pyo3 `m.add(...)`)         | Three `m.add("PL_CHANDELIER_*", ...)?;` lines after `PL_SELL_FILTER_MIN`                             | Without these, Python `bc.PL_CHANDELIER_*` lookups KeyError. |
| 7 | Rust struct          | `core/src/trade_full.rs`                      | Three fields on `TradeParams`: `chandelier_enabled: i64`, `chandelier_activate_pips: f64`, `chandelier_atr_mult: f64` | Place after the existing `partial_*` fields (lines ~40-45). |
| 8 | Rust parser          | `core/src/lib.rs::batch_evaluate`             | Three `let chandelier_* = params[param_layout_s[PL_CHANDELIER_*] as usize] ...;` reads; then written into the struct constructor call | **Most-skipped line. Do this BEFORE logic.** |
| 9 | Rust logic           | `core/src/trade_full.rs`                      | Per-bar block after trailing, before breakeven (or after breakeven — order matters for the "most protective SL" resolution; see below). Also per-trial peak/trough trackers. | See "Rust logic sketch" below |
| 10| UI                   | `app/static/` (no edit)                       | Renders automatically from JSON schema via the parameters tab                                        | Verify in phase 5 smoke |
| 11| Tests — sensitivity  | `tests/test_knob_sensitivity.py`              | Add `_chandelier_row` + `test_chandelier_on_off_changes_outcome` (phase 7 ship checklist)            | Regression net |
| 12| Defaults test        | `tests/golden/…`                              | Golden baseline may shift once chandelier ever gates ON. Re-pin in phase 7 if needed                 | Only triggered when a trial has chandelier on — at level 3+ |

## Sentinel convention

When `engine.chandelier.test == False`, the encoder writes:

- `PL_CHANDELIER_ENABLED = 0` (sentinel: gate off)
- `PL_CHANDELIER_ACTIVATE = -1.0` (sentinel: invalid pip distance)
- `PL_CHANDELIER_ATR_MULT = -1.0` (sentinel: invalid multiplier)

Rust-side guard at the **top** of the chandelier block:

```rust
if chandelier_enabled == 0 { /* skip block entirely */ }
```

Defensive secondary guard (matches signal-filter v5 convention):

```rust
if chandelier_atr_mult <= 0.0 || chandelier_activate_pips < 0.0 {
    /* treat as off — do not touch SL or peak tracker */
}
```

The secondary guard is **not** redundant: it catches a bug where the
encoder ever writes `enabled=1` but forgets to populate the float
slots. (This is the exact defect class signal-filter validation
found: asymmetric sentinels cause silent no-ops.)

## Reset behaviour

| Flag / state                  | Reset location                                        |
|-------------------------------|-------------------------------------------------------|
| `chandelier_peak_high` (long) | Per-trial: set to `actual_entry_high` at trade open  |
| `chandelier_trough_low`(short)| Per-trial: set to `actual_entry_low` at trade open   |
| `chandelier_sl` (both)        | Per-trial: initialised to `NaN` / sentinel at trade open; first valid update inside the activation guard |
| `chandelier_active` bool      | Per-trial: `false` at trade open; flips `true` once `float_pnl_pips >= chandelier_activate_pips` |

None of these are cross-trial; all reset at `trade_open`. **Miss any
one and the next trade will inherit stale state — silent re-fire bug
shape exactly matching the one breakeven validation caught earlier
today.** Phase 4 build log will enumerate the reset line numbers.

## Rust logic sketch (for phase 4)

Skeleton, placed after the existing trailing block (~line 300 in
`trade_full.rs`), *before* the breakeven block — so breakeven's
`max(sl_prev, new_be_sl)` composition picks up any tighter chandelier
SL in the same bar:

```rust
// === Chandelier stop ===
if chandelier_enabled != 0 && chandelier_atr_mult > 0.0 {
    // Update peak / trough each bar regardless of activation state.
    if direction == DIR_BUY {
        chandelier_peak_high = chandelier_peak_high.max(sb_high);
    } else {
        chandelier_trough_low = chandelier_trough_low.min(sb_low);
    }

    // Arm once float-PnL crosses activate threshold.
    if !chandelier_active {
        if float_pnl_pips >= chandelier_activate_pips {
            chandelier_active = true;
        }
    }

    if chandelier_active {
        let chand_dist = chandelier_atr_mult * atr_pips * pip_value;
        if direction == DIR_BUY {
            let raw_sl = chandelier_peak_high - chand_dist;
            // Side-of-price guard — the v2 trailing fix pattern.
            if raw_sl < sb_low {
                sl = sl.max(raw_sl);   // ratchet: only tightens.
            }
        } else {
            let raw_sl = chandelier_trough_low + chand_dist;
            if raw_sl > sb_high {
                sl = sl.min(raw_sl);
            }
        }
    }
}
```

Exit accounting: if `sl` got overwritten by the chandelier update and
this bar stops out via `sl`, set `exit_code = EXIT_CHANDELIER` (= 7).
Pattern mirrors trailing's `EXIT_TRAILING` attribution at
`trade_full.rs:339-367`.

## Codex independent slot map

*Not requested.* The knob-code-map reference document is already the
canonical layer-by-layer map Codex would re-derive; re-asking would
produce a cached paraphrase. Skipping Codex here reserves the
second-opinion budget for phase 6 (validate-forex-knob) where blind
audit matters more than at planning.

If phase 4 reveals a layer that doesn't match this map, *that* is the
signal to pull Codex in for a sanity check — not now.
