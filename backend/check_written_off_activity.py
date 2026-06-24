import json, re, os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SECRET_KEY")
supabase = create_client(url, key)

with open("ledger_master_parsed.json", "r", encoding="utf-8") as f:
    ledgers = json.load(f)

def normalize(name):
    name = name.lower()
    name = re.sub(r'[^a-z0-9\s]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name

written_off_names = [name for name, data in ledgers.items() if data.get("parent") == "Bad Debts Written Off"]
print(f"Total names in 'Bad Debts Written Off': {len(written_off_names)}\n")

customers = supabase.table("customers").select("customer_name").execute().data
existing_norms = {normalize(c["customer_name"]) for c in customers}

already_active = []
truly_dormant = []
for name in written_off_names:
    if normalize(name) in existing_norms:
        already_active.append(name)
    else:
        truly_dormant.append(name)

print(f"Already exist as current customers (have real bills, current or old): {len(already_active)}")
for n in already_active[:10]:
    print(f"  {n}")

print(f"\nTruly dormant - zero bills anywhere, never matched: {len(truly_dormant)}")
for n in truly_dormant[:10]:
    print(f"  {n}")
