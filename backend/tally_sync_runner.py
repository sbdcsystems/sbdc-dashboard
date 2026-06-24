"""
tally_sync_runner.py — automated Tally -> Supabase outstanding sync.

Modes:
  python tally_sync_runner.py                         # full sync (requires office network)
  python tally_sync_runner.py --from-local            # skip Tally fetch, use existing tally_with_dates.xml
  python tally_sync_runner.py --from-local --dry-run  # parse + map only, no DB changes

Safety guarantees:
  - Old data is only deleted AFTER new data is fully inserted (insert-first pattern).
    If the insert fails partway, the table retains the previous sync's data intact.
  - A sanity check aborts the whole run before touching the DB if the freshly fetched
    bill count has dropped more than 50% vs the current DB count.
  - On failure: writes last_sync_status.json and sends an email if SMTP is configured.

Logs: backend/logs/sync_YYYYMMDD_HHMMSS.log
Email config (optional, add to root .env):
  SMTP_FROM=your.gmail@gmail.com
  SMTP_PASSWORD=xxxx xxxx xxxx xxxx   # Gmail App Password (not your login password)
  NOTIFY_EMAIL=number.to.notify@gmail.com
"""

import os
import json
import re
import html
import logging
import smtplib
import sys
from datetime import datetime, timedelta, date
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
import requests
from supabase import create_client

# ── Paths & run identity ───────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
LOG_DIR  = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

RUN_TS         = datetime.now().strftime("%Y%m%d_%H%M%S")
SYNC_TIMESTAMP = datetime.utcnow().isoformat()   # stored in synced_from_tally_at column
log_path       = LOG_DIR / f"sync_{RUN_TS}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(log_path, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

load_dotenv(BASE_DIR.parent / ".env", override=True)

TALLY_IP      = os.environ.get("TALLY_SERVER_IP", "192.168.0.205")
TALLY_PORT    = int(os.environ.get("TALLY_PORT", "9000"))
TALLY_URL     = f"http://{TALLY_IP}:{TALLY_PORT}"
TALLY_COMPANY = os.environ.get("TALLY_COMPANY_NAME", "SUPREME BALAJI DYE CHEM - 25-26")
TALLY_TIMEOUT = 60

RECENT_MONTHS       = 12    # bills older than this -> age_status "stale"
XML_KEEP_DAYS       = 7     # delete XML backups older than this
SUPABASE_BATCH      = 200   # records per insert call
SANITY_DROP_LIMIT   = 0.50  # abort if new bill count < 50% of current DB count

# ── Group → staff mapping (mirrors full_customer_import.py) ───────────────────

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
_CASH_GROUP          = "4.Cash - Parties"
_BAD_DEBT_CURRENT    = "5.Bad Debtors 24-25"
_CASE_FILED          = "6.Case Filed Customers"
_BAD_DEBT_HISTORICAL = "Bad Debts Written Off"


def _fy_start() -> date:
    today = date.today()
    return date(today.year, 4, 1) if today.month >= 4 else date(today.year - 1, 4, 1)


def _extract_phone(text: str) -> str | None:
    if not text:
        return None
    for segment in text.split(","):
        digits = "".join(ch for ch in segment if ch.isdigit())
        if len(digits) == 10 and digits[0] in "6789":
            return digits
        if len(digits) in (10, 11) and digits[0] == "0":
            return digits
    return None


# ── Status file ────────────────────────────────────────────────────────────────

def _write_status(status: str, detail: dict):
    """Write last_sync_status.json so the dashboard (or a manual check) can surface it."""
    payload = {
        "status":    status,
        "run_ts":    RUN_TS,
        "timestamp": datetime.now().isoformat(),
        **detail,
    }
    status_path = BASE_DIR / "last_sync_status.json"
    status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ── Email notification ─────────────────────────────────────────────────────────

def _send_skip_alert_email(unmatched: dict):
    """
    Send an alert when bills are skipped due to unknown customer names.
    Silently skipped if SMTP not configured.
    """
    smtp_from = os.environ.get("SMTP_FROM")
    smtp_pass = os.environ.get("SMTP_PASSWORD")
    notify_to = os.environ.get("NOTIFY_EMAIL")

    if not all([smtp_from, smtp_pass, notify_to]):
        log.info("  Skip-alert email skipped (SMTP not configured in .env)")
        return

    lines = "\n".join(
        f"  {count} bill(s) — {name}"
        for name, count in sorted(unmatched.items(), key=lambda x: -x[1])
    )
    body = (
        f"The SBDC Tally sync completed at {datetime.now().strftime('%Y-%m-%d %H:%M')} "
        f"but {len(unmatched)} customer name(s) from Tally were not found in the database.\n\n"
        f"Skipped customers:\n{lines}\n\n"
        "Action: add these customers to Supabase so their bills are included in future syncs.\n\n"
        "The sync itself succeeded — all other bills loaded correctly.\n"
    )
    msg = MIMEText(body)
    msg["Subject"] = (
        f"SBDC Sync Warning — {len(unmatched)} customer(s) skipped "
        f"[{datetime.now().strftime('%d %b %Y')}]"
    )
    msg["From"] = smtp_from
    msg["To"]   = notify_to

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(smtp_from, smtp_pass)
            smtp.sendmail(smtp_from, notify_to, msg.as_string())
        log.info("  Skip alert sent to %s", notify_to)
    except Exception as exc:
        log.warning("  Could not send skip alert email: %s", exc)


def _send_failure_email(error_summary: str):
    """
    Send a failure notification email via Gmail SMTP.
    Silently skipped if SMTP_FROM / SMTP_PASSWORD / NOTIFY_EMAIL are not in .env.
    """
    smtp_from  = os.environ.get("SMTP_FROM")
    smtp_pass  = os.environ.get("SMTP_PASSWORD")
    notify_to  = os.environ.get("NOTIFY_EMAIL")

    if not all([smtp_from, smtp_pass, notify_to]):
        log.info("  Email notification skipped (SMTP not configured in .env)")
        return

    body = (
        f"The SBDC Tally sync FAILED at {datetime.now().strftime('%Y-%m-%d %H:%M')}.\n\n"
        f"Error:\n{error_summary}\n\n"
        f"Log file:\n{log_path}\n\n"
        "Check the log file and run the sync manually once fixed:\n"
        "  cd C:\\Users\\vsome\\Desktop\\sbdc-system\\backend\n"
        "  ..\\venv\\Scripts\\activate.bat\n"
        "  python tally_sync_runner.py\n"
    )
    msg = MIMEText(body)
    msg["Subject"] = f"SBDC Tally Sync FAILED — {datetime.now().strftime('%d %b %Y %H:%M')}"
    msg["From"]    = smtp_from
    msg["To"]      = notify_to

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(smtp_from, smtp_pass)
            smtp.sendmail(smtp_from, notify_to, msg.as_string())
        log.info("  Failure notification sent to %s", notify_to)
    except Exception as exc:
        log.warning("  Could not send failure email: %s", exc)


# ── Step 4.5: Auto-insert new customers from Tally ledger master ──────────────

def _fetch_tally_ledger_master() -> dict:
    """
    Pull every ledger record from Tally and return
    {name.lower(): {name, parent, phone, address, gstin}}.
    Used to resolve new customer names found in Bills Receivable.
    """
    xml_body = (
        "<ENVELOPE>"
        "<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        "<TYPE>Collection</TYPE><ID>AllLedgers</ID>"
        "</HEADER>"
        "<BODY><DESC>"
        "<STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        "</STATICVARIABLES>"
        "<TDL><TDLMESSAGE>"
        '<COLLECTION NAME="AllLedgers" ISMODIFY="No">'
        "<TYPE>Ledger</TYPE>"
        "<FETCH>NAME,PARENT,LEDGERPHONE,ADDRESS,PARTYGSTIN</FETCH>"
        "</COLLECTION>"
        "</TDLMESSAGE></TDL>"
        "</DESC></BODY>"
        "</ENVELOPE>"
    )
    r   = requests.post(
        TALLY_URL, data=xml_body.encode("utf-8"),
        headers={"Content-Type": "text/xml"}, timeout=90,
    )
    xml = r.content.decode("utf-8", errors="replace")

    result = {}
    for raw_name, block in re.findall(
        r'<LEDGER NAME="(.*?)"[^>]*>(.*?)</LEDGER>', xml, re.DOTALL
    ):
        name     = html.unescape(raw_name.strip())
        parent_m = re.search(r"<PARENT\b[^>]*>(.*?)</PARENT>", block)
        parent   = html.unescape(parent_m.group(1).strip()) if parent_m else None

        phone    = None
        phone_m  = re.search(r"<LEDGERPHONE\b[^>]*>(.*?)</LEDGERPHONE>", block)
        if phone_m:
            phone = _extract_phone(html.unescape(phone_m.group(1)))

        addr_lines = [
            html.unescape(a)
            for a in re.findall(r'<ADDRESS TYPE="String">(.*?)</ADDRESS>', block)
        ]
        if not phone:
            for line in addr_lines:
                phone = _extract_phone(line)
                if phone:
                    break

        gstin_m = re.search(r"<PARTYGSTIN\b[^>]*>(.*?)</PARTYGSTIN>", block)
        gstin   = html.unescape(gstin_m.group(1).strip()) if gstin_m else None

        result[name.lower()] = {
            "name":    name,
            "parent":  parent,
            "phone":   phone,
            "address": ", ".join(addr_lines) if addr_lines else None,
            "gstin":   gstin,
        }
    return result


def auto_insert_new_customers(bills: list, ledger_data: "dict | None" = None, dry_run: bool = False) -> list:
    """
    Step 4.5 — For each customer name in bills that is not yet in the customers
    table, fetch their ledger data from Tally and auto-insert using the same
    group→staff assignment mapping as full_customer_import.py.

    Returns list of newly inserted customer names (or would-be names in dry_run).
    """
    log.info("Step 4.5 — Checking for new customers to auto-insert")

    supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

    # Load existing customer names
    cust_map = {}
    offset   = 0
    while True:
        batch = (
            supa.table("customers")
            .select("customer_name")
            .range(offset, offset + 999)
            .execute().data
        )
        for row in batch:
            cust_map[row["customer_name"].strip().lower()] = True
        if len(batch) < 1000:
            break
        offset += 1000

    missing = sorted({
        b["customer_name"].strip()
        for b in bills
        if b["customer_name"].strip().lower() not in cust_map
    })

    if not missing:
        log.info("  All bill customers already in database — nothing to auto-insert")
        return []

    log.info("  %d customer(s) not in database: %s", len(missing), missing)
    if ledger_data is None:
        log.info("  Fetching ledger master from Tally...")
        ledger_data = _fetch_tally_ledger_master()
        log.info("  Ledger master: %d records fetched", len(ledger_data))

    users        = supa.table("users").select("id, name").execute().data
    user_by_name = {u["name"]: u["id"] for u in users}

    inserted_names = []
    unrecognised   = []   # (name, parent) — inserted with assigned_to=NULL
    not_found      = []   # not in Tally ledger master at all

    for name in missing:
        ldata = ledger_data.get(name.lower())
        if not ldata:
            log.warning("  '%s' not found in Tally ledger master — skipping", name)
            not_found.append(name)
            continue

        parent = ldata["parent"] or ""

        is_gt               = "(GT)" in parent
        is_staff            = parent in _STAFF_GROUPS
        is_cash             = parent == _CASH_GROUP
        is_bad_debt_current = parent == _BAD_DEBT_CURRENT
        is_case_filed       = parent == _CASE_FILED
        is_bad_debt_hist    = parent == _BAD_DEBT_HISTORICAL
        known_group = (
            is_gt or is_staff or is_cash
            or is_bad_debt_current or is_case_filed or is_bad_debt_hist
            or parent == "Sundry Debtors"
            or parent.endswith("Group")
        )

        if not known_group:
            log.warning(
                "  '%s' has unrecognised PARENT group '%s' — inserting with assigned_to=NULL",
                name, parent,
            )
            unrecognised.append((name, parent))

        assigned_to = None
        if is_gt:
            assigned_to = user_by_name.get("Thiagarajan")
        elif is_staff:
            assigned_to = user_by_name.get(_STAFF_GROUPS[parent])

        flagged        = False
        flagged_reason = None
        if is_bad_debt_current:
            flagged        = True
            flagged_reason = f"Bad debtor (Tally group: {parent})"
        elif is_case_filed:
            flagged        = True
            flagged_reason = "Case filed - legal recovery in progress"
        elif is_bad_debt_hist:
            flagged        = True
            flagged_reason = "Historical bad debt - written off in Tally, not currently trading"

        staff_label = (
            "Thiagarajan" if is_gt
            else _STAFF_GROUPS.get(parent, "NULL (unrecognised group)")
        )
        record = {
            "customer_name":  name,
            "customer_type":  "cash" if is_cash else "credit",
            "credit_days":    None if is_cash else 90,
            "assigned_to":    assigned_to,
            "phone":          ldata["phone"],
            "address":        ldata["address"],
            "gst_number":     ldata["gstin"],
            "flagged":        flagged,
            "flagged_reason": flagged_reason,
        }

        if dry_run:
            log.info(
                "  DRY RUN — would insert '%s' (group: %s → %s)",
                name, parent, staff_label,
            )
            inserted_names.append(name)
            continue

        try:
            supa.table("customers").insert(record).execute()
            log.info(
                "  AUTO-INSERTED '%s' (group: %s → %s)",
                name, parent, staff_label,
            )
            inserted_names.append(name)
        except Exception as exc:
            log.warning("  Failed to insert '%s': %s", name, exc)

    # ── Summary ───────────────────────────────────────────────────────────────
    log.info(
        "  Step 4.5 done: %d auto-inserted | %d unrecognised group (NULL assignment) | "
        "%d not found in Tally ledger",
        len(inserted_names), len(unrecognised), len(not_found),
    )
    if unrecognised:
        for n, g in unrecognised:
            log.warning("    Unrecognised group: '%s' -> parent='%s'", n, g)
    if not_found:
        for n in not_found:
            log.warning("    Not in Tally ledger master: '%s'", n)
    return inserted_names


# ── Step 4.6: Refresh contact/address fields from Tally ledger master ─────────

def refresh_ledger_contacts(ledger_data: dict, dry_run: bool = False) -> int:
    """
    Step 4.6 — Compare phone, address, and gst_number for every existing customer
    against the Tally ledger master and update any that have changed.
    Never touches customer_name, assigned_to, flagged, or customer_type.
    Returns number of customers updated.
    """
    def _norm(v):
        return (v or "").strip() or None

    supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

    customers = []
    offset    = 0
    while True:
        batch = (
            supa.table("customers")
            .select("id, customer_name, phone, address, gst_number")
            .range(offset, offset + 999)
            .execute().data
        )
        customers.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000

    updates = []
    for cust in customers:
        ldata = ledger_data.get(cust["customer_name"].strip().lower())
        if not ldata:
            continue
        changed = {}
        if _norm(cust["phone"])      != _norm(ldata["phone"]):   changed["phone"]      = _norm(ldata["phone"])
        if _norm(cust["address"])    != _norm(ldata["address"]): changed["address"]    = _norm(ldata["address"])
        if _norm(cust["gst_number"]) != _norm(ldata["gstin"]):   changed["gst_number"] = _norm(ldata["gstin"])
        if changed:
            updates.append((cust["id"], changed))

    if not updates:
        log.info("  Ledger refresh: no changes")
        return 0

    if dry_run:
        log.info("  Ledger refresh: DRY RUN — would update %d customer(s)", len(updates))
        return len(updates)

    for cid, changed in updates:
        supa.table("customers").update(changed).eq("id", cid).execute()

    log.info("  Ledger refresh: %d customer(s) updated", len(updates))
    return len(updates)


# ── Step 1: Tally connection check ─────────────────────────────────────────────

def check_tally() -> bool:
    log.info("Step 1 — Checking Tally connection at %s", TALLY_URL)
    try:
        requests.get(TALLY_URL, timeout=5)
        log.info("  OK — Tally is reachable")
        return True
    except requests.exceptions.ConnectTimeout:
        log.error("  FAILED — Connection timed out. Are you on the office network?")
    except requests.exceptions.ConnectionError as exc:
        log.error("  FAILED — %s", exc)
    return False


# ── Step 2: Fetch Bills Receivable XML ─────────────────────────────────────────

def fetch_tally_xml() -> str:
    fy    = _fy_start()
    today = date.today()
    log.info(
        "Step 2 — Fetching Bills Receivable (%s to %s)",
        fy.strftime("%d %b %Y"), today.strftime("%d %b %Y"),
    )

    xml_body = (
        "<ENVELOPE>"
        "<HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>"
        "<BODY><EXPORTDATA><REQUESTDESC>"
        "<REPORTNAME>Bills Receivable</REPORTNAME>"
        "<STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        f"<SVFROMDATE>{fy.strftime('%Y%m%d')}</SVFROMDATE>"
        f"<SVTODATE>{today.strftime('%Y%m%d')}</SVTODATE>"
        "<EXPLODEFLAG>Yes</EXPLODEFLAG>"
        "</STATICVARIABLES>"
        "</REQUESTDESC></EXPORTDATA></BODY>"
        "</ENVELOPE>"
    )

    r   = requests.post(
        TALLY_URL, data=xml_body.encode("utf-8"),
        headers={"Content-Type": "text/xml"}, timeout=TALLY_TIMEOUT,
    )
    raw   = r.content
    q_pct = raw.count(b"?") / max(len(raw), 1) * 100
    log.info("  Response: %d bytes, %.1f%% question marks", len(raw), q_pct)

    if len(raw) < 200:
        raise RuntimeError(
            f"Tally returned only {len(raw)} bytes — is the correct company open?"
        )
    if q_pct > 70:
        raise RuntimeError(
            f"Response is {q_pct:.0f}% question marks — encoding error or wrong company. "
            f"Expected: {TALLY_COMPANY}"
        )

    backup = BASE_DIR / f"tally_outstanding_{RUN_TS}.xml"
    backup.write_bytes(raw)
    log.info("  Saved XML backup: %s", backup.name)

    cutoff = datetime.now().timestamp() - XML_KEEP_DAYS * 86400
    for old in BASE_DIR.glob("tally_outstanding_*.xml"):
        if old.stat().st_mtime < cutoff:
            old.unlink()
            log.info("  Deleted old backup: %s", old.name)

    return raw.decode("utf-8", errors="replace")


# ── Step 3: Parse XML -> bill list ─────────────────────────────────────────────

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

# Voucher types in Bills Receivable that represent credits the customer has already paid
# (on-account / advance — unmatched to any specific invoice). These should be stored as
# negative pending_amount so SUM() in the views naturally nets them against outstanding bills.
_CREDIT_VCH_TYPES = {"Payment", "Receipt"}

# KNOWN LIMITATION — "Opening Balance On Account" credits are not captured.
#
# When a customer has an on-account credit that originated as an opening balance
# (i.e. they overpaid in a prior financial year and the credit carried forward),
# it does NOT appear in Bills Receivable as a Payment/Receipt voucher. It only
# exists in the ledger's closing balance (CLOSINGBALANCE), which this sync cannot
# fetch: Tally's API times out computing CLOSINGBALANCE even for a single ledger
# with a large bill history.
#
# In practice this only affects DORMANT accounts — ones with no new invoices or
# receipts in the current FY — because for those, the opening balance equals the
# current closing balance and the discrepancy is stable. For any active customer
# (new bills, payments, etc.) the on-account credit would already be reconciled
# against a specific invoice in Tally and would then disappear from Bills
# Receivable correctly.
#
# Confirmed case: Sri Bhadri Narayana Textiles (flagged Bad Debts Written Off).
# Bills Receivable shows Rs 14,00,846 across 147 stale invoices (2013-2016);
# true Tally balance is Rs 6,06,949 Dr due to an opening-balance credit of
# Rs ~7,94,000. The account has had zero activity since ~2017, so the gap is
# permanent but not a live collections concern. Do not remove this comment and
# treat OPENINGBALANCE from the ledger master as a reliable proxy — it is only
# coincidentally correct for zero-activity accounts like Bhadri.


def parse_xml(xml_text: str) -> list:
    log.info("Step 3 — Parsing Bills Receivable XML")
    matches = _BILL_RE.findall(xml_text)
    log.info("  Found %d bill entries in XML", len(matches))

    if not matches:
        raise RuntimeError(
            "No bill entries found in the XML — structure may have changed."
        )

    cutoff = datetime.now().date() - timedelta(days=RECENT_MONTHS * 30)
    bills  = []

    n_credits = 0
    for date_raw, ref, party, cl_raw, due_raw, overdue_raw, vch_type in matches:
        try:
            inv_date = datetime.strptime(date_raw.strip(), "%d-%b-%y").date()
        except ValueError:
            inv_date = None
        try:
            due_date = datetime.strptime(due_raw.strip(), "%d-%b-%y").date()
        except ValueError:
            due_date = None
        try:
            raw_amt = abs(float(cl_raw.strip()))
            is_credit = vch_type.strip() in _CREDIT_VCH_TYPES
            amount = -raw_amt if is_credit else raw_amt
            if is_credit:
                n_credits += 1
        except ValueError:
            amount = 0.0
        try:
            overdue = int(float(overdue_raw.strip()))
        except ValueError:
            overdue = 0

        if overdue <= 30:    bucket = "0-30"
        elif overdue <= 60:  bucket = "30-60"
        elif overdue <= 90:  bucket = "60-90"
        elif overdue <= 120: bucket = "90-120"
        else:                bucket = "120+"

        if inv_date is None:      age = "unknown"
        elif inv_date >= cutoff:  age = "recent"
        else:                     age = "stale"

        bills.append({
            "customer_name":  html.unescape(party.strip()),
            "invoice_ref":    ref.strip(),
            "invoice_date":   inv_date.isoformat() if inv_date else None,
            "due_date":       due_date.isoformat() if due_date else None,
            "pending_amount": round(amount, 2),
            "days_overdue":   overdue,
            "bucket":         bucket,
            "age_status":     age,
        })

    n_recent  = sum(1 for b in bills if b["age_status"] == "recent" and b["pending_amount"] > 0)
    n_stale   = sum(1 for b in bills if b["age_status"] == "stale"  and b["pending_amount"] > 0)
    n_unknown = sum(1 for b in bills if b["age_status"] == "unknown" and b["pending_amount"] > 0)
    log.info(
        "  Tagged: %d recent (Rs %s), %d stale (Rs %s), %d unknown date",
        n_recent,  f"{sum(b['pending_amount'] for b in bills if b['age_status']=='recent'):,.0f}",
        n_stale,   f"{sum(b['pending_amount'] for b in bills if b['age_status']=='stale'):,.0f}",
        n_unknown,
    )
    if n_credits:
        credit_total = sum(-b["pending_amount"] for b in bills if b["pending_amount"] < 0)
        log.info(
            "  On-account credits: %d entries totalling Rs %s (stored as negative; nets against bills)",
            n_credits, f"{credit_total:,.0f}",
        )

    (BASE_DIR / "parsed_outstanding.json").write_text(
        json.dumps(bills, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info("  Updated parsed_outstanding.json")
    return bills


# ── Steps 4-7: Safe reload of Supabase outstanding ────────────────────────────
#
# Safety pattern — insert-first, delete-old-after:
#
#   Step 4  Build customer map + map bills to DB records
#   Step 5  Sanity check: abort if new count < 50% of current DB count
#   Step 6  Clean up any partial records from a previous failed run of THIS
#           timestamp (no-op on a fresh run, protects against retry scenarios)
#   Step 7  INSERT all new records (tagged with SYNC_TIMESTAMP)
#           -- if this fails, old records (different timestamp) are untouched --
#   Step 8  DELETE all records whose timestamp != SYNC_TIMESTAMP (the old ones)
#
# Result: if Step 7 fails partway, Step 8 is never reached and the table still
# holds the previous sync's complete data. The dashboard stays accurate.

def reload_supabase(bills: list, dry_run: bool = False):
    supabase = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SECRET_KEY"],
    )

    # ── Step 4: Build customer name -> UUID map ────────────────────────────────
    log.info("Step 4 — Loading customer map from Supabase")
    cust_map = {}
    offset   = 0
    while True:
        batch = (
            supabase.table("customers")
            .select("id, customer_name")
            .range(offset, offset + 999)
            .execute().data
        )
        for row in batch:
            cust_map[row["customer_name"].strip().lower()] = row["id"]
        if len(batch) < 1000:
            break
        offset += 1000
    log.info("  %d customers in database", len(cust_map))

    records   = []
    unmatched = {}
    for b in bills:
        cid = cust_map.get(b["customer_name"].strip().lower())
        if not cid:
            unmatched[b["customer_name"]] = unmatched.get(b["customer_name"], 0) + 1
            continue
        records.append({
            "customer_id":          cid,
            "invoice_ref":          b["invoice_ref"],
            "invoice_date":         b["invoice_date"],
            "due_date":             b["due_date"],
            "pending_amount":       b["pending_amount"],
            "bucket":               b["bucket"],
            "days_overdue":         b["days_overdue"],
            "age_status":           b["age_status"],
            "synced_from_tally_at": SYNC_TIMESTAMP,
        })

    n_skipped = len(bills) - len(records)
    if unmatched:
        log.warning(
            "  %d bills skipped — name not in customer table (%d unique):",
            n_skipped, len(unmatched),
        )
        for name, count in sorted(unmatched.items(), key=lambda x: -x[1]):
            log.warning("    %dx  %s", count, name)
    log.info("  %d bills ready to load, %d skipped", len(records), n_skipped)

    # ── Step 5: Sanity check ──────────────────────────────────────────────────
    log.info("Step 5 — Sanity check")
    current_count = (
        supabase.table("outstanding")
        .select("id", count="exact")
        .execute()
        .count
    )
    log.info("  Current DB outstanding rows: %d", current_count)
    log.info("  New records to load:         %d", len(records))

    if current_count > 100 and len(records) < current_count * SANITY_DROP_LIMIT:
        raise RuntimeError(
            f"Sanity check failed: Tally returned data for {len(records)} bills "
            f"but DB currently has {current_count} — that's a drop of "
            f"{100 - len(records)/current_count*100:.0f}%, which exceeds the "
            f"{SANITY_DROP_LIMIT*100:.0f}% threshold. "
            "Aborting to protect existing data. Check Tally for issues."
        )
    log.info("  Sanity check passed.")

    if dry_run:
        log.info("  DRY RUN — all Supabase writes skipped.")
        return len(records), n_skipped, unmatched

    # ── Step 6: Clean up any partial records from a previous failed attempt ───
    # (Deletes rows tagged with THIS run's SYNC_TIMESTAMP, which on a fresh run
    # is zero rows. On a retry after a partial failure, removes the incomplete set.)
    log.info("Step 6 — Clearing any partial records from a previous failed attempt")
    retry_cleaned = 0
    while True:
        r = (
            supabase.table("outstanding")
            .delete()
            .eq("synced_from_tally_at", SYNC_TIMESTAMP)
            .execute()
        )
        retry_cleaned += len(r.data)
        if not r.data:
            break
    if retry_cleaned:
        log.info("  Removed %d partial records from a previous failed attempt", retry_cleaned)
    else:
        log.info("  No partial records to clean up (fresh run)")

    # ── Step 7: Insert all new records ────────────────────────────────────────
    # Old records (different timestamp) are untouched until Step 8.
    # If this step fails, old data remains intact.
    log.info("Step 7 — Inserting %d new records", len(records))
    inserted = 0
    for i in range(0, len(records), SUPABASE_BATCH):
        batch = records[i : i + SUPABASE_BATCH]
        supabase.table("outstanding").insert(batch).execute()
        inserted += len(batch)
        log.info("  %d / %d inserted", inserted, len(records))

    # ── Step 8: Delete old records (only reached if Step 7 fully succeeded) ───
    log.info("Step 8 — Removing old records from previous sync")
    deleted = 0
    # Old records with a different (non-null) timestamp
    while True:
        r = (
            supabase.table("outstanding")
            .delete()
            .neq("synced_from_tally_at", SYNC_TIMESTAMP)
            .execute()
        )
        deleted += len(r.data)
        if not r.data:
            break
    # Old records with no timestamp (loaded before this runner existed)
    while True:
        r = (
            supabase.table("outstanding")
            .delete()
            .is_("synced_from_tally_at", "null")
            .execute()
        )
        deleted += len(r.data)
        if not r.data:
            break
    log.info("  Deleted %d old records", deleted)

    return inserted, n_skipped, unmatched


# ── Step 9: Today's sales from Day Book ───────────────────────────────────────

def sync_today_sales(dry_run: bool = False):
    """
    Fetch today's Day Book from Tally, count sales vouchers, and upsert
    the total + count to daily_sales. Caller should catch exceptions (non-fatal).
    """
    today     = date.today()
    today_str = today.strftime("%Y%m%d")
    log.info("Step 9 — Fetching today's sales (Day Book)")

    xml_body = (
        "<ENVELOPE>"
        "<HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>"
        "<BODY><EXPORTDATA><REQUESTDESC>"
        "<REPORTNAME>Day Book</REPORTNAME>"
        "<STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        f"<SVFROMDATE>{today_str}</SVFROMDATE>"
        f"<SVTODATE>{today_str}</SVTODATE>"
        "</STATICVARIABLES>"
        "</REQUESTDESC></EXPORTDATA></BODY>"
        "</ENVELOPE>"
    )
    r   = requests.post(
        TALLY_URL, data=xml_body.encode("utf-8"),
        headers={"Content-Type": "text/xml"}, timeout=30,
    )
    xml = r.content.decode("utf-8", errors="replace")

    vouchers    = re.findall(r"<VOUCHER\b.*?</VOUCHER>", xml, re.DOTALL)
    sales_total = 0.0
    sales_count = 0
    items       = []
    seen_refs   = set()

    for v in vouchers:
        vtype = re.search(r"<VOUCHERTYPENAME[^>]*>(.*?)</VOUCHERTYPENAME>", v)
        if not (vtype and "SALES" in vtype.group(1).upper()):
            continue

        ref_m = re.search(r"<VOUCHERNUMBER[^>]*>(.*?)</VOUCHERNUMBER>", v)
        ref   = ref_m.group(1).strip() if ref_m else ""
        if ref.startswith("SO-"):
            continue  # skip Sales Order entries

        amt_m = re.search(r"<AMOUNT[^>]*>(.*?)</AMOUNT>", v)
        if not amt_m:
            continue
        try:
            raw_amt = float(amt_m.group(1))
        except ValueError:
            continue

        # Day Book returns one block per ledger entry for each voucher.
        # The party (customer) entry is a DEBIT → AMOUNT is negative in Tally XML.
        # Sales account and GST entries are CREDITs → AMOUNT is positive.
        # Only the party entry carries the GST-inclusive invoice total.
        if raw_amt >= 0:
            continue  # skip sales account and GST credit entries

        if ref in seen_refs:
            continue  # dedup: each SBDC- number should only be counted once
        seen_refs.add(ref)

        party_m = re.search(r"<PARTYLEDGERNAME[^>]*>(.*?)</PARTYLEDGERNAME>", v)
        amt = abs(raw_amt)
        sales_total += amt
        sales_count += 1
        items.append({
            "customer_name": html.unescape(party_m.group(1).strip()) if party_m else "",
            "invoice_ref":   ref,
            "amount":        round(amt, 2),
        })

    log.info(
        "  Today's sales: %d invoice(s), Rs %s",
        sales_count, f"{sales_total:,.2f}",
    )
    has_detail = any(i["customer_name"] or i["invoice_ref"] for i in items)
    log.info(
        "  Per-invoice detail: %s",
        "available" if has_detail
        else "NOT available — PARTYLEDGERNAME/VOUCHERNUMBER absent from Day Book response",
    )

    if dry_run:
        log.info("  DRY RUN — daily_sales upsert skipped")
        return

    supa = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SECRET_KEY"],
    )

    # Resolve each PARTYLEDGERNAME to the customer's DB UUID — same map pattern
    # as Bills Receivable sync. Stored in items so the frontend can do an exact
    # UUID lookup instead of fragile string matching.
    cust_id_map = {}
    offset = 0
    while True:
        batch = (
            supa.table("customers")
            .select("id, customer_name")
            .range(offset, offset + 999)
            .execute().data
        )
        for row in batch:
            cust_id_map[row["customer_name"].strip().lower()] = row["id"]
        if len(batch) < 1000:
            break
        offset += 1000

    matched = 0
    for item in items:
        cid = cust_id_map.get(item["customer_name"].strip().lower())
        item["customer_id"] = cid
        if cid:
            matched += 1
    log.info(
        "  UUID resolved: %d / %d items (%d unmatched — name variation or new customer)",
        matched, len(items), len(items) - matched,
    )

    supa.table("daily_sales").upsert(
        {
            "sale_date":     today.isoformat(),
            "total_amount":  round(sales_total, 2),
            "invoice_count": sales_count,
            "synced_at":     datetime.utcnow().isoformat(),
            "items":         items,
        },
        on_conflict="sale_date",
    ).execute()
    log.info("  daily_sales upserted for %s", today.isoformat())


# ── Step 9b: Today's collections (Receipt vouchers) → daily_collections ──────

def sync_today_collections(dry_run: bool = False):
    """
    Fetch today's Receipt vouchers from the Day Book and upsert to
    daily_collections. Same XML request as sync_today_sales — filtered on
    VOUCHERTYPENAME = 'Receipt'. Caller should catch exceptions (non-fatal).
    """
    today     = date.today()
    today_str = today.strftime("%Y%m%d")
    log.info("Step 9b — Fetching today's collections (Day Book — Receipt vouchers)")

    xml_body = (
        "<ENVELOPE>"
        "<HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>"
        "<BODY><EXPORTDATA><REQUESTDESC>"
        "<REPORTNAME>Day Book</REPORTNAME>"
        "<STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        f"<SVFROMDATE>{today_str}</SVFROMDATE>"
        f"<SVTODATE>{today_str}</SVTODATE>"
        "</STATICVARIABLES>"
        "</REQUESTDESC></EXPORTDATA></BODY>"
        "</ENVELOPE>"
    )
    r   = requests.post(
        TALLY_URL, data=xml_body.encode("utf-8"),
        headers={"Content-Type": "text/xml"}, timeout=30,
    )
    xml = r.content.decode("utf-8", errors="replace")

    vouchers          = re.findall(r"<VOUCHER\b.*?</VOUCHER>", xml, re.DOTALL)
    collections_total = 0.0
    receipt_count     = 0
    items             = []

    for v in vouchers:
        vtype = re.search(r"<VOUCHERTYPENAME[^>]*>(.*?)</VOUCHERTYPENAME>", v)
        if not (vtype and "RECEIPT" in vtype.group(1).upper()):
            continue
        receipt_count += 1
        ref_m   = re.search(r"<VOUCHERNUMBER[^>]*>(.*?)</VOUCHERNUMBER>", v)
        amt_m   = re.search(r"<AMOUNT[^>]*>(.*?)</AMOUNT>", v)
        party_m = re.search(r"<PARTYLEDGERNAME[^>]*>(.*?)</PARTYLEDGERNAME>", v)
        amt = 0.0
        if amt_m:
            try:
                amt = abs(float(amt_m.group(1)))
            except ValueError:
                pass
        collections_total += amt
        items.append({
            "customer_name": html.unescape(party_m.group(1).strip()) if party_m else "",
            "invoice_ref":   ref_m.group(1).strip() if ref_m else "",
            "amount":        round(amt, 2),
        })

    log.info(
        "  Today's collections: %d receipt(s), Rs %s",
        receipt_count, f"{collections_total:,.2f}",
    )

    if dry_run:
        log.info("  DRY RUN — daily_collections upsert skipped")
        return

    supa = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SECRET_KEY"],
    )

    # UUID lookup — same pattern as sync_today_sales
    cust_id_map = {}
    offset = 0
    while True:
        batch = (
            supa.table("customers")
            .select("id, customer_name")
            .range(offset, offset + 999)
            .execute().data
        )
        for row in batch:
            cust_id_map[row["customer_name"].strip().lower()] = row["id"]
        if len(batch) < 1000:
            break
        offset += 1000

    matched = 0
    for item in items:
        cid = cust_id_map.get(item["customer_name"].strip().lower())
        item["customer_id"] = cid
        if cid:
            matched += 1
    log.info(
        "  UUID resolved: %d / %d items (%d unmatched)",
        matched, len(items), len(items) - matched,
    )

    supa.table("daily_collections").upsert(
        {
            "sale_date":     today.isoformat(),
            "total_amount":  round(collections_total, 2),
            "invoice_count": receipt_count,
            "synced_at":     datetime.utcnow().isoformat(),
            "items":         items,
        },
        on_conflict="sale_date",
    ).execute()
    log.info("  daily_collections upserted for %s", today.isoformat())


# ── Step 10: Full FY Sales Vouchers → sales_history ───────────────────────────

def sync_sales_history(dry_run: bool = False):
    """
    Pull Sales Vouchers for the full current FY from Tally Day Book (by monthly
    chunk to avoid timeouts) and upsert into sales_history on voucher_number.
    SO- vouchers (Sales Orders) are filtered out — same rule as daily_sales.
    Caller should catch exceptions (non-fatal step).
    """
    fy_start = _fy_start()
    today    = date.today()
    log.info("Step 10 — Fetching Sales Vouchers FY %s → %s (monthly chunks)", fy_start, today)

    def _parse_qty(raw: str):
        n = re.sub(r"[^0-9.]", "", raw.strip().split(" ")[0])
        try:    return round(float(n), 3)
        except: return None  # noqa: E722

    def _parse_rate(raw: str):
        n = re.sub(r"[^0-9.]", "", raw.strip().split("/")[0])
        try:    return round(float(n), 2)
        except: return None  # noqa: E722

    all_records = []
    current     = fy_start

    while current <= today:
        next_month = (
            date(current.year + 1, 1, 1) if current.month == 12
            else date(current.year, current.month + 1, 1)
        )
        chunk_end = min(next_month - timedelta(days=1), today)
        from_str  = current.strftime("%Y%m%d")
        to_str    = chunk_end.strftime("%Y%m%d")
        log.info("  Chunk %s – %s", from_str, to_str)

        xml_body = (
            "<ENVELOPE>"
            "<HEADER>"
            "<VERSION>1</VERSION>"
            "<TALLYREQUEST>Export</TALLYREQUEST>"
            "<TYPE>Collection</TYPE>"
            "<ID>SalesHistory</ID>"
            "</HEADER>"
            "<BODY><DESC>"
            "<STATICVARIABLES>"
            f"<SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>"
            "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
            f"<SVFROMDATE>{from_str}</SVFROMDATE>"
            f"<SVTODATE>{to_str}</SVTODATE>"
            "</STATICVARIABLES>"
            "<TDL><TDLMESSAGE>"
            '<COLLECTION NAME="SalesHistory" ISMODIFY="No">'
            "<TYPE>Voucher</TYPE>"
            "<NATIVEMETHOD>Date</NATIVEMETHOD>"
            "<FETCH>DATE, VOUCHERNUMBER, PARTYLEDGERNAME, AMOUNT, VOUCHERTYPENAME,"
            " STOCKITEMNAME, ACTUALQTY, RATE</FETCH>"
            "</COLLECTION>"
            "</TDLMESSAGE></TDL>"
            "</DESC></BODY>"
            "</ENVELOPE>"
        )
        try:
            r   = requests.post(
                TALLY_URL, data=xml_body.encode("utf-8"),
                headers={"Content-Type": "text/xml"}, timeout=60,
            )
            xml = r.content.decode("utf-8", errors="replace")
        except requests.exceptions.Timeout:
            log.warning("  Chunk %s–%s timed out — skipping", from_str, to_str)
            current = next_month
            continue

        vouchers    = re.findall(r"<VOUCHER\b.*?</VOUCHER>", xml, re.DOTALL)
        raw_count   = len(vouchers)
        after_sales = 0
        after_so    = 0
        chunk_count = 0
        synced_at   = datetime.utcnow().isoformat()

        for v in vouchers:
            vtype_m = re.search(r"<VOUCHERTYPENAME[^>]*>(.*?)</VOUCHERTYPENAME>", v)
            if not (vtype_m and "SALES" in vtype_m.group(1).upper()):
                continue
            after_sales += 1
            ref_m = re.search(r"<VOUCHERNUMBER[^>]*>(.*?)</VOUCHERNUMBER>", v)
            ref   = ref_m.group(1).strip() if ref_m else None
            if not ref or ref.startswith("SO-"):
                continue
            after_so += 1

            date_m  = re.search(r"<DATE[^>]*>(.*?)</DATE>", v)
            party_m = re.search(r"<PARTYLEDGERNAME[^>]*>(.*?)</PARTYLEDGERNAME>", v)
            amt_m   = re.search(r"<AMOUNT[^>]*>(.*?)</AMOUNT>", v)
            stock_m = re.search(r"<STOCKITEMNAME[^>]*>(.*?)</STOCKITEMNAME>", v)
            qty_m   = re.search(r"<ACTUALQTY[^>]*>(.*?)</ACTUALQTY>", v)
            rate_m  = re.search(r"<RATE[^>]*>(.*?)</RATE>", v)

            sale_date = None
            if date_m:
                raw = date_m.group(1).strip()
                for fmt in ("%Y%m%d", "%d-%b-%y", "%d-%b-%Y"):
                    try:
                        sale_date = datetime.strptime(raw, fmt).date().isoformat()
                        break
                    except ValueError:
                        continue

            amount = None
            if amt_m:
                try:    amount = round(abs(float(amt_m.group(1))), 2)
                except: pass  # noqa: E722
            if amount is None:
                log.warning("  Skipping %s — no parseable amount tag", ref)
                continue

            all_records.append({
                "voucher_number": ref,
                "sale_date":      sale_date,
                "customer_name":  html.unescape(party_m.group(1).strip()) if party_m else None,
                "amount":         amount,
                "stock_item":     html.unescape(stock_m.group(1).strip()) if stock_m else None,
                "quantity":       _parse_qty(qty_m.group(1))  if qty_m  else None,
                "rate":           _parse_rate(rate_m.group(1)) if rate_m else None,
                "voucher_type":   vtype_m.group(1).strip(),
                "synced_at":      synced_at,
            })
            chunk_count += 1

        log.info(
            "  Chunk: %d raw VOUCHER tags | %d after SALES filter | %d after SO- filter | %d added",
            raw_count, after_sales, after_so, chunk_count,
        )
        current = next_month

    log.info("  Total records collected (before dedup): %d", len(all_records))
    if all_records:
        sample = [r["voucher_number"] for r in all_records[:8]]
        log.info("  Sample voucher numbers (first 8): %s", sample)

    # Deduplicate by voucher_number — last occurrence wins (handles chunk-boundary overlaps)
    seen = {}
    for rec in all_records:
        seen[rec["voucher_number"]] = rec
    all_records = list(seen.values())
    log.info("  After dedup: %d unique voucher numbers", len(all_records))
    if all_records:
        sample_dedup = [r["voucher_number"] for r in all_records[:8]]
        log.info("  Sample after dedup (first 8): %s", sample_dedup)

    if not all_records:
        log.warning("  No sales records found — skipping upsert")
        return

    if dry_run:
        log.info("  DRY RUN — sales_history upsert skipped")
        return

    supa = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SECRET_KEY"],
    )
    upserted = 0
    for i in range(0, len(all_records), SUPABASE_BATCH):
        batch = all_records[i : i + SUPABASE_BATCH]
        supa.table("sales_history").upsert(batch, on_conflict="voucher_number").execute()
        upserted += len(batch)
        log.info("  Upserted %d / %d", upserted, len(all_records))
    log.info("  sales_history sync complete")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    dry_run    = "--dry-run"    in sys.argv
    from_local = "--from-local" in sys.argv

    log.info("=" * 60)
    log.info("SUPREME BALAJI — TALLY OUTSTANDING SYNC")
    if from_local: log.info("  MODE: FROM LOCAL FILE (no Tally connection)")
    if dry_run:    log.info("  MODE: DRY RUN (Supabase writes skipped)")
    log.info("  Run: %s", RUN_TS)
    log.info("=" * 60)

    try:
        if from_local:
            local_xml = BASE_DIR / "tally_with_dates.xml"
            if not local_xml.exists():
                raise FileNotFoundError(
                    "--from-local specified but tally_with_dates.xml not found in backend/"
                )
            log.info("Steps 1-2 — SKIPPED (--from-local mode)")
            log.info("  Reading: %s", local_xml)
            xml_text = local_xml.read_text(encoding="utf-8", errors="replace")
        else:
            if not check_tally():
                raise RuntimeError(
                    "Tally not reachable. Run from the office network with Tally open."
                )
            xml_text = fetch_tally_xml()

        bills = parse_xml(xml_text)

        # Steps 4.5 + 4.6 — fetch ledger master once, use for both new-customer
        # insert and contact-field refresh (avoids a second round-trip to Tally).
        auto_inserted = []
        if not from_local:
            try:
                log.info("Step 4.5/4.6 — Fetching Tally ledger master")
                ledger_data = _fetch_tally_ledger_master()
                log.info("  Ledger master: %d records fetched", len(ledger_data))
                auto_inserted = auto_insert_new_customers(bills, ledger_data=ledger_data, dry_run=dry_run)
                refresh_ledger_contacts(ledger_data, dry_run=dry_run)
            except Exception as exc:
                log.warning(
                    "Step 4.5/4.6 WARNING — Ledger sync failed (non-fatal): %s", exc
                )

        inserted, skipped, unmatched = reload_supabase(bills, dry_run=dry_run)

        summary = {
            "bills_from_tally":    len(bills),
            "loaded_to_supabase":  inserted,
            "skipped_no_match":    skipped,
            "new_customers_added": len(auto_inserted),
            "new_customer_names":  auto_inserted,
            "skipped_names":       [
                {"name": n, "bills": c} for n, c in sorted(unmatched.items(), key=lambda x: -x[1])
            ],
        }
        _write_status("success", summary)

        if unmatched:
            log.warning("  Sending skip alert for %d unmatched customer(s)", len(unmatched))
            _send_skip_alert_email(unmatched)

        if not from_local:
            try:
                sync_today_sales(dry_run=dry_run)
            except Exception as exc:
                log.warning("Step 9 WARNING — Today's sales sync failed (non-fatal): %s", exc)

        if not from_local:
            try:
                sync_today_collections(dry_run=dry_run)
            except Exception as exc:
                log.warning("Step 9b WARNING — Today's collections sync failed (non-fatal): %s", exc)

        if not from_local:
            try:
                sync_sales_history(dry_run=dry_run)
            except Exception as exc:
                log.warning("Step 10 WARNING — Sales history sync failed (non-fatal): %s", exc)

        log.info("=" * 60)
        log.info("SYNC COMPLETE%s", " (DRY RUN)" if dry_run else "")
        log.info("  Bills from Tally       : %d", len(bills))
        log.info("  New customers added    : %d%s",
                 len(auto_inserted),
                 (" — " + ", ".join(auto_inserted)) if auto_inserted else "")
        log.info("  Loaded to Supabase     : %d", inserted)
        log.info("  Skipped (no match)     : %d", skipped)
        log.info("  Log: %s", log_path)
        log.info("=" * 60)

    except Exception as exc:
        error_msg = str(exc)
        log.exception("SYNC FAILED: %s", error_msg)
        log.error("Log saved to: %s", log_path)
        _write_status("failed", {"error": error_msg, "log": str(log_path)})
        _send_failure_email(error_msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
