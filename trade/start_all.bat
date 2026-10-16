
@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title Trade_System_Watchdog_and_Scheduler
cd /d "%~dp0"

:: 初始化定時任務的鎖定狀態變數（防止一分鐘內重複觸發）
set "last_killed_hour=-1"
set "last_trigger_33_hour=-1"
set "last_trigger_56_hour=-1"

:loop
cls
set "current_time=%time: =0%"
set "HH=%current_time:~0,2%"
set "MM=%current_time:~3,2%"

echo =======================================================
echo    Check Time: %date% %time%
echo =======================================================

:: 取得當前星期幾的數字 (0-6，WMI 中 0是週日，1是週一... 以此類推)
:: 修正：雙層迴圈設計，完美剃除 WMIC 隱藏的 CR 斷行字元，防止後續 if 結構損壞
for /f "tokens=2 delims==" %%a in ('wmic path win32_localtime get dayofweek /value 2^>nul') do (
    set "temp_DOW=%%a"
    for /f "delims=" %%b in ("!temp_DOW!") do set "DOW=%%b"
)

:: ---------------------------------------------------------
:: 1. 定時排程區塊 (Scheduled Tasks)
:: ---------------------------------------------------------

:: 統一在每小時的 33 分執行各項任務
if "%MM%"=="33" (
    
    :: [每小時重啟 q.py 機制] 確保一小時只殺一次
    if "!last_killed_hour!" neq "%HH%" (
        set "last_killed_hour=%HH%"
        echo [!] Scheduled restart time %HH%:%MM% reached.
        wmic process where "name='python.exe' and commandline like '%%q.py%%'" call terminate >nul 2>&1
        wmic process where "name='py.exe' and commandline like '%%q.py%%'" call terminate >nul 2>&1
        echo [%time%] Processes terminated. Watchdog will restart them now...
        timeout /t 3 /nobreak >nul
    )

    if "!last_trigger_32_hour!" neq "%HH%" (
        set "last_trigger_32_hour=%HH%"
        set "MATCH=0"
        if "%HH%"=="11" set "MATCH=1"
        if "%HH%"=="23" set "MATCH=1"
        if "!MATCH!"=="1" (
            wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "opinion.py" >nul
            if not errorlevel 1 (
                echo [OK] opinion.py is running.
            ) else (
                start "opinion" /max py .\opinion.py
            )
        )

        if "%HH%"=="21" (
            wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "DTE0_SELL.py" >nul
            if not errorlevel 1 (
                echo [OK] DTE0_SELL.py is running.
            ) else (
                start "DTE0_SELL" /max py .\DTE0_SELL.py
            )
        )

	if "%HH%"=="22" (
	    wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "insider.py" >nul
	    if !errorlevel! equ 0 (
	        echo [OK] insider.py is running.
	    ) else (
	        start "insider" /max py .\insider.py
	    )
	)


        if "%HH%"=="23" (
            wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "long-call-options-screener.py" >nul
            if not errorlevel 1 (
                echo [OK] long-call-options-screener.py is running.
            ) else (
                start "long-call-options-screener" /max py .\long-call-options-screener.py
            )
        )

        echo =======================================================
        :: 清除變數以免影響其他天
        set "RUN_SHIOAJI=0"
        
        :: [週三(3) 或 週五(5) 執行 Shioaji 排程]
        echo 當前星期數字為: !DOW!
        if "!DOW!"=="3" set "RUN_SHIOAJI=1"
        if "!DOW!"=="5" set "RUN_SHIOAJI=1"
        
        if "!RUN_SHIOAJI!"=="1" (
            if "%HH%"=="16" (
                wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "open_Shioaji.py" >nul
                if not errorlevel 1 (
                    echo [OK] open_Shioaji.py is running.
                ) else (
                    echo [START] open_Shioaji.py is not running, launching now...
                    start "open_Shioaji" /max py .\open_Shioaji.py
                )
            )
        )
    )
)

:: [改用具體時間取代 timeout 600，避免腳本死鎖卡住]
:: 01:24 執行完 long-call 後，10 分鐘後即 01:34 執行 bull-put.py
if "%MM%"=="58" (
    if "%HH%"=="21" (
    	wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "bull-put.py" >nul
    	if !errorlevel! equ 0 (
    	    echo [OK] bull-put.py is running.
    	) else (
    	    start "bull-put.py" /max py .\bull-put.py
    	)
    )
)

:: ---------------------------------------------------------
:: 2. 常駐進程守門員區塊 (Process Watchdog & Auto-Restart)
:: ---------------------------------------------------------
echo.
wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "router.py" >nul
if not errorlevel 1 (
    echo  [OK] router.py is running.
) else (
    start "router" /min py .\router.py
)

echo.
wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "q.py" >nul
if not errorlevel 1 (
    echo  [OK] q.py is running.
) else (
    start "q.py" /min py .\q.py
)

echo.
wmic process where "name='ngrok.exe'" get commandline 2>nul | find "http 5000" >nul
if not errorlevel 1 (
    echo  [OK] ngrok http 5000 is running.
) else (
    start "ngrok_tunnel" /min "C:\Users\Administrator\Desktop\docker_mc\CT\ngrok\ngrok.exe" http 5000
)

echo.
echo Monitoring in background... (Next check in 10s)
timeout /t 10 >nul

goto loop



::       if "%HH%"=="21" (
::           wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "download_op_price.py" >nul
::
::           if not errorlevel 1 (
::               echo [OK] download_op_price.py is running.
::           ) else (
::               start "download_op_price" /max py .\download_op_price.py --no-line
::           )
::       )
::	if "%HH%"=="22" (
::	    wmic process where "name='py.exe' or name='python.exe'" get commandline 2>nul | find "uoa.py" >nul
::	    if !errorlevel! equ 0 (
::	        echo [OK] uoa.py is running.
::	    ) else (
::	        start "uoa" /max py .\uoa.py
::	    )
::	)
