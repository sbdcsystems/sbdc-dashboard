import os, json, re
from datetime import datetime, timezone
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

ledger_lookup = {}
for name, data in ledgers.items():
    ledger_lookup[normalize(name)] = data

FLAG_GROUPS = {
    "5.Bad Debtors 24-25": "Bad debtor (Tally group: 5.Bad Debtors 24-25)",
    "6.Case Filed Customers": "Case filed against customer (Tally group: 6.Case Filed Customers)",
}

result = supabase.table("customers").select("id, customer_name").execute()
customers = result.data

flagged_count = 0
now_iso = datetime.now(timezone.utc).isoformat()

for c in customers:
    norm = normalize(c["customer_name"])
    ledger_data = ledger_lookup.get(norm)
    if not ledger_data:
        continue

    parent = ledger_data.get("parent")
    if parent in FLAG_GROUPS:
        supabase.table("customers").update({
            "flagged": True,
            "flagged_reason": FLAG_GROUPS[parent],
            "flagged_at": now_iso
        }).eq("id", c["id"]).execute()
        print(f"Flagged: {c['customer_name']} -> {FLAG_GROUPS[parent]}")
        flagged_count += 1

print(f"\nTotal flagged: {flagged_count}")
