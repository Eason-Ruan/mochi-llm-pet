@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First run: uv sync --python 3.12 --locked
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m deskpet %*
set "MOCHI_EXIT=%ERRORLEVEL%"
if not "%MOCHI_EXIT%"=="0" pause
exit /b %MOCHI_EXIT%
