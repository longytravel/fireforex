# Day-watch handover — 2026-04-22

Written as the user leaves for the day. Multi-instance refactor is
merged and live; three strategies are trading in parallel on the
IC Markets MT5 demo account. User wants periodic monitoring + a
clean reconcile when they're back.

## Current live state (2026-04-22 ~11:16 UTC)

- **VPS**: `fxvps-838969`, Tailscale IP `100.102.241.9`, SSH alias `ff-vps`.
- **Runner**: one process, multi-instance, scheduled task `ff-live-runner`.
- **HEAD**: `origin/main` at `695dce6 deploy: live config from run
  complexity_L10_EUR_USD_M15_20260422_111436`.
- **Active instances** (all 28 pairs, M15, no trailing/BE/chand/partial
  per parity_guard; entry + SL + TP only):

| Instance | Signal | Magic |
|---|---|---|
| `complexity_L10_EUR_USD_M15_20260422_111232__20260422_111326` | ema_cross | 20260420 |
| `complexity_L10_EUR_USD_M15_20260422_111400__20260422_111414` | macd_cross | 20260421 |
| `complexity_L10_EUR_USD_M15_20260422_111436__20260422_111458` | donchian | 20260422 |

- **Deactivated**: legacy `complexity_L10_EUR_USD_M15_20260421_212921__20260422_055259`
  (auto-migrated from last night, then superseded by today's three
  deploys via `deploy/instances/active.json`).
- **Web UI**: laptop `http://127.0.0.1:8000`, PID 47692, fresh code loaded.
- **MT5 demo**: flat at start (reset before merge). Positions will
  accumulate as signals fire across instances × pairs.

## Pre-authorisation (written per the user's explicit direction)

While the user is out, the day-watcher (me or next-session Claude after
a context clear) may take ANY of the following actions without asking:

- SSH to `ff-vps` to read state, tail logs, run diagnose.
- Kill + restart the ff-live-runner scheduled task if it dies, stalls
  (>60 min no state), or is duplicated.
- Force-kill stale python.exe runner processes.
- Run `.venv\Scripts\python.exe scripts\reset_live_day.py` if positions
  run away (>40 open per instance, or any sign of uncontrolled firing).
- Edit `deploy/instances/active.json` (commit + push) to deactivate a
  broken instance, then restart runner so the change takes effect.
- Run `.venv\Scripts\python.exe scripts\reconcile_live.py --instance
  <id>` to produce per-instance parity reports.
- Push small config fixes (typos in pairs lists, magic collisions) to
  `origin/main` without additional approval.
- Update `docs/live/HANDOVER-*.md` / `artifacts/live/overnight_watch.log`
  freely — these are append-only audit logs.

What still requires explicit user approval:
- New code on `ff/live/*`, `app/routes.py`, `ff/replay.py` (production
  live-trading code paths). Write on a branch; wait for user.
- Any commit message hinting at schema / parameter semantics changes.
- Any push that rewrites history (force-push, amend of pushed commits).

## Watch cadence + what to record

Every 30 minutes:
1. SSH single query to `ff-vps` covering: runner PIDs; per-instance
   state.json presence; plan counts per instance; ticket counts per
   instance; MT5 open positions count; errors tails.
2. Append one line to `artifacts/live/overnight_watch.log` with the
   diff vs prior tick (new fires per instance, new closes, errors).
3. Detect anomalies per the policy below. Take the fix action
   immediately if authorised above.

Anomaly → action matrix:

| Signal | Action |
|---|---|
| 0 python.exe runners | `schtasks /Change /TN ff-live-runner /ENABLE && schtasks /Run /TN ff-live-runner` + log RESTARTED |
| >1 runner_service processes | `taskkill /F /PID <newer>` keeping oldest |
| state.json missing for any instance 60 min after start | kill + restart once; if repeats log ESCALATE, stop loop |
| non-empty errors.jsonl with Traceback on one instance | deactivate that instance in active.json + push + restart runner; other two keep trading |
| MT5 positions count > 40 on any instance | emergency flat via reset_live_day + disable instance in active.json |
| tickets.jsonl grew since last tick | note new_fires count per instance; continue |
| state_sync push failures | log; don't act — state_sync credentials need laptop-side setup; trading unaffected |

## What the user should expect on return

**If everything worked**: `artifacts/live/overnight_watch.log` has one
line per 30-min tick, fires incrementing, no anomaly notes. The three
instance `tickets.jsonl` files each have ~N rows. MT5 account has
positions tagged by three distinct magics (20260420/21/22). Reconcile
can be run now:

```powershell
.\.venv\Scripts\python.exe scripts\reconcile_live.py --instance complexity_L10_EUR_USD_M15_20260422_111232__20260422_111326
.\.venv\Scripts\python.exe scripts\reconcile_live.py --instance complexity_L10_EUR_USD_M15_20260422_111400__20260422_111414
.\.venv\Scripts\python.exe scripts\reconcile_live.py --instance complexity_L10_EUR_USD_M15_20260422_111436__20260422_111458
```

Each produces `artifacts/live/<id>/reconcile/<stamp>.{html,json}` with
matched/missing counts and per-trade delta columns (`entry_delta_pips`,
`exit_delta_pips`, `pnl_delta`).

Expected volumes with ema_cross 10% WR + macd + donchian on 28 pairs
over ~6h: 20–80 fires per instance; closes depend on SL/TP hit time.
Some trades will still be open on return.

**If something went wrong**: the log shows which instance drifted and
what action was taken. The commit log on origin/main shows any pushes
I made. Three scenarios and what to do:

- `active.json` has fewer than three ids → one instance deactivated
  by me due to repeat errors. Read its errors.jsonl. Re-add to
  active.json + push to resume after fixing.
- Runner PID stale + log hasn't grown in >30 min → VPS reboot
  probably. `ssh ff-vps "schtasks /Change /TN ff-live-runner /ENABLE &&
  schtasks /Run /TN ff-live-runner"` then check log.
- MT5 positions far exceed what tickets.jsonl records → broker-side
  positions opened outside the runner (manual trades? another system?).
  Investigate before trusting the reconcile.

## Session-clear resume instructions

If the user clears context, the NEXT session me should:

1. Read this file + `docs/live/SESSION-RESUME.md`.
2. Read `artifacts/live/overnight_watch.log` to see what the PRIOR
   watcher did.
3. Resume the 30-minute watch pattern (see below for exact prompt).
4. No need to re-earn pre-authorisation — this handover IS the
   authorisation.

## Watch schedule

First wake in 20 minutes (catches first M15 bar close after restart).
Subsequent every 30 minutes. Loop until user prompt arrives or 8 hours
elapse.
