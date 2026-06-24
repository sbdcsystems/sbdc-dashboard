import requests
import os
from dotenv import load_dotenv

load_dotenv()
TALLY_SERVER_IP = os.getenv("TALLY_SERVER_IP", "localhost")
TALLY_URL = f"http://{TALLY_SERVER_IP}:9000"

xml_request = """<ENVELOPE>
    <HEADER>
        <TALLYREQUEST>Export Data</TALLYREQUEST>
    </HEADER>
    <BODY>
        <EXPORTDATA>
            <REQUESTDESC>
                <REPORTNAME>Bills Receivable</REPORTNAME>
            </REQUESTDESC>
        </EXPORTDATA>
    </BODY>
</ENVELOPE>"""

headers = {"Content-Type": "text/xml"}
response = requests.post(TALLY_URL, data=xml_request.encode("utf-8"), headers=headers, timeout=30)
raw = response.content
qmarks = raw.count(b"?")
print(f"Status: {response.status_code}, Size: {len(raw)} bytes, question marks: {qmarks}")

if qmarks < len(raw) * 0.5:
    with open("tally_no_company_var.xml", "wb") as f:
        f.write(raw)
    print("Looks like real data! Saved to tally_no_company_var.xml")
else:
    print("Still mostly question marks")
