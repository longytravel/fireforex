# Phase 6 — Signal filters verdict

## Outcome

Split verdict across the three filter families:

| Family | Verdict |
|--------|---------|
| `PL_SIGNAL_VARIANT` | **(a) Works as advertised.** Exact-integer variant selection with bilateral `-1` opt-out. All Phase 4 rows targeting it pass; sweep-level sensitivity confirms it moves outcomes. No code change required. |
| `PL_BUY_FILTER_MAX` / `PL_SELL_FILTER_MIN` | **(b) Works but semantics differ from the name.** Implements per-direction `==` equality on an `f64` carrier, not a `max`/`min` range. Two latent silent-bug paths: **D5** (float-drift rejection) and **D6** (signal-side `-1` is not an opt-out). Rename or redoc required. |
| `PL_SIGNAL_P0..P9` | **(b) Works but has a latent default-initialisation trap.** `ENGINE_DEFAULTS` omits P0..P9 (**D2**), so any EA that adds a Pk slot without registering it leaves the trial value at `0.0` — treated as an active filter for zero, silently dropping every signal whose `sig_filters[f] ≥ 0` is nonzero. Plus **D4** truncation by `as i64` when a sampler draws continuous values (documented design but easy to stumble over). |

None of the three families are (c) *broken* in the sense of "silently ignored" — the historical `EXEC_BASIC` shape does not apply. Every family materially moves outcomes.

## Evidence summary

- **01-mechanics-brief.md** — standard-retail brief + Codex independent brief. Codex caught semantic names mismatch; I caught float / truncation / init-hole failure modes; Phase 1 disagreement on variant opt-out surfaced and was resolved in Phase 2.
- **02-code-trace.md** — hop-by-hop trace from `ff/sampler.py:37-51` → `ff/encoding.py:32-36, 150-168` → `ff/harness.py:287, 294, 302-304, 353` → `core/src/lib.rs:235-297`. Codex independent trace converged on the same findings and added two I missed (`sig_filters` pre-fill at harness level, identity-layout making Rust's `-1` fallback dead code).
- **03-behaviour-table.md** — nine hand-calculated rows covering all six defect candidates plus three positive controls plus the Phase 1 disagreement.
- **04-micro-test.py** (= `tests/validation/test_signal_filters_mechanics.py`) — 9/9 pass; each defect is pinned against its current-engine expected count so a future fix flips the assertion and self-reports.
- **05-sensitivity-results.md** — added three sweep-level rows to `tests/test_knob_sensitivity.py` (variant, buy filter, sell filter); all pass. Silent no-op hypothesis rejected.

## Defect catalogue (from Phase 2, updated after Phase 4)

| ID | Knob | Description | Severity | Proposed fix |
|----|------|-------------|----------|--------------|
| D1 | buy / sell filter slot names | Names `*_MAX` / `*_MIN` imply range; code does `==`. | Cosmetic. | Rename to `PL_BUY_FILTER_MATCH` / `PL_SELL_FILTER_MATCH`; update docstrings. *Or* leave names and amend docstring — user choice. |
| D2 | `PL_SIGNAL_P0..P9` missing from `ENGINE_DEFAULTS` | Trial side defaults to `0.0` instead of `-1.0`; treated as active filter for value zero. Dormant today (no EA registers Pk) but a trap for the next developer. | **Latent silent-bug.** | One-line patch: extend `ENGINE_DEFAULTS` in `ff/encoding.py` to seed each `PL_SIGNAL_P0..P9` at `-1.0`. |
| D4 | Pk trial-slot `as i64` truncation | `params[col] as i64` truncates toward zero; `2.9 → 2`, `-0.9 → 0`. Continuous sampling silently buckets. | By-design but under-documented. | Docstring note in `ff/encoding.py` header; or explicit round/floor in `slot_int` helper to make intent loud. |
| D5 | Buy/sell float equality brittleness | `sig_filter_value_s[si] != buy_filter_max` on `f64`. Arithmetic drift silently rejects intended matches. | **Latent silent-bug.** | Engine-side fix: compare with a tolerance (e.g. `abs(a-b) < 1e-9`) *or* cast both sides to `i64` and do integer equality. Python-side discipline: families must always write integer-valued floats. Pick one; document the choice. |
| D6 | Buy/sell signal-side `-1` not a bilateral opt-out | Unlike variant and Pk, buy/sell guards reject `-1` when the trial side is active. Semantic asymmetry. | **Latent silent-bug.** | Engine-side: add `sig_filter_value_s[si] >= 0.0 && ...` to the inner `!=` check, matching the Pk family's shape. Two lines at `lib.rs:277-285`. |

## Open questions

1. **D1 — rename or doc-only?** The slot names are exported to Python via `ff_core.PL_BUY_FILTER_MAX`, so a rename is a breaking API change for any downstream code. Safer short-term fix is a docstring + a comment pointer in `encoding.py:15-16`. User decides.
2. **D5 — tolerance or integer-only?** A tolerance compare is backward compatible; integer-only compare is stricter but requires auditing any future signal family that touches `filter_value`. User decides after seeing the list of currently-existing consumers (session-of-hour is the only one: `ff/signal_lib.py:181-184`, integer-valued, safe).
3. **D6 — fix or document?** Two-line engine fix is cheap. But changing the sentinel semantics could break any code that currently *relies* on `-1` meaning "reject everything". There is no such code today — only the default path uses `-1` and only to mean "off". Low risk to align with variant/Pk. User decides.
4. **Chandelier stop and other incoming knobs** — user had previously flagged Chandelier as a candidate. When added, it should run through this same six-phase validation before shipping.

## Recommendation

1. **Merge the micro-test** (`tests/validation/test_signal_filters_mechanics.py`) and the three sensitivity rows added to `tests/test_knob_sensitivity.py` *as is*. These documents what the engine does today and future-proof against regressions. No engine change required to merge them.
2. **Ship D2 and D6 together** as a small Rust + Python fix with its own validation doc (`docs/2026-04-YY-the-signal-filter-bugs.md`), because:
   - D2 is a one-line Python patch that closes a trap without behavioural change on any existing EA.
   - D6 aligns the three filter families on the same sentinel convention.
   - Neither touches any currently-active feature of complex01.
3. **Defer D1 and D5** to a later pass. D1 is cosmetic and breaking; D5 is subtle and requires a decision on tolerance vs integer-only.
4. **Do not re-pin the golden baseline.** None of the defects affect complex01 — every filter in that EA maps through the variant path, which is (a). Golden is unaffected.

## Ship status

**Not shipped.** User was away during Phase 6; the skill's phase-6.5 ship checklist requires explicit user greenlight before any Rust or Python fix is applied. Phases 1-6 artifacts are on disk under `docs/validation/2026-04-19-signal-filters/` for user review on return.

## Reporting-back summary (for the top of the next session)

1. Verdict is a hybrid: **variant = (a); buy/sell = (b); Pk = (b).**
2. Six artifacts live at `docs/validation/2026-04-19-signal-filters/`.
3. Micro-test added at `tests/validation/test_signal_filters_mechanics.py` (9/9 pass).
4. `tests/test_knob_sensitivity.py` extended with three signal-filter rows (3/3 pass; full 9/9 suite green).
5. Open questions on the four defects (D1, D2, D4, D5, D6) need user decisions before any fix ships. D2 and D6 are the cheapest wins; D1 and D5 need a call.
6. Historical sweeps (complex01) are **not compromised** — the defects affect filter families complex01 does not use. No baseline re-pin needed.
