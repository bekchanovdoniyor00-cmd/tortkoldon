@echo off
setlocal
cd /d "%~dp0"
if not "%ERP_SERVICE_APPROVED%"=="INSTALL-APPROVED" (
    echo Installation blocked. Review WINDOWS_PRODUCTION_GUIDE.md, then set
    echo ERP_SERVICE_APPROVED=INSTALL-APPROVED in an elevated console.
    exit /b 2
)
if not exist "%~dp0ToRtkoERP.exe" (
    echo Installation blocked: place the verified WinSW executable at ToRtkoERP.exe.
    exit /b 2
)
if not exist "%~dp0ToRtkoERP.xml" (
    echo Installation blocked: copy and configure ToRtkoERP.xml.example as ToRtkoERP.xml.
    exit /b 2
)
if not defined ERP_SECRET_FILE (
    echo Installation blocked: configure the protected ERP_SECRET_FILE for the service.
    exit /b 2
)
if not exist "%ERP_SECRET_FILE%" (
    echo Installation blocked: ERP_SECRET_FILE does not exist or cannot be read.
    exit /b 2
)
if /I not "%ERP_MODE%"=="production" (
    echo Installation blocked: ERP_MODE must be production.
    exit /b 2
)
"%~dp0ToRtkoERP.exe" install
exit /b %ERRORLEVEL%
