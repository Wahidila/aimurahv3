@echo off
setlocal
set SCRIPT_DIR=%~dp0
set PYTHONPATH=%SCRIPT_DIR%;%PYTHONPATH%
if exist "%SCRIPT_DIR%.venv\Scripts\python.exe" (
    set "PYCMD=%SCRIPT_DIR%.venv\Scripts\python.exe"
) else (
    set PYCMD=python
    where python >nul 2>nul
    if errorlevel 1 set PYCMD=py
)
"%PYCMD%" -m aimurah %*
exit /b %ERRORLEVEL%
