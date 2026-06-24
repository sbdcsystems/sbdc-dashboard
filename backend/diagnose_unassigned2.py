import os, json
from collections import Counter
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

with open("ledger_master_parsed.json", encoding="utf-8") as f:
    ledger = json.load(f)
name_to_parent = {k.lower().strip(): v.get("parent", "UNKNOWN") for k, v in ledger.items()}

# --- Part 1: Unassigned breakdown (already correct, just reprinting summary) ---
print("=== UNASSIGNED CUSTOMERS BY TALLY PARENT GROUP ===")
unassigned_all = supabase.table("customers").select("customer_name").is_("assigned_to", "null").execute().data
parent_counts = Counter()
for c in unassigned_all:
    parent = name_to_parent.get(c["customer_name"].lower().strip(), "NO MATCH")
    parent_counts[parent] += 1
for parent, count in parent_counts.most_common():
    print(f"  {count:4d}  {parent}")
print(f"\nTotal unassigned: {len(unassigned_all)}")

# --- Part 2: Paginate outstanding to get ALL distinct customer_ids ---
print("\n\n=== ASSIGNMENT STATUS — CUSTOMERS WITH OUTSTANDING DUES ===\n")

all_out_rows = []
page_size = 1000
offset = 0
while True:
    batch = supabase.table("outstanding").select("customer_id").range(offset, offset + page_size - 1).execute().data
    all_out_rows.extend(batch)
    if len(batch) < page_size:
        break
    offset += page_size

outstanding_cids = set(r["customer_id"] for r in all_out_rows if r["customer_id"] is not None)
print(f"Total outstanding rows fetched: {len(all_out_rows)}")
print(f"Distinct customers in outstanding table: {len(outstanding_cids)}")

# Fetch those customers
cid_list = list(outstanding_cids)
customers_with_dues = []
for i in range(0, len(cid_list), 500):
    batch = supabase.table("customers").select("id, customer_name, assigned_to, flagged").in_("id", cid_list[i:i+500]).execute().data
    customers_with_dues.extend(batch)

assigned     = [c for c in customers_with_dues if c["assigned_to"] is not None]
unassigned_d = [c for c in customers_with_dues if c["assigned_to"] is None]

print(f"  Assigned:   {len(assigned)}")
print(f"  Unassigned: {len(unassigned_d)}")

if unassigned_d:
    parent_counts_d = Counter()
    for c in unassigned_d:
        parent = name_to_parent.get(c["customer_name"].lower().strip(), "NO MATCH")
        parent_counts_d[parent] += 1
    print("\n  Unassigned-with-dues by PARENT group:")
    for parent, count in parent_counts_d.most_common():
        print(f"    {count:4d}  {parent}")
    print(f"\n  Full list:")
    for c in sorted(unassigned_d, key=lambda x: x["customer_name"]):
        name = c["customer_name"].lower().strip()
        parent = name_to_parent.get(name, "?")
        flagged = " [FLAGGED]" if c.get("flagged") else ""
        print(f"    {c['customer_name']!r}  [{parent}]{flagged}")
