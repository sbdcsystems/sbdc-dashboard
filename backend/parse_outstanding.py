import re
from datetime import datetime, timedelta
import json

INPUT_FILE = "tally_with_dates.xml"
OUTPUT_FILE = "parsed_outstanding.json"
RECENT_MONTHS = 12


def parse_tally_date(date_str):
    if not date_str or date_str.strip() == "":
        return None
    try:
        dt = datetime.strptime(date_str.strip(), "%d-%b-%y")
        return dt.date()
    except ValueError:
        return None


def extract_bills(xml_text):
    pattern = re.compile(
        r"<BILLFIXED>\s*"
        r"<BILLDATE>(.*?)</BILLDATE>\s*"
        r"<BILLREF>(.*?)</BILLREF>\s*"
        r"<BILLPARTY>(.*?)</BILLPARTY>\s*"
        r"</BILLFIXED>\s*"
        r"<BILLCL>(.*?)</BILLCL>\s*"
        r"<BILLDUE>(.*?)</BILLDUE>\s*"
        r"<BILLOVERDUE>(.*?)</BILLOVERDUE>",
        re.DOTALL
    )
    matches = pattern.findall(xml_text)
    bills = []
    for m in matches:
        bill_date, bill_ref, bill_party, bill_cl, bill_due, bill_overdue = m
        bills.append({
            "invoice_date_raw": bill_date.strip(),
            "invoice_ref": bill_ref.strip(),
            "customer_name": bill_party.strip(),
            "pending_amount_raw": bill_cl.strip(),
            "due_date_raw": bill_due.strip(),
            "days_overdue_raw": bill_overdue.strip(),
        })
    return bills


def clean_bill(raw_bill):
    invoice_date = parse_tally_date(raw_bill["invoice_date_raw"])
    due_date = parse_tally_date(raw_bill["due_date_raw"])
    try:
        pending_amount = abs(float(raw_bill["pending_amount_raw"]))
    except ValueError:
        pending_amount = 0.0
    try:
        days_overdue = int(float(raw_bill["days_overdue_raw"]))
    except ValueError:
        days_overdue = 0

    if days_overdue <= 30:
        bucket = "0-30"
    elif days_overdue <= 60:
        bucket = "30-60"
    elif days_overdue <= 90:
        bucket = "60-90"
    elif days_overdue <= 120:
        bucket = "90-120"
    else:
        bucket = "120+"

    return {
        "customer_name": raw_bill["customer_name"],
        "invoice_ref": raw_bill["invoice_ref"],
        "invoice_date": invoice_date.isoformat() if invoice_date else None,
        "due_date": due_date.isoformat() if due_date else None,
        "pending_amount": round(pending_amount, 2),
        "days_overdue": days_overdue,
        "bucket": bucket,
    }


def tag_bill_age(bills, months=RECENT_MONTHS):
    cutoff = datetime.now().date() - timedelta(days=months * 30)
    recent_count = 0
    stale_count = 0
    unknown_count = 0
    for bill in bills:
        if bill["invoice_date"] is None:
            bill["age_status"] = "unknown"
            unknown_count += 1
            continue
        invoice_date = datetime.fromisoformat(bill["invoice_date"]).date()
        if invoice_date >= cutoff:
            bill["age_status"] = "recent"
            recent_count += 1
        else:
            bill["age_status"] = "stale"
            stale_count += 1
    return bills, recent_count, stale_count, unknown_count


def main():
    print("=" * 50)
    print("SUPREME BALAJI -- OUTSTANDING BILLS PARSER")
    print(f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    try:
        with open(INPUT_FILE, "r", encoding="utf-8") as f:
            xml_text = f.read()
    except FileNotFoundError:
        print(f"Could not find {INPUT_FILE}")
        return

    print(f"Loaded {INPUT_FILE} ({len(xml_text)} characters)")

    raw_bills = extract_bills(xml_text)
    print(f"Found {len(raw_bills)} bill entries in the XML")

    if len(raw_bills) == 0:
        print("No bills extracted -- check XML structure")
        print(xml_text[:500])
        return

    cleaned_bills = [clean_bill(b) for b in raw_bills]
    tagged_bills, recent_count, stale_count, unknown_count = tag_bill_age(cleaned_bills)

    print(f"Tagged all {len(tagged_bills)} bills by age:")
    print(f"  Recent (last {RECENT_MONTHS} months): {recent_count}")
    print(f"  Stale (older): {stale_count}")
    print(f"  Unknown date: {unknown_count}")

    recent_bills = [b for b in tagged_bills if b["age_status"] == "recent"]
    stale_bills = [b for b in tagged_bills if b["age_status"] == "stale"]

    total_recent = sum(b["pending_amount"] for b in recent_bills)
    total_stale = sum(b["pending_amount"] for b in stale_bills)
    unique_customers_recent = len(set(b["customer_name"] for b in recent_bills))
    unique_customers_stale = len(set(b["customer_name"] for b in stale_bills))

    print()
    print("Summary:")
    print(f"  RECENT outstanding: Rs {total_recent:,.2f} across {unique_customers_recent} customers")
    print(f"  STALE outstanding (old/bad-debt candidates): Rs {total_stale:,.2f} across {unique_customers_stale} customers")
    print()

    bucket_counts = {}
    for b in recent_bills:
        bucket_counts[b["bucket"]] = bucket_counts.get(b["bucket"], 0) + 1
    print("  Recent bills -- bucket breakdown:")
    for bucket in ["0-30", "30-60", "60-90", "90-120", "120+"]:
        print(f"    {bucket}: {bucket_counts.get(bucket, 0)} bills")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(tagged_bills, f, indent=2, ensure_ascii=False)

    print()
    print(f"Saved ALL {len(tagged_bills)} records (tagged recent/stale/unknown) to {OUTPUT_FILE}")
    print()
    print("=" * 50)
    print("Sample of 3 recent records:")
    print("=" * 50)
    for b in recent_bills[:3]:
        print(json.dumps(b, indent=2, ensure_ascii=False))
        print()

    print("=" * 50)
    print("Sample of 3 stale records:")
    print("=" * 50)
    for b in stale_bills[:3]:
        print(json.dumps(b, indent=2, ensure_ascii=False))
        print()


if __name__ == "__main__":
    main()
