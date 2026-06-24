"""
List unassigned customers with recent outstanding bills.
Run from backend/ directory.
"""
import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

# Pull all recent outstanding for unassigned customers in one join via RPC-free approach:
# 1. Get all unassigned customer IDs
cust_rows = []
offset = 0
while True:
    batch = (
        supa.table("customers")
        .select("id, customer_name, address, phone")
        .is_("assigned_to", "null")
        .range(offset, offset + 999)
        .execute().data
    )
    cust_rows.extend(batch)
    if len(batch) < 1000:
        break
    offset += 1000

cust_map = {r["id"]: r for r in cust_rows}
cust_ids = list(cust_map.keys())

if not cust_ids:
    print("No unassigned customers found.")
    exit()

# 2. Pull recent outstanding for those customers (positive amounts only)
bills = []
offset = 0
while True:
    batch = (
        supa.table("outstanding")
        .select("customer_id, pending_amount")
        .eq("age_status", "recent")
        .gt("pending_amount", 0)
        .in_("customer_id", cust_ids)
        .range(offset, offset + 999)
        .execute().data
    )
    bills.extend(batch)
    if len(batch) < 1000:
        break
    offset += 1000

# 3. Aggregate by customer
totals = {}
for b in bills:
    cid = b["customer_id"]
    totals[cid] = totals.get(cid, 0) + float(b["pending_amount"])

# 4. Sort and print
results = [
    (cust_map[cid], total)
    for cid, total in totals.items()
    if total > 0
]
results.sort(key=lambda x: -x[1])

print(f"{'#':<4}  {'Customer':<40}  {'Pending':>12}  {'Address / Phone'}")
print("-" * 100)
for i, (c, total) in enumerate(results, 1):
    location = c["address"] or ""
    phone    = c["phone"] or ""
    extra    = f"{location}  {phone}".strip(" /") or "—"
    print(f"{i:<4}  {c['customer_name']:<40}  Rs {total:>10,.0f}  {extra}")

print("-" * 100)
print(f"Total: {len(results)} unassigned customers  |  Rs {sum(t for _, t in results):,.0f} combined outstanding")
