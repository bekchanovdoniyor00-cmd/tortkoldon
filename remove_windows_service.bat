@echo off
setlocal
cd /d "%~dp0"
if not "%ERP_SERVICE_APPROVED%"=="REMOVE-APPROVED" (
    echo Removal blocked. Review WINDOWS_PRODUCTION_GUIDE.md, then set
    echo ERP_SERVICE_APPROVED=REMOVE-APPROVED in an elevated console.
    exit /b 2
)
if not exist "%~dp0ToRtkoERP.exe" (
    echo Removal blocked: ToRtkoERP.exe was not found.
    exit /b 2
)
"%~dp0ToRtkoERP.exe" stop
if errorlevel 1 (
    echo Service stop failed; the service was not uninstalled.
    exit /b 1
)
"%~dp0ToRtkoERP.exe" uninstall
exit /b %ERRORLEVEL%
