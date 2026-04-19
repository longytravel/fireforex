# Fire Forex — Architecture

Concise module + data-flow reference. For the operating manual see
`C:\Users\ROG\Projects\Fire Forex\CLAUDE.md`. For the new-session intro
see `C:\Users\ROG\Projects\Fire Forex\docs\next-session-handover.md`.

## Top-level picture

```
 ┌───────────┐     HTTP      ┌──────────────┐     import      ┌────────────┐
 │  Browser  │ ─────────────▶│  FastAPI     │ ───────────────▶│   ff/      │
 │ (SPA, JS) │◀── JSON ──────│  (app/)      │◀───────────────│  package   │
 └───────────┘               └──────────────┘                 └────┬───────┘
                                    │                              │
                                    │ reads/writes                 │ calls
                                    ▼                              ▼
                          ┌────────────────┐            ┌──────────────────┐
                          │ artifacts/     │            │ ff_core  │
                          │  history.csv   │            │ (Rust wheel)     │
                          │  runs/*.npz    │            └──────────────────┘
                          │  baseline.json │                     │
                          │  volatility_   │                     │ reads
                          │    cache.json  │                     ▼
                          └────────────────┘          ┌─────────────────────┐
                                                      │ G:\My Drive\        │
                                                      │  BackTestData\      │
                                                      │   (parquet per TF)  │
                                                      └─────────────────────┘
```

- The browser is a plain single-page app served from
  `C:\Users\ROG\Projects\Fire Forex\app\static\`.
- The backend is FastAPI launched via uvicorn from
  `C:\Users\ROG\Projects\Fire Forex\run.py` (`run.py web`).
- All business logic lives in `C:\Users\ROG\Projects\Fire Forex\ff\` —
  the backend is a thin wrapper.
- The Rust engine is built locally from core/ via maturin (`ff_core`). It is
  called from `C:\Users\ROG\Projects\Fire Forex\ff\harness.py`.
- Data is parquet files on `G:\My Drive\BackTestData\` (Dukascopy);
  location overridable with `FF_DATA_ROOT`.

## Request flow: `POST /api/defaults`

```
browser ──{recipe, complexity_level, overrides}──▶ FastAPI route
                                                           │
                                                           ▼
                          ff.defaults.complexity.complexity_to_ea(recipe, level)
                                                           │  (returns full EA)
                                                           ▼
                          ff.defaults.volatility.derive_ranges(pair, tf)
                                                           │  (ATR-driven,
                                                           │   reads cache or
                                                           │   computes + writes)
                                                           ▼
                          ff.defaults.overrides.apply_overrides(ea, overrides)
                                                           │
                                                           ▼
                          flatten schema for UI rendering
                                                           │
                   ◀──── JSON: groups, knobs, step info, defaults ────
```

Why server-side: the schema includes `Group`, `Branch`, and
`engine_mapping` callables that cannot be sent over JSON. The browser
works with a flattened projection and sends back only the deltas.

## Request flow: `POST /api/run`

```
browser ──{recipe, overrides, n_trials, seed, layer_name}──▶ FastAPI route
                                                                       │
                                                                       ▼
                    app/jobs.py::start()
                                                                       │
                    ┌── _lock.acquire(blocking=False) ───── 409 if busy
                    │
                    │  ─ spawn daemon thread ─
                    ▼
           complexity_to_ea(recipe)  →  apply_overrides(ea, overrides)
                                                                       │
                                                                       ▼
           ff.harness.run(ea, n_trials=..., seed=..., progress_cb)
                                                                       │
                                                                       ▼
           sampler → encoding → ff_core.batch_evaluate()
                                                                       │
                                         ▲                             │
                                         │ heartbeat thread            │
                                         │  0.5s tick, interp 0.45→0.85│
                                         │                             │
                                                                       ▼
                    compute KPIs → write artifacts/history.csv
                                 → write artifacts/runs/<id>.npz
                                                                       │
           ◀──── JobState.result = {kpis, curve, params, ...} ─────────
```

The browser polls `GET /api/jobs/{id}` for `progress`, `message`, and
(when done) the `result` payload.

## Threading model

- One run at a time, enforced by `threading.Lock` in
  `C:\Users\ROG\Projects\Fire Forex\app\jobs.py`. Second `POST /api/run`
  while busy returns **409 Conflict**.
- The harness in
  `C:\Users\ROG\Projects\Fire Forex\ff\harness.py` spawns a **daemon
  heartbeat thread** during the Rust sweep. The Rust call itself is
  blocking and emits no progress, so the heartbeat linearly interpolates
  the progress bar between `0.45` and `0.85` on a 0.5s tick. The thread
  is stopped via a `threading.Event` once the call returns.

## Where the override schema is applied

Override edits are applied **server-side** in
`C:\Users\ROG\Projects\Fire Forex\ff\defaults\overrides.py::apply_overrides`.
They have to land server-side because:

- `engine_mapping` is a list of Python callables generated from
  `engine_schema`. It cannot survive JSON serialisation.
- `Group`, `Branch`, and dataclass knobs are schema types, not dicts.
- The sampler respects group on/off flags during sampling, so the
  final EA must be the real thing, not a JSON projection.

The browser only sends a small override dict (`groups`, `knobs`,
`signal_families`, `global.step_multiplier`). The backend rebuilds the
EA from the recipe and layers the override onto it for every request.

## Volatility cache

`C:\Users\ROG\Projects\Fire Forex\artifacts\volatility_cache.json` holds
the **median 14-bar ATR in pips** per `(pair, main_tf)`. It is the
backbone of `ATR_RULES` in
`C:\Users\ROG\Projects\Fire Forex\ff\defaults\volatility.py`: every
pair-aware knob's default range is derived as an ATR multiple
(`lo_mult`, `hi_mult`).

**How to invalidate:** delete the file (or the offending key inside it).
The next call to `derive_ranges` will read the parquet for that
`(pair, main_tf)` and rewrite the cache entry. Do this after:

- adding a new pair or timeframe to the data directory;
- adding / changing an entry in `ATR_RULES`;
- replacing the underlying parquet history.

Cache I/O is the only filesystem side effect that defaults code has;
everything else is pure.
