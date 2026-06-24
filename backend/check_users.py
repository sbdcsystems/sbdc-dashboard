import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SECRET_KEY")
supabase = create_client(url, key)

result = supabase.table("users").select("*").execute()
print(f"Total users: {len(result.data)}")
for u in result.data:
    print(u)
