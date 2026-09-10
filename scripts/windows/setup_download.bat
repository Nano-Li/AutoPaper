@echo off
setlocal
cd /d "%~dp0..\.."
set "REF_DIR=%CD%\_references\ref-downloader"
set "REF_COMMIT=1cc667351875410197acf4471e667ac272a0092e"

echo Preparing the AutoPaper download environment...
if not exist ".venv\Scripts\python.exe" python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
if errorlevel 1 (
  echo Setup failed. Keep Clash available and try again.
  pause
  exit /b 1
)

if not exist "%REF_DIR%\skills\ref-downloader\scripts\download_refs.py" (
  where git >nul 2>nul
  if errorlevel 1 (
    echo Git is required to obtain the pinned ref-downloader dependency.
    pause
    exit /b 1
  )
  if exist "%REF_DIR%" (
    echo The ref-downloader directory exists but is incomplete:
    echo   %REF_DIR%
    echo Move or remove that directory manually, then run setup again.
    pause
    exit /b 1
  )
  if not exist "%CD%\_references" mkdir "%CD%\_references"
  echo Downloading the pinned ref-downloader dependency...
  git clone https://github.com/ltczding-gif/ref-downloader.git "%REF_DIR%"
  if errorlevel 1 (
    echo Could not clone ref-downloader. Keep Clash available and try again.
    pause
    exit /b 1
  )
  git -C "%REF_DIR%" checkout --detach "%REF_COMMIT%"
  if errorlevel 1 (
    echo Could not select the supported ref-downloader version.
    pause
    exit /b 1
  )
)

if not exist "config\config.local.toml" (
  copy /Y "config\config.example.toml" "config\config.local.toml" >nul
  echo Created config\config.local.toml from the public template.
)

echo Setup complete. The Python environment and Edge download dependency are ready.
echo Edit config\config.local.toml before the first search.
pause
endlocal
