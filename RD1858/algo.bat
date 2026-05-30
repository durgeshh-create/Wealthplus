@echo off
setlocal EnableDelayedExpansion
title  WealthAlgo — Both Instances Launcher


echo.
echo ===================================================
echo   WEALTH++ ALGO  —  RD1858 + PS5673
echo ===================================================
echo   Ulaa    ^|  RD1858  ^|  CDP 9222  ^|  :5000
echo   Chrome  ^|  PS5673  ^|  CDP 9223  ^|  :5001
echo ===================================================

set "ULAA=C:\Program Files\Zoho\Ulaa\Application\ulaa.exe"
if not exist "%ULAA%" set "ULAA=C:\Program Files\Ulaa\ulaa.exe"
if not exist "%ULAA%" set "ULAA=C:\Program Files (x86)\Zoho\Ulaa\Application\ulaa.exe"

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

echo.
echo  Ulaa   : %ULAA%
echo  Chrome : %CHROME%

:: ══════════════════════════════════════════════════════════════════════════════
:: STEP 0  Kill bot processes only — browsers never touched
:: ══════════════════════════════════════════════════════════════════════════════
echo.
echo [0/4] Stopping any running bot instances...

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000 " ^| findstr "LISTENING" 2^>nul') do (
    echo   Killing PID %%a on port 5000 ^(RD1858^)...
    taskkill /F /PID %%a >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5001 " ^| findstr "LISTENING" 2^>nul') do (
    echo   Killing PID %%a on port 5001 ^(PS5673^)...
    taskkill /F /PID %%a >nul 2>&1
)
wmic process where "commandline like '%%RD1858%%run.py%%'"       delete >nul 2>&1
wmic process where "commandline like '%%PS5673%%run.py%%'"       delete >nul 2>&1
wmic process where "commandline like '%%RD1858%%dashboard.py%%'" delete >nul 2>&1
wmic process where "commandline like '%%PS5673%%dashboard.py%%'" delete >nul 2>&1
wmic process where "commandline like '%%RD1858%%start.vbs%%'"    delete >nul 2>&1
wmic process where "commandline like '%%PS5673%%start.vbs%%'"    delete >nul 2>&1

choice /d y /t 2 /c y /n >nul
echo   Done.

:: ══════════════════════════════════════════════════════════════════════════════
:: STEP 1  Ulaa — ensure CDP port 9222 is open for RD1858
::
:: If port 9222 already listening → perfect, nothing to do.
:: If Ulaa not running            → launch it (no profile flag, uses default).
:: If Ulaa running but no 9222   → cannot inject CDP into a running instance.
::   We just warn. Bot will fall back to saved enctoken / credentials login.
::   To fix permanently: add Ulaa to Windows startup with the CDP flag (see note).
:: ══════════════════════════════════════════════════════════════════════════════
echo.
echo [1/4] Checking Ulaa CDP port 9222 ^(RD1858^)...

netstat -ano | findstr /R /C:":9222 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo   Port 9222 already open — Ulaa CDP ready.
    goto :ulaa_ready
)

if not exist "%ULAA%" (
    echo   [WARN] Ulaa not found — RD1858 falls back to credentials login.
    goto :ulaa_ready
)

tasklist /FI "IMAGENAME eq ulaa.exe" 2>nul | find /I "ulaa.exe" >nul 2>&1
if not errorlevel 1 (
    echo   [WARN] Ulaa is open but NOT on --remote-debugging-port=9222.
    echo          CDP token extraction will not work for RD1858.
    echo          FIX: Close Ulaa manually, then re-run this script.
    echo          Or add to Ulaa shortcut: --remote-debugging-port=9222 --remote-allow-origins=*
    goto :ulaa_ready
)

:: Ulaa not running at all — launch it without touching any profile
echo   Ulaa not running — launching with --remote-debugging-port=9222...
start "" "%ULAA%" --remote-debugging-port=9222 --remote-allow-origins=* "https://kite.zerodha.com"
echo   Waiting 8s for Ulaa to load...
choice /d y /t 8 /c y /n >nul

:ulaa_ready

:: ══════════════════════════════════════════════════════════════════════════════
:: STEP 2  Chrome — ensure CDP port 9223 is open for PS5673
:: Same logic as Step 1.
:: ══════════════════════════════════════════════════════════════════════════════
echo.
echo [2/4] Checking Chrome CDP port 9223 ^(PS5673^)...

netstat -ano | findstr /R /C:":9223 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo   Port 9223 already open — Chrome CDP ready.
    goto :chrome_ready
)

if not exist "%CHROME%" (
    echo   [WARN] Chrome not found — PS5673 falls back to credentials login.
    goto :chrome_ready
)

tasklist /FI "IMAGENAME eq chrome.exe" 2>nul | find /I "chrome.exe" >nul 2>&1
if not errorlevel 1 (
    echo   [WARN] Chrome is open but NOT on --remote-debugging-port=9223.
    echo          CDP token extraction will not work for PS5673.
    echo          FIX: Close Chrome manually, then re-run this script.
    echo          Or add to Chrome shortcut: --remote-debugging-port=9223 --remote-allow-origins=*
    goto :chrome_ready
)

:: Chrome not running at all — launch it
echo   Chrome not running — launching with --remote-debugging-port=9223...
start "" "%CHROME%" --remote-debugging-port=9223 --remote-allow-origins=* "https://kite.zerodha.com"
echo   Waiting 6s for Chrome to load...
choice /d y /t 6 /c y /n >nul

:chrome_ready

:: ══════════════════════════════════════════════════════════════════════════════
:: STEP 3  Launch Python engines
:: ══════════════════════════════════════════════════════════════════════════════
echo.
echo [3/4] Launching bot engines...

echo   Starting RD1858 ^(port 5000^)...
start "" wscript.exe "C:\durgesh\RD1858\start.vbs"
choice /d y /t 3 /c y /n >nul

echo   Starting PS5673 ^(port 5001^)...
start "" wscript.exe "C:\durgesh\PS5673\start.vbs"

echo.
echo [3/4] Waiting for both engines ^(up to 120s^)...

set "_loops=0"
set "_rd=0"
set "_ps=0"

:poll_loop
set /a "_loops+=1"
if %_loops% geq 120 goto :poll_done

if !_rd! == 0 (
    netstat -ano | findstr /R /C:":5000 .*LISTENING" >nul 2>&1
    if !errorlevel! == 0 ( echo   [READY] RD1858 port 5000 up! & set "_rd=1" )
)
if !_ps! == 0 (
    netstat -ano | findstr /R /C:":5001 .*LISTENING" >nul 2>&1
    if !errorlevel! == 0 ( echo   [READY] PS5673 port 5001 up! & set "_ps=1" )
)
if !_rd! == 1 if !_ps! == 1 goto :poll_done

choice /d y /t 1 /c y /n >nul
goto :poll_loop

:poll_done
if !_rd! == 0 (
    echo.
    echo   [TIMEOUT] RD1858 did not start. Last 20 lines of bot.log:
    if exist "C:\durgesh\RD1858\bot.log" (
        powershell -NoProfile -Command "Get-Content 'C:\durgesh\RD1858\bot.log' -Tail 20"
    ) else ( echo   bot.log not found. )
)
if !_ps! == 0 (
    echo.
    echo   [TIMEOUT] PS5673 did not start. Last 20 lines of bot.log:
    if exist "C:\durgesh\PS5673\bot.log" (
        powershell -NoProfile -Command "Get-Content 'C:\durgesh\PS5673\bot.log' -Tail 20"
    ) else ( echo   bot.log not found. )
)
if !_rd! == 0 if !_ps! == 0 (
    echo.
    echo   Press any key to exit.
    pause >nul
    exit /b 1
)

:: ══════════════════════════════════════════════════════════════════════════════
:: STEP 4  Open dashboards as new tabs in existing browsers via CDP
:: ══════════════════════════════════════════════════════════════════════════════
echo.
echo [4/4] Opening dashboards...
choice /d y /t 2 /c y /n >nul

if !_rd! == 1 (
    echo   Opening RD1858 dashboard in Ulaa...
    set "_ok=0"
    powershell -NoProfile -Command ^
        "try{Invoke-RestMethod -Uri 'http://localhost:9222/json/new?http://localhost:5000' -Method PUT -TimeoutSec 5|Out-Null;exit 0}catch{exit 1}" ^
        && set "_ok=1"
    if "!_ok!" == "0" start "" "%ULAA%" "http://localhost:5000"
    choice /d y /t 2 /c y /n >nul
)

if !_ps! == 1 (
    echo   Opening PS5673 dashboard in Chrome...
    set "_ok=0"
    powershell -NoProfile -Command ^
        "try{Invoke-RestMethod -Uri 'http://localhost:9223/json/new?http://localhost:5001' -Method PUT -TimeoutSec 5|Out-Null;exit 0}catch{exit 1}" ^
        && set "_ok=1"
    if "!_ok!" == "0" start "" "%CHROME%" "http://localhost:5001"
)

echo.
echo ===================================================
echo   Ulaa   ^(CDP 9222^) : RD1858  http://localhost:5000
echo   Chrome ^(CDP 9223^) : PS5673  http://localhost:5001
echo   Happy trading^^!
echo ===================================================
echo.
echo   Closing in 3 seconds...
exit
