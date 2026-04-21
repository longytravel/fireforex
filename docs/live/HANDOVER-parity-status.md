# Handover — live/backtest parity status

**Written:** 2026-04-21, late afternoon, after parity-bug fix session.
**Previous handover:** `docs/live/SESSION-2026-04-21-end.md` (morning). This
doc supersedes it for parity specifics.

---

## Executive summary

Live trading is **submitting orders**, but the trades MT5 opens are
**not the same trades the backtest would have made**. The issue is not
the signal engine (that is ported correctly). The issue is in two
separate layers:

1. **Parameter freeze** — fixed this session. Live was using EA schema
   defaults (ATR×1.5 SL) instead of the winning trial's pinned values
   (fixed 50-pip SL). Patched: `LiveConfig.best_trial` is now threaded
   through to `_compute_sl_tp_live` and signal variant selection.
2. **Dynamic exit management** — not fixed. Live submits order with
   static SL/TP and walks away. Backtest runs trailing, breakeven,
   chandelier, partial, stale, session exits inside the Rust engine.
   Any trial that relies on these will diverge on exit.

Both must be closed for full parity.

---

## What works

### Entry signals — parity green
- `ff/signal_lib.py` runs on the same `build_signal_library` code path
  on both sides. Live re-evaluates on each main-TF bar close against
  the trailing buffer; backtest evaluates vectorised over history.
- `atr_pips` computation is identical (raw ATR / pip_value).
- `spread_at_fire_pips` snaps from the latest M1 bar's spread column on
  both sides.
- After today's fix, live filters hits to `best_trial["signal_variant"]`
  so only the trial's chosen variant fires — no more "first matching
  variant" drift.

### SL/TP math — parity green (as of this commit)
- `_compute_sl_tp_live` now reads `best_trial["engine"]["stop_loss"]`
  and `best_trial["engine"]["take_profit"]` when a frozen trial is
  present. Mirrors `ff/sl_tp.compute_sl_tp`.
- Supported selectors: `stop_loss.selector ∈ {fixed, atr}`,
  `take_profit.selector ∈ {rr, atr, fixed}`.
- Legacy fallback kept as `_compute_sl_tp_live_legacy` for safety; never
  hit when Deploy-to-live writes a proper `best_trial`.

### Spread & slippage — tracked, not yet closing the loop
- Live plan carries `spread_at_fire_pips`.
- Reconciler surfaces `mismatched_spread` / `mismatched_slippage` per
  trade and per pair. You can see the drift, you just can't act on it
  until you decide to calibrate per-pair.

### State sync — recovered
- Today's error: sync thread crashed silently on an old args signature
  (pre-parity-v2 code). Fix: thread now logs each failure to
  `artifacts/live/errors.jsonl` with a `consecutive_failures` counter,
  so the laptop can see "sync has been failing for 20 minutes".
- `live-state` branch pushes plans / tickets / state / errors /
  crashes every 60s once the thread is alive. Laptop puller pulls
  every 60s. No manual action needed after a restart.

### Reset Live Day — script exists, runs fine manually
- `scripts/reset_live_day.py` flattens all `fireforex` magic positions,
  archives `artifacts/live/` to `archive/<stamp>/`, wipes, restarts
  runner. Desktop shortcut exists but not all VPS users have it —
  running the script directly is fine.

---

## What does NOT work (and why)

### 1. Dynamic exit management — live-side gap

Backtest (Rust `simulate_trade_full`) runs these on every bar after
entry:
- Trailing stop (`trailing.selector=atr`, ATR × mult, activation
  threshold in pips)
- Breakeven move (after trigger pips, shift SL to entry + offset)
- Chandelier exit (after activation pips, ATR × chandelier_mult
  trailing from high)
- Partial close (close pct of size at trigger pips, let the rest run)
- Stale exit (close after N bars regardless)
- Session exit (flat at end of session window)
- Max bars cap

Live submits `order_send` with static SL/TP and stops. MT5 does nothing
dynamic. Every one of the knobs above = **silent parity break** in live.

The current deployed trial (`signal_variant 2423`) has:
- trailing **on**, ATR×0.625, activate 6.5 pips
- chandelier **on**, ATR×3.3, activate 19.875 pips
- breakeven **on**, trigger 23.875, offset 9.5
- partial **on**, 21.25% at 38.875
- stale, session, max_bars **off** (their groups on, but trial has
  `test=false`)

So three management features the backtest exploits are simply absent
in live. Live trades ride full SL or TP; backtest trades harvest early
on trailing / breakeven / partial.

This is the single biggest source of divergence the reconciler will
report once tickets come through.

### 2. Multi-position per pair — no cap

Runner fires a new plan on every main-TF bar close whose signal
condition is true. Previous plans' positions remain open until their
own SL/TP. Expected behaviour, but VPS's MT5 now has 29 positions
across ~10 pairs from stacking over the morning.

No `max_open_per_pair` knob exists. Adding one is an engine-side change
(so backtest respects it too) plus a live gate. Needs
`add-forex-knob` workflow.

### 3. Stale MT5 order comments (historical, now fixed in code)

Old code submitted `comment=plan_id`. `plan_id` contains `+` (UTC
offset and direction suffix) which MT5 rejects. Current code uses
`comment="fireforex"`. VPS was running the old file; `git pull + restart`
fixes it.

This is why 29 positions are open but `errors.jsonl` shows dozens of
rejected submits — the fired signals that happened to hit the old code
path were lost. Reset + pull + restart clears this.

### 4. Parameter deployment path was partial

Before this session the Deploy flow wrote `best_trial` into
`service_config.json` but the runner never loaded it. Fixed in:
- `ff/live/runner.py` — `LiveConfig.best_trial` + `PairState.best_trial`
- `ff/live/runner_service.py` — reads `best_trial` from service config
- `app/live_jobs.py` — laptop UI start accepts `best_trial`
- `app/routes.py` — `/live/start` forwards it

All existing tests pass (158/158).

### 5. Per-pair calibration not wired

One trial runs across 18 pairs. The trial was optimised on EUR_USD M15.
It will be wrong for JPY crosses (pip scale, volatility, spread). The
reconciler already shows per-pair mismatch magnitudes. `scripts/
calibrate_for_parity.py` exists as a starting point but does not flow
into Deploy's `overrides` dict.

### 6. No execution_delay_bars model

ICM publishes closed M1 bars ~30s after the minute ends. Backtest opens
at the next M1 bar's open; live opens 30s+ later. Shows up as
`mismatched_entry_price` + slippage. Not built because the drift may
not be worth modelling — needs a real data sample from a clean day
before deciding.

### 7. Trailing / breakeven etc. in live — not attempted

See (1). This is the biggest gap.

---

## What will break parity for FUTURE EAs

Any new EA that turns on any of these will diverge:

| Knob | Parity-safe in live today? | Reason |
|------|----------------------------|--------|
| Entry signal families | Yes | signal_lib shared |
| SL fixed pips | Yes | math is portable |
| SL ATR × mult | Yes | math is portable |
| TP fixed / ATR / RR | Yes | math is portable |
| Trailing stop | **NO** | MT5 doesn't trail |
| Breakeven move | **NO** | MT5 doesn't move SL |
| Chandelier | **NO** | MT5 doesn't trail |
| Partial close | **NO** | MT5 doesn't split |
| Stale exit | **NO** | MT5 has no bar clock |
| Session exit | **NO** | MT5 has no session logic |
| Max bars | **NO** | same |
| `max_open_per_pair` | **NO** | knob doesn't exist yet |
| `execution_delay_bars` | **NO** | knob doesn't exist yet |
| Per-pair param overrides | **NO** | Deploy path doesn't walk them |

Any "yes" is free. Every "no" needs explicit live-side work or a Deploy
guard that strips the knob before writing `service_config.json`.

---

## Options to close the parity gap

**Decision (2026-04-21, user): Option A. Build it.**

### Option A — live tick-loop exit manager (CHOSEN)

Build `ff/live/exit_manager.py`. Every poll interval:
1. Read open positions from MT5 (already have `fetch_open_positions`).
2. For each position, pull the matching plan's `best_trial` knobs.
3. Recompute SL based on trailing / breakeven / chandelier rules using
   the current M1 bar + time-in-trade.
4. Call `mt5.order_modify` if new SL differs from current.
5. Call `mt5.order_send` for partial closes when trigger hits.
6. Call `mt5.order_close` for stale / session / max_bars.

Mirrors Rust logic in Python. ~200–300 lines. One-time cost. Every
future dynamic-exit knob works automatically because it's the same
code path as the Rust engine — just re-implemented against live data.

### Option B — Deploy-time guard (short-term)

In the Deploy endpoint, inspect `best_trial["engine"]`. If any of
`trailing.test`, `breakeven.test`, `chandelier.test`, `partial.test`,
`stale.test`, `session.test`, `max_bars.test` is true, refuse deploy
with a clear error listing the un-portable knobs. User picks a trial
that avoids them.

Honest but restrictive. Rules out the best trials.

### Option C — expose Rust exit engine as a standalone pyo3 function

In `core/src/lib.rs`, export `evaluate_exit(state, bar, trial) ->
action`. Live calls it each poll. Hardest to build (Rust state
management, no bar-clock abstraction), highest confidence parity.

---

## Priority for next session

1. **Verify today's fixes work end-to-end.** After VPS reset + pull +
   restart, the next fired plan's SL should be exactly 50 pips on
   non-JPY, 50 pips × JPY-pip scale on JPY. Confirm via MT5 positions
   and plan file.
2. **Build Option A — `ff/live/exit_manager.py`.** Chosen approach.
   Mirror Rust engine's exit logic in Python against MT5 positions.
   Scope:
   - Pull deployed trial's engine dict from `service_config.json`.
   - Per-poll loop: for each open position, compute new SL from
     trailing / breakeven / chandelier rules using current M1 bar.
   - `mt5.order_modify` when SL changes.
   - Partial close via `mt5.order_send` (opposite side, pct of
     volume) when partial trigger hits.
   - Close via `mt5.order_close` on stale / session / max_bars.
   - Unit tests comparing Python output against Rust's
     `simulate_trade_full` trace on synthetic bars.
   - Wire into `ff/live/runner.py::_poll_pair` alongside
     `_evaluate_and_fire`.
3. **Per-pair calibration wired into Deploy.** Worst-drift pair first.
4. **`max_open_per_pair` knob via add-forex-knob.** Stops position
   stacking.
5. **execution_delay_bars.** Only if Step 1 shows >1 pip consistent
   drift on entries.

---

## State at end of this session

- Laptop: this commit ready to push. All tests green (158/158).
- VPS: still running stale code. 29 positions open. State sync dead.
  Awaiting reset + pull + restart sequence.
- User: non-technical. Walk in single steps, confirm after each.

### Exact sequence for the user to run on the VPS

Paste to VPS Claude:

```
1. cd "C:\Projects\Fire Forex"
2. schtasks /End /TN ff-live-runner
3. git fetch origin main
4. git reset --hard origin/main
5. .\.venv\Scripts\python.exe scripts\reset_live_day.py
6. schtasks /Run /TN ff-live-runner
7. (wait 60s) git log origin/live-state --oneline -3
   — expect a recent commit pushed by the sync thread
```

After step 7 prints a live-state commit, the loop is healthy.

### Exact sequence on the laptop after a VPS fire

1. Desktop **Restart Fire Forex** shortcut — fetches live-state, pulls
   plans/tickets into `artifacts/live/`, restarts web UI.
2. Browser: Live tab → first plan appears → click it → SL/TP in the
   row must equal `best_trial.engine.stop_loss.fixed.pips=50` and
   `take_profit = atr_pips * 2.375`.
3. After ~1h of trading, Reconcile tab should show matched entries on
   entry price + SL/TP, mismatches only on exits (trailing/breakeven
   not implemented live yet).

---

## Files touched in this session

- `ff/live/runner.py` — `LiveConfig.best_trial` + `PairState.best_trial`
  + `_compute_sl_tp_live` takes frozen trial path + variant filter +
  state_sync error surfacing
- `ff/live/runner_service.py` — passes `best_trial` into LiveConfig
- `app/live_jobs.py` — accepts `best_trial` kwarg
- `app/routes.py` — `/live/start` forwards `best_trial`

No test changes. All 158 tests still pass.
