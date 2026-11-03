@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0ToRtkoERP.exe" (
    echo Service command failed: ToRtkoERP.exe was not found.
    exit /b 2
)
if "%~1"=="" (
    echo Usage: manage_windows_service.bat status^|start^|stop^|restart
    exit /b 2
)
if /I "%~1"=="status" goto status
if /I "%~1"=="start" goto start
if /I "%~1"=="stop" goto stop
if /I "%~1"=="restart" goto restart
echo Invalid service command: %~1
echo Usage: manage_windows_service.bat status^|start^|stop^|restart
exit /b 2

:status
"%~dp0ToRtkoERP.exe" status
exit /b %ERRORLEVEL%

:start
"%~dp0ToRtkoERP.exe" start
exit /b %ERRORLEVEL%

:stop
"%~dp0ToRtkoERP.exe" stop
exit /b %ERRORLEVEL%

:restart
"%~dp0ToRtkoERP.exe" restart
exit /b %ERRORLEVEL%
