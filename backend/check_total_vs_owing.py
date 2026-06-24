import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SECRET_KEY")
supabase = create_client(url, key)

print("=== Customers currently owing money, by staff (this is what the dashboard shows) ===")
result = supabase.table("outstanding_by_staff_summary").select("*").execute()
for row in result.data:
    print(row)

print("\n=== TOTAL customers assigned to each staff member, regardless of whether they owe money ===")
users = supabase.table("users").select("id, name").execute().data
for u in users:
    count = supabase.table("customers").select("id", count="exact").eq("assigned_to", u["id"]).execute()
    print(f"{u['name']}: {count.count} total customers")

unassigned = supabase.table("customers").select("id", count="exact").is_("assigned_to", "null").execute()
print(f"Unassigned: {unassigned.count} total customers")
