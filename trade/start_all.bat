@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title Trade_System_Watchdog_and_Scheduler
cd /d "%~dp0"

set "last_killed_hour=-1"

:loop
cls
set "current_time=%time: =0%"
set "HH=%current_time:~0,2%"
set "MM=%current_time:~3,2%"

echo =======================================================
echo   Trade System Watchdog ^& Auto-Scheduler
echo   Check Time: %date% %time%
echo   Scheduled Restart: Every hour at :34
echo =======================================================

if "%MM%"=="34" (
    if "!last_killed_hour!" neq "%HH%" (
        set "last_killed_hour=%HH%"
        echo.
        echo [!] Scheduled restart time %HH%:%MM% reached.
        echo Terminating q.py, option.py for fresh restart...
        wmic process where "name='python.exe' and commandline like '%%q.py%%'" call terminate >nul 2>&1
        wmic process where "name='py.exe' and commandline like '%%q.py%%'" call terminate >nul 2>&1
        wmic process where "name='python.exe' and commandline like '%%option.py%%'" call terminate >nul 2>&1
        wmic process where "name='py.exe' and commandline like '%%option.py%%'" call terminate >nul 2>&1
        echo [%time%] Processes terminated. Watchdog will restart them now...
        timeout /t 3 /nobreak >nul
    )

    if "%HH%"=="02" (
	echo barchart
	wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "barchart_auto.py" >nul
	if !errorlevel! equ 0 (
	    echo [OK] barchart_auto.py is running.
	) else (
	    echo [WARNING] barchart_auto.py is NOT running! Restarting MAXIMIZED...
	    start "barchart" /max py .\barchart_auto.py
	)
    )
 
    if "%HH%"=="03" (
	echo 0DTE...
	wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "option.py" >nul
	if !errorlevel! equ 0 (
	    echo [OK] option.py is running.
	) else (
	    echo [WARNING] option.py is NOT running! Restarting MAXIMIZED...
	    start "option" /max py .\option.py
	)
    )

)
:: ---------------------------------------------------------
:: 2. Process Watchdog & Auto-Restart
:: ---------------------------------------------------------

echo.
echo [2/4] Checking router.py...
wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "router.py" >nul
if %errorlevel% equ 0 (
    echo  [OK] router.py is running.
) else (
    echo  [WARNING] router.py is NOT running! Restarting MINIMIZED...
    start "router" /min py .\router.py
)

echo.
echo [3/4] Checking q.py...
wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "q.py" >nul
if %errorlevel% equ 0 (
    echo  [OK] q.py is running.
) else (
    echo  [WARNING] q.py is NOT running! Restarting MAXIMIZED...
    start "q.py" /max py .\q.py
)

echo.
echo [4/4] Checking ngrok...
wmic process where "name='ngrok.exe'" get commandline 2>nul | find "http 5000" >nul
if %errorlevel% equ 0 (
    echo  [OK] ngrok http 5000 is running.
) else (
    echo  [WARNING] ngrok is NOT running! Restarting MINIMIZED...
    start "ngrok_tunnel" /min "C:\Users\Administrator\Desktop\docker_mc\CT\ngrok\ngrok.exe" http 5000
)

echo -------------------------------------------------------
echo Monitoring in background... (Next check in 10s)
timeout /t 10 >nul

goto loop
