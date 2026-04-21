@echo off
REM Double-click this on the laptop to pull the latest code, rebuild the Rust
REM engine if needed, kill any running Fire Forex web UI, and restart it.
REM Place a shortcut to this file on the laptop Desktop.

setlocal
cd /d "C:\Users\ROG\Projects\Fire Forex"

echo.
echo === Fire Forex laptop * pull + restart ===
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
echo [3/5] rebuilding Rust engine if core/ changed...
REM findstr /r with a caret-anchored regex is fragile under CMD quoting
REM (the caret gets eaten as an escape char and findstr errors "Cannot
REM open ^core/" - which was enough on some laptops to kill the bat
REM without reaching step 5). Use plain prefix match with /b /c: instead.
set core_changed=0
for /f "delims=" %%F in ('git diff HEAD~1 --name-only 2^>nul') do (
    echo %%F | findstr /b /c:"core/" >nul 2>&1
    if not errorlevel 1 set core_changed=1
)
if "%core_changed%"=="1" (
    echo    core/ changed - running maturin develop --release...
    call ".venv\Scripts\maturin.exe" develop --release
    if errorlevel 1 (
        echo    FAILED: maturin build.
        pause
        exit /b 1
    )
) else (
    echo    core/ unchanged - skipping rebuild.
)

echo.
echo [4/5] pulling live state from VPS (live-state branch)...
REM Silent: state sync runs every minute on the VPS. Fetch may miss on
REM first boot (branch doesn't exist yet) - that's fine.
git fetch origin live-state --quiet 2>nul
git show-ref --verify --quiet refs/remotes/origin/live-state
if %errorlevel% neq 0 (
    echo    no live-state branch yet - skipped.
    goto :after_live_state
)
if not exist "artifacts\live" mkdir "artifacts\live" >nul 2>&1
REM A pipe inside a parenthesised IF block trips CMD on some locales
REM with ". was unexpected at this time." Two-step via a temp tarball
REM instead, keeping the IF block pipe-free.
git archive --format=tar origin/live-state > "%TEMP%\ff_live_state.tar" 2>nul
tar -xf "%TEMP%\ff_live_state.tar" -C "artifacts\live" 2>nul
del "%TEMP%\ff_live_state.tar" >nul 2>&1
echo    OK - plans + tickets + state refreshed.
:after_live_state

echo.
echo [5/5] starting web UI on http://127.0.0.1:8000 ...
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
REM Keep the window visible long enough to see any trailing error.
pause
