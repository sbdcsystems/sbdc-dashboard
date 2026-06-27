"""
find_similar_customers.py — fuzzy duplicate detection for the customers table.

Compares every customer name pair using difflib.SequenceMatcher and outputs
pairs whose similarity exceeds the threshold.

Usage:
    python find_similar_customers.py
    python find_similar_customers.py --threshold 85
    python find_similar_customers.py --threshold 80 --output dupes.csv

Output: similar_customers.csv (or --output path)
Columns: name1, name2, similarity_score, id1, id2
Sorted by similarity_score descending.
"""

import argparse
import csv
import os
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR.parent / ".env", override=True)


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio() * 100


def main():
    parser = argparse.ArgumentParser(description="Find similar customer names in Supabase")
    parser.add_argument("--threshold", type=float, default=80.0,
                        help="Minimum similarity %% to flag (default: 80)")
    parser.add_argument("--output", type=Path, default=BASE_DIR / "similar_customers.csv",
                        help="Output CSV path (default: backend/similar_customers.csv)")
    args = parser.parse_args()

    print(f"Threshold : {args.threshold}%")
    print(f"Output    : {args.output}")

    # ── Fetch all customers ───────────────────────────────────────────────────
    supa      = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
    customers = []
    offset    = 0
    while True:
        batch = (
            supa.table("customers")
            .select("id, customer_name")
            .order("customer_name")
            .range(offset, offset + 999)
            .execute().data
        ) or []
        customers.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000

    print(f"Fetched   : {len(customers)} customers")

    total_pairs = len(customers) * (len(customers) - 1) // 2
    print(f"Pairs     : {total_pairs:,} to compare...")

    # ── Compare all pairs ─────────────────────────────────────────────────────
    matches = []
    for c1, c2 in combinations(customers, 2):
        score = _similarity(c1["customer_name"], c2["customer_name"])
        if score >= args.threshold:
            matches.append({
                "name1":            c1["customer_name"],
                "name2":            c2["customer_name"],
                "similarity_score": round(score, 1),
                "id1":              c1["id"],
                "id2":              c2["id"],
            })

    matches.sort(key=lambda x: x["similarity_score"], reverse=True)
    print(f"Found     : {len(matches)} pair(s) at or above {args.threshold}%")

    # ── Write CSV ─────────────────────────────────────────────────────────────
    fields = ["name1", "name2", "similarity_score", "id1", "id2"]
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(matches)
    print(f"Written   : {args.output}")

    # ── Print top 30 to console ───────────────────────────────────────────────
    if not matches:
        print("\nNo matches found.")
        return

    show = min(30, len(matches))
    print(f"\nTop {show} matches:")
    print(f"  {'SCORE':>6}  {'NAME 1':<45}  NAME 2")
    print("  " + "-" * 100)
    for p in matches[:show]:
        print(f"  {p['similarity_score']:>5.1f}%  {p['name1']:<45}  {p['name2']}")


if __name__ == "__main__":
    main()
