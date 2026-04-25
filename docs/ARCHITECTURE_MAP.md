# Fire Forex — Architecture Map

> Living map of every tracked file in the repo, audited against what each piece is supposed to do. Top of file = first thing the system does. Bottom = unbuilt pillars 2–6.

## Verdict legend

- ✅ working as intended
- ⚠️ partial — known gap or needs hardening
- ❌ broken — does not deliver what its name implies
- 🔘 not started — placeholder for unbuilt component

## End-to-end flow

_Mermaid diagram lands in Phase F. Stages 1–6 below._

## 1 · DATA INGEST
_Audit table lands in Phase B. Files in this stage:_

- `ff/data/__init__.py`
- `ff/data/date_slice.py`
- `ff/data/downloader.py`
- `ff/data/groups.py`
- `ff/data/health.py`
- `ff/data/inventory.py`
- `ff/data/m1_bi5_downloader.py`
- `ff/data/mt5_m1_downloader.py`
- `ff/data/resample.py`
- `ff/data/tick_downloader.py`
- `ff/defaults/volatility.py`
- `scripts/fetch_mt5_history.py`

## 2 · EA DEFINITION
_Audit table lands in Phase B. Files in this stage:_

- `eas/__init__.py`
- `eas/baseline.json`
- `eas/baseline.py`
- `eas/complex01.json`
- `eas/complex01.py`
- `ff/defaults/__init__.py`
- `ff/defaults/complexity.py`
- `ff/defaults/overrides.py`
- `ff/defaults/pair_tf.yaml`
- `ff/encoding.py`
- `ff/sampler.py`
- `ff/schema.py`
- `ff/schema_json.py`

## 3 · BACKTEST SWEEP
_Audit table lands in Phase B. Files in this stage:_

- `core/Cargo.lock`
- `core/Cargo.toml`
- `core/src/constants.rs`
- `core/src/filter.rs`
- `core/src/lib.rs`
- `core/src/metrics.rs`
- `core/src/sl_tp.rs`
- `core/src/trade_full.rs`
- `ff/exit_codes.py`
- `ff/harness.py`
- `ff/preflight.py`
- `ff/signal_lib.py`

## 4 · INSPECT & PICK (Web UI)
_Audit table lands in Phase B. Files in this stage:_

- `app/__init__.py`
- `app/api.py`
- `app/baselines.py`
- `app/jobs.py`
- `app/models.py`
- `app/pairs_scan.py`
- `app/routes.py`
- `app/static/app.js`
- `app/static/index.html`
- `app/static/styles.css`
- `ff/inspect.py`

## 5 · DEPLOY TO VPS
_Audit table lands in Phase B. Files in this stage:_

- `app/live_jobs.py`
- `app/live_state_puller.py`
- `deploy/instances/active.json`
- `deploy/instances/complexity_L10_EUR_USD_M15_20260422_111232__20260422_111326.json`
- `deploy/instances/complexity_L10_EUR_USD_M15_20260422_111400__20260422_111414.json`
- `deploy/instances/complexity_L10_EUR_USD_M15_20260422_111436__20260422_111458.json`
- `deploy/instances/complexity_L10_EUR_USD_M15_20260424_100942__20260424_101044.json`
- `deploy/instances/complexity_L10_EUR_USD_M15_20260424_101119__20260424_101142.json`
- `deploy/instances/complexity_L10_EUR_USD_M15_20260424_101204__20260424_101238.json`
- `deploy/live_config.json`
- `deploy/live_config.json.example`
- `ff/live/__init__.py`
- `ff/live/broker_mt5.py`
- `ff/live/exit_manager.py`
- `ff/live/frozen_signal.py`
- `ff/live/parity_guard.py`
- `ff/live/reconcile.py`
- `ff/live/runner.py`
- `ff/live/runner_service.py`
- `ff/live/state_sync.py`
- `scripts/diagnose_vps.py`
- `scripts/runner_launcher.bat`
- `scripts/vps_bootstrap.ps1`

## 6 · RECONCILE LIVE ⇄ BACKTEST
_Audit table lands in Phase B. Files in this stage:_

- `ff/replay.py`
- `scripts/build_forensic_report.py`
- `scripts/build_trade_comparison.py`
- `scripts/calibrate_for_parity.py`
- `scripts/reconcile_live.py`
- `scripts/reset_live_day.py`

## Appendix A — Documentation (`docs/`)
_Lands in Phase C._

- `docs/2026-04-19-adding-the-chandelier-knob.md`
- `docs/2026-04-19-the-breakeven-offset-bug.md`
- `docs/2026-04-19-the-exec-basic-bug.md`
- `docs/2026-04-19-the-partial-close-bug.md`
- `docs/2026-04-19-the-signal-filter-bugs.md`
- `docs/2026-04-19-the-trailing-bug.md`
- `docs/ARCHITECTURE.md`
- `docs/CHANGES.md`
- `docs/REVIEW.md`
- `docs/ROADMAP.md`
- `docs/bug-hunting-research-brief.md`
- `docs/builds/2026-04-19-chandelier-stop/01-mechanics-brief.md`
- `docs/builds/2026-04-19-chandelier-stop/02-slot-map.md`
- `docs/builds/2026-04-19-chandelier-stop/03-reference-scenarios.md`
- `docs/builds/2026-04-19-chandelier-stop/04-build-log.md`
- `docs/builds/2026-04-19-chandelier-stop/05-smoke-results.md`
- `docs/builds/2026-04-19-chandelier-stop/06-audit-link.md`
- `docs/exec-full-fix-plan.md`
- `docs/knob-explanations.md`
- `docs/live/ARCHITECTURE-multi-instance.md`
- `docs/live/BUG-variant-id-not-stable-2026-04-22.md`
- `docs/live/HANDOVER-2026-04-22-day.md`
- `docs/live/HANDOVER-parity-status.md`
- `docs/live/HOW-TO-DEPLOY.md`
- `docs/live/LIVE-TRADE-ELEMENT.md`
- `docs/live/README.md`
- `docs/live/RECONCILE.md`
- `docs/live/SESSION-2026-04-21-end.md`
- `docs/live/SESSION-2026-04-21-evening.md`
- `docs/live/SESSION-2026-04-21-night-handover.md`
- `docs/live/SESSION-2026-04-21.md`
- `docs/live/SESSION-RESUME.md`
- `docs/live/VPS-HANDOVER.md`
- `docs/live/WAKE-UP-2026-04-22.md`
- `docs/live/parity-plan-2026-04-24.md`
- `docs/metrics.md`
- `docs/next-session-handover.md`
- `docs/rust-wishlist.md`
- `docs/superpowers/plans/2026-04-25-architecture-stocktake.md`
- `docs/superpowers/specs/2026-04-25-architecture-stocktake-design.md`
- `docs/validation/2026-04-19-breakeven-offset/01-mechanics-brief.md`
- `docs/validation/2026-04-19-breakeven-offset/02-code-trace.md`
- `docs/validation/2026-04-19-breakeven-offset/03-behaviour-table.md`
- `docs/validation/2026-04-19-breakeven-offset/04-micro-test.py`
- `docs/validation/2026-04-19-breakeven-offset/05-sensitivity-results.md`
- `docs/validation/2026-04-19-breakeven-offset/06-verdict.md`
- `docs/validation/2026-04-19-breakeven-offset/_sensitivity_runner.py`
- `docs/validation/2026-04-19-chandelier-stop/01-mechanics-brief.md`
- `docs/validation/2026-04-19-chandelier-stop/02-code-trace.md`
- `docs/validation/2026-04-19-chandelier-stop/03-behaviour-table.md`
- `docs/validation/2026-04-19-chandelier-stop/04-micro-test.py`
- `docs/validation/2026-04-19-chandelier-stop/05-sensitivity-results.md`
- `docs/validation/2026-04-19-chandelier-stop/06-verdict.md`
- `docs/validation/2026-04-19-partial-close/01-mechanics-brief.md`
- `docs/validation/2026-04-19-partial-close/02-code-trace.md`
- `docs/validation/2026-04-19-partial-close/03-behaviour-table.md`
- `docs/validation/2026-04-19-partial-close/04-micro-test.py`
- `docs/validation/2026-04-19-partial-close/05-sensitivity-results.md`
- `docs/validation/2026-04-19-partial-close/06-verdict.md`
- `docs/validation/2026-04-19-signal-filters/01-mechanics-brief.md`
- `docs/validation/2026-04-19-signal-filters/02-code-trace.md`
- `docs/validation/2026-04-19-signal-filters/03-behaviour-table.md`
- `docs/validation/2026-04-19-signal-filters/04-micro-test.py`
- `docs/validation/2026-04-19-signal-filters/05-sensitivity-results.md`
- `docs/validation/2026-04-19-signal-filters/06-verdict.md`
- `docs/validation/2026-04-19-stale-exit/01-mechanics-brief.md`
- `docs/validation/2026-04-19-stale-exit/02-code-trace.md`
- `docs/validation/2026-04-19-stale-exit/03-behaviour-table.md`
- `docs/validation/2026-04-19-stale-exit/04-micro-test.py`
- `docs/validation/2026-04-19-stale-exit/05-sensitivity-results.md`
- `docs/validation/2026-04-19-stale-exit/06-verdict.md`
- `docs/validation/2026-04-19-trailing/01-mechanics-brief.md`
- `docs/validation/2026-04-19-trailing/02-code-trace.md`
- `docs/validation/2026-04-19-trailing/03-behaviour-table.md`
- `docs/validation/2026-04-19-trailing/04-micro-test.py`
- `docs/validation/2026-04-19-trailing/05-sensitivity-results.md`
- `docs/validation/2026-04-19-trailing/06-verdict.md`
- `docs/validation/2026-04-19-trailing/_sensitivity_runner.py`

## Appendix B — PRs & GitHub issues
_Lands in Phase C._

## Appendix C — Tests (`tests/`)
_Lands in Phase C._

- `tests/golden/complex01_seed42_500trials.json`
- `tests/test_broker_mt5_submit.py`
- `tests/test_complexity.py`
- `tests/test_data_health.py`
- `tests/test_data_inventory.py`
- `tests/test_date_slice.py`
- `tests/test_exit_manager.py`
- `tests/test_frozen_signal.py`
- `tests/test_golden_baseline.py`
- `tests/test_groups.py`
- `tests/test_knob_sensitivity.py`
- `tests/test_live_runner_synthetic.py`
- `tests/test_math_correctness.py`
- `tests/test_new_metrics.py`
- `tests/test_parity_guard.py`
- `tests/test_reconcile.py`
- `tests/test_replay.py`
- `tests/test_resample.py`
- `tests/test_routes_data.py`
- `tests/test_runner_service_multi_instance.py`
- `tests/test_signal_cache.py`
- `tests/test_state_sync.py`
- `tests/test_tick_to_m1.py`
- `tests/test_trade_log_roundtrip.py`
- `tests/test_variant_fingerprint.py`
- `tests/validation/test_breakeven_offset_mechanics.py`
- `tests/validation/test_chandelier_mechanics.py`
- `tests/validation/test_partial_close_mechanics.py`
- `tests/validation/test_signal_filters_mechanics.py`
- `tests/validation/test_stale_exit_mechanics.py`
- `tests/validation/test_trailing_mechanics.py`

## Appendix D — CI / GitHub workflows (`.github/`)
_Lands in Phase C._

- `.github/CODEOWNERS`
- `.github/ISSUE_TEMPLATE/bug.md`
- `.github/ISSUE_TEMPLATE/feature.md`
- `.github/dependabot.yml`
- `.github/pull_request_template.md`
- `.github/workflows/ci.yml`
- `.github/workflows/codeql.yml`
- `.github/workflows/gitleaks.yml`
- `.github/workflows/pr-checklist.yml`

## Appendix E — `.claude/` rules and hooks
_Lands in Phase C._

- `.claude/commands/handoff.md`
- `.claude/hooks/session-start.sh`
- `.claude/hooks/update-paperwork.sh`
- `.claude/rules/python-style.md`
- `.claude/rules/rust-style.md`
- `.claude/rules/testing.md`
- `.claude/rules/trading.md`
- `.claude/rules/workflow.md`
- `.claude/settings.json`

## Appendix F — Scripts (`scripts/`)
_Lands in Phase C._

- `scripts/desktop/Check Fire Forex.bat`
- `scripts/desktop/Deploy Fire Forex.bat`
- `scripts/desktop/Diagnose Fire Forex.bat`
- `scripts/desktop/Reset Live Day (VPS).bat`
- `scripts/desktop/Restart Fire Forex (laptop).bat`
- `scripts/ff_kill_server.bat`
- `scripts/ff_kill_server.ps1`
- `scripts/ff_restart_server.bat`
- `scripts/ff_restart_server.ps1`
- `scripts/ff_start_server.ps1`
- `scripts/migrate_best_trial_fingerprint.py`
- `scripts/pre-pr.ps1`

## Appendix G — Root files
_Lands in Phase C._

- `CLAUDE.md`
- `HANDOFF.md`
- `PROGRESS.md`
- `README.md`
- `demo_speed.py`
- `ff/VERSION.py`
- `ff/__init__.py`
- `launch_fire_forex.bat`
- `run.py`
- `snapshot-home.md`

## Appendix H — Configs
_Lands in Phase C._

- `.coderabbit.yaml`
- `.gitignore`
- `.pre-commit-config.yaml`
- `core/Cargo.lock`
- `core/Cargo.toml`
- `pyproject.toml`
- `requirements-web.txt`

## Appendix I — Artifacts (`artifacts/`)
_Lands in Phase C._

- `artifacts/baseline.json`
- `artifacts/demo_speed.html`
- `artifacts/history.csv`
- `artifacts/system_audit_report_2026-04-19.md`

## Section 7 — Cleanup punch list
_Lands in Phase D._

## Section 8 — Roadmap (Pillars 2–6)
_Lands in Phase E._
