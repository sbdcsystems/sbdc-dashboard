import os
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(Path(__file__).parent.parent / ".env", override=True)
supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

# Total customers in view
r = supa.table("customer_list_view").select("id", count="exact").execute()
print(f"Total rows in customer_list_view: {r.count}")

# Specifically check if Sree Mookambikai appears within first 1000 rows (default PostgREST cap)
# PostgREST returns in undefined order without .order()
# Let's check rank
rows = supa.table("customer_list_view").select("id, customer_name").range(0, 999).execute().data
names = [r["customer_name"] for r in rows]
if "Sree Mookambikai Dyers" in names:
    print(f"Sree Mookambikai Dyers IS in first 1000 rows (index {names.index('Sree Mookambikai Dyers')})")
else:
    print("Sree Mookambikai Dyers is NOT in first 1000 rows")
    print(f"Total fetched in first 1000: {len(rows)}")
