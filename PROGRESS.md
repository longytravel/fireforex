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

- [x] **Architecture stocktake — Pillar 1** (shipped 2026-04-25, PRs #16, #18, #19, #20, #21, #22, #23) — `docs/ARCHITECTURE_MAP.md` + completeness checker (`scripts/check_map.py`) + stop-hook (`.claude/hooks/check-architecture-map.sh`) + Pillars 2–6 roadmap. 9 cleanup files removed; 231 tracked files audited.
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
