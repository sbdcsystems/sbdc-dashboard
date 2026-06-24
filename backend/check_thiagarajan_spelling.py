import json, re

with open("ledger_master_parsed.json", "r", encoding="utf-8") as f:
    ledgers = json.load(f)

parents = set()
for name, data in ledgers.items():
    p = data.get("parent")
    if p and re.search(r'th[ia]y?agarajan', p, re.IGNORECASE):
        parents.add(p)

print("All parent group names matching any spelling of Thiagarajan:\n")
for p in sorted(parents):
    count = sum(1 for n, d in ledgers.items() if d.get("parent") == p)
    print(f"  {p!r}  ({count} ledger entries)")
