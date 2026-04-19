# Phase 6 — Partial close verdict

## Outcome: **(c) — does not work as advertised**

Two distinct bugs in `core/src/trade_full.rs` lines 285-310:

| ID    | Severity | In production? | Description |
|-------|----------|----------------|-------------|
| Bug A | High     | Yes, every run | Partial realises at `sb_close` rather than the limit price. Over-states pnl on every partial that fires on a trending sub-bar. |
| Bug B | Medium   | No, latent     | Partial checked before TP inside the sub-bar loop. If the TP clamp at `sl_tp.rs:67-69` were removed, a `trigger > tp` sample would fire partial first and realise phantom profit above the TP price. |

## Evidence

- **Code trace** (`02-code-trace.md`) — lines 285-310 show (i) the use of
  `sb_close` as the realisation price and (ii) the partial block running
  before the TP block within the sub-bar iteration.
- **Behaviour table** (`03-behaviour-table.md`) — hand-calculated
  scenarios with unit arithmetic.
- **Micro-test** (`tests/validation/test_partial_close_mechanics.py`) —
  six scenarios run against the live engine. Five fail against the
  post-fix expected values:

  ```
  row_2_partial_on_trigger_lt_tp_long   engine=+36.0  expected=+35.0   (Bug A, Δ=1.0)
  row_3_partial_on_trigger_lt_tp_short  engine=+36.0  expected=+35.0   (Bug A, Δ=1.0)
  row_4_bug_long_trigger_gt_tp          engine=+26.0  expected=+12.0   (Bug B, Δ=14.0)
  row_5_bug_short_trigger_gt_tp         engine=+26.0  expected=+12.0   (Bug B, Δ=14.0)
  row_6_partial_rescues_to_win          engine=+6.6   expected=+4.5    (Bug A, Δ=2.1)
  ```

  Deltas precisely match the hand-calculated bug magnitudes in the
  behaviour table. No ambiguity.

- **Sensitivity test** (`05-sensitivity-results.md`) — passes. The knob
  is wired end-to-end; the failure mode is wrong arithmetic, not silent
  no-op.

## What this means for the 74.5 % win rate that triggered the investigation

**Partial close is materially over-stating pnl on every trade where it
fires and the sub-bar closes above the trigger price.** The magnitude
per trade is `(sb_close - trigger) × pct`. Rough estimate on the user's
trial (trigger=44, pct=72.75 %): a typical 1 – 3 pip sub-bar over-shoot
adds 0.7 – 2.2 pips per partial trade. Across a 500-trial sweep with
~60 % of trials triggering partial, that is enough to shift expectancy
by a few pips and win-rate by 1 – 3 percentage points. **The 74.5 % win
rate is probably closer to 72 – 73 % under correct partial semantics.**

Bug B is latent and does not affect the user's current results, because
the `tp_distance >= sl_distance` clamp at `sl_tp.rs:67-69` keeps
`trigger < tp` under every production trial.

## Fix specification

Two minimal edits to `core/src/trade_full.rs`, both inside the partial
block at lines 285-310:

1. **Skip when TP has priority** (Bug B). Before firing the partial,
   check whether the same sub-bar will hit the TP, and whether the TP
   sits closer to entry than the trigger. If both are true, the TP
   fires first in real trading; skip the partial and let the TP block
   close the full position.
2. **Realise at the trigger price** (Bug A). Replace
   `(sb_close - slippage - actual_entry) / pip * close_pct` with the
   constant `(partial_trigger_pips - slippage_pips) * close_pct`. This
   matches how SL and TP are realised (at the threshold price, not at
   sb_close) and matches how real-world limit orders fill.

Estimated diff size: ≤20 lines of Rust, no new parameters, no new slots.

## Follow-up action

- Apply both fixes (Phase 6.5 of the skill — below in the same
  investigation folder the user will see `docs/2026-04-19-the-partial-
  close-bug.md` for the narrative writeup).
- Bump `ff/VERSION.py` to `v3 partial-fix`.
- Rebuild `ff_core` and restart the web server so the dashboard pill
  shows `v3`.
- Re-run the full Python test suite; re-run any pinned golden baselines;
  re-pin if the numbers shift as expected.
- **Downstream work gated on this fix**: any baseline pinned
  before this fix ships is invalidated. Chandelier stop validation and
  any new parameter sweeps should wait until the new baseline is pinned.

## Open questions

- The sub-bar over-shoot magnitude is data-dependent. It would be
  interesting to quantify the pip-per-trade Bug A impact on real
  EURUSD H1 data rather than the synthetic fixture. Not a blocker — the
  fix is the fix — but useful for the narrative writeup.
- Should Bug B's ordering fix also guard SL? If SL is on the same
  sub-bar as a partial trigger, a wild bar could fire partial
  optimistically before SL pessimistically — but partial reads
  sb_high / sb_low in the favourable direction and SL reads the
  unfavourable side, so this is less of a concern. Flagged for
  review, not blocking.
