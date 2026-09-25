"""
probe_alterid.py - one-time diagnostic: does this Tally install support
filtering vouchers by AlterID via a server-side <FILTER>?

Run on the office PC, with Tally open and the correct company active:
    python probe_alterid.py

What it does (read-only, writes nothing to Tally or Supabase):
  1. Fetches sales vouchers (GST SALES / CC SALES, not cancelled) for the
     CURRENT FY ONLY, FETCHing just VoucherNumber + AlterID, to find the
     highest AlterID among them. This is a real Tally request but a light
     one — only two fields per voucher, one FY. NOTE: this is the max among
     THIS FY's vouchers, not a true all-time global max — an older-FY
     voucher edited very recently would carry a higher AlterID than
     anything shown here, and this probe wouldn't see it. That's fine for
     what this probe is checking (does the filter syntax work at all); it
     matters more once real reliance on AlterID starts, which is exactly
     why this script exists before that happens.
  2. Re-fetches with a server-side filter for AlterID > (max - 50), the
     same <FILTER>/<SYSTEM TYPE="Formulae"> pattern already confirmed
     working for date filters on this Tally install (see CLAUDE.md).
     Prints how many vouchers came back, how long each request took, and
     their voucher numbers/dates/AlterIDs.

Self-contained: does not import tally_sync_runner.py, so running this can't
trigger that module's import-time side effects (log file creation under
backend/logs/, etc.) or reach Supabase. It loads the same .env the way that
module does, but nothing else is shared.
"""

import html
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR.parent / ".env", override=True)

TALLY_IP      = os.environ.get("TALLY_SERVER_IP", "192.168.0.205")
TALLY_PORT    = int(os.environ.get("TALLY_PORT", "9000"))
TALLY_URL     = f"http://{TALLY_IP}:{TALLY_PORT}"
TALLY_COMPANY = os.environ.get("TALLY_COMPANY_NAME", "SUPREME BALAJI DYE CHEM - 25-26")

SALES_VOUCHER_TYPES = ("GST SALES", "CC SALES")
TIMEOUT = 90


def _fy_start(today: date) -> date:
    return date(today.year, 4, 1) if today.month >= 4 else date(today.year - 1, 4, 1)


def _tally_date_literal(d: date) -> str:
    return d.strftime("%d-%b-%Y")


def _xml_escape_formula(formula: str) -> str:
    return formula.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_request(collection_id: str, fetch_fields: str, filter_formula: str) -> str:
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


def _post(xml_body: str) -> bytes:
    r = requests.post(
        TALLY_URL, data=xml_body.encode("utf-8"),
        headers={"Content-Type": "text/xml"}, timeout=TIMEOUT,
    )
    return r.content


def _extract_alterid(voucher_xml: str) -> "int | None":
    m = re.search(r"<ALTERID[^>]*>(.*?)</ALTERID>", voucher_xml)
    if not m:
        return None
    try:
        return int(float(m.group(1).strip()))
    except ValueError:
        return None


def main():
    print(f"Connecting to Tally at {TALLY_URL}, company: {TALLY_COMPANY}")
    try:
        requests.get(TALLY_URL, timeout=5)
    except requests.exceptions.RequestException as exc:
        print(f"ERROR: Tally not reachable ({exc}). Is it open, on the office network, with the right company active?", file=sys.stderr)
        sys.exit(1)

    today    = date.today()
    fy_start = _fy_start(today)
    type_formula = " OR ".join(f'$VoucherTypeName = "{t}"' for t in SALES_VOUCHER_TYPES)

    # ── Step 1: find the max AlterID among this FY's sales vouchers ────────────
    print(f"\nStep 1 — scanning {fy_start} to {today} for the current max AlterID (GST SALES / CC SALES, not cancelled)...")
    formula1 = (
        f'$Date >= $$Date:"{_tally_date_literal(fy_start)}" AND '
        f'$Date <= $$Date:"{_tally_date_literal(today)}" AND '
        f'NOT $IsCancelled AND ({type_formula})'
    )
    xml_body1 = _build_request("ProbeMaxAlterID", "VoucherNumber, AlterID", formula1)

    t0 = time.monotonic()
    try:
        raw1 = _post(xml_body1)
    except requests.exceptions.RequestException as exc:
        print(f"ERROR: Step 1 request failed: {exc}", file=sys.stderr)
        sys.exit(1)
    elapsed1 = time.monotonic() - t0
    xml1 = raw1.decode("utf-8", errors="replace")

    err_m = re.search(r"<LINEERROR>(.*?)</LINEERROR>", xml1, re.DOTALL)
    if err_m:
        print(f"ERROR: Tally reported a TDL error: {html.unescape(err_m.group(1).strip())}", file=sys.stderr)
        print("This likely means the FILTER/SYSTEM Formulae syntax itself has a problem — not AlterID specifically.", file=sys.stderr)
        sys.exit(1)

    vouchers1 = re.findall(r"<VOUCHER\b.*?</VOUCHER>", xml1, re.DOTALL)
    alterids  = [_extract_alterid(v) for v in vouchers1]
    alterids  = [a for a in alterids if a is not None]

    print(f"  {len(vouchers1)} voucher(s) returned in {elapsed1:.1f}s")
    if not alterids:
        print(
            "\nRESULT: No <ALTERID> tag found on any voucher in the response.\n"
            "This Tally install/version may not expose AlterID via this Collection\n"
            "mechanism, or the field name differs. Do NOT enable ALTERID_SYNC_ENABLED\n"
            "based on this — fall back to the month-based approach.",
            file=sys.stderr,
        )
        sys.exit(1)

    max_alterid = max(alterids)
    print(f"  Max AlterID found (this FY's sales vouchers): {max_alterid}")

    # ── Step 2: fetch vouchers with AlterID > (max - 50) ────────────────────────
    threshold = max(0, max_alterid - 50)
    print(f"\nStep 2 — fetching vouchers with AlterID > {threshold} (no date bound, same as the real usage would be)...")
    formula2 = f'$AlterID > {threshold} AND NOT $IsCancelled AND ({type_formula})'
    xml_body2 = _build_request("ProbeAlterIDDelta", "Date, VoucherNumber, VoucherTypeName, Amount, AlterID", formula2)

    t1 = time.monotonic()
    try:
        raw2 = _post(xml_body2)
    except requests.exceptions.RequestException as exc:
        print(f"ERROR: Step 2 request failed: {exc}", file=sys.stderr)
        sys.exit(1)
    elapsed2 = time.monotonic() - t1
    xml2 = raw2.decode("utf-8", errors="replace")

    err_m2 = re.search(r"<LINEERROR>(.*?)</LINEERROR>", xml2, re.DOTALL)
    if err_m2:
        print(f"ERROR: Tally reported a TDL error: {html.unescape(err_m2.group(1).strip())}", file=sys.stderr)
        sys.exit(1)

    vouchers2 = re.findall(r"<VOUCHER\b.*?</VOUCHER>", xml2, re.DOTALL)
    rows = []
    for v in vouchers2:
        ref_m   = re.search(r"<VOUCHERNUMBER[^>]*>(.*?)</VOUCHERNUMBER>", v)
        date_m  = re.search(r"<DATE[^>]*>(.*?)</DATE>", v)
        vtype_m = re.search(r"<VOUCHERTYPENAME[^>]*>(.*?)</VOUCHERTYPENAME>", v)
        amt_m   = re.search(r"<AMOUNT[^>]*>(.*?)</AMOUNT>", v)
        alterid = _extract_alterid(v)
        rows.append({
            "voucher_number": html.unescape(ref_m.group(1).strip()) if ref_m else "?",
            "date":           date_m.group(1).strip() if date_m else "?",
            "voucher_type":   vtype_m.group(1).strip() if vtype_m else "?",
            "amount":         amt_m.group(1).strip() if amt_m else "?",
            "alterid":        alterid,
        })

    print(f"\nRESULT: {len(rows)} voucher(s) returned in {elapsed2:.1f}s for AlterID > {threshold}")
    print(f"{'Voucher Number':<25} {'Date':<12} {'Type':<12} {'Amount':>14} {'AlterID':>10}")
    for r in sorted(rows, key=lambda r: (r["alterid"] or 0)):
        print(f"{r['voucher_number']:<25} {r['date']:<12} {r['voucher_type']:<12} {r['amount']:>14} {str(r['alterid']):>10}")

    print(
        f"\nSanity check: does the count/list above look right for the last ~50 "
        f"AlterID values (i.e. recently created or edited sales vouchers)? If yes, "
        f"AlterID filtering works here and ALTERID_SYNC_ENABLED can be flipped to "
        f"True in tally_sync_runner.py. If the count is 0, way too large, or the "
        f"voucher list looks wrong, do not enable it — report back what you saw."
    )


if __name__ == "__main__":
    main()
