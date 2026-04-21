@echo off
REM Double-click on the VPS desktop to start a fresh live trading day.
REM Stops runner, flattens all Fire Forex MT5 positions, archives today's
REM plans/tickets/state, wipes the originals, restarts the runner.
REM
REM Everything is archived under artifacts\live\archive\<stamp>\ — nothing
REM is destroyed. To recover: copy the archive dir back into artifacts\live\.

setlocal
cd /d "C:\Projects\Fire Forex"

echo.
echo === Fire Forex · reset live day (VPS) ===
echo.

echo This will:
echo   1. Stop the ff-live-runner Scheduled Task
echo   2. Close every MT5 position tagged with the Fire Forex magic number
echo   3. Archive today's plans/tickets/state under artifacts\live\archive\
echo   4. Delete the originals
echo   5. Restart the Scheduled Task
echo.
echo Press Ctrl+C to abort, or any key to proceed.
pause >nul

".venv\Scripts\python.exe" "scripts\reset_live_day.py"
set rc=%errorlevel%

echo.
if %rc% equ 0 (
    echo Done. Check MT5 to confirm all positions closed.
) else (
    echo Reset script exited with errorlevel %rc% — inspect output above.
)
echo.
pause
