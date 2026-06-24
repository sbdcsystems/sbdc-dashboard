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
                <REPORTNAME>List of Accounts</REPORTNAME>
            </REQUESTDESC>
        </EXPORTDATA>
    </BODY>
</ENVELOPE>"""

headers = {"Content-Type": "text/xml"}
response = requests.post(TALLY_URL, data=xml_request.encode("utf-8"), headers=headers, timeout=30)
raw = response.content
qmarks = raw.count(b"?")
print(f"Status: {response.status_code}, Size: {len(raw)} bytes, question marks: {qmarks}")

with open("tally_list_of_accounts.xml", "wb") as f:
    f.write(raw)
print("Saved regardless, for inspection")
