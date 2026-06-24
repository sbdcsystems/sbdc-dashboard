import os
from dotenv import load_dotenv

load_dotenv("../.env", override=True)

key = os.environ.get("SUPABASE_SECRET_KEY")

print(f"Raw repr: {repr(key)}")
print(f"Length: {len(key)}")

stripped = key.strip()
print(f"Stripped length: {len(stripped)}")
print(f"Stripped repr: {repr(stripped)}")
