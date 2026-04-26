# Handoff — 2026-04-26 night (live reconcile + execution guards)

**Branch:** `fix/live-reconcile-and-guards` while this refresh is in review. If this file is on `main`, the bundle has merged.

## Tonight's work — issues #32, #33, #34 closed in one branch

`fix/live-reconcile-and-guards` bundles three live/reconcile follow-ups that all touch the cost-realism story:

- **#32 — `MatchedRow` carries cost-realism columns.** `ff/live/reconcile.py` now propagates `bt_raw_pnl_pips`, `bt_overlay_delta_pips`, `bt_adjusted_pnl_pips`, `bt_gated_out_reason`, `bt_effective_pnl_pips` into every matched row and surfaces them in `render_report_json`. Legacy backtests (pre-#31) fall back to `bt_pnl_pips` so the column is always populated. `ReconcileReport.by_pair` adds `matched_effective_pnl_pips_bt` so a gated trade reads as 0 in the rollup, not raw P&L.
- **#33 — execution_guard reads submit-time tick.** `MT5Broker.current_spread_pips()` queries `mt5.symbol_info_tick` on demand; the runner uses it in `_evaluate_and_fire` immediately before invoking `execution_guard.evaluate`. Previously the guard saw the closed-M1 mean (sometimes 30+ s stale) and could let a spike-time fire through the 3-pip cap. Falls back to the closed-bar value when the broker shim does not expose the new method (synthetic tests).
- **#34 — post-fill 3-pip slippage cap.** After `submit_market_order` returns, the runner computes signed `fill_slippage_pips = (fill_price − entry_ref_price) / pip_value × direction` and feeds it to `gate_rules.is_slippage_too_wide`. When the cap fires, the position is closed via `broker.close_position(reason="slippage_3p")`, never registered in `state.open_positions`, and the ticket row is tagged `slippage_killed=True` for the reconciler.

Tests added: `test_reconcile.py` (4 cost-realism propagation cases — already authored by the user before this session), `test_broker_mt5_submit.py` (2 cases for `current_spread_pips`), `test_live_runner_synthetic.py` (4 cases for fresh-tick guard + post-fill slippage cap). Full suite: 270 passed.

**Branch (older snapshot):** `docs/pr35-handoff-automerge-guardrails` while this refresh is in review. If this file is on `main`, the docs/tooling refresh has merged.
**Main status:** `origin/main` includes PR #35 as squash commit `c6c66da` — History tab cost-realism decomposition columns are shipped.
**Local sync note:** pre-sync local edits to `HANDOFF.md` / `artifacts/history.csv` were protected in stash `pre-sync local handoff/history before PR35 docs refresh` before fast-forwarding local `main`.

## Goal
Make Dukascopy backtests show what live IC Markets would actually have made — cost-realism overlay (3-pip spread cap, 3-pip slippage cap, 21:00–24:00 UTC rollover skip, MT5 session-median spreads, per-pair commission, telemetry-fed slippage), with one source of truth (`gate_rules.py`) shared between the backtest gate and live runner. The UI now decomposes adjusted P&L so users can see *why* adjusted differs from raw.

## Late-evening update — optimiser ranks by IC-realistic adjusted P&L (Option C)

`ff.harness.run` now ranks trials by an IC-aligned adjusted-P&L proxy whenever `artifacts/cost_table.json` covers the swept pair. The proxy is `total_pnl + n_trades * per_trade_overlay_charge_pips(pair)`, where `charge = bt_commission_proxy_rt − ic_real_cost_rt` is computed once from the cost table's liquid-session means. When the table is missing or has no entry for the pair, the optimiser silently falls back to the legacy composite Quality objective. Logs `[pick_best] proxy charge per trade = ±X pips; ranking N trials by adjusted_pnl_proxy` so it's obvious what happened.

**Why:** users want the picked trial to match what is likely to happen live. Quality balanced Sharpe / DD / return on Dukascopy spreads, which under-charged execution cost vs IC Markets — so the headline trial often had high return-per-trade but few trades, which doesn't survive realistic per-trade cost. Option C charges every trial the IC delta and lets sweeps with high-volume, lower-expectancy strategies that still beat IC reality win the slot.

**Engineering choice:** the proxy column lives outside `metrics_out` because adding a column would require bumping `NUM_METRICS` in the Rust engine. `pick_best` now accepts an `objective_array=` kwarg that overrides the metric-column lookup with a synthetic per-trial array (higher-is-better; tie-breakers + profitability filter still come from `metrics_out`). No `core/` changes; no UI changes (existing Cost / Adj. pips / Gate save columns already decompose the realistic side).

**Golden re-pinned.** `tests/golden/complex01_seed42_500trials.json` shifted from a 33-trade outlier-Quality trial (337 pips, PF 5.029) to a 136-trade EMA-cross trend-follower (1248 pips, PF 1.545). The shift is the intended effect; description updated with prior values for traceability.

## Late-afternoon update — MT5 tick downloader resolves cost-realism data quality (PR #42)

The structural floor-bias in MT5 M1 `spread` (issue #39) is sidestepped by switching the cost-table source to **MT5 tick data**. New `ff/data/mt5_tick_downloader.py` pulls per-pair tick history (bid/ask per quote change) via `mt5.copy_ticks_range()` into `{MT5_DATA_ROOT}/{pair}_TICK.parquet`. `ff/cost_realism/cost_table.py` now prefers tick parquets and computes `spread = ask − bid`; entries tagged `spread_source: "tick"` (preferred) or `"m1"` (legacy fallback).

`scripts/fetch_mt5_ticks.py` is the bulk downloader. Verified end-to-end: 28/28 default pairs downloaded (90-day window, 5-7M ticks per major), all 28 in `artifacts/cost_table.json` with realistic per-session spreads. Cross-pair lower-bound floor relaxed from 0.3 to 0.15 pips after calibration against IC Markets tick history.

Last-run summary panel (`app/static/app.js` KPI grid) now shows Adj. pips / Gate save / Cost / Gated cards alongside the raw KPIs, so cost-realism decomposition is visible on the run summary, not just in the History tab.

The earlier 0.1-pip cost-table bug is fully resolved as far as user-visible UI is concerned. The M1 path remains as a documented fallback.

## Late-afternoon update — paperwork gate now enforces PROGRESS.md too

`.github/workflows/pr-checklist.yml` previously required `HANDOFF.md` (always) and `docs/ARCHITECTURE_MAP.md` (on map-sensitive paths) on any PR touching durable paths. After today's session it caught me forgetting `PROGRESS.md` and `ARCHITECTURE_MAP.md`. Added a parallel rule for `PROGRESS.md` so it is now CI-enforced alongside `HANDOFF.md` on durable PRs. `.claude/rules/workflow.md` Paperwork section updated to match.

## Late-afternoon update (cost-table validator + structural data-source finding)

A second cost-realism PR shipped after the morning's #35/#36: `fix/cost-table-mean-spread-validator` switches the cost-table builder from `median()` to `mean()` per session and adds a per-pair lower-bound floor (USD-majors ≥ 0.05 pips, crosses ≥ 0.3 pips). Discovered while debugging why `Cost` overhead was *positive* (i.e. overlay was *refunding* pips) on every survivor of every run: median on the MT5 M1 `spread` distribution returns the broker's 1-point quote-rounding floor (50%+ of bars sit there), making real cost look like 0.1 pips on AUD/NZD, CHF/JPY, etc. Overlay then computed `bt_cost - real_cost` ≈ +0.7 pips/trade on every pair.

Mean is the right statistic but doesn't fully rescue the data: 25 of the 28 default pairs still fail the lower-bound floor because **only the NY session contains genuine quote variation in the MT5 M1 `spread` field**. Non-NY bars almost always close on a 1-point tick (broker's quote-rounding minimum) regardless of true bid/ask. This is a structural limitation of MT5 OHLC data — `spread` is sampled once per bar at close, not time-averaged. Follow-up tracked in PROGRESS as "MT5 M1 spread is structurally floor-biased". Until resolved, the local `artifacts/cost_table.json` will contain ~3 pairs and the overlay will skip the rest (raw + gate effect only, no cost adjustment shown for skipped pairs).

`scripts/inspect_cost_overhead.py` is a forensic diagnostic created during the investigation; left untracked (ad-hoc local tool).

## Completed this session
- **PR #31 merged** as commit `63a3faa` — full cost-realism subsystem (5 PRs bundled): BT post-pass gate/overlay, cost table, telemetry-fed slippage, live execution guard sharing `ff/cost_realism/gate_rules.py`.
- **PR #35 merged** as commit `c6c66da` — History tab now shows:
  - `Adj. pips` — `adjusted_total_pips` after overlay
  - `Gate save` — pips saved by dropping bad-cost trades
  - `Cost` — spread/commission/slippage overhead on surviving trades
  - `Gated` — dropped-trade count
  - `CR` — cost-realism status pill (`ok` / `empty` / `failed`)
- **Harness decomposition persisted** to NPZ + `artifacts/history.csv`: `adjusted = total + gate_save + cost_overhead`. Unit test enforces the identity.
- **End-to-end smoke verified** with the real cost table: raw `278.6`, adjusted `340.5`, gate save `101.9`, cost overhead `-40.0`, gated `7`, status `ok`.
- **Repo merge settings fixed**: auto-merge and delete-branch-on-merge are enabled. PR #35 initially stayed blocked because two stale Gemini review threads were unresolved; resolving them triggered auto-merge immediately.
- **Merge guardrail improved**: `scripts/merge_pr.ps1` is now the preferred Windows-native closer; `scripts/merge_pr.sh` remains the Git Bash equivalent. They resolve review threads, wait for CI, fall back to auto-merge when direct merge is blocked, wait for the PR to actually merge, and delete the remote branch if GitHub leaves it behind.
- **Stop-hook alternative installed**: PR-body ritual text is advisory, but the PR checklist workflow now has a paperwork audit. Durable code/tooling changes must update `HANDOFF.md`; architecture-map-sensitive changes must update `docs/ARCHITECTURE_MAP.md`. This enforces paperwork at PR time, where the agent can fix it, instead of interrupting local work with Stop hooks.
- **Stop hooks remain retired**. `.claude/settings.json` is `{}`; no Stop-hook paperwork gate. Update paperwork directly before finishing.

## Not Yet Done
- **Issue #32** — `MatchedRow` propagation in `ff/live/reconcile.py:82`. This is the smallest cost-realism follow-up: matched live-vs-BT rows still need the new overlay/gate columns carried into the reconcile headline report.
- **Issue #33** — live guard reads stale closed-bar spread, not the submit-time tick. Needs fresh `mt5.symbol_info_tick` immediately before broker submit.
- **Issue #34** — post-fill slippage cap is documented but not enforced after order fill.
- **Three older scanner findings:** #12 path traversal in `app/routes.py`, #13 out-of-bounds `sig_bar_index`, #14 metric key mismatch (`win_rate` vs `win_rate_pct`).
- **Backport the live runner's forming-candle fix** into the BT engine.
- **Docs sweep** — markdownlint MD040 in `docs/superpowers/specs/*` and `docs/superpowers/plans/*`, stale `is_news_window` placeholder reference, session-name canonicalisation.

## Failed Approaches — Don't Repeat
- Don't replace `pnl_pips` with adjusted P&L as the headline metric. Keep raw and adjusted side-by-side.
- Don't store enriched best-trial trades as a numpy recarray in NPZ. JSON-encoded `uint8` bytes avoid pickle and round-trip cleanly.
- Don't insert the live execution guard at the top of `_poll_one_pair`; it belongs immediately before broker submit.
- Don't leave UI work on an unmerged branch after backend work lands. If a user says "I can't see it in the UI", check `git branch -vv`, `gh pr list --head <branch>`, and whether local `main` includes the UI commit.
- Don't assume green checks mean mergeable. Resolve outdated review threads too; branch protection can remain blocked until every thread is resolved.

## Exact Resume Steps
1. Make sure any docs/tooling refresh PR containing this handoff is merged.
2. Run `git checkout main && git pull --ff-only origin main` before new work. If local runtime artifacts are dirty, stash them first.
3. Pick up issue #32 (`MatchedRow` cost-realism column propagation).
4. Then issues #33 and #34.
5. Then the deferred docs sweep.

## Useful Commands
- Merge a green PR safely: `bash scripts/merge_pr.sh <PR#>`
- Check unresolved review threads: `gh pr view <PR#> --json mergeStateStatus,autoMergeRequest,url`
- Sync main safely: `bash scripts/sync_main.sh`

## In Flight
- Docs/tooling guardrail refresh only (`docs/pr35-handoff-automerge-guardrails`).
