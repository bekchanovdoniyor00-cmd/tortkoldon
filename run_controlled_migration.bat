@echo off
setlocal
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Migration blocked: 4-C Python environment was not found.
    exit /b 1
)
if not defined ERP_CONFIG_PATH (
    echo Migration blocked: ERP_CONFIG_PATH is not configured.
    exit /b 1
)
if not defined ERP_ALLOW_MIGRATIONS (
    echo Migration blocked: explicit ERP_ALLOW_MIGRATIONS confirmation is required.
    exit /b 1
)
"%PY%" "%~dp0migrate_database.py" --config "%ERP_CONFIG_PATH%"
exit /b %ERRORLEVEL%
