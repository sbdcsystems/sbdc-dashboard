@echo off
REM ─────────────────────────────────────────────────────────────────────
REM  run_audit.bat  —  used by Windows Task Scheduler to run the daily
REM  Tally-vs-Supabase data-integrity check (audit.py) on the OFFICE PC,
REM  from a git clone at C:\sbdc-system. Mirrors run_sync.bat.
REM
REM  git pull first (non-fatal on failure — runs whatever code is already
REM  on disk rather than aborting, same reasoning as run_sync.bat: a bad
REM  network moment shouldn't skip a day's audit).
REM
REM  Then a sync with --force-outstanding, THEN audit.py — in that order,
REM  back to back. audit.py compares live Tally against Supabase; if
REM  outstanding (Step 2/3) were left on its normal 3h throttle, Supabase
REM  could be reflecting a moment up to 3h stale while Tally reflects right
REM  now, and any bill entered in that gap would show up as a false FAIL
REM  rather than the harmless timing gap it actually is. --force-outstanding
REM  (not --full) keeps this to the one throttle that matters for the
REM  audit, without the cost of a full FY sales_history resweep every day.
REM
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

echo [%date% %time%] pre-audit sync (--force-outstanding) starting >> "%AUDIT_LOG%"
"%VENV_PY%" tally_sync_runner.py --force-outstanding >> "%AUDIT_LOG%" 2>&1
echo [%date% %time%] pre-audit sync finished, exit code %ERRORLEVEL% >> "%AUDIT_LOG%"

echo [%date% %time%] audit.py starting >> "%AUDIT_LOG%"
"%VENV_PY%" audit.py >> "%AUDIT_LOG%" 2>&1
set "AUDIT_EXIT=%ERRORLEVEL%"
echo [%date% %time%] audit.py finished, exit code %AUDIT_EXIT% >> "%AUDIT_LOG%"
echo. >> "%AUDIT_LOG%"

REM Exit code passed through from audit.py (0 = PASS, 1 = FAIL or couldn't
REM run) — the pre-audit sync's own exit code is logged but doesn't gate
REM this, same as run_sync.bat not aborting on a git pull failure: a
REM degraded sync still leaves audit.py something to compare against, and
REM the audit's own PASS/FAIL is what should decide whether Task Scheduler
REM reports this run as failed.
exit /b %AUDIT_EXIT%
