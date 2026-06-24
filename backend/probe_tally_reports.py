"""
probe_tally_reports.py — one-time diagnostic tool.

Tests every major Tally report type not yet used in this project and maps
exactly which XML fields are available in this SBDC Tally installation.

Run once from the office network:
    cd backend
    ..\venv\Scripts\activate.bat
    python probe_tally_reports.py

Output:
  - Console: status, record count, field names for each report
  - backend/probe_<report>.xml: raw XML backup for each successful report
"""

import os, re, time
from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv
import requests

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

TALLY_URL     = f"http://{os.environ.get('TALLY_SERVER_IP','192.168.0.205')}:{os.environ.get('TALLY_PORT','9000')}"
TALLY_COMPANY = os.environ.get("TALLY_COMPANY_NAME", "SUPREME BALAJI DYE CHEM - 25-26")
TIMEOUT       = 30
BASE_DIR      = Path(__file__).parent

today    = date.today()
from_30  = (today - timedelta(days=30)).strftime("%Y%m%d")
to_today = today.strftime("%Y%m%d")

print("=" * 70)
print(f"TALLY REPORT PROBE  —  {TALLY_URL}")
print(f"Company : {TALLY_COMPANY}")
print(f"Date range for voucher reports : {from_30} to {to_today}")
print("=" * 70)
print()

# ── Helpers ────────────────────────────────────────────────────────────────────

def extract_fields(xml: str) -> list[str]:
    """Return sorted list of unique XML tag names found in the response."""
    tags = re.findall(r"<([A-Z][A-Z0-9_.]*)\b[^/]*?>", xml)
    return sorted(set(tags))

def count_records(xml: str, tag: str) -> int:
    return len(re.findall(rf"<{tag}\b", xml))

def post(xml_body: str) -> tuple[str | None, float]:
    """POST to Tally; returns (xml_text, elapsed_seconds) or (None, elapsed) on failure."""
    t0 = time.time()
    try:
        r = requests.post(
            TALLY_URL,
            data=xml_body.encode("utf-8"),
            headers={"Content-Type": "text/xml"},
            timeout=TIMEOUT,
        )
        xml = r.content.decode("utf-8", errors="replace")
        return xml, time.time() - t0
    except requests.exceptions.Timeout:
        return None, time.time() - t0
    except Exception as exc:
        print(f"  CONNECTION ERROR: {exc}")
        return None, time.time() - t0

def save_xml(name: str, xml: str):
    path = BASE_DIR / f"probe_{name}.xml"
    path.write_text(xml, encoding="utf-8")
    print(f"  Saved: {path.name}")

def daybook_body(from_date: str, to_date: str, voucher_type_filter: str = "") -> str:
    filter_tag = (
        f"<VOUCHERTYPENAME>{voucher_type_filter}</VOUCHERTYPENAME>"
        if voucher_type_filter else ""
    )
    return (
        "<ENVELOPE>"
        "<HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>"
        "<BODY><EXPORTDATA><REQUESTDESC>"
        "<REPORTNAME>Day Book</REPORTNAME>"
        "<STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        f"<SVFROMDATE>{from_date}</SVFROMDATE>"
        f"<SVTODATE>{to_date}</SVTODATE>"
        f"{filter_tag}"
        "</STATICVARIABLES>"
        "</REQUESTDESC></EXPORTDATA></BODY>"
        "</ENVELOPE>"
    )

# ── Results accumulator ────────────────────────────────────────────────────────

results = []   # list of (name, status, count, key_fields)

# ── 1. Sales Vouchers ─────────────────────────────────────────────────────────

print("--- 1. Sales Vouchers (Day Book, last 30 days) ---")
TARGET_SALES = [
    "PARTYLEDGERNAME", "VOUCHERNUMBER", "AMOUNT", "DATE",
    "STOCKITEMNAME", "ACTUALQTY", "BILLEDQTY", "RATE",
    "NARRATION", "COSTCENTRENAME", "VOUCHERTYPENAME",
    "LEDGERNAME", "TAXAMOUNT", "BILLNAME",
]
xml, elapsed = post(daybook_body(from_30, to_today))
if xml is None:
    print(f"  TIMED OUT after {elapsed:.0f}s")
    results.append(("Sales Vouchers", "TIMEOUT", 0, []))
else:
    n = count_records(xml, "VOUCHER")
    fields = extract_fields(xml)
    found  = [f for f in TARGET_SALES if f in fields]
    print(f"  {len(xml):,} bytes  |  {n} VOUCHER records  |  {elapsed:.1f}s")
    print(f"  Target fields found   : {found}")
    print(f"  Target fields missing : {[f for f in TARGET_SALES if f not in fields]}")
    print(f"  All tags in response  : {fields}")
    save_xml("sales_vouchers", xml)
    results.append(("Sales Vouchers", "OK", n, found))
print()

# ── 2. Receipt Vouchers ───────────────────────────────────────────────────────

print("--- 2. Receipt Vouchers (Day Book filtered, last 30 days) ---")
TARGET_RECEIPTS = [
    "PARTYLEDGERNAME", "VOUCHERNUMBER", "AMOUNT", "DATE",
    "BILLNAME", "BILLTYPE", "NARRATION", "LEDGERNAME",
]
xml, elapsed = post(daybook_body(from_30, to_today, "Receipt"))
if xml is None:
    print(f"  TIMED OUT after {elapsed:.0f}s")
    results.append(("Receipt Vouchers", "TIMEOUT", 0, []))
else:
    n = count_records(xml, "VOUCHER")
    fields = extract_fields(xml)
    found  = [f for f in TARGET_RECEIPTS if f in fields]
    print(f"  {len(xml):,} bytes  |  {n} VOUCHER records  |  {elapsed:.1f}s")
    print(f"  Target fields found   : {found}")
    print(f"  Target fields missing : {[f for f in TARGET_RECEIPTS if f not in fields]}")
    print(f"  All tags in response  : {fields}")
    save_xml("receipt_vouchers", xml)
    results.append(("Receipt Vouchers", "OK", n, found))
print()

# ── 3. Purchase Vouchers ──────────────────────────────────────────────────────

print("--- 3. Purchase Vouchers (Day Book filtered, last 30 days) ---")
TARGET_PURCHASES = [
    "PARTYLEDGERNAME", "VOUCHERNUMBER", "AMOUNT", "DATE",
    "STOCKITEMNAME", "ACTUALQTY", "RATE", "NARRATION",
    "LEDGERNAME", "TAXAMOUNT",
]
xml, elapsed = post(daybook_body(from_30, to_today, "Purchase"))
if xml is None:
    print(f"  TIMED OUT after {elapsed:.0f}s")
    results.append(("Purchase Vouchers", "TIMEOUT", 0, []))
else:
    n = count_records(xml, "VOUCHER")
    fields = extract_fields(xml)
    found  = [f for f in TARGET_PURCHASES if f in fields]
    print(f"  {len(xml):,} bytes  |  {n} VOUCHER records  |  {elapsed:.1f}s")
    print(f"  Target fields found   : {found}")
    print(f"  Target fields missing : {[f for f in TARGET_PURCHASES if f not in fields]}")
    print(f"  All tags in response  : {fields}")
    save_xml("purchase_vouchers", xml)
    results.append(("Purchase Vouchers", "OK", n, found))
print()

# ── 4. Stock Summary ──────────────────────────────────────────────────────────

print("--- 4. Stock Summary ---")
TARGET_STOCK = [
    "STOCKITEMNAME", "CLOSINGQTY", "CLOSINGRATE", "CLOSINGVALUE",
    "OPENINGQTY", "OPENINGRATE", "OPENINGVALUE",
    "INWARDSQTY", "OUTWARDSQTY", "STDCOSTRATE",
]
body_stock = (
    "<ENVELOPE>"
    "<HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>"
    "<BODY><EXPORTDATA><REQUESTDESC>"
    "<REPORTNAME>Stock Summary</REPORTNAME>"
    "<STATICVARIABLES>"
    f"<SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>"
    "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
    f"<SVTODATE>{to_today}</SVTODATE>"
    "</STATICVARIABLES>"
    "</REQUESTDESC></EXPORTDATA></BODY>"
    "</ENVELOPE>"
)
xml, elapsed = post(body_stock)
if xml is None:
    print(f"  TIMED OUT after {elapsed:.0f}s")
    results.append(("Stock Summary", "TIMEOUT", 0, []))
else:
    n = count_records(xml, "STOCKITEM")
    fields = extract_fields(xml)
    found  = [f for f in TARGET_STOCK if f in fields]
    print(f"  {len(xml):,} bytes  |  {n} STOCKITEM records  |  {elapsed:.1f}s")
    print(f"  Target fields found   : {found}")
    print(f"  Target fields missing : {[f for f in TARGET_STOCK if f not in fields]}")
    print(f"  All tags in response  : {fields}")
    save_xml("stock_summary", xml)
    results.append(("Stock Summary", "OK", n, found))
print()

# ── 5. Cost Centre Summary ────────────────────────────────────────────────────

print("--- 5. Cost Centre Summary ---")
TARGET_CC = [
    "COSTCENTRENAME", "AMOUNT", "DEBIT", "CREDIT",
    "OPENINGBALANCE", "CLOSINGBALANCE", "VOUCHERTYPENAME",
]
body_cc = (
    "<ENVELOPE>"
    "<HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>"
    "<BODY><EXPORTDATA><REQUESTDESC>"
    "<REPORTNAME>Cost Centre Summary</REPORTNAME>"
    "<STATICVARIABLES>"
    f"<SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>"
    "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
    f"<SVFROMDATE>{from_30}</SVFROMDATE>"
    f"<SVTODATE>{to_today}</SVTODATE>"
    "</STATICVARIABLES>"
    "</REQUESTDESC></EXPORTDATA></BODY>"
    "</ENVELOPE>"
)
xml, elapsed = post(body_cc)
if xml is None:
    print(f"  TIMED OUT after {elapsed:.0f}s")
    results.append(("Cost Centre Summary", "TIMEOUT", 0, []))
else:
    n = count_records(xml, "COSTCENTRE")
    fields = extract_fields(xml)
    found  = [f for f in TARGET_CC if f in fields]
    print(f"  {len(xml):,} bytes  |  {n} COSTCENTRE records  |  {elapsed:.1f}s")
    print(f"  Target fields found   : {found}")
    print(f"  Target fields missing : {[f for f in TARGET_CC if f not in fields]}")
    print(f"  All tags in response  : {fields}")
    save_xml("cost_centre_summary", xml)
    results.append(("Cost Centre Summary", "OK", n, found))
print()

# ── Summary table ──────────────────────────────────────────────────────────────

print("=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"{'Report':<25}  {'Status':<8}  {'Records':>8}  Key fields available")
print("-" * 70)
for name, status, count, fields in results:
    field_str = ", ".join(fields) if fields else "—"
    print(f"{name:<25}  {status:<8}  {count:>8}  {field_str}")
print("=" * 70)
