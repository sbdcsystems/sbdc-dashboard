import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SECRET_KEY")
supabase = create_client(url, key)

total = supabase.table("customers").select("id", count="exact").execute()
print(f"Total customers: {total.count}")

flagged = supabase.table("customers").select("id", count="exact").eq("flagged", True).execute()
print(f"Flagged customers: {flagged.count}")

users = supabase.table("users").select("id, name").execute().data
for u in users:
    count = supabase.table("customers").select("id", count="exact").eq("assigned_to", u["id"]).execute()
    print(f"{u['name']}: {count.count} total customers")

unassigned = supabase.table("customers").select("id", count="exact").is_("assigned_to", "null").execute()
print(f"Unassigned: {unassigned.count} total customers")

print("\n=== Outstanding dues summary (should match dashboard) ===")
result = supabase.table("outstanding_status_summary").select("*").execute()
for row in result.data:
    print(row)

print("\n=== Possible near-duplicate customer names (worth a manual look) ===")
all_customers = supabase.table("customers").select("customer_name").execute().data
seen = {}
for c in all_customers:
    key = c["customer_name"].lower().strip()
    seen[key] = seen.get(key, 0) + 1
dupes = {k: v for k, v in seen.items() if v > 1}
if dupes:
    for name, count in dupes.items():
        print(f"  {name!r} appears {count} times")
else:
    print("  No exact-name duplicates found.")
