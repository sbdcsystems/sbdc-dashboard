"""
audit.py — read-only daily data-integrity check.

Compares live Tally (server-side filtered — the same <FILTER>/<SYSTEM
TYPE="Formulae"> mechanism confirmed working and used throughout
tally_sync_runner.py, see CLAUDE.md) against Supabase for:
  - Sales: total + invoice count, for Today / Yesterday / Last 7 days /
    Month-to-date. Supabase side reads sales_history with the exact same
    date-range filter the frontend's Sold card now uses (see the 28-Sep-2026
    "ONE source" fix in App.jsx).
  - Collections: customer-only total + count, same four periods. Supabase
    side reads daily_collections the same way the frontend's Received card
    does — total_amount/invoice_count are already computed server-side with
    non-customer parties excluded (see the 26-Sep-2026 collections-exclusion
    fix), so this checks that daily_collections agrees with Tally, not just
    that Supabase agrees with itself.
  - Outstanding: current total + per-staff, from Tally's Bills Receivable
    report (the exact same report and sign convention tally_sync_runner.py's
    Step 2/3 use) — deliberately NOT ledger ClosingBalance. ClosingBalance is
    a known dead end on this Tally install: it times out for customers with
    a large bill history, and nets on-account credits differently than Bills
    Receivable does (confirmed: Sri Bhadri Narayana Textiles shows
    Rs 14,00,846 in Bills Receivable vs a true ledger balance of
    Rs 6,06,949 — see CLAUDE.md's "KNOWN LIMITATION" comment on
    _CREDIT_VCH_TYPES in tally_sync_runner.py). Bills Receivable is what the
    dashboard's own `outstanding` table is actually built from, so comparing
    against it catches real staleness/drift without re-litigating that
    known, accepted gap.

    The PRIMARY outstanding comparison reads the exact Bills Receivable
    snapshot (tally_outstanding_*.xml) the sync itself just saved — not a
    separate live fetch — so both sides reflect the literal same read,
    eliminating (not just narrowing) the sync-vs-audit race. Confirmed live
    28-Sep-2026 during busy billing: comparing against a fresh live fetch
    instead produced +/- Rs 3k-20k per-staff diffs purely from entries made
    in the ~2 minutes between the sync's fetch and audit.py's own. A
    separate live-Tally comparison is still run and shown, but marked
    informational — it can fail without affecting overall_pass.

Sales/collections voucher fetches skip Tally's empty placeholder
<VOUCHER></VOUCHER> (confirmed live 28-Sep-2026 — every Collection response
includes one with no date/number/amount, which was inflating every count by
exactly 1), and treat any voucher newer than the last sync's own AlterID
threshold (read from sync_state.json) as "pending" rather than a mismatch —
it genuinely can't be in Supabase yet if it was created after the sync
already ran. A pending voucher is excluded from BOTH the Tally side and the
Supabase side of the comparison (confirmed live 28-Sep-2026: excluding it
from only the Tally side produced a fake diff exactly equal to that
voucher's amount, in cases where a concurrent regular sync run had already
written it into Supabase between this script's own Tally-side and
Supabase-side fetches). Pending vouchers are reported, not counted as a FAIL.

Prints a PASS/FAIL table and writes one row to audit_results (best-effort —
see CLAUDE.md for the table's GRANT/RLS setup; a missing table must not
crash this script, matching the sync_status convention).

Standalone by design — does not import tally_sync_runner.py, so running
this can't trigger that module's import-time side effects (log file
creation, sync.lock handling, etc.). Rebuilds its own minimal TDL/Bills
Receivable request builders instead, following the same pattern as
probe_alterid.py and probe_ledger_phones.py.

Run manually:
    python audit.py
Run automatically once a day after the 12:00 sync via run_audit.bat + a
Windows Task Scheduler entry (see CLAUDE.md) — and per CLAUDE.md's standing
rule, run this manually and confirm PASS after any change to sync or
frontend data logic, before calling that work done.
"""

import html
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from supabase import create_client

UTC = timezone.utc

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR.parent / ".env", override=True)

TALLY_IP      = os.environ.get("TALLY_SERVER_IP", "192.168.0.205")
TALLY_PORT    = int(os.environ.get("TALLY_PORT", "9000"))
TALLY_URL     = f"http://{TALLY_IP}:{TALLY_PORT}"
TALLY_COMPANY = os.environ.get("TALLY_COMPANY_NAME", "SUPREME BALAJI DYE CHEM - 25-26")

SALES_VOUCHER_TYPES   = ("GST SALES", "CC SALES")
RECEIPT_VOUCHER_TYPES = ("Receipt", "PoS Receipt", "Cash Receipt")
TIMEOUT = 90
BILLS_RECEIVABLE_TIMEOUT = 120  # full FY, EXPLODEFLAG — same report as Step 2 (TALLY_TIMEOUT=60 there; more headroom here since this runs once/day, not throttled)

# Same regex + sign convention as tally_sync_runner.py's Step 3 (_BILL_RE /
# _CREDIT_VCH_TYPES) — kept as a literal copy here (not imported) so this
# script has no dependency on that module at all.
_BILL_RE = re.compile(
    r"<BILLFIXED>\s*"
    r"<BILLDATE>(.*?)</BILLDATE>\s*"
    r"<BILLREF>(.*?)</BILLREF>\s*"
    r"<BILLPARTY>(.*?)</BILLPARTY>\s*"
    r"</BILLFIXED>\s*"
    r"<BILLCL>(.*?)</BILLCL>\s*"
    r"<BILLDUE>(.*?)</BILLDUE>\s*"
    r"<BILLOVERDUE>(.*?)</BILLOVERDUE>\s*"
    r"<BILLVCHDATE>.*?</BILLVCHDATE>\s*"
    r"<BILLVCHTYPE>(.*?)</BILLVCHTYPE>",
    re.DOTALL,
)
_CREDIT_VCH_TYPES = {"Payment", "Receipt"}

PASS_TOLERANCE_RUPEES = 1.0  # rounding noise only, not a real mismatch
OUTSTANDING_SNAPSHOT_MAX_AGE_MINUTES = 30.0  # older than this, don't trust it as "the same moment" the sync ran


def _fy_start(today: date) -> date:
    return date(today.year, 4, 1) if today.month >= 4 else date(today.year - 1, 4, 1)


def _add_days(d: date, n: int) -> date:
    from datetime import timedelta
    return d + timedelta(days=n)


def _tally_date_literal(d: date) -> str:
    return d.strftime("%d-%b-%Y")


def _xml_escape_formula(formula: str) -> str:
    return formula.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_voucher_request(collection_id: str, fetch_fields: str, filter_formula: str) -> str:
    filter_name = f"{collection_id}Filter"
    return (
        "<ENVELOPE>"
        "<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        f"<TYPE>Collection</TYPE><ID>{collection_id}</ID></HEADER>"
        "<BODY><DESC>"
        "<STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        "</STATICVARIABLES>"
        "<TDL><TDLMESSAGE>"
        f'<COLLECTION NAME="{collection_id}" ISMODIFY="No">'
        "<TYPE>Voucher</TYPE>"
        f"<FETCH>{fetch_fields}</FETCH>"
        f"<FILTER>{filter_name}</FILTER>"
        "</COLLECTION>"
        f'<SYSTEM TYPE="Formulae" NAME="{filter_name}">{_xml_escape_formula(filter_formula)}</SYSTEM>'
        "</TDLMESSAGE></TDL>"
        "</DESC></BODY>"
        "</ENVELOPE>"
    )


def _build_bills_receivable_request(fy_start: date, today: date) -> str:
    return (
        "<ENVELOPE>"
        "<HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>"
        "<BODY><EXPORTDATA><REQUESTDESC>"
        "<REPORTNAME>Bills Receivable</REPORTNAME>"
        "<STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        f"<SVFROMDATE>{fy_start.strftime('%Y%m%d')}</SVFROMDATE>"
        f"<SVTODATE>{today.strftime('%Y%m%d')}</SVTODATE>"
        "<EXPLODEFLAG>Yes</EXPLODEFLAG>"
        "</STATICVARIABLES>"
        "</REQUESTDESC></EXPORTDATA></BODY>"
        "</ENVELOPE>"
    )


def _post(xml_body: str) -> str:
    r = requests.post(
        TALLY_URL, data=xml_body.encode("utf-8"),
        headers={"Content-Type": "text/xml"}, timeout=TIMEOUT,
    )
    return r.content.decode("utf-8", errors="replace")


def _raise_on_tally_error(xml: str):
    m = re.search(r"<LINEERROR>(.*?)</LINEERROR>", xml, re.DOTALL)
    if m:
        raise RuntimeError(f"Tally reported a TDL error: {html.unescape(m.group(1).strip())}")


def check_tally() -> bool:
    try:
        requests.get(TALLY_URL, timeout=5)
        return True
    except requests.exceptions.RequestException:
        return False


# ── Tally-side fetches ───────────────────────────────────────────────────────

def _load_sync_state() -> dict:
    """
    Reads sync_state.json — the same file tally_sync_runner.py writes — for
    the AlterID thresholds the last sync run actually captured
    (last_alterid for sales, last_receipt_alterid for collections). Lets the
    voucher checks below tell a genuinely new voucher (created in the gap
    between the sync finishing and this audit running a moment later) apart
    from a real miss, instead of failing on a race condition every time.
    Best-effort: a missing file or key means "no threshold" (0), so nothing
    gets excluded — never a silent false PASS from a read that didn't work.
    """
    path = BASE_DIR / "sync_state.json"
    if not path.exists():
        return {}
    try:
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _voucher_alterid(v: str) -> int:
    m = re.search(r"<ALTERID\b[^>]*>(.*?)</ALTERID>", v)
    if not m:
        return 0
    try:
        return int(float(m.group(1)))
    except ValueError:
        return 0


def fetch_tally_sales(from_date: date, to_date: date, alterid_threshold: int) -> dict:
    """
    Every Collection response from this Tally install includes one empty
    placeholder <VOUCHER></VOUCHER> with no date/number/amount — confirmed
    live 28-Sep-2026 (counts were off by exactly 1 everywhere, including a
    day with genuinely zero sales showing "1 invoice, Rs 0"). Skipped by
    requiring a real voucher number, date, and parseable amount.

    Vouchers with AlterID > alterid_threshold were created/edited after the
    sync run that populated Supabase last captured its threshold — they
    can't be in Supabase yet, so they're reported separately as "pending"
    rather than counted as a mismatch.
    """
    type_formula = " OR ".join(f'$VoucherTypeName = "{t}"' for t in SALES_VOUCHER_TYPES)
    formula = (
        f'$Date >= $$Date:"{_tally_date_literal(from_date)}" AND '
        f'$Date <= $$Date:"{_tally_date_literal(to_date)}" AND '
        f'NOT $IsCancelled AND ({type_formula})'
    )
    xml = _post(_build_voucher_request("AuditSales", "Date,VoucherNumber,Amount,AlterID", formula))
    _raise_on_tally_error(xml)
    vouchers = re.findall(r"<VOUCHER\b.*?</VOUCHER>", xml, re.DOTALL)
    total, count = 0.0, 0
    pending_total, pending_count = 0.0, 0
    pending_voucher_numbers = set()
    for v in vouchers:
        date_m = re.search(r"<DATE\b[^>]*>(.*?)</DATE>", v)
        vnum_m = re.search(r"<VOUCHERNUMBER\b[^>]*>(.*?)</VOUCHERNUMBER>", v)
        amt_m  = re.search(r"<AMOUNT\b[^>]*>(.*?)</AMOUNT>", v)
        if not date_m or not vnum_m or not amt_m:
            continue
        if not date_m.group(1).strip() or not vnum_m.group(1).strip():
            continue
        try:
            amt = abs(float(amt_m.group(1)))
        except ValueError:
            continue
        vnum = html.unescape(vnum_m.group(1).strip())
        if alterid_threshold and _voucher_alterid(v) > alterid_threshold:
            pending_total += amt
            pending_count += 1
            pending_voucher_numbers.add(vnum)
            continue
        total += amt
        count += 1
    return {
        "total": total, "count": count,
        "pending_total": pending_total, "pending_count": pending_count,
        "pending_voucher_numbers": pending_voucher_numbers,
    }


def fetch_tally_collections(from_date: date, to_date: date, customer_names: set, alterid_threshold: int) -> dict:
    """Same placeholder-voucher and AlterID-pending handling as fetch_tally_sales."""
    type_formula = " OR ".join(f'$VoucherTypeName = "{t}"' for t in RECEIPT_VOUCHER_TYPES)
    formula = (
        f'$Date >= $$Date:"{_tally_date_literal(from_date)}" AND '
        f'$Date <= $$Date:"{_tally_date_literal(to_date)}" AND '
        f'NOT $IsCancelled AND ({type_formula})'
    )
    xml = _post(_build_voucher_request("AuditColl", "Date,VoucherNumber,PartyLedgerName,Amount,AlterID", formula))
    _raise_on_tally_error(xml)
    vouchers = re.findall(r"<VOUCHER\b.*?</VOUCHER>", xml, re.DOTALL)
    total, count = 0.0, 0
    pending_total, pending_count = 0.0, 0
    pending_voucher_numbers = set()
    for v in vouchers:
        date_m  = re.search(r"<DATE\b[^>]*>(.*?)</DATE>", v)
        vnum_m  = re.search(r"<VOUCHERNUMBER\b[^>]*>(.*?)</VOUCHERNUMBER>", v)
        party_m = re.search(r"<PARTYLEDGERNAME\b[^>]*>(.*?)</PARTYLEDGERNAME>", v)
        amt_m   = re.search(r"<AMOUNT\b[^>]*>(.*?)</AMOUNT>", v)
        if not date_m or not vnum_m or not party_m or not amt_m:
            continue
        if not date_m.group(1).strip() or not vnum_m.group(1).strip():
            continue
        party = html.unescape(party_m.group(1)).strip().lower()
        if party not in customer_names:
            continue  # same exclusion rule as sync_today_collections()
        try:
            amt = abs(float(amt_m.group(1)))
        except ValueError:
            continue
        vnum = html.unescape(vnum_m.group(1).strip())
        if alterid_threshold and _voucher_alterid(v) > alterid_threshold:
            pending_total += amt
            pending_count += 1
            pending_voucher_numbers.add(vnum)
            continue
        total += amt
        count += 1
    return {
        "total": total, "count": count,
        "pending_total": pending_total, "pending_count": pending_count,
        "pending_voucher_numbers": pending_voucher_numbers,
    }


def _parse_bills_receivable_xml(xml_text: str, staff_by_name: dict) -> dict:
    """
    Shared parser for both the live fetch and the saved-snapshot-file path
    below. Applies the same on-account-credit sign convention (Payment/
    Receipt bill types negate the amount) as tally_sync_runner.py's Step
    2/3 — see the module docstring for why Bills Receivable replaces the
    earlier ClosingBalance-based check. Sums are SIGNED (not abs), matching
    how the `outstanding` table's own pending_amount is stored and how
    outstanding_by_staff_summary sums it — an on-account credit legitimately
    nets a customer's/staff's total down, sometimes below zero (e.g. an
    "Unassigned" bucket can be negative).
    """
    matches = _BILL_RE.findall(xml_text)
    if not matches:
        raise RuntimeError("No bill entries found in Bills Receivable XML — structure may have changed.")

    total = 0.0
    by_staff = {}
    for _date_raw, _ref, party, cl_raw, _due_raw, _overdue_raw, vch_type in matches:
        try:
            raw_amt = abs(float(cl_raw.strip()))
        except ValueError:
            continue
        amount = -raw_amt if vch_type.strip() in _CREDIT_VCH_TYPES else raw_amt
        staff = staff_by_name.get(html.unescape(party.strip()).lower(), "Unassigned")
        total += amount
        by_staff[staff] = by_staff.get(staff, 0.0) + amount
    return {"total": total, "by_staff": by_staff}


def fetch_tally_bills_receivable_live(fy_start: date, today: date, staff_by_name: dict) -> dict:
    """
    A fresh, right-now fetch from Tally. Used as the INFORMATIONAL-only
    comparison (see fetch_tally_bills_receivable_from_snapshot for the
    primary, gating one) — confirmed live 28-Sep-2026 during busy billing
    that comparing this against Supabase directly produces +/- Rs 3k-20k
    per-staff diffs purely from invoices/payments entered in the ~2 minutes
    between the sync's own Bills Receivable fetch and this one, with no
    real data problem behind them.
    """
    r = requests.post(
        TALLY_URL, data=_build_bills_receivable_request(fy_start, today).encode("utf-8"),
        headers={"Content-Type": "text/xml"}, timeout=BILLS_RECEIVABLE_TIMEOUT,
    )
    if len(r.content) < 200:
        raise RuntimeError(f"Tally returned only {len(r.content)} bytes for Bills Receivable — is the correct company open?")
    return _parse_bills_receivable_xml(r.content.decode("utf-8", errors="replace"), staff_by_name)


def find_latest_outstanding_snapshot(max_age_minutes: float = 30.0):
    """
    Finds the most recent tally_outstanding_*.xml backup written by
    tally_sync_runner.py's Step 2 (fetch_tally_xml) — the exact file a
    --force-outstanding sync run (see run_audit.bat) saves moments before
    this script runs. Comparing Supabase against THIS snapshot instead of a
    separate live fetch means both sides reflect the same Bills Receivable
    read, eliminating the sync-vs-audit race entirely rather than just
    narrowing its window.

    Returns (path, age_minutes) or (None, None) if no backup exists at all.
    A caller must still check age_minutes against its own freshness
    requirement — an old file here doesn't mean today's sync didn't run,
    it could mean this audit was invoked standalone, without run_audit.bat's
    preceding --force-outstanding sync.
    """
    candidates = list(BASE_DIR.glob("tally_outstanding_*.xml"))
    if not candidates:
        return None, None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    age_minutes = (time.time() - newest.stat().st_mtime) / 60
    return newest, age_minutes


def fetch_tally_bills_receivable_from_snapshot(path: Path, staff_by_name: dict) -> dict:
    xml_text = path.read_text(encoding="utf-8", errors="replace")
    return _parse_bills_receivable_xml(xml_text, staff_by_name)


# ── Supabase-side fetches (same query logic the frontend uses) ─────────────

def fetch_supabase_sales(supa, from_date: date, to_date: date, exclude_voucher_numbers: set = frozenset()) -> dict:
    """
    exclude_voucher_numbers must be the SAME set fetch_tally_sales marked
    "pending" for this period — a pending voucher has to be excluded from
    BOTH sides, not just the Tally side. Confirmed live 28-Sep-2026: a
    voucher (CC-181/26-27) landed in sales_history via a concurrent regular
    sync between this function's own Tally-side and Supabase-side fetches,
    so excluding it only from the Tally total produced a fake diff exactly
    equal to that voucher's amount, in the wrong direction.
    """
    rows = []
    offset = 0
    while True:
        batch = (
            supa.table("sales_history").select("amount,voucher_number")
            .gte("sale_date", from_date.isoformat()).lte("sale_date", to_date.isoformat())
            .range(offset, offset + 999).execute().data
        )
        rows.extend(batch)
        if len(batch) < 1000: break
        offset += 1000
    if exclude_voucher_numbers:
        rows = [r for r in rows if r["voucher_number"] not in exclude_voucher_numbers]
    total = sum(float(r["amount"] or 0) for r in rows)
    return {"total": total, "count": len(rows)}


def fetch_supabase_collections(supa, from_date: date, to_date: date, exclude_voucher_numbers: set = frozenset()) -> dict:
    """
    Same pending-exclusion requirement as fetch_supabase_sales. Subtracts
    any excluded item from each day's precomputed total_amount/invoice_count
    (rather than recomputing the day's totals from items from scratch) so
    everything else about how a day's numbers are built stays exactly as
    sync_today_collections/_full_collections_sweep already computed it.
    """
    rows = (
        supa.table("daily_collections").select("total_amount,invoice_count,items")
        .gte("sale_date", from_date.isoformat()).lte("sale_date", to_date.isoformat())
        .execute().data
    )
    total, count = 0.0, 0
    for r in rows:
        day_total = float(r["total_amount"] or 0)
        day_count = r["invoice_count"] or 0
        if exclude_voucher_numbers:
            for item in (r.get("items") or []):
                if item.get("excluded"):
                    continue  # already not part of day_total/day_count
                if item.get("invoice_ref") in exclude_voucher_numbers:
                    day_total -= float(item.get("amount") or 0)
                    day_count -= 1
        total += day_total
        count += day_count
    return {"total": total, "count": count}


def build_staff_maps(supa) -> tuple:
    """
    One shared customers+users fetch, returning both a customer_id-keyed map
    (for the Supabase-side outstanding sum, which has real UUIDs) and a
    lowercase-name-keyed map (for the Tally-side Bills Receivable sum, which
    only has BILLPARTY names — same fallback pattern as the frontend uses
    when a UUID isn't available).
    """
    customers = []
    offset = 0
    while True:
        batch = supa.table("customers").select("id,customer_name,assigned_to").range(offset, offset + 999).execute().data
        customers.extend(batch)
        if len(batch) < 1000: break
        offset += 1000
    users = supa.table("users").select("id,name").execute().data
    name_by_uid = {u["id"]: u["name"] for u in users}

    staff_by_id   = {c["id"]: name_by_uid.get(c["assigned_to"], "Unassigned") for c in customers}
    staff_by_name = {c["customer_name"].strip().lower(): name_by_uid.get(c["assigned_to"], "Unassigned") for c in customers}
    return staff_by_id, staff_by_name


def fetch_supabase_outstanding(supa, staff_by_id: dict) -> dict:
    """
    Sums are SIGNED (not abs) — see fetch_tally_bills_receivable's docstring;
    this must match that sign handling or every comparison would show a
    spurious mismatch even when the underlying data agrees.
    """
    rows = []
    offset = 0
    while True:
        batch = supa.table("outstanding").select("customer_id,pending_amount").range(offset, offset + 999).execute().data
        rows.extend(batch)
        if len(batch) < 1000: break
        offset += 1000

    total = 0.0
    by_staff = {}
    for r in rows:
        amt = float(r["pending_amount"] or 0)
        total += amt
        staff = staff_by_id.get(r["customer_id"], "Unassigned")
        by_staff[staff] = by_staff.get(staff, 0.0) + amt
    return {"total": total, "by_staff": by_staff}


# ── Comparison + reporting ──────────────────────────────────────────────────

def _cmp(label: str, tally_val: float, supa_val: float, tally_count=None, supa_count=None,
         pending_total: float = 0.0, pending_count: int = 0, informational: bool = False) -> dict:
    """
    pending_total/pending_count are vouchers newer than the sync's own
    AlterID threshold (see fetch_tally_sales/fetch_tally_collections) — they
    are NOT part of tally_val/tally_count and never affect `pass`, only
    reported for visibility ("N pending" in the printed table).

    informational=True marks a check that's shown but never allowed to fail
    the run — used for the live-Tally outstanding comparison, which is kept
    purely for visibility once the snapshot-based comparison became the
    primary (gating) one. `pass` is still computed honestly either way;
    run_audit()/print_report() are what actually treat informational checks
    as non-gating.
    """
    diff = round(tally_val - supa_val, 2)
    passed = abs(diff) <= PASS_TOLERANCE_RUPEES
    if tally_count is not None:
        count_diff = tally_count - supa_count
        passed = passed and count_diff == 0
    else:
        count_diff = None
    return {
        "metric": label,
        "tally_value": round(tally_val, 2),
        "supabase_value": round(supa_val, 2),
        "diff": diff,
        "tally_count": tally_count,
        "supabase_count": supa_count,
        "count_diff": count_diff,
        "pending_total": round(pending_total, 2),
        "pending_count": pending_count,
        "informational": informational,
        "pass": passed,
    }


def run_audit() -> dict:
    if not check_tally():
        return {"overall_pass": False, "checks": [], "detail": {"error": "Tally not reachable"}}

    today     = date.today()
    fy_start  = _fy_start(today)
    month_start = today.replace(day=1)
    periods = {
        "today":        (today, today),
        "yesterday":    (_add_days(today, -1), _add_days(today, -1)),
        "last_7_days":  (_add_days(today, -6), today),
        "month_to_date": (month_start, today),
    }

    supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

    # Customer names for the collections exclusion check (same rule as
    # sync_today_collections: a party only counts if it resolves to a
    # real customer row).
    all_customers = []
    offset = 0
    while True:
        batch = supa.table("customers").select("customer_name").range(offset, offset + 999).execute().data
        all_customers.extend(batch)
        if len(batch) < 1000: break
        offset += 1000
    customer_names = {c["customer_name"].strip().lower() for c in all_customers}

    # AlterID thresholds the last sync run actually captured — see
    # _load_sync_state's docstring. A voucher newer than these is expected
    # to be missing from Supabase (the sync hasn't seen it yet), not a bug.
    sync_state = _load_sync_state()
    sales_alterid_threshold = sync_state.get("last_alterid", 0)
    coll_alterid_threshold  = sync_state.get("last_receipt_alterid", 0)

    checks = []
    for period_key, (from_d, to_d) in periods.items():
        t_sales = fetch_tally_sales(from_d, to_d, sales_alterid_threshold)
        s_sales = fetch_supabase_sales(supa, from_d, to_d, t_sales["pending_voucher_numbers"])
        checks.append(_cmp(
            f"sales_total[{period_key}]", t_sales["total"], s_sales["total"], t_sales["count"], s_sales["count"],
            t_sales["pending_total"], t_sales["pending_count"],
        ))

        t_coll = fetch_tally_collections(from_d, to_d, customer_names, coll_alterid_threshold)
        s_coll = fetch_supabase_collections(supa, from_d, to_d, t_coll["pending_voucher_numbers"])
        checks.append(_cmp(
            f"collections_total[{period_key}]", t_coll["total"], s_coll["total"], t_coll["count"], s_coll["count"],
            t_coll["pending_total"], t_coll["pending_count"],
        ))

    staff_by_id, staff_by_name = build_staff_maps(supa)
    s_out = fetch_supabase_outstanding(supa, staff_by_id)

    # Primary (gating) outstanding check: compare Supabase against the exact
    # Bills Receivable snapshot the sync itself just saved, not a separate
    # live fetch — see find_latest_outstanding_snapshot's docstring for why.
    # Falls back to a live fetch only if no recent snapshot exists at all
    # (e.g. audit.py run standalone, without run_audit.bat's preceding
    # --force-outstanding sync) — noted in `detail` either way so a human
    # can tell which mode produced the result.
    snapshot_path, snapshot_age_min = find_latest_outstanding_snapshot()
    outstanding_detail = {}
    if snapshot_path and snapshot_age_min <= OUTSTANDING_SNAPSHOT_MAX_AGE_MINUTES:
        t_out = fetch_tally_bills_receivable_from_snapshot(snapshot_path, staff_by_name)
        outstanding_detail["outstanding_source"] = f"snapshot:{snapshot_path.name} ({snapshot_age_min:.1f} min old)"
    else:
        t_out = fetch_tally_bills_receivable_live(fy_start, today, staff_by_name)
        outstanding_detail["outstanding_source"] = (
            "LIVE (no recent tally_outstanding_*.xml snapshot found — "
            f"newest is {snapshot_age_min:.1f} min old)" if snapshot_path
            else "LIVE (no tally_outstanding_*.xml snapshot exists at all)"
        )

    checks.append(_cmp("outstanding_total", t_out["total"], s_out["total"]))
    all_staff = set(t_out["by_staff"]) | set(s_out["by_staff"])
    for staff in sorted(all_staff):
        checks.append(_cmp(f"outstanding_staff[{staff}]", t_out["by_staff"].get(staff, 0.0), s_out["by_staff"].get(staff, 0.0)))

    # Separate, informational-only live comparison — only meaningful (and
    # only attempted) when the primary check above used the saved snapshot;
    # if it already had to fall back to live, this would just be a duplicate.
    if snapshot_path and snapshot_age_min <= OUTSTANDING_SNAPSHOT_MAX_AGE_MINUTES:
        try:
            t_out_live = fetch_tally_bills_receivable_live(fy_start, today, staff_by_name)
            checks.append(_cmp("outstanding_total_live", t_out_live["total"], s_out["total"], informational=True))
            all_staff_live = set(t_out_live["by_staff"]) | set(s_out["by_staff"])
            for staff in sorted(all_staff_live):
                checks.append(_cmp(
                    f"outstanding_staff_live[{staff}]", t_out_live["by_staff"].get(staff, 0.0), s_out["by_staff"].get(staff, 0.0),
                    informational=True,
                ))
        except Exception as exc:
            outstanding_detail["outstanding_live_error"] = str(exc)

    overall_pass = all(c["pass"] for c in checks if not c.get("informational"))
    return {
        "overall_pass": overall_pass, "checks": checks,
        "detail": {"run_at": datetime.now(UTC).isoformat(), **outstanding_detail},
    }


def print_report(result: dict):
    print("=" * 100)
    print(f"AUDIT — {datetime.now(UTC).isoformat()}")
    print("=" * 100)
    if not result["checks"]:
        print(f"COULD NOT RUN: {result.get('detail', {}).get('error', 'unknown error')}")
        return
    if result.get("detail", {}).get("outstanding_source"):
        print(f"Outstanding source: {result['detail']['outstanding_source']}")
    if result.get("detail", {}).get("outstanding_live_error"):
        print(f"Outstanding live (informational) comparison failed: {result['detail']['outstanding_live_error']}")
    header = f"{'Metric':<32} {'Tally':>16} {'Supabase':>16} {'Diff':>14} {'Counts (T/S)':>16}  Result  Pending"
    print(header)
    print("-" * len(header))
    for c in result["checks"]:
        counts = f"{c['tally_count']}/{c['supabase_count']}" if c["tally_count"] is not None else "—"
        status = "PASS" if c["pass"] else "FAIL"
        if c.get("informational"):
            status += " (info)"
        pending = f"+{c['pending_count']} (Rs {c['pending_total']:,.0f})" if c.get("pending_count") else ""
        print(f"{c['metric']:<32} {c['tally_value']:>16,.2f} {c['supabase_value']:>16,.2f} {c['diff']:>14,.2f} {counts:>16}  {status}  {pending}")
    print("-" * len(header))
    print(f"OVERALL: {'PASS' if result['overall_pass'] else 'FAIL'}", "(informational rows above never affect this)" if any(c.get("informational") for c in result["checks"]) else "")
    total_pending = sum(c.get("pending_count", 0) for c in result["checks"])
    if total_pending:
        print(f"({total_pending} voucher(s) across all checks were newer than the sync's own AlterID threshold — excluded from the comparison above as \"pending\", not a failure.)")


def write_audit_results(result: dict):
    try:
        supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
        supa.table("audit_results").insert({
            "run_at":       datetime.now(UTC).isoformat(),
            "overall_pass": result["overall_pass"],
            "checks":       result["checks"],
            "detail":       result.get("detail"),
        }).execute()
    except Exception as exc:
        print(f"WARNING — could not write to audit_results (non-fatal): {exc}", file=sys.stderr)


def main():
    start = time.monotonic()
    result = run_audit()
    print_report(result)
    write_audit_results(result)
    print(f"\nDone in {time.monotonic() - start:.1f}s")
    sys.exit(0 if result["overall_pass"] else 1)


if __name__ == "__main__":
    main()
