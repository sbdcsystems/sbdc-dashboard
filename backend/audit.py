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
  - Outstanding: current total ledger balance (all ages — this does NOT
    re-verify the recent/stale 12-month bucket split Step 3 computes, only
    that the FULL current balance matches Tally right now) + per-staff,
    using the same _STAFF_GROUPS mapping as auto_insert_new_customers().

Prints a PASS/FAIL table and writes one row to audit_results (best-effort —
see CLAUDE.md for the table's GRANT/RLS setup; a missing table must not
crash this script, matching the sync_status convention).

Standalone by design — does not import tally_sync_runner.py, so running
this can't trigger that module's import-time side effects (log file
creation, sync.lock handling, etc.). Rebuilds its own minimal TDL request
builders instead, following the same pattern as probe_alterid.py and
probe_ledger_phones.py.

Run manually:
    python audit.py
Run automatically once a day after the 12:00 sync (see CLAUDE.md for the
Windows Task Scheduler command) — and per CLAUDE.md's standing rule, run
this manually and confirm PASS after any change to sync or frontend data
logic, before calling that work done.

NOT independently confirmed on this Tally install (flagging rather than
guessing, per this codebase's convention): whether Ledger ClosingBalance's
sign convention needs adjustment for Sundry Debtor-side ledgers. This script
compares absolute values on both sides specifically to sidestep that
uncertainty — eyeball the first real run's per-staff numbers against Tally's
own Outstanding Statement before trusting this check long-term.
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

# Mirrors tally_sync_runner.py's _STAFF_GROUPS — kept as a literal copy here
# (not imported) so this script has no dependency on that module at all.
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

PASS_TOLERANCE_RUPEES = 1.0  # rounding noise only, not a real mismatch


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


def _build_ledger_request() -> str:
    return (
        "<ENVELOPE>"
        "<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        "<TYPE>Collection</TYPE><ID>AuditLedgers</ID>"
        "</HEADER>"
        "<BODY><DESC>"
        "<STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        "</STATICVARIABLES>"
        "<TDL><TDLMESSAGE>"
        '<COLLECTION NAME="AuditLedgers" ISMODIFY="No">'
        "<TYPE>Ledger</TYPE>"
        "<FETCH>NAME,PARENT,ClosingBalance</FETCH>"
        "</COLLECTION>"
        "</TDLMESSAGE></TDL>"
        "</DESC></BODY>"
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

def fetch_tally_sales(from_date: date, to_date: date) -> dict:
    type_formula = " OR ".join(f'$VoucherTypeName = "{t}"' for t in SALES_VOUCHER_TYPES)
    formula = (
        f'$Date >= $$Date:"{_tally_date_literal(from_date)}" AND '
        f'$Date <= $$Date:"{_tally_date_literal(to_date)}" AND '
        f'NOT $IsCancelled AND ({type_formula})'
    )
    xml = _post(_build_voucher_request("AuditSales", "Amount", formula))
    _raise_on_tally_error(xml)
    vouchers = re.findall(r"<VOUCHER\b.*?</VOUCHER>", xml, re.DOTALL)
    total = 0.0
    for v in vouchers:
        m = re.search(r"<AMOUNT\b[^>]*>(.*?)</AMOUNT>", v)
        if m:
            try: total += abs(float(m.group(1)))
            except ValueError: pass
    return {"total": total, "count": len(vouchers)}


def fetch_tally_collections(from_date: date, to_date: date, customer_names: set) -> dict:
    type_formula = " OR ".join(f'$VoucherTypeName = "{t}"' for t in RECEIPT_VOUCHER_TYPES)
    formula = (
        f'$Date >= $$Date:"{_tally_date_literal(from_date)}" AND '
        f'$Date <= $$Date:"{_tally_date_literal(to_date)}" AND '
        f'NOT $IsCancelled AND ({type_formula})'
    )
    xml = _post(_build_voucher_request("AuditColl", "PartyLedgerName,Amount", formula))
    _raise_on_tally_error(xml)
    vouchers = re.findall(r"<VOUCHER\b.*?</VOUCHER>", xml, re.DOTALL)
    total, count = 0.0, 0
    for v in vouchers:
        party_m = re.search(r"<PARTYLEDGERNAME\b[^>]*>(.*?)</PARTYLEDGERNAME>", v)
        amt_m   = re.search(r"<AMOUNT\b[^>]*>(.*?)</AMOUNT>", v)
        if not party_m or not amt_m:
            continue
        party = html.unescape(party_m.group(1)).strip().lower()
        if party not in customer_names:
            continue  # same exclusion rule as sync_today_collections()
        try:
            total += abs(float(amt_m.group(1)))
            count += 1
        except ValueError:
            pass
    return {"total": total, "count": count}


def fetch_tally_outstanding() -> dict:
    xml = _post(_build_ledger_request())
    _raise_on_tally_error(xml)
    total = 0.0
    by_staff = {}
    for raw_name, block in re.findall(r'<LEDGER NAME="(.*?)"[^>]*>(.*?)</LEDGER>', xml, re.DOTALL):
        parent_m = re.search(r"<PARENT\b[^>]*>(.*?)</PARENT>", block)
        parent = html.unescape(parent_m.group(1)).strip() if parent_m else ""
        staff = _STAFF_GROUPS.get(parent)
        if staff is None and "(GT)" in parent:
            staff = "Thiagarajan"
        if staff is None:
            continue
        cb_m = re.search(r"<CLOSINGBALANCE\b[^>]*>(.*?)</CLOSINGBALANCE>", block)
        if not cb_m:
            continue
        try:
            bal = abs(float(cb_m.group(1)))
        except ValueError:
            continue
        total += bal
        by_staff[staff] = by_staff.get(staff, 0.0) + bal
    return {"total": total, "by_staff": by_staff}


# ── Supabase-side fetches (same query logic the frontend uses) ─────────────

def fetch_supabase_sales(supa, from_date: date, to_date: date) -> dict:
    rows = []
    offset = 0
    while True:
        batch = (
            supa.table("sales_history").select("amount")
            .gte("sale_date", from_date.isoformat()).lte("sale_date", to_date.isoformat())
            .range(offset, offset + 999).execute().data
        )
        rows.extend(batch)
        if len(batch) < 1000: break
        offset += 1000
    total = sum(float(r["amount"] or 0) for r in rows)
    return {"total": total, "count": len(rows)}


def fetch_supabase_collections(supa, from_date: date, to_date: date) -> dict:
    rows = (
        supa.table("daily_collections").select("total_amount,invoice_count")
        .gte("sale_date", from_date.isoformat()).lte("sale_date", to_date.isoformat())
        .execute().data
    )
    total = sum(float(r["total_amount"] or 0) for r in rows)
    count = sum(r["invoice_count"] or 0 for r in rows)
    return {"total": total, "count": count}


def fetch_supabase_outstanding(supa) -> dict:
    customers = []
    offset = 0
    while True:
        batch = supa.table("customers").select("id,assigned_to").range(offset, offset + 999).execute().data
        customers.extend(batch)
        if len(batch) < 1000: break
        offset += 1000
    users = supa.table("users").select("id,name").execute().data
    name_by_uid = {u["id"]: u["name"] for u in users}
    staff_by_cust = {c["id"]: name_by_uid.get(c["assigned_to"], "Unassigned") for c in customers}

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
        amt = abs(float(r["pending_amount"] or 0))
        total += amt
        staff = staff_by_cust.get(r["customer_id"], "Unassigned")
        by_staff[staff] = by_staff.get(staff, 0.0) + amt
    return {"total": total, "by_staff": by_staff}


# ── Comparison + reporting ──────────────────────────────────────────────────

def _cmp(label: str, tally_val: float, supa_val: float, tally_count=None, supa_count=None) -> dict:
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

    checks = []
    for period_key, (from_d, to_d) in periods.items():
        t_sales = fetch_tally_sales(from_d, to_d)
        s_sales = fetch_supabase_sales(supa, from_d, to_d)
        checks.append(_cmp(f"sales_total[{period_key}]", t_sales["total"], s_sales["total"], t_sales["count"], s_sales["count"]))

        t_coll = fetch_tally_collections(from_d, to_d, customer_names)
        s_coll = fetch_supabase_collections(supa, from_d, to_d)
        checks.append(_cmp(f"collections_total[{period_key}]", t_coll["total"], s_coll["total"], t_coll["count"], s_coll["count"]))

    t_out = fetch_tally_outstanding()
    s_out = fetch_supabase_outstanding(supa)
    checks.append(_cmp("outstanding_total", t_out["total"], s_out["total"]))
    all_staff = set(t_out["by_staff"]) | set(s_out["by_staff"])
    for staff in sorted(all_staff):
        checks.append(_cmp(f"outstanding_staff[{staff}]", t_out["by_staff"].get(staff, 0.0), s_out["by_staff"].get(staff, 0.0)))

    overall_pass = all(c["pass"] for c in checks)
    return {"overall_pass": overall_pass, "checks": checks, "detail": {"run_at": datetime.now(UTC).isoformat()}}


def print_report(result: dict):
    print("=" * 100)
    print(f"AUDIT — {datetime.now(UTC).isoformat()}")
    print("=" * 100)
    if not result["checks"]:
        print(f"COULD NOT RUN: {result.get('detail', {}).get('error', 'unknown error')}")
        return
    header = f"{'Metric':<32} {'Tally':>16} {'Supabase':>16} {'Diff':>14} {'Counts (T/S)':>16}  Result"
    print(header)
    print("-" * len(header))
    for c in result["checks"]:
        counts = f"{c['tally_count']}/{c['supabase_count']}" if c["tally_count"] is not None else "—"
        status = "PASS" if c["pass"] else "FAIL"
        print(f"{c['metric']:<32} {c['tally_value']:>16,.2f} {c['supabase_value']:>16,.2f} {c['diff']:>14,.2f} {counts:>16}  {status}")
    print("-" * len(header))
    print(f"OVERALL: {'PASS' if result['overall_pass'] else 'FAIL'}")


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
