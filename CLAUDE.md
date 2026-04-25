# Fire Forex — Operating manual for Claude

## Purpose

Fire Forex is a local optimisation system for forex strategies with a VPS
live-trading runner. Users describe an EA as a schema of knobs; the backend
sweeps parameters against a **local Rust engine** (`ff_core` in `core/`); the
web UI compares each run to a pinned baseline. Production-style live trading
runs on a VPS and reconciles daily against backtest.

Deep tour: `docs/ARCHITECTURE_MAP.md` (the audited file-by-file map — start here), `docs/ARCHITECTURE.md`, `docs/next-session-handover.md`, `HANDOFF.md`.

## How to talk to the user

- Plain English. No shell commands, yaml/json, or file paths in chat.
- One short sentence before acting; one short sentence after.
- Technical detail lives in files and tool calls, not chat.
- Max 5 bullets when presenting a plan.
- User is non-technical — avoid specs/ADRs for sign-off. See memory `feedback_no_rubber_stamp_process.md`.

## Where the rules live

| Rule | File |
|---|---|
| Python style | `.claude/rules/python-style.md` |
| Rust style (ff_core) | `.claude/rules/rust-style.md` |
| Testing | `.claude/rules/testing.md` |
| Live-trading discipline | `.claude/rules/trading.md` |
| PR workflow + PROGRESS maintenance | `.claude/rules/workflow.md` |

## Session paperwork

- `HANDOFF.md` — current state, refreshed at session end (Stop hook blocks if stale).
- `PROGRESS.md` — milestone register. Tick boxes when work ships, never rewrite.
- Both are injected at SessionStart by `.claude/hooks/session-start.sh`.

## Directory map

- `run.py` — CLI entry (`run.py web` to serve UI; positional EA path to sweep).
- `core/` — Rust engine (`ff_core` crate, pyo3 bindings).
- `ff/` — Python engine package (schema, sampler, encoding, harness, signal_lib, defaults).
- `app/` — FastAPI backend + vanilla-JS frontend + live-trading runner.
- `eas/` — example EA configs.
- `artifacts/` — history.csv, runs/*.npz, baseline.json, volatility_cache.json.
- `scripts/` — operational scripts (restart server, VPS bootstrap, reconcile, etc.).
- `docs/` — architecture, handovers, metrics, parity plan.
- `tests/` — pytest suite.

## Run commands

```powershell
# Web UI (user runs this, not Claude)
.\scripts\ff_restart_server.ps1

# CLI backtest sweep
.\.venv\Scripts\python.exe run.py eas\complex01.py --trials 500 --seed 42

# Inspect an EA without running
.\.venv\Scripts\python.exe run.py eas\complex01.py --inspect

# Tests
.\.venv\Scripts\python.exe -m pytest tests\

# Rebuild Rust engine after core/ changes
.\.venv\Scripts\maturin.exe develop --release

# Pre-PR ritual (after /simplify + /code-review)
.\scripts\pre-pr.ps1
```

## Do

- Reuse the `ff/` package — it's tested and stable.
- Add pair-aware knobs via `ff/defaults/volatility.py::ATR_RULES` (one entry per knob, `key → (lo_mult, hi_mult)`).
- Scale-free knobs (RR ratios, EMA periods, hour-of-day) go in the scale-free block of `derive_ranges`.
- Pick best trial via `ff.harness.pick_best()` — don't re-hardcode `argmax(metrics[:,9])`. See `docs/metrics.md` for metric keys.
- Run overrides + mapping server-side. Frontend sends a recipe + an override dict only.
- Restart the web UI via `scripts\ff_restart_server.ps1` (or the `.bat` wrapper). Never let Claude spawn uvicorn directly.
- Use the `add-forex-knob` skill (and `validate-forex-knob`) for any new signal/exit/filter. Silent-no-op bugs have shipped before; the skill prevents them.
- Follow live-trading discipline in `.claude/rules/trading.md` before touching anything under `app/live_runner/` or signal-variant code.

## Don't

- Don't edit the installed `.pyd`. Engine source is in `core/src/`; rebuild with `maturin develop --release`.
- Don't add Streamlit/Gradio. FastAPI + vanilla JS is the chosen stack.
- Don't add a database. JSON + `artifacts/history.csv` is sufficient.
- Don't ship a hosted app. Local-only on `127.0.0.1`.
- Don't touch `ff/defaults/pair_tf.yaml` without a reason — it's a fallback; volatility cache is truth.
- Don't start uvicorn during a Claude session (see `.claude/rules/trading.md`).
- Don't write specs for the user to sign off on — they'll rubber-stamp. Keep plans plain-English and short.

## Overrides shape

```json
{
  "groups":          { "trailing": false, "breakeven": true },
  "knobs":           { "stop_loss.atr.mult":
                         {"min": 1.0, "max": 3.0, "step": 0.1,
                          "enabled": true, "frozen": 1.5 } },
  "signal_families": { "ema_cross": true, "macd_cross": false },
  "global":          { "step_multiplier": 2.0 }
}
```

All keys optional. Unknown paths ignored.

## Flow: UI → backend

```
POST /api/defaults → complexity_to_ea(recipe, level) → apply_overrides → flattened schema
POST /api/run     → jobs.start() (threading.Lock) → rebuild EA server-side
                   → apply_overrides → harness.run(ea, progress_cb=...)
                   → heartbeat thread → artifacts/history.csv + runs/*.npz
```

## Root CLAUDE.md discipline

This file stays under 150 lines. New universal rule → consider twice before adding here. Path-scoped rule → `.claude/rules/<topic>.md` with `paths:` frontmatter. Domain fact → an ADR under `docs/adr/` (not a standing discipline — only for genuine architectural decisions).
