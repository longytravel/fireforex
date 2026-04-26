# Handoff — 2026-04-26 afternoon (PR #31 — fail-open fixes landed, ready to merge)

**Branch:** `feat/cost-realism-overlay` (PR #31, awaiting CodeRabbit + Gemini on the new commit)
**Status:** Codex round 3's three fail-open bugs are fixed — harness now writes a `cost_realism_status` marker (`"ok"|"empty"|"failed"`), looks up per-pair slippage from `cost_table.json` (matching `reconcile_live.py`), and the live runner passes `float("nan")` instead of `0.0` on missing spread telemetry so `execution_guard.evaluate` fail-closes via `unknown_spread`. 31 cost-realism tests green, 204 broader tests green, harness smoke test now also asserts `cost_realism_status ∈ {"ok","empty"}` so a silent failure can never slip past CI again. Architectural items #32–#34 remain follow-ups.

## Goal
Make Dukascopy backtests show what live IC Markets would actually have made — by adding a post-pass cost-realism overlay (3-pip spread cap, 3-pip slippage cap, 21:00–24:00 UTC rollover skip, MT5 session-median spreads, per-pair commission, telemetry-fed slippage), with one source of truth (`gate_rules.py`) shared between the backtest gate and the live runner so they can never drift.

## Completed this session
- **Codex round 3 fail-open fixes landed** on top of the cost-realism subsystem:
  - `ff/harness.py` — added `cost_realism_status` field (`"ok"`/`"empty"`/`"failed"`) persisted to NPZ + `history.csv`. A silent overlay exception now publishes `status="failed"` so downstream readers can detect that `adjusted_pnl_total_pips` fell back to raw P&L.
  - `ff/harness.py` — telemetry slippage is now looked up per-pair from `cost_table.json` (mirrors the `scripts/reconcile_live.py` pattern), with `0.5` only as the final fillna fallback. Harness gating no longer disagrees with reconcile.
  - `ff/live/runner.py` — `spread_at_fire_pips` defaults to `float("nan")` instead of `0.0` so `execution_guard.evaluate` returns `block=True, reason="unknown_spread"` when telemetry is missing rather than silently passing the 3-pip cap.
  - `tests/test_harness_cost_realism.py` — asserts `cost_realism_status ∈ {"ok","empty"}` so a regression to silent fail-open trips CI immediately.
- Pre-existing subsystem context (still applies):
  - New subsystem `ff/cost_realism/` shipped: shared 3-and-3 gate rules, MT5-backed cost table generator, post-pass overlay, BT trade gate, slippage telemetry feedback loop. All TDD with 31 unit tests.
  - Live mirror: `ff/live/execution_guard.py` wired into the live runner before broker submit, reusing the spread reading already in scope. Slippage cap is documented as post-fill (the runner enforces after the order returns — see issue #34).
  - Reconcile script and the harness both default-ON the overlay. Harness wiring is **conservative**: existing headline metrics (`pnl_pips`, `total_pips`) and the metrics dtype are unchanged so baseline-compare and history.csv readers don't shift; new `adjusted_total_pips`, `n_gated_trades`, and `cost_realism_status` columns are appended, and a JSON-encoded enriched-trades blob lands in the NPZ.
  - 28-pair `artifacts/cost_table.json` smoke-generated successfully from real MT5 history.
  - Codex rounds 1+2 already folded in: NaN fail-open in the gate, tz-aware non-UTC timestamps coerced to UTC, absurd-spread sanity guard, CLI exits 1 on empty cost-table, None/pd.NA → NaN in bt_gate, raise on missing reconcile columns, zero adjusted P&L on gated rows. `build_comparison_html` `allow_pickle=False` regression also fixed.

## Not yet done
- **Issue #33** — live guard reads stale closed-bar spread, not the submit-time tick. Architectural; fix needs a fresh `mt5.symbol_info_tick` immediately before order send.
- **Issue #34** — post-fill slippage cap is not wired. The execution guard docstring promises it; the runner doesn't enforce it. Architectural.
- **Issue #32** — propagate the new columns through `MatchedRow` and the matched-row HTML/JSON writers so the reconcile report shows the cost-adjusted view, not just the raw view.
- **Three open scanner findings from prior PRs:** path traversal in routes (#12), out-of-bounds index in trade simulation (#13), metric-key naming mismatch (#14).
- **Backport the live runner's forming-candle fix** into the BT engine so closed-bar BT matches closed-bar live.
- **DST and exchange-local sessions** — deliberate v1 deferral. The current session-boundary table is fixed UTC; London / NY shift ±1 hour with DST. Document in spec → consider an exchange-local session module if reconcile drift on DST boundary days proves material.

## Failed approaches — DON'T REPEAT
- The original Task 7 plan said to **replace** `pnl_pips` with `adjusted_pnl_pips` as the harness's headline metric and mutate the metrics dtype. That would silently shift every existing baseline-compare result and break `app/baselines.py` + `app/static/app.js` which read fixed column names. The conservative parallel-column approach used instead is the right call here.
- Storing the enriched best-trial trades as a numpy recarray inside `np.savez_compressed`. Recarrays with datetime64/object columns require pickle, which the existing comparison HTML loader explicitly disallowed. Storing as JSON-encoded uint8 bytes round-trips cleanly without the pickle dependency.
- Inserting the live execution guard at the top of `_poll_one_pair`. That short-circuits other legitimate early-returns (duplicate-plan dedup, position-cap). Always insert immediately before the broker submit call.

## Exact resume steps for next session
1. Watch CodeRabbit + Gemini on the new commit; resolve any threads.
2. Squash-merge PR #31 via `bash scripts/merge_pr.sh 31`; sync local main with `bash scripts/sync_main.sh`.
3. Tick the two PROGRESS.md items (Cost-realism overlay + Execution Guard module) once the merge lands.
4. Pick up issue #32 (MatchedRow propagation) as a small follow-up PR — should be the smallest of the three follow-ups.
5. Then issues #33 (submit-time spread tick) and #34 (post-fill slippage cap) — both architectural live-runner work.

## In flight
- PR #31 — open with the round 3 fixes pushed; awaiting CodeRabbit + Gemini re-review.
