# 06 — Verdict: `engine.chandelier`

## Outcome

**(a) Works as advertised.**

The `engine.chandelier` knob, newly added this session via the
`add-forex-knob` skill, behaves as the phase 1 mechanics brief
specified:

- SL anchored to highest-high-since-entry (long) or
  lowest-low-since-entry (short); ratchets one-way.
- Distance = `atr_mult * atr_pips * pip_value` — dimensionless
  multiplier over the engine's existing ATR-in-price-units tracker.
- Activation gated on float-PnL ≥ `activate_pips`.
- Side-of-price guard uses `sb_low` (long) / `sb_high` (short) —
  matches the v2 trailing-fix pattern, deliberately rejecting
  Codex's "fire immediately" interpretation.
- Sentinel `(enabled=0, activate=-1, atr_mult=-1)` short-circuits
  the entire block; no peak/trough update and no SL write.
- Exit attribution: chandelier > trailing > breakeven > plain SL.

## Evidence

1. **Mechanics brief (phase 1).** Cross-checked against Codex
   `gpt-5.4 high`. One load-bearing disagreement (guard vs
   fire-immediately), resolved in favour of the guard to stay
   consistent with the v2 trailing-fix shape.
2. **Code trace (phase 2).** My trace and Codex's independent
   trace match line-for-line across seven layers:
   `eas/complex01.json:222-245`, `eas/complex01.py:119-127`,
   `ff/encoding.py:98-134`, `core/src/constants.rs:31,106-108`,
   `core/src/lib.rs:234-236,387-389,461-463`,
   `core/src/trade_full.rs:49-51,80-92,155-162,302-369,427-436,
   453-462`. Zero hazards found on the six-point checklist.
3. **Micro-test (phase 4).** Five hand-calculated scenarios at
   `tests/validation/test_chandelier_mechanics.py`. All pass,
   including the guard-rejects edge case and the sentinel
   strict-no-op assertion.
   ```
   5 passed in 0.08s
   ```
4. **Sensitivity test (phase 5).** Added
   `test_chandelier_knob_moves_outcomes` in
   `tests/test_knob_sensitivity.py:211`. Passes.
5. **Full-sweep A/B on real data.** 500-trial EUR_USD H1 sweeps
   with chandelier forced OFF vs ON diverge in every best-trial
   knob and in total PnL (+2806 vs +2414 pips), trade count
   (356 vs 421), and max DD (16.8% vs 15.8%).
6. **Existing test suite.** 82 passed, 1 skipped (the
   `@pytest.mark.slow` golden baseline — may need re-pinning after
   this change; see open questions).

## Open questions

- **Golden baseline.** The level-4 golden at
  `tests/golden/complex01_seed42_500trials.json` was pinned
  pre-chandelier. Level 4 does not expose chandelier
  (`_optional_keys_for_level(4)` does not include it), so the
  golden should still be numerically identical. Confirm by
  running the golden test and, if it fails, re-pin with the
  user's approval.
- **Explain-bundle rendering.** `best_params_english` does not
  yet list chandelier settings in the human-readable trial
  summary. Non-blocking; a user scanning a run summary cannot
  tell at a glance whether chandelier was on without looking at
  the NPZ columns. Ticket this for a future session.
- **Pair-aware activate defaults.** `ff/defaults/volatility.py`
  `ATR_RULES` does not yet include a chandelier entry. The
  current 5-25 pip range is fixed across pairs; JPY-quoted pairs
  will see a different effective behaviour. Defer until the knob
  has real post-ship usage data.

## Follow-up action

None blocking. Proceed to phase 7 of the `add-forex-knob` skill
(ship checklist).
