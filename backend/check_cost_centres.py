import requests

TALLY_URL = "http://192.168.0.205:9000"

xml_request = """<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>CostCentreCheck</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVCURRENTCOMPANY>SUPREME BALAJI DYE CHEM - 25-26</SVCURRENTCOMPANY>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   </STATICVARIABLES>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="CostCentreCheck" ISMODIFY="No">
      <TYPE>Cost Centre</TYPE>
      <FETCH>NAME, PARENT, CATEGORY</FETCH>
     </COLLECTION>
    </TDLMESSAGE>
   </TDL>
  </DESC>
 </BODY>
</ENVELOPE>"""

print("Checking Tally for Cost Centres...")
response = requests.post(TALLY_URL, data=xml_request.encode("utf-8"), timeout=60)

print(f"Status code: {response.status_code}")
print(f"Response length: {len(response.content)} bytes")
print()
print(response.content.decode("utf-8", errors="replace")[:3000])
