# Read me first — picking up Fire Forex live trading

If you (human or Claude) just joined the repo, read this before touching
anything.

## 1-minute state summary

- **Repo**: `C:\Users\ROG\Projects\Fire Forex`. Python 3.12 + Rust
  (`ff_core` via maturin) + FastAPI web UI + MT5 live runner.
- **Live runner**: process on VPS `fxvps-838969` (Tailscale IP
  `100.102.241.9`), scheduled task `ff-live-runner`, logs to
  `artifacts/live/runner.log`.
- **VPS access**: SSH alias `ff-vps` (Tailscale + OpenSSH). Key at
  `~/.ssh/ff_vps`. From laptop Bash: `ssh ff-vps "dir \"C:\\Projects\\Fire
  Forex\\artifacts\\live\""`.
- **Architecture**: multi-instance — one runner process manages N
  strategies via `artifacts/live/<instance_id>/` per-instance scope.
  See `docs/live/ARCHITECTURE-multi-instance.md`.

## Before you touch anything, run this

```bash
ssh ff-vps ".venv/Scripts/python.exe scripts/diagnose_vps.py"
```

It prints: open positions, scheduled task state, every active
instance's config summary, log tails. Read it, understand what's live,
then act.

## What is committed where

- `main` — production code + live runner. Currently at the multi-
  instance refactor (2026-04-22).
- `live-state` — orphan branch. VPS pushes `artifacts/live/*` here every
  60s so the laptop can see plans/tickets/state without SSH.
- `deploy/instances/*.json` — committed deploy configs. `active.json`
  lists which are running.
- `artifacts/live/` — runtime state (GITIGNORED).
- `artifacts/runs/` — backtest NPZs (GITIGNORED).

## Key files to read if…

- **…you want to understand the runner loop**:
  `ff/live/runner.py::run()` (line ~240). Outer loop iterates
  `instances`, inner loop iterates pairs per instance.
- **…you want to understand Deploy**:
  `app/routes.py::post_live_deploy_from_run` mints instance_id,
  allocates magic, writes deploy/instances/<id>.json, updates
  active.json, commits + pushes.
- **…you want to understand boot**:
  `ff/live/runner_service.py::main()` — distribute deploy configs,
  migrate legacy, discover actives, build LiveConfigs, call run().
- **…you want to understand parity**:
  `ff/live/reconcile.py::reconcile()` matches bt vs live trades.
  `scripts/reconcile_live.py --instance <id>` is the CLI.

## Minimum-risk checklist before merging or deploying

- [ ] `pytest tests/ -q` green
- [ ] No pending changes to `ff/live/*` or `app/routes.py` without a
      Codex review (see `codex` skill — we've been running
      gpt-5.4 reasoning=high reviews)
- [ ] VPS runner process alive or explicitly stopped
      (check `schtasks /Query /TN ff-live-runner`)
- [ ] `deploy/instances/active.json` reflects what you actually want
      running; NOT a stale list

## Invariants not to break

1. Never spawn a second runner_service process. MT5 is single-terminal.
2. Never commit `artifacts/live/` (gitignored for a reason).
3. Never bypass `parity_guard.un_portable_knobs()` in Deploy — it
   blocks trials using management groups the live runner can't honour.
4. Every instance gets a unique `magic_number`. Never reuse.
5. Stop the runner on VPS before changing `ff/live/runner*.py` or
   major live code paths.

## Open items (tracked in plans / handovers)

- `scripts/desktop/Deploy Fire Forex.bat` still copies the legacy
  `deploy/live_config.json`. Works because runner auto-migrates, but
  should eventually consume `deploy/instances/` directly.
- Reactivation of a deactivated instance keeps its old config. Deliberate
  but ugly — future UI should offer "delete instance" first.
- Reconciler `build_live_df` doesn't yet ingest MT5 deals — exit-side
  parity numbers are NaN until that lands.

## Session memory

- User profile + feedback: `C:\Users\ROG\.claude\projects\C--Users-ROG-Projects-Fire-Forex\memory\MEMORY.md`
- Architectural plan: `C:\Users\ROG\.claude\plans\go-into-plan-mode-graceful-matsumoto.md`
- Tonight's overnight watch log: `artifacts/live/overnight_watch.log`
- Prior session handovers: `docs/live/SESSION-2026-04-21-*.md`,
  `docs/live/WAKE-UP-2026-04-22.md`

Don't skip these. They hold context the code alone can't give you.
