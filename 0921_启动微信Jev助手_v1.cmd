@echo off
setlocal
set "APP_DIR=%~dp0"
cd /d "%APP_DIR%" || (
  echo Failed to open the application directory.
  pause
  exit /b 1
)
set "PYTHONW=%APP_DIR%.venv\Scripts\pythonw.exe"
set "ENTRY=%APP_DIR%main.pyw"
if not exist "%PYTHONW%" (
  echo Dependencies are missing. Run setup.ps1 first.
  pause
  exit /b 1
)
if not exist "%ENTRY%" (
  echo main.pyw was not found.
  pause
  exit /b 1
)
start "" "%PYTHONW%" "%ENTRY%"
if errorlevel 1 (
  echo Failed to start WeChat Jev Assistant.
  pause
  exit /b 1
)
exit /b 0
