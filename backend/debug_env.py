import os
from dotenv import load_dotenv

load_dotenv("../.env", override=True)

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SECRET_KEY")

print(f"URL loaded: {url}")
if key:
    print(f"Key loaded: YES, length={len(key)}, starts with: {key[:12]}...")
else:
    print("Key loaded: NO (None)")
