import os, json, re
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

result = supabase.table("customers").select("id, customer_name").execute()
customers = result.data

updated = 0
got_address = 0
got_phone = 0
got_gst = 0
no_match = 0

for c in customers:
    norm = normalize(c["customer_name"])
    ledger_data = ledger_lookup.get(norm)

    if not ledger_data:
        no_match += 1
        continue

    update_fields = {}
    if ledger_data.get("address"):
        update_fields["address"] = ledger_data["address"]
        got_address += 1
    if ledger_data.get("phone"):
        update_fields["phone"] = ledger_data["phone"]
        got_phone += 1
    if ledger_data.get("gstin"):
        update_fields["gst_number"] = ledger_data["gstin"]
        got_gst += 1

    if update_fields:
        supabase.table("customers").update(update_fields).eq("id", c["id"]).execute()
        updated += 1

print(f"Total customers: {len(customers)}")
print(f"No Tally match: {no_match}")
print(f"Updated (at least one field): {updated}")
print(f"  - got address: {got_address}")
print(f"  - got phone:   {got_phone}")
print(f"  - got GST:     {got_gst}")
