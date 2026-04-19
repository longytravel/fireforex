# Phase 2 — Partial close code trace

End-to-end trace of the `partial.{pct, trigger, test}` knob. Line numbers are
as of commit current on 2026-04-19.

## 1. Schema

**File:** `ff/defaults/complexity.py:208-219`

```python
def _build_partial(level: int, r: dict) -> Group:
    trig_hi = max(15.0, float(r["fixed_sl_pips"]["max"]) * 0.8)
    return Group(
        test=Choice([True, False]),
        on_value=True,
        when_on={
            "pct":     FloatRange(20.0, 75.0, scale="linear",
                                  step=_float_step(level, 20.0, 75.0)),
            "trigger": FloatRange(5.0, trig_hi, scale="log",
                                  step=_float_step(level, 5.0, trig_hi)),
        },
    )
```

- Paths in the schema tree: `engine.partial.test`,
  `engine.partial.when_on.pct`, `engine.partial.when_on.trigger`.
- `trigger` upper bound is 80 % of the max SL in pips (or 15, whichever is
  larger). **No cross-constraint against the TP range** — the sampler is
  free to draw `trigger > tp_distance_pips`.

## 2. Sampler

**File:** `ff/sampler.py` (no partial-specific logic)

Standard `Group` sampling — `test` is drawn from the Choice; if `on_value`
matches (True), the `when_on` dict is sampled. Otherwise the `when_on` values
are left out of the trial dict and the encoding layer's `slot_if_on`
returns a default (0 in practice).

## 3. Encoding

**File:** `ff/defaults/complexity.py:454-465` (mapping builder used by the
complexity presets)

```python
if "partial" in engine_schema:
    mapping.append((bc.PL_PARTIAL_ENABLED,
                    enc.slot_bool_to_int(("engine", "partial", "test"))))
    mapping.append((bc.PL_PARTIAL_PCT, enc.slot_if_on(
        ("engine", "partial", "test"),
        ("engine", "partial", "when_on", "pct"),
    )))
    mapping.append((bc.PL_PARTIAL_TRIGGER, enc.slot_if_on(
        ("engine", "partial", "test"),
        ("engine", "partial", "when_on", "trigger"),
    )))
```

Complex01 hard-codes the same mapping at `eas/complex01.py:93-101`.

## 4. Rust entry (parse slots)

**File:** `core/src/lib.rs:227-229`

```rust
let partial_en  = params[param_layout_s[PL_PARTIAL_ENABLED] as usize] as i64;
let partial_pct = params[param_layout_s[PL_PARTIAL_PCT] as usize];
let partial_trig = params[param_layout_s[PL_PARTIAL_TRIGGER] as usize];
```

Passed into `simulate_trade_full` at **`core/src/lib.rs:366-368`** as
`partial_en, partial_pct, partial_trig`. Slot indices are registered in
`core/src/constants.rs:63-65`:

```rust
pub const PL_PARTIAL_ENABLED: usize = 17;
pub const PL_PARTIAL_PCT:     usize = 18;
pub const PL_PARTIAL_TRIGGER: usize = 19;
```

## 5. Rust use (trade simulation)

**File:** `core/src/trade_full.rs`

### 5a. Function signature

```rust
// lines 42-44
partial_enabled: i64,
partial_pct: f64,
partial_trigger_pips: f64,
```

### 5b. State initialisation

```rust
// line 76-77
let mut position_pct = 1.0_f64;
let mut partial_done = false;
```

`partial_done` is a **once-only** guard. `position_pct` starts at 100 % and
is decremented when partial fires.

### 5c. Float pnl calculation (per sub-bar)

```rust
// lines 160-170
let (float_pnl_pips, _worst_pnl_pips) = if is_buy {
    (
        (sb_high - actual_entry) / pip_value,    // ← uses sb_high
        (sb_low  - actual_entry) / pip_value,
    )
} else {
    (
        (actual_entry - sb_low)  / pip_value,    // ← uses sb_low
        (actual_entry - sb_high) / pip_value,
    )
};
```

**Finding 2a.** Trigger eligibility uses `sb_high` (for a long) — the peak
price inside the sub-bar. This is intentional across breakeven, trailing
and partial. It models "did the bar ever touch the trigger level".

### 5d. Partial body

```rust
// lines 285-310
if partial_enabled > 0 && !partial_done {
    if float_pnl_pips >= partial_trigger_pips {
        partial_done = true;
        let close_pct = partial_pct / 100.0;
        let partial_pnl = if is_buy {
            (sb_close - slippage_price - actual_entry) / pip_value * close_pct
        } else {
            (actual_entry - sb_close - slippage_price) / pip_value * close_pct
        };
        // Deduct proportional sell spread for partial close
        let partial_spread_cost = if !is_buy {
            let sb_spread = if sb < sub_spread.len() { ... } else { 0.0 };
            sb_spread / pip_value * close_pct
        } else {
            0.0
        };
        realized_pnl_pips += partial_pnl - partial_spread_cost;
        position_pct -= close_pct;
    }
}
```

**Finding 2b (critical).** The realisation price is **`sb_close`**, not the
trigger level. Long pnl realised on the partial:

```
realized_partial = (sb_close - slippage - actual_entry) / pip_value × pct/100
```

On a sub-bar where `sb_high` crosses the trigger but `sb_close` closes well
above the trigger (strong trend sub-bar), the realised partial pnl is
**greater** than it would be for a real limit order sitting at the trigger
level.

**Finding 2c (critical).** The partial block runs at lines 285-310. The
SL / TP block runs at lines 312-361. **Partial is checked before TP within
the same sub-bar iteration.** On a sub-bar where both
`sb_high >= partial_trigger` and `sb_high >= tp_price` are satisfied:

1. Partial fires at `sb_close`, realises `(sb_close - entry) × pct/100`.
2. `position_pct -= close_pct`.
3. TP check fires on the remainder at `tp_price`, realises
   `(tp_price - entry) × (1 - pct/100)`.

Final reported pnl =
`(sb_close - entry) × pct/100  +  (tp - entry) × (1 - pct/100)`.

If `sb_close > tp` (i.e. the trigger was above tp and the bar closed above
the trigger too), the reported pnl exceeds the honest tp-only outcome by

```
(sb_close - tp) × pct/100
```

### 5e. Subsequent exits

SL, TP, max-bars, and stale exits all multiply their pnl by `position_pct`,
so the partial reduction flows through correctly. The `EXIT_*` code
recorded reflects only the *final* exit; the partial step is recorded only
in `realized_pnl_pips`.

## 6. Long / short symmetry check

| Step                 | Long                                          | Short                                         |
|----------------------|-----------------------------------------------|-----------------------------------------------|
| Trigger condition    | `(sb_high − actual_entry)/pip ≥ trigger`      | `(actual_entry − sb_low )/pip ≥ trigger`      |
| Partial pnl          | `(sb_close − slippage − entry)/pip × pct`     | `(entry − sb_close − slippage)/pip × pct`     |
| Spread cost          | 0 (entry spread paid at entry)                | `sb_spread/pip × pct` (exit-side ask)         |
| position_pct update  | `position_pct -= close_pct`                   | same                                          |

Symmetry is preserved: long / short signs mirror correctly, entry/exit
spread cost bookkeeping is consistent with the rest of the engine's
bid-array convention.

## 7. Silent-no-op check (EXEC_BASIC-shape bug)

Every hop is populated:
- schema → sampler → trial dict ✓
- trial dict → param vector (`slot_bool_to_int`, `slot_if_on`) ✓
- param vector → Rust locals (`partial_en`, `partial_pct`, `partial_trig`) ✓
- locals → `simulate_trade_full` arguments ✓
- arguments → conditional body at lines 285-310 ✓

**No silent no-op.** The knob is wired end-to-end.

## 8. Summary of findings

| ID  | Concern                                                              | Severity |
|-----|----------------------------------------------------------------------|----------|
| 2a  | Trigger uses `sb_high`; realisation uses `sb_close`.                 | Medium   |
| 2b  | On strong bars, realised partial pnl exceeds trigger-level fill.     | Medium   |
| 2c  | Partial checked BEFORE TP. When `trigger > tp`, partial over-fires.  | **High** |
| 2d  | Sampler has no cross-constraint stopping `trigger > tp_pips`.        | Medium   |
| 2e  | `EXIT_TP`, `EXIT_SL` codes never reflect that a partial preceded.    | Low      |

2c is the hypothesis from Phase 1 — confirmed by line-level code reading.
2a and 2b compound 2c: the partial not only fires when TP should have, it
realises at a price (sb_close) which is typically further from entry than
tp_price, inflating realised pnl by `(sb_close - tp) × pct/100` per
affected trade.

## 9. Codex independent trace

(Pending — this section will be populated by a Codex GPT-5.4 high reasoning
run without priming on the findings above. See Phase 2 instruction in the
skill.)
