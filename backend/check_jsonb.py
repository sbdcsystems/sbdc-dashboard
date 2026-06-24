import os, json
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client
from datetime import date

load_dotenv(Path(__file__).parent.parent / ".env", override=True)
supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

today = date.today().isoformat()
print("Checking daily_sales for date:", today)

r = supa.table("daily_sales").select("items").eq("sale_date", today).maybe_single().execute()
if not r.data:
    # Also check the most recent date
    print("No daily_sales row for today -- checking most recent row")
    r2 = supa.table("daily_sales").select("sale_date, items").order("sale_date", desc=True).limit(1).execute()
    if not r2.data:
        print("No rows at all in daily_sales")
    else:
        row = r2.data[0]
        print(f"Most recent row: {row['sale_date']}")
        items = row.get("items", [])
        print(f"Total items in JSONB: {len(items)}")
        for item in items:
            name = (item.get("customer_name") or "")
            if "mookambikai" in name.lower() or "chandrasekaran" in name.lower():
                print("MATCH:", json.dumps(item, indent=2))
        print()
        print("First 3 items (to check structure):")
        for item in items[:3]:
            print(json.dumps(item))
else:
    items = r.data.get("items", [])
    print(f"Total items in JSONB: {len(items)}")
    for item in items:
        name = (item.get("customer_name") or "")
        if "mookambikai" in name.lower() or "chandrasekaran" in name.lower():
            print("MATCH:", json.dumps(item, indent=2))
    print()
    print("First 3 items (to check structure):")
    for item in items[:3]:
        print(json.dumps(item))
