@echo off
title Kill Algo Bot Instances
echo ===================================================
echo  STOPPING BOTH ALGO BOT INSTANCES
echo ===================================================
echo.

:: ── Kill by port (most reliable — targets only the Flask/Python listener) ─────
echo [1/3] Killing Python process on port 5000 (RD1858)...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000 " ^| findstr "LISTENING"') do (
    echo   Found PID %%a on port 5000 -- terminating...
    taskkill /F /PID %%a >nul 2>&1
    echo   Done.
)

echo [2/3] Killing Python process on port 5001 (PS5673)...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5001 " ^| findstr "LISTENING"') do (
    echo   Found PID %%a on port 5001 -- terminating...
    taskkill /F /PID %%a >nul 2>&1
    echo   Done.
)

:: ── Clean up orphaned Python/wscript processes for these bots only ────────────
:: Targets only processes whose command line references RD1858 or PS5673 paths.
:: Does NOT touch chrome.exe, ulaa.exe, or any browser process.
echo [3/3] Cleaning up orphaned Python/wscript processes...
wmic process where "commandline like '%%RD1858%%dashboard.py%%'" delete >nul 2>&1
wmic process where "commandline like '%%PS5673%%dashboard.py%%'" delete >nul 2>&1
wmic process where "commandline like '%%RD1858%%run.py%%'"       delete >nul 2>&1
wmic process where "commandline like '%%PS5673%%run.py%%'"       delete >nul 2>&1
wmic process where "commandline like '%%RD1858%%start.vbs%%'"    delete >nul 2>&1
wmic process where "commandline like '%%PS5673%%start.vbs%%'"    delete >nul 2>&1

:: ── Verify ports are free ─────────────────────────────────────────────────────
echo.
netstat -ano | findstr ":5000 " | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (echo   Port 5000: FREE) else (echo   Port 5000: still in use!)

netstat -ano | findstr ":5001 " | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (echo   Port 5001: FREE) else (echo   Port 5001: still in use!)

echo.
echo ===================================================
echo  Both Python engines stopped. Browsers untouched.
echo  Safe to relaunch via algo.bat.
echo ===================================================
timeout /t 3 /nobreak