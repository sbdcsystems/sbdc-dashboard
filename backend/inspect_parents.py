import re

with open("ledger_master.xml", "r", encoding="utf-8", errors="replace") as f:
    content = f.read()

parents = re.findall(r'<PARENT TYPE="String">(.*?)</PARENT>', content)

from collections import Counter
counts = Counter(parents)

with open("parent_groups.txt", "w", encoding="utf-8") as out:
    out.write(f"Total ledgers with a PARENT tag: {len(parents)}\n")
    out.write(f"Unique PARENT group names: {len(counts)}\n\n")
    for name, count in sorted(counts.items()):
        out.write(f"{count:4d}  {name}\n")

print("Saved to parent_groups.txt")
