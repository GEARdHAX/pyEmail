@echo off
rem Daily pyEmail routine. Double-click for the report, or run:  daily.cmd --send
cd /d "%~dp0"
python run.py daily %*
echo.
pause
