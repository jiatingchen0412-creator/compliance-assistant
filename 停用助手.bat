@echo off
rem ============================================================
rem  IMPORTANT: keep this file PURE ASCII.
rem  Reason: see the comment block in the launcher .bat.
rem  Chinese messages are printed by tools/stop_service.py instead.
rem ============================================================
setlocal
cd /d "%~dp0"
title Stop Compliance Self-Check Assistant

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "tools\stop_service.py"
    goto done
)

rem Fallback when the venv does not exist yet
set PORT=8765
echo Stopping service on port %PORT% ...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
    if not errorlevel 1 echo   Stopped process PID %%a
)
echo Done.

:done
timeout /t 3 >nul
