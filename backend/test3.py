import requests, os
from dotenv import load_dotenv
load_dotenv()
TALLY_URL = f"http://{os.getenv('TALLY_SERVER_IP')}:9000"

xml = """<ENVELOPE>
<HEADER><TALLYREQUEST>EXPORT</TALLYREQUEST></HEADER>
<BODY><EXPORTDATA><REQUESTDESC><STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
</STATICVARIABLES><REPORTNAME>List of Accounts</REPORTNAME></REQUESTDESC></EXPORTDATA></BODY>
</ENVELOPE>"""

r = requests.post(TALLY_URL, data=xml.encode("utf-8"), headers={"Content-Type":"text/xml"}, timeout=30)
raw = r.content
q = raw.count(b"?")
print(f"Size: {len(raw)}, qmarks: {q}")
with open("tally_xml_format_test.xml","wb") as f:
    f.write(raw)
