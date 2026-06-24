import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SECRET_KEY")
supabase = create_client(url, key)

c = supabase.table("customers").select("id").eq("customer_name", "Sri Bhadri Narayana Textiles").execute()
cust_id = c.data[0]["id"]

bills = supabase.table("outstanding").select("invoice_ref, invoice_date, pending_amount, age_status").eq("customer_id", cust_id).order("invoice_date").execute()

print(f"Total bills: {len(bills.data)}\n")
total = 0
for b in bills.data:
    total += float(b["pending_amount"] or 0)
    print(f"  {b['invoice_date']}  ref={b['invoice_ref']}  Rs.{float(b['pending_amount']):,.0f}  ({b['age_status']})")

print(f"\nTotal pending (sum of all bills): Rs.{total:,.0f}")

oldest = bills.data[0]["invoice_date"] if bills.data else None
print(f"Oldest bill date: {oldest}")
