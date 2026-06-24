import os, json, re
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SECRET_KEY")
supabase = create_client(url, key)

with open("ledger_master_parsed.json", "r", encoding="utf-8") as f:
    ledgers = json.load(f)

case_filed_names = [name for name, data in ledgers.items() if data.get("parent") == "6.Case Filed Customers"]

now_iso = datetime.now(timezone.utc).isoformat()
added = 0

for name in case_filed_names:
    data = ledgers[name]
    record = {
        "customer_name": name,
        "customer_type": "credit",
        "address": data.get("address") or None,
        "phone": data.get("phone") or None,
        "gst_number": data.get("gstin") or None,
        "flagged": True,
        "flagged_reason": "Case filed against customer (Tally group: 6.Case Filed Customers)",
        "flagged_at": now_iso,
    }
    result = supabase.table("customers").insert(record).execute()
    print(f"Added: {name}")
    added += 1

print(f"\nTotal added: {added}")
