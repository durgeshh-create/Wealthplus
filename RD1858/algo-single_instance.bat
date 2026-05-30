@echo off
setlocal EnableDelayedExpansion
title  RD1858 — WealthAlgo Launcher


echo.
echo ===================================================
echo   WEALTH++ ALGO  —  RD1858  ^|  port 5000
echo ===================================================
echo   Ulaa  ^|  CDP 9222
echo ===================================================

set "BOT_DIR=C:\durgesh\RD1858"
set "BOT_PORT=5000"
set "BOT_URL=http://localhost:%BOT_PORT%"
set "CDP_PORT=9222"

set "ULAA=C:\Program Files\Zoho\Ulaa\Application\ulaa.exe"
if not exist "%ULAA%" set "ULAA=C:\Program Files\Ulaa\ulaa.exe"
if not exist "%ULAA%" set "ULAA=C:\Program Files (x86)\Zoho\Ulaa\Application\ulaa.exe"

echo.
echo  Bot dir : %BOT_DIR%
echo  Ulaa    : %ULAA%

if not exist "%BOT_DIR%\run.py" (
    echo.
    echo  [ERROR] %BOT_DIR%\run.py not found. Check BOT_DIR.
    pause >nul
    exit /b 1
)

:: ══════════════════════════════════════════════════════════════════════════════
:: STEP 0  Kill bot process only — Ulaa never touched
:: ══════════════════════════════════════════════════════════════════════════════
echo.
echo [0/3] Stopping any running RD1858 bot instance...

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%BOT_PORT% " ^| findstr "LISTENING" 2^>nul') do (
    echo   Killing PID %%a on port %BOT_PORT%...
    taskkill /F /PID %%a >nul 2>&1
)
wmic process where "commandline like '%%RD1858%%run.py%%'"       delete >nul 2>&1
wmic process where "commandline like '%%RD1858%%dashboard.py%%'" delete >nul 2>&1
wmic process where "commandline like '%%RD1858%%start.vbs%%'"    delete >nul 2>&1

choice /d y /t 2 /c y /n >nul
echo   Done. Port %BOT_PORT% is free.

if exist "%BOT_DIR%\bot.log" (
    copy "%BOT_DIR%\bot.log" "%BOT_DIR%\bot.log.bak" >nul 2>&1
    del  "%BOT_DIR%\bot.log" >nul 2>&1
)

:: ══════════════════════════════════════════════════════════════════════════════
:: STEP 1  Ulaa — ensure CDP port 9222 is open
::
:: If port 9222 already listening → perfect, nothing to do.
:: If Ulaa not running            → launch it (uses existing logged-in session).
:: If Ulaa running but no 9222   → warn. Bot falls back to saved enctoken.
::   FIX: close Ulaa manually and re-run, or add the flag to Ulaa's shortcut.
:: ══════════════════════════════════════════════════════════════════════════════
echo.
echo [1/3] Checking Ulaa CDP port %CDP_PORT%...

netstat -ano | findstr /R /C:":%CDP_PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo   Port %CDP_PORT% already open — Ulaa CDP ready.
    goto :ulaa_ready
)

if not exist "%ULAA%" (
    echo   [WARN] Ulaa not found — falling back to credentials login.
    goto :ulaa_ready
)

tasklist /FI "IMAGENAME eq ulaa.exe" 2>nul | find /I "ulaa.exe" >nul 2>&1
if not errorlevel 1 (
    echo   [WARN] Ulaa is open but NOT on --remote-debugging-port=%CDP_PORT%.
    echo          CDP token extraction will not work.
    echo          FIX: Close Ulaa and re-run this script.
    echo          Or update Ulaa shortcut target to include:
    echo            --remote-debugging-port=%CDP_PORT% --remote-allow-origins=*
    goto :ulaa_ready
)

:: Ulaa not running — launch it using its default profile (logged-in session)
echo   Ulaa not running — launching with --remote-debugging-port=%CDP_PORT%...
start "" "%ULAA%" --remote-debugging-port=%CDP_PORT% --remote-allow-origins=* "https://kite.zerodha.com"
echo   Waiting 8s for Ulaa + Kite to load...
choice /d y /t 8 /c y /n >nul

:ulaa_ready

:: ══════════════════════════════════════════════════════════════════════════════
:: STEP 2  Launch Python engine
:: ══════════════════════════════════════════════════════════════════════════════
echo.
echo [2/3] Launching RD1858 engine on port %BOT_PORT%...
start "" wscript.exe "%BOT_DIR%\start.vbs"

echo.
echo [2/3] Waiting for port %BOT_PORT% ^(up to 90s^)...
call :FastPortCheck %BOT_PORT% RD1858
if errorlevel 1 (
    echo.
    echo  [ERROR] Engine did not start in 90s.
    if exist "%BOT_DIR%\bot.log" (
        echo  Last 40 lines of bot.log:
        powershell -NoProfile -Command "Get-Content '%BOT_DIR%\bot.log' -Tail 40"
    ) else (
        echo  bot.log not found.
    )
    echo.
    echo  Press any key to exit.
    pause >nul
    exit /b 1
)

:: ══════════════════════════════════════════════════════════════════════════════
:: STEP 3  Open dashboard as new tab in existing Ulaa via CDP
:: ══════════════════════════════════════════════════════════════════════════════
echo.
echo [3/3] Opening dashboard in Ulaa...

set "_ok=0"
netstat -ano | findstr /R /C:":%CDP_PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    powershell -NoProfile -Command ^
        "try{Invoke-RestMethod -Uri 'http://localhost:%CDP_PORT%/json/new?%BOT_URL%' -Method PUT -TimeoutSec 5|Out-Null;exit 0}catch{exit 1}" ^
        && set "_ok=1"
)

if "!_ok!" == "0" (
    echo   CDP unavailable — opening via start...
    if exist "%ULAA%" (
        start "" "%ULAA%" "%BOT_URL%"
    ) else (
        start "" "%BOT_URL%"
    )
)

echo.
echo ===================================================
echo   RD1858 live : http://localhost:%BOT_PORT%
echo   Ulaa CDP    : port %CDP_PORT%
echo   Happy trading^^!
echo ===================================================
echo.
echo   Closing in 3 seconds...
exit

:FastPortCheck
set "_port=%~1"
set "_name=%~2"
set "_loops=0"
echo   [%_name%] Polling port %_port%...
:_port_loop
set /a "_loops+=1"
if %_loops% geq 90 (
    echo   [TIMEOUT] Port %_port% not open after 90s.
    exit /b 1
)
netstat -ano | findstr /R /C:":%_port% .*LISTENING" >nul 2>&1
if !errorlevel! == 0 (
    echo   [READY] Port %_port% open after %_loops%s!
    exit /b 0
)
choice /d y /t 1 /c y /n >nul
goto _port_loop
