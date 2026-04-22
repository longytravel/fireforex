# Live vs Backtest Reconcile — usage

One-page guide to the three-way parity loop:

1. Backtest in the UI → deploy the pinned run to live.
2. Live runner fires trades, close events land in
   `artifacts/live/<instance>/plans/*.jsonl` + `tickets.jsonl`.
3. **Here**: post-session, replay the same config against two data
   sources and compare live ↔ both backtests.

This answers the only question that matters:

> Do the backtested trades match the forward-traded results?

## Shape

```
  Dukascopy M1              MT5 M1
 (bi5 downloader)       (mt5_m1_downloader)
        │                       │
        ▼                       ▼
   DATA_ROOT              MT5_DATA_ROOT
   *_M15.parquet          *_M15.parquet
        │                       │
        └─────────┬─────────────┘
                  │  harness.run(data_source=...)
                  │
         ┌────────┴────────┐
         ▼                 ▼
   duka-BT trades    mt5-BT trades
         │                 │
         │   live plans ─┐ │
         │   (VPS)       │ │
         ▼               ▼ ▼
   ┌─────────────────────────────────────┐
   │   ff.live.reconcile.reconcile()     │
   │                                     │
   │   A. live   vs duka-BT   ← parity   │
   │   B. live   vs mt5-BT    ← sanity   │
   │   C. duka-BT vs mt5-BT   ← drift    │
   └─────────────────────────────────────┘
              HTML + JSON per pass
```

## Where it all runs

Everything runs on the **laptop**, not the VPS. The VPS holds none of the
Dukascopy parquet (`G:\` drive is laptop-only) and doesn't need the MT5
history. Live trades are pulled from VPS via scp before reconcile.

## End-to-end command sequence

```powershell
# 1. Sync live artifacts from VPS → laptop. Plans/tickets/state only.
scp -r administrator@<vps>:"C:/Users/Administrator/Fire Forex/artifacts/live/<instance>/" `
    "C:\Users\ROG\Projects\Fire Forex\artifacts\live\"

# 2. Top up MT5 history for the live window. Windows-only; MT5 terminal
#    must be open + logged in. Skip if you already pulled MT5 today.
.\.venv\Scripts\python.exe scripts\fetch_mt5_history.py --pair EUR_USD --days 30

# 3. Run the three-way reconcile. Duka data is topped up inside the replay.
.\.venv\Scripts\python.exe scripts\reconcile_live.py --data-source both
```

Outputs land in `artifacts/live/<instance>/reconcile/`:
- `<stamp>_A_live_vs_duka.html` / `.json` — the parity question.
- `<stamp>_B_live_vs_mt5.html` / `.json` — live against a different data
  source, to separate strategy drift from data-source drift.
- `<stamp>_C_duka_vs_mt5.html` / `.json` — pure data-source drift, live
  excluded. Quantifies the Duka-vs-MT5 pip delta already seen (~6-13
  pips on AUD_CAD mid-session).

## One-time setup on first use

Existing deployed configs carry only the bare `signal_variant` int —
migrate them to fingerprint form so new live runs fire the right
strategy:

```powershell
.\.venv\Scripts\python.exe scripts\migrate_best_trial_fingerprint.py
```

Idempotent — re-running is a no-op. Don't re-run unless a config was
redeployed from a sweep that predates the fingerprint fix.

## Flags

| `--data-source` | Behaviour |
|---|---|
| `dukascopy` (default) | Single replay, single report. Backwards-compatible with pre-MT5 usage. |
| `mt5` | Single replay using MT5 parquet, single live-vs-mt5 report. |
| `both` | Two replays, three reports (A/B/C above). |

| `--skip-replay` | Reuse the most recent replay NPZ for that data source. Pulls from `latest_stamp_<source>.txt`. |
| `--instance <id>` | Pick a specific instance dir when multiple are active. |

## Expected outcomes

- **Report A matched > 0** is the pass condition. Bar-exact match on
  `(pair, direction, signal_bar_ts)` for every live plan. Entry-price
  delta in pips is surfaced — broker fill vs bar-close drift is expected
  and not corrected for.
- **Report A matched = 0** → you're either (a) still on a legacy config
  that missed the fingerprint migration, (b) running a signals_cfg drift
  from the one that produced the pinned NPZ, or (c) seeing the original
  variant-id bug (see `BUG-variant-id-not-stable-2026-04-22.md`).
- **Report C pip delta > 15 pips** on a major → broker outage or MT5
  feed stale. Refetch MT5 and retry.

## Why reconcile-on-laptop, not on the VPS

- VPS has no `G:\My Drive\BackTestData` — `harness.DATA_ROOT` resolves
  to a laptop-only path.
- VPS has no MT5 history puller configured — the MT5 terminal runs
  there for live trading only.
- The VPS auto-reconciler thread
  (`ff/live/runner.py::_spawn_auto_reconciler`) fails silently on the
  VPS for exactly these reasons. Treat it as a no-op there.
