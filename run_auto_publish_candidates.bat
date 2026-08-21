@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist "data\election_candidates\tainan_2026\logs" mkdir "data\election_candidates\tainan_2026\logs"
"C:\Users\User\AppData\Local\Programs\Python\Python312\python.exe" -m app.election_candidates.auto_publish_candidates --config config/election_candidate_pipeline.yaml >> data\election_candidates\tainan_2026\logs\auto_publish_candidates.log 2>&1
endlocal & exit /b %ERRORLEVEL%
