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
  - [ ] #12 Path-traversal validation in `app/routes.py`
  - [ ] #13 Out-of-bounds bounds check on `sig_bar_index`
  - [ ] #14 Metric key mismatch (`win_rate` vs `win_rate_pct`)
- [ ] **Triage 10 dependabot PRs** auto-opened on 2026-04-25
- [ ] **Pin test fixtures** so CI can run `test_golden_baseline` and `test_trade_log_roundtrip` (currently skipped on Linux for missing data)
- [x] **Cost-realism overlay** (shipped 2026-04-26, PR #31) — post-pass spread/commission/slippage adjustment from MT5 medians + slippage telemetry feedback loop. Dukascopy stays the BT engine; overlay surfaces raw + `adjusted_pnl_total_pips` side-by-side with a `cost_realism_status` health field. Spec: `docs/superpowers/specs/2026-04-25-cost-realism-design.md`.
- [x] **Execution Guard module ("3-and-3")** (shipped 2026-04-26, PR #31) — single source of truth in `ff/cost_realism/gate_rules.py`, imported by BT post-pass (`bt_gate.py`) and live runner (`execution_guard.py`). Blocks when live spread > 3 pips, realised slippage > 3 pips, or UTC hour in 21:00–24:00 daily-rollover window. News-window block deferred to v2. Architectural follow-ups: #32 (`MatchedRow` columns), #33 (submit-time tick), #34 (post-fill slippage cap).
- [x] **Cost-realism History columns** (shipped 2026-04-26, PR #35) — UI now shows adjusted pips, gate-save pips, cost overhead, gated-trade count, and CR status. Harness persists `gate_save_pips` + `cost_overhead_pips`; test enforces `adjusted = total + gate_save + cost_overhead`.
- [x] **Agent-owned PR guardrail** (shipped 2026-04-26, docs/tooling refresh) — repo auto-merge + delete-branch-on-merge enabled; `scripts/merge_pr.ps1` / `.sh` now resolve threads, wait for CI, fall back to auto-merge when direct merge is blocked, wait for the merge to land, and clean up leftover remote branches. Stop hooks stay retired; PR-time paperwork audit enforces HANDOFF/map updates without user babysitting.
- [x] **Cost-table validator + mean-not-median fix** (shipped 2026-04-26 PM) — `ff/cost_realism/cost_table.py` switched from `median()` to `mean()` per session (median was reporting the broker's 1-point quote-rounding floor on liquid pairs, silently understating real cost). Added per-pair lower-bound validator (USD-majors ≥ 0.05 pips, crosses ≥ 0.3 pips). `build_cost_table` now catches per-pair ValueError, logs, and continues so one bad pair can't block the rebuild. Three regression tests: floor-biased mean, cross-pair tight rejection, USD-major exemption.
- [ ] **MT5 M1 spread is structurally floor-biased** (open follow-up — discovered 2026-04-26 PM during cost-table validator rollout). Even with mean-per-session, 25 of 28 default pairs fail the lower-bound floor — the MT5 OHLC `spread` field captures only the bar's *closing tick* spread, not a time-weighted average. During quiet bars the closing tick lands at the broker's 1-point quote-rounding floor, so non-NY sessions read as ~0.1 pips for almost every pair. Only NY-session bars show genuine quote variation (3–6 pips on JPY pairs, 1.5–2 pips on majors). Cost-realism overlay's per-session spread granularity is therefore not currently trustworthy. Follow-up paths: tick-level bid/ask sampling, broker-published spread averages, or per-pair calibration from account statements. Until resolved: the `artifacts/cost_table.json` will only have ~3 pairs that just barely clear the floor; pairs without entries fall through the overlay (raw P&L + gate-effect only, no cost adjustment).
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
