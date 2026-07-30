@echo off
setlocal

cd /d "%~dp0"
if not exist "data" mkdir "data"

chcp 65001 >nul

py -m app.main
set "EXIT_CODE=%ERRORLEVEL%"

endlocal & exit /b %EXIT_CODE%
