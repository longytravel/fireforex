# 06 — Verdict: breakeven.offset

Phase 6 of validate-forex-knob, run 2026-04-19.

## Outcome

- [ ] (a) Works as advertised.
- [ ] (b) Works, but semantics differ from the name / docs.
- [x] **(c) Broken.** The engine accepts a BE stop-loss that is on
      the wrong side of current price and fires it on the next
      sub-bar, producing an unearned `+offset` pip win. The name
      and docs say "breakeven"; the behaviour is "exit with
      unearned +offset pips". The 78 % win-rate reported on the
      morning of 2026-04-19 is an artefact of this bug, not a
      strategy finding.

## Evidence

- **Phase 1 — Mechanics brief.** My brief and Codex's independent
  brief both predict that a correct engine should reject a long
  BE modification that places the SL above current bid, and both
  flag the 78 %-win-rate scenario as a classic silent-bug pattern.
- **Phase 2 — Code trace.** My trace and Codex's independent trace
  both find that `core/src/trade_full.rs:180` and `:185` only
  perform a monotonicity check (`be_price > current_sl` for long,
  `be_price < current_sl` for short). Neither compares `be_price`
  to `sb_low`, `sb_high`, `sb_close`, or any approximation of the
  bid / ask book. Codex's phrasing: *"I found no check rejecting
  `new_sl > current price` for a long."*
- **Phase 4 — Micro-test.** 6 / 6 hand-calculated scenarios pass
  against the live engine. Rows 2, 3, 5 — the scenarios that
  exercise `offset > trigger` — confirm the engine produces
  exactly `+offset` pips on the next sub-bar, matching the broken
  arithmetic predicted by the trace. The test is committed at
  `tests/validation/test_breakeven_offset_mechanics.py`.
- **Phase 5 — Sensitivity.** On an 800-bar seeded fixture,
  `offset = 10` and `offset = 2` produce the *same number* of
  BE-triggered wins but `offset = 10` adds ~148 pips of extra
  profit. The magnitude of each BE-triggered exit scales linearly
  with `offset` with no physical basis. This is the bug signature
  the optimiser has been rewarding.

## The missing guard

Proposed fix for `core/src/trade_full.rs:180 – 190`:

```rust
// --- Breakeven lock (deferred) ---
if breakeven_enabled > 0 && !be_locked && !pending_be_locked {
    if float_pnl_pips >= breakeven_trigger_pips {
        let be_price = if is_buy {
            actual_entry + breakeven_offset_pips * pip_value
        } else {
            actual_entry - breakeven_offset_pips * pip_value
        };

        // Side-of-price guard: a long SL must be below current price
        // (bid-side approximation: use sb_low or sb_close), and a
        // short SL must be above current price (ask-side: use sb_high
        // or sb_close). Otherwise the modification would fire
        // immediately on the next sub-bar, producing unearned pips.
        let accept = if is_buy {
            be_price > current_sl && be_price < sb_close
        } else {
            be_price < current_sl && be_price > sb_close
        };

        if accept {
            pending_sl = be_price;
            pending_be_locked = true;
            pending_trailing_active = trailing_active;
            has_pending_update = true;
        }
    }
}
```

Notes on the guard choice:

- `sb_close` is the conservative reference point — it is the
  most recent "confirmed" price on this sub-bar. Using `sb_low`
  (for long) or `sb_high` (for short) would accept a slightly
  wider set of modifications but is harder to defend.
- When the guard rejects the modification, the BE logic re-arms
  automatically on the next sub-bar, because `be_locked` and
  `pending_be_locked` remain False. This means a strategy with
  `offset > trigger` simply never fires BE — which is the
  correct behaviour, not a regression.
- No additional guard is needed at `:185` for the short case; the
  symmetric check covers both.

## Follow-up action

1. **Do not trust the current golden baseline.** The golden at
   `tests/golden/complex01_seed42_500trials.json` was pinned
   *with* the buggy BE behaviour. Sampled trials with
   `offset > trigger` contributed inflated pips to the metrics.
   The golden must be re-pinned after the fix.

2. **Pause the Chandelier stop work.** The Chandelier knob would
   inherit the same lack of side-of-price guard unless we carry
   the fix into its arithmetic. Land the BE fix first, then
   build Chandelier with the guard pattern baked in from day one.

3. **Apply the guard in one commit.** Add the `sb_close` check
   at `trade_full.rs:180 – 190`, rebuild with
   `maturin develop --release`, re-run the validation micro-test
   at `tests/validation/test_breakeven_offset_mechanics.py`, and
   update the *expected* values for rows 2, 3, 5 to reflect the
   fixed behaviour (trade continues or exits on original SL/TP).
   Commit both the engine change and the updated expectations
   together.

4. **Re-pin the golden baseline.** After the fix, run the
   500-trial complex01 sweep with seed 42 again. Update
   `tests/golden/complex01_seed42_500trials.json` with the new
   metrics. Move the current broken numbers into
   `_meta.prior_broken_numbers` as a second historical entry
   (there is already one there from the EXEC_BASIC removal
   earlier today).

5. **Consider flagging `offset > trigger` at schema level.** The
   FloatRange bounds currently permit the combination
   `offset = 10, trigger = 5`. After the fix, this combination
   is physically inert (BE never fires). Either:
   - Leave the schema alone (inert combinations waste optimiser
     budget but do not misbehave), or
   - Add a schema-level cross-knob constraint `offset < trigger`
     (cleaner but requires sampler support for constrained
     Groups).

   Cheapest path: leave the schema alone for now. The inert
   combinations will self-select out because the optimiser will
   stop preferring them once they do not print money.

## Open questions

- **Other knobs with the same pattern.** Trailing-stop
  (`core/src/trade_full.rs:195 – 266`) uses a similar
  monotonicity-only guard. Worth a dedicated validation run to
  confirm it does not have an analogous bug — e.g. a trailing
  distance smaller than the sub-bar spread could write an SL
  above current price on first activation. Candidate next
  validation target.
- **Long-side SL fills do not apply spread.** This is a separate,
  lower-priority concern surfaced in Phase 2 but not in scope
  for this run. Worth a follow-up ticket.

## Links

- [01-mechanics-brief.md](01-mechanics-brief.md) — my brief + Codex brief + diff.
- [02-code-trace.md](02-code-trace.md) — my trace + Codex trace + diff.
- [03-behaviour-table.md](03-behaviour-table.md) — hand-calculated scenarios.
- [04-micro-test.py](04-micro-test.py) — pytest scenarios, all 6 pass.
- [05-sensitivity-results.md](05-sensitivity-results.md) — sweep-scale evidence.
- Live micro-test:
  `tests/validation/test_breakeven_offset_mechanics.py`.
