@echo off
REM ─────────────────────────────────────────────────────────────────────
REM  run_audit.bat  —  used by Windows Task Scheduler to run the daily
REM  Tally-vs-Supabase data-integrity check (audit.py) on the OFFICE PC,
REM  from a git clone at C:\sbdc-system. Mirrors run_sync.bat.
REM
REM  git pull first (non-fatal on failure — runs whatever code is already
REM  on disk rather than aborting, same reasoning as run_sync.bat: a bad
REM  network moment shouldn't skip a day's audit), then audit.py itself.
REM  All output is appended to logs\audit.log.
REM
REM  Scheduled as SYSTEM (see CLAUDE.md for the schtasks command) — git
REM  auth still works under SYSTEM because the PAT is embedded directly in
REM  the remote URL (.git/config), not stored in a per-user credential
REM  vault that SYSTEM wouldn't have access to.
REM ─────────────────────────────────────────────────────────────────────

setlocal

set "REPO_DIR=C:\sbdc-system"
set "VENV_PY=C:\sbdc-system\venv\Scripts\python.exe"
set "AUDIT_LOG=%REPO_DIR%\backend\logs\audit.log"

if not exist "%REPO_DIR%\backend\logs" mkdir "%REPO_DIR%\backend\logs"

cd /d "%REPO_DIR%"

echo [%date% %time%] git pull starting >> "%AUDIT_LOG%"
git pull >> "%AUDIT_LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] WARNING: git pull FAILED — continuing with existing code on disk >> "%AUDIT_LOG%"
) else (
    echo [%date% %time%] git pull OK >> "%AUDIT_LOG%"
)

cd /d "%REPO_DIR%\backend"

echo [%date% %time%] audit.py starting >> "%AUDIT_LOG%"
"%VENV_PY%" audit.py >> "%AUDIT_LOG%" 2>&1
set "AUDIT_EXIT=%ERRORLEVEL%"
echo [%date% %time%] audit.py finished, exit code %AUDIT_EXIT% >> "%AUDIT_LOG%"
echo. >> "%AUDIT_LOG%"

REM Exit code passed through from Python (0 = PASS, 1 = FAIL or couldn't run).
REM Task Scheduler marks the run as failed only on a non-zero exit code.
exit /b %AUDIT_EXIT%
