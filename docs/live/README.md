# Live parity validator — VPS operator guide

This is the handover doc for running the Fire Forex live-parity runner on a
Windows VPS connected to an IC Markets MT5 demo account. Goal: prove the
backtest fires, fills, and closes the same as the live engine would —
within a bounded tolerance — across a dense set of trades.

## Prerequisites on the VPS

1. Windows Server (tested: 2022). `MetaTrader5` pip package is Windows-only.
2. Python 3.11 on PATH (`py -3.11 --version` should work).
3. Rust toolchain (`rustup`), needed for `maturin develop --release`.
4. MT5 terminal installed and **already logged into the IC Markets account**.
   The terminal session must stay running; the `MetaTrader5` Python package
   attaches to an active terminal, not a headless connection.
5. A local network tunnel to reach the web UI (tailscale or
   `ssh -L 8000:127.0.0.1:8000 vps`). The server is bound to 127.0.0.1 by
   design — no public exposure.

## One-time install

```powershell
git clone <repo> C:\FireForex
cd C:\FireForex
powershell -ExecutionPolicy Bypass -File scripts\vps_bootstrap.ps1
```

The bootstrap script:

- creates `.venv`,
- installs `requirements-web.txt` + `MetaTrader5` + `maturin`,
- rebuilds the Rust engine (`ff_core`),
- refuses to continue if `.env.live` is missing,
- registers two Scheduled Tasks: `ff-web` and `ff-live-runner`, triggered
  on system startup, restarting every 60s on failure.

## Credentials

`.env.live` lives on the VPS only and is in `.gitignore`. Never commit it.
Format:

```
MT5_LOGIN=52754648
MT5_PASSWORD=<demo password>
MT5_SERVER=ICMarketsSC-Demo
MT5_TERMINAL_PATH=C:\Program Files\MetaTrader 5 IC Markets\terminal64.exe
```

Rotating the password:

1. `schtasks /End /TN ff-live-runner`
2. Edit `.env.live`
3. `schtasks /Run /TN ff-live-runner`

## Starting a live session

1. Reach the web UI over the tunnel (e.g. `http://127.0.0.1:8000`).
2. Go to the **Live** tab.
3. First time only — write a `artifacts/live/service_config.json` on the VPS
   with the recipe, overrides, and pair list you want the runner to use.
   Example:

   ```json
   {
     "recipe": { "pair": "EUR_USD", "main_tf": "H1", "sub_tf": "M1", "level": 3 },
     "overrides": {},
     "pairs": [
       "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD",
       "NZD_USD", "USD_CHF", "EUR_JPY", "GBP_JPY"
     ],
     "poll_interval_sec": 10.0,
     "size_lots": 0.01,
     "deviation_pips": 3,
     "magic_number": 20260420,
     "symbol_map": { "EUR_USD": "EURUSD", "GBP_USD": "GBPUSD", "USD_JPY": "USDJPY" }
   }
   ```

   (The calibration script at `scripts/calibrate_for_parity.py` produces
   per-pair override dictionaries you can merge into `overrides`.)

4. `schtasks /Run /TN ff-live-runner` (or click **Start** in the Live tab).
5. Watch the **Plans** feed in the Live tab. Each live-fired plan matches a
   backtest trade by `(pair, direction, signal_bar_ts)`.

## Running the reconciler

From the web UI, click **Run reconcile** and enter the `run_id` of the
backtest whose trade log you want to diff against today's live activity.
The iframe below refreshes with the per-trade classification table.

From the command line:

```powershell
.venv\Scripts\python.exe -c "from scripts.run_reconcile import main; main()"
```

(Dedicated entry-point script lives in `scripts/run_reconcile.py` — not yet
shipped; fall back to calling `ff.live.reconcile.reconcile()` directly
until it is.)

## Log locations

- `artifacts/live/plans/YYYY-MM-DD.jsonl` — one plan per line.
- `artifacts/live/tickets.jsonl` — one MT5 submit outcome per line.
- `artifacts/live/state.json` — current open positions. Atomically rewritten.
- `artifacts/live/errors.jsonl` — non-fatal per-pair errors.
- `artifacts/live/crashes.jsonl` — uncaught exceptions. Scheduled Task
  restarts after each one.
- `artifacts/live/reconcile/YYYYMMDD_HHMMSS.{html,json}` — reconcile reports.

## Rollback / stop

1. `schtasks /End /TN ff-live-runner`
2. In the MT5 terminal, flatten any leftover open positions manually — the
   Python engine will not auto-close on shutdown.
3. Archive the day's logs if useful, then delete or rotate
   `artifacts/live/*` for the next session.

## Known residual parity risks

- **Data-feed mismatch (Dukascopy vs IC Markets).** Backtests run off
  Dukascopy M1; live runs off IC Markets M1. OHLC tends to differ by a few
  tenths of a pip per bar. The reconciler's default tolerance absorbs this,
  but large divergences imply a bad Dukascopy day. Consider pulling an ICM
  M1 parquet alongside Dukascopy for the pairs that drift most.
- **Sub-M1 trailing lag.** The backtest evaluates trailing SL on every M1
  close; the live runner re-polls on a 10s interval, so SL moves can lag by
  up to 10s. Flagged but not fatal — trailing trades may see a wider actual
  fill by the size of the lag.
- **MT5 server-side SL/TP.** The broker enforces SL/TP natively. If an SL
  fires between Python polls, the reconciler recovers the fill from
  `history_deals_get` on the next pass. Matches are on timestamp + price,
  not on who pulled the trigger.

## Upgrading the runner

```powershell
cd C:\FireForex
git pull
.venv\Scripts\maturin.exe develop --release  # if core/ changed
schtasks /End /TN ff-web
schtasks /End /TN ff-live-runner
schtasks /Run /TN ff-web
schtasks /Run /TN ff-live-runner
```

Always run a ≥48h reconcile-green pass on demo before pointing the runner
at a real account.
