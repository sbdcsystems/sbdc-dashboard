# SBDC System — Claude context

Supreme Balaji Dye Chem (SBDC) internal collections dashboard.
Python pipeline (Tally → Supabase) + Vite/React frontend.

---

## Project layout

```
sbdc-system/
  .env                        ← credentials (project root — NOT inside backend/)
  venv/                       ← Python virtualenv (root level)
  backend/
    tally_sync_runner.py      ← main sync script (Steps 1-10)
    full_customer_import.py   ← one-time ledger master import (reference only)
    assign_one.py             ← one-off manual assignment script
    add_customers.py          ← one-off customer insert script
    check_jsonb.py            ← diagnostic: inspect daily_sales JSONB
    probe_tally_reports.py    ← diagnostic: test Tally report types
    logs/                     ← sync logs (sync_YYYYMMDD_HHMMSS.log)
    last_sync_status.json     ← written after every sync run
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
python tally_sync_runner.py

# Parse local XML backup without Tally connection:
python tally_sync_runner.py --from-local

# Dry run (no DB writes):
python tally_sync_runner.py --from-local --dry-run
```

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
| customer_name | text | |
| amount | numeric | nullable (some Tally vouchers have no parseable amount) |
| stock_item | text | |
| quantity | numeric | |
| rate | numeric | |
| voucher_type | text | |
| synced_at | timestamptz | |

### Views (read-only, used by dashboard)
| View | Purpose |
|---|---|
| `customer_list_view` | All customers with `assigned_to_name`, `present_pending`, `archived_pending` |
| `outstanding_status_summary` | Total by age_status (recent/stale) |
| `outstanding_bucket_summary` | Total per aging bucket |
| `flagged_customers_summary` | Flagged customers with outstanding |
| `outstanding_by_staff_summary` | Outstanding per staff member |

`customer_list_view` has **1,067+ rows** — must be fetched with pagination in the frontend.

---

## Sync runner step map

| Step | Function | Description |
|---|---|---|
| 1 | `check_tally()` | Ping Tally HTTP API |
| 2 | `fetch_tally_xml()` | Fetch Bills Receivable XML, save backup |
| 3 | `parse_xml()` | Parse bill entries, tag age/bucket |
| 4 | `reload_supabase()` | Build customer map from DB |
| **4.5** | `auto_insert_new_customers()` | Auto-insert new customers from Tally ledger master |
| 5 | inside reload_supabase | Sanity check (abort if >50% drop) |
| 6 | inside reload_supabase | Clear any partial rows from a failed previous run |
| 7 | inside reload_supabase | Insert all new outstanding rows |
| 8 | inside reload_supabase | Delete old rows (previous sync timestamp) |
| 9 | `sync_today_sales()` | Fetch Day Book, upsert daily_sales |
| 10 | `sync_sales_history()` | Fetch full FY sales by monthly chunks, upsert sales_history |

Steps 9 and 10 are **non-fatal** — wrapped in try/except so a Tally timeout doesn't abort the outstanding sync.
Step 4.5 is also non-fatal and **skipped in `--from-local` mode** (can't reach Tally).

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
