@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 尚未安装依赖，请先运行 setup.ps1。
  pause
  exit /b 1
)
set PYTHONPATH=%~dp0src
".venv\Scripts\python.exe" -m unittest discover -s tests -v
pause

