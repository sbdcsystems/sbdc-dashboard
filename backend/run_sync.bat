@echo off
REM ─────────────────────────────────────────────────────────────────────
REM  run_sync.bat  —  used by Windows Task Scheduler to run the daily sync
REM
REM  Task Scheduler setup (do this once):
REM    1. Open "Task Scheduler" from the Start menu
REM    2. Click "Create Basic Task..." in the right panel
REM    3. Name: SBDC Tally Sync
REM    4. Trigger: Daily — pick a time when Tally is open (e.g. 9:00 AM)
REM    5. Action: Start a Program
REM    6. Program/script: C:\Users\vsome\Desktop\sbdc-system\backend\run_sync.bat
REM    7. "Start in (optional)": C:\Users\vsome\Desktop\sbdc-system\backend
REM    8. Click Finish
REM
REM  To run manually right now:
REM    Double-click this file, or run it from a terminal
REM ─────────────────────────────────────────────────────────────────────

cd /d C:\Users\vsome\Desktop\sbdc-system\backend
call ..\venv\Scripts\activate.bat
python tally_sync_runner.py

REM Exit code is passed through from Python (0 = success, 1 = failure)
REM Task Scheduler will mark the run as failed if exit code is non-zero.
exit /b %ERRORLEVEL%
