# Plan — delete the EXEC_BASIC/EXEC_FULL distinction entirely

**Status:** 2026-04-19. Revised after the user's call to remove the mode
switch instead of making it smart. Awaiting approval before editing code.

## The decision

Fire Forex forked away from ClaudeBackTester to keep things simple. The
two-mode execution path (`EXEC_BASIC` + `EXEC_FULL`) is legacy complexity
inherited at the fork. There's no reason to preserve it — the full path
handles every case the basic path handles (a trailing stop with mode=OFF
is a no-op, same for BE/partial/stale/max-bars). We should delete the
basic path entirely. One mode, no switch, no future silent-ignore bug.

The previous version of this plan proposed a runtime mode detector. That
adds a branch and a test and another place future Claude (or future you)
can get it wrong. Deleting the option is strictly better.

## Why this works

The two Rust code paths today:

- `core/src/trade_basic.rs` (188 lines) — SL/TP fills on sub-bars only.
  No management knob logic.
- `core/src/trade_full.rs` (435 lines) — everything in basic, plus
  trailing, BE, partial, stale, max-bars.

Claim: with all management knobs set to OFF/zero, `trade_full.rs`
produces the same trade list as `trade_basic.rs`. If true, basic is
purely redundant. We need to prove this before deleting.

## The fix — phased

### Phase A — prove basic is redundant (no edits)

- **A1.** Read `trade_full.rs` and confirm it has the same SL/TP
  sub-bar walk as `trade_basic.rs`, gated on the management flags.
  Specifically, confirm that when `trailing_mode=TRAIL_OFF`,
  `breakeven_enabled=0`, `partial_enabled=0`, `stale_enabled=0`, and
  `max_bars<=0`, the loop degenerates to the same scan order as basic.
- **A2.** Write a Rust unit test that runs both paths on the same
  input and asserts bit-for-bit equal trade lists. If it passes, basic
  is provably redundant.

**Exit criterion:** green Rust test proving equivalence. If it fails,
stop and investigate.

### Phase B — delete the basic path

- **B1.** Remove `core/src/trade_basic.rs`.
- **B2.** Remove `mod trade_basic;` from `core/src/lib.rs`.
- **B3.** Remove the `exec_mode: i64` argument from
  `batch_evaluate`'s pyo3 signature.
- **B4.** Remove the `if exec_mode == EXEC_BASIC { ... } else { ... }`
  branch inside `batch_evaluate`. Always call the (renamed) trade
  loop.
- **B5.** Rename `trade_full.rs` → `trade.rs` since there's no
  longer a second path to contrast it with. Same for the function
  `run_trade_full` → `run_trade`.
- **B6.** Remove `EXEC_BASIC` and `EXEC_FULL` from
  `core/src/constants.rs`.
- **B7.** Rebuild: `maturin develop --release` from repo root.

### Phase C — update Python to match

- **C1.** `ff/harness.py`: delete the `bc.EXEC_BASIC` argument from
  both `batch_evaluate` call sites (lines 314 and 355). Signature
  shrinks by one.
- **C2.** Delete any reference to `EXEC_BASIC` or `EXEC_FULL` in
  the Python code. Grep confirms there are none outside harness.

### Phase D — safety net against this class of bug recurring

- **D1.** Write `tests/test_knob_sensitivity.py` with one test per
  management knob. Each constructs a minimal EA where the only
  varying input is that knob, runs two trials (ON / OFF), and
  asserts the PnL or trade count differs. If any knob goes dead in
  a future refactor, its test fails immediately.
- **D2.** The tests are parametrised so adding a new knob (e.g.
  Chandelier) takes one line.

### Phase E — re-pin the golden baseline

- **E1.** Run complex01 with seed=42, trials=500 under the fixed
  engine. Capture the new numbers.
- **E2.** Update `tests/golden/complex01_seed42_500trials.json`
  with the new values. Add a `_meta.description` note explaining
  that the prior values (trades=616, pips=822, etc.) were from the
  broken EXEC_BASIC path and are no longer valid.
- **E3.** Delete the now-outdated `_meta.known_caveat` in the
  golden file.
- **E4.** Re-run the existing `test_golden_complex01_seed42_500trials`
  and confirm it passes against the new golden.

## Verification — how we know the fix works

1. **Phase A2 green** — Rust proves the two paths produce identical
   results when management knobs are off.
2. **After Phase B+C, Rust tests still pass** — the 14 existing Rust
   tests (none of which tested the basic path directly from Python)
   still run green because they live inside `trade_full.rs`.
3. **Phase D tests go green** — every management knob measurably
   moves PnL when flipped. Any knob that doesn't is still silently
   broken and needs separate investigation.
4. **Phase E golden passes** — the new numbers are locked. From this
   point forward any shift in PnL / trade count / best variant is
   caught by the golden test.

## Expected numeric impact

Today's complex01 seed=42 500-trial output:

- trades 616, win 6.49%, total pips +822, expectancy +1.34, PF 1.227.

These numbers were produced under the broken basic path. After the
fix they will change — and that's the point. The new numbers are the
first ones that reflect the EA's declared behaviour.

The win rate in particular should move meaningfully: a trailing stop
that actually trails will convert some losers to smaller losers and
some winners to early exits. PF and expectancy will shift; max DD
will probably drop; trade count may change because max-bars now
really limits trade duration.

## Rollback path

If something goes wrong after deletion, we can recover `trade_basic.rs`
from git history. But we won't want to — basic was pure code debt.

## What this plan is NOT addressing

The other findings from this morning's audit:

- `app/jobs.py` lock released in worker thread (concurrency bug)
- `history.csv` not crash-safe under concurrent writes
- `_PARQUET_CACHE` unbounded
- Rust trade panics silently zero metrics (Codex flagged)
- NaN/Inf can poison ranking
- `layer_name` path-traversal in `/api/run`
- Same-M1 break-even vs SL collision edge case
- `PL_BUY_FILTER_MAX` / `PL_SELL_FILTER_MIN` misleading names

Those are separate issues, each with its own plan. This plan tackles
only the biggest-impact one.

## Effort estimate

- Phase A: 30 min (read + Rust equivalence test).
- Phase B: 30 min (Rust deletions + rebuild).
- Phase C: 10 min (Python call-site updates).
- Phase D: 60 min (five sensitivity tests).
- Phase E: 10 min (rerun + update JSON).

**Total:** about 2.5 hours of focused work. All reversible via git.

---

*Waiting for approval. Say "go" and I'll do Phase A first (proof the
full path handles the all-off case), pause there for review, then the
rest.*
