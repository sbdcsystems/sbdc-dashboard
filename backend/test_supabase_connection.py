import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_SECRET_KEY")
print("URL:", url)
print("KEY (first 20 chars):", key[:20])
print("KEY length:", len(key))

supabase = create_client(url, key)
response = supabase.table("customers").select("id").limit(1).execute()
print("SUCCESS! Response:", response.data)
