# Phase 2 — Stale-exit code trace

Trace from UI schema definition to the last line of Rust arithmetic that consumes a stale-exit parameter. All three slots (`STALE_ENABLED`, `STALE_BARS`, `STALE_ATR_THRESH`) traced in parallel.

## Hop 1 — Schema definition

**File:** `ff/defaults/complexity.py:222-233` (`_build_stale`)

```python
def _build_stale(level: int, r: dict) -> Group:
    step = _int_step(level, 180)
    return Group(
        test=Choice([True, False]),
        on_value=True,
        when_on={
            "bars": IntRange(20, 200, step=step),
            "atr_thresh": FloatRange(0.3, 2.5, scale="linear",
                                     step=_float_step(level, 0.3, 2.5)),
        },
    )
```

**Invariants:**

- `test` is a `Choice[bool]` with exactly two values; `on_value=True` (schema.py line 98-103 enforces this).
- `bars` is an `IntRange` bounded to `[20, 200]` — cannot be zero, negative, or float.
- `atr_thresh` is a `FloatRange` bounded to `[0.3, 2.5]` on the sampled path. Overrides-JSON can widen (e.g. the sensitivity test sets `atr_thresh = 100.0`).
- The Group is wired into the schema at `complexity.py:319-321`:
  ```python
  if "stale" in opt:
      schema["stale"] = _build_stale(level, r)
  ```
  so it only exists when `"stale"` is in the opt set (`complexity.py:302` adds it at all levels where stale is requested).

## Hop 2 — Sampler / trial dict shape

**File:** `ff/sampler.py` (Group draw). When a trial is sampled, the shape is:

- If `stale.test` is drawn `True`: `{"engine": {..., "stale": {"test": True, "when_on": {"bars": 42, "atr_thresh": 0.8}}}}`.
- If `stale.test` is drawn `False`: the sampler skips `when_on` (`schema.py` Group contract), leaving `{"engine": {..., "stale": {"test": False, "when_on": {}}}}` **or** a dict that just lacks the `when_on` sub-keys.

Either shape is safe because the encoders below treat a missing path identically to an off-switch.

## Hop 3 — Encoding (Python → param row)

The three slots are wired in two places — identical logic, duplicated for historical reasons. Both were verified:

### 3a. Per-EA wiring — `eas/complex01.py:109-117`

```python
(bc.PL_STALE_ENABLED, enc.slot_bool_to_int(("engine", "stale", "test"))),
(bc.PL_STALE_BARS, enc.slot_if_on(
    ("engine", "stale", "test"),
    ("engine", "stale", "when_on", "bars"),
)),
(bc.PL_STALE_ATR_THRESH, enc.slot_if_on(
    ("engine", "stale", "test"),
    ("engine", "stale", "when_on", "atr_thresh"),
)),
```

### 3b. Complexity default mapping — `ff/defaults/complexity.py:475-486`

Same three bindings, generated when building an EA from a `(pair, tf, level)` recipe.

### Encoder semantics — `ff/encoding.py:94-132`

```python
def slot_bool_to_int(path, on_value=True):
    # Returns 1.0 iff the value at `path` == on_value; else 0.0.

def slot_if_on(test_path, value_path, default=0.0, on_value=True):
    # If test_path != on_value: return default (0.0).
    # Else: return float(value at value_path) or default if missing.
```

**Off-branch behaviour:** when `stale.test == False`, all three slots encode to **0.0**. No garbage, no leftover state. This is the first clean-room check: there is no way for the encoder to leak a non-zero value into any of the three slots when the group is off.

## Hop 4 — Rust parser

**File:** `core/src/lib.rs:230-233` (inside the `par_iter_mut()` closure over trial rows)

```rust
let max_bars_val       = params[param_layout_s[PL_MAX_BARS]        as usize] as i64;
let stale_en           = params[param_layout_s[PL_STALE_ENABLED]   as usize] as i64;
let stale_bars_val     = params[param_layout_s[PL_STALE_BARS]      as usize] as i64;
let stale_atr          = params[param_layout_s[PL_STALE_ATR_THRESH] as usize];  // f64
```

**Constant table — `core/src/constants.rs:66-69`:**

```rust
pub const PL_MAX_BARS:         usize = 20;
pub const PL_STALE_ENABLED:    usize = 21;
pub const PL_STALE_BARS:       usize = 22;
pub const PL_STALE_ATR_THRESH: usize = 23;
```

All three slot indices are exported to Python via `m.add("PL_STALE_*", ...)` at `lib.rs:449-451`, so the test suite imports them as `ff_core.PL_STALE_*` and they always match the Rust compile-time constants. No drift risk.

**Pass-through to the trade simulator — `lib.rs:369-372`:**

```rust
let result = simulate_trade_full(
    direction, bar_idx, entry_p, sl_tp.sl_price, sl_tp.tp_price, atr_p,
    high_s, low_s, close_s, spread_s, pip_value, slippage_pips, n_bars,
    trailing_mode, trail_activate, trail_distance, trail_atr_m,
    be_enabled, be_trigger, be_offset,
    partial_en, partial_pct, partial_trig,
    max_bars_val,            // ← 9th management arg
    stale_en,                // ← 10th
    stale_bars_val,          // ← 11th
    stale_atr,               // ← 12th
    commission_pips,
    sub_high_s, sub_low_s, sub_close_s, sub_spread_s,
    h1_to_sub_start_s, h1_to_sub_end_s,
);
```

Signature-matches `simulate_trade_full` arg order at `trade_full.rs:20-59` exactly.

## Hop 5 — Rust use

**File:** `core/src/trade_full.rs:113-136`

```rust
if stale_enabled > 0 && bars_held >= stale_bars {
    let lookback_start = if (entry_bar as i64 + 1) > (bar as i64 - stale_bars + 1) {
        entry_bar + 1
    } else {
        (bar as i64 - stale_bars + 1) as usize
    };
    let mut max_range = 0.0_f64;
    for b in lookback_start..=bar {
        let r = (high[b] - low[b]) / pip_value;
        if r > max_range {
            max_range = r;
        }
    }
    if max_range < stale_atr_thresh * atr_pips {
        let pnl = if is_buy {
            (bar_close - slippage_price - actual_entry) / pip_value * position_pct
        } else {
            (actual_entry - bar_close - slippage_price) / pip_value * position_pct
        };
        final_pnl = realized_pnl_pips + pnl;
        exit_reason = EXIT_STALE;
        exit_bar = bar;
        break 'bar_loop;
    }
}
```

This is the **only** site in the crate that reads `stale_enabled`, `stale_bars`, or `stale_atr_thresh`. Grepped the whole `core/src/` tree — confirmed.

### Line-by-line behaviour

| Line | Arithmetic | Behaviour |
|------|-----------|-----------|
| `:113` | guard `stale_enabled > 0 && bars_held >= stale_bars` | Feature off when `stale_enabled == 0`. When on, waits until `bars_held` ≥ `stale_bars`. |
| `:114-118` | `lookback_start = max(entry_bar+1, bar - stale_bars + 1)` | Excludes the entry bar itself. On the first trigger bar (`bars_held == stale_bars`), the lookback exactly covers `stale_bars` bars, starting at `entry_bar+1`. |
| `:119-124` | `max_range = max((high[b]-low[b]) / pip_value) for b in lookback_start..=bar` | Single-bar max H-L range across the lookback, in pips. |
| `:126` | threshold test `max_range < stale_atr_thresh * atr_pips` | Trigger when every lookback bar's H-L range < `K × ATR_at_entry`. `atr_pips` is the signal-time ATR (frozen at signal emission — see lib.rs:296 `let atr_p = sig_atr_pips_s[si]`). |
| `:127-132` | `pnl = (bar_close ± slippage_price ± actual_entry) / pip_value × position_pct` | Long: `(close - slippage - entry) / pip × frac`. Short: `(entry - close - slippage) / pip × frac`. Slippage symmetric; spread NOT re-applied (already baked into `actual_entry`). |
| `:133` | `final_pnl = realized_pnl_pips + pnl` | Correctly folds in any prior partial-close realization; `position_pct` is the residual fraction. |
| `:134` | `exit_reason = EXIT_STALE` | Distinct code (= 6, per `constants.rs:30`). |
| `:135` | `exit_bar = bar` | Current H1 bar index. |
| `:136` | `break 'bar_loop;` | Exits the outer bar loop; no sub-bar loop on this bar. |

### Ordering relative to other exits

Inside the for-bar loop at `trade_full.rs:90-113`:

1. **Max-bars** check fires first. If triggered, `break 'bar_loop;` → stale cannot fire this bar.
2. **Stale** check fires second. If triggered, `break 'bar_loop;` → sub-bar loop does not run this bar.
3. **Sub-bar loop** (trailing / BE / partial / SL / TP) runs only if neither H1 check fired.

**Consequence:** on a bar where both max-bars and stale would fire, max-bars wins and `EXIT_MAX_BARS` is recorded. On a bar where both stale and a sub-bar SL/TP would fire, stale wins (because H1 check runs before sub-bar simulation). The latter is a *lookahead* artifact: we decide to exit at bar_close before simulating whether a sub-bar SL or TP hit earlier inside the same bar.

### Guards / flags / reset logic

- No persistent `stale_locked` flag. The stale condition is re-evaluated every bar after `bars_held >= stale_bars`.
- `max_range` is recomputed each bar from the high/low arrays — no running state. This is O(stale_bars) per bar, but `stale_bars ≤ 200`, so the cost is negligible.
- `position_pct` and `realized_pnl_pips` are set by the partial-close branch in the sub-bar loop. Stale correctly uses both, so partial + stale composes as expected.
- No clamp on negative or zero `stale_bars`. If the user smuggles `stale_bars = 0` through an override, the condition `bars_held >= 0` is always true at bar `entry_bar + 1` (where `bars_held = 1`). Combined with the lookback clamp (`entry_bar+1` dominates), this would compute `max_range` over a one-bar window and fire the first bar whose range < threshold. Defence-in-depth miss, but the schema bounds prevent it from the sampler.

## Missing-hop check

Every hop is present:

| Hop | Status |
|-----|--------|
| Schema → Sampler | ✅ `_build_stale` produces valid Group. |
| Sampler → Trial dict | ✅ off-branch is clean (empty `when_on` or missing keys). |
| Trial dict → Param row | ✅ `slot_bool_to_int` + `slot_if_on` with default 0.0. |
| Param row → Rust struct | ✅ three slots extracted at `lib.rs:231-233`. |
| Rust struct → Arithmetic | ✅ all three read at `trade_full.rs:113-126`. |
| Arithmetic → PnL output | ✅ `final_pnl + exit_reason = EXIT_STALE` recorded correctly. |

**No silent no-op.** The previous 2026-04-19 morning bug was an execution-mode bypass, not a slot wiring gap — now that the harness passes `EXEC_FULL` and the code path is unified (only `simulate_trade_full`), the slot is read. The sensitivity test (test_knob_sensitivity.py:200-208) confirms a measurable effect under `atr_thresh=100.0, bars=2`.

## Resolutions for Phase 1 open questions

| # | Phase 1 question | Answer from trace |
|---|-----------------|-------------------|
| Q1 | Lookback bounds | `max(entry_bar+1, bar - stale_bars + 1)`. On first trigger (`bars_held == stale_bars`), inspects exactly `stale_bars` bars starting at `entry_bar+1`. Entry bar excluded. |
| Q2 | Exit price source | `bar_close` of the triggering H1 bar, minus `slippage_price` for longs, plus `slippage_price` via sign convention for shorts. **Same bar's close, not next-bar open.** Matches Codex's "lookahead" concern. |
| Q3 | Sub-bar stomp | Stale's `break 'bar_loop;` prevents sub-bar loop on the triggering bar entirely. So partial/SL/TP that *would have* hit within the same bar are skipped. |
| Q4 | Encoding slots off-branch | All three slots encode to 0.0 when `stale.test == False`. `stale_enabled = 0` short-circuits the guard. Clean. |

## Resolutions for Codex's Phase 1 disagreements

| # | Codex's challenge | Answer from code |
|---|-------------------|------------------|
| 1 | "Pricing at bar_close is lookahead; clean convention is next-bar open + spread/slippage." | **Confirmed lookahead-adjacent.** Engine uses `close[bar]` on the triggering bar, not `open[bar+1]`. This is a systemic engine design (same for max-bars), not a stale-specific bug. Worth logging as an engine realism caveat in the verdict. |
| 2 | "Stale should win over max-bars when both fire." | **Engine does the opposite** — max-bars wins. Not a correctness bug (both conditions are true), but a semantic-labelling choice. Flag in verdict. |
| 3 | "Exit spread (bid/ask) not re-applied — understates cost." | **Confirmed.** `bar_close - slippage` is symmetric; spread is not re-applied at exit. Systemic engine property, same for every exit path. |
| 4 | "Bars < 2 should be clamped at the engine boundary." | **Not clamped in Rust.** Schema bounds (20-200) prevent from sampler; override-JSON could bypass. Low-priority hardening. |

## Codex independent trace

*(Verbatim from `codex exec --sandbox read-only -m gpt-5.4 --config model_reasoning_effort=high`, 2026-04-19.)*

> **Trace**
>
> 1. **Schema entry** — `ff/defaults/complexity.py:222-230`: `_build_stale()` returns a `Group` with `test=Choice([True, False])`, `on_value=True`, `bars=IntRange(20, 200, step=_int_step(level, 180))`, and `atr_thresh=FloatRange(0.3, 2.5, scale="linear", step=_float_step(...))`. Stale is only added to the engine schema when `"stale"` is optional/present: `ff/defaults/complexity.py:301-303`, `:319-320`.
>
> 2. **Encoding slot** — `PL_STALE_ENABLED = 21`, `PL_STALE_BARS = 22`, `PL_STALE_ATR_THRESH = 23` in `core/src/constants.rs:67-69`. Generated default mapping at `ff/defaults/complexity.py:477-486`; EA-level mapping at `eas/complex01.py:109-117`. `slot_bool_to_int()` returns 1.0 only when the path equals `True`, else 0.0 (`ff/encoding.py:94-99`). `slot_if_on()` returns default 0.0 when the test path is not `True`, without reading the child value (`ff/encoding.py:122-130`).
>
> 3. **Rust parser** — `batch_evaluate()` reads the three slots at `core/src/lib.rs:231-233`; passes to `simulate_trade_full()` at `:369-372`, received as `stale_enabled`, `stale_bars`, `stale_atr_thresh` at `trade_full.rs:45-48`.
>
> 4. **Rust arithmetic** — Bar loop starts at `entry_bar + 1`, gets `bar_close = close[bar]`, increments `bars_held` (`trade_full.rs:94-98`). Max-bars is checked first, breaks if true (`:101-110`). Stale logic: `if stale_enabled > 0 && bars_held >= stale_bars` (`:113`). `lookback_start = max(entry_bar + 1, bar - stale_bars + 1)` (`:114-118`). `max_range` init inside the stale block (`:119`), computed by `for b in lookback_start..=bar` with `(high[b] - low[b]) / pip_value` (`:120-125`). Threshold test `max_range < stale_atr_thresh * atr_pips` (`:126`). Long PnL `(bar_close - slippage_price - actual_entry) / pip_value * position_pct` (`:127-128`), short `(actual_entry - bar_close - slippage_price) / pip_value * position_pct` (`:129-130`). Sets `EXIT_STALE`, `exit_bar`, breaks (`:132-135`).
>
> 5. **Guards / flags** — No persistent `stale_locked` / `stale_done`. Persistent flags are `partial_done`, `be_locked`, `trailing_active` (`:77-79`). `max_range` recomputed every eligible bar (init inside the if block at `:119-125`). Bar-loop break sites: max-bars `:110`, stale `:135`, buy SL/TP `:350/:358`, short SL/TP `:374/:382`.
>
> **Specific Answers**
>
> (A) On the first bar where `bars_held >= stale_bars`, the inspected range is `entry_bar + 1` through the triggering `bar`, inclusive. Entry bar not included. (`trade_full.rs:94-98`, `:114-120`.)
>
> (B) Stale exit PnL uses `bar_close = close[bar]` from the triggering bar, not previous close or next open. (`:97`, `:127-130`.)
>
> (C) If max-bars and stale both fire on the same bar, **max-bars wins**. Checked first and breaks before stale. (`:101-113`.)
>
> (D) If stale fires on a bar and a sub-bar TP would also hit inside that same bar, **stale wins**. Stale is evaluated before the sub-bar loop starts. (`:113-143`; TP at `:352-358` and `:376-382`.)
>
> (E) When `stale.test == False`, the encoded slots are `PL_STALE_ENABLED=0.0`, `PL_STALE_BARS=0.0`, `PL_STALE_ATR_THRESH=0.0`. `slot_bool_to_int()` at `ff/encoding.py:94-99` and `slot_if_on()` at `:122-130`. On the normal Python encoding path, no non-zero leakage. **Suspicious edge:** Rust itself trusts `param_matrix` — `batch_evaluate()` directly indexes these slots without an off-group consistency check at `core/src/lib.rs:231-233`.

## Diff — my trace vs Codex

Both traces land on the same conclusions; no methodological disagreement. Full alignment on:

- Line numbers for all five hops.
- Parameter slot constants (21 / 22 / 23).
- Lookback bounds (`max(entry_bar+1, bar - stale_bars + 1)`).
- Exit price (`close[bar]` on triggering bar).
- Priority: max-bars > stale > sub-bar TP.
- Off-branch encoder behaviour (all three slots → 0.0).

**Codex adds one observation I didn't make explicit:** the Rust parser trusts the input `param_matrix` blindly — there's no defensive check that `stale_enabled == 0` implies `stale_bars == 0` and `stale_atr_thresh == 0`. A caller that passes a non-zero `stale_bars` alongside `stale_enabled = 0` would have those values silently ignored (correct, because the guard at `:113` short-circuits). But a caller that passes `stale_enabled > 0` alongside `stale_bars = 0` and `stale_atr_thresh = 0` would get the Rust equivalent of "always fire at bar entry+1, always pass the ceiling (because 0 × anything = 0 > 0 is false, wait that fails the `<` test)". Let me check this more carefully:

- `stale_enabled = 1, stale_bars = 0, atr_thresh = 0` → at bar `entry_bar + 1` (bars_held = 1 ≥ 0), lookback_start = max(entry_bar + 1, bar - 0 + 1) = max(entry_bar + 1, bar + 1). Since `bar = entry_bar + 1`, `bar + 1 = entry_bar + 2`. Max = entry_bar + 2. Then `for b in entry_bar+2..=entry_bar+1` is an empty range; `max_range` stays 0. Threshold = 0 × 10 = 0. `0 < 0` is **false** → no fire. Harmless degenerate.
- `stale_enabled = 1, stale_bars = 1, atr_thresh = 0` → at bar `entry_bar + 1`, lookback = max(entry_bar + 1, entry_bar + 1) = entry_bar + 1. Range[entry_bar+1] whatever it is. Threshold = 0 → `max_range < 0` false → no fire. Harmless.

Good news: zero thresholds make stale silently never fire. Bad news: an engine caller who sets `stale_enabled = 1` and forgets to populate the other slots gets a silent no-op that the sensitivity test wouldn't catch at those exact values. Not a user-facing bug through the Python path (encoder guarantees all three are zero when `.test == False`), but a defensive Rust validation would be a cheap hardening. Flag as low-priority in verdict.

Otherwise the two traces are in complete agreement. **No methodological disagreement to escalate to the user.**

---

---

*File: `docs/validation/2026-04-19-stale-exit/02-code-trace.md`*
*Written 2026-04-19 from direct read of `ff/defaults/complexity.py`, `ff/encoding.py`, `eas/complex01.py`, `core/src/constants.rs`, `core/src/lib.rs`, `core/src/trade_full.rs`.*
