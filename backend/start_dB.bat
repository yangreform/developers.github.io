@echo off
title Webhook_Watchdog

:loop
cls
echo ------------------------------------------
echo Check Time: %date% %time%
echo Checking Processes...
echo ------------------------------------------

:: Check for Sunday Database Update at 03:00
for /f "skip=1" %%d in ('wmic path win32_localtime get dayofweek') do (
    if "%%d"=="0" (
        if "%HH%"=="03" (
            if "%MM%"=="00" (
                echo [!] It's Sunday 3:00 AM. Running Database Update...
                :: start "" cmd /c "..\backend\schedule_update.bat"
                :: We don't sleep here because the loop will sleep at the end anyway,
                :: but to prevent re-triggering within the same minute:
                timeout /t 65 /nobreak >nul
            )
        )
    )
)


wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "fill_missing_developers_selenium.py" >nul
if %errorlevel% equ 0 (
    echo [OK] fill_missing_developers_selenium.py is running.
) else (
    echo [WARNING] fill_missing_developers_selenium.py is NOT running! Restarting MAXIMIZED...
    start "fill_missing_developers_selenium.py" /min py fill_missing_developers_selenium.py
)

echo.
echo Waiting 30 seconds for the next check...
timeout /t 100 >nul

goto loop
