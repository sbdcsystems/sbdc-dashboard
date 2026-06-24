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

result = supabase.table("customer_list_view").select("customer_name, present_pending, archived_pending").execute()
customers = result.data

print("Customers whose Tally PARENT group contains the word group:\n")
total_present = 0
for c in customers:
    norm = normalize(c["customer_name"])
    ledger_data = ledger_lookup.get(norm)
    if ledger_data and ledger_data.get("parent") and "group" in ledger_data["parent"].lower():
        print(f"{c['customer_name']:40s} parent={ledger_data['parent']:35s} present=Rs.{c['present_pending']:.0f}")
        total_present += float(c["present_pending"] or 0)

print(f"\nTotal present-due sitting in related-party accounts: Rs.{total_present:.0f}")
