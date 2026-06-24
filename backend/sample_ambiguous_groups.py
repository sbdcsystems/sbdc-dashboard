import json, re

with open("ledger_master_parsed.json", "r", encoding="utf-8") as f:
    ledgers = json.load(f)

def unescape(s):
    return s.replace("&amp;", "&").replace("&#4;", "").strip()

groups_to_sample = [
    "Bad Debts Written Off",
    "Write Off 2024-25",
    "Kanagaraj - Parties",
    "Kumar Parties",
    "Magamayee - Group",
]

for g in groups_to_sample:
    names = [unescape(n) for n, d in ledgers.items() if d.get("parent") == g]
    print(f"=== {g} ({len(names)} total) ===")
    for n in names[:8]:
        print(f"  {n}")
    print()

print("=== The &#4; Primary entry ===")
for n, d in ledgers.items():
    if d.get("parent") and "&#4;" in d.get("parent", ""):
        print(f"  Name: {n!r}")
        print(f"  Opening balance: {d.get('opening_balance')}")
        print(f"  Address: {d.get('address')}")
