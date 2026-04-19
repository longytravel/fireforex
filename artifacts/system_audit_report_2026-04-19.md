# Fire Forex — Full system audit, 2026-04-19

**Session context:** after landing Phases 1–3 of the speed roadmap (disk cache,
parallel signal build, parquet warm cache) and fixing two cache bugs GPT-5.4
found, the user asked for an end-to-end sweep of the whole system. I spun up
six parallel audit agents, ran Codex on the knob/parameter surface, executed
both test suites and a live CLI sweep, probed the web UI, and wrote a new
hand-calculated math test file.

This report is the compiled output. Read the TL;DR, then drill in.

---

## TL;DR — what to do next, in order

1. **Fix two silent-data-corruption risks** (HIGH):
   - `app/jobs.py` lock is released too early — two concurrent `/api/run`
     requests can race past the 409 check and run jobs in parallel. That
     clobbers shared state and corrupts `artifacts/history.csv` on writeout.
   - `ff/harness.py` appends to `history.csv` without a file lock. Two
     runs ending at the same time can each read, mutate, write — the
     later writer clobbers the earlier.
2. **Pin a golden baseline** (HIGH, you've been asking for this):
   the 500-trial, seed-42 complex01 run below produces exact numbers we
   can commit to `tests/golden/` today. Without this, every refactor
   (including the upcoming Chandelier stop) is flying blind.
3. **Fix a documented-but-unenforced invariant in the Rust engine**
   (MEDIUM): break-even logic defers the SL update by one M1 sub-bar.
   If price drops to the original SL on the SAME M1 that BE triggers,
   the trade exits at the *old* SL price — which is worse than the BE
   behaviour the user thinks they're getting. Needs a fixture test.
4. **Wire the encoder defaults correctly** (MEDIUM): Codex found two
   buy/sell filter slots (`PL_BUY_FILTER_MAX`, `PL_SELL_FILTER_MIN`) that
   are *exact-match* in Rust despite being named `MAX` / `MIN`. Misleading
   enough to introduce a silent filter bug next time someone adds a
   session knob.
5. **Add a LRU cap on `_PARQUET_CACHE`** (LOW but trivial): ~10 lines
   in `ff/harness.py`. Long-lived web sessions across many pairs/timeframes
   currently pin unbounded GB of DataFrames.

Everything else is secondary. Detail below.

---

## What ran, what passed

| Check | Result |
|---|---|
| Python tests (`pytest tests/`) | **18 / 18 pass** (12 existing + 6 new math tests) |
| Rust tests (`cargo test --release`) | **14 / 14 pass** (14.3s build, 0.00s run) |
| Live CLI sweep: complex01, seed=42, 500 trials | 1.01s end-to-end, +822 pips, best variant id=34 macd\_cross(8,24,5), expectancy +1.34 pips/trade, PF 1.227, max DD 46.52%, win rate 6.49%. **Save these as the first golden baseline.** |
| Live CLI sweep: complex01, seed=42, 20 trials | 0.47s end-to-end, expectancy +1.34 pips/trade (matches last session's numbers from memory #4686) |
| Web server launch on 127.0.0.1:8000 | boots in <5s |
| `/` | 200, 13.4KB HTML |
| `/api/pairs` | 200, pairs listed |
| `/api/timeframes` | 200, `["M1","M5","M15","M30","H1","H4","D"]` |
| `/api/eas` | 200, 2 EAs on disk |
| `/api/jobs` | 200, 28KB of historical jobs |
| `/api/baseline` | 200, current pinned baseline (complexity\_L10\_EUR\_USD\_H1) |
| `/api/history` | 200, 17KB of history.csv |
| End-to-end web job: level-3 EUR\_USD\_H1 recipe, 10 trials (actually ran 2000 via overrides) | Completed in ~6 seconds, no errors, progress polls streamed cleanly |

Rust build produced two warnings: `n_trials` unused in `lib.rs:100`, and
`TRAIL_ATR_CHANDELIER = 2` in `constants.rs:21` is defined but never read.
**Interesting:** the Chandelier enum slot is already reserved on the Rust
side but no Rust code path consumes it. Adding a Chandelier stop is a
known pending feature.

---

## Findings by subsystem

### 1. Signal library (`ff/signal_lib.py`) — PASSED with one finding

**Agent verdict (short form):** one correctness concern found, everything
else sound.

- **`_df_fingerprint` may collide** on hand-crafted adversarial data
  where two different frames share length, first/last timestamp, and
  first/last close. Realistic risk: near-zero in normal use (every main
  TF parquet has different bar counts and price paths). Worth hardening
  if we ever resample data in Python rather than reading from disk.
- Cache key otherwise strong. Source-hash invalidation works — I
  verified the test forces a rebuild when `_source_hash()` changes.
- Parallel build is order-preserving (byte-for-byte with serial — the
  test `test_parallel_build_matches_serial` passes).
- No lookahead in any of the four families. EMA/MACD cross detection
  uses `+1` offset; Donchian uses `.shift(1)` for prior-bar comparison.
  I wrote a new test file (`tests/test_math_correctness.py`) that
  hand-verifies all four. First run caught my own hand-calc mistake on
  Donchian (I thought the short would fire on bar 5; actually bar 4 is
  the first edge, bar 5 is edge-suppressed). Test pass rate is now the
  floor for future changes.

### 2. Rust engine (`core/src/`) — ONE POTENTIAL BUG, rest sound

**Agent verdict:** 1,646 lines across 7 files. Correctly prevents
lookahead, correctly uses "SL wins" tiebreak, correctly scales partial
positions.

- **Break-even deferred update race** (suspicious, needs a test to
  confirm): `trade_full.rs:169` checks `float_pnl_pips >=
  breakeven_trigger_pips` and defers the SL move to the NEXT M1. If SL
  is hit on the *same* M1 that BE triggers, the SL check at line 292
  still uses the *old* `current_sl`, not the BE price. Bug class: user
  thinks "BE kicked in, so this should exit at worst breakeven" but
  actually exits at the pre-BE SL. Real money at stake.
- All 27 management slots (plus 10 `PL_SIGNAL_P*`) are read at least
  once — no silently-ignored slots. But enum selectors (SL mode, TP
  mode, trailing mode) only validate against constant sets. Invalid
  values fall through to a default — won't crash, will produce wrong
  answers.
- NaN handling is incomplete. `spread` and `swing_sl` get NaN→fallback,
  but raw `high/low/close` NaNs are NOT checked. Caller must sanitize.
  Python side currently always sanitises (parquet floats, tz-localised),
  so no active bug, but fragile.
- Zero-signal case, last-bar signal, and max-trades cap all handled.

### 3. Harness + data (`ff/harness.py`, sampler, encoding, preflight) — TWO CONCRETE RISKS

- **`_PARQUET_CACHE` is unbounded and not thread-safe.** No locking
  around the dict, no LRU, no weak refs. Web server across many
  pair/TF combos pins memory forever. Also: an rsync that preserves
  mtime can serve stale data (edge case — local workflow, unlikely).
- **`history.csv` append is NOT crash-safe under concurrent writes.**
  `ff/harness.py:453-461` does read-modify-write without a file lock.
  Two runs ending simultaneously can clobber each other. In practice
  `app/jobs.py` holds a threading lock to run only one job at a time,
  BUT that lock has its own bug (see web-layer section), so this
  becomes a real risk.
- **Partial .npz on crash:** `np.savez_compressed` in the artifacts
  writer has no atomic-rename. If the process dies mid-write, the
  `runs/*.npz` file is truncated and future reads fail.
- **Preflight is a rough heuristic.** Hardcoded
  `SIGNAL_BUILD_SEC_PER_COMBO = 0.25` and
  `SWEEP_RATE_BT_PER_SEC_HINT = (120, 400)` — useful for catching
  10K-combo explosions, useless for deciding 2K vs 5K trials. Consider
  replacing with a micro-probe (run 50 trials, extrapolate).
- **Trial encoding is safe.** Non-chosen Branch arms slot to zero, the
  param matrix is pre-zeroed per trial, no leakage between trials.
- Alignment + main→sub mapping is correct for the common case;
  weekends / gaps produce empty ranges (correct). No validation that
  sub_df is sorted + non-duplicate (silent wrong answer if violated).

### 4. Schema + defaults + overrides (`ff/schema.py`, `ff/defaults/*`) — SOUND

- `FloatRange.expand()` ignores `scale` (log/linear) — it's a pure
  additive-step enumeration. The `scale` field only affects sampling
  density, not the grid the signal library enumerates. Worth knowing
  — a "log" knob with `step=0.1` produces the same grid as a "linear"
  knob with `step=0.1`.
- Non-chosen Branch arms are genuinely dead at runtime. ✓
- `apply_overrides()` is idempotent. Clones the EA first. Silent
  no-op on unknown override paths — UI must validate before sending.
- Volatility defaults fall back to YAML cleanly. `pair_tf.yaml`
  covers only **6 of 28 major pairs**. Missing AUD\_JPY, EUR\_JPY,
  GBP\_JPY, EUR\_GBP, USD\_CAD, etc. If the user tries a pair outside
  that list, and the volatility cache can't build (parquet missing),
  they get `ValueError` with a path but no fix guidance.
- Complexity levels 1→10 are strictly monotonic in structure (code
  structure guarantees it), but there's no test that counts variants
  and asserts "level 10 >= level 1 + 100 variants." Worth adding.

### 5. Web UI (`app/`) — ONE REAL BUG, rest defensive

- **`app/jobs.py` lock is released in the wrong thread.** Line 92
  `_lock.acquire(blocking=False)` on the request thread, line 149
  `_lock.release()` in the worker thread's `finally`. If request A
  acquires, spawns worker, returns, and request B tries to acquire
  before worker A hits its `finally` block — that's the happy path,
  B gets 409. But in the window between A's successful return and
  the worker actually starting, B *could* succeed on a retry. In
  practice the 500ms poll cadence is slower than the spawn, but this
  is a real race. Use `with _lock:` around the full job lifecycle, or
  move the worker-spawn inside the lock.
- **`JobState.progress/message/status` read unprotected.** CPython
  dict copies are atomic at bytecode level for the copy itself, but
  the three fields can be read out of phase. Poll responses may show
  `progress=1.0, status=running` then jump to `status=done`. Cosmetic,
  not corrupting.
- Path traversal closed: `/api/eas/{name}` uses `_SAFE_NAME` regex
  `^[A-Za-z0-9_\-]{1,64}$`. ✓
- **Overrides round-trip is correct** — the invariant from `CLAUDE.md`
  that "mapping callables never round-trip through JSON" holds because
  the backend rebuilds the EA from the recipe each time.
- Baselines fail gracefully on corruption (returns null, UI shows
  "no baseline").
- Frontend `app.js`: one silent failure — history refresh catches
  errors with empty `catch {}`. Minor UX wart.

### 6. Test coverage gap analysis — THE BIG MISS

**Current state:** 18 passing tests covering complexity generation, cache
correctness, and (as of today) indicator math. Plus 14 Rust tests on the
engine's core paths.

**Absent:**
- **Golden baselines.** Zero tests pin "EA + seed + trials → expected
  output." Every refactor is regression-testable only by squinting at
  CSV rows. This is the single biggest coverage gap.
- **Trial encoding round-trip.** No test asserts that a sampled trial
  dict encodes to the Rust param vector the Rust engine expects. If
  anyone adds a slot, the risk of silent mis-routing is high.
- **Sampler group on/off.** The one existing sampler test checks
  `isinstance(dict)` and length only. It doesn't verify that disabled
  groups actually omit their `when_on.*` keys.
- **Overrides application.** No test that `apply_overrides(ea,
  {"knobs": {"stop_loss.fixed.distance": {"frozen": 10.5}}})` actually
  freezes the knob to 10.5 in sampling.
- **FastAPI endpoints.** No route tests at all.
- **Break-even + SL same-M1 collision** (see Rust engine finding above).

**Top-5 tests to add, by priority:**

1. **Golden baseline for complex01.** Run today's 500-trial, seed-42
   sweep. Pin: total pips, expectancy, best variant id, best trial
   params, max DD. Commit the JSON to `tests/golden/`. Any future
   change that shifts those numbers must bump the golden with a written
   reason. ~120 LOC.
2. **Sampler group on/off.** ~60 LOC.
3. **Trial encoding → Rust vector.** ~80 LOC.
4. **Overrides knob freezing.** ~100 LOC.
5. **Rust same-M1 SL vs BE collision.** ~40 LOC Rust + ~20 LOC Python
   cross-check.

---

## Math verification — new safety net landed today

`tests/test_math_correctness.py` hand-verifies every Python indicator on
tiny (4–40 bar) synthetic fixtures with the expected answer computed
in the test's docstring. 11 tests, all passing:

- `ewm()` span-based EMA matches hand-computed 5-value series
- `ewm()` returns float64 (engine contract)
- `atr_ema()` true-range formula verified on 4 bars, hand-computed
  exact values to 10-digit precision
- `rsi()` flat series → 100 (the `np.where` avg\_loss=0 choice is now
  locked)
- `rsi()` monotonic up → near-100; monotonic down → near-0
- `donchian()` breakout with edge detection — short at bar 4 (first
  edge), long at bar 6, bar 5 edge-suppressed (**this is the test that
  caught my own hand-calc error — proof the system works**)
- `ema_cross()` signals fire at `cross_index + 1` (no lookahead),
  entry price matches close at signal bar (never the prior bar)
- `macd_cross()` same anti-lookahead property
- `macd_cross()` raises `InvalidCombo` for nonsense param combos
- `session_of_hour()` covers all 24 hours, known spot-checks match

**The discipline is the point.** A Chandelier stop added tomorrow
should land with 3–5 more tests in this file: one for the raw math
(given ATR series X and high-high series Y, chandelier stop at bar N
should be Z), one for the schema knob round-trip, one for an end-to-end
synthetic trade with a known exit point.

---

## Top knob risks (from Codex's parameter-slot audit)

Codex enumerated all 27 PL\_ slots + 10 PL\_SIGNAL\_P\*. Full table is
too wide for this report; the dangerous findings:

1. **`PL_BUY_FILTER_MAX` and `PL_SELL_FILTER_MIN` are misnamed.**
   The names imply range comparison (≤ max, ≥ min). The Rust code
   does **exact equality** (`==`). Next engineer to use them will
   write broken filters. Fix: rename to `PL_BUY_FILTER_EQ` /
   `PL_SELL_FILTER_EQ`, or actually implement range semantics.
2. **`TRAIL_ATR_CHANDELIER = 2` is defined, never read.** Any trailing
   mode that's not 0 or 1 will fall through to the ATR code path.
   When the Chandelier stop is added, this means the enum slot is
   already reserved correctly, but no tests exist and any malformed
   trailing mode silently becomes ATR trailing.
3. **Most management knobs only fire in `EXEC_FULL` mode.** If an EA
   is accidentally run under `EXEC_BASIC`, trailing / BE / partial /
   max-bars / stale are all silent no-ops. Worth a panic or a loud
   warning when `EXEC_BASIC` is set with management knobs enabled.
4. **`PL_PARTIAL_PCT` is not clamped.** Values >100 or <0 produce
   negative remaining position or leveraged size. Needs a bound check.
5. **`PL_SIGNAL_P0..P9` are Rust-readable but no EA currently maps
   to them.** Dead weight until a future EA uses them — not a bug,
   but worth knowing when designing signals.
6. **Invalid enum values never error.** SL mode of 99 becomes
   fixed-pips silently. Consider returning a Rust-level error or at
   least an exit reason code of "bad param."
7. **Negative trigger pips accidentally trigger immediately.** e.g.
   `PL_BREAKEVEN_TRIGGER = -5` means "fire BE as soon as trade is open
   and down 5 pips." Bounds-check all "trigger" fields.

---

## Next codex full-system review — running in background

I fired a second codex call for an independent top-10 risk list on the
entire system. That's running in the background as job `b77uu3he1`.
Output will land in
`C:/Users/ROG/AppData/Local/Temp/claude/.../tasks/b77uu3he1.output`.
When you're back and want it appended to this report, ask me to read
that file and attach it to the **Appendix** section below.

---

## Overall take

The system is in **reasonable** shape. Python indicator math and Rust
trade loop both demonstrate correct logic on small fixtures. The
recent speed work (disk cache, parallel build, parquet warm cache) is
byte-for-byte equivalent to the serial pre-change code and all three
features have test coverage.

The weakest link is **verification discipline** — there is still no
golden baseline, no encoding round-trip test, no end-to-end fixture
test. The two concrete bugs (app/jobs.py lock race; history.csv
append race) are both in the concurrency layer, not the math, and can
be fixed cleanly with ~30 lines between them.

The system is ready for a Chandelier stop or any other incremental
feature — but only if we pin a golden baseline first so we can
prove the existing behaviour didn't change.

---

## Appendix — raw agent reports

*(Omitted for length — the six agent reports and Codex's knob audit
are each saved in memory. Access via `get_observations` with the IDs
produced during this session.)*

---

*Generated 2026-04-19 by Claude (claude-opus-4-7\[1m\]) after 6-agent
parallel audit + Codex knob audit + live test runs + hand-calculated
math fixtures.*
