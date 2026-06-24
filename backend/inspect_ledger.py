import re

with open("ledger_master.xml", "r", encoding="utf-8", errors="replace") as f:
    content = f.read()

idx = content.find("United Processors")
if idx == -1:
    print("Name not found, showing first 2000 chars instead")
    print(content[:2000])
else:
    start = max(0, idx - 800)
    end = idx + 200
    print(content[start:end])
