import os, re, json
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SECRET_KEY")
supabase = create_client(url, key)

result = supabase.table("customers").select("customer_name, phone").is_("phone", "null").execute()
blanks = [c["customer_name"] for c in result.data]

with open("ledger_master.xml", "r", encoding="utf-8", errors="replace") as f:
    content = f.read()

def unescape(s):
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()

ledger_blocks = re.findall(r'<LEDGER NAME="(.*?)" RESERVEDNAME="[^"]*">(.*?)</LEDGER>', content, re.DOTALL)
block_lookup = {unescape(name): block for name, block in ledger_blocks}

print("--- Properly-bounded address lines for first 5 still-blank customers ---\n")
for name in blanks[:5]:
    block = block_lookup.get(name)
    if block is None:
        print(f"{name}: NOT FOUND as an exact ledger name\n")
        continue
    addr_lines = re.findall(r'<ADDRESS TYPE="String">(.*?)</ADDRESS>', block)
    ledgerphone = re.search(r'<LEDGERPHONE[^>]*>(.*?)</LEDGERPHONE>', block)
    print(f"{name}:")
    for a in addr_lines:
        print(f"  {unescape(a)}")
    if ledgerphone:
        print(f"  LEDGERPHONE field: {unescape(ledgerphone.group(1))}")
    print()
