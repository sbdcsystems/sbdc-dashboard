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
for name in ledgers:
    ledger_lookup[normalize(name)] = name

result = supabase.table("customers").select("id, customer_name").execute()
customers = result.data

print(f"Total customers in Supabase: {len(customers)}")

matched = []
unmatched = []

for c in customers:
    norm = normalize(c["customer_name"])
    if norm in ledger_lookup:
        matched.append((c["customer_name"], ledger_lookup[norm]))
    else:
        unmatched.append(c["customer_name"])

print(f"Matched: {len(matched)}")
print(f"Unmatched: {len(unmatched)}")

with open("match_report.txt", "w", encoding="utf-8") as out:
    out.write(f"Total customers in Supabase: {len(customers)}\n")
    out.write(f"Matched: {len(matched)}\n")
    out.write(f"Unmatched: {len(unmatched)}\n\n")
    out.write("--- UNMATCHED (need closer look) ---\n")
    for name in unmatched:
        out.write(f"{name}\n")

print("Full report saved to match_report.txt")
