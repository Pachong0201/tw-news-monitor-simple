@echo off
cd /d "%~dp0"
py -m app.main --test-feishu-file
echo.
pause
exit /b %ERRORLEVEL%
