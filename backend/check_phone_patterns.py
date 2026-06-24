import re

with open("ledger_master.xml", "r", encoding="utf-8", errors="replace") as f:
    content = f.read()

ledgerphone_count = len(re.findall(r'<LEDGERPHONE', content))
print(f"LEDGERPHONE tags found: {ledgerphone_count}")

# look for any line in address that looks like a 10-digit phone number, regardless of keyword
addr_lines = re.findall(r'<ADDRESS TYPE="String">(.*?)</ADDRESS>', content)
digit_lines = [a for a in addr_lines if re.search(r'\d{10}', a)]
print(f"Address lines containing a 10-digit number: {len(digit_lines)}")
print("\nSample of these lines:")
for line in digit_lines[:15]:
    print(f"  {line}")
