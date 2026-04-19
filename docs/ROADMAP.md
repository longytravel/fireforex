# Fire Forex — Roadmap (2026-04-18 → 2026-07-18)

North star: "make it a best project, solid foundations." This doc is the 2–3-month queue. Scope per item: **effort (S/M/L)**, **risk (L/M/H)**, **preconditions**. Do not do anything later in the list before its preconditions land.

Legend for effort: S ≈ half-day, M ≈ 1–3 days, L ≈ 1–2 weeks.

---

## 1. Now (this week)

### 1.1 Optuna (TPE) integration
**Effort:** M · **Risk:** M · **Preconditions:** none (random sampler is the reference).

**Goal:** a second optimiser that reliably beats `RandomSampler` on the same harness (same data, same 2,257 signals, same seed), appearing as **Optimiser: Optuna (TPE)** in the Run-tab dropdown.

**Concrete plan:**
- Add `ff/optimisers/optuna_tpe.py` exposing `class OptunaTPESampler` with the same public contract as `ff.sampler.RandomSampler` (`__init__(engine_schema, n_variants, seed)`, `.sample(n_trials) -> list[dict]`, `.seed`). Internally wraps `optuna.samplers.TPESampler` and an in-memory study. Expose `report(trial_dict, objective_value)` so the harness can feed each evaluated trial back before the next `sample(1)` suggestion.
- Refactor `ff/harness.py`: `harness.run(ea, *, n_trials, seed, optimiser="random", progress_cb=None)`. Dispatch on `optimiser` to a sampler factory (`ff/optimisers/__init__.py`). Preserve today's batch-sampling fast path for `"random"`; for TPE, switch to a `sample(1)` → evaluate → `report` loop.
- UI: add an **Optimiser** `<select>` on the Run tab next to seed/trials (`random` default, `optuna_tpe`). Wire through `/api/run` body as `optimiser: str`. Persist in history CSV as a new column.
- Deps: add `optuna>=3.6` to `requirements-web.txt`. No other new deps.
- **Fallback:** if after 500 trials Optuna's best objective ≤ random's best (same seed), the run still completes and Results shows a red "no improvement over random" badge. Keep the random baseline pinned; do not silently swap defaults.

### 1.2 Multi-run comparison dashboard
**Effort:** S · **Risk:** L · **Preconditions:** history CSV already persists the 8 KPIs + `run_file`.

- History tab: per-row actions — **Pin as baseline** (exists), **Overlay** (new, toggles an `.npz` equity curve onto a shared chart), **Re-run** (deferred to §2.4).
- New panel above the table: chart overlaying equity curves from every checked row; colour by layer name; hover shows final total pips.
- Keep it client-only — read existing `/api/runs/{file}` endpoint; no backend changes.

### 1.3 Basic pytest coverage
**Effort:** S · **Risk:** L · **Preconditions:** none.

- Add `tests/test_overrides.py` — round-trip `apply_overrides` on a known `complexity_to_ea` recipe; assert tickbox=off removes the knob, min/step/max round-trip, invalid keys raise clearly.
- Add `tests/test_jobs_progress.py` — drive `app/jobs.py` with a stub harness that calls `progress_cb(0.1)` → `progress_cb(1.0)`; assert `JobState.progress` and `message` update monotonically and `status` transitions `running → completed`. Use a `threading.Event` to avoid `time.sleep`.
- Wire a minimal `pytest.ini` + GitHub-Actions-free local `run.py test` shortcut. Target: green on Windows Python 3.12.

---

## 2. Next (this month)

### 2.1 Walk-forward evaluation
**Effort:** M · **Risk:** M · **Preconditions:** §1.1 (need harness already split from optimiser choice).

- Add `ff/walkforward.py` with `split(dates, n_folds, oos_frac) -> list[(is_slice, oos_slice)]`. Default 4 folds, 30 % OOS per fold, no embargo v1.
- `harness.run(..., walkforward=True)` optimises on IS, scores the best params on OOS, returns **both** KPI sets. Results tab gains an "OOS" column next to "IS"; the 8-number panel shows OOS as the headline and IS as the small number — the user has been clear OOS is the honest metric.
- History CSV stores IS *and* OOS columns; baseline compares OOS.

### 2.2 Seed sweep
**Effort:** S · **Risk:** L · **Preconditions:** §2.1 (so "best params" is already OOS-selected and actually worth re-running).

- Run tab gets a **Seed sweep** checkbox (default off). When on, after the main run the server re-runs the best-params-only recipe across N∈{5,10,20} seeds and reports median, p10, p90 of total-pips + Sharpe.
- New results panel: horizontal box-plot per KPI. One number to trust: "median-of-N OOS Sharpe". Stored in history as `seed_sweep_n`, `seed_sweep_median_*`.

### 2.3 Per-signal-family auto-range
**Effort:** M · **Risk:** M · **Preconditions:** none functionally, but lands cleanest after §2.1 so we can verify auto-ranges don't hurt OOS.

- `ff/defaults/volatility.py` already derives pip ranges from 14-bar ATR. Extend the same pattern:
  - `ff/defaults/signal_density.py` — for each `(pair, main_tf)` and each family (`ema_cross`, `macd_cross`, `donchian`), compute a "target crossings per year" band (e.g. 80–400). Invert through the indicator to produce period ranges that yield that band on the actual series. Cache keyed by `(pair, main_tf, family)` in the same parquet-mtime-aware cache as volatility.
  - Plug into `complexity_to_ea` so Parameters-tab defaults for EMA fast/slow, MACD fast/slow/signal, Donchian lookback come from real density instead of the current hand-coded `_ir(...)` bands.
  - Fallback: hand-coded bands stay as the YAML-style last resort.

### 2.4 Re-run with tweaks from History
**Effort:** S · **Risk:** L · **Preconditions:** §1.2 (history row actions wired).

- **Re-run** row action copies the row's recipe + overrides back into the Parameters tab and jumps the user there with a toast ("Loaded from <layer>@<timestamp>"). User can bump seed/trials/tickboxes and fire a fresh run. No new endpoints — reuses `POST /api/eas/<name>/load` pattern.

---

## 3. Later (quarter)

### 3.1 Rust engine wishlist (consolidate from `docs/rust-wishlist.md`)
**Effort:** L · **Risk:** M · **Preconditions:** needs engine-side changes in the local `core/` crate. Track as external dependency.

- **Per-hour slippage** — hourly slippage vector in place of scalar `slippage_pips`; new `PL_SLIPPAGE_MODE` slot.
- **New exit families** — chandelier exit, Kelly-scaled partial, TP-laddering. Each is a `Branch` arm under the exits `Group`.
- **Extra `PL_SIGNAL` filter slots** — 2 more filter slots so users can stack (e.g.) session + ADX + RSI. Requires bumping `NUM_PL` and the encoding contract.

Each item ships as: Rust PR → new Python-side schema nodes → one complexity-slider level that exposes it → smoke-test on EUR/USD H1.

### 3.2 Multi-pair portfolio testing
**Effort:** L · **Risk:** M · **Preconditions:** §2.1 (OOS numbers), §2.2 (seed sweep). Without those the aggregate is meaningless.

- Run tab multi-select for pairs. Backend runs the same EA over each pair in sequence (engine is already 800 bt/s, so 25 pairs × 500 trials ≈ minutes not hours). Aggregate: equal-weight portfolio equity curve, portfolio Sharpe, per-pair contribution bar chart.
- History stores one row per pair *and* one aggregate row.

### 3.3 Save/load recipes with versioning
**Effort:** S · **Risk:** L · **Preconditions:** none.

- `eas/user_*.json` already saves recipes. Add `schema_version` (int) and a migration shim `ff/recipes/migrate.py` so old files auto-upgrade on load. Add a **Duplicate / Rename / Delete** menu to the Parameters-tab recipe picker.

### 3.4 Cloud-optional mode (flagged off)
**Effort:** M · **Risk:** H · **Preconditions:** never land until §1–§2 are proven and the user explicitly asks.

- Put behind `FF_CLOUD=1`. Adds an `/api/run` variant that submits to a remote worker (e.g. Modal or a small EC2) with the same recipe JSON + parquet streamed from G:. Strictly opt-in — user is local-only today and wants to stay that way until there's a reason to move.

---

## 4. Risks & unknowns

Each guess gets one cheap measurement we can run next session.

| Guess | Why we're unsure | One measurement |
|---|---|---|
| **Optuna will beat random on this problem.** | Rust engine already does ~800 bt/s; random with a 500-sample budget is cheap and well-covered. TPE's win case is expensive objectives, not ours. | Run `random@seed=42,n=500` and `tpe@seed=42,n=500` on `eas/complex01.py`. Compare best OOS Sharpe. If gap < 0.1, TPE is not worth the complexity. |
| **ATR-derived pip ranges are better than the YAML fallback.** | The fallback was hand-tuned; derivation might widen or narrow in bad ways. | For 5 pairs × 3 TFs, run the same EA with `derive_ranges` on and off. Compare best OOS total pips. |
| **Walk-forward will find an edge where in-sample already looks like luck.** | Handover explicitly flags EUR/USD EMA-cross results as knife-edge (PF 1.006). | Once §2.1 lands, look at the OOS Sharpe distribution across all 25 pairs at complexity=3. If median ≤ 0 the honest answer is "EMA-cross has no edge; move on to new signal families". |
| **Seed sweep matters.** | If single-seed results are stable we're wasting compute. | On one pair, run the current baseline across 20 seeds. If stdev of total-pips / mean < 10 %, single-seed is fine and seed sweep becomes opt-in. |
| **Per-hour slippage moves the needle.** | Intuition says yes (London/NY spreads differ), but we've never quantified it. | Before the Rust change: simulate in Python with a crude 2× slippage on 22:00–02:00 UTC bars and re-score the current best params. If OOS Sharpe shifts > 0.2, it's real. |
| **Multi-pair portfolio beats single-pair.** | Diversification story is intuitive, but correlations across majors are high. | Compute pairwise correlation of daily returns from the top EUR/USD variant ported to GBP/USD and USD/JPY. If mean |ρ| > 0.6, portfolio Sharpe gains will be small. |

---

## 5. Explicitly out of scope for now

Listed so we stop re-discussing them: position sizing beyond fixed lots, live trading, MT5 bridge, alternative data, ML-based signal generation, anything requiring a GPU.
