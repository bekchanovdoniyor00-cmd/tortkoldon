@echo off
setlocal
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Backup stopped: 4-C Python environment was not found.
    exit /b 1
)
"%PY%" "%~dp0daily_sqlite_backup.py" --config "%~dp0backup_config.json"
exit /b %ERRORLEVEL%
