"""
compare_step3.py - standalone, READ-ONLY comparison of the OLD vs NEW
Step 3 (Bills Receivable parsing) credit/due sign logic.

OLD logic (as currently committed in tally_sync_runner.py): sign decided
purely by BILLVCHTYPE membership in {"Payment", "Receipt"}.

NEW logic (agreed, not yet committed to the runner): sign decided primarily
by BILLVCHAMOUNT's own sign (negative = Debit = due, positive = Credit = an
on-account credit - confirmed live: GST SALES is always negative, Receipt is
always positive). Falls back to BILLVCHTYPE membership in {"Receipt", "Cash
Receipt", "PoS Receipt", "Credit Note"} only when BILLVCHAMOUNT is missing,
zero, or unparseable. Does NOT include "Payment" - investigation found
"Payment" bills (e.g. staff/vendor advances like "Gowtham - TA Expense")
carry the same negative BILLVCHAMOUNT sign as a genuine due, not the same
sign as Receipt.

Deliberately self-contained: does NOT import tally_sync_runner.py, so
running this can never trigger that module's import-time side effects
(creating backend/logs/, writing a log file, loading .env) or its Supabase/
Tally-calling code paths. Makes zero network calls and writes nothing to
disk - it only reads the XML file given on the command line and prints a
report to stdout.

Usage:
    python compare_step3.py path/to/bills_receivable.xml
"""

import argparse
import html
import re
import sys
from collections import defaultdict
from pathlib import Path

_BILL_RE = re.compile(
    r"<BILLFIXED>\s*"
    r"<BILLDATE>(.*?)</BILLDATE>\s*"
    r"<BILLREF>(.*?)</BILLREF>\s*"
    r"<BILLPARTY>(.*?)</BILLPARTY>\s*"
    r"</BILLFIXED>\s*"
    r"<BILLCL>(.*?)</BILLCL>\s*"
    r"<BILLDUE>(.*?)</BILLDUE>\s*"
    r"<BILLOVERDUE>(.*?)</BILLOVERDUE>\s*"
    r"<BILLVCHDATE>.*?</BILLVCHDATE>\s*"
    r"<BILLVCHTYPE>(.*?)</BILLVCHTYPE>\s*"
    r"<BILLVCHNUMBER>(.*?)</BILLVCHNUMBER>\s*"
    r"<BILLVCHAMOUNT>(.*?)</BILLVCHAMOUNT>",
    re.DOTALL,
)

OLD_CREDIT_VCH_TYPES = {"Payment", "Receipt"}
NEW_CREDIT_VCH_TYPES = {"Receipt", "Cash Receipt", "PoS Receipt", "Credit Note"}


def _parse_raw_bills(xml_text: str) -> list:
    """Extract raw (unsigned) bill fields once, shared by both sign logics."""
    bills = []
    for date_raw, ref, party, cl_raw, due_raw, overdue_raw, vch_type, vch_number, vchamt_raw in _BILL_RE.findall(xml_text):
        try:
            raw_amt = abs(float(cl_raw.strip()))
        except ValueError:
            raw_amt = 0.0
        try:
            vchamt = float(vchamt_raw.strip())
        except ValueError:
            vchamt = None
        bills.append({
            "customer_name": html.unescape(party.strip()),
            "invoice_ref":   ref.strip(),
            "vch_type":      vch_type.strip(),
            "vch_number":    vch_number.strip(),
            "raw_amt":       raw_amt,
            "vchamt":        vchamt,
        })
    return bills


def _old_sign(bill: dict) -> "tuple[float, bool]":
    is_credit = bill["vch_type"] in OLD_CREDIT_VCH_TYPES
    amount = -bill["raw_amt"] if is_credit else bill["raw_amt"]
    return amount, is_credit


def _new_sign(bill: dict) -> "tuple[float, bool, str]":
    if bill["vchamt"] is not None and bill["vchamt"] != 0:
        is_credit = bill["vchamt"] > 0
        source = "sign"
    else:
        is_credit = bill["vch_type"] in NEW_CREDIT_VCH_TYPES
        source = "type_fallback"
    amount = -bill["raw_amt"] if is_credit else bill["raw_amt"]
    return amount, is_credit, source


def main():
    parser = argparse.ArgumentParser(
        description="Compare OLD vs NEW Step 3 credit/due sign logic on a Bills Receivable XML. "
                     "Read-only: no Supabase writes, no Tally calls, no files written."
    )
    parser.add_argument("xml_path", help="Path to a Bills Receivable XML export (e.g. tally_outstanding_*.xml)")
    args = parser.parse_args()

    xml_path = Path(args.xml_path)
    if not xml_path.exists():
        print(f"File not found: {xml_path}", file=sys.stderr)
        sys.exit(1)

    xml_text = xml_path.read_text(encoding="utf-8", errors="replace")
    bills = _parse_raw_bills(xml_text)
    if not bills:
        print("No bill entries found - is this a Bills Receivable XML export (BILLFIXED/BILLCL/.../BILLVCHAMOUNT)?", file=sys.stderr)
        sys.exit(1)

    print(f"Parsed {len(bills)} bill entries from {xml_path}\n")

    by_cust_old = defaultdict(float)
    by_cust_new = defaultdict(float)
    by_type_old = defaultdict(lambda: {"count": 0, "total": 0.0})
    by_type_new = defaultdict(lambda: {"count": 0, "total": 0.0})
    old_amounts = []
    new_amounts = []
    fallback_count = 0
    flips = []

    for b in bills:
        old_amt, old_is_credit = _old_sign(b)
        new_amt, new_is_credit, source = _new_sign(b)

        old_amounts.append(old_amt)
        new_amounts.append(new_amt)
        if source == "type_fallback":
            fallback_count += 1

        by_cust_old[b["customer_name"]] += old_amt
        by_cust_new[b["customer_name"]] += new_amt

        by_type_old[b["vch_type"]]["count"] += 1
        by_type_old[b["vch_type"]]["total"] += old_amt
        by_type_new[b["vch_type"]]["count"] += 1
        by_type_new[b["vch_type"]]["total"] += new_amt

        if old_is_credit != new_is_credit:
            flips.append({
                "customer_name": b["customer_name"],
                "invoice_ref":   b["invoice_ref"],
                "vch_type":      b["vch_type"],
                "vch_number":    b["vch_number"],
                "old_amount":    old_amt,
                "new_amount":    new_amt,
                "sign_source":   source,
            })

    old_total = sum(old_amounts)
    new_total = sum(new_amounts)
    old_due_total    = sum(a for a in old_amounts if a > 0)
    new_due_total    = sum(a for a in new_amounts if a > 0)
    old_credit_total = sum(a for a in old_amounts if a < 0)
    new_credit_total = sum(a for a in new_amounts if a < 0)
    old_credit_count = sum(1 for a in old_amounts if a < 0)
    new_credit_count = sum(1 for a in new_amounts if a < 0)

    print("=" * 78)
    print("TOTAL OUTSTANDING (net = due + credit)")
    print(f"  Before (old logic): Rs {old_total:,.2f}")
    print(f"  After  (new logic): Rs {new_total:,.2f}")
    print(f"  Change:             Rs {new_total - old_total:,.2f}")
    print()
    print("  Due only    - before: Rs {:,.2f}   after: Rs {:,.2f}".format(old_due_total, new_due_total))
    print()
    print("ON-ACCOUNT CREDIT TOTAL")
    print(f"  Before: {old_credit_count} entries, Rs {old_credit_total:,.2f}")
    print(f"  After:  {new_credit_count} entries, Rs {new_credit_total:,.2f}")
    print()
    print(f"  {fallback_count} / {len(bills)} entries fell back to the voucher-type list "
          "(BILLVCHAMOUNT missing/zero/unparseable) under the new logic.")

    print()
    print("=" * 78)
    print("PER-VOUCHER-TYPE COUNTS AND TOTALS")
    all_types = sorted(
        set(by_type_old) | set(by_type_new),
        key=lambda t: -(by_type_old[t]["count"] + by_type_new[t]["count"]),
    )
    print(f"{'Voucher Type':<20} {'Count':>7} {'Before Total':>18} {'After Total':>18} {'Change':>15}")
    for t in all_types:
        old_d = by_type_old[t]
        new_d = by_type_new[t]
        print(f"{t:<20} {old_d['count']:>7} {old_d['total']:>18,.2f} {new_d['total']:>18,.2f} {new_d['total'] - old_d['total']:>15,.2f}")

    print()
    print("=" * 78)
    all_names = set(by_cust_old) | set(by_cust_new)
    diffs = []
    for name in all_names:
        before = by_cust_old.get(name, 0.0)
        after  = by_cust_new.get(name, 0.0)
        delta  = after - before
        if abs(delta) > 0.01:
            diffs.append((name, before, after, delta))
    diffs.sort(key=lambda x: -abs(x[3]))
    print(f"{len(diffs)} customer(s) with a changed balance. Top 15 by |change|:\n")
    print(f"{'Customer':<45} {'Before':>15} {'After':>15} {'Change':>15}")
    for name, before, after, delta in diffs[:15]:
        print(f"{name[:45]:<45} {before:>15,.2f} {after:>15,.2f} {delta:>15,.2f}")

    print()
    print("=" * 78)
    print(f"EVERY ENTRY WHOSE SIGN FLIPS ({len(flips)} total)")
    if flips:
        print(f"{'Customer':<38} {'Bill Ref':<18} {'Vch Type':<14} {'Vch #':<8} {'Before':>13} {'After':>13} {'Sign source':<14}")
        for f in flips:
            print(
                f"{f['customer_name'][:38]:<38} {f['invoice_ref'][:18]:<18} {f['vch_type'][:14]:<14} "
                f"{f['vch_number'][:8]:<8} {f['old_amount']:>13,.2f} {f['new_amount']:>13,.2f} {f['sign_source']:<14}"
            )
    else:
        print("  (none)")


if __name__ == "__main__":
    main()
