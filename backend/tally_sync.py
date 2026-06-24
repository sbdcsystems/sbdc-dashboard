import requests
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TALLY_SERVER_IP = os.getenv("TALLY_SERVER_IP", "localhost")
TALLY_PORT = 9000
TALLY_COMPANY_NAME = os.getenv("TALLY_COMPANY_NAME", "Supreme Balaji Dye Chem")
TALLY_URL = f"http://{TALLY_SERVER_IP}:{TALLY_PORT}"


def test_tally_connection():
    try:
        response = requests.get(TALLY_URL, timeout=5)
        print(f"Tally server reachable at {TALLY_URL}")
        return True
    except Exception:
        print(f"Cannot reach Tally server at {TALLY_URL}")
        return False


def build_request_xml(report_name):
    return f"""<ENVELOPE>
    <HEADER>
        <TALLYREQUEST>Export Data</TALLYREQUEST>
    </HEADER>
    <BODY>
        <EXPORTDATA>
            <REQUESTDESC>
                <REPORTNAME>{report_name}</REPORTNAME>
                <STATICVARIABLES>
                    <SVCURRENTCOMPANY>{TALLY_COMPANY_NAME}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </REQUESTDESC>
        </EXPORTDATA>
    </BODY>
</ENVELOPE>"""


def try_report(report_name):
    xml_request = build_request_xml(report_name)
    headers = {"Content-Type": "text/xml"}
    response = requests.post(TALLY_URL, data=xml_request.encode("utf-8"), headers=headers, timeout=30)
    raw_bytes = response.content
    is_error = all(b == 0x3f for b in raw_bytes[:50]) if len(raw_bytes) > 0 else True
    print(f"Report '{report_name}': status={response.status_code}, size={len(raw_bytes)} bytes, looks_like_error={is_error}")
    return raw_bytes, is_error


def main():
    print("=" * 50)
    print("SUPREME BALAJI -- TALLY REPORT NAME TEST")
    print(f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    if not test_tally_connection():
        return

    report_names_to_try = [
        "Bills Receivable",
        "Bills Payable",
        "Ledger Outstandings",
    ]

    for name in report_names_to_try:
        raw_bytes, is_error = try_report(name)
        if not is_error:
            filename = f"tally_success_{name.replace(' ', '_')}.xml"
            with open(filename, "wb") as f:
                f.write(raw_bytes)
            print(f"  -> SUCCESS! Saved real data to {filename}")
        print()

    print("=" * 50)
    print("Check above -- whichever report shows 'looks_like_error=False'")
    print("is the one that worked. Open that saved file and share it.")
    print("=" * 50)


if __name__ == "__main__":
    main()
