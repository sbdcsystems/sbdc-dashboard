"""
Add two new customers to the customers table.
Run from backend/ directory.
"""
import os
from dotenv import load_dotenv
from supabase import create_client
from pathlib import Path

load_dotenv(Path(__file__).parent.parent / ".env", override=True)
supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

# Look up staff UUIDs by name
staff = supa.table("users").select("id, name").execute().data
print("Staff members:", staff)
staff_by_name = {r["name"].strip().lower(): r["id"] for r in staff}

thiagarajan_id = staff_by_name.get("thiagarajan")
venkatesh_id   = staff_by_name.get("venkatesh")

print(f"Thiagarajan UUID : {thiagarajan_id}")
print(f"Venkatesh UUID   : {venkatesh_id}")

if not thiagarajan_id or not venkatesh_id:
    print("ERROR: Could not find one or both staff members. Check name spelling.")
    exit(1)

new_customers = [
    {
        "customer_name": "Eswari Dyeing",
        "assigned_to":   thiagarajan_id,
        "customer_type": "credit",
        "credit_days":   90,
    },
    {
        "customer_name": "Sita Laxmi Silk Traders (K.M.Shanmugam)",
        "assigned_to":   venkatesh_id,
        "customer_type": "credit",
        "credit_days":   90,
    },
]

for c in new_customers:
    # Check if already exists
    existing = supa.table("customers").select("id, customer_name").ilike("customer_name", c["customer_name"]).execute().data
    if existing:
        print(f"SKIP: '{c['customer_name']}' already exists: {existing}")
        continue

    r = supa.table("customers").insert(c).execute()
    if r.data:
        print(f"INSERTED: '{c['customer_name']}' id={r.data[0]['id']}")
    else:
        print(f"FAILED: '{c['customer_name']}' — {r}")
