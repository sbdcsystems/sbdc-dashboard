import requests, re

TALLY_URL = "http://192.168.0.205:9000"

xml_request = """<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>ClosingBalanceTest2</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVCURRENTCOMPANY>SUPREME BALAJI DYE CHEM - 25-26</SVCURRENTCOMPANY>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <SVFROMDATE>20010401</SVFROMDATE>
    <SVTODATE>20260620</SVTODATE>
   </STATICVARIABLES>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="ClosingBalanceTest2" ISMODIFY="No">
      <TYPE>Ledger</TYPE>
      <FETCH>NAME, PARENT, CLOSINGBALANCE</FETCH>
     </COLLECTION>
    </TDLMESSAGE>
   </TDL>
  </DESC>
 </BODY>
</ENVELOPE>"""

print("Fetching with explicit date range...")
response = requests.post(TALLY_URL, data=xml_request.encode("utf-8"), timeout=120)
content = response.content.decode("utf-8", errors="replace")

ledger_blocks = re.findall(r'<LEDGER NAME="(.*?)" RESERVEDNAME="[^"]*">(.*?)</LEDGER>', content, re.DOTALL)

print(f"\nAll ledger names containing 'bhadri' (case-insensitive):\n")
for name, block in ledger_blocks:
    if "bhadri" in name.lower():
        cb_match = re.search(r'<CLOSINGBALANCE[^>]*>(.*?)</CLOSINGBALANCE>', block)
        cb = cb_match.group(1) if cb_match else "NOT FOUND"
        parent_match = re.search(r'<PARENT[^>]*>(.*?)</PARENT>', block)
        parent = parent_match.group(1) if parent_match else "?"
        print(f"  Name: {name!r}")
        print(f"  Parent: {parent}")
        print(f"  Closing balance: {cb!r}")
        print()
