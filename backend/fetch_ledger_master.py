import requests

TALLY_URL = "http://192.168.0.205:9000"

xml_request = """<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>LedgerMaster</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVCURRENTCOMPANY>SUPREME BALAJI DYE CHEM - 25-26</SVCURRENTCOMPANY>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   </STATICVARIABLES>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="LedgerMaster" ISMODIFY="No">
      <TYPE>Ledger</TYPE>
      <FETCH>NAME, PARENT, ADDRESS, LEDGERPHONE, EMAIL, PARTYGSTIN, CREDITPERIOD, OPENINGBALANCE</FETCH>
     </COLLECTION>
    </TDLMESSAGE>
   </TDL>
  </DESC>
 </BODY>
</ENVELOPE>"""

print("Connecting to Tally...")
response = requests.post(TALLY_URL, data=xml_request.encode("utf-8"), timeout=60)

print(f"Status code: {response.status_code}")
print(f"Response length: {len(response.content)} bytes")

qmark_count = response.content.count(b"?")
pct = qmark_count / max(len(response.content), 1) * 100
print(f"Question mark count: {qmark_count} ({pct:.1f}% of bytes)")

with open("ledger_master.xml", "wb") as f:
    f.write(response.content)

print("Saved to ledger_master.xml")
