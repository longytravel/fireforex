@echo off
REM Double-click this to pull the latest deploy config from the laptop and
REM restart the live runner on the VPS.
REM
REM Place a shortcut to this file on the VPS Desktop after first setup.

setlocal
cd /d "C:\Projects\Fire Forex"

echo.
echo === Fire Forex * pull + restart runner ===
echo.

echo [1/3] git fetch + hard reset to origin/main...
REM VPS is a dumb consumer of origin/main. If a local commit exists here that
REM isn't on origin, it was almost certainly a transient bootstrap edit (never
REM pushed) and is safe to discard. This keeps the VPS state deterministic.
git fetch origin main
if errorlevel 1 (
    echo    FAILED: git fetch - see message above.
    pause
    exit /b 1
)
git reset --hard origin/main
if errorlevel 1 (
    echo    FAILED: git reset - see message above.
    pause
    exit /b 1
)

echo.
echo [2/3] copying deploy\live_config.json to artifacts\live\service_config.json...
if not exist "deploy\live_config.json" (
    echo    FAILED: no deploy\live_config.json found. Did you click Deploy on the laptop first?
    pause
    exit /b 1
)
if not exist "artifacts\live" mkdir "artifacts\live"
copy /Y "deploy\live_config.json" "artifacts\live\service_config.json" >nul
echo    OK.

echo.
echo [3/3] re-enabling + restarting ff-live-runner scheduled task...
REM Reset disables the task so it stays truly stopped. Re-enable here
REM before /Run, else Run fails with "the task is disabled".
schtasks /Change /TN ff-live-runner /ENABLE >nul 2>&1
schtasks /End /TN ff-live-runner >nul 2>&1
schtasks /Run /TN ff-live-runner
if errorlevel 1 (
    echo    FAILED: could not start ff-live-runner. Is the task registered? Run scripts\vps_bootstrap.ps1 as admin.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Done. Runner is restarting with the new deploy config.
echo  Open http://127.0.0.1:8000 (in a VPS browser) and click
echo  the Live tab to see signals fire.
echo ============================================================
echo.
pause
