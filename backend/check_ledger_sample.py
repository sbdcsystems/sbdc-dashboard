import json
with open("ledger_master_parsed.json", encoding="utf-8") as f:
    data = json.load(f)
# Show a few entries to understand field names
for k in list(data.keys())[:3]:
    print(f"\n--- {k} ---")
    print(data[k])
