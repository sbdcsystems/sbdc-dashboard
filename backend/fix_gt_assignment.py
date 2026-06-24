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

gt_names = [name for name, data in ledgers.items() if data.get("parent") and "(GT)" in data["parent"]]
print(f"Total Tally ledger names under any (GT) town group: {len(gt_names)}\n")

customers = supabase.table("customers").select("id, customer_name, assigned_to").execute().data
customer_by_norm = {normalize(c["customer_name"]): c for c in customers}

users = supabase.table("users").select("id, name").execute().data
id_to_name = {u["id"]: u["name"] for u in users}
thiagarajan_id = next(u["id"] for u in users if u["name"] == "Thiagarajan")

found = []
for name in gt_names:
    norm = normalize(name)
    c = customer_by_norm.get(norm)
    if c:
        found.append(c)

print(f"{len(found)} of {len(gt_names)} exist as current customer records.\n")

currently_unassigned = sum(1 for c in found if c["assigned_to"] is None)
currently_other = [(c["customer_name"], id_to_name.get(c["assigned_to"])) for c in found if c["assigned_to"] not in (None, thiagarajan_id)]

print(f"Currently unassigned: {currently_unassigned}")
print(f"Currently assigned to someone else (potential conflict): {len(currently_other)}")
for n, a in currently_other:
    print(f"  {n} -> {a}")

print("\nFixing: assigning all (GT) customers to Thiagarajan...")
fixed = 0
for c in found:
    if c["assigned_to"] != thiagarajan_id:
        supabase.table("customers").update({"assigned_to": thiagarajan_id}).eq("id", c["id"]).execute()
        fixed += 1

print(f"Updated {fixed} customer records to Thiagarajan.")

print("\n=== Recalculated total customers per staff ===")
for u in users:
    count = supabase.table("customers").select("id", count="exact").eq("assigned_to", u["id"]).execute()
    print(f"{u['name']}: {count.count} total customers")
unassigned = supabase.table("customers").select("id", count="exact").is_("assigned_to", "null").execute()
print(f"Unassigned: {unassigned.count} total customers")
