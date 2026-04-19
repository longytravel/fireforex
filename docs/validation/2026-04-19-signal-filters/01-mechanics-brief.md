# Phase 1 — Signal filters mechanics brief

Target knobs: `PL_SIGNAL_VARIANT` (slot 24), `PL_BUY_FILTER_MAX` / `PL_SELL_FILTER_MIN` (slots 25 / 26), and `PL_SIGNAL_P0..P9` (slots 27–36). All three are evaluated at the top of the per-signal loop in `core/src/lib.rs:270-297`, upstream of every SL/TP/management knob previously validated.

## 1. Definitions in plain English

### 1a. `PL_SIGNAL_VARIANT` — the variant picker
The signal library pools all `(family, param-combo)` pairs into one flat array tagged with an integer variant id. `PL_SIGNAL_VARIANT` tells the engine "this trial only trades signals whose variant tag matches this id". It is not a filter in the predicate sense — it is a **selector**. Setting it to `-1` means "trade any variant" (no selection), which in Fire Forex's batch-evaluate design is usually *not* what you want because the pooled array contains dozens of variants and firing all of them at once is mixing strategies.

### 1b. `PL_BUY_FILTER_MAX` / `PL_SELL_FILTER_MIN` — the direction-scoped value filter
Each signal carries a single float `filter_value` populated by its family (e.g. session id, ATR-regime bucket, RSI bucket). On a trial the engine checks, for **buy** signals only, whether the signal's `filter_value` equals `PL_BUY_FILTER_MAX`; and for **sell** signals only, whether it equals `PL_SELL_FILTER_MIN`. Each is disabled when set to `-1`.

**The names are misleading.** "max" / "min" suggest inequality semantics (`filter_value <= max` / `filter_value >= min`). The actual engine operation is **equality**. A family writing, say, `filter_value = rsi_bucket` (0..5) will work fine because the trial sweeps over exact bucket ids; but a family writing a raw RSI value (0..100 float) will almost never match, so the filter will silently reject every signal in that direction.

### 1c. `PL_SIGNAL_P0..P9` — the generic exact-match filters
Ten integer slots. Each slot `f` asks: for this signal, is `sig_filters[f]` equal to the trial's value? If the signal's own value is `-1`, the signal opts out of that filter row. If the trial's value is `-1`, the trial opts out of that filter row. Otherwise the two integers must match exactly or the signal is skipped. Ten independent AND-combined gates.

## 2. Units

- **Variant**: unit-less integer id. Range is `[0, n_variants-1]` or `-1` for off.
- **buy/sell filter**: `f64` (for historical reasons — the engine stores it as `f64` and compares as `!=`). Almost always carrying an integer-valued float (session id, regime id).
- **Generic P0..P9**: `i64`. Cast from the `f64` param vector by `as i64` — truncation toward zero, so `0.9` becomes `0` and `-0.5` becomes `0` too.

## 3. Interaction with bid / ask spread

None direct — these filters run *before* the entry bar is priced. They decide whether the signal is admitted; if admitted, the spread rules take over at the entry-price step further down. The one indirect interaction is that the `filter_value` could encode a session id, and sessions correlate with spread regimes, but the filter itself is spread-blind.

## 4. Interaction with slippage on SL fills

None. Filters are pre-entry gates; slippage is a per-bar fill mechanic.

## 5. Long vs short asymmetry

This is where the filter design is deliberately **asymmetric**.

| | Long (DIR_BUY)                              | Short (DIR_SELL)                            |
|-|---------------------------------------------|---------------------------------------------|
| Variant      | same (match on `sig_variant`)  | same                                        |
| Value filter | compares `filter_value` to `PL_BUY_FILTER_MAX` | compares `filter_value` to `PL_SELL_FILTER_MIN` |
| P0..P9       | same gates apply to both       | same                                        |

Meaning a single trial can say "only take long trades whose session is London, and only take short trades whose session is New York", by setting `BUY_FILTER_MAX` to the London id and `SELL_FILTER_MIN` to the NY id. This is a useful feature *if* the family populates `filter_value` with that specific encoding. If the family encodes something else (ATR regime, say), the asymmetric split still operates but now the split is by ATR regime not by session — the knob's *semantic* depends on the family's `filter_value` convention. **There is no engine-level check that buy and sell families agree on the meaning of `filter_value`.** A new family that encodes, say, signal strength into `filter_value` will silently collide with an older family that encoded session.

## 6. Edge cases

- **Trial sets `BUY_FILTER_MAX = 2` but every signal has `filter_value = 0.0`** — the filter rejects every buy signal silently. Trade count drops to short-only. Can look like a "great strategy" if shorts happened to be profitable in the sample.
- **Float drift on the value filter.** The engine comparison is `sig_filter_value_s[si] != buy_filter_max` on `f64`. If the Python sampler drew `2.0` but the signal family stored `2.0000000001` (because it was computed from a division), the `!=` fires and the signal is skipped. Silent-rejection bug.
- **Generic P0..P9 with truncation.** The trial's param vector slot is `f64`; cast to `i64` via `as i64`. If the sampler draws `1.4` for a slot the family expects to compare against an integer regime id in `{1, 2, 3}`, the trial tests `1`; the signal's stored regime `1` matches, trade goes through. But if the sampler draws `0.6`, `as i64` rounds to `0` (truncation), and the filter rejects `1`. A sampler drawing continuous values instead of discrete ids will spray trials across regimes **quietly**; this is only a bug if the schema wasn't supposed to allow continuous values.
- **`-1` opt-out on both sides.** Both the trial and the signal can write `-1`. If a signal writes `-1` for a filter slot *and* the trial's slot is not `-1`, the engine treats the signal as opting out and still admits it. Intended. But it means a family that forgets to populate `sig_filters[f]` (zero-initialised numpy default is `0`, not `-1`) will NOT opt out, and every such signal is held to equality with the trial slot — a very easy silent filter-too-aggressive bug.
- **Variant filter with `trial_variant >= 0 && sig_variant_s[si] >= 0`.** The variant check only fires when *both* are non-negative. If a signal's `sig_variant` is `-1` (library writes `-1` for "no variant tag"), that signal is admitted regardless of the trial's variant choice. Whether this is intended is unclear from the comments — document it explicitly in Phase 2.

## 7. What "correct" looks like for signal filters

Applying the standard from the primer §9:

1. Schema slots exist and are sampled — **verify in Phase 2**.
2. Encoding writes values into the right PL_* slots — **verify in Phase 2**.
3. Rust reads every slot inside the per-signal loop — **lib.rs:270-297 confirmed read-present; verify arithmetic in Phase 2**.
4. Arithmetic matches: equality for variant & P0..P9 (integer), equality for buy/sell (float, brittle).
5. Flipping the knob materially changes admitted trade count — **Phase 5 sensitivity**.
6. Hand-calculated scenarios match — **Phase 3 + 4**.

## 8. Silent-bug hypotheses to test

1. **Float equality on buy/sell filter drops signals due to drift.** Test: signal writes `3.0` from an arithmetic expression; trial samples `3.0` directly. Do they match?
2. **Zero-init `filter_value` collides with trial value `0`.** Test: trial sets `BUY_FILTER_MAX = 0`; signal family did not write `filter_value` (defaults to zero). Does every buy signal pass (because 0 == 0)?
3. **P0..P9 truncation masks continuous sampling.** Test: sampler draws `FloatRange(0.3, 0.9)` into `PL_SIGNAL_P0`; signal family writes `0` in `sig_filters[0]`. Does every signal admit because `as i64` truncates the trial to `0`?
4. **Variant opt-out admits cross-strategy signals.** Test: trial chooses `variant=5`; a signal with `sig_variant=-1` — does it admit?
5. **No cross-family coherence on `filter_value` meaning.** Test: two families register both of which populate `filter_value`, but with incompatible conventions. What does the trial see?

These are the questions the Phase 4 micro-test must answer.

## Codex independent brief

*Captured verbatim from `codex exec -c model_reasoning_effort=high` on 2026-04-19. Account refused the `gpt-5.4 high` model literal; high reasoning effort was passed via config override instead.*

Mechanics brief: I would disagree with two of the knob names. In the implementation described, these are not forex execution parameters; they are pre-trade signal-selection gates.

`PL_SIGNAL_VARIANT` means “only allow signals whose `sig_variant_s[si]` equals this integer.” In retail forex practice, this is not a standard market concept like spread, pip value, margin, or lot size. It is a strategy-internal selector: a pooled family/parameter/regime id. Unit: dimensionless integer id. If trial variant is `>= 0`, any signal with `sig_variant == -1` is excluded by exact match.

`PL_BUY_FILTER_MAX` / `PL_SELL_FILTER_MIN` are disputed names. In normal trading language, “buy max” and “sell min” imply threshold guards, for example “only buy if spread <= max” or “only sell if score >= min.” But the described implementation uses `==` against `filter_value`, separately by direction. So mechanically they are direction-scoped exact-value gates, not max/min range filters. Unit: whatever the producing signal family encoded into `filter_value`; that is dangerous if families disagree.

`PL_SIGNAL_P0..P9` are generic categorical gates. Each slot is exact-matched against `sig_filters[slot]`, with `-1` meaning wildcard on either side. Unit: dimensionless encoded integer. These may represent timeframe id, pattern id, indicator bucket, session code, spread bucket, etc., but the engine itself does not know.

These filters sit upstream of stop loss, take profit, position management, bid/ask handling, and slippage. Therefore they affect whether a trade candidate exists, not how it fills or exits. Spread and slippage still affect realized P/L after a signal passes. Standard retail forex execution uses ask to open longs and bid to close longs; bid to open shorts and ask to close shorts. Spread is therefore an immediate cost, and slippage changes actual fill price. See common retail forex education from NFA/CFTC and platform docs such as MetaTrader; BIS FX material is also useful for market-structure context.

Long/short asymmetry exists only where the engine creates it. `PL_BUY_FILTER_MAX` applies to `direction == +1`; `PL_SELL_FILTER_MIN` applies to `direction == -1`. There is no automatic sign flip unless the signal family encoded one. A momentum value of `+3` for buys and `-3` for sells will not be normalized by these gates. `P0..P9` and variant matching are direction-neutral unless the encoded values themselves include direction.

Edge cases are material. Float equality on `f64` is brittle if `filter_value` comes from arithmetic, indicators, normalized prices, ATR, z-scores, or continuous samplers. It is only safe when values are assigned from a small discrete set, such as `0.0`, `1.0`, `2.0`, or exact integer-like constants. Zero-initialized `filter_value` can collide with a trial value of `0`, silently selecting uninitialized or default signals. Continuous trial values cast with `as i64` truncate toward zero in Rust, so `2.9` becomes `2`, `-0.9` becomes `0`, and many sampled values collapse into unintended buckets. This makes `P0..P9` behave like coarse categorical bins, not continuous parameters.

If two signal families disagree on `filter_value`, the shared knob becomes semantically invalid. Family A might encode RSI bucket, family B spread bucket, family C pattern confidence. Exact matching then selects an accidental cross-family mixture unless `PL_SIGNAL_VARIANT` first isolates the family.

A silent no-op filter would leave sweep metrics nearly unchanged: trade count, win rate, average trade, drawdown shape, and selected signal distribution remain close to baseline. A broken-aggressive filter usually shows a sharp trade-count collapse, unstable win rate from tiny samples, missing directions, or all performance concentrated in a few accidental variants. If it accidentally matches defaults like `0`, it may look active but actually selects initialization artifacts. Reference anchors: BIS FX market surveys for FX conventions; NFA/CFTC retail forex materials for bid/ask and execution risk; Robert Pardo’s trading-system testing work for parameter-sweep and overfitting concerns.

## Diff summary

**Agreements (high-confidence shared facts):**

- All three knob families are **pre-entry signal-selection gates**, not post-entry execution parameters. They do not interact with spread or slippage directly.
- `buy_filter_max` / `sell_filter_min` names are misleading. The implementation is equality, not a max/min threshold. Both briefs flag this as a **semantic-gap candidate** before Phase 2 even runs.
- `P0..P9` behave as coarse categorical bins because the trial value is `f64` truncated to `i64` via `as`. Sampling continuous values across this cast silently collapses neighbours into the same bucket.
- Float equality on `f64` filter_value is brittle when the signal family computes the value arithmetically. Integer-like constants are the only safe convention.
- Zero-initialised `filter_value` is a collision hazard when the trial samples `0` — signals that never populated the slot will be treated as a real class.
- `filter_value` has no cross-family coherence enforced by the engine. Two families using incompatible encodings can silently cross-contaminate a pooled variant sweep.
- Silent no-op vs broken-aggressive look sharply different at sweep level: flat metrics vs collapsed trade count. This gives Phase 5 two clear predictions to discriminate.

**Disagreements (load-bearing — must resolve before Phase 3):**

1. **What happens to a signal with `sig_variant == -1` when `trial_variant >= 0`?**
   - *My reading* (from the indexed snippet `if trial_variant >= 0 && sig_variant_s[si] >= 0`): the variant check only fires when *both* sides are non-negative; if the signal writes `-1`, it is admitted regardless of the trial's variant choice.
   - *Codex's reading*: "any signal with `sig_variant == -1` is excluded by exact match" when trial_variant is `>= 0`.
   - **These are opposite conclusions.** One of us mis-read the guard. Resolve in Phase 2 by re-reading `lib.rs:270-276` line-by-line and citing the exact comparison. This is the highest-priority disagreement — if the opt-out is *not* honoured, signal pools that mix tagged and untagged variants silently drop all untagged signals whenever a trial picks a variant, and every sweep ever run has quietly under-traded.

2. **Minor: wording on `max`/`min` naming disputed status.**
   - Both briefs flag the names as misleading; Codex is more emphatic ("disputed names"); I treat it as a documentation gap. Same underlying finding, same corrective action — rename or document. No conflict for Phase 3.

**Action before Phase 3:** Phase 2 must answer disagreement #1 explicitly. If my reading wins, the `-1` opt-out on the signal side is a real feature and a family can declare itself variant-free. If Codex wins, the engine silently excludes untagged signals whenever the trial picks a variant — a separate silent bug on top of the naming mismatch.

