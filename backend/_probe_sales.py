"""
Probe Tally's sales voucher structure to answer two questions:
  1. Does SBDC track individual stock items / quantities per sale?
  2. Can we pull today's sales total + invoice count reliably?

Fetches the last 7 days of sales vouchers and inspects the XML.
"""
import os, re
from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv
import requests

load_dotenv(Path(__file__).parent.parent / '.env', override=True)

TALLY_URL     = f"http://{os.environ.get('TALLY_SERVER_IP','192.168.0.205')}:{os.environ.get('TALLY_PORT','9000')}"
TALLY_COMPANY = os.environ.get('TALLY_COMPANY_NAME', 'SUPREME BALAJI DYE CHEM - 25-26')

today   = date.today()
week_ago = today - timedelta(days=7)

FROM = week_ago.strftime('%Y%m%d')
TO   = today.strftime('%Y%m%d')
TODAY_STR = today.strftime('%Y%m%d')

print(f"Tally : {TALLY_URL}")
print(f"Range : {FROM} to {TO}")
print()

# ── Approach A: Day Book (simplest built-in report) ────────────────────────
print("--- Approach A: Day Book ---")
body_daybook = f"""<ENVELOPE>
 <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
 <BODY><EXPORTDATA><REQUESTDESC>
  <REPORTNAME>Day Book</REPORTNAME>
  <STATICVARIABLES>
   <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   <SVFROMDATE>{FROM}</SVFROMDATE>
   <SVTODATE>{TO}</SVTODATE>
  </STATICVARIABLES>
 </REQUESTDESC></EXPORTDATA></BODY>
</ENVELOPE>"""

try:
    r = requests.post(TALLY_URL, data=body_daybook.encode('utf-8'),
                      headers={'Content-Type': 'text/xml'}, timeout=60)
    xml = r.content.decode('utf-8', errors='replace')
    print(f"  {len(xml):,} bytes")

    # Show all unique top-level tags inside TALLYMESSAGE
    tags = set(re.findall(r'<([A-Z][A-Z0-9_]+)[\s>]', xml))
    voucher_tags = sorted(t for t in tags if 'VOUCH' in t or 'STOCK' in t or 'INVEN' in t or 'ITEM' in t)
    print(f"  Voucher/stock tags present: {voucher_tags}")

    # Count vouchers by type
    vtypes = re.findall(r'<VOUCHERTYPENAME[^>]*>(.*?)</VOUCHERTYPENAME>', xml)
    from collections import Counter
    print(f"  Voucher type counts: {dict(Counter(vtypes).most_common(10))}")

    # Check if any stock item entries exist
    stock_items = re.findall(r'<STOCKITEMNAME[^>]*>(.*?)</STOCKITEMNAME>', xml)
    print(f"  Stock item entries: {len(stock_items)} {'(inventory tracked!)' if stock_items else '(none — invoice-total only)'}")
    if stock_items:
        unique_items = sorted(set(stock_items))[:10]
        print(f"  Sample items: {unique_items}")

    # Show the first sales voucher block in full
    sales_match = re.search(
        r'<VOUCHER\b[^>]*>(?:(?!</VOUCHER>).)*?(?:GST SALES|CC SALES)(?:(?!</VOUCHER>).)*?</VOUCHER>',
        xml, re.DOTALL
    )
    if not sales_match:
        # fallback: just grab first VOUCHER block
        sales_match = re.search(r'<VOUCHER\b.*?</VOUCHER>', xml, re.DOTALL)
    if sales_match:
        block = sales_match.group(0)
        print(f"\n  First sales voucher block ({len(block)} chars):")
        # Show key fields only, not the full XML
        for tag in ['VOUCHERTYPENAME','DATE','VOUCHERNUMBER','PARTYLEDGERNAME',
                    'AMOUNT','NARRATION','STOCKITEMNAME','ACTUALQTY','RATE',
                    'BILLEDQTY','BATCHNAME']:
            vals = re.findall(rf'<{tag}[^>]*>(.*?)</{tag}>', block)
            if vals:
                print(f"    {tag}: {vals[:3]}")

except requests.exceptions.Timeout:
    print("  TIMED OUT")
except Exception as e:
    print(f"  ERROR: {e}")

print()

# ── Approach B: Voucher Collection (for clean today-only totals) ───────────
print("--- Approach B: Voucher Collection (today only) ---")
body_coll = f"""<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE><ID>TodaySales</ID>
 </HEADER>
 <BODY><DESC>
  <STATICVARIABLES>
   <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   <SVFROMDATE>{TODAY_STR}</SVFROMDATE>
   <SVTODATE>{TODAY_STR}</SVTODATE>
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <COLLECTION NAME="TodaySales" ISMODIFY="No">
    <TYPE>Voucher</TYPE>
    <BELONGSTO>Sales Accounts</BELONGSTO>
    <FILTER>IsSales</FILTER>
    <FETCH>DATE, VOUCHERNUMBER, PARTYLEDGERNAME, AMOUNT, VOUCHERTYPENAME</FETCH>
   </COLLECTION>
   <SYSTEM TYPE="Formulae">
    <IsSales>$$IsSales:$VoucherTypeName</IsSales>
   </SYSTEM>
  </TDLMESSAGE></TDL>
 </DESC></BODY>
</ENVELOPE>"""

try:
    r = requests.post(TALLY_URL, data=body_coll.encode('utf-8'),
                      headers={'Content-Type': 'text/xml'}, timeout=30)
    xml_b = r.content.decode('utf-8', errors='replace')
    print(f"  {len(xml_b):,} bytes")
    entries = re.findall(r'<VOUCHER\b.*?</VOUCHER>', xml_b, re.DOTALL)
    print(f"  Voucher entries returned: {len(entries)}")
    if entries:
        print(f"  First entry snippet: {entries[0][:300]}")
except requests.exceptions.Timeout:
    print("  TIMED OUT")
except Exception as e:
    print(f"  ERROR: {e}")

print()

# ── Approach C: Day Book scoped to today only ──────────────────────────────
print("--- Approach C: Day Book today-only ---")
body_today = f"""<ENVELOPE>
 <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
 <BODY><EXPORTDATA><REQUESTDESC>
  <REPORTNAME>Day Book</REPORTNAME>
  <STATICVARIABLES>
   <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   <SVFROMDATE>{TODAY_STR}</SVFROMDATE>
   <SVTODATE>{TODAY_STR}</SVTODATE>
  </STATICVARIABLES>
 </REQUESTDESC></EXPORTDATA></BODY>
</ENVELOPE>"""

try:
    r = requests.post(TALLY_URL, data=body_today.encode('utf-8'),
                      headers={'Content-Type': 'text/xml'}, timeout=30)
    xml_c = r.content.decode('utf-8', errors='replace')
    print(f"  {len(xml_c):,} bytes")

    # Pull every voucher with its type and amount
    vouchers = re.findall(
        r'<VOUCHER\b.*?</VOUCHER>', xml_c, re.DOTALL
    )
    print(f"  Total vouchers today: {len(vouchers)}")

    sales_total = 0.0
    sales_count = 0
    for v in vouchers:
        vtype = re.search(r'<VOUCHERTYPENAME[^>]*>(.*?)</VOUCHERTYPENAME>', v)
        amt   = re.search(r'<AMOUNT[^>]*>(.*?)</AMOUNT>', v)
        if vtype and 'SALES' in vtype.group(1).upper():
            sales_count += 1
            if amt:
                try:
                    sales_total += abs(float(amt.group(1)))
                except ValueError:
                    pass

    print(f"  Sales vouchers today : {sales_count}")
    print(f"  Total sales today    : Rs {sales_total:,.2f}")

    # Show all voucher types present today
    from collections import Counter
    vtypes_today = [
        re.search(r'<VOUCHERTYPENAME[^>]*>(.*?)</VOUCHERTYPENAME>', v).group(1)
        for v in vouchers
        if re.search(r'<VOUCHERTYPENAME[^>]*>(.*?)</VOUCHERTYPENAME>', v)
    ]
    print(f"  Voucher types today: {dict(Counter(vtypes_today))}")

except requests.exceptions.Timeout:
    print("  TIMED OUT")
except Exception as e:
    print(f"  ERROR: {e}")
