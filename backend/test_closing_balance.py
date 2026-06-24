import requests, re

TALLY_URL = "http://192.168.0.205:9000"

xml_request = """<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>ClosingBalanceTest</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVCURRENTCOMPANY>SUPREME BALAJI DYE CHEM - 25-26</SVCURRENTCOMPANY>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   </STATICVARIABLES>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="ClosingBalanceTest" ISMODIFY="No">
      <TYPE>Ledger</TYPE>
      <FETCH>NAME, PARENT, CLOSINGBALANCE</FETCH>
     </COLLECTION>
    </TDLMESSAGE>
   </TDL>
  </DESC>
 </BODY>
</ENVELOPE>"""

print("Fetching ledger closing balances from Tally...")
response = requests.post(TALLY_URL, data=xml_request.encode("utf-8"), timeout=120)
content = response.content.decode("utf-8", errors="replace")

print(f"Status: {response.status_code}, response length: {len(content)} bytes\n")

# Search specifically for Sri Bhadri Narayana Textiles to validate against the known-correct Rs.6,06,949
idx = content.find("Bhadri")
if idx == -1:
    print("Could not find 'Bhadri' in the response. First 1500 chars of raw response:")
    print(content[:1500])
else:
    print("Found Bhadri record. Surrounding XML:")
    print(content[max(0, idx-300):idx+500])
