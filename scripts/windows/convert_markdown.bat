@echo off
setlocal
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" (
  echo Please run scripts\windows\setup_download.bat first.
  pause
  exit /b 1
)
if not exist "config\mineru.local.toml" (
  echo Copy config\mineru.example.toml to config\mineru.local.toml and fill in your token.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m autopaper.mineru --dry-run
if errorlevel 1 (
  pause
  exit /b 1
)
echo This will upload pending PDFs to MinerU and resume saved tasks.
set "MINERU_CONFIRM="
set /p "MINERU_CONFIRM=Type YES to continue: "
if /I not "%MINERU_CONFIRM%"=="YES" exit /b 0
.venv\Scripts\python.exe -m autopaper.mineru
set "MINERU_EXIT=%ERRORLEVEL%"
echo Markdown run finished. See workspace\STATUS.md for progress.
echo Exit code: %MINERU_EXIT% (0=complete, 2=unfinished, 130=interrupted)
pause
exit /b %MINERU_EXIT%
