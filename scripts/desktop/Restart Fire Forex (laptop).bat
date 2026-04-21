@echo off
REM Double-click this on the laptop to pull the latest code, rebuild the Rust
REM engine if needed, kill any running Fire Forex web UI, and restart it.
REM Place a shortcut to this file on the laptop Desktop.

setlocal
cd /d "C:\Users\ROG\Projects\Fire Forex"

echo.
echo === Fire Forex laptop · pull + restart ===
echo.

echo [1/4] killing whatever is bound to port 8000...
REM Port-based kill is reliable. Any process listening on :8000 dies. Other
REM python processes (Jupyter, Claude, etc.) untouched.
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 2 /nobreak >nul
echo    OK.

echo.
echo [2/4] git pull...
git pull --ff-only
if errorlevel 1 (
    echo    FAILED: git pull. Resolve conflicts and try again.
    pause
    exit /b 1
)

echo.
echo [3/4] rebuilding Rust engine if core/ changed...
git diff HEAD~1 --name-only 2>nul | findstr /r "^core/" >nul
if not errorlevel 1 (
    echo    core/ changed — running maturin develop --release...
    call .venv\Scripts\maturin.exe develop --release
    if errorlevel 1 (
        echo    FAILED: maturin build.
        pause
        exit /b 1
    )
) else (
    echo    core/ unchanged — skipping rebuild.
)

echo.
echo [4/4] starting web UI on http://127.0.0.1:8000 ...
start "Fire Forex web UI" "C:\Users\ROG\Projects\Fire Forex\.venv\Scripts\python.exe" "C:\Users\ROG\Projects\Fire Forex\run.py" web

REM Give uvicorn a few seconds to bind, then open the browser.
timeout /t 4 /nobreak >nul
start "" "http://127.0.0.1:8000"

echo.
echo ============================================================
echo  Done. New browser tab should be opening on the UI.
echo  Old tab: hit Ctrl+F5 to hard-refresh.
echo ============================================================
echo.
timeout /t 3 /nobreak >nul
