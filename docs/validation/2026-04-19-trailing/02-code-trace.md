# 02 — Code trace: trailing stop family

Phase 2 of validate-forex-knob, run 2026-04-19 (afternoon). Line
numbers pinned to the repo state AFTER the v1 breakeven-fix.

## Hop 1 — Schema

**File:** `ff/defaults/complexity.py`, `_build_trailing`
**Lines:** 174 – 191

```python
def _build_trailing(level: int, r: dict) -> Group:
    dist_lo = max(5.0, float(r["fixed_sl_pips"]["min"]))
    dist_hi = max(dist_lo + 1.0, float(r["fixed_sl_pips"]["max"]) * 0.6)
    return Group(
        test=Choice([True, False]),
        on_value=True,
        when_on={
            "mode": Branch(
                selector=Choice(["fixed", "atr"]),
                arms={
                    "fixed": {"distance": FloatRange(dist_lo, dist_hi,
                                                     scale="log", step=...)},
                    "atr":   {"mult": _fr(r["trail_atr_mult"], scale="linear")},
                },
            ),
            "activate": _fr(r["trail_activation_pips"], scale="log"),
        },
    )
```

**Critical observations:**

- `distance` has a **lower bound of 5 pips**. The optimiser cannot
  sample a fixed `distance < 5`. That mitigates the fixed-mode
  pathological cases somewhat — a 1-pip trail can't be sampled.
- `atr.mult` has no corresponding floor — it comes directly from
  `r["trail_atr_mult"]` which is derived from the volatility
  cache. If the cache allows `mult = 0.1`, and ATR is small,
  effective distance can fall below 5 pips.
- `activate` is a FloatRange in pips on a log scale; bounds come
  from the volatility cache (`trail_activation_pips`).

## Hop 2 — Sampler

Same as breakeven: `ff/sampler.py` samples the Group, then
samples `when_on` only when `test = True`. The `mode` Branch
selects either `fixed` or `atr`, and the sampler draws only the
arm-specific sub-knobs.

## Hop 3 — Encoding

**File:** `ff/encoding.py` (and mirror in `ff/defaults/complexity.py`)
**Slot mapping** (from `eas/complex01.py` and the sensitivity test
constants):

```
PL_TRAILING_MODE     = int,  via slot_mode_or_off(test_path, mode_path,
                               mode_map={"fixed": 1, "atr": 2})
                        → 0 = TRAIL_OFF when test = False
                        → 1 = TRAIL_FIXED_PIP when mode = fixed
                        → 2 = TRAIL_ATR_CHANDELIER when mode = atr
PL_TRAIL_ACTIVATE    = slot_if_on(trailing.test, trailing.when_on.activate)
PL_TRAIL_DISTANCE    = fixed-arm slot for trailing.when_on.mode.fixed.distance
PL_TRAIL_ATR_MULT    = atr-arm slot for trailing.when_on.mode.atr.mult
```

When `trailing.test = False`, all four slots receive sentinel
values; `PL_TRAILING_MODE = 0 = TRAIL_OFF` is what the Rust side
checks (line 195 `if trailing_mode != TRAIL_OFF`).

## Hop 4 — Rust parser

**File:** `core/src/lib.rs::batch_evaluate`
Standard identity-layout param_matrix unpack. Each slot is a column
index. Values flow into `simulate_trade_full` as positional args:

- `trailing_mode: i64` — trade_full.rs:35
- `trail_activate_pips: f64` — trade_full.rs:36
- `trail_distance_pips: f64` — trade_full.rs:37
- `trail_atr_mult: f64` — trade_full.rs:38

**Constants** in `core/src/constants.rs`:
- `TRAIL_OFF = 0`
- `TRAIL_FIXED_PIP = 1`
- `TRAIL_ATR_CHANDELIER = 2`

## Hop 5 — Rust per-bar use

**File:** `core/src/trade_full.rs`. Two distinct code paths:
**activation** (first time the trail fires) and **ongoing**
(each sub-bar after activation).

### The common distance formula (lines 199 – 204 and 232 – 236)

```rust
let trail_dist = if trailing_mode == TRAIL_FIXED_PIP {
    trail_distance_pips * pip_value
} else {
    // TRAIL_ATR_CHANDELIER
    trail_atr_mult * atr_pips * pip_value
};
```

`atr_pips` is passed in per signal (entry-time ATR, not refreshed
during the trade — verified from `batch_evaluate`'s signal array
`sig_atr_pips`). So ATR mode does not suffer the "ATR collapses
mid-trade" variant. One concern ruled out.

### Activation block (lines 195 – 229)

```rust
if trailing_mode != TRAIL_OFF {
    if !trailing_active && !pending_trailing_active {
        if float_pnl_pips >= trail_activate_pips {
            pending_trailing_active = true;
            let trail_dist = /* as above */;
            if is_buy {
                let new_sl = sb_high - trail_dist;            // line 206
                let effective_sl = if has_pending_update && pending_sl > 0.0 {
                    pending_sl
                } else {
                    current_sl
                };
                if new_sl > effective_sl {                    // line 212 — GUARD
                    pending_sl = new_sl;
                }
            } else {
                let new_sl = sb_low + trail_dist;             // line 216
                let effective_sl = if has_pending_update && pending_sl > 0.0 {
                    pending_sl
                } else {
                    current_sl
                };
                if new_sl < effective_sl {                    // line 222 — GUARD
                    pending_sl = new_sl;
                }
            }
            pending_be_locked = /*...*/;
            has_pending_update = true;
        }
    }
    // … ongoing block follows …
}
```

### Ongoing trail block (lines 231 – 265)

```rust
if trailing_active {
    let trail_dist = /* recomputed each sub-bar */;
    if is_buy {
        let new_sl = sb_high - trail_dist;                    // line 239
        let effective_sl = /* as above */;
        if new_sl > effective_sl {                            // line 245 — GUARD
            pending_sl = new_sl;
            pending_be_locked = /*...*/;
            pending_trailing_active = true;
            has_pending_update = true;
        }
    } else {
        let new_sl = sb_low + trail_dist;                     // line 252
        let effective_sl = /* as above */;
        if new_sl < effective_sl {                            // line 258 — GUARD
            pending_sl = new_sl;
            /* … */
        }
    }
}
```

## The Guards — THE KEY QUESTIONS

### (a) Side-of-price check on candidate SL?

**No.** Lines 212, 222, 245, 258 are **all monotonicity-only**.
They ensure the new SL is *tighter* than the current / pending SL
(`new_sl > effective_sl` for long, `new_sl < effective_sl` for
short). Neither the activation block nor the ongoing block checks
the new SL against `sb_close`, `sb_low` (long), or `sb_high`
(short), or any approximation of bid / ask.

**Same structural bug as breakeven pre-fix**, now replicated in
four places instead of two. If `sb_high - trail_dist > sb_close`
for a long, the engine writes an SL above current price and the
next sub-bar almost always fires it for `+trail_dist` pips.

### (b) Minimum stop distance check?

**No.** The schema's 5-pip floor on `distance` is the only
defence, and it applies only in `TRAIL_FIXED_PIP` mode. In
`TRAIL_ATR_CHANDELIER` mode, `atr_mult * atr_pips` can produce
arbitrarily small distances depending on the volatility cache's
`trail_atr_mult` range. The Rust engine does not enforce a minimum.

### (c) ATR refresh semantics

**Fixed at entry.** `atr_pips` is read from the signal's
entry-time value (`sig_atr_pips_s[s]`). It does not refresh per
bar. The "ATR collapses mid-trade → tiny distance" failure mode is
not present.

### (d) Short-side trigger using bid or ask?

**Raw mid-ish (not ask).** Short SL hit check at line 321 is
`sb_high >= current_sl`. `sb_high` is the raw OHLC high — the
same series used for the long's favorable extreme. Spread is NOT
added. For a real short the trigger should be
`(sb_high + spread) >= current_sl` (ask crosses SL).

**Consequence:** on a short, the engine is both:
- *Too lenient* — short SL triggers later than it should (need
  more price rise to fire).
- *Compounding the trailing bug* — when the ongoing trail writes
  an SL below `sb_close` for a short, the engine's own trigger
  check uses `sb_high` (not ask), so the SL can still fire on the
  same-bar low-end wick. Net effect depends on which dominates.

This is a separate, standing spread-handling concern surfaced in
the breakeven trace — not unique to trailing. Worth its own
validation run later.

### (e) Interaction with breakeven

Both blocks can fire on the same sub-bar. Order of evaluation:
**breakeven runs first** (line 172 – 192), then **trailing**
(line 195 – 265), both writing into the same `pending_sl`. If BE
sets `pending_sl = 1.10002` and the trail then computes
`new_sl = 1.10005`, the trail's guard on line 212 checks
`1.10005 > effective_sl`; `effective_sl` is read as `pending_sl
(1.10002)` if `has_pending_update` is set, so the trail wins when
tighter. Reasonable. But the compound effect is that a single
sub-bar can both activate BE *and* activate the trail, setting an
SL even further above price. See Phase 3.

## Summary

- **All hops present?** Yes.
- **Silent no-op?** No — the knobs move outcomes.
- **Sign flip on short?** Symmetric — and compounded by the
  short-side bid/ask issue.
- **Missing guards?** **Yes.** Four sites (lines 212, 222, 245,
  258) all lack a side-of-price check. Same shape as the
  pre-fix breakeven bug.
- **Fix scope:** apply the same `new_sl < sb_close` (long) /
  `new_sl > sb_close` (short) guard to all four sites.

**Verdict direction for Phase 6:** heading for **(c) broken**,
same fix pattern as v1.

---

## Codex independent trace

*Verbatim from Codex (gpt-5.4 high, read-only sandbox over the
repo, instructed not to read docs/validation/).*

> **Trace**
>
> 1. `trailing.test`
> - Schema: optional `Group`; `test=Choice([True, False])`,
>   `on_value=True`. `ff/schema.py:81-95`. Complex01 source at
>   `eas/complex01.json:125-135`.
> - Complexity builder: enabled only at complexity `>= 4` in
>   `ff/defaults/complexity.py:291-315`; built as
>   `Group(test=Choice([True, False]), on_value=True)` at
>   `ff/defaults/complexity.py:174-190`.
> - Encoding: `PL_TRAILING_MODE` uses `slot_mode_or_off(...)` in
>   `eas/complex01.py:65-69`. Helper returns `off_value` default
>   `0.0` when `test != True` at `ff/encoding.py:102-119`.
> - Rust parser: `PL_TRAILING_MODE = 10` in
>   `core/src/constants.rs:56`; parsed as `trailing_mode` from
>   `params[param_layout_s[PL_TRAILING_MODE]]` at
>   `core/src/lib.rs:220`; passed to `simulate_trade_full` at
>   `core/src/lib.rs:359`.
> - Rust arithmetic: trailing block gated by
>   `if trailing_mode != TRAIL_OFF` at
>   `core/src/trade_full.rs:205`; constants `TRAIL_OFF=0`,
>   `TRAIL_FIXED_PIP=1`, `TRAIL_ATR_CHANDELIER=2` at
>   `core/src/constants.rs:19-21`.
>
> 2. `trailing.when_on.mode.selector`
> - Branch selector `Choice(["fixed", "atr"])` at
>   `eas/complex01.json:136-145`.
> - Rust compares it to `TRAIL_FIXED_PIP` and otherwise treats
>   non-off as ATR at `core/src/trade_full.rs:209-214` and
>   `core/src/trade_full.rs:242-246`.
>
> 3. `trailing.when_on.activate`
> - Complex01 bounds `FloatRange min=5 max=80 scale=log` at
>   `eas/complex01.json:164-169`.
> - `PL_TRAIL_ACTIVATE = 11` at `core/src/constants.rs:57`;
>   parsed at `core/src/lib.rs:221`; passed as
>   `trail_activate_pips` at `core/src/trade_full.rs:36`.
> - Activation fires when
>   `float_pnl_pips >= trail_activate_pips` at
>   `core/src/trade_full.rs:207`. `float_pnl_pips` uses
>   `sb_high` for longs and `sb_low` for shorts at
>   `core/src/trade_full.rs:160-169`.
>
> 4. `trailing.when_on.fixed.distance`
> - Complex01 bounds `FloatRange min=5 max=50 scale=log` at
>   `eas/complex01.json:146-152`.
> - `PL_TRAIL_DISTANCE = 12` at `core/src/constants.rs:58`;
>   parsed at `core/src/lib.rs:222`; passed as
>   `trail_distance_pips` at `core/src/trade_full.rs:37`.
> - Fixed distance is `trail_distance_pips * pip_value` at
>   activation `core/src/trade_full.rs:209-210` and ongoing
>   trail `core/src/trade_full.rs:242-243`.
>
> 5. `trailing.when_on.atr.mult`
> - Complex01 bounds `FloatRange min=0.3 max=3.0 scale=linear`
>   at `eas/complex01.json:154-160`.
> - `PL_TRAIL_ATR_MULT = 13` at `core/src/constants.rs:59`;
>   parsed at `core/src/lib.rs:223`; passed as
>   `trail_atr_mult` at `core/src/trade_full.rs:38`.
> - ATR trail distance is `trail_atr_mult * atr_pips * pip_value`
>   at activation `core/src/trade_full.rs:212-214` and ongoing
>   `core/src/trade_full.rs:244-245`. `atr_pips` is the signal
>   ATR loaded once from `sig_atr_pips_s[si]` at
>   `core/src/lib.rs:311` and passed at `core/src/lib.rs:351`;
>   no per-bar ATR refresh appears in this path.
>
> **Arithmetic Details**
>
> - Activation logic: gated by off/mode at
>   `core/src/trade_full.rs:205`, inactive state at `206`,
>   threshold at `207`, then `pending_trailing_active = true`
>   at `208`. Pending updates apply on the next sub-bar at
>   `core/src/trade_full.rs:148-157`.
> - Activation candidate SL: long uses `sb_high - trail_dist` at
>   `core/src/trade_full.rs:216`; short uses
>   `sb_low + trail_dist` at `core/src/trade_full.rs:226`.
> - Activation guard: long only checks `new_sl > effective_sl`
>   at `core/src/trade_full.rs:222`; short only checks
>   `new_sl < effective_sl` at `core/src/trade_full.rs:232`.
> - Ongoing candidate SL: long uses `sb_high - trail_dist` at
>   `core/src/trade_full.rs:249`; short uses
>   `sb_low + trail_dist` at `core/src/trade_full.rs:262`.
> - Ongoing guard: long only checks `new_sl > effective_sl`
>   at `core/src/trade_full.rs:255`; short only checks
>   `new_sl < effective_sl` at `core/src/trade_full.rs:268`.
>
> **Guards**
>
> - Correct side of current price: NO trailing-side check like
>   `new_sl < sb_close` for long or `new_sl > sb_close` for
>   short appears in the trailing block. By contrast, breakeven
>   currently has side-of-price checks at
>   `core/src/trade_full.rs:190-194`.
> - Minimum stop distance: no runtime minimum stop-distance
>   check appears; only schema / range lower bounds exist.
> - ATR refresh: ATR-mode uses fixed entry/signal ATR, NOT
>   refreshed per bar.
> - Short SL trigger price: short stop trigger compares raw
>   `sb_high >= current_sl` at `core/src/trade_full.rs:331`; it
>   does not add spread/ask there. Short spread is deducted
>   later from PnL at `core/src/trade_full.rs:370-381`.
>
> **Suspicious Hop**
>
> Trailing has the same monotonicity-only shape at
> `core/src/trade_full.rs:222`, `232`, `255`, and `268`.
> Breakeven now includes both monotonicity AND side-of-price in
> one `accept` condition at `core/src/trade_full.rs:190-194`;
> trailing does not.

---

## Diff

### Load-bearing agreements

- **Four guard sites, all monotonicity-only.** Codex and I land
  on the same four line numbers (222, 232, 255, 268 post-BE-fix).
  No side-of-price check at any of them.
- **Same shape as the pre-fix breakeven bug.** Codex calls this
  out explicitly in its "Suspicious Hop" section.
- **`atr_pips` is fixed at entry** (single-lookup at lib.rs:311).
  ATR-collapse-mid-trade ruled out for now.
- **Short SL trigger uses raw `sb_high`** (line 331), not ask.
  Spread is applied at PnL deduction time (370-381), not at
  trigger comparison. Separate concern.
- **Trailing-mode enum values:** TRAIL_OFF=0, TRAIL_FIXED_PIP=1,
  TRAIL_ATR_CHANDELIER=2.

### Codex-supplied details I was missing / imprecise on

- **Slot indices:** `PL_TRAILING_MODE=10`, `PL_TRAIL_ACTIVATE=11`,
  `PL_TRAIL_DISTANCE=12`, `PL_TRAIL_ATR_MULT=13`. I had omitted
  the numeric values.
- **Exact line numbers post-BE-fix.** My initial trace used
  pre-fix numbers (the BE fix added ~10 lines). Correct
  numbers: activation long-side SL at `:216`, guard at `:222`,
  short-side SL at `:226`, guard at `:232`. Ongoing block:
  long SL at `:249`, guard at `:255`, short SL at `:262`, guard
  at `:268`. All subsequent artifacts use Codex's numbers.
- **Trailing added at complexity ≥ 4**, not ≥ 5 like
  breakeven. Sweeps at levels 4–10 can sample this knob.

### Disagreements

**None of substance.**

### What Phase 3 must cover

Four guard sites × two modes (fixed / ATR) × two directions
(long / short) × two lifecycle phases (activation / ongoing) is a
lot. I will focus Phase 3 on the failure modes the Phase 1 brief
predicted, not exhaustive cartesian coverage:

1. Long activation with `distance` small enough that
   `sb_high − distance > sb_close` — core bug.
2. Mirror on short with `distance` small enough that
   `sb_low + distance < sb_close`.
3. ATR-mode variant — same bug via `atr_mult × atr_pips`.
4. Ongoing trail — same pattern happens each sub-bar, so if
   activation is safe but the market continues moving
   favourably, ongoing trails can still write an SL above
   current close.
5. Control: a generous `distance` (≥ 10 pips) on a normal
   uptrend — trail should accept and track as expected.
6. Short + spread asymmetry: skip — tangential, earmark for a
   later validation.
