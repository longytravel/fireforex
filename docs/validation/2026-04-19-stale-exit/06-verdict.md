# Phase 6 — Stale-exit verdict

## Outcome: **(a) Works as advertised.**

The stale-exit knob in `core/src/trade_full.rs:113-136` matches its documented semantics. Every hop from Python schema to Rust arithmetic is wired. The Rust engine correctly reads all three slots (`PL_STALE_ENABLED`, `PL_STALE_BARS`, `PL_STALE_ATR_THRESH`), computes the max-range lookback excluding the entry bar, compares against `atr_thresh × ATR_at_entry`, and exits at the triggering bar's close with slippage applied.

**No fix required.** This is the first of the four session-validated management knobs (breakeven, trailing, partial, stale) that passes clean — the prior three all had real bugs. Session hit-rate: 3/4, not 4/4.

## Evidence

| Phase | Artefact | Result |
|-------|----------|--------|
| 1 | `01-mechanics-brief.md` + Codex second opinion | Semantics agreed; four open questions identified for Phase 2. |
| 2 | `02-code-trace.md` + Codex independent trace | Every hop present; traces agree on every line number. |
| 3 | `03-behaviour-table.md` | 7 scenarios hand-calculated. |
| 4 | `tests/validation/test_stale_exit_mechanics.py` (also at `04-micro-test.py`) | 7/7 pass on first run. |
| 5 | `05-sensitivity-results.md` | Monotone activation curve; effect in predicted direction; trade count invariant as expected. |

## What passed

- **Long-side firing** at the first eligible bar (`bars_held == stale_bars`) — row 1.
- **Short-side symmetry** — same fixture, opposite sign PnL — row 2.
- **Non-firing case** (range > ceiling) — row 3.
- **Stale-OFF control** — proves rows 1, 5, 6, 7 actually fired at the documented bar — row 4.
- **`atr_thresh = 100` degenerate-to-time-exit** — row 5; this is the sensitivity-test detection configuration.
- **Entry bar excluded from lookback** — row 6; the engine correctly clamps `lookback_start ≥ entry_bar + 1`.
- **Slippage applied at both entry and exit** — row 7; round-trip 4-pip cost.
- **Sensitivity curve** is monotone in aggressiveness (shorter lookback, higher threshold → more effect). A silent no-op would not produce a monotone curve.

## Caveats (not bugs, but worth logging)

These are systemic engine properties, not stale-specific defects. They affect the knob's realism but do not break its stated semantics.

1. **Exit-price is `close[bar]` of the triggering H1 bar, not `open[bar+1]`.**
   Codex's Phase-1 brief called this lookahead-adjacent — a clean backtest convention is "detect at bar close, fill at next bar open + spread + slippage". The current engine fills at the same bar's close, which is mildly optimistic. Same property applies to `EXIT_MAX_BARS`. If the user ever cares about strict no-lookahead semantics, this is one of several spots to audit — but it's a systemic change, not a stale fix.

2. **`max_bars` wins over `stale` on same-bar ties.**
   `trade_full.rs:101` is evaluated before `:113`, so if both would fire on the same bar the recorded exit reason is `EXIT_MAX_BARS`. Codex argued `stale` is the more specific economic reason and should win. The current priority is not market-standard, merely a code-order artefact. Not a correctness bug — PnL is identical either way — but if exit-reason attribution ever feeds into strategy-quality metrics, the labelling may skew.

3. **Exit spread is not re-applied** (beyond the long/short asymmetric entry cost).
   For longs: `actual_entry = entry_price + slippage + spread_at_entry_sub`, exit at `close - slippage`. For shorts: `actual_entry = entry_price - slippage` (no spread at entry), exit at `close + slippage` with a post-loop `sell_spread` deduction proportional to `position_pct`. This long/short asymmetry in the spread accounting is a known systemic quirk — stale inherits it, does not introduce it.

4. **Rust does not defensively validate the stale slots.**
   The encoder guarantees `stale_bars = 0` and `atr_thresh = 0` when `stale_enabled = 0`, but Rust reads the three slots independently without a consistency check. If an override-JSON or hand-crafted param matrix sets `stale_enabled = 1, stale_bars = 0, atr_thresh = 0`, the effective guard short-circuits harmlessly (threshold = 0 × ATR = 0, `max_range < 0` is never true), but defence-in-depth would reject or clamp the config at the batch-evaluate boundary. Low priority; no observable bug today.

## Open questions (for follow-up, not blocking)

- **Q-A.** Would switching the stale-exit fill to `open[bar+1]` meaningfully change sweep outcomes, or is the difference within slippage noise? A quick experiment on `eas/complex01` with seed=42, 500 trials, two rebuilds of the Rust engine would settle this.
- **Q-B.** Should `exit_reason` attribution for same-bar `max_bars + stale` ties be reversed (stale wins)? This only matters if the UI or a future analytics layer surfaces exit-reason breakdowns.
- **Q-C.** Should `stale_bars` be rejected below some minimum at the Rust boundary? Codex suggested 2; the schema's lower bound is 20, so this is only a concern for unusual override paths.

## Follow-up recommendations

- **Commit the micro-test** (`tests/validation/test_stale_exit_mechanics.py`) as a permanent guardrail.
- **Keep** the existing `tests/test_knob_sensitivity.py::test_stale_exit_knob_moves_outcomes` — the two tests protect different things (semantics vs wiring).
- **Skip** the Phase 6.5 ship checklist — verdict is (a), no engine change needed.
- **Do not** re-pin the golden baseline for this verdict — the engine behaviour hasn't changed.

## Session follow-up

With breakeven, trailing, partial, and stale now validated, the remaining management knobs with documented bug potential from this session's audit (2026-04-19) are:

1. **Signal filters** (`buy_filter_max`, `sell_filter_min`, generic `PL_SIGNAL_P0..P9`) — gate every entry; highest blast radius of the three remaining targets.
2. **TP-clamp / RR semantics** — `sl_tp.rs:67-69` raises TP to at least SL distance; dashboard shows requested TP, not effective. Narrow fix (UI honesty or schema constraint).
3. **Chandelier stop / new knobs** — when added, run this same six-phase protocol.

My recommendation (unchanged from earlier in the session): signal filters next, in a fresh validation session. TP-clamp is tractable but doesn't rest on the same mechanics-brief foundation (it's more of a UI/presentation audit).

---

*File: `docs/validation/2026-04-19-stale-exit/06-verdict.md`*
*Session verdict written 2026-04-19 autonomously; user review pending.*
