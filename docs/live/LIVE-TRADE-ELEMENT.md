# Live trade element — component reference

Focused technical doc for the live trading layer. Read this when you need to
debug / extend / reason about what the live runner is actually doing.

## What it is

A Python process on the VPS that:

1. Polls IC Markets MT5 for the latest M1 bars across N pairs (every 1s).
2. Rolls those up to the main-TF (default M15 or H1) and evaluates the Fire
   Forex signal library on the newest closed bar.
3. If a signal fires, submits a market order to MT5 via `order_send`, with
   native server-side SL/TP.
4. Logs a "plan" and a "ticket" per fill for later reconciliation.

No MQL5 EA. No ZeroMQ. No DLL. Everything via the `MetaTrader5` pip package
talking to the locally installed MT5 terminal on the same VPS.

## Data flow

```
MT5 terminal (IC Markets demo)
        │
        │  mt5.copy_rates_from_pos(symbol, TIMEFRAME_M1, 0, N)
        ▼
ff.live.broker_mt5.MT5Broker.copy_rates_m1()
        │  (broker→UTC offset applied)
        ▼
ff.live.runner.PairState.m1_buf   (per-pair rolling M1 DataFrame)
        │  _rollup_main_tf()
        ▼
ff.live.runner.PairState.main_buf (per-pair main-TF DataFrame)
        │  build_signal_library() on trailing window
        ▼
SignalLibrary.bar_index == len(main_buf)-1 ?
        │  yes → compute SL/TP via _compute_sl_tp_live()
        ▼
emit trade plan → artifacts/live/plans/YYYY-MM-DD.jsonl
        │  MT5Broker.submit_market_order()
        ▼
artifacts/live/tickets.jsonl
artifacts/live/state.json (open positions)
```

## Key modules

| Module | Responsibility |
|---|---|
| `ff/live/runner.py` | Main loop. `LiveConfig`, `PairState`, `_poll_pair`, `_evaluate_and_fire`, auto-reconcile thread. |
| `ff/live/broker_mt5.py` | MT5 bridge. Connect, copy_rates_m1, submit_market_order, modify_sl, close_position, fetch_recent_deals. |
| `ff/live/reconcile.py` | Join backtest trade log ↔ live tickets+deals. Classify matched/missing/extra/price-mismatch. |
| `ff/live/runner_service.py` | Windows Scheduled Task entry point. Reads `.env.live` + `service_config.json`, runs `runner.run()` in a blocking thread, logs crashes. |
| `app/live_jobs.py` | In-process host for starting the runner from the web UI (not used in the VPS deploy path). |
| `app/routes.py::/api/live/*` | Web endpoints: status, start, stop, plans, positions, deploy_from_run, reconcile. |

## Per-file state on the VPS

```
C:\Projects\Fire Forex\artifacts\live\
├── service_config.json   # runtime config (recipe, pairs, broker profile keys)
├── pinned_run.json       # source_run_id for auto-reconcile
├── params_pinned.json    # best-trial param vector (if written)
├── state.json            # open positions, atomically rewritten on every change
├── plans/
│   └── YYYY-MM-DD.jsonl  # one line per signal fired today
├── tickets.jsonl         # one line per MT5 submit result (retcode, fill price)
├── errors.jsonl          # non-fatal per-pair errors (rejected orders etc.)
├── crashes.jsonl         # uncaught exceptions — Scheduled Task restarts after
└── reconcile/
    ├── YYYYMMDD_HHMMSS.html  # per-run reconcile report
    ├── YYYYMMDD_HHMMSS.json
    └── latest.html           # copy of the most recent
```

`service_config.json` is the single source of runtime truth. It's written by
the laptop's Deploy button (and mirrored to `deploy/live_config.json` which
travels via git to the VPS).

## Config shape

```json
{
  "source_run_id": "complexity_L10_EUR_USD_M15_20260421_095645",
  "recipe": { "pair": "EUR_USD", "main_tf": "M15", "sub_tf": "M1", "level": 10 },
  "overrides": { },
  "pairs": ["EUR_USD", "GBP_USD", ...],
  "best_trial": { "signal_variant": 1, "engine": { ... } },
  "poll_interval_sec": 1.0,
  "size_lots": 0.01,
  "deviation_pips": 3.0,
  "magic_number": 20260420,
  "symbol_map": { "EUR_USD": "EURUSD" },
  "auto_reconcile_interval_min": 60
}
```

- `pairs` can be expanded beyond the `recipe.pair` — the runner uses the
  recipe's pair only as the calibration reference and applies the same
  parameter set to every entry in `pairs`.
- `symbol_map` lets you override MT5 symbol naming if the broker uses
  suffixes (`EURUSD.a`, `-cent`, etc.). Empty map → `pair.replace("_", "")`.
- `magic_number` tags every order so downstream filters can find Fire Forex
  positions quickly.
- `auto_reconcile_interval_min` drives the background thread that runs the
  reconciler every N minutes.

## What the runner loop actually does (one cycle)

For each pair in `cfg.pairs`:

1. `broker.copy_rates_m1(pair, N)` → fetch last N M1 bars (broker→UTC converted).
2. Merge into `state.m1_buf`, dedupe on index, trim to the retention window.
3. `_rollup_main_tf(m1_buf, main_tf_minutes)` → `state.main_buf`. The last
   bar is dropped if the rolling M1 hasn't yet reached its right edge (i.e.
   the bar is still in-progress).
4. If the last `main_buf` timestamp is newer than `state.last_main_ts` → a
   new main-TF bar has just closed.
5. Build a signal library on the trailing `main_buf` window. Filter results
   to `bar_index == len(main_buf)-1`.
6. If a signal fires: compute SL/TP via `_compute_sl_tp_live(ea, direction,
   entry_ref_price, atr_pips, pip_value)`, emit plan, submit order.
7. Repeat for next pair.
8. Sleep `max(0.5, poll_interval_sec - elapsed)`.

Heartbeat thread logs uptime + open-positions count every 30s.
Auto-reconcile thread runs `reconcile()` every `auto_reconcile_interval_min`
and writes `reconcile/<stamp>.html`.

## Execution realism — what the runner models vs doesn't

| Aspect | Status |
|---|---|
| Entry timing: at main-TF bar close | ✔ signal fires on closed bar |
| Entry price: first M1 open inside next main-TF interval | ✘ live fills at market when Python wakes up, ~30s–2s after bar close |
| SL/TP enforcement | ✔ server-side via `sl` / `tp` fields on the order |
| Spread | Partial — broker applies real ask/bid, backtest reads Dukascopy spread column; drift 0.1–1 pip |
| Slippage | ✔ `deviation_pips` param on `order_send` (rejected if exceeded) |
| Commission | Not modelled on the live side yet — broker applies it invisibly |
| Partial fills | Detected (`result.volume < request.volume`); logged, treated as open at actual size |
| Requote | Retried once with `deviation * 2`, then abort |
| Reject | Logged, no retry |
| Disconnect | Status flip to "degraded", retry `mt5.initialize()` every 30s |

## Debugging

1. **Runner dead?** `schtasks /Query /TN ff-live-runner` → Status. `Ready`
   = not running. `Running` = alive. The `Check Fire Forex` desktop bat
   wraps this.

2. **Runner running but no plans?** Wait one main-TF bar. If a full interval
   passes with no plans, the signal library isn't firing — check the
   `overrides` in `service_config.json`; a too-strict filter can zero out
   hits.

3. **Plans fire but no positions?** Read `errors.jsonl`. Common retcodes:
   - `-2 Invalid "comment"` — fixed in commit 24d8765.
   - `10014 Invalid volume` — lot size below broker minimum. Raise `size_lots`.
   - `10016 Invalid stops` — SL/TP too close to current price. Engine uses
     ATR-scaled stops so this normally self-corrects.
   - `10021 No prices` — market closed for that symbol (weekends, news blackouts).

4. **Position opens but closes at weird price?** SL/TP was hit between polls;
   MT5 enforced it server-side. Check `mt5.history_deals_get(since_ts, now)`
   to see the fill price. The reconciler does this on every run.

5. **Plan timestamps off by 2–3h?** Broker↔UTC offset not applied. Look for
   `[mt5] broker-UTC offset detected: ±Ns` in the runner log. If missing,
   the probe tick failed (symbol_info_tick returned None). Confirm
   `EURUSD` is in Market Watch on the MT5 terminal.

6. **Tests for the live layer:**
   - `tests/test_live_runner_synthetic.py` — bar ingest + rollup plumbing,
     no MT5.
   - `tests/test_reconcile.py` — match categories.

## Extending

- **New pair:** add to `pairs` in `service_config.json`. If broker uses a
  suffixed symbol, add to `symbol_map`. Redeploy.
- **New main-TF:** change `recipe.main_tf` in `service_config.json`.
  Supported: any value in `TF_MINUTES` (M1 M5 M15 M30 H1 H4 D W).
- **New SL/TP mode:** `_compute_sl_tp_live` currently handles ATR-distance SL
  and RR-ratio TP. For fixed-pip SL / ATR-mult TP, add branches there.
- **Per-pair calibration:** hold an `overrides_by_pair: {pair: {...}}` dict in
  `service_config.json`, merge it into `cfg.overrides` inside
  `_build_pair_state` before `apply_overrides`.
- **Tick-level polling:** replace `broker.copy_rates_m1` with a
  `mt5.symbol_info_tick` loop. Lots of plumbing; only do this if the current
  ~30s floor becomes a parity problem.

## Known parity gaps (open work)

1. **Backtest data feed mismatch.** Dukascopy M1 vs IC Markets M1 → few
   tenths of a pip per bar. Fix: add `ff/data/icm_m1_downloader.py` that
   pulls via `mt5.copy_rates_range` into
   `G:\My Drive\BackTestData\{pair}_M1_ICM.parquet`. Parity runs load the
   ICM version.
2. **Engine fill timing.** Engine fills at M1 open after signal bar close;
   live fills ~30s+ later. Add an `execution_delay_bars` knob to
   `simulate_trade_full` so the engine fills at `h1_to_sub_start[entry_bar]
   + delay` instead of `h1_to_sub_start[entry_bar]`.
3. **Backtest trade log has no pair column.** Reconciler can't group live
   trades by pair against a single-pair backtest. Add `pair` to
   `ff.harness._build_best_trade_log()`.
4. **Spread asymmetry.** Engine subtracts sell-side spread from exit_price
   (commit landed earlier today); the buy-side is already handled on entry.
   Residual drift comes from bid/ask vs broker's actual quote during
   volatile moments — reconciler tolerance should absorb this.
5. **Commission.** Engine takes `commission_pips` as a flat knob; live
   broker applies a variable commission. Add a post-fill commission field
   to tickets.jsonl, pulled from `mt5.history_deals_get`.

## One-line lifecycle summary

> The runner is a stateless poller over MT5 M1 data that runs the backtest
> signal pipeline on every closed bar, submits orders with server-side
> SL/TP, and writes enough paper trail for the reconciler to later prove
> each live fill matches (within tolerance) what the backtest said it
> should have been.
