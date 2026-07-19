@echo off
echo ========================================
echo   LandlordSG Weekly Database Update
echo ========================================
echo Started at: %date% %time%

:: Ensure we are in the correct directory
cd /d "%~dp0"

:: Run the python script
python update_db.py

echo.
echo Finished at: %date% %time%
echo ========================================
