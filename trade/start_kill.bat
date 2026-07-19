@echo off
setlocal enabledelayedexpansion
title Task Killer (5-Hour Intervals)

:loop
cls
:: Format current time (handling space for hours 0-9)
set "current_time=%time: =0%"
set "HH=%current_time:~0,2%"
set "MM=%current_time:~3,2%"

echo ============================================
echo   Current System Time: %current_time%
echo   Target Schedule: Every 1 Hours at :34
echo ============================================

set /a "HH_dec=1%HH% - 100"
set /a "hour_mod=%HH_dec% %% 1"

:: Check if Minutes == 40 AND Hour is a multiple of 5
if "%MM%"=="34" (
    if %hour_mod%==0 (
        echo [!] Target time %HH%:%MM% detected.
        echo Killing ONLY the main.py process...
        
        :: Target and terminate only the Python process running main.py
        wmic process where "name='python.exe' and commandline like '%%main.py%%'" call terminate >nul 2>&1
        wmic process where "name='py.exe' and commandline like '%%main.py%%'" call terminate >nul 2>&1
        
        echo [%time%] Task executed successfully.
        echo Waiting 65s to prevent re-triggering...
        timeout /t 65 /nobreak >nul
    )
)

echo.
:: Check for Sunday Database Update at 03:00
for /f "skip=1" %%d in ('wmic path win32_localtime get dayofweek') do (
    if "%%d"=="0" (
        if "%HH%"=="03" (
            if "%MM%"=="00" (
                echo [!] It's Sunday 3:00 AM. Running Database Update...
                start "" cmd /c "..\backend\schedule_update.bat"
                :: We don't sleep here because the loop will sleep at the end anyway,
                :: but to prevent re-triggering within the same minute:
                timeout /t 65 /nobreak >nul
            )
        )
    )
)

echo Monitoring in background... (Checking every 60s)
timeout /t 60 /nobreak >nul
goto loop
