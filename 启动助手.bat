@echo off
rem ============================================================
rem  IMPORTANT: keep this file PURE ASCII. Do not put Chinese here.
rem
rem  Why: cmd.exe reads .bat files using the ACTIVE console code page.
rem  If this file contains multi-byte characters and the code page does
rem  not match the file encoding, cmd mis-parses the commands -- `echo`
rem  gets split into `ho`, and the script dies silently on double-click.
rem  The symptom is "double-click does nothing at all".
rem
rem  So: all user-facing Chinese lives in Python instead. Python writes
rem  to the console through the wide-char API, which is unaffected by
rem  the code page.
rem ============================================================
setlocal
cd /d "%~dp0"
title Compliance Self-Check Assistant

if not exist ".venv\Scripts\python.exe" goto first_run

".venv\Scripts\python.exe" -m app.main
goto done

:first_run
rem First run: hand over to Python so the user sees proper Chinese messages.
where python >nul 2>&1
if errorlevel 1 goto no_python

python "tools\first_run.py"
if errorlevel 1 goto setup_failed
goto done

:no_python
echo.
echo   ============================================================
echo    Python was not found on this computer.
echo   ============================================================
echo.
echo    1. Download Python 3.10 or newer from:
echo       https://www.python.org/downloads/
echo.
echo    2. During installation, CHECK this box:
echo       "Add python.exe to PATH"
echo.
echo    3. Then double-click this file again.
echo.
pause
exit /b 1

:setup_failed
echo.
echo   Setup did not finish. Please read the messages above.
echo.
pause
exit /b 1

:done
echo.
echo Service stopped. You can close this window.
pause