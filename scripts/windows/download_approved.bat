@echo off
setlocal
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" (
  echo Please run scripts\windows\setup_download.bat first.
  pause
  exit /b 1
)
if not exist "workspace\download_queue.jsonl" (
  echo Approve papers and export the download queue before running this script.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m autopaper.edge_download --config config\config.local.toml --queue workspace\download_queue.jsonl --output-dir paper_inbox\pdf --log workspace\downloads\results.jsonl
set "AUTOPAPER_DOWNLOAD_EXIT=%ERRORLEVEL%"
echo Download run finished. See workspace\STATUS.md for progress.
pause
exit /b %AUTOPAPER_DOWNLOAD_EXIT%
