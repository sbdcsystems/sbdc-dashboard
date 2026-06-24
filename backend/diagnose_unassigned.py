import os, json
from collections import Counter
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

with open("ledger_master_parsed.json", encoding="utf-8") as f:
    ledger = json.load(f)

# Build a lookup: lowercase name -> parent group
name_to_parent = {k.lower().strip(): v.get("parent", "UNKNOWN") for k, v in ledger.items()}

# --- Part 1: Unassigned customers by Tally PARENT ---
print("=== UNASSIGNED CUSTOMERS BY TALLY PARENT GROUP ===\n")
unassigned = supabase.table("customers").select("customer_name").is_("assigned_to", "null").execute().data

parent_counts = Counter()
no_match = []
for c in unassigned:
    name = c["customer_name"].lower().strip()
    parent = name_to_parent.get(name)
    if parent:
        parent_counts[parent] += 1
    else:
        no_match.append(c["customer_name"])

for parent, count in parent_counts.most_common():
    print(f"  {count:4d}  {parent}")

print(f"\n  {len(no_match):4d}  [no match in ledger_master]")
if no_match[:10]:
    print("         (first 10):", no_match[:10])

print(f"\nTotal unassigned: {sum(parent_counts.values()) + len(no_match)}")

# --- Part 2: Assignment status for customers WITH outstanding dues ---
print("\n\n=== ASSIGNMENT STATUS — CUSTOMERS WITH OUTSTANDING DUES ===\n")

# Get all customer_ids that appear in outstanding table (any bill)
out_rows = supabase.table("outstanding").select("customer_id").execute().data
outstanding_cids = set(r["customer_id"] for r in out_rows)
print(f"Distinct customers in outstanding table: {len(outstanding_cids)}")

# Pull those customers with their assigned_to
# Supabase in-filter: batch if needed (254+ is fine for a single call)
cid_list = list(outstanding_cids)
batch_size = 500
customers_with_dues = []
for i in range(0, len(cid_list), batch_size):
    batch = cid_list[i:i+batch_size]
    rows = supabase.table("customers").select("id, customer_name, assigned_to").in_("id", batch).execute().data
    customers_with_dues.extend(rows)

assigned = [c for c in customers_with_dues if c["assigned_to"] is not None]
unassigned_dues = [c for c in customers_with_dues if c["assigned_to"] is None]

print(f"  Assigned:   {len(assigned)}")
print(f"  Unassigned: {len(unassigned_dues)}")

if unassigned_dues:
    print(f"\n  Unassigned customers WITH active dues ({len(unassigned_dues)} total):")
    # Cross-ref with Tally PARENT
    parent_counts_dues = Counter()
    no_match_dues = []
    for c in unassigned_dues:
        name = c["customer_name"].lower().strip()
        parent = name_to_parent.get(name)
        if parent:
            parent_counts_dues[parent] += 1
        else:
            no_match_dues.append(c["customer_name"])
    print("\n  By PARENT group:")
    for parent, count in parent_counts_dues.most_common():
        print(f"    {count:4d}  {parent}")
    if no_match_dues:
        print(f"    {len(no_match_dues):4d}  [no match in ledger]:", no_match_dues[:5])
    print(f"\n  Names (for manual review):")
    for c in sorted(unassigned_dues, key=lambda x: x["customer_name"]):
        name = c["customer_name"].lower().strip()
        parent = name_to_parent.get(name, "?")
        print(f"    {c['customer_name']!r}  [{parent}]")
