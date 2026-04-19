# 01 — Mechanics brief: `engine.chandelier`

> This knob was built using the `add-forex-knob` skill earlier in the
> same session. The mechanics brief was written and Codex-diffed at
> build time. Do not duplicate — read the source:

**Primary brief:** [`docs/builds/2026-04-19-chandelier-stop/01-mechanics-brief.md`](../../builds/2026-04-19-chandelier-stop/01-mechanics-brief.md)

## Summary for the validation reader

- **Definition.** ATR trailing stop anchored to the highest-high-since-entry
  (long) or lowest-low-since-entry (short), ratcheting one-way.
- **Math.**
  - Long:  `sl = max(sl_prev, peak_high - atr_mult*atr)`
  - Short: `sl = min(sl_prev, trough_low + atr_mult*atr)`
- **Units.** `atr_mult` dimensionless, `activate` pips.
- **Activation.** Arms once `float_pnl_pips >= chandelier_activate_pips`;
  until then, peak/trough track but no SL write.
- **Side-of-price guard.** Adopts `raw_sl` only if
  `raw_sl < sb_low` (long) or `raw_sl > sb_high` (short) — mirrors the
  v2 trailing fix. This was the load-bearing disagreement with Codex
  at build time; the decision to use the guard (vs Codex's "fire
  immediately" interpretation) is pinned here.
- **Sentinel.** Off → `enabled=0`, `activate=-1`, `atr_mult=-1`.
- **Interaction with siblings.** Independent Group — stacks with
  trailing / breakeven / partial by most-protective-SL-wins.

## Codex second opinion

Already collected at build time. Verbatim response + diff section in
the build-side brief. Summary of the diff:

- **Agreements:** formula, units, bid/ask sides, ratchet, long/short
  sign flips, most-protective-SL stacking, LeBeau 3.0 midpoint.
- **Load-bearing disagreement:** Codex recommended "SL above price →
  fire immediately." Build-time decision: **reject** Codex's position;
  use side-of-price guard. Reason: consistency with the v2 trailing fix
  (same shape) and to avoid reintroducing the unearned-trail-win bug.

## Why no re-run

The brief is 3 hours old and the code it describes is the code this
validation is about to trace. Re-briefing would either duplicate the
earlier output or, if Codex drifts, introduce noise — the real
independence test for this validation is the **phase 2 code trace**,
where Codex reads the actual newly-written Rust and Python blind.
Budget one Codex call there.
