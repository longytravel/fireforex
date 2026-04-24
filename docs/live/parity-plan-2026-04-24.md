# Live ↔ Backtest Parity Plan — 2026-04-24

**Status on 2026-04-24 20:40 UTC** — forensic reconciliation is working for
Dukascopy (10/10 closed trades matched with narrative explanations). MT5
replay correctly shows 0/10 matches for pre-forming-fix trades (which is
what validates that `a66b211` was the right fix). The forming-candle fix is
deployed on VPS but today's closed trades are all pre-fix; post-fix trades
are still open.

This doc is the **durable plan** — safe to clear context, then pick up
here next session.

---

## Architectural principles (do not change without a reason)

1. **Dukascopy is the main backtester.** It has years of history. The
   whole optimisation/sweep system depends on long windows. MT5 history
   is broker-capped (~42h at IC Markets) so it cannot replace Dukascopy
   for strategy search.

2. **Reconciliation uses BOTH data sources, side-by-side.** The point of
   reconcile is to SEE the differences and understand them — not to
   pick a winner. Keep Dukascopy *and* MT5 replay columns in every
   reconcile report.

3. **MT5 data always in sync, zero manual setup.** The laptop MT5
   terminal might not be open. The VPS always has it open (live runner
   is live on it). So VPS is the source of truth; laptop pulls from VPS
   before every reconcile.

4. **Live trades via VPS MT5; reconcile runs locally.** VPS = execution
   only. Laptop = analysis and UI. Never run reconcile on VPS.

---

## Current state (what actually exists)

### Working
- `scripts/build_trade_comparison.py` — joins VPS artifacts + replay NPZ
  and writes the clear-view HTML.
- `scripts/build_forensic_report.py` — per-trade narrative report (fire
  timing, slippage, spread, tick vs bar resolution, broker-vs-data
  divergence). Served at `/api/live/reconcile/latest.html`.
- `ff/data/mt5_m1_downloader.py` patched: `FF_MT5_SKIP_DOWNLOAD=1` falls
  back to on-disk parquet when MT5 terminal isn't reachable.
- Forming-candle fix (`a66b211`) deployed: live skips the still-forming
  M1 candle so it agrees with closed-bar replay.
- Broker-time fix (`dc6e7cc`): VPS syncs MT5 deals using broker UTC+3
  time, no more missed closes.
- BOM tolerance in `reconcile_live.py` and `ff/replay.py`.

### Known gaps (in order of impact)

| # | Gap | Observed | Impact |
|---|-----|----------|--------|
| G1 | Laptop MT5 parquet is frozen at whatever was last synced from VPS | 15:39 UTC data on laptop; post-15:39 closes can't be reconciled | Needs fix before tier 2 |
| G2 | All 10 closed trades fired from provisional M15 bar (pre-fix) | MT5 replay 0/10 correctly can't reproduce | Self-heals once post-fix trades close |
| G3 | Dukascopy replay data window runs out mid-trade | GBP_NZD #1611174277 BT exit = NONE at the data edge, not a real close | Needs wider fetch window |
| G4 | Broker vs Dukascopy price path diverge → SL hit at different times | GBP_JPY 81 min; EUR_NZD 66 min | Inherent. Not a bug — this is exactly what reconcile is meant to surface |
| G5 | BT exits at M1 bar close; live exits mid-minute on tick | 10-60s drift on every clean SL hit | Solvable with tick data on recon window only |
| G6 | BT assumes zero execution latency | Measured 287ms-19s fill latency live; worst case = 2 pips adverse | Solvable with latency model |
| G7 | BT assumes fixed spread; live spread varies | BT spread 1.8-4.8 pips vs live tick spread | Solvable by reading `spread` column from M1 parquet |
| G8 | Chandelier stop evaluates at M15 bar close in BT; live uses tick | 19s Δ on chandelier exits | Subsumed by G5 |

---

## Data strategy (answers the tick-data question)

**Two-tier data model — keep them separate, don't conflate.**

### Tier A — strategy search / optimisation (unchanged)
- **Source:** Dukascopy M1 parquet on `G:\My Drive\BackTestData\`
- **Window:** years
- **Speed priority:** critical (sweep = millions of trials)
- **Stays M1.** Tick-level in the main harness would make sweeps
  unaffordable.

### Tier B — reconciliation replay (for closed trades)
- **Source:** both Dukascopy + MT5 M1 parquets on `G:\My Drive\BackTestData_MT5\`
- **Window:** just enough to cover the deployed trading window + exit paths (today + 1-2 days)
- **Speed:** fast enough, M1 works

### Tier C — forensic exit-timing replay (NEW — closes G5 & G8)
- **Source:** tick data, both Dukascopy and MT5
- **Window:** only the window of each individual closed trade (typically 30 min to 3 hours)
- **Volume:** ~10-50 trades/day × 3h × 60 ticks/sec ≈ 2M ticks/day total. Trivially small.
- **What it does:** for each closed trade, replay the M1 backtest window at
  tick resolution so SL/TP/chandelier exits are evaluated millisecond-
  accurate. Eliminates the 10-60s "same SL, different time" noise in G5
  and G8.
- **What it won't fix:** the 81 min / 66 min broker-vs-Dukascopy price
  path divergence (G4) — that requires tick from the actual broker, which
  MT5 tick data provides. Dukascopy tick data will still differ from IC
  Markets tick data because they're different liquidity providers.

**Decision: build Tier C.** It's small, fast, and closes a real gap.
Backtest speed is NOT affected because main sweep stays Tier A (M1). Only
reconciliation uses tick.

---

## Execution phases

Each phase has a clear entry/exit criterion. Don't skip ahead.

### Phase 1 — Always-fresh data (no manual setup)
**Goal:** every reconcile starts with data as fresh as the VPS live runner saw.

- [ ] **1.1** Add `scripts/sync_vps_mt5.ps1` — scp-rsync VPS
  `BackTestData_MT5/*.parquet` → laptop `G:\My Drive\BackTestData_MT5\`.
  Idempotent. Should take <30s.
- [ ] **1.2** `scripts/reconcile_live.py` calls 1.1 at start unless
  `--skip-sync` is passed. Remove `FF_MT5_SKIP_DOWNLOAD` env dance — it
  just becomes the natural path.
- [ ] **1.3** Extend Dukascopy top-up in `_ensure_data` so the window
  always covers `now + 2 hours` buffer. Closes G3.
- [ ] **1.4** One-command "refresh and rebuild": new script
  `scripts/reconcile_all.py` that syncs VPS → runs Duka + MT5 reconcile
  for every active instance → regenerates forensic report.

**Exit criterion:** `python scripts/reconcile_all.py` produces a fresh
forensic report with no stale-data warnings, in <2 min.

### Phase 2 — Validate the forming-candle fix
**Goal:** prove `a66b211` closes the loop.

- [ ] **2.1** Wait for 3+ post-fix trades to close (trades opened after
  ~17:49 UTC 2026-04-24 that fire *after* their M15 bar close).
- [ ] **2.2** Re-run forensic on those. Acceptance: MT5 replay reproduces
  each signal (match_status=exact_signal_bar), and Δpnl within 2 pips for
  ≥80% of them.
- [ ] **2.3** If acceptance fails, record the specific failure pattern
  and route back to Phase 3.

**Exit criterion:** a set of post-fix closed trades has MT5 replay
matches at ≥80%.

### Phase 3 — Close the minute-level drift (tick-data replay)
**Goal:** make BT exits agree with live exits to within 5 seconds where
the price path isn't genuinely divergent.

- [ ] **3.1** Dukascopy tick downloader — short-window only. Write
  `ff/data/tick_duka_downloader.py` keyed on (pair, start_ts, end_ts).
  Store in `G:\My Drive\BackTestData_Ticks\<pair>_<start>_<end>.parquet`.
- [ ] **3.2** MT5 tick downloader — use `copy_ticks_range`. Mirror shape.
- [ ] **3.3** New exit-replay function in `ff/replay.py`:
  `replay_trade_window(plan, ticket, data_source)` — takes ONE trade,
  pulls tick data covering the live trade window ± 30 min, re-runs the
  exit logic (SL/TP/chandelier) at tick resolution. Returns exit_price
  and exit_ts at millisecond precision.
- [ ] **3.4** `build_forensic_report.py` calls 3.3 for every closed trade
  and adds two new columns: `duka_tick_exit_sec_delta`,
  `mt5_tick_exit_sec_delta`.
- [ ] **3.5** Narrative block updates: "tick-level exit agreed within
  Xs" vs "tick-level exit also differs by Y min → real broker-path
  divergence".

**Exit criterion:** for trades where BT and live disagree on exit time,
the tick-level replay either (a) resolves the disagreement to <5s, OR
(b) confirms it's a real broker-vs-data divergence (G4). Both outcomes
are successes — the point is to KNOW.

### Phase 4 — Model the last of the live mechanics
**Goal:** close Δpnl gap to <1 pip on all but genuinely-divergent trades.

- [ ] **4.1** Execution latency knob: add `engine.execution.latency_ms`
  to the schema. BT advances entry_sub_bar_index by that latency before
  computing fill price. Default: median of measured live latencies (seed
  from real fills).
- [ ] **4.2** Variable spread: BT already reads `spread_entry_pips`; just
  make sure M1 parquet's `spread` column is used instead of any fixed
  fallback.
- [ ] **4.3** SL/TP rounding to broker pip-precision (5 / 3 decimals).

**Exit criterion:** median Δpnl between live and MT5 tick-replay ≤ 1 pip.

### Phase 5 — Parity monitoring (rolling)
**Goal:** notice drift the moment it happens.

- [ ] **5.1** Rolling 7-day chart in the UI: Δpnl per trade, live vs
  MT5-tick BT.
- [ ] **5.2** Alerts if median Δpnl > 2 pips or stddev > 5 pips over the
  last 24h. Routes to the notification channel.

---

## Files / locations

| Path | Role | Editable? |
|------|------|-----------|
| `ff/replay.py` | Replay harness; gates data download + BT | yes |
| `ff/data/mt5_m1_downloader.py` | MT5 M1 pull | yes |
| `ff/data/m1_bi5_downloader.py` | Dukascopy M1 pull | yes |
| `ff/data/tick_duka_downloader.py` | Dukascopy tick (Phase 3) | **new** |
| `ff/data/mt5_tick_downloader.py` | MT5 tick (Phase 3) | **new** |
| `scripts/reconcile_live.py` | Orchestrates backtest replay + 3-way diff | yes |
| `scripts/reconcile_all.py` | Phase 1 one-command entrypoint | **new** |
| `scripts/sync_vps_mt5.ps1` | VPS → laptop parquet sync | **new** |
| `scripts/build_trade_comparison.py` | Clear-view comparison HTML | yes |
| `scripts/build_forensic_report.py` | Per-trade narrative HTML | yes |
| `artifacts/live/<instance>/` | VPS plans/tickets/deals (gitignored) | do not touch |
| `artifacts/replay/<run>/<stamp>_<source>/trades.npz` | Replay output | do not touch |
| `artifacts/live/reconcile/latest.html` | UI-served forensic page | auto-generated |
| `G:\My Drive\BackTestData\` | Dukascopy M1 (main backtester data) | read-only after fetch |
| `G:\My Drive\BackTestData_MT5\` | MT5 M1 (laptop cache; synced from VPS in 1.1) | read-only after sync |
| `G:\My Drive\BackTestData_Ticks\` | Tick parquets (Phase 3) | read-only after fetch |

---

## What NOT to do

- Don't run reconcile on VPS. VPS is execution only.
- Don't change the Dukascopy-based main backtester. Strategy sweeps need
  long windows and M1 speed.
- Don't remove either data source from the forensic report. Both must
  appear side-by-side.
- Don't add "if broker disagrees with Dukascopy, trust broker" logic.
  Show both; let the user read the report.
- Don't store tick data indefinitely. Tick parquets are per-trade-window
  only. Delete/rotate after 30 days.
- Don't spawn a local uvicorn. Ask the user to run
  `scripts\ff_restart_server.ps1` if the UI needs a restart.

---

## Open questions for next session

1. **Tick-data volume** — MT5 `copy_ticks_range` caps at broker-defined
  history. Need to verify IC Markets exposes enough tick data for yesterday +
  today. If not, fall back to second-resolution by interpolating M1.
2. **Scheduled re-run** — should Phase 5 run on cron, or wake up on
  VPS-side close events? (Prefer close-event trigger; lower latency,
  lower cost.)
3. **How many post-fix trades** before we declare the forming-candle fix
  validated? Gut feeling: 10-20. Need clean-pair coverage too (at least
  one GBP pair post-fix so we test where the pre-fix drift was
  concentrated).

---

## Quick-start next session

```
cd "C:\Users\ROG\Projects\Fire Forex"
# 1. Sanity: what's new since last session?
git log --oneline f37d35c..HEAD

# 2. See the forensic report as it stands
# Open: http://127.0.0.1:8000/api/live/reconcile/latest.html

# 3. Start Phase 1 — Task 1.1 is the first concrete step.
#    sync_vps_mt5.ps1 is greenfield.
```

Recent commits you should know about:

```
e32b987 feat: forensic reconciliation report
(commit removing leaked logs)
f421aec feat: annotate pre-forming-fix trades in comparison report
f37d35c feat: refine trade-comparison verdicts
6edaf4f fix: match trades directly against BT replay NPZ
dd082e1 feat: add build_trade_comparison script + gitignore
8e00e2a fix: tolerate windows bom in replay + reconcile scripts
dc6e7cc fix: query mt5 deal history in broker time
a66b211 fix: skip forming mt5 bars in live runner
```
