import json, re, os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SECRET_KEY")
supabase = create_client(url, key)

with open("ledger_master.xml", "r", encoding="utf-8", errors="replace") as f:
    raw_xml = f.read()

def unescape(s):
    if not s:
        return s
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()

def normalize(name):
    name = name.lower()
    name = re.sub(r'[^a-z0-9\s]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def extract_phone_from_text(text):
    if not text:
        return None
    for segment in text.split(","):
        digits = "".join(ch for ch in segment if ch.isdigit())
        if len(digits) == 10 and digits[0] in "6789":
            return digits
        if len(digits) in (10, 11) and digits[0] == "0":
            return digits
    return None

ledger_blocks = re.findall(r'<LEDGER NAME="(.*?)" RESERVEDNAME="[^"]*">(.*?)</LEDGER>', raw_xml, re.DOTALL)

ledger_data = {}
for name, block in ledger_blocks:
    name_clean = unescape(name)
    parent_match = re.search(r'<PARENT[^>]*>(.*?)</PARENT>', block)
    parent = unescape(parent_match.group(1)) if parent_match else None

    gstin_match = re.search(r'<PARTYGSTIN[^>]*>(.*?)</PARTYGSTIN>', block)
    gstin = unescape(gstin_match.group(1)) if gstin_match else None

    addr_lines = [unescape(a) for a in re.findall(r'<ADDRESS TYPE="String">(.*?)</ADDRESS>', block)]
    address = ", ".join(addr_lines) if addr_lines else None

    phone = None
    ledgerphone_match = re.search(r'<LEDGERPHONE[^>]*>(.*?)</LEDGERPHONE>', block)
    if ledgerphone_match:
        phone = extract_phone_from_text(unescape(ledgerphone_match.group(1)))
    if not phone:
        for line in addr_lines:
            phone = extract_phone_from_text(line)
            if phone:
                break

    ledger_data[name_clean] = {"parent": parent, "gstin": gstin, "address": address, "phone": phone}

STAFF_GROUPS = {
    "1.Venkatesh - Parties": "Venkatesh",
    "Bill Wise - J.Venkatesh": "Venkatesh",
    "2.Thiagarajan - Parties": "Thiagarajan",
    "Bill Wise - G.Thiagarajan": "Thiagarajan",
    "3.Gowtham - Parties": "Gowtham",
    "Bill Wise - S.Gowtham": "Gowtham",
    "7.Levaset - Parties": "Vijaya Priya",
    "8.Vetri-Parties": "Vijaya Priya",
    "9.Vijayapriya - Parties": "Vijaya Priya",
    "Kanagaraj - Parties": "Vijaya Priya",
}
CASH_GROUP = "4.Cash - Parties"
BAD_DEBT_CURRENT_GROUP = "5.Bad Debtors 24-25"
CASE_FILED_GROUP = "6.Case Filed Customers"
BAD_DEBT_HISTORICAL_GROUP = "Bad Debts Written Off"
GENERIC_CUSTOMER_GROUP = "Sundry Debtors"

EXCLUDED_GROUPS = {
    "&#4; Primary", "Administrative Expenses", "Advertisement Expenses", "Axis Bank Ltd",
    "Bank Accounts", "Bank IMPS & Other Charges @18%", "Bank OD A/c", "Bonus Paid",
    "Building Maintenance", "Buildings & Lands", "CCTV", "Capital Account", "Cash-in-hand",
    "Computers & Accessories", "Direct Expenses", "Direct Incomes", "Duties & Taxes",
    "Dyers Commission - TDS", "Dyers Commission A/c", "Electrical Maintenance", "Finance Charges",
    "Freight Charges", "Furniture & Fittings", "Gst Payable",
    "Indirect Expenses", "Indirect Incomes", "Insurance Paid", "Interest Paid",
    "J.Venkatesh Capital A/c", "Office Expense", "Others Payable", "Output Tax",
    "Partners Salary Payable", "Plant & Machinery", "Power & Fuel Charges", "Prepaid Taxes",
    "Printing & Stationery", "Processing & Renewal Charges", "Professional Charges",
    "Purchase Accounts", "Rates & Taxes Paid", "Repairs and Maintenance", "Sales Accounts",
    "Secured Loan From Banks", "Secured Loans From NBFC", "Selling & Distribution",
    "Short Term Loans and Advances", "Staff - Salary", "Staff Welfare Expense",
    "Sundry Creditors", "Sundry Creditors (TDS)", "Sundry Creditors - Other Purchase",
    "Sundry Creditors - Others", "Suspense A/c", "TDS & GST Late Payment and Interest",
    "Tds Payable", "Telephone & Internet Charges", "Travelling Expenses", "Unsecured Loans",
    "Vehicle Maintenance", "Vehicles", "Wages Paid", "Write Off 2024-25",
}

users = supabase.table("users").select("id, name").execute().data
name_to_userid = {u["name"]: u["id"] for u in users}

existing_customers = supabase.table("customers").select("id, customer_name, flagged").execute().data
existing_by_norm = {normalize(c["customer_name"]): c for c in existing_customers}

inserted = 0
flagged_historical_count = 0
flying_colours_flagged = 0
skipped_existing = 0
skipped_excluded = 0
skipped_unknown = 0
sample_inserted = []
unknown_parents_seen = set()

for name, data in ledger_data.items():
    parent = data["parent"]
    if not parent:
        continue
    norm = normalize(name)

    if parent == "Flying Colourss - Group":
        existing = existing_by_norm.get(norm)
        if existing and not existing["flagged"]:
            supabase.table("customers").update({
                "flagged": True,
                "flagged_reason": "Family-owned company (Flying Colours), not an SBDC customer"
            }).eq("id", existing["id"]).execute()
            flying_colours_flagged += 1
        continue

    if parent in EXCLUDED_GROUPS:
        skipped_excluded += 1
        continue

    is_gt = "(GT)" in parent
    is_staff = parent in STAFF_GROUPS
    is_cash = parent == CASH_GROUP
    is_bad_debt_current = parent == BAD_DEBT_CURRENT_GROUP
    is_case_filed = parent == CASE_FILED_GROUP
    is_bad_debt_historical = parent == BAD_DEBT_HISTORICAL_GROUP
    is_generic = parent == GENERIC_CUSTOMER_GROUP
    is_group_suffix = parent.endswith("Group")

    if not (is_gt or is_staff or is_cash or is_bad_debt_current or is_case_filed or is_bad_debt_historical or is_generic or is_group_suffix):
        skipped_unknown += 1
        unknown_parents_seen.add(parent)
        continue

    if norm in existing_by_norm:
        skipped_existing += 1
        continue

    assigned_to = None
    if is_gt:
        assigned_to = name_to_userid.get("Thiagarajan")
    elif is_staff:
        assigned_to = name_to_userid.get(STAFF_GROUPS[parent])

    customer_type = "cash" if is_cash else "credit"
    credit_days = None if is_cash else 90

    flagged = False
    flagged_reason = None
    if is_bad_debt_current:
        flagged = True
        flagged_reason = "Bad debtor (current, FY24-25) per Tally"
    elif is_case_filed:
        flagged = True
        flagged_reason = "Case filed - legal recovery in progress"
    elif is_bad_debt_historical:
        flagged = True
        flagged_reason = "Historical bad debt - written off in Tally, not currently trading"
        flagged_historical_count += 1

    new_record = {
        "customer_name": name,
        "customer_type": customer_type,
        "credit_days": credit_days,
        "assigned_to": assigned_to,
        "phone": data["phone"],
        "address": data["address"],
        "gst_number": data["gstin"],
        "flagged": flagged,
        "flagged_reason": flagged_reason,
    }
    supabase.table("customers").insert(new_record).execute()
    inserted += 1
    if len(sample_inserted) < 12:
        sample_inserted.append(name)

print("=== IMPORT COMPLETE ===\n")
print(f"New customers inserted: {inserted}")
print(f"  of which historical bad-debt (flagged, dormant): {flagged_historical_count}")
print(f"Already existed, skipped: {skipped_existing}")
print(f"Excluded (non-customer accounts): {skipped_excluded}")
print(f"Flying Colours record(s) flagged as non-customer: {flying_colours_flagged}")
print(f"Skipped, unrecognized group (none expected): {skipped_unknown}")
if unknown_parents_seen:
    print("Unrecognized parent groups encountered:")
    for p in unknown_parents_seen:
        print(f"  {p}")

print("\nSample of newly inserted customers:")
for n in sample_inserted:
    print(f"  {n}")

total = supabase.table("customers").select("id", count="exact").execute()
print(f"\nTotal customers in database now: {total.count}")
