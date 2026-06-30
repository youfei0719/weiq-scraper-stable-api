@echo off
setlocal

cd /d %~dp0

if not exist ".venv\Scripts\python.exe" (
  echo [错误] 未检测到 .venv\Scripts\python.exe，请先完成依赖安装。
  pause
  exit /b 1
)

".venv\Scripts\python.exe" scraper.py
