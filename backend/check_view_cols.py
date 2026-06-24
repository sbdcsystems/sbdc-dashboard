import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

row = supabase.table("customer_list_view").select("*").limit(1).execute()
if row.data:
    print("customer_list_view columns:", list(row.data[0].keys()))
    print("Sample:", row.data[0])
