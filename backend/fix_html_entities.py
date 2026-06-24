import os, re
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SECRET_KEY")
supabase = create_client(url, key)

def unescape(s):
    return (s.replace("&amp;", "&")
              .replace("&lt;", "<")
              .replace("&gt;", ">")
              .replace("&quot;", '"')
              .replace("&#39;", "'"))

result = supabase.table("customers").select("id, customer_name").execute()
customers = result.data

fixed = 0
for c in customers:
    clean_name = unescape(c["customer_name"])
    if clean_name != c["customer_name"]:
        supabase.table("customers").update({"customer_name": clean_name}).eq("id", c["id"]).execute()
        print(f"Fixed: {c['customer_name']!r} -> {clean_name!r}")
        fixed += 1

print(f"\nTotal fixed: {fixed}")
