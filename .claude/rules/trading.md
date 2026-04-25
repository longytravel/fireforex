---
description: Live-trading discipline for Fire Forex (VPS runner, broker-state, signal variants)
paths: ["app/live_runner/**", "scripts/**/*.ps1", "scripts/desktop/**", "ff/harness.py"]
---

# Live-trading discipline

## Hard rules
- **Never spawn a uvicorn** from a Claude session. Ask the user to run `scripts\ff_restart_server.ps1` instead. Background uvicorns cause stale-`.pyc` bugs; they've recurred multiple times.
- **Any change to signal-variant ID resolution** must bump `signal_lib.py` stable-variant-count protection AND update the migration script AND be verified on VPS before closing the PR.
- **Any new broker/MT5 code path** must go through a fingerprint-based reconciliation test before shipping — not just a live smoke test.
- **Never commit MT5 credentials**. `.gitignore` excludes `live_artifacts/creds*` — if you think something contains credentials, assume it does.

## Required before claiming "ships fine to VPS"
- Forward-run at least one closed trade and verify it reconciles against BT at the same signal variant.
- Timezone: confirm broker time vs UTC handling on every new time-sensitive field.
- Forming candles: confirm runner skips the forming M1 candle and only acts on closed bars.

## Don't
- Don't add retry loops that can fire a signal twice.
- Don't deploy a new VPS runner without verifying the signal_lib variant count matches training.
- Don't assume Dukascopy BT and MT5 BT produce matching trades — they don't; reconcile against the source that drove the live execution.
