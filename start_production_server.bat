@echo off
setlocal
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Server startup blocked: 4-C Python environment was not found.
    exit /b 1
)
if not defined ERP_CONFIG_PATH (
    echo Server startup blocked: ERP_CONFIG_PATH is not configured.
    exit /b 1
)
if not defined ERP_SECRET_KEY if not defined ERP_SECRET_FILE (
    echo Server startup blocked: provide ERP_SECRET_KEY or protected ERP_SECRET_FILE.
    exit /b 1
)
if defined ERP_SECRET_FILE if not exist "%ERP_SECRET_FILE%" (
    echo Server startup blocked: ERP_SECRET_FILE does not exist or cannot be read.
    exit /b 1
)
if /I not "%ERP_MODE%"=="production" (
    echo Server startup blocked: ERP_MODE must be production.
    exit /b 1
)
"%PY%" -u "%~dp0production_server.py" --config "%ERP_CONFIG_PATH%"
exit /b %ERRORLEVEL%
