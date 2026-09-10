@echo off
setlocal
cd /d "%~dp0..\.."

if not exist ".venv\Scripts\python.exe" (
  echo Download environment is missing. Run setup_download.bat first.
  pause
  exit /b 1
)
if not exist "_references\ref-downloader\skills\ref-downloader\scripts\download_refs.py" (
  echo The ref-downloader reference checkout is missing.
  pause
  exit /b 1
)
if not exist "browser_profiles\edge-autopaper\Default" (
  echo The dedicated Edge profile has not been initialized.
  echo Run prepare_edge_profile.bat first, verify access, and close that Edge window.
  pause
  exit /b 1
)

.venv\Scripts\python.exe -m autopaper.ref_downloader_adapter --queue examples\download_queue.jsonl --output-dir paper_inbox\ref_downloader_test
if errorlevel 1 (
  pause
  exit /b 1
)

set "REF_DOWNLOADER_CONFIG=%CD%\config\ref_downloader_edge.toml"
set "REF_DOWNLOADER_BROWSER=edge"
echo.
echo The visible Edge window is controlled by ref-downloader v0.4.1.
echo Keep Clash closed. If a normal verification page appears, complete it manually.
echo Close every AutoPaper Edge window now, then continue.
pause

.venv\Scripts\python.exe "_references\ref-downloader\skills\ref-downloader\scripts\download_refs.py" "paper_inbox\ref_downloader_test"
echo.
echo Edge test finished. Results are under paper_inbox\ref_downloader_test.
pause
endlocal
