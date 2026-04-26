# Progress

Living milestone register. Tick boxes as things ship. Never rewritten — only appended.

## In flight

- [x] **Dev workbench port** (shipped 2026-04-25, PRs #11 + bootstrap commit)
  - [x] Deny list + hooks + rules scaffolded
  - [x] Root CLAUDE.md slimmed (264 → 118 lines)
  - [x] GitHub side: PR template, CI (ruff/maturin/pytest/clippy), branch protection, CodeRabbit, Gemini Code Assist installed
  - [x] Pre-PR ritual script (`scripts/pre-pr.ps1`, Codex gpt-5.4-mini reasoning=high)
  - [x] First end-to-end workflow demo: PR #11 reviewed by Codex + CodeRabbit + Gemini, 3 real findings tracked as issues #12/#13/#14, merged cleanly without admin override
- [ ] **Live↔BT parity** (plan: `docs/live-bt-parity-plan.md`)
  - [x] Forensic reconciliation report
  - [x] MT5 deal history timezone fix
  - [x] Forming-M1-candle fix
  - [x] Trade comparison report builder
  - [x] MT5 downloader offline-replay mode
  - [ ] Three-tier data architecture implemented
  - [ ] 100% trade match on next 10 live closes
  - [ ] Parity harness as CI gate

## Next

- [x] **Architecture stocktake — Pillar 1** (shipped 2026-04-25, PRs #16, #18, #19, #20, #21, #22, #23) — `docs/ARCHITECTURE_MAP.md` + completeness checker (`scripts/check_map.py`) + Pillars 2–6 roadmap. 9 cleanup files removed; 231 tracked files audited. (The map's Stop-hook was retired 2026-04-26 with the rest of the Stop hooks.)
- [x] **Cleanup pass 2** (shipped 2026-04-25, PR #24) — 10 more stale docs removed (`ROADMAP.md`, `rust-wishlist.md`, `CHANGES.md`, `REVIEW.md`, `exec-full-fix-plan.md`, `bug-hunting-research-brief.md`, 3× dated `docs/live/` files, `snapshot-home.md`).
- [x] **PR-system refinements** (shipped 2026-04-25, PR #25) — batching rule, docs-only checklist auto-skip, CodeRabbit-primary policy, force-push allowed on feature branches, gitignore patterns for transient PR artifacts.
- [x] **MT5 direct toolkit** (shipped 2026-04-25, PR #26) — `scripts/import_mt5_report.py` + `scripts/mt5_status.py` + 2 desktop shortcuts. Hits the running MT5 terminal directly (no manual HTML export). Broker→UTC offset, SL/TP enrichment, digit-aware spread.
- [x] **Dependabot triage (3 of 10)** (2026-04-25, PRs #3, #6, #7) — actions/checkout v6, fastapi, pytest merged. Remaining 7 need `@dependabot rebase`.
- [ ] **Address review-flagged bugs**:
  - [x] #12 Path-traversal validation in `app/routes.py` — `_resolve_run_npz` now resolves the joined path and asserts it lives under `artifacts/runs` (defends against symlink escape); `/runs/{run_id}/trades.csv` reuses the helper instead of its ad-hoc string check. Regression tests in `tests/test_path_traversal_routes.py`.
  - [ ] #13 Out-of-bounds bounds check on `sig_bar_index`
  - [ ] #14 Metric key mismatch (`win_rate` vs `win_rate_pct`)
- [ ] **Triage 10 dependabot PRs** auto-opened on 2026-04-25
- [ ] **Pin test fixtures** so CI can run `test_golden_baseline` and `test_trade_log_roundtrip` (currently skipped on Linux for missing data)
- [x] **Cost-realism overlay** (shipped 2026-04-26, PR #31) — post-pass spread/commission/slippage adjustment from MT5 medians + slippage telemetry feedback loop. Dukascopy stays the BT engine; overlay surfaces raw + `adjusted_pnl_total_pips` side-by-side with a `cost_realism_status` health field. Spec: `docs/superpowers/specs/2026-04-25-cost-realism-design.md`.
- [x] **Execution Guard module ("3-and-3")** (shipped 2026-04-26, PR #31) — single source of truth in `ff/cost_realism/gate_rules.py`, imported by BT post-pass (`bt_gate.py`) and live runner (`execution_guard.py`). Blocks when live spread > 3 pips, realised slippage > 3 pips, or UTC hour in 21:00–24:00 daily-rollover window. News-window block deferred to v2. Architectural follow-ups: #32 (`MatchedRow` columns), #33 (submit-time tick), #34 (post-fill slippage cap).
- [x] **Cost-realism History columns** (shipped 2026-04-26, PR #35) — UI now shows adjusted pips, gate-save pips, cost overhead, gated-trade count, and CR status. Harness persists `gate_save_pips` + `cost_overhead_pips`; test enforces `adjusted = total + gate_save + cost_overhead`.
- [x] **Agent-owned PR guardrail** (shipped 2026-04-26, docs/tooling refresh) — repo auto-merge + delete-branch-on-merge enabled; `scripts/merge_pr.ps1` / `.sh` now resolve threads, wait for CI, fall back to auto-merge when direct merge is blocked, wait for the merge to land, and clean up leftover remote branches. Stop hooks stay retired; PR-time paperwork audit enforces HANDOFF/map updates without user babysitting.
- [x] **Optimiser ranks by IC-adjusted P&L (Option C)** (shipped 2026-04-26 evening) — `ff.harness.pick_best` now accepts an `objective_array` override; the harness wires in `total_pnl + n_trades * per_trade_overlay_charge_pips(pair)` from `cost_table.json` so the picked trial reflects realistic-live P&L instead of Dukascopy quality. Falls back to legacy Quality when the cost table is missing or pair not covered. Golden fixture re-pinned (33-trade Quality outlier → 136-trade EMA-cross trend-follower). Plan: `C:\Users\ROG\.claude\plans\crystalline-enchanting-pebble.md`.
- [x] **PR-checklist enforces PROGRESS.md** (shipped 2026-04-26 PM) — `.github/workflows/pr-checklist.yml` paperwork audit previously required `HANDOFF.md` (always) and `docs/ARCHITECTURE_MAP.md` (on map-sensitive paths). Added a symmetric rule for `PROGRESS.md` so it is now CI-enforced alongside `HANDOFF.md` on durable PRs. `.claude/rules/workflow.md` Paperwork section updated. Closes the gap where forgetting a milestone tick wouldn't block merge.
- [x] **Cost-table validator + mean-not-median fix** (shipped 2026-04-26 PM) — `ff/cost_realism/cost_table.py` switched from `median()` to `mean()` per session (median was reporting the broker's 1-point quote-rounding floor on liquid pairs, silently understating real cost). Added per-pair lower-bound validator (USD-majors ≥ 0.05 pips, crosses ≥ 0.3 pips). `build_cost_table` now catches per-pair ValueError, logs, and continues so one bad pair can't block the rebuild. Three regression tests: floor-biased mean, cross-pair tight rejection, USD-major exemption.
- [x] **MT5 tick downloader + cost-table-from-ticks** (shipped 2026-04-26 PM, PR #42) — new `ff/data/mt5_tick_downloader.py` mirrors the M1 downloader but pulls real bid/ask via `mt5.copy_ticks_range()` into `{pair}_TICK.parquet`. `ff/cost_realism/cost_table.py` rewired to prefer tick parquets (computes `spread = ask − bid`; tags entries `spread_source: "tick" | "m1"`). Cross-pair lower-bound floor relaxed from 0.3 to 0.15 pips (calibrated against 90-day IC Markets tick history). `scripts/fetch_mt5_ticks.py` is the bulk downloader. Verified: 28/28 pairs in cost_table with realistic per-session spreads (AUD_NZD: London 0.58 / NY 3.37 / Rollover 0.60 — NY shows the wide spread as it should). Last-run summary panel (`app/static/app.js`) now shows Adj. pips / Gate save / Cost / Gated cards alongside the raw KPIs.
- [x] **MT5 M1 spread is structurally floor-biased** (resolved-by-bypass via PR #42) — sidestepped by switching cost-table source to MT5 tick data. M1 path remains as documented legacy fallback for pairs without tick coverage; entries from that path are tagged `spread_source: "m1"` so downstream readers can distinguish them.
- [ ] **Optuna optimiser** (replaces random sweep for serious runs)
- [ ] **Walk-forward validation** (in-sample / out-of-sample rolling windows)
- [ ] **Monte Carlo robustness** (stressed seeds, confidence bands on metrics)
- [ ] **Experiment tracker** (history of sweeps, not just latest)

## Shipped

- [x] Rust engine (`ff_core`) — pyo3 + maturin
- [x] Random sweep harness + heartbeat thread
- [x] Web UI (FastAPI + vanilla JS, local-only)
- [x] VPS live runner (scheduled task, autonomous trade close reconciliation loop)
- [x] ATR-driven per-pair, per-TF default ranges + volatility cache
- [x] Dukascopy M1 via raw `.bi5` downloader
- [x] MT5 replay mode for parity validation
