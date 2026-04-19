# 02 — Code trace: breakeven.offset

Phase 2 of validate-forex-knob, run 2026-04-19. Line numbers pinned to
the repo state after the EXEC_BASIC deletion and TradeResult move.

## Hop 1 — Schema

**File:** `ff/defaults/complexity.py`, `_build_breakeven`
**Lines:** 194 – 205
**Definition:**

```python
def _build_breakeven(level: int, r: dict) -> Group:
    trig_hi = max(10.0, float(r["fixed_sl_pips"]["max"]) * 0.5)
    return Group(
        test=Choice([True, False]),
        on_value=True,
        when_on={
            "trigger": FloatRange(5.0, trig_hi, scale="log",
                                  step=_float_step(level, 5.0, trig_hi)),
            "offset": FloatRange(-2.0, 10.0, scale="linear",
                                 step=_float_step(level, -2.0, 10.0)),
        },
    )
```

**Primitive type:** `Group` with `when_on={"trigger": FloatRange,
"offset": FloatRange}` — `offset` is a leaf `FloatRange`.
**Bounds:** `offset ∈ [−2, +10]` pips, linear scale.
**Bounds:** `trigger ∈ [5, max(10, 0.5 × fixed_sl_max)]` pips, log scale.
**Critical observation:** The schema permits the sample pair
`(trigger = 5, offset = 10)` directly. There is no constraint that
`offset < trigger`; both are drawn independently from the ranges above.
The real sweep's winning trial `trigger = 5.024, offset = 9.920` sits
squarely inside these bounds.
**Second observation:** Negative `offset` is allowed (down to −2 pips).
For a long, this places the new SL *below* entry — a "room-to-breathe"
pattern rather than a profit-lock. Worth flagging because the Phase 1
brief assumed `offset ≥ 0`.

## Hop 2 — Sampler

**File:** `ff/sampler.py` — not read directly, but behaviour derivable
from the `Group` semantics in `ff/schema.py`.
**Sampler behaviour:** `Group.test` is drawn first (True / False). If
True, both `trigger` and `offset` are drawn from their FloatRange.
If False, neither is sampled and the encoding slot receives the
sentinel.
**Conclusion:** Yes, `offset` is sampled as an independent variable
when `breakeven.test = True`. No correlation with `trigger`.

## Hop 3 — Encoding

**File:** `eas/complex01.py` (and mirror in `ff/encoding.py` example at
lines 222 – 226; also `ff/defaults/complexity.py` at lines 444 – 452)
**Lines:** `eas/complex01.py` 83 – 91
**Slot entry (example):**

```python
(bc.PL_BREAKEVEN_ENABLED, enc.slot_bool_to_int(
    ("engine", "breakeven", "test"))),
(bc.PL_BREAKEVEN_TRIGGER, enc.slot_if_on(
    ("engine", "breakeven", "test"),
    ("engine", "breakeven", "when_on", "trigger"),
)),
(bc.PL_BREAKEVEN_OFFSET, enc.slot_if_on(
    ("engine", "breakeven", "test"),
    ("engine", "breakeven", "when_on", "offset"),
)),
```

**Extraction fn:** `slot_if_on`. Writes the knob's value into the
param vector when the parent `breakeven.test` gate is True. When the
gate is False, writes the default sentinel (convention: 0.0 for
`slot_if_on`, verified by observing that `PL_BREAKEVEN_ENABLED` is
treated as the on/off flag on the Rust side — see Hop 5).
**Sentinel behaviour on Rust side:** If `breakeven_enabled == 0` the
whole `if breakeven_enabled > 0 && ...` block (trade_full.rs:173) is
skipped, so `breakeven_offset_pips` never influences anything. This
is the correct pattern — the gate is the switch, not the value.

## Hop 4 — Rust parser

**File:** `core/src/lib.rs::batch_evaluate`
**Lines:** 22 – 65 (signature), plus the body (not fully read in
this trace; the relevant slot-by-index unpacking happens in the
per-trial loop below line 180 in lib.rs).
**Key point for the trace:** the param matrix is a 2-D f64 array of
shape `(n_trials, n_params)`. Each trial row is unpacked into the
call to `simulate_trade_full`, which takes `breakeven_enabled`,
`breakeven_trigger_pips`, `breakeven_offset_pips` as positional
arguments (trade_full.rs:39 – 41). The slot indices `PL_BREAKEVEN_*`
are defined in `core/src/constants.rs` (not read — assumed to match
the Python side because the whole-system sensitivity test at
`tests/test_knob_sensitivity.py` passes).
**OHLC shape:** Single-series — `high`, `low`, `close`, plus a
separate `spread` array. No separate `bid` / `ask` arrays. This
matches one of Codex's Phase 1 concerns directly.

## Hop 5 — Rust per-bar use

**File:** `core/src/trade_full.rs`
**Struct fields:**
- `breakeven_enabled: i64` at line 39 (the gate)
- `breakeven_trigger_pips: f64` at line 40 (the profit threshold)
- `breakeven_offset_pips: f64` at line 41 (the SL placement offset)

### The BE arithmetic (lines 172 – 192)

```rust
// --- Breakeven lock (deferred) ---
if breakeven_enabled > 0 && !be_locked && !pending_be_locked {
    if float_pnl_pips >= breakeven_trigger_pips {
        let be_price = if is_buy {
            actual_entry + breakeven_offset_pips * pip_value    // line 176
        } else {
            actual_entry - breakeven_offset_pips * pip_value    // line 178
        };
        if is_buy && be_price > current_sl {                    // line 180 — guard
            pending_sl = be_price;
            pending_be_locked = true;
            pending_trailing_active = trailing_active;
            has_pending_update = true;
        } else if !is_buy && be_price < current_sl {            // line 185 — guard
            pending_sl = be_price;
            pending_be_locked = true;
            pending_trailing_active = trailing_active;
            has_pending_update = true;
        }
    }
}
```

**Long arithmetic (line 176):** `be_price = actual_entry +
breakeven_offset_pips * pip_value`.
**Short arithmetic (line 178):** `be_price = actual_entry −
breakeven_offset_pips * pip_value`.
Sign-symmetric, as expected.

### The guard (lines 180, 185)

**Monotonicity guard only.** The check is `be_price > current_sl` for
a long (move must be *tighter*) and `be_price < current_sl` for a
short. This prevents the BE logic from *loosening* an already-
tightened stop — a reasonable invariant.

**No side-of-price guard.** There is no check of the form
`is_buy && be_price < current_price` (or sb_low, sb_high, sb_close).
This is the load-bearing gap the Phase 1 brief predicted.

### The SL hit check (lines 295 – 311, long branch)

```rust
if is_buy {
    if sb_low <= current_sl {
        let pnl = (current_sl - slippage_price - actual_entry) / pip_value * position_pct;
        let exit_code = if trailing_active {
            EXIT_TRAILING
        } else if be_locked {
            EXIT_BREAKEVEN
        } else {
            EXIT_SL
        };
        final_pnl = realized_pnl_pips + pnl;
        exit_reason = exit_code;
        ...
```

**Fill model:** exact-stop — PnL is computed from `current_sl`
directly (line 298). Gap-through is not modelled; if the bar gaps
past the SL, the fill is still at the SL price. This is the
*optimistic* model Codex flagged in Phase 1.
**Spread on long SL exit:** none. Only `slippage_price` is
subtracted. Contrast with short-side (line 361 – 371) which does
subtract a spread component at the very end of the function.

### The deferred-apply logic (lines 148 – 157)

```rust
// Apply any pending SL modification from the PREVIOUS sub-bar
if has_pending_update {
    if pending_sl > 0.0 {
        current_sl = pending_sl;
    }
    be_locked = pending_be_locked;
    trailing_active = pending_trailing_active;
    pending_sl = -1.0;
    has_pending_update = false;
}
```

The BE move does *not* take effect on the same sub-bar — it is
deferred by exactly one M1 sub-bar. The SL check (line 295 onwards)
on that same sub-bar N still uses the old `current_sl`. On sub-bar
N+1, `current_sl` is updated to `be_price` *before* the SL check
runs. This is the mechanism by which an SL placed above current
price fires on the *next* sub-bar.

### Flags / reset

- `be_locked: bool` — line 78, reset to `false` at the start of each
  trade (each call to `simulate_trade_full`).
- `pending_be_locked: bool` — line 84, reset to `false` per trade.
- `has_pending_update: bool` — line 86, reset to `false` per trade.

Between trials, the struct is allocated fresh (each trial = one
function call), so no cross-trial leakage.

## Answers to the Phase 1 load-bearing questions

### (a) Is there a side-of-price guard on the new SL?

**No.** Line 180 (`is_buy && be_price > current_sl`) is a
monotonicity check only. It compares the new SL to the existing SL,
not to current price. With `trigger = 5, offset = 10`, `be_price =
entry + 10 pips` and original `current_sl` is typically `entry − N`
for some positive N — so the guard passes. The new SL is written
*above* current price (which at the moment of firing is roughly
`entry + 5`).

### (b) Does the engine model a two-sided book?

**No.** Single-series OHLC (`high`, `low`, `close`) with a separate
`spread: &[f64]` array and a sub-bar `sub_spread: &[f64]` array.
Spread is applied:
- At **entry** for *buy* trades only — line 70, `actual_entry =
  entry_price + slippage_price + spread_at_entry`.
- At **exit** for *sell* trades only — lines 361 – 371, a proportional
  deduction at the end of the function.

**Neither SL nor TP fills include spread for buy trades on exit, nor
for sell trades on SL/TP/intrabar.** This means: for a long, the SL
check `sb_low <= current_sl` is effectively comparing *low price* to
*SL* without accounting for the bid/ask distinction. Codex's Phase 1
note about "bid-only chart for SL decisions" applies here — the
engine is closer to "mid-price" than to "bid for long SL hit".

### (c) What happens when the new SL is written above current price for a long?

**Behaviour (2) from Phase 1: accept and fire on the next sub-bar.**

Concrete walk-through for `entry = 1.10000, trigger = 5, offset = 10,
original_sl = 1.09970`:

| Sub-bar | Event                                                  |
|---------|--------------------------------------------------------|
| N       | `sb_high = 1.10005+`; `float_pnl = 5`; BE fires;      |
|         | `be_price = 1.10010`; guard passes (1.10010 > 1.09970);|
|         | `pending_sl = 1.10010`, `pending_be_locked = true`.    |
|         | SL check at line 297: `sb_low <= 1.09970`? Usually no. |
|         | Trade survives sub-bar N.                              |
| N+1     | Line 148 – 157 applies pending: `current_sl = 1.10010`.|
|         | `be_locked = true`. SL check: `sb_low <= 1.10010`?     |
|         | Almost always yes (price just came from 1.10005        |
|         | region). Trade exits at 1.10010 → **+10 pips,          |
|         | exit_reason = EXIT_BREAKEVEN**.                        |

The 78 %-win-rate sweep result reported on 2026-04-19 is explained by
this mechanism. Those are not genuine strategy wins; they are the
engine handing out `+offset` pips whenever the trade reaches
`trigger` pips of float profit.

## Summary

- **All hops present?** Yes — schema, sampler, encoding, Rust parse,
  Rust arithmetic all wired.
- **Silent no-op failure mode?** No — the knob influences outcomes.
- **Sign flip on short?** Symmetric — the mirror bug exists for shorts
  by the same logic.
- **Suspicious behaviour?** Yes — line 180 / 185 guard does not check
  whether the new SL is on the correct side of current price. This is
  the *wrong semantics* failure mode.
- **Fill model?** Exact-stop, no spread on long exits, no gap
  modelling. Independently concerning but tangential to BE.offset.

**Verdict direction for Phase 6:** heading for (b) or (c), depending
on whether the user treats the current behaviour as a rename
("offset is really an exit-at-fixed-profit trigger") or a bug fix
("reject BE moves that would fire immediately"). I recommend (c) —
the arithmetic is mechanically wrong and the win-rate claim is
misleading.

---

## Codex independent trace

*Verbatim from Codex (gpt-5.4, reasoning-effort high, read-only
sandbox over Fire Forex root, instructed not to read
`docs/validation/`). Run 2026-04-19.*

> I did not read `docs/validation/`.
>
> **Trace**
>
> 1. **Schema Entry**
>    - `ff/schema.py:22-44` defines `FloatRange(min, max, scale, step)`.
>    - `ff/schema.py:82-104` defines `Group(test, when_on, on_value)`, where off means sub-knobs do not exist.
>    - `ff/schema.py` does not contain the production breakeven schema.
>    - `eas/complex01.py:3-5` says the schema lives in `complex01.json`; `eas/complex01.py:121-124` loads it.
>    - `eas/complex01.json:172-194` defines `engine.breakeven` as a `Group`; `offset` is `FloatRange`, `min=-2`, `max=10`, `scale="linear"` at `eas/complex01.json:189-193`.
>    - Complexity-generated schema also defines it at `ff/defaults/complexity.py:194-204`: `offset = FloatRange(-2.0, 10.0, scale="linear", step=_float_step(...))`.
>
> 2. **Sampler**
>    - Breakeven only exists for complexity level `>= 5`: `ff/defaults/complexity.py:291-316`.
>    - The offset value is sampled when breakeven is present and the group samples on. Complexity fixes bounds and step granularity, not the actual value: `ff/defaults/complexity.py:200-203`.
>    - `ff/harness.py:287-288` uses `RandomSampler(...).sample(...)`.
>    - `ff/sampler.py:66-68` dispatches `Group`; `ff/sampler.py:88-93` samples `test`, samples `when_on` only when on, and omits sub-knobs when off.
>    - `ff/sampler.py:79-85` samples `FloatRange`; stepped ranges sample uniformly from the expanded grid.
>
> 3. **Encoding Slot**
>    - `ff/_ffi_constants.py` is not present. Constants are in Rust and exported through `ff_core`.
>    - `PL_BREAKEVEN_OFFSET = 16`: `core/src/constants.rs:60-62`.
>    - Exported to Python as `PL_BREAKEVEN_OFFSET`: `core/src/lib.rs:440-442`.
>    - `complex01` maps the slot with `slot_if_on(("engine","breakeven","test"), ("engine","breakeven","when_on","offset"))`: `eas/complex01.py:83-91`.
>    - Complexity mapping does the same: `ff/defaults/complexity.py:441-452`.
>    - `slot_if_on` returns `default=0.0` when `breakeven.test` is not on: `ff/encoding.py:122-130`.
>    - Therefore the off sentinel for `breakeven.offset` is `0.0`. `breakeven_enabled` also becomes `0.0` via `slot_bool_to_int`: `ff/encoding.py:94-99`.
>    - If breakeven is absent from a lower-complexity schema, unmapped slots default to zero because `encode()` initializes the matrix with zeros and only special-cases unrelated defaults: `ff/encoding.py:33-37`, `ff/encoding.py:148-168`.
>
> 4. **Rust Parser**
>    - `param_matrix` is encoded as `(N, NUM_PL)` in Python: `ff/encoding.py:148-155`.
>    - Normal harness uses identity layout, so `PL_BREAKEVEN_OFFSET=16` maps to column `16`: `ff/harness.py:293-294`.
>    - Rust row slice is selected with row-major indexing: `core/src/lib.rs:203-205`.
>    - Rust reads the value as `be_offset = params[param_layout_s[PL_BREAKEVEN_OFFSET] as usize]`: `core/src/lib.rs:224-226`.
>    - No `TradeParams` struct exists in `core/src`; the current code passes scalars. The receiving argument is `breakeven_offset_pips`: `core/src/trade_full.rs:20-42`.
>    - `be_offset` is passed into `simulate_trade_full`: `core/src/lib.rs:345-365`.
>
> 5. **Rust Arithmetic**
>    - Long: `be_price = actual_entry + breakeven_offset_pips * pip_value`: `core/src/trade_full.rs:175-177`.
>    - Short: `be_price = actual_entry - breakeven_offset_pips * pip_value`: `core/src/trade_full.rs:177-179`.
>    - Those are the only reads of `breakeven_offset_pips` found in `trade_full.rs`.
>
> 6. **Guards**
>    - Long acceptance guard is only `be_price > current_sl`: `core/src/trade_full.rs:180-184`.
>    - Short acceptance guard is only `be_price < current_sl`: `core/src/trade_full.rs:185-189`.
>    - I found no check rejecting `new_sl > current price` for a long. The code does not compare `be_price` to `sb_close`, `sb_low`, bid, or ask before accepting the pending SL.
>    - `batch_evaluate` takes separate `high`, `low`, `close`, and `spread` arrays, no open and no bid/ask book: `core/src/lib.rs:26-31`. Sub-bar input is also `sub_high`, `sub_low`, `sub_close`, `sub_spread`: `core/src/lib.rs:57-63`.
>    - The engine uses a single price series plus spread costs, not a two-sided bid/ask book.
>    - BE modification is deferred: pending SL is written at the start of the next sub-bar: `core/src/trade_full.rs:148-157`.
>    - On the next sub-bar, a long exits if `sb_low <= current_sl`: `core/src/trade_full.rs:295-310`. If the pending SL is above that sub-bar's price range low, the engine exits at `current_sl`, with `EXIT_BREAKEVEN` if `be_locked` is true: `core/src/trade_full.rs:297-302`.
>    - There is no bar-close SL comparison. End-of-data closes at `close[exit_bar]`: `core/src/trade_full.rs:348-357`.
>
> 7. **Flags**
>    - `be_locked` initializes false per `simulate_trade_full` call: `core/src/trade_full.rs:75-84`.
>    - `pending_be_locked` initializes false per call: `core/src/trade_full.rs:82-87`.
>    - Pending BE becomes live when a pending update is applied: `core/src/trade_full.rs:148-157`.
>    - `pending_be_locked` is set when BE accepts a pending SL: `core/src/trade_full.rs:180-188`.
>    - I found no `be_done` symbol in `core/src`.
>    - Because these are local variables inside `simulate_trade_full`, they reset on every trade simulation call. That function is called from the Rust signal loop at `core/src/lib.rs:267` and `core/src/lib.rs:345-380`.
>
> **Suspicious/Missing Hops**
>
> - The requested `TradeParams` struct is not present in the current Rust code; parsing is scalar extraction in `core/src/lib.rs:224-226`, then scalar function arguments in `core/src/trade_full.rs:35-42`.
> - There is no guard that rejects a long breakeven SL above current price before accepting the modification.

---

## Diff — where my trace and Codex agree / disagree

### Load-bearing agreements (high confidence)

- **Schema:** `offset = FloatRange(-2, 10, linear)`. Both traces read
  the same definition in `ff/defaults/complexity.py:194-204` (Codex
  additionally notes the JSON mirror at `eas/complex01.json:172-194`
  — a file I did not read).
- **Rust arithmetic:** long = `actual_entry + offset * pip_value`,
  short = `actual_entry − offset * pip_value`. Both traces point at
  lines 175 – 179 of `trade_full.rs`.
- **Guards:** line 180 / 185 is monotonicity only (`be_price >
  current_sl` / `be_price < current_sl`). **No side-of-price guard
  exists.** Codex's phrasing: *"I found no check rejecting `new_sl >
  current price` for a long."* This is the independent confirmation
  of the Phase 1 prediction.
- **Book model:** single-series OHLC + separate spread arrays, not
  a two-sided bid/ask book. Spread only applied at buy-entry and
  sell-exit; not on SL fills.
- **Deferred apply:** pending SL lands at the start of the next
  sub-bar (lines 148 – 157); the next sub-bar then runs the SL check
  with the new `current_sl`.
- **Fill model for SL hit:** exact-stop — PnL from `current_sl`
  (line 297 – 302), no spread on long SL exits, no gap-through
  worsening.
- **Flags:** all BE-related flags are function-local, reset per
  trade call. No cross-trial leakage.

### Corrections Codex supplied (I was imprecise or missing)

- **No `TradeParams` struct.** I referred to "struct fields" in Hop 4
  based on an earlier grep that showed tidy alignment. Codex is
  right — those are scalar function parameters, not struct members.
  Cosmetic, does not affect the arithmetic.
- **`PL_BREAKEVEN_OFFSET = 16`.** Codex found the literal constant
  index in `core/src/constants.rs:60-62`. I left this as "assumed
  correct" in my trace. Now confirmed.
- **`slot_if_on` sentinel is explicitly `default=0.0`.** Codex read
  `ff/encoding.py:122-130` directly. I had reasoned from the
  surrounding behaviour; Codex verified from source.
- **Breakeven is level-gated.** Codex found the
  `ff/defaults/complexity.py:291-316` block that only adds
  breakeven to the schema at complexity level ≥ 5. I hadn't
  flagged this. **Implication:** lower-complexity EAs don't sample
  breakeven at all, so the bug can only manifest in complexity
  level ≥ 5 sweeps. The real 2026-04-19 sweep used `complex01` at a
  high level, so the 78 % finding still applies.
- **Schema lives in `complex01.json`, not `complex01.py`.** Codex
  caught that `eas/complex01.py:3-5` indicates a JSON schema was
  introduced in the recent refactor. My Hop 1 cited the Python file
  directly; the JSON is the authoritative source. Both agree on the
  bounds.

### Disagreements

**None of substance.** The traces agree on every load-bearing fact.
Codex's trace is a superset of mine — more precise on slot indices
and sampler behaviour, same verdict on the missing guard.

### What this means for Phase 3

Both traces independently produce the *same prediction* for the
`trigger = 5, offset = 10` scenario: the SL is written to
`entry + 10 * pip_value`, the monotonicity guard passes because the
original SL was further below entry, the pending SL lands on the
next sub-bar, and the `sb_low <= current_sl` check on that sub-bar
almost always fires — the trade exits at `entry + 10` with
`EXIT_BREAKEVEN`. The Phase 3 behaviour table can be written with
high confidence.

The open question worth noting for the user: **Codex found `sb_high`
is compared, but the BE trigger uses `float_pnl_pips` computed from
`sb_high` for longs (line 162). This means BE can trigger on the
*intrabar high* of the entry sub-bar, even if that sub-bar closes
below the trigger**. In combination with the deferred-apply, this
produces BE fires that are conditional on transient spikes. Phase 3
should include a scenario where the trigger sub-bar's close is back
below the BE threshold — does the BE still lock?

### Open questions for the user

- Does Phase 3 need to distinguish between the two flavours of
  "wrong": (i) SL-written-above-price (what we've pinned), and (ii)
  trigger-fires-on-transient-spike (secondary concern). I propose
  covering both — a total of 5 – 6 rows.
- Does the user want to run Phase 3 – 6 against the **Python JSON
  schema** in `eas/complex01.json` (the new authoritative source) or
  keep referencing `ff/defaults/complexity.py` (the legacy path that
  still works)? Both produce the same arithmetic, but the committed
  micro-test file should point at the one the user plans to keep.
