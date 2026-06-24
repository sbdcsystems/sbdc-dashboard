import os
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(Path(__file__).parent.parent / ".env", override=True)
supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

# Check if Sree Mookambikai Dyers appears in customer_list_view
r = supa.table("customer_list_view").select("*").ilike("customer_name", "%Mookambikai%").execute()
print("customer_list_view rows matching Mookambikai:")
print(r.data)

print()
# Also check raw customers table
r2 = supa.table("customers").select("id, customer_name, assigned_to, customer_type").ilike("customer_name", "%Mookambikai%").execute()
print("customers table rows matching Mookambikai:")
print(r2.data)
