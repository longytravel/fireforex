@echo off
REM Double-click to see if the live runner is alive and what it's doing.
REM Place a shortcut to this on the VPS Desktop next to "Deploy Fire Forex".

setlocal enabledelayedexpansion
cd /d "C:\Projects\Fire Forex"

echo.
echo =========================================================
echo   Fire Forex live runner · status check
echo =========================================================
echo.

echo [1] Scheduled Task state:
schtasks /Query /TN ff-live-runner /FO LIST | findstr /R "TaskName Status Last"
echo.

echo [2] Python process holding the runner:
tasklist /FI "IMAGENAME eq python.exe" /V /FO LIST | findstr /R "^PID: ^Image ff.live.runner"
if errorlevel 1 echo   (no python.exe — runner NOT running)
echo.

echo [3] state.json (open positions):
if exist "artifacts\live\state.json" (
    type "artifacts\live\state.json"
) else (
    echo   no state.json yet — runner hasn't persisted any open positions.
)
echo.

echo [4] Today's plans:
for /f "tokens=2 delims==" %%d in ('"wmic os get localdatetime /value"') do set dt=%%d
set today=%dt:~0,4%-%dt:~4,2%-%dt:~6,2%
if exist "artifacts\live\plans\%today%.jsonl" (
    echo   file: artifacts\live\plans\%today%.jsonl
    echo   line count:
    find /c /v "" "artifacts\live\plans\%today%.jsonl"
    echo.
    echo   last 5 plans:
    powershell -NoProfile -Command "Get-Content 'artifacts\live\plans\%today%.jsonl' -Tail 5"
) else (
    echo   no plans yet today.
)
echo.

echo [5] Errors today:
if exist "artifacts\live\errors.jsonl" (
    echo   last 5 errors:
    powershell -NoProfile -Command "Get-Content 'artifacts\live\errors.jsonl' -Tail 5"
) else (
    echo   no errors file — good.
)
echo.

echo [6] Crashes:
if exist "artifacts\live\crashes.jsonl" (
    echo   *** crashes file exists — runner has fallen over at least once ***
    powershell -NoProfile -Command "Get-Content 'artifacts\live\crashes.jsonl' -Tail 3"
) else (
    echo   no crashes file — good.
)
echo.

echo [7] MT5 positions with Fire Forex magic number (20260420):
powershell -NoProfile -Command "try { $p = Get-Process -Name terminal64 -ErrorAction Stop; Write-Host ('  MT5 terminal64 is running (PID ' + $p.Id + ')') } catch { Write-Host '  MT5 terminal64 NOT running — runner cannot place orders' }"
echo.

echo =========================================================
echo   Done. Close this window when you're happy.
echo =========================================================
pause
