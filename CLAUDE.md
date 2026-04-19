# Fire Forex — Operating manual for Claude

## Purpose

Fire Forex is a local optimisation system for forex strategies. Users
describe an EA as a schema of knobs (with per-pair, per-TF defaults),
the backend runs a parameter sweep against a **local Rust engine**
(`ff_core`, source in `core/`), and the web UI compares each run to a
pinned baseline. Everything runs on `127.0.0.1` — no DB, no hosted app.

For the deep tour see `docs/ARCHITECTURE.md` and
`docs/next-session-handover.md`.

## Directory map

- `run.py` — CLI entry point (`run.py web` or positional EA path).
- `demo_speed.py` — original 2-knob harness. Locked: seed, data,
  signals. Only the **optimiser** changes between layers.
- `core/` — **the Rust engine**. Self-contained crate (`ff_core`).
  `src/lib.rs` exposes `batch_evaluate` via pyo3. Rebuilt into `.venv`
  with `maturin develop --release` from the repo root.
- `pyproject.toml` — maturin build config. Points at `core/Cargo.toml`.
- `ff/` — the engine-side Python package.
  - `schema.py` — primitive types: `FloatRange`, `IntRange`, `Choice`,
    `Group`, `Branch`.
  - `signal_lib.py` — registry of entry signals.
  - `sampler.py` — random sampler, respects on/off groups.
  - `encoding.py` — trial dict → Rust param vector.
  - `preflight.py` — sizing + runtime estimates.
  - `harness.py` — the run loop; spawns the heartbeat thread during the
    Rust sweep and calls the progress callback.
  - `inspect.py` — text-based parameter inspector.
  - `defaults/complexity.py` — `complexity_to_ea(recipe, level)`:
    builds an EA from a `(pair, main_tf, sub_tf, level)` recipe.
  - `defaults/overrides.py` — `apply_overrides(ea, overrides)`:
    applies UI edits.
  - `defaults/volatility.py` — per-(pair, TF) ATR-driven ranges,
    cached to `artifacts/volatility_cache.json`. `ATR_RULES` is the
    extension point for new pair-aware knobs.
  - `defaults/pair_tf.yaml` — **fallback only**. Used when the
    volatility cache is missing. Don't edit without a reason.
- `app/` — FastAPI backend + static frontend.
  - `routes.py` / `api.py` — HTTP routes.
  - `jobs.py` — one-at-a-time background runner (`threading.Lock`).
  - `baselines.py` — pin/load the baseline snapshot.
  - `static/` — single-page frontend (plain JS, no framework).
- `eas/` — example EA config modules (`baseline.py`, `complex01.py`).
- `artifacts/` — `history.csv`, `runs/*.npz`, `baseline.json`,
  `volatility_cache.json`, `comparison.html`.
- `docs/` — handover, architecture, roadmap, knob explanations,
  rust wishlist, `metrics.md` (25-column catalogue + DSR primer).
- `tests/` — unit tests.

## Run commands

```powershell
# Web UI (primary)
.\.venv\Scripts\python.exe run.py web

# CLI backtest sweep against an EA module
.\.venv\Scripts\python.exe run.py eas\complex01.py --trials 500 --seed 42

# Inspect an EA's parameter tree without running
.\.venv\Scripts\python.exe run.py eas\complex01.py --inspect

# Tests
.\.venv\Scripts\python.exe -m pytest tests\

# Rebuild the Rust engine after editing anything in core/
.\.venv\Scripts\maturin.exe develop --release
```

## Do

- **Reuse the `ff/` package.** It's tested and stable.
- **Add new knobs via `ff/defaults/volatility.py::ATR_RULES`.** One
  entry per pair-aware knob — `key → (lo_mult, hi_mult)`. Scale-free
  knobs (RR ratios, EMA periods, hour-of-day) go in the scale-free
  block at the bottom of `derive_ranges` instead.
- **Write tests** for any new knob, override path, or default rule.
- **Pick best trial via `ff.harness.pick_best()`.** Don't re-hardcode
  `argmax(metrics[:,9])`. The helper takes `objective=<metric key>`,
  optional `constraints={"trades":{">=":100},...}`, and `tie_break`.
  Metric keys are listed in `ff.harness.METRIC_COLUMNS`; see
  `docs/metrics.md` for what each one means and why DSR is the
  recommended default for random sweeps.
- **Run server-side.** Overrides, defaults, mapping generation — all
  happen on the backend. The frontend only sends a recipe + an override
  dict.

## Don't

- **Don't edit the installed `.pyd`.** The engine source lives in
  `core/src/`. After changes, run `maturin develop --release` from the
  repo root — it rebuilds the `ff_core` wheel and drops the new `.pyd`
  into `.venv/Lib/site-packages/`. Restart the web server to pick it up.
- **Don't add Streamlit or Gradio.** FastAPI + vanilla JS is the
  chosen stack. An old plan mentioned Streamlit — it is obsolete.
- **Don't hide progress behind a blocking call.** The harness spawns
  a heartbeat thread during the Rust sweep; mimic that pattern for any
  new long-running step.
- **Don't touch `ff/defaults/pair_tf.yaml` without a reason.** It's a
  fallback when the volatility cache can't be computed. The real source
  of truth is the ATR-driven cache.
- **Don't add a database.** JSON files + `artifacts/history.csv` are
  sufficient.
- **Don't ship a hosted app.** Local-only, bound to `127.0.0.1`.

## Conventions

- **File layout** — business logic in `ff/`, HTTP + jobs in `app/`,
  example EAs in `eas/`.
- **Naming** — snake_case throughout; knob paths are dotted
  (e.g. `stop_loss.atr.mult`). Inside a `Branch`, the segment is the
  arm name. Inside a `Group`, use `when_on` as a segment
  (e.g. `session.when_on.hours_start`).
- **Override shape:**
  ```json
  {
    "groups":         { "trailing": false, "breakeven": true },
    "knobs":          { "stop_loss.atr.mult":
                          {"min": 1.0, "max": 3.0, "step": 0.1,
                           "enabled": true, "frozen": 1.5 } },
    "signal_families":{ "ema_cross": true, "macd_cross": false },
    "global":         { "step_multiplier": 2.0 }
  }
  ```
  All keys optional. Unknown paths are ignored.

## How user overrides flow

```
UI (parameters tab)
   │  recipe + overrides
   ▼
POST /api/defaults        → ff.defaults.complexity.complexity_to_ea(recipe, level)
                          → ff.defaults.overrides.apply_overrides(ea, overrides)
                          → returns flattened schema for rendering

POST /api/run             → app/jobs.py::start()  (acquires threading.Lock)
                          → rebuilds EA server-side from the same recipe
                          → ff.defaults.overrides.apply_overrides(ea, overrides)
                          → ff.harness.run(ea, progress_cb=...)
                          → heartbeat thread updates JobState.progress
                          → writes artifacts/history.csv + artifacts/runs/*.npz
```

Both endpoints rebuild the EA from the recipe — mapping callables never
round-trip through JSON.
