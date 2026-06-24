import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

# Get one row to see all columns
row = supabase.table("customers").select("*").limit(1).execute()
if row.data:
    print("customers columns:", list(row.data[0].keys()))
