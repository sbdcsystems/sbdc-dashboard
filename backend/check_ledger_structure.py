import json
with open("ledger_master_parsed.json", encoding="utf-8") as f:
    data = json.load(f)
# Show structure of first entry
print(type(data))
if isinstance(data, list):
    print("Length:", len(data))
    print("First entry keys:", list(data[0].keys()) if data else "empty")
    print("Sample:", data[0])
elif isinstance(data, dict):
    print("Top-level keys:", list(data.keys())[:10])
