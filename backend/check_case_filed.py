import re, json

with open("ledger_master_parsed.json", "r", encoding="utf-8") as f:
    ledgers = json.load(f)

case_filed = [name for name, data in ledgers.items() if data.get("parent") == "6.Case Filed Customers"]
print(f"Case Filed Customers found in Tally ledger master: {len(case_filed)}")
for name in case_filed:
    print(f"  - {name}")
