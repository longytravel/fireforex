# Phase 2 — Signal filters code trace

Goal: follow the three signal-filter knob families (variant, buy/sell value filter, generic P0..P9) from UI → schema → sampler → encoding → Rust parser → Rust arithmetic. Every hop cited to file and line.

Scope reminder from Phase 1: the mechanics brief and Codex's independent brief **disagreed** on what happens when a signal carries `sig_variant = -1` while the trial picks `trial_variant ≥ 0`. This trace settles that disagreement at §4 below. The answer drives the Phase 3 table.

## 1. Schema — `ff/schema.py` + per-EA signal blocks

Signal filters are not first-class schema primitives. They enter the parameter vector in two distinct ways:

1. **Variant** is injected by the sampler at trial time, *not* declared in the schema.
   - `ff/sampler.py:37-51` — `RandomSampler(engine_schema, n_variants, seed)` stores `self._n_variants` and on every sample writes `"signal_variant": int(self._rng.integers(0, self._n_variants))`. No upper/lower range is taken from the schema; it is hard-wired by the variant count of the pooled signal library.
2. **`buy_filter_max` / `sell_filter_min` / `PL_SIGNAL_P0..P9`** are populated only when the EA's `engine_mapping` registers a tuple for the slot. In `eas/complex01.py:21` the mapping registers `(bc.PL_SIGNAL_VARIANT, enc.slot_int(("signal_variant",)))` and **does not** register any of the buy/sell/Pk slots. That means for complex01 those slots are never written to the param matrix — they keep their `ENGINE_DEFAULTS` (`-1.0`) from `ff/encoding.py:32-36`, i.e. the filter is off.

Implication: `complex01` today exercises **only** the variant filter. The buy/sell and P0..P9 gates are dormant from the Python side in the repo's flagship EA. Any validation of them must be done either with a bespoke EA or a direct `batch_evaluate` call from a test fixture.

## 2. Encoding — `ff/encoding.py`

Three artefacts here:

- `ff/encoding.py:32-36` — `ENGINE_DEFAULTS` dict seeds every slot with `-1.0`, matching the Rust "off" sentinel:
  ```python
  ENGINE_DEFAULTS: dict[int, float] = {
      bc.PL_SIGNAL_VARIANT: -1.0,
      bc.PL_BUY_FILTER_MAX: -1.0,
      bc.PL_SELL_FILTER_MIN: -1.0,
  }
  ```
  Note: P0..P9 are not in this dict. They default to whatever the general initialiser writes — inspection of the `encode(...)` helper shows unwritten slots default to `0.0` via `np.zeros`, not `-1.0`. **This is the first real defect found.** An EA that registers P0..P9 and samples them explicitly is fine; an EA that forgets to register a Pk slot will leave it at `0.0`, which the Rust `sig_val >= 0 && sig_val != trial_sig_filters[f]` gate treats as an active filter for value `0`, not as off. Confirm in §5 below.
- `ff/encoding.py:202` — the complex01 mapping registers `(bc.PL_SIGNAL_VARIANT, slot_int(("signal_variant",)))`. `slot_int` reads the trial dict at path `signal_variant` and writes the float-cast integer into column `PL_SIGNAL_VARIANT` of the param matrix.
- `ff/encoding.py:14-20` — docstring says `PL_SIGNAL_VARIANT = -1` disables variant filtering and `PL_BUY_FILTER_MAX / PL_SELL_FILTER_MIN = -1` disables "exact-match only, no range". **The documentation confirms the naming is wrong.** The comment itself admits the slots are exact-match; only the slot *name* still says max/min.

## 3. Harness — `ff/harness.py`

- `ff/harness.py:270-281` — builds the signal library via `build_signal_library`, which pools every family × param combo into one flat array, tagging each signal with an integer variant id assigned sequentially starting at `0` (see `ff/signal_lib.py` variant-parts construction: `np.full(n, next_id, dtype=np.int64)`). In current Fire Forex practice **every signal is tagged with `variant ≥ 0`**; there is no code path that writes `variant = -1` for a pooled signal.
- `ff/harness.py:287` — `RandomSampler(engine_schema, n_variants=lib.n_variants, seed=seed)`. Sampler integers range `[0, n_variants)`. `trial_variant = -1` never occurs in normal sweeps.
- `ff/harness.py:302-312` — pre-flight filter matrix is constructed but the only value passed in for pre-flight is `lib.filter_value[:1]` — filter_value is threaded through to the engine unconditionally.
- `ff/harness.py:353` — the actual full call passes `lib.swing_sl, lib.filter_value, lib.variant, …` to `batch_evaluate`.

So: while `-1` is nominally the off-sentinel for both trial and signal sides, production sweeps never write `-1` on either side.

## 4. Rust parser — `core/src/lib.rs`

Slot constants — `core/src/constants.rs:70-85`:

```rust
pub const PL_SIGNAL_VARIANT: usize = 24;
pub const PL_BUY_FILTER_MAX: usize = 25;
pub const PL_SELL_FILTER_MIN: usize = 26;
pub const PL_SIGNAL_P0: usize = 27;
// … P1..P9 sequential
pub const PL_SIGNAL_P9: usize = 36;
pub const NUM_SIGNAL_PARAMS: usize = 10;
```

`batch_evaluate` signature — `core/src/lib.rs:16-55`:

```rust
sig_direction: PyReadonlyArray1<'py, i64>,
sig_filter_value: PyReadonlyArray1<'py, f64>,
sig_variant: PyReadonlyArray1<'py, i64>,
sig_filters: PyReadonlyArray2<'py, i64>,   // shape (NUM_SIGNAL_PARAMS, n_signals)
```

The inline comment at `lib.rs:44` reinforces: `"Values of -1 mean 'no filter'"`.

Shape check at `lib.rs:123-131` — rejects if `sig_filters.rows != NUM_SIGNAL_PARAMS` or cols != `n_signals`. Correct.

Per-trial extraction — `core/src/lib.rs:235-262`:

```rust
let variant_col = param_layout_s[PL_SIGNAL_VARIANT];
let trial_variant = if variant_col >= 0 {
    params[variant_col as usize] as i64
} else { -1 };

let bfm_col = param_layout_s[PL_BUY_FILTER_MAX];
let buy_filter_max = if bfm_col >= 0 {
    params[bfm_col as usize]
} else { -1.0 };

let sfm_col = param_layout_s[PL_SELL_FILTER_MIN];
let sell_filter_min = if sfm_col >= 0 {
    params[sfm_col as usize]
} else { -1.0 };

let mut trial_sig_filters: [i64; NUM_SIGNAL_PARAMS] = [-1; NUM_SIGNAL_PARAMS];
for f in 0..NUM_SIGNAL_PARAMS {
    let col = param_layout_s[PL_SIGNAL_P0 + f];
    if col >= 0 {
        trial_sig_filters[f] = params[col as usize] as i64;
    }
}
```

Notes:
- `trial_variant` and `trial_sig_filters[f]` are cast `f64 → i64` via `as i64` → truncation toward zero. Continuous sampling at these slots collapses to integer buckets silently.
- `buy_filter_max` / `sell_filter_min` stay `f64`. No cast, so no truncation; but float equality brittleness is preserved.
- When `param_layout[slot] < 0` (slot not registered by the EA's engine_mapping), the trial side defaults to `-1` / `-1.0` — the engine's "off" sentinel. **That default, combined with the ENGINE_DEFAULTS dict, means an EA that forgets a slot gets the correct "off" behaviour.** This matches Phase 1 expectation.

## 5. Rust arithmetic — `core/src/lib.rs:270-297`

The per-signal guard loop:

```rust
'signal_loop: for si in 0..n_signals {
    // 5a — Variant filter
    if trial_variant >= 0 && sig_variant_s[si] >= 0 {
        if sig_variant_s[si] != trial_variant {
            continue;
        }
    }

    let direction = sig_direction_s[si];

    // 5b — Strategy-specific buy/sell value filter
    if buy_filter_max >= 0.0 && direction == DIR_BUY {
        if sig_filter_value_s[si] != buy_filter_max {
            continue;
        }
    }
    if sell_filter_min >= 0.0 && direction == DIR_SELL {
        if sig_filter_value_s[si] != sell_filter_min {
            continue;
        }
    }

    // 5c — Generic signal param filters (PL_SIGNAL_P0..P9)
    for f in 0..NUM_SIGNAL_PARAMS {
        if trial_sig_filters[f] >= 0 {
            let sig_val = sig_filters_s[f * n_filter_cols + si];
            if sig_val >= 0 && sig_val != trial_sig_filters[f] {
                continue 'signal_loop;
            }
        }
    }

    // … time filter, spread filter, entry logic below
}
```

### Resolving the Phase 1 disagreement

The variant guard at `lib.rs:272-276` fires **only when both** `trial_variant ≥ 0` AND `sig_variant_s[si] ≥ 0`. If the signal writes `-1`, the inner equality check is skipped and the signal is **admitted** irrespective of the trial's variant choice.

**My Phase 1 reading is correct. Codex is wrong** on this specific point. Codex stated "any signal with `sig_variant == -1` is excluded by exact match" when `trial_variant >= 0`; the code does the opposite — it *admits* such a signal. The opt-out is bilateral.

Practical consequence: because `ff/signal_lib.py` always tags pooled signals with `variant ≥ 0`, the bilateral opt-out is dead code in production today. A future family that wanted to be "untagged / always admitted" could write `variant = -1` and silently bypass the trial's variant selector — a feature, or a trap, depending on intent. Flag for the EA designer.

### Buy / sell filter — behaviour confirmed, with an important asymmetry

- Active condition is `buy_filter_max >= 0.0` (float) AND `direction == DIR_BUY`. The Python default `-1.0` exactly satisfies `< 0.0` → check never fires → all buy signals admit. Good.
- When active, comparison is `sig_filter_value_s[si] != buy_filter_max` on `f64`. Brittle to float drift (Phase 1 hypothesis 1, remains testable in Phase 4).
- Asymmetry #1 (direction): a trial can set buy_filter but leave sell_filter = -1. Short signals admit unconditionally; long signals must match. Confirms Phase 1 asymmetry analysis.
- Asymmetry #2 (sentinel) — **only discovered after Codex read the guard carefully**: `sig_filter_value = -1.0` on the signal side is **NOT** an opt-out. Once the trial side is active (`>= 0.0`), any signal with `filter_value != buy_filter_max` is rejected, including `filter_value = -1`. This differs from the variant filter and the P0..P9 family, both of which honour bilateral `-1` opt-out. If a family ever writes `-1` to mean "I don't want to participate in value-filtering", that signal will be silently excluded whenever the trial sets a buy/sell filter. **Add to defect list as D6.**

### Generic Pk filter — bilateral opt-out, with signal-side initialisation confirmed

- `trial_sig_filters[f] >= 0` gates whether the Pk filter is active for this trial.
- Inside: `sig_val >= 0 && sig_val != trial_sig_filters[f]` — the signal's own `-1` is a bilateral opt-out. Good.
- **Corrected from initial reading**: the signal-side `sig_filters` matrix is explicitly built at `ff/harness.py:302-304` as `np.full((bc.NUM_SIGNAL_PARAMS, lib.bar_index.size), -1, dtype=np.int64)`. Every cell is pre-initialised to `-1` unless a family explicitly writes a value. That means the zero-init-as-real-value hazard I originally flagged (D3) does **not** apply to P0..P9 in current Fire Forex — because the signal library itself does not even produce Pk arrays yet (`ff/signal_lib.py:387-399` — SignalLibrary fields stop at `filter_value, swing_sl, variant`; no `filters` field). **D3 as stated is wrong.** Replace with a related but narrower concern: when a Pk filter is ever wired up, the family must opt into it explicitly by writing non-`-1` values into `sig_filters`, otherwise every signal passes.

## 6. Guards / flags summary

No `done` / `locked` flags for signal filters — they fire once per signal at the top of the per-trial per-signal loop. No intra-bar re-evaluation. Clean gating semantics.

## 7. Summary of defects found (before Phase 3)

| # | Defect | Severity | Action |
|---|--------|---------|--------|
| D1 | Slot names `buy_filter_max` / `sell_filter_min` claim range semantics but implement equality | **Cosmetic / documentation** — user-facing semantic gap. Rename or redoc. | Verdict (b) |
| D2 | `ENGINE_DEFAULTS` omits `PL_SIGNAL_P0..P9`; unregistered Pk slots default to `0.0` instead of `-1.0` | **Latent silent-bug** — any EA that uses Pk and forgets to register will silently filter to value-0 signals. | Phase 4 micro-test |
| D3 | ~~Filter-matrix default fill may leave `sig_filters[f][si] = 0`~~ | **Retracted** — `ff/harness.py:304` pre-fills every Pk cell with `-1`. | — |
| D4 | `trial_sig_filters[f] = params[col] as i64` truncates toward zero | **By design / documentation gap** — must be documented if continuous sampling ever hits Pk. | Verdict (b) if it matters |
| D5 | Float equality on `sig_filter_value_s[si] != buy_filter_max` | **Latent silent-bug** — if a family computes `filter_value` from arithmetic, `!=` can reject intended matches. | Phase 4 with computed-value fixture |
| D6 | Buy / sell filter does **not** honour signal-side `-1` as opt-out; variant and Pk do. Asymmetric sentinel semantics. | **Semantic gap / latent silent-bug** — any family writing `filter_value=-1` is silently dropped whenever the trial's buy or sell filter is active. | Phase 4 + document |

Variant filter itself is **clean** (criteria 1–6 of primer §9 are all met for integer-id semantics).

## Codex independent trace

*Captured verbatim from `codex exec -c model_reasoning_effort=high` on 2026-04-19. Codex read the source tree directly via ripgrep under sandbox; line numbers are Codex's, cross-checked against my own trace above.*

**Trace (table form):**

| Knob | Python origin and slot | Encoding behavior | Rust extraction | Rust guard/arithmetic |
|---|---|---|---|---|
| `PL_SIGNAL_VARIANT` | Slot `24`: `core/src/constants.rs:70`. Trial value is sampled explicitly as `signal_variant = rng.integers(0, n_variants)`: `ff/sampler.py:37-42`, `ff/sampler.py:49-52`. Harness sets `n_variants=lib.n_variants`: `ff/harness.py:287`. Signal-side variant ids are assigned by `next_id`: `ff/signal_lib.py:689`, `ff/signal_lib.py:703-705`, pooled into `lib.variant`: `ff/signal_lib.py:722`. | Default is `-1.0`: `ff/encoding.py:33-36`. Self-test maps slot with `slot_int(("signal_variant",))`: `ff/encoding.py:201-202`; `slot_int` writes `float(int(v))`: `ff/encoding.py:75-79`. Mapped slots override defaults: `ff/encoding.py:166-168`. | `variant_col = param_layout_s[PL_SIGNAL_VARIANT]`: `core/src/lib.rs:236`; `trial_variant = params[variant_col as usize] as i64`: `core/src/lib.rs:237-238`; fallback `-1`: `core/src/lib.rs:239-240`. | If both trial and signal variant are non-negative, require equality: `if trial_variant >= 0 && sig_variant_s[si] >= 0`: `core/src/lib.rs:269`; mismatch continues/skips: `core/src/lib.rs:270-271`. |
| `PL_BUY_FILTER_MAX` / `PL_SELL_FILTER_MIN` | Slots `25` and `26`: `core/src/constants.rs:71-72`. Signal-side value is `SignalSet.filter_value`: `ff/signal_lib.py:94`; zero-length defaults to zeros length `n`: `ff/signal_lib.py:103-105`; pooled into `lib.filter_value`: `ff/signal_lib.py:701`, `ff/signal_lib.py:720`. Harness passes it to Rust: `ff/harness.py:312`, `ff/harness.py:353`. | Defaults are `-1.0`: `ff/encoding.py:35-36`, applied by `ff/encoding.py:156-157`. The provided `ff/encoding.py` self-test does not map these slots; it only prints their default values: `ff/encoding.py:242`. Uncertain for real EAs from the requested files alone — need the EA `engine_mapping` source/config that registers these slots. | Buy: `bfm_col = param_layout_s[PL_BUY_FILTER_MAX]`: `core/src/lib.rs:242`; `buy_filter_max = params[bfm_col as usize]`: `core/src/lib.rs:243-244`; fallback `-1.0`: `core/src/lib.rs:245-246`. Sell: `sfm_col`: `core/src/lib.rs:248`; `sell_filter_min = params[sfm_col as usize]`: `core/src/lib.rs:249-250`; fallback `-1.0`: `core/src/lib.rs:251-252`. No `as i64` cast here. | Direction constants: buy `1`, sell `-1`: `core/src/constants.rs:5-6`. Long guard: `if buy_filter_max >= 0.0 && direction == DIR_BUY`, then `sig_filter_value_s[si] != buy_filter_max` skips: `core/src/lib.rs:276-280`. Short guard: `if sell_filter_min >= 0.0 && direction == DIR_SELL`, then `sig_filter_value_s[si] != sell_filter_min` skips: `core/src/lib.rs:282-285`. |
| `PL_SIGNAL_P0..P9` | Slots `27..36`, `NUM_SIGNAL_PARAMS = 10`: `core/src/constants.rs:75-85`. Signal-side matrix is not produced by `SignalLibrary`; its fields stop at `filter_value`, `swing_sl`, `variant`, `variant_map`: `ff/signal_lib.py:387-399`. Harness explicitly creates `sig_filters = np.full((bc.NUM_SIGNAL_PARAMS, lib.bar_index.size), -1, dtype=np.int64)`: `ff/harness.py:302-304`, and passes it: `ff/harness.py:313`, `ff/harness.py:354`. | `encode()` starts with zeros for all `NUM_PL` slots: `ff/encoding.py:155`. Only slots in `ENGINE_DEFAULTS` get nonzero defaults, and that dict contains only variant/buy/sell: `ff/encoding.py:33-36`. Therefore unmapped `PL_SIGNAL_P0..P9` encode as `0.0` from the matrix zero-fill. Any mapping, if present, would override via `ff/encoding.py:166-168`. Uncertain for real EA mappings from requested files alone — need the EA `engine_mapping` source/config that registers `PL_SIGNAL_P*`. | Rust initializes trial filters to `-1`: `core/src/lib.rs:256`; loops `0..NUM_SIGNAL_PARAMS`: `core/src/lib.rs:257`; reads layout slot `PL_SIGNAL_P0 + f`: `core/src/lib.rs:258`; if layout col non-negative, writes `params[col as usize] as i64`: `core/src/lib.rs:259-260`. Harness uses identity layout `np.arange(bc.NUM_PL)`: `ff/harness.py:294`, so encoded zero defaults are read as trial value `0`, not Rust fallback `-1`. | For each `f`, if trial value is non-negative: `core/src/lib.rs:289-290`; read signal-side `sig_val`: `core/src/lib.rs:291`; if signal value is also non-negative and unequal, skip: `core/src/lib.rs:292-293`. |

**Sentinel behavior (Codex's summary):**

- Trial-side `-1` disables `PL_SIGNAL_VARIANT` (`lib.rs:269`). Signal-side variant `-1` admits the signal. **This contradicts Codex's Phase 1 brief, which claimed the signal would be excluded. Codex corrected itself on reading the code.**
- Trial-side `-1.0` disables buy/sell filters (`lib.rs:277`, `282`). **Signal-side `filter_value == -1` is NOT an opt-out** — once the relevant direction's trial filter is active, equality is required (`lib.rs:278-279`, `283-284`). This is a new finding not in my own trace; added as defect **D6**.
- Trial-side `PL_SIGNAL_P* == -1` disables that generic filter (`lib.rs:290`). Signal-side `sig_filters[f,si] == -1` admits the signal (`lib.rs:291-293`).

**Name/arithmetic mismatch (Codex's conclusion):** `PL_BUY_FILTER_MAX`/`PL_SELL_FILTER_MIN` implement `!=` equality, not range arithmetic. Confirmed at `core/src/lib.rs:278`, `283`. Python comments at `ff/encoding.py:15-16` and `ff/signal_lib.py:181-184` already document the exact-match semantics — the defect is the name, not the docstring.

## Diff summary

**Agreements (both readings converged):**

- Slot numbers and NUM_SIGNAL_PARAMS = 10.
- Variant guard requires both sides `≥ 0` — so signal-side `-1` is a bilateral opt-out. (Codex flipped here from its Phase 1 brief — a *feature* of the skill's two-phase process, not a bug.)
- ENGINE_DEFAULTS only overrides variant/buy/sell. P0..P9 default to `0.0` in the param matrix when an EA forgets to register them.
- Pk signal-side opt-out is bilateral (trial-side `-1` off; signal-side `-1` admits).
- Names `*_MAX` / `*_MIN` disagree with the implemented `!=` equality.

**New findings from Codex that I missed:**

1. **Harness builds `sig_filters` with `-1` everywhere** (`ff/harness.py:304`). My D3 defect ("zero-init collides with real filter value 0") is **retracted** — the signal-side P0..P9 matrix is safely pre-filled. I updated §5 and the defect table accordingly.
2. **`param_layout = np.arange(bc.NUM_PL)`** at `ff/harness.py:294` — the identity layout. The Rust fallback-to-`-1` when `param_layout[slot] < 0` is dead code in production. Only the encoded matrix value reaches the trial-side.
3. **Buy/sell filter does NOT honour signal-side `-1` as opt-out** (asymmetric sentinel semantics vs variant and Pk). Added as **D6**.
4. **SignalLibrary does not even carry a `sig_filters` field** (`ff/signal_lib.py:387-399`); the Pk matrix only exists because the harness synthesises it. So the Pk filter family is currently a dormant hook with no family-side wiring.

**Disagreements remaining:** none. Phase 2 closes with convergent findings.

**Action before Phase 3:** the defect list is now D1 (naming), D2 (ENGINE_DEFAULTS missing P0..P9), D4 (truncation), D5 (float equality brittleness), D6 (buy/sell asymmetric sentinel). D3 is retracted. Phase 3 will build scenarios that exercise each remaining defect with hand-calculated expected outcomes, plus the "normal" variant-filter path to confirm it works correctly when properly used.

