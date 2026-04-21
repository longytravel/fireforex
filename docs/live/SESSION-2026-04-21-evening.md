# Live parity session — evening 2026-04-21

Written at the end of a long session where we shipped the exit-manager
parity port, deployed a 10-pair M15 live trial, and discovered several
stacked bugs that made the deploy visible-to-MT5 but divergent from
the backtest. Trading is halted as of this writing.

## What happened today

### Shipped (in order)

1. **`ff/live/exit_manager.py`** — Python port of the per-sub-bar
   management loop from `core/src/trade_full.rs` (165-502). Covers
   trailing, breakeven, chandelier, partial close. Parity tests drive
   the same synthetic scenarios through both `ff_core.batch_evaluate`
   and the port and assert `exit_reason + exit_price` match.
2. **`ff/live/parity_guard.py`** — refuses deploys that use
   stale/session/max_bars (not ported yet). Wired into
   `app/routes.py::post_live_deploy_from_run`.
3. **`LiveConfig.max_open_per_pair`** (default 1) — cap in
   `_evaluate_and_fire` stops positions from stacking on every bar
   close. Plumbed through `runner_service`, `live_jobs`, and the
   deploy endpoint.
4. **Desktop bat hardening** —
   - LF → CRLF (cmd.exe needs CRLF or it parses char-by-char).
   - Stripped em-dashes / smart quotes that broke parenthesised IF
     blocks with `". was unexpected at this time."`.
   - Rewrote the `live-state` tar extract without a pipe inside an
     IF block (two-step via `%TEMP%`).
5. **`Reset Live Day (VPS).bat` + `reset_live_day.py`** redesigned —
   reset stops trading (no auto-restart), closes ALL positions by
   default (orphan positions from prior deploys were surviving the
   magic-only filter), disables the scheduled task (`/End` alone only
   stops the current run; the task re-triggered on a timer and picked
   up the stale service_config).
6. **`Deploy Fire Forex.bat`** — re-enables the scheduled task before
   `/Run`, pairing with the new reset behaviour.

### Bugs found late in the session

All shipped to `origin/main` within the session.

- **`parity_guard` false-negative** — checked
  `engine.<group>.when_on.test` for on/off, but the sampler puts on/off
  at `engine.<group>.test`. The guard flagged nothing and a trial with
  `session.test=True` deployed live. The live runner has no session
  check, so every fire outside the backtest's 9-14 UTC window was a
  silent parity break. **Fix**: check `group.test` (commit `ae88beb`).
- **`exit_manager.params_from_trial` zero-out** — same on/off bug, plus
  wrong leaf key names (`trigger_pips` / `activate_pips` /
  `distance_pips`). Effect: every management group read as zero
  live-side even when the trial had it active — trailing never armed,
  breakeven never locked, chandelier never tightened, partial never
  fired. **Fix**: correct paths
  (`when_on.trigger`, `when_on.activate`, `when_on.mode.fixed.distance`,
  `when_on.mode.atr.mult`) — same commit.
- **`spread_at_fire_pips` off by 10000x** — MT5 returns rates.spread as
  an integer in broker POINTS, not price units. On a 5-digit /
  3-digit-JPY broker 1 pip = 10 points. Runner was dividing by
  `pip_value` (0.0001), surfacing 50000-pip spreads on normal majors.
  **Fix**: divide by 10 (commit `08e262d`).
- **`state_sync` push timeout** — first orphan-branch push to
  `origin/live-state` over the VPS link exceeded the 60s default and
  errored out every iteration, so `live-state` never appeared on
  origin. **Fix**: bump _git default timeout to 180s (commit `08e262d`).

### Head of `main` now

`08e262d live: reset truly disables runner + spread unit fix + state_sync timeout`

## Current state as of shutdown

- **VPS scheduled task**: disabled by reset_live_day.py (`schtasks
  /Change /DISABLE`). Will NOT auto-launch.
- **MT5 demo**: flat (reset closed all positions by default).
- **artifacts/live/**: state.json / plans / tickets / errors / crashes
  archived under `artifacts/live/archive/20260421_132645/` and similar.
- **Laptop ↔ origin ↔ VPS**: origin/main at `08e262d`. Laptop in
  sync. VPS needs a `Reset Live Day (VPS).bat` or
  `Deploy Fire Forex.bat` double-click to pull `08e262d`.
- **`live-state` branch**: not on origin (see bug above). With the
  180s timeout fix, next state_sync push should create it on the
  first runner start-up post-deploy. Laptop Restart bat will then
  populate `artifacts/live/` from that branch.

## What is still broken or unverified

1. **Live-to-backtest reconciliation has never been run end-to-end.**
   The auto-reconciler wants a pinned backtest NPZ plus live
   plans+tickets plus MT5 deals in the same UTC window. We have not
   once produced a matched row with non-zero `entry_delta_pips` on a
   known-good trial.
2. **State sync** not proven end-to-end since the timeout fix. Needs
   one full session with a legit trial to confirm `live-state` appears
   on origin and the laptop Restart bat extracts it cleanly.
3. **Live management** (trailing/BE/chand/partial) wired into
   `_manage_open_positions` but the side-effects in MT5 have never
   been observed — no fire in the session made it past entry before
   the bugs were caught and the runner was shut down.
4. **Parity test coverage**: the 4 parity tests in
   `tests/test_exit_manager.py` drive `batch_evaluate` vs the Python
   port on hand-built M1 series. They proved the state machine matches.
   They do NOT prove the key-mapping in `params_from_trial` matches
   what the sampler emits — that was the bug nobody caught until we
   inspected a deployed `service_config.json` by hand. The regression
   test added today pins the real shape.
5. **58+ python.exe processes on the VPS**: cosmetic (the Check bat
   counts all of them), but worth filtering to the runner PID only so
   future diagnosis is clearer.
6. **Deploy endpoint does not forward `max_open_per_pair`**.
   LiveConfig defaults to 1 so the cap is still live, but the UI has
   no way to change it per deploy.

## Dev ergonomics — recommendation for the next session

The user is juggling two machines (laptop UI + VPS runner) and has had
to context-switch between Claude Code instances on each. Friction
points:

- Laptop Claude cannot see VPS logs or MT5 state without the user
  pasting screenshots of Check/runner output.
- VPS Claude cannot see the laptop's backtest results or service_config
  mutations without the user pasting output back.
- The user pastes the same content back-and-forth and gets tired / makes
  mistakes (we churned three times on desktop bat CRLF / em-dash /
  pipe-in-IF before the script actually ran to completion).

**Claude ↔ Claude bridge — concrete proposals:**

**Option A. Git-mediated message bus (cheapest, works today).**
Each Claude writes to a shared directory under `artifacts/bridge/` on
a `claude-bridge` orphan branch, polled by the other. Messages are
plain JSON files timestamped and namespaced by sender. Laptop Claude
posts "I pushed commit X, please pull and restart"; VPS Claude
responds "pulled, runner up, state_sync pushed, here are the first 5
plans." Reuses the same orphan-branch pattern that state_sync already
uses. No new infra. ~1 hour of work.

**Option B. HTTP bridge** over the existing `:8000` FastAPI app.
Add a `/bridge/message` endpoint on the VPS runner process (already
running FastAPI via `run.py web` is laptop-side only — the VPS has the
runner but no web app, so this needs a new tiny app or re-uses the
runner's thread). Latency is sub-second but requires a port exposed
through the VPS firewall. More work, brittle.

**Option C. Shared session-log scrape.** Each Claude's session
transcript lives under `C:\Users\<user>\.claude\projects\<slug>\`.
One could tail + push it to a shared branch. The other Claude reads.
Full visibility into what each Claude tried. Privacy-wise this is the
user's data, stays inside their own repo — fine. Works without new
code but produces a very chatty branch. ~30 min to prototype. This is
the user's own suggestion and it is the most direct path to "the two
Claudes just see each other's work."

Recommendation: **Option C first** as a 30-minute experiment (just
symlink or scripted push of the Claude Code transcript file into a
`claude-bridge` orphan branch at each turn), graduate to **Option A**
if the transcript-scrape turns out to be too noisy. Skip Option B
until there is a real-time requirement.

### Near-term priority list for the next session

1. **Deploy a clean trial with no session/stale/max_bars.** Watch
   a full fire -> manage -> exit cycle end-to-end. Confirm the Python
   exit_manager emits modify_sl, partial_close and close as expected
   and MT5 reflects the moves.
2. **Run the auto-reconciler** against the pinned NPZ and the live
   tickets. Surface the first real parity delta numbers.
3. **Confirm `live-state` appears on origin** after a live runner has
   been up for >60s (180s timeout fix + the orphan-branch first-push
   case). Laptop Restart bat pulling it and materialising
   `artifacts/live/` is the proof.
4. **Prototype Claude ↔ Claude bridge (Option C).** Even a minimal
   `push-transcript.bat` on each side would remove ~80% of the
   user's manual paste load.
5. **Filter the Check script's python.exe list** to the runner PID
   only. Probably a one-line Get-WmiObject filter by command line.
