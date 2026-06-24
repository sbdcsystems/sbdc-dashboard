import os, re, json
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SECRET_KEY")
supabase = create_client(url, key)

def extract_clean_phone(raw_text):
    if not raw_text:
        return None
    m = re.search(r'(?<!\d)[6-9]\d{9}(?!\d)', raw_text)
    if m:
        return m.group(0)
    m = re.search(r'(?<!\d)0\d{9,10}(?!\d)', raw_text)
    if m:
        return m.group(0)
    return None

def unescape(s):
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()

with open("ledger_master.xml", "r", encoding="utf-8", errors="replace") as f:
    content = f.read()

ledger_blocks = re.findall(r'<LEDGER NAME="(.*?)" RESERVEDNAME="[^"]*">(.*?)</LEDGER>', content, re.DOTALL)

clean_phones = {}
for name, block in ledger_blocks:
    name_clean = unescape(name)

    phone = None
    ledgerphone_match = re.search(r'<LEDGERPHONE[^>]*>(.*?)</LEDGERPHONE>', block)
    if ledgerphone_match:
        phone = extract_clean_phone(unescape(ledgerphone_match.group(1)))

    if not phone:
        addr_lines = [unescape(a) for a in re.findall(r'<ADDRESS TYPE="String">(.*?)</ADDRESS>', block)]
        for line in addr_lines:
            phone = extract_clean_phone(line)
            if phone:
                break

    clean_phones[name_clean] = phone

def normalize(name):
    name = name.lower()
    name = re.sub(r'[^a-z0-9\s]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name

phone_lookup = {normalize(k): v for k, v in clean_phones.items()}

customers_result = supabase.table("customers").select("id, customer_name, phone").execute()
customers = customers_result.data

fixed = 0
unchanged = 0
for c in customers:
    norm = normalize(c["customer_name"])
    new_phone = phone_lookup.get(norm)
    if new_phone != c["phone"]:
        supabase.table("customers").update({"phone": new_phone}).eq("id", c["id"]).execute()
        if c["phone"] and new_phone != c["phone"]:
            print(f"Fixed: {c['customer_name']!r}: {c['phone']!r} -> {new_phone!r}")
        fixed += 1
    else:
        unchanged += 1

print(f"\nUpdated: {fixed}")
print(f"Unchanged: {unchanged}")
