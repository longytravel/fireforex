# Handoff - 2026-04-26 Night

**Status:** mega brute-force work from PR #40 is being merged to `main`.
**Worktree:** `C:\Users\ROG\Projects\Fire Forex mega-brute`.
**Original checkout:** `C:\Users\ROG\Projects\Fire Forex` had separate Claude/live-reconcile dirty files, so the brute-force branch was kept isolated.

## What Changed

### Mega brute-force sweeps

- Signal libraries now cache contiguous per-variant slices (`variant_start` / `variant_end`).
- Rust `batch_evaluate` detects variant-contiguous arrays and evaluates only the selected variant slice.
- Large random sweeps stream compact metrics into `.npy` sidecars instead of storing full PnL/trades for every trial.
- `artifact_mode=auto` switches to lean mode above `50,000` trials.
- Rich artifacts remain capped at `50,000`; web/API trial cap is now `50,000,000`.
- Run page scatter/trial APIs read lean artifacts.

### Retained candidate bench

- `retain_top_per_metric` is configurable from the Run page.
- Default is `200`; API max is `10,000`; env override is `FF_LEAN_RETAIN_TOP_PER_METRIC`.
- Retained candidates are deduped across objectives.
- Retained artifacts include equity/PnL rows, metrics, encoded params, and trial JSON.
- `Trades` intentionally means raw highest trade count, even if losing, because it is useful for live-runner/execution stress tests.
- Jump-to-best uses retained objective maps, so retained winners have real equity curves.

### Stability fixes after UI testing

- Retained candidate memory is bounded to active top benches.
- Stale candidate detail is pruned during chunk processing.
- Retained trade records are trimmed to actual trade count rather than max-trades width.
- The incorrect profit gate on the `Trades` objective was removed.

## Recent Main Work Preserved

- PR #42 switched cost-table building to MT5 tick data for real bid/ask spreads and added last-run cost cards.
- PR #43 ranks optimiser-selected trials by an IC-realistic adjusted-PnL proxy when `artifacts/cost_table.json` covers the pair, falling back to legacy Quality otherwise.
- PR #41 made the PR paperwork checklist enforce `PROGRESS.md` alongside `HANDOFF.md` and `docs/ARCHITECTURE_MAP.md`.

## Verification

- `uv run python scripts/check_map.py` passed: all tracked files are referenced in `docs/ARCHITECTURE_MAP.md`.
- `uvx ruff check ff/harness.py app/routes.py app/jobs.py app/models.py tests/test_lean_artifact_routes.py` passed.
- Earlier focused route/cache tests passed: `9 passed`.
- Earlier Node syntax check passed for `app/static/app.js`.
- Earlier PR verification included Rust `cargo test` and a synthetic variant fast-path benchmark.
- Real UI smoke: `150,000` CAD_CHF Level 10 trials completed in `24.05s`, about `6,237 evals/sec`, retaining `2,422` unique candidates.

## Data Caveat Found During Testing

The data store is not uniformly full-history. The UI's "Full" button uses the full range present in the parquet files for the selected pair/timeframes, but some pair files only contain recent history.

Short H1 examples currently seen:

- `AUD_CHF`: starts `2026-03-23`
- `CAD_CHF`: starts `2026-03-23`
- `NZD_CAD`: starts `2026-03-23`
- `NZD_CHF`: starts `2026-03-23`
- `EUR_USD`: starts `2025-04-20`
- `GBP_USD`: starts `2024-01-15`

Long-history examples still present:

- `AUD_USD`: starts `2006-11-12`
- `USD_JPY`: starts `2007-03-27`
- `USD_CAD`: starts `2012-01-11`
- `EUR_AUD`: starts `2012-01-11`

For high-trade-count brute-force tests, use a long-history pair until the short parquet files are repaired.

## Not Yet Done

- True metrics-only Rust kernel. Lean mode still asks the current Rust entrypoint to fill PnL/trade buffers per chunk, then retains only the top bench. The next speed jump is a metrics-only kernel plus full-detail replay for retained/on-demand trials.
- Data repair for short-history parquet files.
- Issue #32: propagate new cost-realism columns through `MatchedRow` in `ff/live/reconcile.py`.
- Issue #33: live guard should read submit-time tick spread, not stale closed-bar spread.
- Issue #34: post-fill slippage cap is documented but not enforced after order fill.
- Older scanner findings:
  - #12 path traversal in `app/routes.py`
  - #13 out-of-bounds `sig_bar_index`
  - #14 metric key mismatch (`win_rate` vs `win_rate_pct`)
- Backport the live runner forming-candle fix into the BT engine.
- Deferred docs sweep: markdownlint MD040 and stale docs references.

## Resume Steps

1. Pull `main` in the normal checkout.
2. Test long-history brute-force runs on `AUD_USD`, `USD_JPY`, `USD_CAD`, or `EUR_AUD`.
3. Use `Retain top` at the default `200`, or raise it if future walk-forward needs a wider candidate bench.
4. Repair/rebuild short parquet files for `AUD_CHF`, `CAD_CHF`, `NZD_CAD`, and `NZD_CHF`.
5. Build the metrics-only Rust kernel next for another large speed step.

## Useful Commands

- Start the UI from this worktree:
  `python run.py web --host 127.0.0.1 --port 8765 --no-browser --no-reload`
- Focused tests:
  `python -m pytest tests/test_lean_artifact_routes.py tests/test_signal_cache.py -q`
- Ruff:
  `python -m ruff check ff/harness.py app/routes.py app/jobs.py app/models.py tests/test_lean_artifact_routes.py`

## Current In Flight

- After this merge: no active brute-force PR.
