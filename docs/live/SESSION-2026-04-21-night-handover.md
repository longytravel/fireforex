# Night handover — 2026-04-21 20:30 GMT+1

Written while user was out. Plan file at
`C:\Users\ROG\.claude\plans\go-into-plan-mode-graceful-matsumoto.md`
(Ultraplan-refined, user-approved).

## What shipped this session (solo, unpushed)

Commit staged on `main` (not yet pushed — user decides when to deploy to
VPS):

1. **`scripts/reset_live_day.py`** — added `"service_config.json"` to
   `ARCHIVE_TARGETS` (line 47). Reset now wipes the trial alongside
   plans/tickets/state/errors/crashes. Kills the "stale config resurrects
   yesterday's trial after reset" footgun documented as item 7 in the
   evening handover.

2. **`scripts/reconcile_live.py`** — new ~140 LOC CLI. Glue over
   `ff.replay.replay_service_config` + `ff.live.reconcile.build_live_df`
   + `reconcile.reconcile` + `reconcile.write_report`. Single command
   from laptop after a live trade closes:
   ```
   .\.venv\Scripts\python.exe scripts\reconcile_live.py
   ```
   Writes `artifacts/live/reconcile/<stamp>.html` + `.json`. Prints
   `{matched, missing_in_live, extra_in_live}` counts.
   Supports `--skip-replay` to reuse the last replay NPZ (fast rerun
   during debugging).

Tests: **173 passed, 4 warnings in 15.39s** after changes.

## What user has to do next (in order)

### Step A — Tailscale + OpenSSH (Phase 0 of plan)

Only the user can run these. After this, I can `ssh ff-vps "<cmd>"`
from laptop Bash and see everything directly.

**Laptop:**
1. Install Tailscale for Windows → https://tailscale.com/download/windows
2. Sign in (Google or GitHub account), accept the default tailnet.

**VPS (admin PowerShell — RDP in first):**
```powershell
# Install Tailscale (same sign-in as laptop so both land on same tailnet)
# Use the MSI from https://tailscale.com/download/windows

# Enable OpenSSH Server
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service sshd -StartupType Automatic
New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH SSH Server' `
  -Enabled True -Direction Inbound -Protocol TCP `
  -Action Allow -LocalPort 22
```

**Laptop — generate key + copy to VPS:**
```bash
ssh-keygen -t ed25519 -f ~/.ssh/ff_vps -N ""
cat ~/.ssh/ff_vps.pub
```
Then on the VPS, append that public key to **either**:
- `C:\Users\<vps-user>\.ssh\authorized_keys` (if non-admin account), OR
- `C:\ProgramData\ssh\administrators_authorized_keys` (if VPS user is
  admin — Windows OpenSSH uses this file instead for admin users).

ACLs for the admin file matter. If using it, also run on VPS:
```powershell
icacls "C:\ProgramData\ssh\administrators_authorized_keys" /inheritance:r
icacls "C:\ProgramData\ssh\administrators_authorized_keys" /grant "Administrators:F" "SYSTEM:F"
```

**Laptop — `~/.ssh/config` entry:**
```
Host ff-vps
  HostName <tailscale-magicdns-name-or-100.x.x.x>
  User <vps-user>
  IdentityFile ~/.ssh/ff_vps
```

**Verify from laptop:**
```bash
ssh ff-vps "hostname && dir \"C:\Projects\Fire Forex\artifacts\live\""
```

Get that working → tell me in chat → I take over for the rest.

### Step B — I handle the rest

Once `ssh ff-vps ...` works from my side, I will:

1. **Push this commit** to `origin/main` so the VPS gets the reset fix.
2. **Run reset over SSH** — `ssh ff-vps 'cmd /c "...\Reset Live Day (VPS).bat"'`
   — archives everything including the new service_config; closes all
   MT5 positions; disables the scheduled task.
3. **Drive the L1 backtest** via the web UI (you'll need to `/fast` the
   server — `scripts\ff_restart_server.ps1` — and click through Parameters
   tab; I'll describe the exact clicks).
4. **Deploy** via the Deploy button (pushes config to origin/main).
5. **Run deploy bat on VPS over SSH** to kick the runner.
6. **Monitor** for the first M15 bar-close fire via SSH + `live-state`
   branch.
7. **Wait** for natural SL or TP hit (could be minutes to hours).
8. **Run reconcile** — `python scripts\reconcile_live.py` — produces
   the first-ever end-to-end parity report.

## What I could NOT do solo

- **Install Tailscale + OpenSSH** — needs physical access / RDP + admin
  creds on VPS. Neither I nor laptop Claude can do this without you.
- **Push to origin/main** — holding off. The reset fix is destructive
  to the VPS state on next Reset bat run (archives `service_config.json`
  where it used to leave it). Not a blast-radius issue — just prefer
  you approve the push.
- **Physically run the Restart Fire Forex bat / Deploy bat** — I can
  trigger them over SSH *once SSH is up*. Until then, the UI is
  laptop-only and the VPS needs your double-click.

## Open questions for when you're back

1. **`pytest` warnings** — 4 RSI divide-by-zero warnings in
   `ff/signal_lib.py:199`. Pre-existing. Safe to ignore for tonight.
2. **MT5 demo state** — unknown. I have not SSH access. Best guess from
   memory: reset bat was run earlier with good intent but
   `service_config.json` survived. On next Reset (after push) that
   unknown state gets fully archived.
3. **Live-state branch** — 180s timeout fix shipped in `08e262d` but
   never verified on VPS. Will confirm during Step B-6.

## File index touched tonight

- `scripts/reset_live_day.py` — edit (1 line of new content).
- `scripts/reconcile_live.py` — new.
- `docs/live/SESSION-2026-04-21-night-handover.md` — new (this file).
- Plan file (not in repo): `C:\Users\ROG\.claude\plans\go-into-plan-mode-graceful-matsumoto.md`.

## Sanity

- 173 tests pass.
- `scripts\reconcile_live.py --help` works.
- No runner or MT5 action taken in your absence.
