@echo off
setlocal
cd /d "%~dp0..\.."
set "EDGE_EXE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
set "PROFILE_DIR=%CD%\browser_profiles\edge-autopaper"

if not exist "%EDGE_EXE%" (
  echo Microsoft Edge was not found at the expected location.
  pause
  exit /b 1
)

if not exist "%PROFILE_DIR%" mkdir "%PROFILE_DIR%"
echo Opening the dedicated AutoPaper Edge profile.
echo In this window, verify that both PRL and ScienceDirect pages open.
echo Manually click each PDF button once and confirm campus access works.
echo Then close every AutoPaper Edge window before running download_edge_test.bat.
start "" "%EDGE_EXE%" --user-data-dir="%PROFILE_DIR%" --profile-directory=Default --no-first-run --no-default-browser-check "https://doi.org/10.1103/2yzc-fsm3" "https://www.sciencedirect.com/science/article/abs/pii/S138589471931736X"
endlocal
