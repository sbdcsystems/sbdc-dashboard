"""
probe_ledger_phones.py - read-only diagnostic: for the customers with no
phone in Supabase (no phone, not flagged, assigned to staff, has
outstanding), does Tally's LIVE ledger master hold a phone number anywhere
that the current sync isn't finding?

Run on the office PC, with Tally open and the correct company active:
    python probe_ledger_phones.py

What it does:
  1. Reads Supabase (read-only) for the exact same cohort investigated
     locally: no phone, not flagged, assigned to a staff member, has
     outstanding rows.
  2. Fetches Tally's ledger master live, with a BROADER field list than
     production's Step 4.5/4.6 currently uses (NAME, LEDGERPHONE, ADDRESS,
     PARTYGSTIN only) — adds LEDGERMOBILE, LEDGERFAX, EMAIL, NARRATION.
     These are standard, well-documented Tally ledger fields; this does NOT
     attempt Tally's separate multi-contact-person feature (that uses a
     different, version-dependent TDL structure not confirmed here — if
     this script's fields come up empty too, that feature is the next thing
     to check, not something to guess field names for blind).
  3. For each target customer, searches every fetched field with a WIDER
     phone-pattern match than production's _extract_phone (which cannot
     match a 10-digit mobile prefixed with the 91 country code, or a
     multi-extension format like "0427-2466651/55" — both confirmed live
     bugs against the 20-Jun-2026 local snapshot; this checks whether they
     also explain gaps against TODAY's real data).
  4. Prints: how many of the cohort have a phone-like number findable at
     all, broken down by which field it came from, and 10 anonymised
     examples (last 4 digits kept).

Self-contained: does not import tally_sync_runner.py (avoids that module's
import-time side effects). Read-only everywhere — no Supabase writes, no
Tally writes, writes no file except printing to stdout.
"""
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from supabase import create_client

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR.parent / ".env", override=True)

TALLY_IP      = os.environ.get("TALLY_SERVER_IP", "192.168.0.205")
TALLY_PORT    = int(os.environ.get("TALLY_PORT", "9000"))
TALLY_URL     = f"http://{TALLY_IP}:{TALLY_PORT}"
TALLY_COMPANY = os.environ.get("TALLY_COMPANY_NAME", "SUPREME BALAJI DYE CHEM - 25-26")


def broader_phone_candidates(text):
    """
    Wider net than tally_sync_runner._extract_phone: also catches a
    10-digit mobile prefixed with the 91 country code (12 digits total),
    which that function's `len(digits) in (10, 11)` check structurally
    cannot match.
    """
    found = []
    for m in re.finditer(r"(?:\+?\d[\d\-\.\s]{6,16}\d)", text):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
            found.append(("mobile_with_91_prefix", digits[2:]))
        elif len(digits) == 10 and digits[0] in "6789":
            found.append(("mobile_10digit", digits))
        elif len(digits) in (10, 11) and digits[0] == "0":
            found.append(("landline_with_0", digits))
    return found


def main():
    supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

    print("Fetching customer_list_view from Supabase...")
    all_v = []
    offset = 0
    while True:
        batch = (
            supa.table("customer_list_view")
            .select("id,customer_name,phone,flagged,assigned_to_name,present_pending,archived_pending")
            .range(offset, offset + 999).execute().data
        )
        all_v.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000

    no_phone = [c for c in all_v if not (c["phone"] or "").strip()]
    target = [
        c for c in no_phone
        if not c["flagged"] and c["assigned_to_name"]
        and (float(c["present_pending"] or 0) != 0 or float(c["archived_pending"] or 0) != 0)
    ]
    print(f"Total customers: {len(all_v)} | no phone: {len(no_phone)} | target cohort: {len(target)}\n")

    print(f"Connecting to Tally at {TALLY_URL}, company: {TALLY_COMPANY}")
    try:
        requests.get(TALLY_URL, timeout=5)
    except requests.exceptions.RequestException as exc:
        print(f"ERROR: Tally not reachable ({exc}).", file=sys.stderr)
        sys.exit(1)

    xml_body = (
        "<ENVELOPE>"
        "<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        "<TYPE>Collection</TYPE><ID>AllLedgersBroad</ID></HEADER>"
        "<BODY><DESC>"
        "<STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        "</STATICVARIABLES>"
        "<TDL><TDLMESSAGE>"
        '<COLLECTION NAME="AllLedgersBroad" ISMODIFY="No">'
        "<TYPE>Ledger</TYPE>"
        "<FETCH>NAME,PARENT,LEDGERPHONE,LEDGERMOBILE,LEDGERFAX,EMAIL,ADDRESS,NARRATION</FETCH>"
        "</COLLECTION>"
        "</TDLMESSAGE></TDL>"
        "</DESC></BODY>"
        "</ENVELOPE>"
    )
    print("Fetching ledger master with a broader field list (NAME,PARENT,LEDGERPHONE,"
          "LEDGERMOBILE,LEDGERFAX,EMAIL,ADDRESS,NARRATION)...")
    r = requests.post(TALLY_URL, data=xml_body.encode("utf-8"), headers={"Content-Type": "text/xml"}, timeout=90)
    xml = r.content.decode("utf-8", errors="replace")

    err_m = re.search(r"<LINEERROR>(.*?)</LINEERROR>", xml, re.DOTALL)
    if err_m:
        print(f"ERROR: Tally reported a TDL error: {err_m.group(1).strip()}", file=sys.stderr)
        print("If this is about an unknown field, one of LEDGERMOBILE/LEDGERFAX/NARRATION may not "
              "exist on this Tally version — remove it from the FETCH list above and re-run.", file=sys.stderr)
        sys.exit(1)

    ledger_blocks = {}
    for raw_name, block in re.findall(r'<LEDGER NAME="(.*?)"[^>]*>(.*?)</LEDGER>', xml, re.DOTALL):
        ledger_blocks[raw_name.strip().lower()] = block
    print(f"Fetched {len(ledger_blocks)} ledger(s) from live Tally.\n")

    # Report which tags actually came back non-empty at all, across every
    # ledger, so it's obvious if e.g. LEDGERMOBILE doesn't exist on this
    # install rather than just happening to be blank for this cohort.
    tag_presence = {}
    for field in ("LEDGERPHONE", "LEDGERMOBILE", "LEDGERFAX", "EMAIL", "NARRATION"):
        count = len(re.findall(rf"<{field}\b[^>]*>[^<]+</{field}>", xml))
        tag_presence[field] = count
    print("Non-empty tag counts across ALL ledgers (sanity check that fields exist on this install):")
    for field, count in tag_presence.items():
        print(f"  {field}: {count}")
    print()

    fields_to_check = [
        ("LEDGERPHONE",  r"<LEDGERPHONE\b[^>]*>(.*?)</LEDGERPHONE>"),
        ("LEDGERMOBILE", r"<LEDGERMOBILE\b[^>]*>(.*?)</LEDGERMOBILE>"),
        ("LEDGERFAX",    r"<LEDGERFAX\b[^>]*>(.*?)</LEDGERFAX>"),
        ("EMAIL",        r"<EMAIL\b[^>]*>(.*?)</EMAIL>"),
        ("ADDRESS",      r'<ADDRESS TYPE="String">(.*?)</ADDRESS>'),
        ("NARRATION",    r"<NARRATION\b[^>]*>(.*?)</NARRATION>"),
    ]

    results = []
    no_ledger_match = 0
    for c in target:
        block = ledger_blocks.get(c["customer_name"].strip().lower())
        if block is None:
            no_ledger_match += 1
            continue
        for field_name, pattern in fields_to_check:
            for m in re.finditer(pattern, block, re.DOTALL):
                for kind, number in broader_phone_candidates(m.group(1)):
                    results.append((c["customer_name"], field_name, kind, number))

    print("=" * 78)
    print(f"LIVE Tally cross-check for the {len(target)} target customers")
    print("=" * 78)
    print(f"No matching ledger found in live Tally at all: {no_ledger_match}")
    print(f"Customers with a phone-like number findable somewhere: {len(set(r[0] for r in results))}")

    by_field = {}
    for name, field, kind, number in results:
        by_field.setdefault(field, []).append((name, kind, number))
    for field, rows in by_field.items():
        kinds = {}
        for name, kind, number in rows:
            kinds[kind] = kinds.get(kind, 0) + 1
        print(f"  found in {field}: {len(rows)} number(s) — {kinds}")

    print("\n10 anonymised examples (last 4 digits kept):")
    for name, field, kind, number in results[:10]:
        masked = "X" * (len(number) - 4) + number[-4:]
        print(f"  {name[:40]:<40} | field={field:<12} | kind={kind:<22} | {masked}")
    if not results:
        print("  (none found)")


if __name__ == "__main__":
    main()
