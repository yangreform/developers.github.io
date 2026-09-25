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
echo   Check Time: %date% %time%
echo =======================================================

:: 取得當前星期幾的數字
for /f "tokens=2 delims==." %%a in ('wmic path win32_localtime get dayofweek /value') do set DOW=%%a

if "%MM%"=="34" (
    if "!last_killed_hour!" neq "%HH%" (
        set "last_killed_hour=%HH%"
        echo [!] Scheduled restart time %HH%:%MM% reached.
        wmic process where "name='python.exe' and commandline like '%%q.py%%'" call terminate >nul 2>&1
        wmic process where "name='py.exe' and commandline like '%%q.py%%'" call terminate >nul 2>&1
        echo [%time%] Processes terminated. Watchdog will restart them now...
        timeout /t 3 /nobreak >nul
    )

    if "%HH%"=="01" (
	wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "insider.py" >nul
	if !errorlevel! equ 0 (
	    echo [OK] insider.py is running.
	) else (
	    start "insider" /max py .\insider.py
	)
    )

    if "%HH%"=="02" (
	wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "barchart_auto.py" >nul
	if !errorlevel! equ 0 (
	    echo [OK] barchart_auto.py is running.
	) else (
	    start "barchart" /max py .\barchart_auto.py
	)
    )
 
    if "%HH%"=="03" (
	wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "option.py" >nul
	if !errorlevel! equ 0 (
	    echo [OK] option.py is running.
	) else (
	    start "option" /max py .\option.py
	)
    )


    echo =======================================================
    if %DOW% == 3 (
    	if "%HH%"=="09" (
    	    wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "close_Shioaji.py" >nul
    	    if !errorlevel! equ 0 (
    	        echo [OK] close_Shioaji.py is running.
    	    ) else (
    	        start "close_Shioaji" /max py .\close_Shioaji.py
    	    )
    	)
    	if "%HH%"=="15" (
    	    wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "open_Shioaji.py" >nul
    	    if !errorlevel! equ 0 (
    	        echo [OK] open_Shioaji.py is running.
    	    ) else (
    	        start "open_Shioaji" /max py .\open_Shioaji.py
    	    )
    	)
    )

    if %DOW% == 5 (
    	if "%HH%"=="09" (
    	    wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "close_Shioaji.py" >nul
    	    if !errorlevel! equ 0 (
    	        echo [OK] close_Shioaji.py is running.
    	    ) else (
    	        start "close_Shioaji" /max py .\close_Shioaji.py
    	    )
    	)
    	if "%HH%"=="15" (
    	    wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "open_Shioaji.py" >nul
    	    if !errorlevel! equ 0 (
    	        echo [OK] open_Shioaji.py is running.
    	    ) else (
    	        start "open_Shioaji" /max py .\open_Shioaji.py
    	    )
    	)
    )
    if %DOW% == 6 (
    	if "%HH%"=="04" (
    	    wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "close_Shioaji.py" >nul
    	    if !errorlevel! equ 0 (
    	        echo [OK] close_Shioaji.py is running.
    	    ) else (
    	        start "close_Shioaji" /max py .\close_Shioaji.py
    	    )
    	)
    )
    if %DOW% == 1 (
    	if "%HH%"=="09" (
    	    wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "open_Shioaji.py" >nul
    	    if !errorlevel! equ 0 (
    	        echo [OK] open_Shioaji.py is running.
    	    ) else (
    	        start "open_Shioaji" /max py .\open_Shioaji.py
    	    )
    	)
    )


)

:: 2. Process Watchdog & Auto-Restart

echo.
wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "router.py" >nul
if %errorlevel% equ 0 (
    echo  [OK] router.py is running.
) else (
    start "router" /min py .\router.py
)

echo.
wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "q.py" >nul
if %errorlevel% equ 0 (
    echo  [OK] q.py is running.
) else (
    start "q.py" /max py .\q.py
)

echo.
wmic process where "name='ngrok.exe'" get commandline 2>nul | find "http 5000" >nul
if %errorlevel% equ 0 (
    echo  [OK] ngrok http 5000 is running.
) else (
    start "ngrok_tunnel" /min "C:\Users\Administrator\Desktop\docker_mc\CT\ngrok\ngrok.exe" http 5000
)

echo Monitoring in background... (Next check in 10s)
timeout /t 10 >nul

goto loop
