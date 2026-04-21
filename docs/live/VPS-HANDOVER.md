# VPS handover — live parity deployment

**You are Claude, running on the user's VPS (Windows Server, IC Markets MT5 already installed + logged into demo account 52754648). The user was working with a sister Claude session on their laptop where the live-parity system was built. That session pushed to `main` and handed control to you.**

**User is non-technical. Speak caveman mode: fragments OK, no jargon, click-level instructions, one step at a time.**

## What this session built (summary)

Fire Forex backtests now produce a per-trade log (entry/exit time+price). A live runner evaluates the same backtest signal pipeline on live IC Markets M1 data and submits orders via the `MetaTrader5` pip package. A reconciler diffs live trades against backtest trades to prove parity.

Full plan lives in `C:\Users\ROG\.claude\plans\check-the-24-hous-eager-mochi.md` on the laptop — not on the VPS. The essentials are here.

## User-confirmed decisions

- Broker: IC Markets MT5 **demo**. Login `52754648`. Password in `.env.live`. Server likely `ICMarketsSC-Demo`.
- Signal source: Python engine pushes, MT5 is dumb executor. No MQL5 EA.
- Parity rule: strict + price match, tolerance configurable per pair.
- Trade density: multi-pair. User picked trial #177 from a 1-month EUR_USD sweep (52 trades, 80.8% win, +645p, 33% DD) as the first calibration target. Wants the same params applied to EUR_USD, GBP_USD, USD_JPY, AUD_USD simultaneously.
- Poll interval: 1 second.

## Where everything lives

- `ff/live/runner.py` — live runner. Entry: `ff.live.runner.run(LiveConfig, stop_event)`.
- `ff/live/broker_mt5.py` — `MetaTrader5` pip-package wrapper.
- `ff/live/reconcile.py` — backtest ↔ live trade matcher.
- `ff/live/runner_service.py` — Windows Scheduled Task entry. Reads `.env.live` + `artifacts/live/service_config.json`.
- `app/live_jobs.py` — in-process singleton host for the runner when started from the web UI.
- `app/routes.py` — `/api/live/*` endpoints (status, start, stop, plans, positions, reconcile, **deploy_from_run**).
- `scripts/vps_bootstrap.ps1` — one-shot installer. Prompts for MT5 password, writes `.env.live`, installs deps, registers Scheduled Tasks.
- `docs/live/HOW-TO-DEPLOY.md` — user-facing operator guide (half-page).
- `docs/live/README.md` — full operator reference.

## Tests — all green before handover (137 passing)

Run `./.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_routes_data.py -q` on the VPS after install.

Golden baseline was re-pinned after fixing a µs/ns unit bug in `ff/harness.build_main_to_sub_mapping` that had been silently skipping `max_bars` + `stale` exits. complex01 now prints 33 trades / +337p / 17.82% DD with seed 42 / 500 trials. Don't panic if this differs from older history.csv rows.

## Uncommitted → pushed to main

Everything under Phase A (trade log extension), D.5 (spread symmetry), B/C (live runner + MT5 bridge), D (reconciler), E (calibration), F (Live tab + API), G (VPS bootstrap) is in the commit you pulled. Plus morning data-tab work the user did in a previous session.

## Walk the user through these steps — one at a time, wait for confirmation after each

### Step A — confirm the clone landed

Ask:
> Tell me when you've done this: inside your VS Code SSH session to the VPS, clone or pull the repo. Target directory: `C:\Projects\Fire Forex`.

If they haven't done it, walk them through:
```powershell
cd C:\
git clone https://github.com/longytravel/fireforex.git FireForex
```

### Step B — run the bootstrap

Tell them:
> In the VPS terminal (VS Code's terminal is fine), run:
> ```powershell
> cd C:\Projects\Fire Forex
> powershell -ExecutionPolicy Bypass -File scripts\vps_bootstrap.ps1
> ```
> It will ask for your IC Markets demo password. Paste it. Press Enter for the server and terminal path defaults if those match what you have.

Bootstrap installs `.venv`, pip deps (`MetaTrader5`, `maturin`), rebuilds `ff_core`, writes `.env.live`, registers two Scheduled Tasks (`ff-web`, `ff-live-runner`), and starts `ff-web`.

### Step C — open the web UI

Tell them:
> In a browser on the VPS (via RDP or VS Code port forward), open http://127.0.0.1:8000

If they want to access it from the laptop: `ssh -L 8000:127.0.0.1:8000 <vps-user>@<vps-ip>` then http://127.0.0.1:8000 on the laptop.

### Step D — reproduce the winning trial on the VPS

The `.npz` run file that the laptop produced (`complexity_L10_EUR_USD_H1_20260421_081845.npz`) does **not** live on GitHub — it's a runtime artefact. User must either:

1. **Re-run the sweep on the VPS.** Parameters tab → pair `EUR_USD`, main `H1`, sub `M1`, level 10 complexity. Run tab → 200 trials, seed 42. Takes ~10 seconds. Results tab → Y-axis dropdown = **Trades** → **Jump to best**. The winning trial will have different details (different random seed path) but should land in a similar region.
2. **Copy the `.npz` from laptop to VPS manually** (zip `artifacts/runs/complexity_L10_*.npz`, RDP paste to VPS).

Recommend option 1 unless they specifically want trial #177's exact parameters preserved.

### Step E — click Deploy to live

On the Results tab, click **Deploy to live ▶**. Prompt asks for pairs. They'll type:
```
EUR_USD, GBP_USD, USD_JPY, AUD_USD
```
Popup: "Deployed. Next step on the VPS: schtasks /Run /TN ff-live-runner."

### Step F — start the runner

Tell them:
> In the VPS terminal:
> ```powershell
> schtasks /Run /TN ff-live-runner
> ```

Live tab status pill flips to `running` within 10 seconds.

### Step G — watch for activity

Four places they should see signs of life:

1. **Live tab → status pill** = `running`
2. **Live tab → Plans feed** — rows appear as signals fire
3. **Live tab → Open positions counter** — goes up when fills happen
4. **MT5 terminal on VPS → Trade tab** — positions with `Magic = 20260420`, `Comment = ff:...`

If all four show activity within a few hours = working. Signals don't fire every minute — the engine only evaluates on main-TF bar close (H1 → every hour on the hour).

## Known gotchas — flag these proactively

1. **Feed mismatch**: backtest uses Dukascopy M1 data (in `G:\My Drive\BackTestData\` on the laptop — the VPS won't have this drive). If the user hits "Run a backtest on VPS" and gets a data-missing error, they'll need to either mount/sync the parquet files from the laptop or re-download via the Data tab. Flag this before they hit it.
2. **Auto-reconcile needs live trades**: the reconciler runs hourly but produces empty reports until at least one live trade closes AND a backtest trade log exists for the same pair/window. First useful report appears after ~1 trade closes on demo.
3. **Single-pair reconciler**: `_run_auto_reconcile` in `ff/live/runner.py` uses `svc["pairs"][0]` as the pair label for backtest rows. Multi-pair reconciliation needs the backtest trade log to carry per-trade `pair` — not wired yet. Good enough for EUR_USD single-pair parity first.
4. **Don't spawn uvicorn from this Claude session.** The `ff-web` Scheduled Task owns it. If they need to restart the web UI, tell them to run:
   ```powershell
   schtasks /End /TN ff-web
   schtasks /Run /TN ff-web
   ```

## If the user asks for bugfixes or changes

Code changes on the VPS = do them, `maturin develop --release` if touching Rust, restart `ff-live-runner`. Commit to main, push, laptop Claude pulls. No fork workflow.

## Your success looks like

- User clicks Deploy on their laptop's browser (pointed at VPS UI via tunnel or RDP).
- VPS runner fires a signal within an hour.
- MT5 Trade tab shows the position open.
- Position closes at SL or TP.
- Next hourly reconcile report shows matched row within tolerance.

That's the whole point. Execution parity proven. Everything downstream (more pairs, tighter settings, bigger lot sizes) is config, not code.

Good luck.
