# 02 — Code trace: `engine.chandelier`

## Trace (my own read)

### 1. Schema entry

`eas/complex01.json:222-245` — `"chandelier"` Group, sibling of
`trailing` / `breakeven` / `partial` / `stale`.

```json
"chandelier": {
  "type": "Group",
  "test": { "type": "Choice", "values": [true, false] },
  "on_value": true,
  "when_on": {
    "activate": { "type": "FloatRange", "min": 5,   "max": 25,  "scale": "log" },
    "atr_mult": { "type": "FloatRange", "min": 2.0, "max": 4.0, "scale": "linear" }
  }
}
```

Bounds: `activate ∈ [5, 25]` pips, log scale; `atr_mult ∈ [2.0, 4.0]`,
linear. Group `test` toggles the whole block.

### 2. Encoding slot

Three tuples in `eas/complex01.py:119-127`:

```python
(bc.PL_CHANDELIER_ENABLED, enc.slot_bool_to_int(("engine", "chandelier", "test"))),
(bc.PL_CHANDELIER_ACTIVATE, enc.slot_if_on(
    ("engine", "chandelier", "test"),
    ("engine", "chandelier", "when_on", "activate"),
    default=-1.0,
)),
(bc.PL_CHANDELIER_ATR_MULT, enc.slot_if_on(
    ("engine", "chandelier", "test"),
    ("engine", "chandelier", "when_on", "atr_mult"),
    default=-1.0,
)),
```

- `slot_bool_to_int` (`ff/encoding.py:98-105`): returns `1.0` if test
  value is `True`, else `0.0`.
- `slot_if_on(default=-1.0)` (`ff/encoding.py:126-135`): if test value
  `== True`, returns float at value_path; else returns the passed
  default `-1.0`.

**Sentinel when Group is off:**
- `PL_CHANDELIER_ENABLED` → `0.0`
- `PL_CHANDELIER_ACTIVATE` → `-1.0`
- `PL_CHANDELIER_ATR_MULT` → `-1.0`

Matches the slot-map intention.

### 3. Rust parser

**Constants** — `core/src/constants.rs:91-99`:

```rust
pub const PL_SIGNAL_P9: usize = 36;
pub const NUM_SIGNAL_PARAMS: usize = 10;

// Chandelier stop (peak-based ATR trailing, distinct from TRAIL_ATR_CHANDELIER
// which is actually a distance-from-current-high trail). Added 2026-04-19.
pub const PL_CHANDELIER_ENABLED: usize = 37;
pub const PL_CHANDELIER_ACTIVATE: usize = 38;
pub const PL_CHANDELIER_ATR_MULT: usize = 39;
```

`EXIT_CHANDELIER: i64 = 7` at `constants.rs:31`.

**Parse** — `core/src/lib.rs:234-236`:

```rust
let chandelier_en = params[param_layout_s[PL_CHANDELIER_ENABLED] as usize] as i64;
let chandelier_activate = params[param_layout_s[PL_CHANDELIER_ACTIVATE] as usize];
let chandelier_atr_m = params[param_layout_s[PL_CHANDELIER_ATR_MULT] as usize];
```

**Thread into sim call** — `core/src/lib.rs:384-386`:

```rust
chandelier_en,
chandelier_activate,
chandelier_atr_m,
commission_pips,
```

Inserted between `stale_atr` and `commission_pips`, matching the
`simulate_trade_full` signature order.

**pyo3 exports** — `core/src/lib.rs:461-463`:

```rust
m.add("PL_CHANDELIER_ENABLED", PL_CHANDELIER_ENABLED)?;
m.add("PL_CHANDELIER_ACTIVATE", PL_CHANDELIER_ACTIVATE)?;
m.add("PL_CHANDELIER_ATR_MULT", PL_CHANDELIER_ATR_MULT)?;
```

### 4. Rust arithmetic

**Signature** — `core/src/trade_full.rs:49-51`:

```rust
chandelier_enabled: i64,
chandelier_activate_pips: f64,
chandelier_atr_mult: f64,
```

**State init** (per-trade) — `core/src/trade_full.rs:80-88`:

```rust
let mut chandelier_active = false;
let mut chandelier_peak_high = actual_entry;
let mut chandelier_trough_low = actual_entry;
...
let mut pending_chandelier_active = false;
```

Peak/trough seeded at `actual_entry` (the spread-adjusted fill),
matching the build log note.

**Pending-apply** — `core/src/trade_full.rs:158` (adds to the
existing apply block):

```rust
chandelier_active = pending_chandelier_active;
```

Lifts at the same sub-bar boundary as `be_locked` and
`trailing_active`. ✓

**Per-sub-bar block** — `core/src/trade_full.rs:~288-358` (inserted
after trailing, before partial close):

```rust
if chandelier_enabled != 0
    && chandelier_atr_mult > 0.0
    && chandelier_activate_pips >= 0.0
{
    // Peak / trough update (intrabar, unconditional once gated on).
    if is_buy {
        if sb_high > chandelier_peak_high { chandelier_peak_high = sb_high; }
    } else {
        if sb_low < chandelier_trough_low { chandelier_trough_low = sb_low; }
    }

    let armed_now = chandelier_active
        || pending_chandelier_active
        || float_pnl_pips >= chandelier_activate_pips;

    if armed_now {
        let chand_dist = chandelier_atr_mult * atr_pips * pip_value;
        if is_buy {
            let new_sl = chandelier_peak_high - chand_dist;
            let effective_sl = if has_pending_update && pending_sl > 0.0 {
                pending_sl
            } else {
                current_sl
            };
            if new_sl > effective_sl && new_sl < sb_low {
                pending_sl = new_sl;
                ...
                pending_chandelier_active = true;
                has_pending_update = true;
            } else if !has_pending_update {
                pending_chandelier_active = pending_chandelier_active
                    || chandelier_active
                    || float_pnl_pips >= chandelier_activate_pips;
                if pending_chandelier_active {
                    pending_be_locked = be_locked;
                    pending_trailing_active = trailing_active;
                    has_pending_update = true;
                }
            } else {
                pending_chandelier_active = true;
            }
        } else {
            // mirror for short with sb_high, min-ratchet
        }
    }
}
```

Guard comparator: `new_sl < sb_low` (long) / `new_sl > sb_high`
(short) — **v2 trailing-fix pattern**, not the Codex-preferred
"fire immediately" or the earlier breakeven-bug `sb_close` pattern.

Ratchet: `new_sl > effective_sl` (long) / `new_sl < effective_sl`
(short). Monotone-one-way. ✓

**Exit attribution** — `core/src/trade_full.rs:~348-358` and
`~374-384`:

```rust
let exit_code = if chandelier_active {
    EXIT_CHANDELIER
} else if trailing_active {
    EXIT_TRAILING
} else if be_locked {
    EXIT_BREAKEVEN
} else {
    EXIT_SL
};
```

Chandelier takes priority. If both chandelier_active and
trailing_active are true on the fill bar, `EXIT_CHANDELIER` wins.

### 5. Guards / reset

- `chandelier_enabled == 0` → outer-`if` short-circuits the whole
  block (no peak/trough update, no SL write, no arm).
- `chandelier_atr_mult <= 0.0` → outer-`if` also fails. Secondary
  sentinel defence. ✓
- `chandelier_activate_pips < 0.0` → outer-`if` fails. Handles the
  encoder's off-sentinel even if `enabled` were somehow set.
- `chandelier_active` → per-trade bool, initialised `false` at the
  top of `simulate_trade_full`. Not cross-trade.
- `chandelier_peak_high` / `chandelier_trough_low` → per-trade
  floats, initialised at `actual_entry`.
- `pending_chandelier_active` → per-trade bool, `false` at start.

No `!*_done` semantic — chandelier can re-arm on the same trade
after it fires, but since firing exits the trade, re-arming never
happens in practice. Intentional: a chandelier stop is not a
one-shot like breakeven.

### 6. Hazards surveyed

| # | Hazard                                              | Status |
|---|-----------------------------------------------------|--------|
| a | Block skipped entirely when `enabled==0`            | ✅ Outer `if` covers entire peak/trough + arm + SL write. |
| b | Pending lifts `chandelier_active` at same sub-bar boundary as trailing/BE | ✅ Added to apply block line 158. |
| c | Exit priority when both trailing+chandelier active  | ✅ Chandelier wins (explicit `if` ordering). |
| d | Sentinel precedence — `enabled=0` with `mult>0` still writing SL? | ✅ Outer `if` requires **both** `enabled != 0` **and** `atr_mult > 0.0`; either sentinel short-circuits. |
| e | Side-of-price guard: `sb_low` (correct), `sb_close` (bug), or current? | ✅ Uses `sb_low` (long) / `sb_high` (short). Matches v2 trailing pattern, differs from Codex's "fire immediately" intent — conscious choice per phase 1 brief. |

## Codex independent trace

*(Codex `gpt-5.4 high`, 2026-04-19, verbatim)*

Trace complete. No missing hop found.

**Schema Entry**
`eas/complex01.json:222-246` defines `engine.chandelier` as a `Group`.
Its `test` is a boolean `Choice` with values `true/false` at lines
224-230, `on_value: true` at line 231. When on: `activate` is
`FloatRange` min `5`, max `25`, log scale at lines 233-238;
`atr_mult` is `FloatRange` min `2.0`, max `4.0`, linear at lines
239-243.

**Encoding Slot**
`eas/complex01.py:119` maps `PL_CHANDELIER_ENABLED` through
`enc.slot_bool_to_int(("engine", "chandelier", "test"))`.
`ff/encoding.py:98-103` returns `1.0` only when the value equals
`on_value` (`True` by default), else `0.0`.

`PL_CHANDELIER_ACTIVATE` uses `slot_if_on(..., default=-1.0)` at
`eas/complex01.py:120-124`; `PL_CHANDELIER_ATR_MULT` uses the same
helper/default at lines 125-129. `slot_if_on` returns `float(default)`
when the parent test is not on at `ff/encoding.py:126-134`, so parent
group off writes exact sentinel `-1.0` for both activate and ATR
multiplier.

`core/src/constants.rs:31` has `EXIT_CHANDELIER = 7`; lines 106-108
have `PL_CHANDELIER_ENABLED = 37`, `ACTIVATE = 38`, `ATR_MULT = 39`.

**Rust Parser**
`batch_evaluate` starts at `core/src/lib.rs:24`. It reads from the
params row via layout indirection, not raw fixed columns:
`params[param_layout_s[PL_CHANDELIER_ENABLED] as usize]` into
`chandelier_en` at lines 234-236, meaning layout slots `37/38/39`.
These are passed to `simulate_trade_full` at lines 387-389. In the
callee signature, they are positional args 28-30: `chandelier_enabled`,
`chandelier_activate_pips`, `chandelier_atr_mult` at
`core/src/trade_full.rs:49-51`.

**Rust Arithmetic**
Initial state: `chandelier_active=false`, peak/trough initialized to
`actual_entry` at `trade_full.rs:83-85`; `pending_chandelier_active=false`
at line 92.

Pending transfer happens at sub-bar start: if `has_pending_update`,
then `be_locked`, `trailing_active`, and `chandelier_active` are
lifted together at lines 155-162. `pending_sl` resets to `-1.0` and
`has_pending_update=false` at lines 163-164. The pending active
booleans are not reset within the trade.

The entire Chandelier block is guarded by `chandelier_enabled != 0 &&
chandelier_atr_mult > 0.0 && chandelier_activate_pips >= 0.0` at
lines 302-305.

Long path: peak updates only inside that guard: `sb_high >
chandelier_peak_high` then assign at lines 307-310. Activation reads
`chandelier_active || pending_chandelier_active || float_pnl_pips >=
chandelier_activate_pips` at lines 317-319. Distance is
`chandelier_atr_mult * atr_pips * pip_value` at line 322. Raw SL is
`chandelier_peak_high - chand_dist` at line 324. Ratchet/side guard
is `new_sl > effective_sl && new_sl < sb_low` at line 330, so it
uses `sb_low`, not `sb_close` or current price. Activation/write
paths set `pending_chandelier_active` at lines 334, 337-345.

Short path: trough updates via `sb_low < chandelier_trough_low`
then assign at lines 311-314. Same activation and distance lines
317-322. Raw SL is `chandelier_trough_low + chand_dist` at line 348.
Ratchet/side guard is `new_sl < effective_sl && new_sl > sb_high` at
line 354. Pending activation/write paths are lines 358, 361-369.

Exit attribution: long SL fill at lines 427-436 and short SL fill at
lines 453-462 both check `chandelier_active` first, then
`trailing_active`, then `be_locked`, else plain SL. If both trailing
and chandelier are active, Chandelier wins.

**Guards / Hazards**
`enabled==0` skips the whole Chandelier block, including peak/trough
updates; no Chandelier write path runs. No place found where
`chandelier_enabled==0` plus positive ATR multiplier can write,
because the write logic is inside lines 302-305.

Pending-SL activation lifts `chandelier_active` on the same boundary
as BE/trailing at lines 155-162.

Other guards: BE uses `!be_locked && !pending_be_locked` at line
191; trailing activation uses `!trailing_active &&
!pending_trailing_active` at line 214; partial uses `!partial_done`
at line 387. These flags initialize false at lines 80-92 and latch
true within the trade; no reset after initialization was found.

## Diff: my trace vs Codex

**Agreements (100% — every hop confirmed):**

- Schema: exact bounds match (activate 5–25 log, atr_mult 2.0–4.0 linear).
- Sentinels: `enabled=0`, `activate=-1.0`, `atr_mult=-1.0` when Group off.
- Constants: PL_CHANDELIER_ENABLED=37, ACTIVATE=38, ATR_MULT=39, EXIT_CHANDELIER=7.
- Rust parse: three reads via `param_layout_s` indirection, threaded into `simulate_trade_full` as positional args 28-30.
- State init: peak/trough at `actual_entry`, active flags false, per-trade.
- Outer guard: `enabled != 0 && atr_mult > 0.0 && activate_pips >= 0.0` — conjunction, catches any one-sentinel leak.
- Peak/trough update: conditional on outer guard (zero wasted work when disabled).
- Side-of-price guard: **`new_sl < sb_low` (long) / `new_sl > sb_high` (short)** — the v2 trailing-fix pattern. Neither `sb_close` (breakeven bug) nor `current_price` (Codex brief's preference) is used.
- Ratchet: monotone one-way (max for long, min for short).
- Exit priority: chandelier > trailing > breakeven > plain SL. Deterministic.
- Pending-SL lift: on sub-bar boundary alongside be_locked / trailing_active.

**Disagreements:** None. Codex's independent read reproduces the same
line numbers and the same semantic conclusions as my own trace.

**Net:** zero hazards from the six-point checklist. The code is
wired end-to-end, gated correctly, and honours the mechanics brief.
Phase 4 micro-test proves the arithmetic matches hand-calculation.
