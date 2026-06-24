import os, json
from collections import defaultdict
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

with open("ledger_master_parsed.json", encoding="utf-8") as f:
    ledger = json.load(f)
name_to_parent = {k.lower().strip(): v.get("parent", "UNKNOWN") for k, v in ledger.items()}

# PARENT groups that are NOT real customers
NON_CUSTOMER_PARENTS = {
    "sundry creditors - other purchase",
    "sundry creditors (tds)",
    "sundry creditors",
    "sundry creditors - others",
    "administrative expenses",
    "short term loans and advances",
    "flying colourss - group",
}

# Get unassigned customers with dues, not flagged
all_out = []
offset = 0
while True:
    batch = supabase.table("outstanding").select("customer_id").range(offset, offset + 999).execute().data
    all_out.extend(batch)
    if len(batch) < 1000:
        break
    offset += 1000

cids = list(set(r["customer_id"] for r in all_out if r["customer_id"]))

candidates = []
for i in range(0, len(cids), 500):
    rows = supabase.table("customers").select("id, customer_name, phone, assigned_to, flagged").in_("id", cids[i:i+500]).execute().data
    candidates.extend(rows)

# Filter: unassigned + not flagged
target = [c for c in candidates if c["assigned_to"] is None and not c["flagged"]]

# Filter: not a non-customer PARENT group
real_customers = []
for c in target:
    parent = name_to_parent.get(c["customer_name"].lower().strip(), "UNKNOWN")
    if parent.lower() not in NON_CUSTOMER_PARENTS:
        c["_parent"] = parent
        real_customers.append(c)

# Get outstanding amounts for these customers
target_ids = [c["id"] for c in real_customers]
all_bills = []
offset = 0
while True:
    batch = supabase.table("outstanding").select("customer_id, pending_amount, age_status").range(offset, offset + 999).execute().data
    all_bills.extend(batch)
    if len(batch) < 1000:
        break
    offset += 1000

bills_by_cid = defaultdict(list)
for b in all_bills:
    if b["customer_id"] in target_ids:
        bills_by_cid[b["customer_id"]].append(b)

# Build final rows
rows = []
for c in real_customers:
    bills = bills_by_cid[c["id"]]
    recent = sum(b["pending_amount"] for b in bills if b["age_status"] == "recent")
    stale  = sum(b["pending_amount"] for b in bills if b["age_status"] == "stale")
    total  = recent + stale
    rows.append({
        "name": c["customer_name"],
        "phone": c["phone"] or "—",
        "parent": c["_parent"],
        "recent": recent,
        "stale": stale,
        "total": total,
    })

rows.sort(key=lambda r: r["total"], reverse=True)

# Print clean list
print(f"CUSTOMERS WITH OUTSTANDING DUES — NO STAFF ASSIGNED")
print(f"(For review with Venkatesh — {len(rows)} customers)\n")
print(f"{'#':<4} {'Customer Name':<45} {'Phone':<15} {'Tally Group':<35} {'Recent Due':>13} {'Stale Due':>13} {'Total Due':>13}")
print("-" * 145)
for i, r in enumerate(rows, 1):
    print(f"{i:<4} {r['name']:<45} {r['phone']:<15} {r['parent']:<35} {r['recent']:>13,.0f} {r['stale']:>13,.0f} {r['total']:>13,.0f}")

total_recent = sum(r["recent"] for r in rows)
total_stale  = sum(r["stale"]  for r in rows)
total_all    = sum(r["total"]  for r in rows)
print("-" * 145)
print(f"{'TOTAL':<4} {'':<45} {'':<15} {'':<35} {total_recent:>13,.0f} {total_stale:>13,.0f} {total_all:>13,.0f}")
print(f"\nAll amounts in Indian Rupees (INR).")
