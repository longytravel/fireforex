@echo off
REM Double-click on the VPS desktop when laptop Claude needs to see
REM what is actually happening. Dumps MT5 positions, scheduled task
REM state, service_config shape, git HEAD, and log tails into
REM artifacts\live\diag_<stamp>.txt AND prints to console.
REM
REM Safe to run any time - read-only, touches no state.

setlocal
cd /d "C:\Projects\Fire Forex"

echo.
echo === Fire Forex diagnostic dump ===
echo.

".venv\Scripts\python.exe" "scripts\diagnose_vps.py"
set rc=%errorlevel%

echo.
if not "%rc%"=="0" (
    echo diagnose_vps.py exited with errorlevel %rc%.
) else (
    echo Diagnostic report saved under artifacts\live\diag_*.txt
    echo Paste the console output above into the laptop chat.
)
echo.
pause
