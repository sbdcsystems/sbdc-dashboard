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

GROUP_TO_STAFF = {
    '1.venkatesh - parties': 'Venkatesh',
    '2.thiagarajan - parties': 'Thiagarajan',
    '3.gowtham - parties': 'Gowtham',
    '7.levaset - parties': 'Vijaya Priya',
    '8.vetri-parties': 'Vijaya Priya',
    '9.vijayapriya - parties': 'Vijaya Priya',
    'bill wise - j.venkatesh': 'Venkatesh',
    'bill wise - s.gowtham': 'Gowtham',
    'bill wise - g.thiagarajan': 'Thiagarajan',
}

def normalize_group(g):
    g = g.lower()
    g = re.sub(r'[^a-z0-9]+', ' ', g)
    return re.sub(r'\s+', ' ', g).strip()

GROUP_TO_STAFF_NORM = {normalize_group(k): v for k, v in GROUP_TO_STAFF.items()}

users_result = supabase.table("users").select("id, name").execute()
staff_lookup = {u["name"]: u["id"] for u in users_result.data}

customers_result = supabase.table("customers").select("id, customer_name").execute()
customers = customers_result.data

assigned = 0
skipped_no_match = 0
skipped_no_group_mapping = 0

for c in customers:
    norm = normalize(c["customer_name"])
    ledger_data = ledger_lookup.get(norm)
    if not ledger_data or not ledger_data.get("parent"):
        skipped_no_match += 1
        continue

    group_norm = normalize_group(ledger_data["parent"])
    staff_name = GROUP_TO_STAFF_NORM.get(group_norm)
    if not staff_name:
        skipped_no_group_mapping += 1
        continue

    staff_id = staff_lookup.get(staff_name)
    if not staff_id:
        continue

    supabase.table("customers").update({"assigned_to": staff_id}).eq("id", c["id"]).execute()
    assigned += 1

print(f"Assigned: {assigned}")
print(f"No Tally group match at all: {skipped_no_match}")
print(f"Had a group, but not a staff group (cash/town/bad-debt/etc): {skipped_no_group_mapping}")
