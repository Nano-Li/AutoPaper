@echo off
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" (
  echo Download environment is missing. Run setup_download.bat while Clash is available.
  pause
  exit /b 1
)
echo AutoPaper direct-download test
echo Close Clash first, then press any key to continue.
pause >nul
.venv\Scripts\python.exe -m autopaper.download --config config\config.local.toml --queue examples\download_queue.jsonl
echo.
pause
