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

**Supposed to:** Pull historical price data from two sources (Dukascopy public archive + MT5 broker terminal), keep parquet stores in sync, run health checks, and derive per-(pair, TF) volatility defaults that drive the rest of the system.

| Component | Path | Supposed to | Verdict | Notes |
|---|---|---|---|---|
| Data package init | `ff/data/__init__.py` | Re-export `date_slice`, `health`, `inventory` | ✅ | One-liner; nothing to break. |
| UTC date clipping | `ff/data/date_slice.py` | Clip a DataFrame to a `[start, end]` UTC window, inclusive, with end-of-day expansion when only a date is given | ✅ | Pure utility; pandas-version aware. |
| Retired bar API | `ff/data/downloader.py` | Tombstone — raises `ImportError` on import | ✅ | Intentional: upstream Dukascopy bar API returns null rows in `dukascopy_python` 3.x/4.x. Kept as an explicit barrier so stale imports fail loud. Replacements: `m1_bi5_downloader` + `resample.derive_higher_tfs`. |
| Pair groups (UI) | `ff/data/groups.py` | Single source of truth for Majors / Crosses / Metals / Indices / Crypto headings in the Data tab | ✅ | Static data; consumed by `app/`. |
| Parquet health check | `ff/data/health.py` | NaN / OHLC sanity / timestamp ordering / gap detection (with FX weekend mask) | ✅ | Returns roll-up `ok/warn/fail` per file. Pandas 3.0 `asi8` change handled via `to_numpy.view('i8')`. |
| Parquet inventory | `ff/data/inventory.py` | Scan known data roots, headers-only, with 1-hour TTL cache to `artifacts/data_inventory.json` | ✅ | Drives the Data tab list. Cache TTL hard-coded — fine. |
| Dukascopy M1 downloader | `ff/data/m1_bi5_downloader.py` | Per-day `.bi5` → LZMA → struct unpack → `{pair}_M1.parquet` (BID/ASK + computed spread) | ✅ | Replaces broken `dukascopy_python`. Empirically verified against GBP_USD 2024-06-03 sample. |
| MT5 M1 downloader | `ff/data/mt5_m1_downloader.py` | Mirror of bi5 downloader for MT5 broker terminal — same parquet shape into `BackTestData_MT5/` | ⚠️ | Pair-coverage gap: only pairs the laptop's MT5 market-watch includes get pulled (memory `project_mt5_replay_pair_coverage.md`). Windows-only (MetaQuotes binary). |
| Resampler | `ff/data/resample.py` | `tick_to_m1` + `derive_higher_tfs` — TICK → M1 → M5/M15/M30/H1/H4/D/W with forex-correct OHLCV aggregation, atomic `.partial` writes | ✅ | Preserves DatetimeIndex on merge (memory `feedback_resample_merge_preserves_history.md` — earlier wipe-history bug fixed). |
| Dukascopy tick downloader | `ff/data/tick_downloader.py` | Hourly `.bi5` tick files → unpack → `{pair}_TICK.parquet`, append-only beyond existing max ts | ✅ | Stdlib + pandas only. JPY/non-JPY scale handled. |
| ATR-driven defaults | `ff/defaults/volatility.py` | Median 14-bar ATR per (pair, TF) → derive stop / target / trailing ranges as ATR multiples; cached to `artifacts/volatility_cache.json` | ✅ | `ATR_RULES` is the single point of extension for new pair-aware knobs. |
| MT5 fetch CLI | `scripts/fetch_mt5_history.py` | One-pair MT5 fetch + fan-out to higher TFs via `resample.derive_higher_tfs` | ✅ | Argparse CLI; UTF-8 stdout reconfigured for Windows cp1252. |
| Volatility cache | `artifacts/volatility_cache.json` *(gitignored runtime)* | Cache the truth of computed ATR ranges (CLAUDE.md: cache is truth, `pair_tf.yaml` is fallback) | ✅ | Not tracked — runtime artifact rebuilt from parquet. |
| Three-tier data architecture | _(unbuilt)_ | Dukascopy / MT5 / merged tiers with explicit provenance per row, so Stage 3 sweeps can run on any tier | 🔘 | Designed (memory `Three-Tier Data Architecture for Live-Backtest Parity`); blocks 100% reconcile parity. Lands as part of Pillar 5 dependencies. |
| Automated data integrity check | _(unbuilt)_ | Detect gaps / corrupt bars / future-dated bars across all roots and alert | 🔘 | `health.py` checks per-file on demand; no scheduled sweep. Pillar 3 (safety). |

**Flows down to Stage 2** via `ff.harness.load_parquet` (called from the EA-build path) and `ff.defaults.volatility.derive_ranges` (supplies default ranges to the schema layer).
**Flows up from Stage 5** via the MT5 broker round-trip — `mt5_m1_downloader` + `fetch_mt5_history.py` pull live broker data back to the laptop, enabling the dual backtest the future-architecture sketch describes.

## 2 · EA DEFINITION

**Supposed to:** Declare an EA as a schema of knobs (signals + engine + execution + data), let the user pick a complexity level or load an existing config, apply per-knob overrides from the UI, and flatten the result into the `(NUM_PL,)` float64 row the Rust engine consumes.

| Component | Path | Supposed to | Verdict | Notes |
|---|---|---|---|---|
| EAs package init | `eas/__init__.py` | Package marker for example EA fixtures | ✅ | |
| Baseline schema (JSON) | `eas/baseline.json` | Pinned-baseline EA schema as plain data so the UI can load it | ✅ | Data file. |
| Baseline EA | `eas/baseline.py` | Minimal EA: ATR SL + RR TP, all hours / Mon-Fri. Provides `ENGINE_MAPPING` for the Rust slot layout | ✅ | Reference implementation; keeps the surface tiny so regressions show up clearly. |
| complex01 schema (JSON) | `eas/complex01.json` | Complex EA schema (~22 tunable dims when every group is ON) | ✅ | Data file consumed by the UI. |
| complex01 EA | `eas/complex01.py` | The fixture EA: full ENGINE_MAPPING with categorical SL selector, multi-arm branches, breakeven / trailing groups | ✅ | The fixture used in goldens, demos, and `add-forex-knob` flows. |
| Defaults package init | `ff/defaults/__init__.py` | Package marker | ✅ | |
| `complexity_to_ea` | `ff/defaults/complexity.py` | Build a Fire Forex EA from a 1..10 complexity level using `pair_tf.yaml` as the per-(pair, main_tf) range table | ✅ | Result is a strict subset of `complex01`'s shape — the structural-validity guarantee. |
| `apply_overrides` | `ff/defaults/overrides.py` | Apply UI overrides (`groups` / `knobs` / `global` / `signal_families`) to an already-built EA, returning a new EA with originals untouched | ✅ | All keys optional; unknown paths ignored (see CLAUDE.md "Overrides shape"). |
| Pair / TF fallback table | `ff/defaults/pair_tf.yaml` | Hand-written per-(pair, main_tf) knob ranges used when the volatility cache is unavailable | ✅ | Don't edit without reason — volatility cache is the truth. |
| Rust slot encoder | `ff/encoding.py` | Map a sampled trial dict → `(NUM_PL,)` float64 row using `(slot_index, encoder_fn)` pairs declared per-EA. NUM_PL = 27. | ✅ | Encodes the foot-guns from `ff_core` (signal-variant disable = -1, days bitmask, SL/TP/Trailing modes) as defaults. |
| Random sampler | `ff/sampler.py` | Random uniform / log-uniform / discrete sampling over the EA schema, deterministic per seed | ⚠️ | Random only. No Bayesian / CMA-ES / walk-forward yet — Pillar 2. |
| Schema primitives | `ff/schema.py` | `FloatRange` / `IntRange` / `Choice` / `Group` / `Branch` dataclasses; defines knob composition semantics | ✅ | Frozen dataclasses; group-off semantics correctly suppress sub-knobs. |
| Schema JSON ser/de | `ff/schema_json.py` | `node_to_dict` / `dict_to_ea` — bridge dataclasses to plain JSON so the UI can save / load EAs | ✅ | `engine_mapping` is intentionally NOT serialised (callable references); supplied separately. |
| Bayesian sampler (Optuna) | _(unbuilt)_ | Smart sampler — same trial budget, far better best-trial than random | 🔘 | Pillar 2. |
| CMA-ES sampler | _(unbuilt)_ | Evolutionary sampler for continuous knobs | 🔘 | Pillar 2. |
| Walk-forward sampler | _(unbuilt)_ | Rolling in-sample / out-of-sample wrapper around any of the above | 🔘 | Pillar 2 + Pillar 3 (kills overfitting flatter). |

**Flows up from Stage 1** via `ff.defaults.volatility.derive_ranges` which supplies pair / TF-aware default ranges into `complexity_to_ea`.
**Flows down to Stage 3** via `ff.encoding.encode_trial` — each sampled trial is flattened to the `(NUM_PL,)` row consumed by `ff_core` (Rust engine).

## 3 · BACKTEST SWEEP

**Supposed to:** Build the signal library, sample N trial parameter sets, run them through the Rust engine in parallel, compute per-trial metrics, pick the best, and persist run artifacts (`artifacts/runs/*.npz` + `artifacts/history.csv`) for the UI.

| Component | Path | Supposed to | Verdict | Notes |
|---|---|---|---|---|
| Rust crate manifest | `core/Cargo.toml` | Declare `ff_core` crate, deps (pyo3, numpy, rayon), build profile | ✅ | Also referenced from Appendix H. |
| Rust lockfile | `core/Cargo.lock` | Pin exact dep versions | ✅ | Tracked so reproducible builds. |
| Engine constants | `core/src/constants.rs` | DIR / SL / TP / TRAIL / EXIT mode codes; **must stay in sync with `ff_core` slot constants** | ✅ | Single source of truth for the Rust↔Python contract on mode codes. |
| Time filter | `core/src/filter.rs` | `signal_passes_time_filter` — hours start/end (with wrap-around) + days bitmask (Mon=bit 0) | ✅ | Pure function; trivially correct. |
| Engine entrypoint | `core/src/lib.rs` | pyo3 module, `batch_evaluate` parallelised over rayon, panic-safe boundary | ⚠️ | Crate-wide `allow(dead_code)` lists `SL_FIXED_PIPS / TP_RR_RATIO / TRAIL_ATR_CHANDELIER / M_DSR / tp_pips` as reserved for upcoming variants. Comment explicitly says "to be reviewed in the architecture stocktake". This is now that review — decide per-name in a follow-on PR. |
| Metric computation | `core/src/metrics.rs` | `compute_metrics_inline` — all metrics for one trial, including PSR via `norm_cdf` | ✅ | Norm CDF via Abramowitz & Stegun 7.1.26; max abs err ~1.5e-7 — sufficient for ranking. Issue #14 (`win_rate` vs `win_rate_pct` mismatch) cuts across this + harness + UI. |
| SL/TP computation | `core/src/sl_tp.rs` | `compute_sl_tp` — derive SL/TP prices from mode codes + ATR pips + entry price | ✅ | NaN sentinel for missing swing_sl handled correctly. |
| Trade simulator | `core/src/trade_full.rs` | `simulate_trade_full` — one trade end-to-end with trailing, breakeven, partial close, stale exit, max bars | ⚠️ | Issue #13 — out-of-bounds risk on `sig_bar_index` (CodeRabbit critical + minor). Tracked. |
| Exit-code translation | `ff/exit_codes.py` | Numeric exit-reason codes → human names (`SL`, `TP`, `TRAILING`, ...) | ✅ | Mirrors `core/src/constants.rs` EXIT_* — must stay in sync; defensive `UNKNOWN` fallback. |
| Run harness | `ff/harness.py` | End-to-end orchestrator: load data → build signal library → sample → encode → call `ff_core.batch_evaluate` → save NPZ + history.csv → regenerate `comparison.html` | ✅ | The 11-step flow defined in module docstring. Heartbeat thread + parallel build (≥500 trials) per memory `project_speed_phases_1_2_3.md`. |
| Pre-flight estimator | `ff/preflight.py` | Estimate library combo count + sweep time + effective dimensionality before paying for a long run | ✅ | Heuristic; `SIGNAL_BUILD_SEC_PER_COMBO = 0.25` may drift over time. |
| Signal library | `ff/signal_lib.py` | Family registry + Cartesian-product expansion of per-family parameter grids → pooled `SignalSet` with stable variant IDs sorted by bar index | ✅ | Now keeps zero-signal variants so variant IDs stay stable across builds (memory `Signal Library Now Keeps Zero-Signal Variants for Stable Variant IDs`). |
| Bayesian sweep (Optuna) | _(unbuilt)_ | Plug-in optimiser feeding the harness — same trial budget, smarter sampling | 🔘 | Pillar 2. |
| CMA-ES sweep | _(unbuilt)_ | Evolutionary optimiser for continuous knobs | 🔘 | Pillar 2. |
| Walk-forward orchestration | _(unbuilt)_ | Roll the sweep over expanding/sliding train+test windows | 🔘 | Pillar 2 + 3. |
| Monte Carlo robustness | _(unbuilt)_ | Re-run a winning trial with seed/spread/order perturbations to get confidence bands | 🔘 | Pillar 3. |

**Flows up from Stage 2** via the encoded `(N, NUM_PL)` float64 matrix that `ff.encoding.encode_trial` produces; the harness picks one row per trial and ships it through `ff_core.batch_evaluate`.
**Flows down to Stage 4** via `artifacts/runs/{layer}_{stamp}.npz` (per-run trial outputs) and `artifacts/history.csv` (one row per run); the Web UI reads both for baseline comparison.

## 4 · INSPECT & PICK (Web UI)

**Supposed to:** Show every knob of every EA in plain English, run sweeps as one-at-a-time background jobs, persist a pinned baseline run, and let the user compare each new run against that baseline. Local-only on `127.0.0.1` — never hosted.

| Component | Path | Supposed to | Verdict | Notes |
|---|---|---|---|---|
| Web app package init | `app/__init__.py` | Package marker | ✅ | |
| FastAPI app entry | `app/api.py` | Mount router + static files; bind 127.0.0.1 only; kick the live-state daemon | ✅ | Docstring's `uvicorn app.api:api` is the user-run command (`scripts/ff_restart_server.ps1`) — Claude must never spawn uvicorn (`.claude/rules/trading.md`). |
| Pinned baseline storage | `app/baselines.py` | Persist a baseline snapshot to `artifacts/baseline.json` with `_KPI_KEYS` (trades, win_rate_pct, total_pips, expectancy_pips, max_dd_pct, profit_factor, sharpe, return_pct) | ⚠️ | Issue #14 — `win_rate_pct` here vs `win_rate` elsewhere in the engine / harness. Real bug, tracked. |
| One-at-a-time job runner | `app/jobs.py` | Single-slot background runner with `threading.Lock`; rebuilds EA from recipe server-side so engine-mapping callables never round-trip JSON | ✅ | 409 on concurrent POST. Heartbeat callback updates progress. |
| API request/response shapes | `app/models.py` | Pydantic models for `RunRequest`, `JobProgress`, `DefaultsRequest`, etc. | ✅ | Type-safe API surface. |
| Pair / TF scanner | `app/pairs_scan.py` | Thin adapter around `ff.data.inventory` so legacy callers don't break | ✅ | Cached scan; lives outside `ff/` because it's HTTP-adjacent. |
| HTTP endpoints | `app/routes.py` | All `/api/*` endpoints — defaults, run, jobs, baseline, instances, EA catalog, docs proxy | ⚠️ | Issue #12 — path traversal vulnerability (CodeQL × 3 + CodeRabbit major on `instance_id`). Real bug, tracked. |
| Frontend JS | `app/static/app.js` | Vanilla JS — recipe + override builder, run launch, job progress polling, baseline compare | ✅ | No framework, no build step. |
| Frontend HTML | `app/static/index.html` | Single-page UI scaffold | ✅ | |
| Frontend CSS | `app/static/styles.css` | Tailwind / vanilla CSS for the local UI | ✅ | |
| EA inspect report | `ff/inspect.py` | `inspect_dict` (structured) + `inspect_report` (human-readable) — every knob, TF choice, step size visible | ✅ | The "non-coder can read every parameter" guarantee. Powers `--inspect` CLI and the UI's EA preview. |
| Experiment tracker | _(unbuilt)_ | History of every sweep, not just the latest one — tag, compare, archive | 🔘 | Pillar 2 — currently `history.csv` is one row per run, no rich provenance. |
| Equity / drawdown / Sharpe charts | _(unbuilt)_ | Rolling charts on the run page | 🔘 | Pillar 4. |
| Per-pair / per-session breakdowns | _(unbuilt)_ | Slice metrics by pair, by hour-of-day, by regime | 🔘 | Pillar 4. |
| Knob-sensitivity heatmaps | _(unbuilt)_ | "What happens if I move stop_loss.atr.mult by ±10%?" — visualised | 🔘 | Pillar 4. |

**Flows up from Stage 3** by reading `artifacts/runs/*.npz` (per-trial outputs) and `artifacts/history.csv` (one row per run); both produced by the harness.
**Flows down to Stage 5** when the user picks a winner — config gets exported into `deploy/instances/<name>.json`.

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

**Supposed to:** Re-run a deployed live config as a backtest over the same window, join live artifacts (plans / tickets / deals) against the replay trade log, and report match / better / worse / missing / extra per trade. Goal: 100% match → parity harness becomes a CI gate.

**Stage-level verdict: ⚠️.** Individual scripts work as designed. The 100% match goal is unmet (1 of 8 trades matched in last forensic, memory `Reconciliation Accuracy Gap`). Root cause is upstream — Stage 1's three-tier data architecture is not built, so live MT5 fills can't be reconciled against the right BT data source.

| Component | Path | Supposed to | Verdict | Notes |
|---|---|---|---|---|
| Replay engine | `ff/replay.py` | Replay a deployed live config as a single-trial backtest, per pair, over the live window derived from `plans/*.jsonl` (±1 day pad) | ✅ | Frozen-trial path through `harness.run`. Output: `artifacts/replay/<source_run_id>/<stamp>/trades.npz`. |
| Forensic reconcile report | `scripts/build_forensic_report.py` | For each closed live trade, walk fire timing → entry → exit → narrative; explain every ms / pip of drift | ✅ | HTML report at `artifacts/live/reconcile/<stamp>_forensic.html`. Memory `forensic reconciliation report`. |
| Trade-comparison report | `scripts/build_trade_comparison.py` | Live-vs-BT trade comparison CSV (dealfix schema) + clear-view HTML; refined with intermediate verdicts (BT data cutoff, price-path drift) | ✅ | Joins `plans` + `tickets` + `deals` against latest `*_dukascopy_live_vs_dukascopy.json` reconcile output. Memory `Trade Comparison Verdicts Refined`. |
| Parity calibrator | `scripts/calibrate_for_parity.py` | Multi-pair high-trade-count calibration so live-vs-BT parity can be measured quickly. Optimises `trades / day` (NOT profit) subject to floor sanity. | ✅ | Output: `artifacts/calibration/{pair}_{main_tf}_parity.json` per pair. |
| End-to-end reconcile stitcher | `scripts/reconcile_live.py` | One command: `replay_service_config` → build live DF → match → write HTML + JSON. Pure glue. | ✅ | The script works; the underlying mismatch is data-source provenance (memory `Reconciliation Mismatch Root Cause`). |
| Live-day reset | `scripts/reset_live_day.py` | Clean-slate: stop runner, flatten MT5 positions, archive `plans/tickets/state/errors/crashes`. VPS-only. | ✅ | Archives — nothing destroyed; recoverable. Runner stays stopped (deliberate). |
| Three-tier data architecture | _(unbuilt)_ | Tag every BT row with provenance (Dukascopy / MT5 / merged) so reconcile can pick the right source per pair | 🔘 | The blocker for 100% match. Cross-listed in Stage 1. |
| Parity harness as CI gate | _(unbuilt)_ | A PR that drifts live⇄BT beyond threshold fails before merge | 🔘 | Pillar 5. |
| Drift detector + alert | _(unbuilt)_ | Watch every closed live trade → if drift > threshold, alert (and optionally trigger re-sweep) | 🔘 | Pillar 5. |
| Auto-retune trigger | _(unbuilt)_ | "When parity slips by X for Y days, kick off a fresh sweep" | 🔘 | Pillar 5. |

**Flows up from Stage 5** by reading `artifacts/live/<instance>/{plans, tickets.jsonl, deals.jsonl, config.json}` — VPS-side artifacts pulled back to the laptop.
**Flows up from Stage 1** by re-running the BT against Dukascopy and (when available) MT5 broker data over the same window.
**Flows down to Stage 4** by emitting HTML reports under `artifacts/live/reconcile/` that the UI links to from the run page.

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
