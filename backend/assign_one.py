"""
One-off customer assignment. Run from backend/ directory.
Usage: python assign_one.py
"""
import os
from dotenv import load_dotenv
from supabase import create_client
from pathlib import Path

load_dotenv(Path(__file__).parent.parent / ".env", override=True)
supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

THIAGARAJAN = "e3b14742-ebfb-4ec9-ac08-f06448751695"

# Find the customer first (ilike in case of minor name variation)
found = supa.table("customers").select("id, customer_name, assigned_to").ilike("customer_name", "%Mookambikai%").execute().data

if not found:
    print("No customer matching 'Mookambikai' found.")
else:
    for row in found:
        print(f"Found: {row['customer_name']}  (assigned_to={row['assigned_to']})")

    if len(found) == 1:
        r = supa.table("customers").update({"assigned_to": THIAGARAJAN}).eq("id", found[0]["id"]).execute()
        if r.data:
            print(f"Assigned '{found[0]['customer_name']}' to Thiagarajan.")
        else:
            print("Update returned no data — check Supabase RLS or key.")
    else:
        print("Multiple matches — edit this script to pick the right one by id.")
