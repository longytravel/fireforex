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
- **Always restart the web UI via `scripts\ff_restart_server.ps1`**
  (or the `.bat` wrapper). It kills stale listeners, clears
  `__pycache__`, and starts one clean uvicorn. Running
  `python run.py web` directly is now guarded — it will abort if :8000
  is already in use.

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
- **Don't start a uvicorn during testing.** If you need the web UI
  up to verify a change, ask the user to run
  `scripts\ff_restart_server.ps1`. Background uvicorns spawned by
  Claude are the root cause of the stale-code / stale-`.pyc` bugs
  that keep recurring.

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

# context-mode — MANDATORY routing rules

You have context-mode MCP tools available. These rules are NOT optional — they protect your context window from flooding. A single unrouted command can dump 56 KB into context and waste the entire session.

## BLOCKED commands — do NOT attempt these

### curl / wget — BLOCKED
Any Bash command containing `curl` or `wget` is intercepted and replaced with an error message. Do NOT retry.
Instead use:
- `ctx_fetch_and_index(url, source)` to fetch and index web pages
- `ctx_execute(language: "javascript", code: "const r = await fetch(...)")` to run HTTP calls in sandbox

### Inline HTTP — BLOCKED
Any Bash command containing `fetch('http`, `requests.get(`, `requests.post(`, `http.get(`, or `http.request(` is intercepted and replaced with an error message. Do NOT retry with Bash.
Instead use:
- `ctx_execute(language, code)` to run HTTP calls in sandbox — only stdout enters context

### WebFetch — BLOCKED
WebFetch calls are denied entirely. The URL is extracted and you are told to use `ctx_fetch_and_index` instead.
Instead use:
- `ctx_fetch_and_index(url, source)` then `ctx_search(queries)` to query the indexed content

## REDIRECTED tools — use sandbox equivalents

### Bash (>20 lines output)
Bash is ONLY for: `git`, `mkdir`, `rm`, `mv`, `cd`, `ls`, `npm install`, `pip install`, and other short-output commands.
For everything else, use:
- `ctx_batch_execute(commands, queries)` — run multiple commands + search in ONE call
- `ctx_execute(language: "shell", code: "...")` — run in sandbox, only stdout enters context

### Read (for analysis)
If you are reading a file to **Edit** it → Read is correct (Edit needs content in context).
If you are reading to **analyze, explore, or summarize** → use `ctx_execute_file(path, language, code)` instead. Only your printed summary enters context. The raw file content stays in the sandbox.

### Grep (large results)
Grep results can flood context. Use `ctx_execute(language: "shell", code: "grep ...")` to run searches in sandbox. Only your printed summary enters context.

## Tool selection hierarchy

1. **GATHER**: `ctx_batch_execute(commands, queries)` — Primary tool. Runs all commands, auto-indexes output, returns search results. ONE call replaces 30+ individual calls.
2. **FOLLOW-UP**: `ctx_search(queries: ["q1", "q2", ...])` — Query indexed content. Pass ALL questions as array in ONE call.
3. **PROCESSING**: `ctx_execute(language, code)` | `ctx_execute_file(path, language, code)` — Sandbox execution. Only stdout enters context.
4. **WEB**: `ctx_fetch_and_index(url, source)` then `ctx_search(queries)` — Fetch, chunk, index, query. Raw HTML never enters context.
5. **INDEX**: `ctx_index(content, source)` — Store content in FTS5 knowledge base for later search.

## Subagent routing

When spawning subagents (Agent/Task tool), the routing block is automatically injected into their prompt. Bash-type subagents are upgraded to general-purpose so they have access to MCP tools. You do NOT need to manually instruct subagents about context-mode.

## Output constraints

- Keep responses under 500 words.
- Write artifacts (code, configs, PRDs) to FILES — never return them as inline text. Return only: file path + 1-line description.
- When indexing content, use descriptive source labels so others can `ctx_search(source: "label")` later.

## ctx commands

| Command | Action |
|---------|--------|
| `ctx stats` | Call the `ctx_stats` MCP tool and display the full output verbatim |
| `ctx doctor` | Call the `ctx_doctor` MCP tool, run the returned shell command, display as checklist |
| `ctx upgrade` | Call the `ctx_upgrade` MCP tool, run the returned shell command, display as checklist |
