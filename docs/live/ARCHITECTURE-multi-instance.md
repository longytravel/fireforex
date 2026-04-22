# Live runner — multi-instance architecture

One runner process drives N strategy instances against ONE MT5 demo
account. Each instance = one trial, one set of pairs, one unique magic
number, one artifact subdir. They share the MT5 terminal, state_sync
thread, heartbeat, and auto-reconciler.

Shipped on branch `multi-instance`, merged to main 2026-04-22.

## Why

Before the refactor, `ff/live/runner.py` hardcoded module-level paths
(`LIVE_DIR / "plans"`, `LIVE_DIR / "tickets.jsonl"`, ...) and carried a
single `_pair_states_cache` global. Running two strategies required two
runner processes — blocked by MT5's single-terminal/single-login model
and by artifact collisions (plans dir, tickets file, state.json).

## Layout

```
artifacts/live/                          (gitignored — runtime state)
├── instances.json                       index + magic_counter
├── runner.log                           shared process log
├── crashes.jsonl                        shared
├── state_sync_errors.jsonl              shared
├── archive/                             reset dumps here
└── <instance_id>/                       per-instance scope
    ├── config.json                      mirrors deploy/instances/<id>.json
    ├── pinned_run.json                  pointer to backtest NPZ
    ├── plans/YYYY-MM-DD.jsonl           per-day plan log
    ├── tickets.jsonl                    MT5 submit results
    ├── state.json                       open positions snapshot
    ├── errors.jsonl                     per-instance errors
    └── reconcile/<stamp>.{html,json}    reconciler output

deploy/                                  (committed — source of truth)
├── instances/
│   ├── active.json                      {"active": ["id1", "id2"]}
│   └── <instance_id>.json               per-instance config
└── live_config.json                     backcompat mirror of last deploy
```

## Instance identity

- **instance_id**: `<source_run_id>__<YYYYMMDD_HHMMSS>` minted by the
  Deploy endpoint. Unique per deploy. Filename stem == `config.instance_id`
  (enforced on distribute).
- **magic_number**: allocated from `instances.json.magic_counter`,
  monotonically increasing, never recycled.
- **plan_id**: `<instance_id>_<pair>_<signal_bar_ts>_<+1|-1>` — prevents
  two instances trading same pair/bar from dedup-collapsing.

## Deploy pipeline

```
Laptop UI -> POST /api/live/deploy_from_run
  │  mints instance_id; allocates magic
  │  writes deploy/instances/<id>.json
  │  appends id to deploy/instances/active.json
  │  git add + commit + push
  ▼
origin/main
  ▼
VPS: git pull + schtasks /Run ff-live-runner
  ▼
runner_service.main()
  │  _distribute_deploy_configs()    reads active.json, imports configs
  │                                   deactivates any id NOT in active list
  │  _auto_migrate_legacy()          migrates pre-refactor service_config.json
  │                                   skips if embedded instance_id already in deploy/
  │  _discover_instance_configs()    filters by instances.json.active
  │  _build_live_config()            one LiveConfig per active instance
  ▼
runner.run(instances)
  │  rejects duplicate instance_id or magic (fail-fast)
  │  ONE MT5Broker shared
  │  poll tick: for inst in instances: for pair in inst.pairs: _poll_pair(inst, ...)
  │  each fire swaps broker.cfg to inst.broker via _with_broker_cfg
  ▼
MT5 positions tagged by magic_number per instance
```

## Critical invariants

1. **One MT5 terminal per runner process.** Don't spawn multiple
   runner_service processes. The explicit duplicate-instance check in
   `run()` will also catch that if one process loads overlapping configs.
2. **Unique magic per instance.** Enforced at Deploy time (counter) and
   at runner boot (fail-fast check). Never reuse a magic even after
   archive — `magic_counter` is monotonic.
3. **Per-instance artifact scope.** Anything that could collide between
   instances (plans, tickets, state, errors, pinned run, reconcile
   reports) lives under `artifacts/live/<instance_id>/`. Shared files at
   the top level are process-wide (runner log, crashes, sync errors,
   index).
4. **`active.json` is the source of `which should trade`.** Import gate
   AND deactivate trigger. Removing an id from `active.json` + next
   boot = `instances.json.active = false` for that id, runner skips it.
5. **Broker object is stateless per call.** `_with_broker_cfg(broker,
   cfg, fn)` swaps `broker.cfg` around every order-issuing call so
   MT5 requests carry the calling instance's magic. Works because the
   main loop is single-threaded.

## Known caveats

- If an instance is deactivated then later re-added to `active.json`,
  `_distribute_deploy_configs` skips the existing `artifacts/live/<id>/
  config.json` so the OLD trial stays loaded. Deliberate — reactivation
  UI will remove the old dir first. Not blocking.
- `reset_live_day.py --magic-only` currently reads magic only from the
  legacy service_config.json. Not multi-instance aware. Default (no
  flag) closes ALL positions on the account, which is safe for demo.
- `deploy/live_config.json` still written for bat backcompat. Safe to
  delete once `scripts/desktop/Deploy Fire Forex.bat` is updated to
  consume `deploy/instances/` directly (future chore).

## Ops runbook

**Deploy a new instance**: web UI → Parameters tab → Deploy button.
Writes `deploy/instances/<new_id>.json`, updates `active.json`, commits,
pushes. On VPS: double-click "Deploy Fire Forex" shortcut (pulls main,
kicks runner). New instance starts firing next bar close.

**Deactivate an instance**: edit `deploy/instances/active.json` — remove
the id from the array. Commit + push. On VPS: restart runner (double
click Deploy bat or `schtasks /End` + `/Run`). Runner boot flips
`instances.json.instances[<id>].active = false`, skips it.

**Pause everything**: `Reset Live Day (VPS)` shortcut. Closes all
positions, disables task, archives artifacts. Deploy re-enables per
`active.json` on next start.

**Per-instance reconcile**: `python scripts/reconcile_live.py
--instance <id>`. Writes HTML/JSON under
`artifacts/live/<id>/reconcile/<stamp>.*`.

**See all instance state**: `ssh ff-vps '.venv\Scripts\python.exe
scripts\diagnose_vps.py'`. Lists every active instance, pairs, magics,
open position counts.

## File-by-file change index

| File | Role |
|---|---|
| `ff/live/runner.py` | `LiveConfig.instance_id` + path properties; `run()` takes list; `_with_broker_cfg` helper; `_plan_id` includes instance_id; duplicate ID/magic fail-fast; dedup scans plans + tickets |
| `ff/live/runner_service.py` | `_distribute_deploy_configs` (active.json filter + deactivate missing); `_auto_migrate_legacy` (preserves embedded instance_id, skips if deploy already has it); `_discover_instance_configs` (active filter); file logging to `artifacts/live/runner.log` |
| `ff/live/state_sync.py` | `SYNC_GLOBS` recursive (`*/plans/...`, `*/tickets.jsonl`, ...) |
| `ff/replay.py` | `plans_dir = config_path.parent / "plans"` (per-instance) |
| `app/routes.py::post_live_deploy_from_run` | mints instance_id + allocates magic + writes per-instance config + updates active.json + commits all |
| `app/live_jobs.py` | `_instance_roots()` honors `instances.json.active`; status/plans/positions iterate every active instance; each row tagged with `instance_id` |
| `scripts/reconcile_live.py` | `--instance` flag; auto-pick when single active |
| `scripts/reset_live_day.py` | `--instance` flag (per-instance archive); full reset walks every subdir |
| `scripts/diagnose_vps.py` | iterates instance configs + dumps `instances.json` |
| `tests/test_live_runner_synthetic.py` | +4 multi-instance unit tests |
| `tests/test_runner_service_multi_instance.py` | 4 deploy-pipeline service tests |

## How to verify after a merge

1. `pytest tests/ -q` → 181 tests pass
2. `scripts/reconcile_live.py --help` — shows `--instance` flag
3. `scripts/reset_live_day.py --help` — shows `--instance` flag
4. On VPS after pull + restart: `artifacts/live/runner.log` has
   `[svc] distributed deploy instance -> ...` lines, one per active
   instance. Each instance directory exists under `artifacts/live/`.
5. Deploy a second strategy via UI (with different pairs so fire-rate
   is visible), restart runner on VPS, confirm both instances poll in
   the log and both show `instance=<id>` in their lines.
