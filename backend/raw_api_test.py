import os, requests
from dotenv import load_dotenv

load_dotenv("../.env", override=True)

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SECRET_KEY")

endpoint = f"{url}/rest/v1/customers?select=id,customer_name&limit=3"
headers = {
    "apikey": key,
    "Authorization": f"Bearer {key}"
}

response = requests.get(endpoint, headers=headers)
print(f"Status code: {response.status_code}")
print(f"Response: {response.text[:500]}")
