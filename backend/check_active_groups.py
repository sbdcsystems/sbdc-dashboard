import json, re
from collections import Counter

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

import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SECRET_KEY")
supabase = create_client(url, key)

customers_result = supabase.table("customers").select("customer_name").execute()
customers = customers_result.data

parent_counts = Counter()
for c in customers:
    norm = normalize(c["customer_name"])
    ledger_data = ledger_lookup.get(norm)
    if ledger_data and ledger_data.get("parent"):
        parent_counts[ledger_data["parent"]] += 1
    else:
        parent_counts["NO PARENT FOUND"] += 1

with open("active_customer_groups.txt", "w", encoding="utf-8") as out:
    for parent, count in parent_counts.most_common():
        out.write(f"{count:4d}  {parent}\n")

print("Saved to active_customer_groups.txt")
print("\nTop groups among your 240 active customers:")
for parent, count in parent_counts.most_common(15):
    print(f"{count:4d}  {parent}")
