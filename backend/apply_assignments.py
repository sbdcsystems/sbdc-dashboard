"""
Apply staff assignments for previously-unassigned customers.
Run from backend/ directory.
"""
import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("../.env", override=True)
supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

VENKATESH   = "122839f7-01da-4b62-aaba-18fe5b750d41"
GOWTHAM     = "99403aa6-c53b-438f-88b3-53012f15d3d5"
THIAGARAJAN = "e3b14742-ebfb-4ec9-ac08-f06448751695"

ASSIGNMENTS = {
    VENKATESH: [
        "Flying Colourss - Sales",
        "Maruthi Bleaching",
        "Ananthu Textiles",
        "Muniraj Colours (P) Ltd",
        "Kavin Yarn Dyeing",
        "Sri Kobi Impex",
        "Sri Ranga Tex",
        "Kumaresan (G.V. Enterprises)",
        "Maruthi Bleaching - Yasodha Colours",
        "Sv Dyeing",
        "Yazhini Colours(Selvam Sample Dyeing)",
        "Sridhar",
    ],
    GOWTHAM: [
        "Psk Processing Mill",
        "Varnalaya Textile Mill",
        "Guhaan Textiles (Sree Laksme Narayan Fabrics)",
        "Royal Colours",
        "Kingbell",
    ],
    THIAGARAJAN: [
        "Shree Karumariamman Denim",
        "Sri Baalaji Colors - New",
        "Sri Sivasakthi Textiles ( Unit - I Mani )",
        "Sri Baalaji Colors - Old",
    ],
}

STAFF_NAMES = {VENKATESH: "Venkatesh", GOWTHAM: "Gowtham", THIAGARAJAN: "Thiagarajan"}

# ── Apply assignments ──────────────────────────────────────────────────────────

total_updated = 0
for staff_id, names in ASSIGNMENTS.items():
    updated = 0
    not_found = []
    for name in names:
        r = (
            supa.table("customers")
            .update({"assigned_to": staff_id})
            .eq("customer_name", name)
            .is_("assigned_to", "null")
            .execute()
        )
        if r.data:
            updated += len(r.data)
        else:
            not_found.append(name)
    print(f"{STAFF_NAMES[staff_id]:<14} — {updated} assigned", end="")
    if not_found:
        print(f"  WARNING: not found or already assigned: {not_found}", end="")
    print()
    total_updated += updated

# ── Flag Court Fees Paid ───────────────────────────────────────────────────────

r = (
    supa.table("customers")
    .update({
        "flagged":        True,
        "flagged_reason": "Not a customer — legal expense ledger entry imported incorrectly",
        "assigned_to":    None,
    })
    .eq("customer_name", "Court Fees Paid")
    .execute()
)
flagged_count = len(r.data)
print(f"Court Fees Paid — flagged={flagged_count > 0}")

# ── Confirm new unassigned count ───────────────────────────────────────────────

remaining = (
    supa.table("customers")
    .select("id", count="exact")
    .is_("assigned_to", "null")
    .execute()
    .count
)

print()
print(f"Total assignments applied : {total_updated}")
print(f"Customers still unassigned: {remaining}")
