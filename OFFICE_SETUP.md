# Office PC setup — converting to a git clone

The office PC's `sbdc-system` folder today is just a plain copy of files —
not a git clone — which is why `run_sync.bat`'s `git pull` step won't work
until this is done once. This doc walks through doing that safely (without
losing `.env` or the sync logs) and re-pointing Task Scheduler at the new
`run_sync.bat`, which now runs every 30 minutes from 10:00 to 18:30.

Do this on the **office PC itself**, logged in as whichever Windows user
Task Scheduler runs the sync as. Everything below is PowerShell — right-click
the Start menu → "Windows PowerShell" (does not need to be Administrator
except where noted for the Task Scheduler step).

---

## 1. Disable the current scheduled task first

Stop it from firing mid-migration:

1. Open **Task Scheduler** (Start menu → search "Task Scheduler").
2. Find the task (likely named **SBDC Tally Sync**).
3. Right-click it → **Disable**. Leave the window open — you'll come back to it in step 5.

## 2. Back up the existing folder

Don't delete anything until a real sync has succeeded from the new clone (step 6).

```powershell
# Adjust the path if the office copy lives somewhere other than C:\sbdc-system
Rename-Item "C:\sbdc-system" "C:\sbdc-system-old-backup"
```

If PowerShell says the folder is in use, make sure no sync is currently
running (check Task Manager for `python.exe` / `tally_sync_runner.py`) and
that no one has the folder open in File Explorer, then retry.

## 3. Clone the repo fresh

```powershell
cd C:\
git clone https://github.com/sbdcsystems/sbdc-system.git
```

If this prompts for credentials, sign in with the GitHub account that has
access to `sbdcsystems/sbdc-system` (username + a Personal Access Token as
the password — GitHub no longer accepts your account password here). If you
want `git pull` in `run_sync.bat` to run unattended without prompting every
30 minutes, set up **Git Credential Manager** first (it usually ships with
Git for Windows already and just needs one interactive login to cache the
token) — do a manual `git pull` once inside `C:\sbdc-system` right after
cloning and confirm it doesn't ask again.

## 4. Copy back what git doesn't track

These are all deliberately excluded from the repo (`.gitignore`) because
they're either credentials or machine-local runtime data — a fresh clone
will not have them, so copy them from the backup:

```powershell
# Credentials — required, the sync will not run without this
Copy-Item "C:\sbdc-system-old-backup\.env" "C:\sbdc-system\.env"

# Sync logs — optional, keeps history
Copy-Item "C:\sbdc-system-old-backup\backend\logs" "C:\sbdc-system\backend\logs" -Recurse -ErrorAction SilentlyContinue

# Last-run status — optional, only affects the "last successful sync" display until the next run overwrites it
Copy-Item "C:\sbdc-system-old-backup\backend\last_sync_status.json" "C:\sbdc-system\backend\" -ErrorAction SilentlyContinue

# Local outstanding XML backup — optional, only used by --from-local (not used in scheduled runs)
Copy-Item "C:\sbdc-system-old-backup\backend\tally_with_dates.xml" "C:\sbdc-system\backend\" -ErrorAction SilentlyContinue
```

Do **not** copy `sync_state.json` or `sync.lock` — those are new to this
version of the code and don't exist on the old install. `sync_state.json`
gets created automatically on the first live run; `sync.lock` only exists
transiently while a sync is actually running.

Do **not** copy the old `venv\` folder — it's tied to the old folder's
absolute path and excluded from git; create a fresh one in the next step.

## 5. Create the Python virtual environment

```powershell
cd C:\sbdc-system
python -m venv venv
.\venv\Scripts\activate.bat
pip install -r backend\requirements.txt
```

## 6. Verify before trusting it

```powershell
cd C:\sbdc-system\backend
..\venv\Scripts\activate.bat
python tally_sync_runner.py --from-local --dry-run
```

This should complete without errors using the copied `tally_with_dates.xml`
(no live Tally connection needed for this check). Once you're confident, do
one real run with Tally open and the correct company loaded:

```powershell
python tally_sync_runner.py
```

Check the log it prints (`backend\logs\sync_<timestamp>.log`) and
`backend\last_sync_status.json` for `"status": "success"` (or `"partial"` —
that's still fine, it just means one of the secondary steps like collections
had a hiccup; `"failed"` means look at the log).

Only after this succeeds, delete the backup:

```powershell
Remove-Item "C:\sbdc-system-old-backup" -Recurse -Force
```

## 7. Point Task Scheduler at the new setup, every 30 min from 10:00–18:30

The task's **Action** needs to run `C:\sbdc-system\backend\run_sync.bat`
(already correct if the task already pointed there — it's the same path as
before, just now a git clone). What needs to change is the **trigger**.

### Option A — PowerShell (fastest, exact)

Run PowerShell **as Administrator** (right-click → "Run as administrator"):

```powershell
$taskName = "SBDC Tally Sync"   # adjust if your task has a different name — check with: Get-ScheduledTask | Format-Table TaskName

$action = New-ScheduledTaskAction -Execute "C:\sbdc-system\backend\run_sync.bat" `
            -WorkingDirectory "C:\sbdc-system\backend"

$trigger = New-ScheduledTaskTrigger -Once -At 10:00AM `
            -RepetitionInterval (New-TimeSpan -Minutes 30) `
            -RepetitionDuration (New-TimeSpan -Hours 8 -Minutes 35)
            # 8h35m, not 8h30m — makes sure the 18:30 run itself fires
            # (repetition duration boundaries can be exclusive) without
            # adding a spurious 19:00 run.

Set-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger
```

If that errors with "task not found", the task doesn't exist yet under that
name — create it instead:

```powershell
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Description "SBDC Tally -> Supabase sync, every 30 min, 10:00-18:30"
```

Then re-enable it (it may register as enabled already, but confirm):

```powershell
Enable-ScheduledTask -TaskName $taskName
```

Verify:

```powershell
Get-ScheduledTaskInfo -TaskName $taskName
(Get-ScheduledTask -TaskName $taskName).Triggers
```

### Option B — Task Scheduler GUI (if you'd rather click through it)

1. In Task Scheduler, right-click **SBDC Tally Sync** → **Properties**.
2. **Triggers** tab → select the existing trigger → **Edit**.
3. Set **Begin the task**: On a schedule, **Daily**, start time **10:00:00 AM**, recur every **1 day**.
4. Check **Repeat task every:** and set it to **30 minutes**.
5. Set **for a duration of:** — pick **8 hours** from the dropdown if that's
   the closest option available on this Windows version (some versions only
   offer preset durations); if you can type a custom value into that field,
   use **8 hours 35 minutes** for the same reason as Option A. If only whole
   hours are selectable, **8 hours** means the last run is at 18:00 instead
   of 18:30 — acceptable, but Option A gets the exact 18:30 slot.
6. Click **OK**, then **Enable** the task if it's still disabled from step 1.

---

## Notes

- Office hours: the office closes at 7 PM and the PC is switched off
  overnight — this schedule (10:00–18:30) deliberately never triggers a run
  when the PC won't be there to run it or answer for Tally.
- `run_sync.bat`'s `git pull` failing (no network, merge conflict, etc.) does
  **not** stop the sync — it logs the failure to
  `backend\logs\run_sync_bat.log` and runs whatever code is already on disk.
  Check that log occasionally to make sure the office copy isn't silently
  stuck behind on fixes.
- If you ever need to update the office code without waiting for the next
  scheduled run, just run `git pull` yourself from `C:\sbdc-system`.
