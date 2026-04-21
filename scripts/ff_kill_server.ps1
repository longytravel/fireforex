# Kill any orphaned Fire Forex web server processes.
#
# Match by command line (python.exe + run.py + web) so recycled port
# numbers can't fool us. Port 8000 listeners are a belt-and-braces
# fallback for any non-matching process holding the socket.
#
# Usage from the repo root:
#   .\scripts\ff_kill_server.ps1

$killed = @()

$procs = Get-CimInstance Win32_Process `
  | Where-Object { $_.Name -eq 'python.exe' `
                   -and $_.CommandLine -like '*run.py*' `
                   -and $_.CommandLine -like '*web*' }
foreach ($p in $procs) {
    Write-Host "Killing PID $($p.ProcessId) -- $($p.CommandLine)"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    $killed += $p.ProcessId
}

$conns = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
foreach ($c in $conns) {
    if ($killed -notcontains $c.OwningProcess) {
        Write-Host "Killing port-8000 listener PID $($c.OwningProcess)"
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
        $killed += $c.OwningProcess
    }
}

if ($killed.Count -eq 0) {
    Write-Host "No Fire Forex web server processes found."
} else {
    Write-Host "`nKilled PIDs: $($killed -join ', ')"
}

# Sockets can take a moment to release after Stop-Process returns. Poll up
# to 4s before declaring success or failure so the user sees the truth.
$remaining = $null
for ($i = 0; $i -lt 8; $i++) {
    Start-Sleep -Milliseconds 500
    $remaining = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    if (-not $remaining) { break }
}

Write-Host "`nPort 8000 listeners after cleanup:"
if ($remaining) {
    $remaining | Format-Table LocalPort,State,OwningProcess
    # If the OwningProcess no longer exists, this is a Windows orphan TCB:
    # process is dead but the kernel socket lingers. Stop-Process can't help.
    # Usually clears within a few minutes; reboot or alt port if urgent.
    $orphan = $remaining | Where-Object { -not (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue) }
    if ($orphan) {
        Write-Host "ORPHAN TCB: PID $($orphan[0].OwningProcess) is gone but the socket is still held by the Windows kernel." -ForegroundColor Yellow
        Write-Host "  - Wait 1-5 min for the OS to reclaim it, OR" -ForegroundColor Yellow
        Write-Host "  - Restart with an alt port: .\.venv\Scripts\python.exe run.py web --port 8001" -ForegroundColor Yellow
        Write-Host "  - Reboot if it sticks." -ForegroundColor Yellow
    } else {
        Write-Host "Live process still bound -- re-run this script." -ForegroundColor Yellow
    }
} else {
    Write-Host "  (none)"
}
