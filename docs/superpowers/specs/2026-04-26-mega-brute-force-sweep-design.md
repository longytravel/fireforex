# Mega brute-force sweep design

**Date:** 2026-04-26  
**Area:** Stage 3 - BACKTEST SWEEP  
**Status:** planning only; no implementation in this PR

## Goal

Make raw brute-force backtesting scale from today's interactive sweeps into
mega sweeps without losing the Run page workflow.

Target capabilities:

- 2M trials as a normal serious run.
- 50M trials as an overnight/headless mega mode.
- 100+ conditional parameters without Python dict overhead dominating.
- Tick-data validation for survivors, not for every random candidate.
- Run page support for scatter, metric selection, "jump to best", and selected
  trial detail view.

Optimisers such as Optuna, CMA-ES, Bayesian search, and walk-forward are
explicitly later work. This design is for the raw brute-force foundation.

## Current bottleneck

The current harness builds one pooled signal library and passes all signals to
Rust. Each trial chooses one `signal_variant`, but the Rust trial loop still
scans the whole pooled signal array and filters out non-matching variants.

That is fine for small libraries. It collapses when Level 10 pair scans create
millions of pooled signals. Recent history rows show:

- `complexity_L10_AUD_NZD_H1`: 2,000 trials, 3,556 variants, ~13.2M pooled
  signals, ~152 bt/sec.
- `complexity_L10_CHF_JPY_H1`: 2,000 trials, 3,556 variants, ~13.3M pooled
  signals, ~180 bt/sec.
- Smaller signal libraries can run in the thousands to tens of thousands
  bt/sec.

The slow shape is therefore mostly algorithmic and memory-bandwidth related,
not "Python instead of Rust". Rust is already the hot loop; it is being asked
to scan too much irrelevant data.

## Non-goals

- Do not add Optuna/CMA/walk-forward in the same work.
- Do not make tick replay the first-stage evaluator for huge random sweeps.
- Do not store every trade log/equity curve for every mega-sweep trial.
- Do not remove UI functionality to make the engine faster.

## Proposed architecture

### 1. Variant-indexed signal library

Store signal arrays in a layout that lets the engine jump directly to the
chosen variant.

Instead of:

```text
trial -> scan all pooled signals -> skip non-matching variant
```

Use:

```text
trial -> variant_id -> [start, end) signal slice -> evaluate only that slice
```

Implementation shape:

- Keep pooled arrays for cache locality.
- Add `variant_start` and `variant_end` arrays, one row per variant.
- Sort/group signals by variant first, then optionally by bar index inside each
  variant.
- Preserve `variant_map` for fingerprint resolution and live deploy parity.

Expected effect: the slow L10 cases should become proportional to the selected
variant's signal count instead of the entire pooled library's signal count.

### 2. Chunked mega sweeps

Do not allocate `n_trials * max_trades` buffers for the whole run.

Run in chunks:

```text
for chunk in chunks(seed, n_trials, chunk_size):
    generate params for this trial-id range
    evaluate chunk
    append compact metrics
    update top-K heap
    checkpoint progress
```

Suggested chunk sizes:

- interactive: 2k-50k
- serious: 50k-250k
- overnight/headless: tune by memory budget

This enables 2M and 50M runs without requiring one giant in-memory matrix.

### 3. Compact metrics ledger

Persist every trial's summary metrics, not every trial's trade log.

Keep per trial:

- trial id
- metric row (`NUM_METRICS`, likely `float32` on disk)
- compact parameter row or deterministic replay seed path
- enough metadata to reconstruct the trial exactly

This preserves the Run page scatter and metric-selection workflow.

For 2M trials x 25 metrics as `float32`, metrics are roughly 200 MB raw before
compression. That is large but feasible. The currently explosive part is
per-trial PnL/trade buffers.

### 4. Top-K retention

Maintain top-K candidates during the sweep so the UI and deploy flow have fast
access to likely winners.

Keep top-K by one or more objectives:

- quality
- total pips / return
- profit factor
- drawdown-constrained quality
- trade-count objective

Store full detail for:

- best trial
- top-K selected candidates, configurable
- any trial the user explicitly opens, after on-demand replay

### 5. On-demand single-trial replay

The Run page can still support "jump to best" without storing every trade log.

Flow:

```text
user selects metric / clicks jump to best
API finds best trial id from compact metrics
API reconstructs that trial's params
API runs one-trial replay
UI displays equity curve + trades
optional: cache that replay result
```

Instant UI actions read the compact scoreboard. Expensive detail views are lazy.

### 6. Compiled sampler path

Python dict sampling is acceptable at 2k trials, but not at 2M-50M or 100+
parameters.

Add a compiled sampler representation:

- flatten schema into a sampler plan
- encode conditional groups/branches as masks
- generate param rows in Rust for a deterministic trial-id range
- make trial id + global seed enough to reconstruct any trial

This keeps random brute force reproducible without storing every full Python
trial dict.

### 7. Tiered tick-data validation

Tick data is the final judge, not the first filter.

Recommended pipeline:

```text
50M coarse candidates
-> keep top broad survivor set
-> M1/sub-bar serious replay
-> tick replay on survivors
-> robustness / perturbation / walk-forward later
```

Tick replay across every random candidate will waste the powerful laptop on
obviously bad configurations. The engine should first eliminate candidates
using the cheapest faithful approximation.

## Run page behavior

Keep instant:

- scatter plot
- Y-axis metric dropdown
- "jump to best"
- history row
- best-trial summary
- top-K table
- deploy selected winner

Lazy-load:

- clicked trial equity curve
- clicked trial trade list
- per-trial cost-realism overlay
- replay/export for non-best trials

For ordinary small runs, the existing rich artifact mode can remain. Mega mode
uses compact artifacts by default.

## Proposed modes

### Rich mode

Use for small interactive runs.

Stores:

- metrics
- per-trial PnL buffers
- best trade log
- rich UI data

### Lean mode

Use for mega sweeps.

Stores:

- metrics ledger
- param/replay metadata
- top-K candidates
- best trade log
- checkpoints

Replays non-best trials on demand.

## Build order

1. Add benchmark instrumentation around signal build, sampling, encoding, Rust
   evaluation, artifact save, and UI load.
2. Refactor signal library cache to include variant start/end offsets.
3. Add a Rust entrypoint that evaluates only the selected variant slice.
4. Add a chunked sweep executor with compact metrics accumulation.
5. Add top-K retention and exact trial reconstruction.
6. Add lean artifact format and Run page lazy replay.
7. Lift/replace the 50k cap for lean/chunked mega mode.
8. Move large-run sampling/encoding into Rust.
9. Add tiered tick replay for survivor sets.

## Acceptance criteria

First implementation milestone:

- Existing small/rich sweeps remain compatible.
- Run page scatter and "jump to best" still work.
- A Level 10 run with millions of pooled signals evaluates by selected variant
  slice, not by full pooled scan.
- 2M-trial lean sweep can complete without allocating per-trial trade buffers.
- A selected trial can be replayed from `trial_id + seed + schema` and produce
  the same metrics/trades as the original evaluation.
- The old 50k web cap is replaced by a mode-aware limit with clear warnings.

Stretch milestone:

- 50M-trial headless run can checkpoint, resume, and produce a compact ledger
  plus top-K candidates.
- Tick replay runs on a survivor set rather than the full brute-force set.

