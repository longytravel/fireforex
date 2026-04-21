# Session state — 2026-04-21 end of day (parity-v2 shipped)

Picks up where `SESSION-2026-04-21.md` left off. If you're a fresh Claude
session (or a new human) landing on this repo, **read this first**. It
covers what parity-v2 added, how the laptop↔VPS loop now works, the
"reset live day" workflow, and what's still unfinished.

## TL;DR

- **Backtest↔live parity workbench is live.** `python run.py replay`
  takes the deployed config and re-runs it as a single-trial backtest
  per pair against Dukascopy, writing per-trade NPZ + summary JSON under
  `artifacts/replay/<run_id>/<stamp>/`.
- **Reconciler now matches every parity field** (signal variant, spread,
  slippage, close reason) and rolls up per pair. Per-pair card grid on
  the Live tab. `/api/live/stats_by_pair` returns the rollup as JSON.
- **Plans/tickets/state auto-sync VPS → laptop.** VPS runner force-pushes
  to a `live-state` branch every 60s. Laptop Restart shortcut fetches +
  extracts the branch into `artifacts/live/`. Web server also re-pulls
  every 60s so the tab stays fresh passively.
- **Reset Live Day shortcut** (VPS desktop): flattens all fireforex
  positions in MT5, archives today's plans/tickets/state under
  `artifacts/live/archive/<stamp>/`, wipes originals, restarts the
  runner. One click = clean slate for a new debug day.
- **Browser auto-reload** on server restart via a boot_id poll.
- Tests: **158 passing**.
- Last commit on main: `ee85fb1 parity: replay CLI, per-pair reconcile,
  vps<->laptop state sync`.

## Machine topology (unchanged, re-stated for clarity)

- **Laptop** (dev + backtest)
  - Repo: `C:\Users\ROG\Projects\Fire Forex`
  - Desktop has **Restart Fire Forex** shortcut → kills :8000, git pull,
    fetches live-state, extracts to `artifacts/live/`, starts web UI,
    opens browser.
  - Runs the web UI. Never runs the live trading loop.
  - Dukascopy parquets live at `G:\My Drive\BackTestData\`.

- **VPS** (live trading only)
  - Repo: `C:\Projects\Fire Forex`
  - Desktop has **Deploy Fire Forex**, **Check Fire Forex**,
    **Reset Live Day** shortcuts.
  - IC Markets MT5 demo account 52754648 (GMT+2/+3), logged in 24/7.
  - Scheduled Task `ff-live-runner` auto-restarts on failure every 60s.
  - `.env.live` carries MT5 creds. Gitignored.

- **GitHub** (source of truth)
  - Branch `main` — normal code. Laptop pushes, VPS hard-resets to origin
    on each Deploy click.
  - Branch `live-state` — orphan, force-pushed by VPS every 60s. Carries
    only `plans/*.jsonl`, `tickets.jsonl`, `state.json`, `errors.jsonl`,
    `crashes.jsonl`. Never touched by humans.

## Day-in-the-life (zero commands, four shortcuts)

**Laptop** (morning):
1. Double-click **Restart Fire Forex**. Pulls main, fetches live-state,
   opens http://127.0.0.1:8000 in a browser.
2. Pair cards under Live tab show per-pair trade count + Δpips vs
   replay. Cards refresh every 5s (UI poll) + every 60s (server pulls
   fresh state from VPS).

**VPS** (after config changes on laptop):
1. Laptop: in browser, Deploy to live ▶ → writes/commits/pushes
   `deploy/live_config.json`.
2. VPS: double-click **Deploy Fire Forex** → hard-reset to origin/main
   (picks up new config + any runner-code changes), restarts
   ff-live-runner Scheduled Task.

**VPS** (end of the debug day, starting a clean one):
1. Double-click **Reset Live Day**. Confirms once. Stops runner,
   flattens all positions with magic=20260420 in MT5, archives today's
   plans/tickets/state under `artifacts/live/archive/<stamp>/`, wipes
   originals, restarts runner. Runner boots cold, state.json rebuilds
   from MT5's empty position snapshot.
2. Nothing is destroyed — archives stay on VPS disk. To recover, copy
   the archive dir back into `artifacts/live/`.

## The parity workbench — how to use it

### Run a replay

Laptop terminal:

```powershell
.\.venv\Scripts\python.exe run.py replay
```

No arguments needed. Defaults to `artifacts/live/service_config.json`.
The command:

1. Derives the replay window from the min/max `signal_bar_ts` across
   `artifacts/live/plans/*.jsonl` (±1 day pad). Falls back to
   (today − 30, today) when plans are absent.
2. For each pair in the deployed config:
   - Tops up Dukascopy M1 for the window and fans out M5/M15/M30/H1/H4/D.
   - Rebuilds the EA via `complexity_to_ea` + `apply_overrides` (same
     code path the VPS runner uses).
   - Calls `harness.run(..., frozen_trial=config["best_trial"],
     save_artifacts=False)` — single-trial, same param vector, no sweep
     artifacts written.
3. Concatenates per-pair trade logs into one NPZ at
   `artifacts/replay/<source_run_id>/<stamp>/trades.npz`, with a
   sibling `summary.json` and `latest_stamp.txt` pointer.

### Read the rollup

The Live tab's **Per-pair parity** card shows each pair's live trade
count, matched-vs-backtest Δpips, and any mismatch categories. Backed
by `GET /api/live/stats_by_pair` which joins the latest replay NPZ
against today's plans.

### Run the per-trade reconciler

Live tab → **Run reconcile** button. The iframe loads the full
reconcile HTML report:

- Per-pair summary table at the top.
- Per-trade breakdown with every parity field side-by-side:
  signal_variant, signal_family, bt/live entry and exit prices,
  bt/live spread, live slippage, bt/live close reason, pnl. Rows
  within tolerance render green; mismatches amber with the failing
  category listed.

Close-reason mismatches collapse engine-managed exits (TRAILING,
BREAKEVEN, CHANDELIER, MAX_BARS, STALE) and live EXPERT/CLIENT/MOBILE
under a canonical OTHER bucket — only SL↔!SL and TP↔!TP count as a
divergence. Keeps the report signal-to-noise high.

## Files that matter (new or materially changed in this session)

| File | Role |
|------|------|
| `ff/harness.py` | Widened trade log; `frozen_trial` + `save_artifacts` kwargs on `run`; per-run exec scalars stamped into NPZ |
| `ff/exit_codes.py` | Numeric engine exit codes → human names, used by the trade log widener and reconcile canonicalisation |
| `ff/replay.py` | One-shot replay orchestrator. `replay_service_config(path)` + `_resolve_window(plans_dir)` |
| `run.py` | New `replay` subcommand — `python run.py replay [config.json]` |
| `ff/live/runner.py` | Plan dict now carries `signal_variant` + `signal_family` + `spread_at_fire_pips`. Spawns `_spawn_state_sync` thread alongside heartbeat + auto-reconciler |
| `ff/live/state_sync.py` | Git-worktree-backed pusher of `artifacts/live/{plans, tickets.jsonl, state.json, errors.jsonl, crashes.jsonl}` to the `live-state` branch. Force-pushes each minute |
| `ff/live/broker_mt5.py` | `fetch_recent_deals` now carries commission + swap + reason code + mapped reason name; new `DEAL_REASON_NAMES` enum |
| `ff/live/reconcile.py` | Tolerances + MatchedRow + _classify widened with parity-v2 fields. New `ReconcileReport.by_pair()`. Render HTML now has a per-pair summary header + per-trade parity columns |
| `app/routes.py` | New `/api/live/stats_by_pair`. `/api/version` returns `boot_id` |
| `app/live_state_puller.py` | 60s daemon that fetches + extracts `live-state` branch on the laptop web server |
| `app/api.py` | Wires `live_state_puller.start_pull_thread` into FastAPI startup |
| `app/static/index.html` | New "Per-pair parity" card hosting `#live-pair-cards`. JS cache-buster bumped to `?v=parity-v2` |
| `app/static/app.js` | `liveRefreshPairCards()` on the existing 5s timer; boot-id mismatch auto-reloads the page |
| `scripts/reset_live_day.py` | Stop runner → flatten MT5 → archive → wipe → restart. Called by the VPS desktop shortcut |
| `scripts/desktop/Reset Live Day (VPS).bat` | The VPS-side double-click wrapper |
| `scripts/desktop/Restart Fire Forex (laptop).bat` | Gained a step 4/5: fetch + extract `origin/live-state` into `artifacts/live/` |
| `.gitignore` | Added `artifacts/replay/` (replay NPZs stay local) |

Test files: `test_trade_log_roundtrip.py` (dtype + scalar asserts),
`test_reconcile.py` (six new parity-v2 fixtures + by_pair test),
`test_live_runner_synthetic.py` (parity fields on emitted plan),
`test_replay.py` (new), `test_state_sync.py` (new).

## What currently works

- VPS runner fires plans on every M15 close across 18 pairs.
- Plans/tickets/state sync VPS → GitHub → laptop automatically.
- Replay reproduces the deployed config deterministically per pair.
- Per-pair card grid + per-field reconcile report on Live tab.
- Browser auto-reloads after a laptop restart (no more Ctrl+F5).
- Reset Live Day gives you a clean debug slate in one click.

## Known gaps / follow-ups

1. **IC Markets M1 ingest is still deferred.** Replay uses Dukascopy,
   same feed as the pinned backtest, so parity is apples-to-apples on
   the engine side. Drift against real ICM fills shows up as
   mismatched_spread / mismatched_slippage in the reconcile report.
   Only worth building if those deltas prove too noisy to be useful.

2. **`execution_delay_bars` knob is not in the engine yet.** ICM only
   publishes closed M1 bars ~30s after the minute ends, so live fills
   lag the backtest's "next M1 open". The reconciler already surfaces
   this as `mismatched_entry_price` + slippage. If the drift is
   consistently >1 pip on majors, add the knob on top of
   `simulate_trade_full`.

3. **Per-pair calibration isn't wired into Deploy.** The pinned config
   uses one trial for all 18 pairs. `scripts/calibrate_for_parity.py`
   exists but isn't connected to the Deploy flow — would need per-pair
   overrides that apply_overrides walks.

4. **Reconciler live-side join uses plans-only** (plus broker deals via
   `fetch_recent_deals`). If MT5's deal history is gappy the rollup
   undercounts. Wire a closed-deal poll into the runner loop to
   persist them to a local tickets-close jsonl before trusting this
   aggregate for production decisions.

5. **Signal timing is NOT a bug.** Live plans show
   `signal_bar_ts=09:00:00` with `fired_at_ts=09:15:56`. Both the live
   rollup (`ff/live/runner.py::_rollup_main_tf`) and the offline
   resampler (`ff/data/resample.py::_resample_bars`) use
   `label="left", closed="left", origin="start_day"`. Label = bar
   open time. 09:00 label = `[09:00, 09:15)` interval, closed at
   09:15 when the next M1 bar arrives. Same convention both sides,
   so reconciler timestamps line up.

6. **Multi-position per pair is expected.** Runner doesn't block new
   signals on existing open positions. Each main-TF bar close =
   new deterministic plan_id. If you want one-at-a-time, that's a new
   knob.

## State at handover (end of session)

- Last commit: `ee85fb1 parity: replay CLI, per-pair reconcile, ...`.
- Laptop: UI running, freshly pulled. Pair cards will populate the
  moment the VPS runner has pushed its first state snapshot.
- VPS: user has applied the Deploy + Restart flow (steps 1 + 2). Reset
  Live Day shortcut needs one manual "Send to Desktop (create
  shortcut)" on the .bat file — see handover step at top.
- No open questions blocking next work.

## Priority list for next session

1. **Wait and measure.** Let today's trading proceed, watch the
   reconcile report after a few hours. The magnitude of
   mismatched_spread + mismatched_slippage is what decides whether
   `execution_delay_bars` and/or ICM M1 ingest are worth building.
2. **Per-pair calibration wired into Deploy.** Pick one pair with
   the worst drift and verify a tailored override closes the gap.
3. **Closed-deal poll** on the runner so the reconciler has a
   complete live side without relying on ad-hoc `fetch_recent_deals`.
4. **One-shot debug harness for a single plan_id** — point at a live
   fill, replay just that bar in the engine, step through the Rust
   exit logic. Useful when a mismatched_closure fires.
