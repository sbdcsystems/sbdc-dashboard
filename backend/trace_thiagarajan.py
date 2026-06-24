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

thiagarajan_ledger_names = [
    name for name, data in ledgers.items()
    if data.get("parent") in ("2.Thiagarajan - Parties", "Bill Wise - G.Thiagarajan")
]

print(f"Total Tally ledger names under Thiagarajan's two groups: {len(thiagarajan_ledger_names)}\n")

customers = supabase.table("customers").select("id, customer_name, assigned_to").execute().data
customer_by_norm = {normalize(c["customer_name"]): c for c in customers}

users = supabase.table("users").select("id, name").execute().data
id_to_name = {u["id"]: u["name"] for u in users}

found_count = 0
not_found = []
for name in thiagarajan_ledger_names:
    norm = normalize(name)
    c = customer_by_norm.get(norm)
    if c:
        found_count += 1
        assigned_name = id_to_name.get(c["assigned_to"], "UNASSIGNED")
        print(f"FOUND as customer: {name!r} -> currently assigned to: {assigned_name}")
    else:
        not_found.append(name)

print(f"\n{found_count} of {len(thiagarajan_ledger_names)} exist as customer records.")
print(f"\nNames NOT found in customers table at all (likely no current outstanding bill):")
for n in not_found:
    print(f"  {n}")
