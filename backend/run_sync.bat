@echo off
REM ─────────────────────────────────────────────────────────────────────
REM  run_sync.bat  —  used by Windows Task Scheduler to run the Tally sync
REM  on the OFFICE PC, from a git clone at C:\sbdc-system.
REM
REM  See OFFICE_SETUP.md (repo root) for how to turn the office folder into
REM  a git clone the first time, and how to point Task Scheduler at this
REM  file with a 30-minute recurring trigger from 10:00 to 18:30.
REM
REM  Each run: git pull first (so office always runs the latest fixes), then
REM  the sync itself. If the pull fails — no network, merge conflict, GitHub
REM  auth issue — this does NOT abort: it logs the failure and runs the sync
REM  with whatever code is already on disk, so a bad network moment never
REM  stops staff from getting a sync.
REM ─────────────────────────────────────────────────────────────────────

setlocal

set "REPO_DIR=C:\sbdc-system"
set "VENV_DIR=C:\sbdc-system\venv"
set "PULL_LOG=%REPO_DIR%\backend\logs\run_sync_bat.log"

if not exist "%REPO_DIR%\backend\logs" mkdir "%REPO_DIR%\backend\logs"

cd /d "%REPO_DIR%"

echo [%date% %time%] git pull starting >> "%PULL_LOG%"
git pull
if errorlevel 1 (
    echo [%date% %time%] WARNING: git pull FAILED — continuing with existing code on disk >> "%PULL_LOG%"
    echo WARNING: git pull failed — continuing with existing code on disk
) else (
    echo [%date% %time%] git pull OK >> "%PULL_LOG%"
)

cd /d "%REPO_DIR%\backend"
call "%VENV_DIR%\Scripts\activate.bat"
python tally_sync_runner.py

REM Exit code is passed through from Python (0 = success/partial, 1 = failed).
REM Task Scheduler marks the run as failed only on a non-zero exit code.
exit /b %ERRORLEVEL%
