import os
import json
from datetime import datetime
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY")
INPUT_FILE = "parsed_outstanding.json"

supabase = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)


def load_parsed_data():
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_existing_customers():
    print("Fetching existing customers from Supabase...")
    response = supabase.table("customers").select("id, customer_name").execute()
    customer_map = {}
    for row in response.data:
        normalized_name = row["customer_name"].strip().lower()
        customer_map[normalized_name] = row["id"]
    print(f"Found {len(customer_map)} existing customers in Supabase")
    return customer_map


def create_missing_customers(bills, customer_map):
    unique_names = set(b["customer_name"].strip() for b in bills if b["customer_name"].strip())
    missing_names = [name for name in unique_names if name.lower() not in customer_map]

    print(f"Found {len(unique_names)} unique customer names in bills")
    print(f"Need to create {len(missing_names)} new customer records")

    if not missing_names:
        return customer_map

    batch_size = 100
    for i in range(0, len(missing_names), batch_size):
        batch = missing_names[i:i + batch_size]
        records = [
            {"customer_name": name, "customer_type": "credit", "credit_days": None}
            for name in batch
        ]
        response = supabase.table("customers").insert(records).execute()
        for row in response.data:
            customer_map[row["customer_name"].strip().lower()] = row["id"]
        print(f"  Created batch of {len(batch)} customers ({i + len(batch)}/{len(missing_names)})")

    return customer_map


def insert_outstanding_records(bills, customer_map):
    records = []
    skipped = 0
    for bill in bills:
        normalized_name = bill["customer_name"].strip().lower()
        customer_id = customer_map.get(normalized_name)
        if not customer_id:
            skipped += 1
            continue
        records.append({
            "customer_id": customer_id,
            "invoice_ref": bill["invoice_ref"],
            "invoice_date": bill["invoice_date"],
            "due_date": bill["due_date"],
            "pending_amount": bill["pending_amount"],
            "bucket": bill["bucket"],
            "days_overdue": bill["days_overdue"],
            "age_status": bill["age_status"],
        })

    print(f"Prepared {len(records)} records for insertion ({skipped} skipped)")

    batch_size = 200
    total_inserted = 0
    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        supabase.table("outstanding").insert(batch).execute()
        total_inserted += len(batch)
        print(f"  Inserted batch ({total_inserted}/{len(records)})")

    return total_inserted


def main():
    print("=" * 50)
    print("SUPREME BALAJI -- LOAD OUTSTANDING TO SUPABASE")
    print(f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    bills = load_parsed_data()
    print(f"Loaded {len(bills)} bills from {INPUT_FILE}")
    print()

    customer_map = get_existing_customers()
    print()

    customer_map = create_missing_customers(bills, customer_map)
    print()

    total_inserted = insert_outstanding_records(bills, customer_map)
    print()

    print("=" * 50)
    print(f"DONE -- Inserted {total_inserted} outstanding records into Supabase")
    print(f"Total customers in system: {len(customer_map)}")
    print("=" * 50)


if __name__ == "__main__":
    main()
