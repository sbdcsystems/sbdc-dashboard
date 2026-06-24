import re, json

with open("ledger_master.xml", "r", encoding="utf-8", errors="replace") as f:
    content = f.read()

ledger_blocks = re.findall(r'<LEDGER NAME="(.*?)" RESERVEDNAME="[^"]*">(.*?)</LEDGER>', content, re.DOTALL)

def unescape(s):
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()

def extract_phone_from_text(text):
    match = re.search(r'(?:Ph|Cell|Mob|Tel)[:.\s]*(\d{8,12})', text, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r'\b(\d{10})\b', text)
    if match:
        return match.group(1)
    return None

ledgers = {}
for name, block in ledger_blocks:
    name_clean = unescape(name)

    parent_match = re.search(r'<PARENT TYPE="String">(.*?)</PARENT>', block)
    parent = unescape(parent_match.group(1)) if parent_match else None

    gstin_match = re.search(r'<PARTYGSTIN TYPE="String">(.*?)</PARTYGSTIN>', block)
    gstin = unescape(gstin_match.group(1)) if gstin_match else None

    opening_match = re.search(r'<OPENINGBALANCE TYPE="Amount">(.*?)</OPENINGBALANCE>', block)
    opening = opening_match.group(1) if opening_match else None

    addr_lines = [unescape(a) for a in re.findall(r'<ADDRESS TYPE="String">(.*?)</ADDRESS>', block)]

    # Primary source: dedicated LEDGERPHONE field
    ledgerphone_match = re.search(r'<LEDGERPHONE[^>]*>(.*?)</LEDGERPHONE>', block)
    phone = unescape(ledgerphone_match.group(1)) if ledgerphone_match else None
    phone = re.sub(r'\D', '', phone) if phone else None

    # Fallback: scan address lines, removing any line that was a phone line from the address itself
    clean_addr_lines = []
    for line in addr_lines:
        found = extract_phone_from_text(line)
        if found and not phone:
            phone = found
        # only strip the line from address if it looks like a phone-only line
        if found and len(re.sub(r'\D', '', line)) >= 8:
            continue
        clean_addr_lines.append(line)

    ledgers[name_clean] = {
        "parent": parent,
        "gstin": gstin,
        "address": ", ".join(clean_addr_lines),
        "phone": phone,
        "opening_balance": opening,
    }

print(f"Total ledger entries parsed: {len(ledgers)}")
with_phone = sum(1 for l in ledgers.values() if l["phone"])
print(f"Entries with a phone number: {with_phone}")

with open("ledger_master_parsed.json", "w", encoding="utf-8") as f:
    json.dump(ledgers, f, indent=2, ensure_ascii=False)

print("Saved to ledger_master_parsed.json")
