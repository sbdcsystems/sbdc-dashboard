import requests, os
from dotenv import load_dotenv
load_dotenv()
TALLY_COMPANY = os.getenv("TALLY_COMPANY_NAME")
TALLY_URL = "http://" + os.getenv("TALLY_SERVER_IP") + ":9000"

xml = "<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER><BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Bills Receivable</REPORTNAME><STATICVARIABLES><SVCURRENTCOMPANY>" + TALLY_COMPANY + "</SVCURRENTCOMPANY></STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"

headers = {"Content-Type": "text/xml"}
r = requests.post(TALLY_URL, data=xml.encode("utf-8"), headers=headers, timeout=30)
raw = r.content
q = raw.count(b"?")
print("Status:", r.status_code, "Size:", len(raw), "bytes, question marks:", q)
with open("tally_retest_after_printer_fix.xml","wb") as f:
    f.write(raw)
print("Saved for inspection")
