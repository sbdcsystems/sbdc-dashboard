import os, re, json
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SECRET_KEY")
supabase = create_client(url, key)

result = supabase.table("customers").select("customer_name, phone").is_("phone", "null").execute()
blanks = [c["customer_name"] for c in result.data]

print(f"Total still blank: {len(blanks)}\n")
for name in blanks[:8]:
    print(name)

with open("ledger_master.xml", "r", encoding="utf-8", errors="replace") as f:
    content = f.read()

def unescape(s):
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()

print("\n--- Raw address lines for first 5 ---\n")
for name in blanks[:5]:
    idx = content.find(f'NAME="{name}"')
    if idx == -1:
        print(f"{name}: NOT FOUND in XML by exact name match\n")
        continue
    block = content[idx:idx+2000]
    addr_lines = re.findall(r'<ADDRESS TYPE="String">(.*?)</ADDRESS>', block)
    print(f"{name}:")
    for a in addr_lines:
        print(f"  {unescape(a)}")
    print()
