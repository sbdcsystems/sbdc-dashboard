# SBDC System — Claude context

Supreme Balaji Dye Chem (SBDC) internal collections dashboard.
Python pipeline (Tally → Supabase) + Vite/React frontend.

---

## Standing instructions for Claude

- **New Supabase table**: add a `VERCEL ACTION NEEDED` reminder at the top of the response listing what SQL grants are needed, and add the table to the schema section below.
- **New frontend env variable (`VITE_*`)**: add a `VERCEL ACTION NEEDED` reminder at the top of the response listing the variable name and value to add in Vercel → Project Settings → Environment Variables, and add it to the Vite env vars section below.

Format: `VERCEL ACTION NEEDED: [exact action required]` — one line per item, at the very top of the response before any other content.

---

## Project layout

```
sbdc-system/
  .env                        ← credentials (project root — NOT inside backend/)
  venv/                       ← Python virtualenv (root level)
  backend/
    tally_sync_runner.py      ← main sync script (Steps 1-10)
    compare_step3.py          ← standalone read-only old-vs-new Step 3 sign-logic comparison (see step3-sign-fix branch)
    probe_alterid.py          ← standalone diagnostic: does this Tally install support AlterID filtering? Run before flipping ALTERID_SYNC_ENABLED
    full_customer_import.py   ← one-time ledger master import (reference only)
    assign_one.py             ← one-off manual assignment script
    add_customers.py          ← one-off customer insert script
    check_jsonb.py            ← diagnostic: inspect daily_sales JSONB
    probe_tally_reports.py    ← diagnostic: test Tally report types
    logs/                     ← sync logs (sync_YYYYMMDD_HHMMSS.log)
    last_sync_status.json     ← written after every sync run (overall + per-step status, see below)
    sync_state.json           ← sales_history rotation/throttle state incl. last_alterid, last_ledger_master_run (not committed — machine-local runtime state)
    sync.lock                 ← overlap guard, present only while a sync is running (not committed)
    debug_skipped_vouchers.xml ← Phase 4 debug capture, written when Step 10 skips a voucher with no parseable amount (not committed)
    tally_with_dates.xml      ← local XML backup for --from-local mode
  frontend/
    src/
      App.jsx                 ← entire dashboard UI (single component)
      App.css                 ← all styles
      lib/supabaseClient.js   ← supabase JS client init
    index.html
    vite.config.js
```

---

## Running scripts

**Always run backend scripts from the `backend/` directory:**

```powershell
cd C:\Users\vsome\Desktop\sbdc-system\backend
..\venv\Scripts\activate.bat

# Full sync (must be on office network with Tally open):
# — heavy steps (outstanding, ledger master, sales_history) are throttled;
#   see "Load-reduction throttles" below for exactly when each one fires.
python tally_sync_runner.py

# Force a full FY sales_history sweep + deletion reconciliation now:
python tally_sync_runner.py --full

# Parse local XML backup without Tally connection (outstanding only — Steps 9/9b/10 skipped, can't reach Tally):
python tally_sync_runner.py --from-local

# Dry run (no DB writes, no Supabase sync_status write, no throttle state persisted either):
python tally_sync_runner.py --from-local --dry-run

# One-time diagnostic before enabling ALTERID_SYNC_ENABLED — see "Load-reduction throttles":
python probe_alterid.py
```

A `sync.lock` file prevents two runs overlapping — if you see "Another sync is already running" and you're sure nothing is actually running, it's safe to delete `backend/sync.lock` (the runner also auto-clears it if the PID is dead or it's >20 min old).

**Frontend dev server:**
```powershell
cd C:\Users\vsome\Desktop\sbdc-system\frontend
npm run dev
# Dashboard at http://localhost:5173
```

---

## .env file rules

- Lives at **project root** (`C:\Users\vsome\Desktop\sbdc-system\.env`)
- Backend loads it with: `load_dotenv(Path(__file__).parent.parent / ".env", override=True)`
- **MUST be written with `-Encoding ascii` in PowerShell** — UTF-8 adds a BOM that silently corrupts the first variable Python reads. Never use `utf8` encoding when writing .env.

---

## Tally connection

| Setting | Value |
|---|---|
| IP | `192.168.0.205` |
| Port | `9000` |
| URL | `http://192.168.0.205:9000` |
| Company name | `SUPREME BALAJI DYE CHEM - 25-26` |
| Access | Office LAN only — unreachable from outside |

Tally must be open and the correct company active before running a live sync.

### Confirmed live facts (24-Sep-2026, office PC)

- **Sales voucher types are `GST SALES` and `CC SALES`** — `Sales Order` is a
  separate, distinct voucher type (was already excluded via the `SO-`
  voucher-number prefix check regardless).
- **TDL `<COLLECTION>` DOES support real server-side filtering** — a
  `<FILTER>` referencing a `<SYSTEM TYPE="Formulae">` object filters before
  Tally sends the response, and it's dramatically cheaper. Confirmed in two
  rounds: a single-date filter (`$Date = $$Date:"24-Sep-2026"`) returned 52
  vouchers in 8.1s vs ~14,000+ vouchers and ~60s unfiltered; a date-**range**
  filter (`$Date >= $$Date:"17-Sep-2026" AND $Date <= $$Date:"23-Sep-2026"`,
  with `<=` escaped as `&lt;=` in the request XML) returned exactly those
  578 vouchers in 12.8s. Earlier sessions only tried `SVFROMDATE`/`SVTODATE`
  report variables against Collections, which don't work — a different
  mechanism from a TDL `<FILTER>` entirely, and the two should not be
  confused. Steps 9, 9b, 10, and the reconcile count check now all use
  `<FILTER>` + `<SYSTEM TYPE="Formulae">`. Both the single-date and
  date-range forms are now confirmed; the combined AND-with-voucher-type
  formulas built around them are still this codebase's extrapolation, not
  separately verified. The code logs a "server response: N raw voucher(s),
  dates seen X to Y" line for every such request so any future filter
  regression shows up in real logs.
- **The "no parseable amount tag" vouchers Step 10 used to warn about are
  cancelled vouchers** — `IsCancelled = Yes`, with empty party, amount, and
  ledger entries. Example: `SBDC-56/26-27`. These are now filtered
  server-side (`NOT $IsCancelled`) and skipped client-side with a single
  aggregate INFO count, not a per-voucher warning. A `no parseable amount
  tag` warning firing today would mean a *new*, still-unexplained case, not
  this one.
- **`$Amount` gives the correct invoice total as an absolute value** — no
  sign convention change needed for how Step 9/9b/10 already use it.
- **Step 9 vs Step 10 totals can legitimately differ by one invoice** — on
  24-Sep-2026, Step 9 saw 22 GST SALES vouchers (₹7,76,306) and Step 10 saw
  23 (₹7,88,781) because `SBDC-4082/26-27` was entered in Tally in the gap
  between the two steps running. Not a bug — a real live sync will always
  have some chance of this on the current day.
- **Step 9b legitimately returning 0 on a receipt-free day** is confirmed
  correct behaviour, not a bug — there were genuinely 0 receipts on
  24-Sep-2026. This is the scenario the Phase 2 "never write zero on a
  failed fetch" guard has to tell apart from a broken fetch; a well-formed
  empty response is not an error.
- **Collections come in as THREE distinct exact voucher types** —
  `Receipt`, `PoS Receipt`, and `Cash Receipt` — confirmed live on a
  follow-up day that actually had receipts. Step 9b's filter now matches all
  three (`$VoucherTypeName = "Receipt" OR ... = "PoS Receipt" OR ... =
  "Cash Receipt"`); matching only `"Receipt"` would have silently dropped
  PoS and cash receipts from `daily_collections` every day. The
  `RECEIPT_VOUCHER_TYPES` constant is the single source of truth — anything
  else that ever needs to count collections should use it rather than
  re-guessing a voucher type list. **Not touched**: `_CREDIT_VCH_TYPES =
  {"Payment", "Receipt"}` in the Step 3 Bills Receivable parser (a
  completely different Tally report/field — `BILLVCHTYPE` from Bills
  Receivable, not `VoucherTypeName` from a Voucher Collection). Whether
  Bills Receivable's on-account-credit detection also needs the PoS/Cash
  split is unconfirmed; flagging rather than guessing.
- **Credit Notes (sales returns) are not included or netted anywhere on the
  dashboard.** Checked both sides: nothing in this repo (frontend or
  backend) references "Credit Note" or "Sales Return" at all, and Step
  9/9b/10 only ever fetch voucher types matching `GST SALES`/`CC SALES` (now
  server-side, previously via the "SALES" substring check) — a Credit Note
  voucher has a different `VoucherTypeName` and was never fetched into
  `daily_sales` or `sales_history` even before this filter change. The
  frontend just sums `daily_sales.total_amount` / `sales_history.amount` as
  stored, with no separate Credit Note netting logic. This was **not**
  changed as part of this work — flagging as info only, per instruction.

---

## Supabase

- Project URL: `https://ipmmpentjzasatknnbvb.supabase.co`
- Anon key (frontend): `sb_publishable_vMSmM7BP6FyLL_hrG_kGhg_41XTtJrB`
- Secret key: in root `.env` as `SUPABASE_SECRET_KEY` (never hardcode)
- PostgREST **hard caps responses at 1000 rows**. Any table with >1000 rows must be fetched in a paginated loop using `.range(offset, offset + 999)`.
- **Tables created via SQL Editor need explicit permissions** — the Supabase UI table editor auto-grants anon access, but raw `CREATE TABLE` does not. The frontend will get `null` with no error (silent permission denial). After creating any table via SQL Editor, run:
  ```sql
  GRANT SELECT ON [table] TO anon;
  -- and if RLS is enabled:
  CREATE POLICY "anon_read" ON [table] FOR SELECT TO anon USING (true);
  ```
  Example: `daily_collections` was created via SQL Editor and returned `null` to the frontend until `GRANT SELECT ON daily_collections TO anon` was run.

---

## Database schema

### `users` — staff members
| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| name | text | |
| role | text | `md`, `marketing` |
| phone | text | |
| active | bool | |

### Staff UUIDs
| Name | UUID |
|---|---|
| Venkatesh | `122839f7-01da-4b62-aaba-18fe5b750d41` |
| Thiagarajan | `e3b14742-ebfb-4ec9-ac08-f06448751695` |
| Gowtham | `99403aa6-c53b-438f-88b3-53012f15d3d5` |
| Vijaya Priya | `8acc0225-67f3-4ec7-89de-e7ea0b4c0dd7` |

### `customers`
| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| customer_name | text | must match Tally PARTYLEDGERNAME exactly for sync to work |
| customer_type | text | `credit` or `cash` |
| credit_days | int | null for cash customers |
| assigned_to | uuid FK→users.id | null = unassigned |
| phone | text | |
| address | text | |
| gst_number | text | |
| flagged | bool | |
| flagged_reason | text | |

### `outstanding` — bills receivable rows
| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| customer_id | uuid FK→customers.id | |
| invoice_ref | text | e.g. `SBDC-1234/25-26` |
| invoice_date | date | |
| due_date | date | |
| pending_amount | numeric | negative = on-account credit (Payment/Receipt vouchers) |
| bucket | text | `0-30`, `30-60`, `60-90`, `90-120`, `120+` |
| days_overdue | int | |
| age_status | text | `recent` (≤12 months) or `stale` |
| synced_from_tally_at | timestamptz | used by insert-first/delete-old safety pattern |

### `daily_sales` — today's Day Book summary
| Column | Type | Notes |
|---|---|---|
| sale_date | date PK | |
| total_amount | numeric | |
| invoice_count | int | |
| synced_at | timestamptz | |
| items | jsonb | array of `{customer_name, customer_id, invoice_ref, amount}` |

`customer_id` in items is the customers.id UUID — used for UUID-keyed lookup in the frontend so name mismatches can't cause "Unassigned".

### `sales_history` — full FY sales vouchers
| Column | Type | Notes |
|---|---|---|
| voucher_number | text UNIQUE | e.g. `SBDC-1234/26-27` |
| sale_date | date | |
| customer_name | text | raw Tally PARTYLEDGERNAME — may differ in casing from customers table |
| customer_id | uuid FK→customers.id | nullable; stamped by Step 10. Requires: `ALTER TABLE sales_history ADD COLUMN IF NOT EXISTS customer_id uuid REFERENCES customers(id);` |
| amount | numeric | nullable (some Tally vouchers have no parseable amount) |
| stock_item | text | |
| quantity | numeric | |
| rate | numeric | |
| voucher_type | text | |
| synced_at | timestamptz | |

**Name-drift fix**: `sales_history.customer_name` is the raw Tally `PARTYLEDGERNAME` and often differs in casing from `customers.customer_name`. Frontend fetches by `.ilike()` (case-insensitive) with a fuzzy suffix-strip fallback. Backend stamps `customer_id` UUID in Step 10 — once populated, frontend can switch to UUID-primary lookup.

### Views (read-only, used by dashboard)
| View | Purpose |
|---|---|
| `customer_list_view` | All customers with `assigned_to_name`, `present_pending`, `archived_pending` |
| `outstanding_status_summary` | Total by age_status (recent/stale) |
| `outstanding_bucket_summary` | Total per aging bucket |
| `flagged_customers_summary` | Flagged customers with outstanding |
| `outstanding_by_staff_summary` | Outstanding per staff member |

`customer_list_view` has **1,067+ rows** — must be fetched with pagination in the frontend.

### `sync_status` — per-run sync health (added for honest status reporting)
| Column | Type | Notes |
|---|---|---|
| id | bigint identity PK | |
| run_at | timestamptz | |
| status | text | `success`, `partial`, or `failed` |
| steps | jsonb | `{"outstanding": "success"\|"failed"\|"skipped", "today_sales": ..., "collections": ..., "sales_history": ...}` |
| last_success_outstanding | timestamptz | carried forward from the previous run if this run didn't touch that step |
| last_success_today_sales | timestamptz | |
| last_success_collections | timestamptz | |
| last_success_sales_history | timestamptz | |
| detail | jsonb | bill counts, skipped names, reconcile_status, or error message |

One row is inserted per run by `_write_status()`. Requires the same table setup as any new table:
```sql
CREATE TABLE IF NOT EXISTS sync_status (
  id                          bigint generated by default as identity primary key,
  run_at                      timestamptz NOT NULL DEFAULT now(),
  status                      text NOT NULL,
  steps                       jsonb NOT NULL,
  last_success_outstanding    timestamptz,
  last_success_today_sales    timestamptz,
  last_success_collections    timestamptz,
  last_success_sales_history  timestamptz,
  detail                      jsonb
);

GRANT SELECT ON sync_status TO anon;
ALTER TABLE sync_status ENABLE ROW LEVEL SECURITY;
CREATE POLICY "anon_read" ON sync_status FOR SELECT TO anon USING (true);
```
Writing to this table is best-effort (wrapped in try/except) — the sync never fails because this table is missing or unreachable; it just logs a warning and `last_sync_status.json` is still written locally either way.

---

## Sync runner step map

| Step | Function | Description |
|---|---|---|
| 1 | `check_tally()` | Ping Tally HTTP API — retries up to 3× with backoff. **Always runs.** |
| 2 | `fetch_tally_xml()` | Fetch Bills Receivable XML, save backup. **Throttled** — only if last successful run > `OUTSTANDING_THROTTLE_HOURS` (3h) old |
| 3 | `parse_xml()` | Parse bill entries, tag age/bucket. Runs iff Step 2 ran |
| 4 | `reload_supabase()` | Build customer map from DB. Runs iff Step 2/3 ran |
| **4.5** | `auto_insert_new_customers()` | Auto-insert new customers from Tally ledger master. **Throttled** with 4.6 (once a day); additionally a no-op (not a failure) on a run where Step 2/3 was itself throttled, since it needs this run's freshly parsed bills |
| 5 | inside reload_supabase | Sanity check (abort if >50% drop) |
| 6 | inside reload_supabase | Clear any partial rows from a failed previous run |
| 7 | inside reload_supabase | Insert all new outstanding rows |
| 8 | inside reload_supabase | Delete old rows (previous sync timestamp) |
| 9 | `sync_today_sales()` | TDL Collection with a server-side date+type+not-cancelled `<FILTER>`, today only, upsert daily_sales. **Always runs** (small, filtered request) |
| 9b | `sync_today_collections()` | Same approach as Step 9 (`Receipt`/`PoS Receipt`/`Cash Receipt` types), upsert daily_collections. **Always runs** |
| 10 | `sync_sales_history()` | Full FY sweep weekly/`--full`, AlterID incremental once confirmed, month-based fallback otherwise — see below. **Throttled**, mode varies |

Steps 9, 9b, and 10 are **non-fatal** — wrapped in try/except so a Tally timeout doesn't abort the outstanding sync, and each is tracked individually in `sync_status`/`last_sync_status.json` so a degraded run shows as `partial`, not silently as `success`. Step 4.5/4.6 is also non-fatal, tracked under an informational `ledger_master` key that never gates `overall_status` (same as before throttling existed), and is **skipped entirely in `--from-local` mode** (can't reach Tally).

**Never treat an exception from Steps 9/9b as "zero"**: `_fetch_vouchers_for_day()` raises if Tally's response is implausibly small, or if Tally reports a TDL error (`_raise_on_tally_error` — a `<LINEERROR>` in the response, e.g. a bad filter formula), rather than silently returning an empty result — a broken fetch must never overwrite `daily_sales`/`daily_collections` with 0. The existing per-step try/except means nothing gets upserted at all when that happens; the previous value is left untouched. See "Confirmed live facts" above for what's actually been verified about the filters themselves vs. what's this codebase's untested extrapolation.

**Cancelled vouchers**: `IsCancelled=Yes` vouchers are excluded server-side (`NOT $IsCancelled` in the filter) and, as a safety net, also skipped client-side — Steps 9/9b/10 all check `_is_voucher_cancelled()` and log one aggregate INFO count, not a warning per voucher. This is what the old per-voucher "no parseable amount tag" warnings in Step 10 turned out to be.

**Overlap lock**: `sync.lock` (pid + start time) stops two runs from overlapping. Treated as stale — removed and the new run proceeds — if the recorded PID isn't running, or the lock is older than 20 minutes (the office PC is switched off at 7 PM and can be shut down mid-run, which would otherwise leave a lock nothing could ever clear).

**RECONCILE tri-state**: the post-sync voucher-count cross-check reports `OK`, `MISMATCH`, or `UNKNOWN` — previously an exception while fetching Tally's independent count silently left the status at `OK` (the `count_ok` flag defaulted `True` and nothing set it `False` on that path). `UNKNOWN` doesn't fail the run; a confirmed `MISMATCH` downgrades the overall run status to `partial`. Now also skipped outright (logged, not a mismatch) on a run where Step 10 didn't fetch anything fresh (`sync_sales_history` returned `None`) — comparing Step 9's fresh total against a stale/absent Step 10 number would flag a mismatch purely because Step 10 was throttled, not because anything's actually wrong.

**Timestamp rule — any value written to a Supabase `timestamptz` column must be timezone-aware** (`datetime.now(UTC)`, not `datetime.now()`). A naive local datetime's `.isoformat()` has no offset in the string, so Postgres has no way to know it was IST and stores it as if it were UTC — a real bug caught in `_write_status()`: `sync_status.run_at` for an 18:31 IST run was stored as `18:31:28+00`, 5.5 hours off. Fixed by splitting it into `now_utc_iso` (aware — used for `run_at` and every `last_success_*` column) and `now_local_iso` (naive — used only for `last_sync_status.json`'s own `"timestamp"` field, a local log-style value nobody reads except a human on that machine, same convention as `RUN_TS`). Values that never reach Supabase (`sync.lock`'s `started_at`, `RUN_TS`, email body/subject text, the XML-backup retention cutoff) are fine staying naive local — the bug is specifically about the round trip through a `timestamptz` column.

---

## Load-reduction throttles (25-Sep-2026)

The office reported Tally lagging on every connected PC — each scheduled run's heavy steps kept the shared billing PC busy 80-120s every 30 minutes. Steps 1/9/9b stay light and always run (small, filtered requests already); everything else now reuses previous data instead of refetching it, gated by how long it's actually been since it last ran. Throttle windows are constants near the top of `tally_sync_runner.py`: `OUTSTANDING_THROTTLE_HOURS` (3h), `LEDGER_MASTER_THROTTLE_HOURS` (24h), `SALES_HISTORY_FULL_SWEEP_DAYS` (7d), `FALLBACK_CURRENT_MONTH_HOURS` (2h), `FALLBACK_OLDER_MONTH_HOURS` (24h).

**Due decisions read real elapsed time, not a call count** — `_is_due(iso_str, hours)` (missing/unparseable timestamp = always due) checks against `last_success.outstanding` in `last_sync_status.json` for Step 2/3, and against fields in `sync_state.json` for the ledger master and Step 10.

**dry_run must never advance a throttle.** `_write_status()` only updates `last_success[step]` when `not dry_run` — a `--dry-run` test run doesn't actually write to that step's target table, so it must not count as a "last success" for throttling purposes (it would otherwise silently suppress the next `OUTSTANDING_THROTTLE_HOURS` of real outstanding syncs). Every new state write this phase added (`last_ledger_master_run`, `last_alterid`, `last_current_month_fetch_at`, `last_older_month_fetch_at`, `last_full_sales_history_sweep`) is likewise gated by `if not dry_run`. Caught by an actual `main()`-level test (mocking Tally, calling `main()` twice in a row) before shipping — worth re-running that style of test if this logic changes again, since a unit test on the helper functions alone wouldn't have caught it.

**Step 10 has three modes**, chosen by `sync_sales_history()`:

1. **Full FY sweep** (`_full_sales_history_sweep`) — every `SALES_HISTORY_FULL_SWEEP_DAYS`, or whenever `--full` is passed (also true on a fresh install with no state file — `_is_due` on a missing timestamp is always `True`). The only mode that can detect deletions, since an incremental fetch (date-range OR AlterID) can only ever add/update. A voucher missing from the sweep's results is purged from `sales_history` whether it was truly deleted in Tally or simply cancelled after being a real sale — both are already excluded from every Step 10 fetch, so "not in the current set" is the right signal either way (see `_reconcile_sales_history_deletions`). Two safety layers: deletion is skipped entirely (upserts still happen) if any month chunk failed to fetch this sweep, and separately refused if it would remove more than `SALES_HISTORY_DELETE_SANITY_LIMIT` (20%) of the FY's previously-tracked vouchers — either could otherwise turn a temporary Tally hiccup into mass data loss.
2. **AlterID incremental** (`_sync_sales_history_alterid`) — **live since 25-Sep-2026** (`ALTERID_SYNC_ENABLED = True`). Every run, no throttle needed: it only ever fetches sales vouchers with Tally's `AlterID` greater than the highest one seen last time (`sync_state.json`'s `last_alterid`), which by construction is cheap regardless of frequency. AlterID increases company-wide on every create/edit, so this needs no date bound at all and picks up an edit or cancellation to an old invoice for free — a date-range fetch would never look at that month again once its rotation had passed. **Reset guard**: an empty result is ambiguous on its own (genuinely nothing new, or Tally's AlterID sequence went backward — a data repair/restore — leaving the stored threshold permanently above everything that currently exists and silently blinding every future fetch too). Only on that empty result does it pay for one heavier independent check, `_fetch_max_sales_alterid_this_fy` (same FY-scoped scan `probe_alterid.py` uses, confirmed ~9.2s) — a normal run that finds new vouchers never pays that cost. If the independent check comes back lower than the stored value, it logs a `WARNING`, resets `last_alterid` to 0, and runs a full FY sweep instead of trusting the stale threshold (mode `"full-after-alterid-reset"`) — the weekly full sweep would eventually catch this too, but not for up to `SALES_HISTORY_FULL_SWEEP_DAYS`.
3. **Month-based fallback** (`_sync_sales_history_fallback`) — dead code path now that AlterID is confirmed working, kept as the escape hatch if `ALTERID_SYNC_ENABLED` ever needs to go back to `False` (e.g. a future Tally upgrade stops exposing AlterID the same way). Same current+rotation idea as the original Phase 1 design, rate-limited instead of running every single call: current month at most every `FALLBACK_CURRENT_MONTH_HOURS`, one older FY month (previous month first, for `NEW_MONTH_CATCHUP_RUNS` runs after a rollover) at most every `FALLBACK_OLDER_MONTH_HOURS`. Returns `(None, ..., "skipped")` when neither throttle is due — `None`, not `0.0`, so a throttled run can never look like a real zero to the reconcile cross-check.

**`ALTERID_SYNC_ENABLED = True`, confirmed live 25-Sep-2026** via `backend/probe_alterid.py` on the office PC: current max sales-voucher AlterID was 174542, and `AlterID > 174492` correctly returned exactly 9 real recent vouchers (GST SALES + CC SALES, all that day) in 3.5s — vs 9.2s for the equivalent full-FY scan with no AlterID filter. Both the exact-threshold form used in production and the general `<FILTER>`/`<SYSTEM TYPE="Formulae">` mechanism are now confirmed on this install. `backend/probe_alterid.py` is still useful going forward as a standalone, read-only sanity check if this ever needs re-verifying (e.g. after a Tally version upgrade).

**`ledger_master` step status** is tracked in `sync_status`'s `steps` JSONB (an extra key alongside the four `STATUS_STEPS`) purely for visibility — it does not have its own `last_success_*` column and, matching its pre-throttling behaviour, never gates `overall_status`.

**Run duration is now logged** for every mode (normal sync, backfill) and recorded in `sync_status`/`last_sync_status.json`'s `detail.duration_seconds`, alongside `detail.sales_history_mode` (`"full"`/`"alterid"`/`"fallback"`/`"skipped"`/`None`) — both requested specifically so a degraded/throttled run's actual behaviour is visible without reading the full log.

---

## Tally group → staff assignment mapping

Used in `auto_insert_new_customers()` and originally in `full_customer_import.py`:

```python
_STAFF_GROUPS = {
    "1.Venkatesh - Parties":     "Venkatesh",
    "Bill Wise - J.Venkatesh":   "Venkatesh",
    "2.Thiagarajan - Parties":   "Thiagarajan",
    "Bill Wise - G.Thiagarajan": "Thiagarajan",
    "3.Gowtham - Parties":       "Gowtham",
    "Bill Wise - S.Gowtham":     "Gowtham",
    "7.Levaset - Parties":       "Vijaya Priya",
    "8.Vetri-Parties":           "Vijaya Priya",
    "9.Vijayapriya - Parties":   "Vijaya Priya",
    "Kanagaraj - Parties":       "Vijaya Priya",
}
# "(GT)" in parent → Thiagarajan
# "4.Cash - Parties" → customer_type=cash, assigned_to=NULL
# "5.Bad Debtors 24-25" → flagged=True
# "6.Case Filed Customers" → flagged=True
# "Bad Debts Written Off" → flagged=True, historical
```

---

## Frontend key patterns

- **UUID-primary lookup for Today's Sales**: `staffById[item.customer_id]` with name-based fallback. Never rely on string matching alone — name variations in Tally will silently cause "Unassigned".
- **Paginated Supabase fetches**: Both `customer_list_view` (1067+ rows) and `sales_history` (FY data) must use `.range()` loops — single requests are capped at 1000.
- **Vite env vars**: Frontend credentials are in `frontend/.env` as `VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY`. Baked into the build at compile time.
- **Sales history date range**: Fetches from FY start (April 1) to today. Period switcher: This month / Last month / This FY.

---

## People

| Person | Role | Relation |
|---|---|---|
| Venkatesh | MD / Owner | User's father |
| Thiagarajan | Field marketing | Staff |
| Gowtham | Field marketing | Staff |
| Vijaya Priya | Marketing | User's mother |
| Sumeet (user) | Builder | Owner's child, final-year AIML student |

Former staff (Vetri, Levaset, Kanagaraj) — their Tally groups map to Vijaya Priya in the assignment system.

<!-- last updated: 2026-09-25 -->
