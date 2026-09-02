@echo on
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
