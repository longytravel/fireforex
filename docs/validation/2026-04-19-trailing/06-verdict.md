# 06 — Verdict: trailing stop family

Phase 6 of validate-forex-knob, run 2026-04-19 (afternoon).

## Outcome

- [ ] (a) Works as advertised.
- [ ] (b) Works, but semantics differ from the name / docs.
- [x] **(c) Broken — now fixed.** The trailing block in
      `core/src/trade_full.rs` had the same structural bug as the
      pre-v1 breakeven block: four monotonicity-only guards at
      `:222`, `:232`, `:255`, and `:268`, each allowing a trailing
      SL to be written on the wrong side of current price. The fix
      adds a `new_sl < sb_close` (long) / `new_sl > sb_close`
      (short) condition at each of the four sites. Engine version
      bumped to **v2 trailing-fix**.

## Evidence

- **Phase 1** — my brief and Codex's brief both predicted the
  same failure mode: a trailing SL placed past the close-out side
  of the current price will fire immediately and produce
  `+distance`-ish pips of impossible profit. Both briefs flag
  this as a classic backtest bug pattern.
- **Phase 2** — my trace and Codex's trace both find four
  monotonicity-only guards at the four sites listed above.
  Codex's trace adds: breakeven's post-v1 block has the correct
  `accept` shape at `:190-194`, but the trailing block did not —
  the fix was never carried across.
- **Phase 4** — 5 / 5 hand-calculated scenarios passed both
  pre-fix (confirming the bug in all three variants: fixed long,
  fixed short, ATR long) and post-fix (confirming the guard
  rejects the invalid moves, leaving the two control scenarios
  unchanged).
- **Phase 5** — sensitivity at sweep scale. The pre-fix bug
  configuration (`fixed, activate=5, distance=1`) now produces
  **identical** metrics to "trail off" — 56 trades, 17 wins,
  −206 pips. Bug fully neutralised in fixed mode. Generous
  trailing (B, E) continues to produce legitimate profit.

## The fix, in ~10 lines

Four sites in `core/src/trade_full.rs` — activation long / short
(lines 222 / 232) and ongoing long / short (lines 255 / 268) —
each gained a side-of-price condition:

```rust
// Was:   if new_sl > effective_sl { ... }
// Now:   if new_sl > effective_sl && new_sl < sb_close { ... }   // long
//        if new_sl < effective_sl && new_sl > sb_close { ... }   // short
```

`sb_close` is the conservative reference point (consistent with
the v1 breakeven fix). When the guard rejects, the trail simply
does not advance on that sub-bar — the next sub-bar may accept if
price has moved enough. No other state is touched.

## Follow-up action

1. **VERSION bumped to `v2 trailing-fix`.** `ff/VERSION.py`
   updated; web UI pill shows the new label on next server
   restart.
2. **`tests/validation/test_trailing_mechanics.py` committed.**
   5 scenarios, all green. Rows 2, 3, 4 now assert `0 pips`
   (bug rejected) and serve as regression guard against future
   edits.
3. **Golden baseline remains invalidated.** Same status as
   after the v1 BE fix. Re-pin after the first v2 sweep you
   trust.
4. **CLAUDE.md / top-level docs.** Mention v2 trailing-fix
   alongside v1 in the bug-doc series and the session history.

## Residual concerns (not in scope for v2)

- **ATR mode + small `atr_mult × atr_pips`.** Phase 5 config D
  still shows the knob moving outcomes — just in a legitimate
  "tight trail" way rather than a bug way. A minimum-stop-distance
  check (say, `new_sl < sb_close - min_gap`) would further
  suppress this, but is not required to kill the impossible-profit
  mechanism. Future work if the optimiser starts preferring very
  small ATR mults again.
- **Short SL trigger uses raw `sb_high` not ask.** Flagged in
  Phase 2 as a separate concern; not addressed here. Own
  validation run material.
- **Long SL exits do not deduct spread.** Standing spread-handling
  asymmetry, unchanged since the breakeven investigation.

## Open questions

- Does the Chandelier stop proposal need any additional guard
  shape beyond what v2 now provides? Tentative answer: no, if
  Chandelier is implemented as a mode inside the existing
  trailing block, it inherits the four guards for free. If it is
  implemented as a separate block, it needs its own
  side-of-price check from day one.

## Links

- [01-mechanics-brief.md](01-mechanics-brief.md)
- [02-code-trace.md](02-code-trace.md)
- [03-behaviour-table.md](03-behaviour-table.md)
- [04-micro-test.py](04-micro-test.py)
- [05-sensitivity-results.md](05-sensitivity-results.md)
- Live micro-test:
  `tests/validation/test_trailing_mechanics.py`.
