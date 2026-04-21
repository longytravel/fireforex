# Deploy Fire Forex live — the short version

Read nothing else. Do these steps.

## On the VPS (once)

1. Copy the repo to `C:\FireForex` (clone, or zip + unzip).
2. Right-click `Start` → `Windows Terminal (Admin)`.
3. Run:

    ```
    cd C:\FireForex
    powershell -ExecutionPolicy Bypass -File scripts\vps_bootstrap.ps1
    ```

4. When it asks, type:
   - MT5 login (e.g. `52754648`)
   - MT5 password (hidden)
   - MT5 server (press Enter for `ICMarketsSC-Demo`)
   - MT5 terminal path (press Enter for the default)
5. Done. The web UI is now running.

## Reach the web UI

From your laptop:

```
ssh -L 8000:127.0.0.1:8000 <your-vps-user>@<your-vps-ip>
```

Then open http://127.0.0.1:8000 in a browser.

## Make it trade

1. **Parameters tab** → pick a pair, timeframes, set any overrides.
2. **Run tab** → hit the run button. Wait until it finishes.
3. **Results tab** → scroll up, click **Deploy to live ▶**.
4. A prompt asks which pairs to trade. Comma-separated. Hit OK.
5. A popup tells you: *"Deployed. Next step on the VPS: schtasks /Run /TN ff-live-runner."*
6. **On the VPS** (SSH):

    ```
    schtasks /Run /TN ff-live-runner
    ```

7. **Live tab** in the web UI. Plans appear as signals fire. Reconcile report auto-refreshes every hour.

## Stop it

```
schtasks /End /TN ff-live-runner
```

Then flatten any leftover open trades manually in the MT5 terminal.

## If it breaks

- Logs: `C:\FireForex\artifacts\live\errors.jsonl` and `crashes.jsonl`.
- The Scheduled Task restarts itself every 60 seconds on failure. Usually self-heals.
- If the web UI dies: `schtasks /Run /TN ff-web`.

## Changing the password later

```
notepad C:\FireForex\.env.live
schtasks /End /TN ff-live-runner
schtasks /Run /TN ff-live-runner
```

That's it.
