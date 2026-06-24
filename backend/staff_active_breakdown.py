"""
staff_active_breakdown.py

Per-staff breakdown:
  active   = assigned customers with at least 1 'recent' outstanding bill
  dormant  = assigned customers with no 'recent' bills (stale-only or nothing at all)

Run from backend/ with venv activated:
  python staff_active_breakdown.py
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])


def paginate(table, select, **filters):
    rows, offset = [], 0
    while True:
        q = sb.table(table).select(select)
        for col, val in filters.items():
            q = q.eq(col, val)
        batch = q.range(offset, offset + 999).execute().data
        rows += batch
        if len(batch) < 1000:
            break
        offset += 1000
    return rows


# ── Load data ──────────────────────────────────────────────────────────────────

print("Loading users …")
users = {r["id"]: r["name"] for r in sb.table("users").select("id, name").execute().data}

print("Loading customers …")
customers = paginate("customers", "id, customer_name, assigned_to")

print("Loading outstanding (recent only) …")
recent_rows = paginate("outstanding", "customer_id", age_status="recent")
recent_customer_ids = {r["customer_id"] for r in recent_rows}

print(f"  {len(customers)} customers total, {len(recent_customer_ids)} have recent bills\n")

# ── Aggregate ──────────────────────────────────────────────────────────────────

from collections import defaultdict

staff_active  = defaultdict(list)
staff_dormant = defaultdict(list)

for c in customers:
    uid   = c["assigned_to"]
    name  = users.get(uid, "Unassigned") if uid else "Unassigned"
    cname = c["customer_name"]
    if c["id"] in recent_customer_ids:
        staff_active[name].append(cname)
    else:
        staff_dormant[name].append(cname)

# ── Print ──────────────────────────────────────────────────────────────────────

all_staff = sorted(
    set(staff_active) | set(staff_dormant),
    key=lambda n: -len(staff_active[n]),
)

print(f"{'Staff':<18} {'Active':>6}  {'Dormant':>7}  {'Total':>5}  {'Active %':>8}")
print("-" * 52)
for name in all_staff:
    a = len(staff_active[name])
    d = len(staff_dormant[name])
    t = a + d
    pct = a / t * 100 if t else 0
    print(f"{name:<18} {a:>6}  {d:>7}  {t:>5}  {pct:>7.0f}%")

print()

# ── Dormant detail for assigned staff (skip Unassigned — too many) ─────────────
for name in all_staff:
    if name == "Unassigned":
        continue
    dormant = staff_dormant[name]
    if not dormant:
        continue
    print(f"-- {name} dormant customers ({len(dormant)}) ------------------")
    for n in sorted(dormant):
        print(f"   {n}")
    print()
