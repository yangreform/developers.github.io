@echo off
title Webhook_Watchdog

:loop
cls
echo ------------------------------------------
echo Check Time: %date% %time%
echo Checking Processes...
echo ------------------------------------------

wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "main.py" >nul
if %errorlevel% equ 0 (
    echo [OK] main.py is running.
) else (
    echo [WARNING] main.py is NOT running! Restarting MAXIMIZED...
    start "main.py" /min py .\main.py
)


wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "router.py" >nul
if %errorlevel% equ 0 (
    echo [OK] router.py is running.
) else (
    echo [WARNING] router.py is NOT running! Restarting MINIMIZED...
    start "router" /min py .\router.py
)

wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "q.py" >nul
if %errorlevel% equ 0 (
    echo [OK] q.py is running.
) else (
    echo [WARNING] q.py is NOT running! Restarting MAXIMIZED...
    start "q.py" /max py .\q.py
)

wmic process where "name='ngrok.exe'" get commandline 2>nul | find "http 5000" >nul
if %errorlevel% equ 0 (
    echo [OK] ngrok http 5000 is running.
) else (
    echo [WARNING] ngrok is NOT running! Restarting MINIMIZED...
    start "ngrok_tunnel" /min "C:\Users\Administrator\Desktop\docker_mc\CT\ngrok\ngrok.exe" http 5000
)

echo.
echo Waiting 30 seconds for the next check...
timeout /t 10 >nul

goto loop
