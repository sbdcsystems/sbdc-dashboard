import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SECRET_KEY")
supabase = create_client(url, key)

names = ["United Processors", "M.R.P.Dyeing", "Sri Bhadri Narayana Textiles", "Karthiga Colours (Gandhi)"]

for name in names:
    c = supabase.table("customers").select("id, customer_name, flagged, flagged_reason").ilike("customer_name", f"%{name.split('(')[0].strip()}%").execute()
    if not c.data:
        print(f"{name}: not found in customers table")
        continue
    for cust in c.data:
        o = supabase.table("outstanding").select("pending_amount, age_status").eq("customer_id", cust["id"]).execute()
        total = sum(float(row["pending_amount"] or 0) for row in o.data)
        print(f"{cust['customer_name']}: pending = Rs.{total:,.0f}  ({len(o.data)} bills)  flagged={cust['flagged']}")
