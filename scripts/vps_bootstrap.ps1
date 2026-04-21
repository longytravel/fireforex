# Fire Forex — one-shot VPS bootstrap.
#
# Run this once on the Windows VPS. Answers ONE question (MT5 password),
# then installs everything, writes .env.live, and registers the services
# that keep the web UI + live runner alive across reboots.
#
# Usage (as Administrator, from the repo root):
#   powershell -ExecutionPolicy Bypass -File scripts\vps_bootstrap.ps1
#
# After it finishes:
#   1. Open http://127.0.0.1:8000 (via SSH tunnel or tailscale).
#   2. Run a backtest on the Parameters tab you want to go live.
#   3. Click "Deploy to live" on the Results tab.
#   4. Done. Runner trades the same settings on the demo account.

$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $ROOT

function _ok($msg) { Write-Host "  OK  $msg" -ForegroundColor Green }
function _step($msg) { Write-Host "[$msg]" -ForegroundColor Cyan }

_step "1/6  python venv"
if (-not (Test-Path ".venv")) { py -3.11 -m venv .venv }
_ok ".venv ready"

_step "2/6  pip install"
& .\.venv\Scripts\pip.exe install --upgrade pip | Out-Null
& .\.venv\Scripts\pip.exe install -r requirements-web.txt | Out-Null
& .\.venv\Scripts\pip.exe install MetaTrader5 maturin | Out-Null
_ok "deps installed"

_step "3/6  Rust engine build"
& .\.venv\Scripts\maturin.exe develop --release | Out-Null
_ok "ff_core built"

_step "4/6  .env.live"
$envFile = Join-Path $ROOT ".env.live"
if (Test-Path $envFile) {
    _ok "$envFile already exists, skipping creds prompt"
} else {
    Write-Host ""
    Write-Host "Enter your IC Markets DEMO credentials." -ForegroundColor Yellow
    Write-Host "(These go into .env.live only — never committed to git.)" -ForegroundColor Yellow
    $login = Read-Host "MT5 login (numeric)"
    $secure = Read-Host "MT5 password" -AsSecureString
    $password = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    )
    $server = Read-Host "MT5 server (default: ICMarketsSC-Demo)"
    if ([string]::IsNullOrWhiteSpace($server)) { $server = "ICMarketsSC-Demo" }
    $termPath = Read-Host "MT5 terminal path (default: C:\Program Files\MetaTrader 5 IC Markets\terminal64.exe)"
    if ([string]::IsNullOrWhiteSpace($termPath)) {
        $termPath = "C:\Program Files\MetaTrader 5 IC Markets\terminal64.exe"
    }
    $env_contents = @"
MT5_LOGIN=$login
MT5_PASSWORD=$password
MT5_SERVER=$server
MT5_TERMINAL_PATH=$termPath
"@
    Set-Content -Path $envFile -Value $env_contents -Encoding UTF8
    _ok "$envFile written"
}

_step "5/6  Scheduled Tasks"
function _register_task($name, $cmd, $args) {
    schtasks /Query /TN $name 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { schtasks /Delete /TN $name /F | Out-Null }
    schtasks /Create `
        /TN $name `
        /TR "`"$cmd`" $args" `
        /SC ONSTART `
        /RL HIGHEST `
        /RU "$env:USERNAME" `
        /F | Out-Null
    _ok "registered $name"
}
$python = Join-Path $ROOT ".venv\Scripts\python.exe"
_register_task "ff-web"         $python "run.py web"
_register_task "ff-live-runner" $python "-m ff.live.runner_service"

_step "6/6  kick the web UI"
schtasks /Run /TN ff-web | Out-Null
_ok "ff-web started (live runner waits for a Deploy click)"

Write-Host ""
Write-Host "=============================================================" -ForegroundColor Green
Write-Host " Done. Open http://127.0.0.1:8000 over your SSH tunnel:" -ForegroundColor Green
Write-Host "   ssh -L 8000:127.0.0.1:8000 <vps-user>@<vps-ip>" -ForegroundColor Gray
Write-Host ""
Write-Host " Next:" -ForegroundColor Green
Write-Host "   1. Run a backtest in the web UI." -ForegroundColor Gray
Write-Host "   2. Results tab -> 'Deploy to live'." -ForegroundColor Gray
Write-Host "   3. Live tab -> 'Start'." -ForegroundColor Gray
Write-Host "=============================================================" -ForegroundColor Green
