"""
tally_sync_runner.py — automated Tally -> Supabase outstanding sync.

Modes:
  python tally_sync_runner.py                                            # full sync (requires office network)
  python tally_sync_runner.py --from-local                              # skip Tally fetch, use existing tally_with_dates.xml
  python tally_sync_runner.py --from-local --dry-run                    # parse + map only, no DB changes
  python tally_sync_runner.py --backfill --from 2026-04-01 --to 2026-04-30   # backfill daily_sales + collections
  python tally_sync_runner.py --backfill --from 2026-04-01 --to 2026-04-30 --force  # overwrite existing rows
  python tally_sync_runner.py --backfill --from 2026-04-01 --to 2026-04-30 --dry-run  # preview without writes

Safety guarantees:
  - Old data is only deleted AFTER new data is fully inserted (insert-first pattern).
    If the insert fails partway, the table retains the previous sync's data intact.
  - A sanity check aborts the whole run before touching the DB if the freshly fetched
    bill count has dropped more than 50% vs the current DB count.
  - On failure: writes last_sync_status.json and sends an email if SMTP is configured.

Logs: backend/logs/sync_YYYYMMDD_HHMMSS.log
Email config (optional, add to root .env):
  SMTP_FROM=your.gmail@gmail.com
  SMTP_PASSWORD=xxxx xxxx xxxx xxxx   # Gmail App Password (not your login password)
  NOTIFY_EMAIL=number.to.notify@gmail.com
"""

import argparse
import os
import json
import re
import html
import logging
import smtplib
import sys
import time
from datetime import datetime, timedelta, date, UTC
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
import requests
from supabase import create_client

# ── Paths & run identity ───────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
LOG_DIR  = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

RUN_TS         = datetime.now().strftime("%Y%m%d_%H%M%S")
SYNC_TIMESTAMP = datetime.now(UTC).isoformat()   # stored in synced_from_tally_at column
log_path       = LOG_DIR / f"sync_{RUN_TS}.log"

# Reconfigure stdout/stderr to real UTF-8 before logging is set up. Task
# Scheduler runs this with stdout redirected to a file using the OS default
# codepage (not UTF-8 on this Windows box), which silently mangles the em
# dashes (—) used throughout these log messages into garbage bytes. The log
# FILE handler below already forces UTF-8; this makes the console/redirected
# stream match it.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass  # non-interactive stream or a Python old enough to lack reconfigure()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(log_path, encoding="utf-8", errors="replace"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

load_dotenv(BASE_DIR.parent / ".env", override=True)

TALLY_IP      = os.environ.get("TALLY_SERVER_IP", "192.168.0.205")
TALLY_PORT    = int(os.environ.get("TALLY_PORT", "9000"))
TALLY_URL     = f"http://{TALLY_IP}:{TALLY_PORT}"
TALLY_COMPANY = os.environ.get("TALLY_COMPANY_NAME", "SUPREME BALAJI DYE CHEM - 25-26")

# Timeouts, sized to what each request actually asks Tally to do. The billing
# PC is not powerful and must stay responsive for staff during office hours.
TALLY_CHECK_TIMEOUT      = 5    # Step 1 — just a reachability ping
TALLY_TIMEOUT             = 60   # Step 2 — Bills Receivable, full FY, EXPLODEFLAG
TALLY_LEDGER_TIMEOUT      = 90   # Step 4.5/4.6 — full ledger master, no date scope
TALLY_COLLECTION_TIMEOUT  = 90   # Step 9 / 9b / 10-chunk / reconcile — TDL <COLLECTION> voucher fetches

# Pause after every Tally request so a run of several requests in a row
# doesn't monopolise Tally while staff are billing on the same PC.
PAUSE_BETWEEN_REQUESTS = 2.0  # seconds

# Confirmed live against Tally on 24-Sep-2026 (office PC). Sales voucher types
# are "GST SALES" and "CC SALES" — "Sales Order" is a separate type and was
# never included (excluded via the SO- voucher-number prefix check anyway).
SALES_VOUCHER_TYPES  = ("GST SALES", "CC SALES")
# Confirmed live 24-Sep-2026 (a follow-up test, on a day with real receipts):
# collections come in as three distinct exact voucher types, not one.
RECEIPT_VOUCHER_TYPES = ("Receipt", "PoS Receipt", "Cash Receipt")

RECENT_MONTHS       = 12    # bills older than this -> age_status "stale"
XML_KEEP_DAYS       = 7     # delete XML backups older than this
SUPABASE_BATCH      = 200   # records per insert call
SANITY_DROP_LIMIT   = 0.50  # abort if new bill count < 50% of current DB count

# ── Load-reduction throttles ─────────────────────────────────────────────────────
# 25-Sep-2026: office reported Tally lagging on every connected PC — each
# scheduled run's heavy steps kept the shared billing PC busy 80-120s every
# 30 minutes. Steps 1/9/9b stay light and run every time (small, filtered
# requests already); everything below now reuses previous data instead of
# refetching it, gated by how long it's actually been since it last ran.
OUTSTANDING_THROTTLE_HOURS        = 3     # Step 2/3 (Bills Receivable) — only if older than this
LEDGER_MASTER_THROTTLE_HOURS      = 24    # Step 4.5/4.6 (ledger master) — at most once a day
SALES_HISTORY_FULL_SWEEP_DAYS     = 7     # Step 10 — full FY + deletion reconciliation, at most weekly
SALES_HISTORY_DELETE_SANITY_LIMIT = 0.20  # abort deletion reconciliation if it would remove > this fraction of tracked vouchers

# Step 10 fallback cadence — used only while ALTERID_SYNC_ENABLED is False.
FALLBACK_CURRENT_MONTH_HOURS = 2     # current month — at most this often
FALLBACK_OLDER_MONTH_HOURS   = 24    # one rotating older month — at most this often

# Tally's AlterID increases company-wide whenever any object is created or
# edited — filtering by it is the standard way to fetch only what changed
# since last time, including edits/cancellations to old invoices, with no
# date bound needed at all. Confirmed live on this Tally install: date
# filters via <FILTER>/<SYSTEM TYPE="Formulae"> (see CLAUDE.md). AlterID
# filtering itself is NOT yet confirmed — flip this to True only after
# running backend/probe_alterid.py on the office PC and confirming it
# returns sane results. Until then, Step 10 uses the month-based fallback.
ALTERID_SYNC_ENABLED = False

# ── Phase 1 incremental sales_history state ────────────────────────────────────
STATE_FILE            = BASE_DIR / "sync_state.json"
NEW_MONTH_CATCHUP_RUNS = 6   # re-fetch previous month for this many runs after a month rolls over

# ── Phase 2 overlap lock ────────────────────────────────────────────────────────
LOCK_FILE           = BASE_DIR / "sync.lock"
LOCK_STALE_MINUTES  = 20   # office PC can be switched off mid-run; don't let a dead lock block forever

# ── Shared Tally request helper ─────────────────────────────────────────────────

def _tally_post(xml_body: str, timeout: int, pause_after: float = PAUSE_BETWEEN_REQUESTS) -> bytes:
    """
    POST one TDL/export request to Tally and pause briefly afterward so a run
    making several requests in a row doesn't monopolise the billing PC.
    Returns the raw response bytes. Raises on a request-level failure
    (timeout, connection error) — callers decide whether that's fatal.
    """
    try:
        r = requests.post(
            TALLY_URL, data=xml_body.encode("utf-8"),
            headers={"Content-Type": "text/xml"}, timeout=timeout,
        )
        return r.content
    finally:
        if pause_after:
            time.sleep(pause_after)


# ── Server-side TDL filtering ────────────────────────────────────────────────────
#
# Confirmed live against Tally, two rounds:
#   24-Sep-2026: a <COLLECTION> with a <FILTER> referencing a
#     <SYSTEM TYPE="Formulae"> object filters server-side and is dramatically
#     cheaper — 52 vouchers in 8.1s vs ~14,000+ vouchers and ~60s for the same
#     request with no filter. Prior versions of this script fetched the whole
#     Voucher collection every time and filtered entirely in Python because an
#     earlier attempt only tried the (ineffective) SVFROMDATE/SVTODATE report
#     variables, not a real TDL FILTER.
#   Follow-up: the date-RANGE form ($Date >= ... AND $Date <= ...) also works
#     server-side, with <= escaped as &lt;= per _xml_escape_formula below — a
#     17–23 Sep range returned exactly those 578 vouchers in 12.8s.
# So both the single-date-equality and date-range forms are now confirmed.
# The combined AND-with-voucher-type formulas are still this codebase's
# extrapolation from those two confirmations, not separately verified. The
# existing Python-side date/type/cancelled checks are kept as a safety net
# regardless — if a formula is subtly wrong, correctness still holds, only
# the performance win might not. The "server response" log lines added
# throughout this module are what confirmed the above and remain useful for
# catching any future filter regression.

def _tally_date_literal(d: date) -> str:
    """Tally TDL date-literal format, e.g. 24-Sep-2026 — matches $$Date:"..." usage."""
    return d.strftime("%d-%b-%Y")


def _xml_escape_formula(formula: str) -> str:
    """
    Escape a TDL formula for embedding as XML element text. Only <, >, and &
    need it — the confirmed-working live example embedded raw double quotes
    unescaped inside the <SYSTEM> element, so quotes are left as-is to match
    that exactly rather than guessing an escaping style Tally wasn't shown to
    accept.
    """
    return formula.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_voucher_collection_request(collection_id: str, fetch_fields: str, filter_formula: str) -> str:
    """Build a TDL Collection request with a server-side FILTER formula."""
    filter_name = f"{collection_id}Filter"
    return (
        "<ENVELOPE>"
        "<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        f"<TYPE>Collection</TYPE><ID>{collection_id}</ID></HEADER>"
        "<BODY><DESC>"
        "<STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        "</STATICVARIABLES>"
        "<TDL><TDLMESSAGE>"
        f'<COLLECTION NAME="{collection_id}" ISMODIFY="No">'
        "<TYPE>Voucher</TYPE>"
        f"<FETCH>{fetch_fields}</FETCH>"
        f"<FILTER>{filter_name}</FILTER>"
        "</COLLECTION>"
        f'<SYSTEM TYPE="Formulae" NAME="{filter_name}">{_xml_escape_formula(filter_formula)}</SYSTEM>'
        "</TDLMESSAGE></TDL>"
        "</DESC></BODY>"
        "</ENVELOPE>"
    )


def _raise_on_tally_error(xml: str, context: str):
    """Tally reports a bad TDL request (e.g. a formula syntax error) as a <LINEERROR> in an otherwise well-formed response — that would otherwise look like a silent, legitimate zero."""
    err_m = re.search(r"<LINEERROR>(.*?)</LINEERROR>", xml, re.DOTALL)
    if err_m:
        raise RuntimeError(f"Tally reported a TDL error for {context}: {html.unescape(err_m.group(1).strip())}")


def _parse_tally_date(raw: str) -> "date | None":
    raw = raw.strip()
    for fmt in ("%Y%m%d", "%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _is_voucher_cancelled(voucher_xml: str) -> bool:
    m = re.search(r"<ISCANCELLED[^>]*>(.*?)</ISCANCELLED>", voucher_xml, re.IGNORECASE)
    return bool(m and m.group(1).strip().lower() == "yes")


def _log_voucher_date_span(context: str, raw_count: int, dates_seen: list):
    """
    Diagnostic for verifying the new server-side date filters actually work —
    if a request scoped to one month comes back with dates spanning the whole
    FY, the filter silently didn't apply and Tally returned everything.
    """
    if dates_seen:
        log.info(
            "  %s server response: %d raw voucher(s), dates seen %s to %s",
            context, raw_count, min(dates_seen).isoformat(), max(dates_seen).isoformat(),
        )
    else:
        log.info("  %s server response: %d raw voucher(s), no parseable dates", context, raw_count)


# ── Local run-state (sales_history rotation) ────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("  Could not parse %s (%s) — starting with fresh state", STATE_FILE.name, exc)
    return {}


def _save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ── Throttle helpers (load-reduction) ─────────────────────────────────────────

def _hours_since(iso_str: "str | None") -> "float | None":
    """
    Hours elapsed since an ISO timestamp. Returns None if iso_str is falsy or
    unparseable — callers treat that as "infinitely overdue" (never run yet,
    or the record is corrupt, both mean "go ahead and run it"). A naive
    (offset-less) timestamp is treated as UTC rather than rejected, since
    RUN_TS-style local strings can still show up in older state.
    """
    if not iso_str:
        return None
    try:
        then = datetime.fromisoformat(iso_str)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return (datetime.now(UTC) - then).total_seconds() / 3600


def _is_due(iso_str: "str | None", hours: float) -> bool:
    """True if iso_str is missing/unparseable, or at least `hours` old."""
    elapsed = _hours_since(iso_str)
    return elapsed is None or elapsed >= hours


def _load_last_status() -> dict:
    """Read last_sync_status.json — used both to write it (carry last_success
    forward) and to decide whether a throttled step is due."""
    status_path = BASE_DIR / "last_sync_status.json"
    if status_path.exists():
        try:
            return json.loads(status_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("  Could not parse %s (%s)", status_path.name, exc)
    return {}


# ── Overlap lock ─────────────────────────────────────────────────────────────────

def _pid_running(pid: int) -> bool:
    """Best-effort check for whether a PID is still alive, on Windows or POSIX."""
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def acquire_lock() -> bool:
    """
    Refuse to start a second run while one is already in progress. A lock is
    treated as stale (safe to remove) if its PID is no longer running OR it
    is older than LOCK_STALE_MINUTES — the office PC is switched off at 7 PM
    and can be shut down mid-run, which would otherwise leave a lock nothing
    can ever clear.
    """
    if LOCK_FILE.exists():
        lock_pid, lock_started = None, None
        try:
            info         = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
            lock_pid     = info.get("pid")
            lock_started = datetime.fromisoformat(info.get("started_at"))
        except Exception as exc:
            log.warning("  Lock file unreadable (%s) — treating as stale", exc)

        age_minutes = (
            (datetime.now() - lock_started).total_seconds() / 60
            if lock_started is not None else None
        )
        pid_alive = lock_pid is not None and _pid_running(lock_pid)
        stale = (
            lock_started is None
            or not pid_alive
            or age_minutes > LOCK_STALE_MINUTES
        )

        if not stale:
            log.warning(
                "  Another sync is already running (PID %s, started %s, %.0f min ago) — exiting cleanly",
                lock_pid, lock_started.isoformat() if lock_started else "?", age_minutes or 0,
            )
            return False

        log.warning(
            "  Removing stale lock (pid=%s alive=%s age=%s min) and continuing",
            lock_pid, pid_alive, f"{age_minutes:.0f}" if age_minutes is not None else "?",
        )
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass

    LOCK_FILE.write_text(
        json.dumps({"pid": os.getpid(), "started_at": datetime.now().isoformat()}),
        encoding="utf-8",
    )
    return True


def release_lock():
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except OSError as exc:
        log.warning("  Could not remove lock file: %s", exc)


# ── Group → staff mapping (mirrors full_customer_import.py) ───────────────────

_STAFF_GROUPS = {
    "1.Venkatesh - Parties":     "Venkatesh",
    "Bill Wise - J.Venkatesh":   "Venkatesh",
    "2.Thiagarajan - Parties":   "Thiagarajan",
    "Bill Wise - G.Thiagarajan": "Thiagarajan",
    "3.Gowtham - Parties":       "Gowtham",
    "Bill Wise - S.Gowtham":     "Gowtham",
    "7.Levaset - Parties":       "Vijaya Priya",
    "8.Vetri-Parties":           "Vijaya Priya",
    "9.Vijayapriya - Parties":   "Vijaya Priya",
    "Kanagaraj - Parties":       "Vijaya Priya",
}
_CASH_GROUP          = "4.Cash - Parties"
_BAD_DEBT_CURRENT    = "5.Bad Debtors 24-25"
_CASE_FILED          = "6.Case Filed Customers"
_BAD_DEBT_HISTORICAL = "Bad Debts Written Off"


def _fy_start() -> date:
    today = date.today()
    return date(today.year, 4, 1) if today.month >= 4 else date(today.year - 1, 4, 1)


def _extract_phone(text: str) -> str | None:
    if not text:
        return None
    for segment in text.split(","):
        digits = "".join(ch for ch in segment if ch.isdigit())
        if len(digits) == 10 and digits[0] in "6789":
            return digits
        if len(digits) in (10, 11) and digits[0] == "0":
            return digits
    return None


# ── Status file ────────────────────────────────────────────────────────────────

STATUS_STEPS = ("outstanding", "today_sales", "collections", "sales_history")


def _write_status(status: str, steps: dict, detail: "dict | None" = None, dry_run: bool = False):
    """
    Write last_sync_status.json AND mirror the same record to the Supabase
    sync_status table, so a run that half-fails is visible as "partial" rather
    than the old behaviour of always logging SYNC COMPLETE / status "success"
    regardless of what actually happened.

    steps: {"outstanding": "success"|"failed"|"skipped", "today_sales": ...,
            "collections": ..., "sales_history": ...} — see STATUS_STEPS.
    Carries forward each step's last successful timestamp from the previous
    run so the dashboard can show e.g. "collections last synced 40 min ago"
    even on a run where that step failed or didn't run.
    """
    detail = detail or {}

    status_path = BASE_DIR / "last_sync_status.json"
    prev = _load_last_status()

    # BUG FIX: this used to be datetime.now().isoformat() — a naive local
    # (IST, on the office PC) timestamp with no offset in the string.
    # Postgres has no way to know that "18:31:28" meant IST, so a timestamptz
    # column stored it as 18:31:28 UTC — 5.5 hours off from the real instant.
    # `now_local_iso` (naive) is kept only for last_sync_status.json's own
    # "timestamp" field, which is a local log-style value nobody but a human
    # reading that file locally ever looks at (same convention as RUN_TS).
    # Everything that round-trips through Supabase — run_at and every
    # last_success_* column, all `timestamptz` — must use the aware
    # `now_utc_iso` instead.
    now_utc_iso   = datetime.now(UTC).isoformat()
    now_local_iso = datetime.now().isoformat()
    last_success  = dict(prev.get("last_success", {}))
    for step in STATUS_STEPS:
        # dry_run guard: a dry run doesn't actually write anything to that
        # step's target table, so it must never count as a "last success" —
        # that value now also drives the Step 2/3 throttle in main(), and a
        # dry-run test would otherwise silently suppress the next real
        # outstanding sync for OUTSTANDING_THROTTLE_HOURS.
        if steps.get(step) == "success" and not dry_run:
            last_success[step] = now_utc_iso

    payload = {
        "status":       status,
        "run_ts":       RUN_TS,
        "timestamp":    now_local_iso,
        "steps":        steps,
        "last_success": last_success,
        **detail,
    }
    status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if dry_run:
        log.info("  DRY RUN — sync_status Supabase mirror skipped")
        return payload

    # Best-effort mirror to Supabase. Table: sync_status (see CLAUDE.md schema —
    # requires GRANT SELECT ON sync_status TO anon + an RLS policy, same as any
    # new table). Never let this fail the sync itself.
    try:
        supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
        supa.table("sync_status").insert({
            "run_at":                     now_utc_iso,
            "status":                     status,
            "steps":                      steps,
            "last_success_outstanding":   last_success.get("outstanding"),
            "last_success_today_sales":   last_success.get("today_sales"),
            "last_success_collections":   last_success.get("collections"),
            "last_success_sales_history": last_success.get("sales_history"),
            "detail":                     detail,
        }).execute()
    except Exception as exc:
        log.warning("  Could not write sync_status to Supabase (non-fatal): %s", exc)

    return payload


# ── Email notification ─────────────────────────────────────────────────────────

def _send_skip_alert_email(unmatched: dict):
    """
    Send an alert when bills are skipped due to unknown customer names.
    Silently skipped if SMTP not configured.
    """
    smtp_from = os.environ.get("SMTP_FROM")
    smtp_pass = os.environ.get("SMTP_PASSWORD")
    notify_to = os.environ.get("NOTIFY_EMAIL")

    if not all([smtp_from, smtp_pass, notify_to]):
        log.info("  Skip-alert email skipped (SMTP not configured in .env)")
        return

    lines = "\n".join(
        f"  {count} bill(s) — {name}"
        for name, count in sorted(unmatched.items(), key=lambda x: -x[1])
    )
    body = (
        f"The SBDC Tally sync completed at {datetime.now().strftime('%Y-%m-%d %H:%M')} "
        f"but {len(unmatched)} customer name(s) from Tally were not found in the database.\n\n"
        f"Skipped customers:\n{lines}\n\n"
        "Action: add these customers to Supabase so their bills are included in future syncs.\n\n"
        "The sync itself succeeded — all other bills loaded correctly.\n"
    )
    msg = MIMEText(body)
    msg["Subject"] = (
        f"SBDC Sync Warning — {len(unmatched)} customer(s) skipped "
        f"[{datetime.now().strftime('%d %b %Y')}]"
    )
    msg["From"] = smtp_from
    msg["To"]   = notify_to

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(smtp_from, smtp_pass)
            smtp.sendmail(smtp_from, notify_to, msg.as_string())
        log.info("  Skip alert sent to %s", notify_to)
    except Exception as exc:
        log.warning("  Could not send skip alert email: %s", exc)


def _send_failure_email(error_summary: str):
    """
    Send a failure notification email via Gmail SMTP.
    Silently skipped if SMTP_FROM / SMTP_PASSWORD / NOTIFY_EMAIL are not in .env.
    """
    smtp_from  = os.environ.get("SMTP_FROM")
    smtp_pass  = os.environ.get("SMTP_PASSWORD")
    notify_to  = os.environ.get("NOTIFY_EMAIL")

    if not all([smtp_from, smtp_pass, notify_to]):
        log.info("  Email notification skipped (SMTP not configured in .env)")
        return

    body = (
        f"The SBDC Tally sync FAILED at {datetime.now().strftime('%Y-%m-%d %H:%M')}.\n\n"
        f"Error:\n{error_summary}\n\n"
        f"Log file:\n{log_path}\n\n"
        "Check the log file and run the sync manually once fixed:\n"
        "  cd C:\\Users\\vsome\\Desktop\\sbdc-system\\backend\n"
        "  ..\\venv\\Scripts\\activate.bat\n"
        "  python tally_sync_runner.py\n"
    )
    msg = MIMEText(body)
    msg["Subject"] = f"SBDC Tally Sync FAILED — {datetime.now().strftime('%d %b %Y %H:%M')}"
    msg["From"]    = smtp_from
    msg["To"]      = notify_to

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(smtp_from, smtp_pass)
            smtp.sendmail(smtp_from, notify_to, msg.as_string())
        log.info("  Failure notification sent to %s", notify_to)
    except Exception as exc:
        log.warning("  Could not send failure email: %s", exc)


# ── Step 4.5: Auto-insert new customers from Tally ledger master ──────────────

def _fetch_tally_ledger_master() -> dict:
    """
    Pull every ledger record from Tally and return
    {name.lower(): {name, parent, phone, address, gstin}}.
    Used to resolve new customer names found in Bills Receivable.
    """
    xml_body = (
        "<ENVELOPE>"
        "<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        "<TYPE>Collection</TYPE><ID>AllLedgers</ID>"
        "</HEADER>"
        "<BODY><DESC>"
        "<STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        "</STATICVARIABLES>"
        "<TDL><TDLMESSAGE>"
        '<COLLECTION NAME="AllLedgers" ISMODIFY="No">'
        "<TYPE>Ledger</TYPE>"
        "<FETCH>NAME,PARENT,LEDGERPHONE,ADDRESS,PARTYGSTIN</FETCH>"
        "</COLLECTION>"
        "</TDLMESSAGE></TDL>"
        "</DESC></BODY>"
        "</ENVELOPE>"
    )
    raw = _tally_post(xml_body, timeout=TALLY_LEDGER_TIMEOUT)
    xml = raw.decode("utf-8", errors="replace")

    result = {}
    for raw_name, block in re.findall(
        r'<LEDGER NAME="(.*?)"[^>]*>(.*?)</LEDGER>', xml, re.DOTALL
    ):
        name     = html.unescape(raw_name.strip())
        parent_m = re.search(r"<PARENT\b[^>]*>(.*?)</PARENT>", block)
        parent   = html.unescape(parent_m.group(1).strip()) if parent_m else None

        phone    = None
        phone_m  = re.search(r"<LEDGERPHONE\b[^>]*>(.*?)</LEDGERPHONE>", block)
        if phone_m:
            phone = _extract_phone(html.unescape(phone_m.group(1)))

        addr_lines = [
            html.unescape(a)
            for a in re.findall(r'<ADDRESS TYPE="String">(.*?)</ADDRESS>', block)
        ]
        if not phone:
            for line in addr_lines:
                phone = _extract_phone(line)
                if phone:
                    break

        gstin_m = re.search(r"<PARTYGSTIN\b[^>]*>(.*?)</PARTYGSTIN>", block)
        gstin   = html.unescape(gstin_m.group(1).strip()) if gstin_m else None

        result[name.lower()] = {
            "name":    name,
            "parent":  parent,
            "phone":   phone,
            "address": ", ".join(addr_lines) if addr_lines else None,
            "gstin":   gstin,
        }
    return result


def auto_insert_new_customers(bills: list, ledger_data: "dict | None" = None, dry_run: bool = False) -> list:
    """
    Step 4.5 — For each customer name in bills that is not yet in the customers
    table, fetch their ledger data from Tally and auto-insert using the same
    group→staff assignment mapping as full_customer_import.py.

    Returns list of newly inserted customer names (or would-be names in dry_run).
    """
    log.info("Step 4.5 — Checking for new customers to auto-insert")

    supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

    # Load existing customer names
    cust_map = {}
    offset   = 0
    while True:
        batch = (
            supa.table("customers")
            .select("customer_name")
            .range(offset, offset + 999)
            .execute().data
        )
        for row in batch:
            cust_map[row["customer_name"].strip().lower()] = True
        if len(batch) < 1000:
            break
        offset += 1000

    missing = sorted({
        b["customer_name"].strip()
        for b in bills
        if b["customer_name"].strip().lower() not in cust_map
    })

    if not missing:
        log.info("  All bill customers already in database — nothing to auto-insert")
        return []

    log.info("  %d customer(s) not in database: %s", len(missing), missing)
    if ledger_data is None:
        log.info("  Fetching ledger master from Tally...")
        ledger_data = _fetch_tally_ledger_master()
        log.info("  Ledger master: %d records fetched", len(ledger_data))

    users        = supa.table("users").select("id, name").execute().data
    user_by_name = {u["name"]: u["id"] for u in users}

    inserted_names = []
    unrecognised   = []   # (name, parent) — inserted with assigned_to=NULL
    not_found      = []   # not in Tally ledger master at all

    for name in missing:
        ldata = ledger_data.get(name.lower())
        if not ldata:
            log.warning("  '%s' not found in Tally ledger master — skipping", name)
            not_found.append(name)
            continue

        parent = ldata["parent"] or ""

        is_gt               = "(GT)" in parent
        is_staff            = parent in _STAFF_GROUPS
        is_cash             = parent == _CASH_GROUP
        is_bad_debt_current = parent == _BAD_DEBT_CURRENT
        is_case_filed       = parent == _CASE_FILED
        is_bad_debt_hist    = parent == _BAD_DEBT_HISTORICAL
        known_group = (
            is_gt or is_staff or is_cash
            or is_bad_debt_current or is_case_filed or is_bad_debt_hist
            or parent == "Sundry Debtors"
            or parent.endswith("Group")
        )

        if not known_group:
            log.warning(
                "  '%s' has unrecognised PARENT group '%s' — inserting with assigned_to=NULL",
                name, parent,
            )
            unrecognised.append((name, parent))

        assigned_to = None
        if is_gt:
            assigned_to = user_by_name.get("Thiagarajan")
        elif is_staff:
            assigned_to = user_by_name.get(_STAFF_GROUPS[parent])

        flagged        = False
        flagged_reason = None
        if is_bad_debt_current:
            flagged        = True
            flagged_reason = f"Bad debtor (Tally group: {parent})"
        elif is_case_filed:
            flagged        = True
            flagged_reason = "Case filed - legal recovery in progress"
        elif is_bad_debt_hist:
            flagged        = True
            flagged_reason = "Historical bad debt - written off in Tally, not currently trading"

        staff_label = (
            "Thiagarajan" if is_gt
            else _STAFF_GROUPS.get(parent, "NULL (unrecognised group)")
        )
        record = {
            "customer_name":  name,
            "customer_type":  "cash" if is_cash else "credit",
            "credit_days":    None if is_cash else 90,
            "assigned_to":    assigned_to,
            "phone":          ldata["phone"],
            "address":        ldata["address"],
            "gst_number":     ldata["gstin"],
            "flagged":        flagged,
            "flagged_reason": flagged_reason,
        }

        if dry_run:
            log.info(
                "  DRY RUN — would insert '%s' (group: %s → %s)",
                name, parent, staff_label,
            )
            inserted_names.append(name)
            continue

        try:
            supa.table("customers").insert(record).execute()
            log.info(
                "  AUTO-INSERTED '%s' (group: %s → %s)",
                name, parent, staff_label,
            )
            inserted_names.append(name)
        except Exception as exc:
            log.warning("  Failed to insert '%s': %s", name, exc)

    # ── Summary ───────────────────────────────────────────────────────────────
    log.info(
        "  Step 4.5 done: %d auto-inserted | %d unrecognised group (NULL assignment) | "
        "%d not found in Tally ledger",
        len(inserted_names), len(unrecognised), len(not_found),
    )
    if unrecognised:
        for n, g in unrecognised:
            log.warning("    Unrecognised group: '%s' -> parent='%s'", n, g)
    if not_found:
        for n in not_found:
            log.warning("    Not in Tally ledger master: '%s'", n)
    return inserted_names


# ── Step 4.6: Refresh contact/address fields from Tally ledger master ─────────

def refresh_ledger_contacts(ledger_data: dict, dry_run: bool = False) -> int:
    """
    Step 4.6 — Compare phone, address, and gst_number for every existing customer
    against the Tally ledger master and update any that have changed.
    Never touches customer_name, assigned_to, flagged, or customer_type.
    Returns number of customers updated.
    """
    def _norm(v):
        return (v or "").strip() or None

    supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

    customers = []
    offset    = 0
    while True:
        batch = (
            supa.table("customers")
            .select("id, customer_name, phone, address, gst_number")
            .range(offset, offset + 999)
            .execute().data
        )
        customers.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000

    updates = []
    for cust in customers:
        ldata = ledger_data.get(cust["customer_name"].strip().lower())
        if not ldata:
            continue
        changed = {}
        if _norm(cust["phone"])      != _norm(ldata["phone"]):   changed["phone"]      = _norm(ldata["phone"])
        if _norm(cust["address"])    != _norm(ldata["address"]): changed["address"]    = _norm(ldata["address"])
        if _norm(cust["gst_number"]) != _norm(ldata["gstin"]):   changed["gst_number"] = _norm(ldata["gstin"])
        if changed:
            updates.append((cust["id"], changed))

    if not updates:
        log.info("  Ledger refresh: no changes")
        return 0

    if dry_run:
        log.info("  Ledger refresh: DRY RUN — would update %d customer(s)", len(updates))
        return len(updates)

    for cid, changed in updates:
        supa.table("customers").update(changed).eq("id", cid).execute()

    log.info("  Ledger refresh: %d customer(s) updated", len(updates))
    return len(updates)


# ── Step 1: Tally connection check ─────────────────────────────────────────────

def check_tally(retries: int = 3, backoff_seconds: float = 5.0) -> bool:
    """
    Ping Tally. Retries a few times with backoff before giving up — about
    20% of runs historically failed here with either a refused connection
    (Tally closed) or a short read timeout (Tally busy on another request),
    and both are often transient enough that a second try succeeds.
    """
    log.info("Step 1 — Checking Tally connection at %s", TALLY_URL)
    for attempt in range(1, retries + 1):
        try:
            requests.get(TALLY_URL, timeout=TALLY_CHECK_TIMEOUT)
            log.info("  OK — Tally is reachable (attempt %d/%d)", attempt, retries)
            return True
        except requests.exceptions.ConnectTimeout:
            log.warning("  Attempt %d/%d — connection timed out (Tally busy?)", attempt, retries)
        except requests.exceptions.ConnectionError as exc:
            log.warning("  Attempt %d/%d — %s", attempt, retries, exc)
        if attempt < retries:
            time.sleep(backoff_seconds * attempt)
    log.error(
        "  FAILED after %d attempt(s) — Tally unreachable. "
        "Either Tally is closed, the wrong company is open, or it's too busy to answer.",
        retries,
    )
    return False


# ── Step 2: Fetch Bills Receivable XML ─────────────────────────────────────────

def fetch_tally_xml() -> str:
    fy    = _fy_start()
    today = date.today()
    log.info(
        "Step 2 — Fetching Bills Receivable (%s to %s)",
        fy.strftime("%d %b %Y"), today.strftime("%d %b %Y"),
    )

    xml_body = (
        "<ENVELOPE>"
        "<HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>"
        "<BODY><EXPORTDATA><REQUESTDESC>"
        "<REPORTNAME>Bills Receivable</REPORTNAME>"
        "<STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        f"<SVFROMDATE>{fy.strftime('%Y%m%d')}</SVFROMDATE>"
        f"<SVTODATE>{today.strftime('%Y%m%d')}</SVTODATE>"
        "<EXPLODEFLAG>Yes</EXPLODEFLAG>"
        "</STATICVARIABLES>"
        "</REQUESTDESC></EXPORTDATA></BODY>"
        "</ENVELOPE>"
    )

    raw   = _tally_post(xml_body, timeout=TALLY_TIMEOUT)
    q_pct = raw.count(b"?") / max(len(raw), 1) * 100
    log.info("  Response: %d bytes, %.1f%% question marks", len(raw), q_pct)

    if len(raw) < 200:
        raise RuntimeError(
            f"Tally returned only {len(raw)} bytes — is the correct company open?"
        )
    if q_pct > 70:
        raise RuntimeError(
            f"Response is {q_pct:.0f}% question marks — encoding error or wrong company. "
            f"Expected: {TALLY_COMPANY}"
        )

    backup = BASE_DIR / f"tally_outstanding_{RUN_TS}.xml"
    backup.write_bytes(raw)
    log.info("  Saved XML backup: %s", backup.name)

    cutoff = datetime.now().timestamp() - XML_KEEP_DAYS * 86400
    for old in BASE_DIR.glob("tally_outstanding_*.xml"):
        if old.stat().st_mtime < cutoff:
            old.unlink()
            log.info("  Deleted old backup: %s", old.name)

    return raw.decode("utf-8", errors="replace")


# ── Step 3: Parse XML -> bill list ─────────────────────────────────────────────

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
    r"<BILLVCHTYPE>(.*?)</BILLVCHTYPE>",
    re.DOTALL,
)

# Voucher types in Bills Receivable that represent credits the customer has already paid
# (on-account / advance — unmatched to any specific invoice). These should be stored as
# negative pending_amount so SUM() in the views naturally nets them against outstanding bills.
_CREDIT_VCH_TYPES = {"Payment", "Receipt"}

# KNOWN LIMITATION — "Opening Balance On Account" credits are not captured.
#
# When a customer has an on-account credit that originated as an opening balance
# (i.e. they overpaid in a prior financial year and the credit carried forward),
# it does NOT appear in Bills Receivable as a Payment/Receipt voucher. It only
# exists in the ledger's closing balance (CLOSINGBALANCE), which this sync cannot
# fetch: Tally's API times out computing CLOSINGBALANCE even for a single ledger
# with a large bill history.
#
# In practice this only affects DORMANT accounts — ones with no new invoices or
# receipts in the current FY — because for those, the opening balance equals the
# current closing balance and the discrepancy is stable. For any active customer
# (new bills, payments, etc.) the on-account credit would already be reconciled
# against a specific invoice in Tally and would then disappear from Bills
# Receivable correctly.
#
# Confirmed case: Sri Bhadri Narayana Textiles (flagged Bad Debts Written Off).
# Bills Receivable shows Rs 14,00,846 across 147 stale invoices (2013-2016);
# true Tally balance is Rs 6,06,949 Dr due to an opening-balance credit of
# Rs ~7,94,000. The account has had zero activity since ~2017, so the gap is
# permanent but not a live collections concern. Do not remove this comment and
# treat OPENINGBALANCE from the ledger master as a reliable proxy — it is only
# coincidentally correct for zero-activity accounts like Bhadri.


def parse_xml(xml_text: str) -> list:
    log.info("Step 3 — Parsing Bills Receivable XML")
    matches = _BILL_RE.findall(xml_text)
    log.info("  Found %d bill entries in XML", len(matches))

    if not matches:
        raise RuntimeError(
            "No bill entries found in the XML — structure may have changed."
        )

    cutoff = datetime.now().date() - timedelta(days=RECENT_MONTHS * 30)
    bills  = []

    n_credits = 0
    for date_raw, ref, party, cl_raw, due_raw, overdue_raw, vch_type in matches:
        try:
            inv_date = datetime.strptime(date_raw.strip(), "%d-%b-%y").date()
        except ValueError:
            inv_date = None
        try:
            due_date = datetime.strptime(due_raw.strip(), "%d-%b-%y").date()
        except ValueError:
            due_date = None
        try:
            raw_amt = abs(float(cl_raw.strip()))
            is_credit = vch_type.strip() in _CREDIT_VCH_TYPES
            amount = -raw_amt if is_credit else raw_amt
            if is_credit:
                n_credits += 1
        except ValueError:
            amount = 0.0
        try:
            overdue = int(float(overdue_raw.strip()))
        except ValueError:
            overdue = 0

        if overdue <= 30:    bucket = "0-30"
        elif overdue <= 60:  bucket = "30-60"
        elif overdue <= 90:  bucket = "60-90"
        elif overdue <= 120: bucket = "90-120"
        else:                bucket = "120+"

        if inv_date is None:      age = "unknown"
        elif inv_date >= cutoff:  age = "recent"
        else:                     age = "stale"

        bills.append({
            "customer_name":  html.unescape(party.strip()),
            "invoice_ref":    ref.strip(),
            "invoice_date":   inv_date.isoformat() if inv_date else None,
            "due_date":       due_date.isoformat() if due_date else None,
            "pending_amount": round(amount, 2),
            "days_overdue":   overdue,
            "bucket":         bucket,
            "age_status":     age,
        })

    n_recent  = sum(1 for b in bills if b["age_status"] == "recent" and b["pending_amount"] > 0)
    n_stale   = sum(1 for b in bills if b["age_status"] == "stale"  and b["pending_amount"] > 0)
    n_unknown = sum(1 for b in bills if b["age_status"] == "unknown" and b["pending_amount"] > 0)
    log.info(
        "  Tagged: %d recent (Rs %s), %d stale (Rs %s), %d unknown date",
        n_recent,  f"{sum(b['pending_amount'] for b in bills if b['age_status']=='recent'):,.0f}",
        n_stale,   f"{sum(b['pending_amount'] for b in bills if b['age_status']=='stale'):,.0f}",
        n_unknown,
    )
    if n_credits:
        credit_total = sum(-b["pending_amount"] for b in bills if b["pending_amount"] < 0)
        log.info(
            "  On-account credits: %d entries totalling Rs %s (stored as negative; nets against bills)",
            n_credits, f"{credit_total:,.0f}",
        )

    (BASE_DIR / "parsed_outstanding.json").write_text(
        json.dumps(bills, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info("  Updated parsed_outstanding.json")
    return bills


# ── Steps 4-7: Safe reload of Supabase outstanding ────────────────────────────
#
# Safety pattern — insert-first, delete-old-after:
#
#   Step 4  Build customer map + map bills to DB records
#   Step 5  Sanity check: abort if new count < 50% of current DB count
#   Step 6  Clean up any partial records from a previous failed run of THIS
#           timestamp (no-op on a fresh run, protects against retry scenarios)
#   Step 7  INSERT all new records (tagged with SYNC_TIMESTAMP)
#           -- if this fails, old records (different timestamp) are untouched --
#   Step 8  DELETE all records whose timestamp != SYNC_TIMESTAMP (the old ones)
#
# Result: if Step 7 fails partway, Step 8 is never reached and the table still
# holds the previous sync's complete data. The dashboard stays accurate.

def reload_supabase(bills: list, dry_run: bool = False):
    supabase = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SECRET_KEY"],
    )

    # ── Step 4: Build customer name -> UUID map ────────────────────────────────
    log.info("Step 4 — Loading customer map from Supabase")
    cust_map = {}
    offset   = 0
    while True:
        batch = (
            supabase.table("customers")
            .select("id, customer_name")
            .range(offset, offset + 999)
            .execute().data
        )
        for row in batch:
            cust_map[row["customer_name"].strip().lower()] = row["id"]
        if len(batch) < 1000:
            break
        offset += 1000
    log.info("  %d customers in database", len(cust_map))

    records   = []
    unmatched = {}
    for b in bills:
        cid = cust_map.get(b["customer_name"].strip().lower())
        if not cid:
            unmatched[b["customer_name"]] = unmatched.get(b["customer_name"], 0) + 1
            continue
        records.append({
            "customer_id":          cid,
            "invoice_ref":          b["invoice_ref"],
            "invoice_date":         b["invoice_date"],
            "due_date":             b["due_date"],
            "pending_amount":       b["pending_amount"],
            "bucket":               b["bucket"],
            "days_overdue":         b["days_overdue"],
            "age_status":           b["age_status"],
            "synced_from_tally_at": SYNC_TIMESTAMP,
        })

    n_skipped = len(bills) - len(records)
    if unmatched:
        log.warning(
            "  %d bills skipped — name not in customer table (%d unique):",
            n_skipped, len(unmatched),
        )
        for name, count in sorted(unmatched.items(), key=lambda x: -x[1]):
            log.warning("    %dx  %s", count, name)
    log.info("  %d bills ready to load, %d skipped", len(records), n_skipped)

    # ── Step 5: Sanity check ──────────────────────────────────────────────────
    log.info("Step 5 — Sanity check")
    current_count = (
        supabase.table("outstanding")
        .select("id", count="exact")
        .execute()
        .count
    )
    log.info("  Current DB outstanding rows: %d", current_count)
    log.info("  New records to load:         %d", len(records))

    if current_count > 100 and len(records) < current_count * SANITY_DROP_LIMIT:
        raise RuntimeError(
            f"Sanity check failed: Tally returned data for {len(records)} bills "
            f"but DB currently has {current_count} — that's a drop of "
            f"{100 - len(records)/current_count*100:.0f}%, which exceeds the "
            f"{SANITY_DROP_LIMIT*100:.0f}% threshold. "
            "Aborting to protect existing data. Check Tally for issues."
        )
    log.info("  Sanity check passed.")

    if dry_run:
        log.info("  DRY RUN — all Supabase writes skipped.")
        return len(records), n_skipped, unmatched

    # ── Step 6: Clean up any partial records from a previous failed attempt ───
    # (Deletes rows tagged with THIS run's SYNC_TIMESTAMP, which on a fresh run
    # is zero rows. On a retry after a partial failure, removes the incomplete set.)
    log.info("Step 6 — Clearing any partial records from a previous failed attempt")
    retry_cleaned = 0
    while True:
        r = (
            supabase.table("outstanding")
            .delete()
            .eq("synced_from_tally_at", SYNC_TIMESTAMP)
            .execute()
        )
        retry_cleaned += len(r.data)
        if not r.data:
            break
    if retry_cleaned:
        log.info("  Removed %d partial records from a previous failed attempt", retry_cleaned)
    else:
        log.info("  No partial records to clean up (fresh run)")

    # ── Step 7: Insert all new records ────────────────────────────────────────
    # Old records (different timestamp) are untouched until Step 8.
    # If this step fails, old data remains intact.
    log.info("Step 7 — Inserting %d new records", len(records))
    inserted = 0
    for i in range(0, len(records), SUPABASE_BATCH):
        batch = records[i : i + SUPABASE_BATCH]
        supabase.table("outstanding").insert(batch).execute()
        inserted += len(batch)
        log.info("  %d / %d inserted", inserted, len(records))

    # ── Step 8: Delete old records (only reached if Step 7 fully succeeded) ───
    log.info("Step 8 — Removing old records from previous sync")
    deleted = 0
    # Old records with a different (non-null) timestamp
    while True:
        r = (
            supabase.table("outstanding")
            .delete()
            .neq("synced_from_tally_at", SYNC_TIMESTAMP)
            .execute()
        )
        deleted += len(r.data)
        if not r.data:
            break
    # Old records with no timestamp (loaded before this runner existed)
    while True:
        r = (
            supabase.table("outstanding")
            .delete()
            .is_("synced_from_tally_at", "null")
            .execute()
        )
        deleted += len(r.data)
        if not r.data:
            break
    log.info("  Deleted %d old records", deleted)

    return inserted, n_skipped, unmatched


# ── Shared: one day of vouchers via TDL Collection ────────────────────────────
#
# Step 9 (sales) and Step 9b (collections) fetch the same shape of data for a
# single day and differ only in which voucher type they keep. Step 9b used to
# use a Day Book EXPORTDATA report instead, which returns one row per ledger
# entry rather than per voucher — ambiguous for multi-ledger receipts. Both
# now go through TDL Collection with a server-side date+type+not-cancelled
# FILTER (see "Server-side TDL filtering" above), plus the same client-side
# checks as a safety net in case a formula doesn't do exactly what it looks
# like it should.

def _fetch_vouchers_for_day(target_date: date, kind: str, collection_id: str) -> list:
    """
    Fetch one day's vouchers of a given kind ("sales" or "receipt") from
    Tally. Returns a list of {customer_name, invoice_ref, amount} dicts.

    Raises RuntimeError if the response is implausibly small (e.g. Tally
    dropped the connection mid-response, or answered with an error page)
    rather than silently returning an empty list — a genuinely empty but
    well-formed response is still just as small in bytes for a single-day
    request, but a *broken* one is smaller still (no ENVELOPE/BODY wrapper).
    This is what stops a failed fetch from ever being recorded as an honest
    zero: the caller never even gets a result to upsert, it gets an exception.
    Also raises if Tally reports a TDL error on the request (_raise_on_tally_error).
    """
    if kind == "sales":
        type_formula = " OR ".join(f'$VoucherTypeName = "{t}"' for t in SALES_VOUCHER_TYPES)
        type_substr  = "SALES"
    elif kind == "receipt":
        # Collections come in as three distinct exact voucher types — confirmed
        # live 24-Sep-2026 on a day with real receipts. All three must be
        # matched or e.g. PoS/Cash receipts would silently vanish.
        type_formula = " OR ".join(f'$VoucherTypeName = "{t}"' for t in RECEIPT_VOUCHER_TYPES)
        type_substr  = "RECEIPT"  # substring match catches all three: "Receipt"/"PoS Receipt"/"Cash Receipt"
    else:
        raise ValueError(f"unknown kind: {kind}")

    formula = f'$Date = $$Date:"{_tally_date_literal(target_date)}" AND NOT $IsCancelled AND ({type_formula})'
    fetch_fields = "Date, VoucherNumber, PartyLedgerName, Amount, VoucherTypeName, IsCancelled"
    xml_body = _build_voucher_collection_request(collection_id, fetch_fields, formula)

    raw = _tally_post(xml_body, timeout=TALLY_COLLECTION_TIMEOUT)
    if len(raw) < 200:
        raise RuntimeError(
            f"Tally returned only {len(raw)} bytes for {collection_id} on {target_date.isoformat()} "
            "— treating this as a failed fetch, not a real zero"
        )
    xml = raw.decode("utf-8", errors="replace")
    _raise_on_tally_error(xml, collection_id)

    vouchers          = re.findall(r"<VOUCHER\b.*?</VOUCHER>", xml, re.DOTALL)
    items             = []
    skipped_date      = 0
    skipped_cancelled = 0
    dates_seen        = []

    for v in vouchers:
        date_m     = re.search(r"<DATE[^>]*>(.*?)</DATE>", v)
        voucher_dt = _parse_tally_date(date_m.group(1)) if date_m else None
        if voucher_dt:
            dates_seen.append(voucher_dt)

        vtype_m = re.search(r"<VOUCHERTYPENAME[^>]*>(.*?)</VOUCHERTYPENAME>", v)
        if not (vtype_m and type_substr in vtype_m.group(1).upper()):
            continue

        ref_m = re.search(r"<VOUCHERNUMBER[^>]*>(.*?)</VOUCHERNUMBER>", v)
        ref   = ref_m.group(1).strip() if ref_m else ""
        # Sales Orders (SO-) aren't real sales — same exclusion Step 9 always had.
        # Receipts have no equivalent prefix to exclude.
        if kind == "sales" and (not ref or ref.startswith("SO-")):
            continue

        if _is_voucher_cancelled(v):
            skipped_cancelled += 1
            continue

        if voucher_dt != target_date:
            skipped_date += 1
            continue

        amt_m = re.search(r"<AMOUNT[^>]*>(.*?)</AMOUNT>", v)
        if not amt_m:
            continue
        try:
            amt = round(abs(float(amt_m.group(1))), 2)
        except ValueError:
            continue

        party_m = re.search(r"<PARTYLEDGERNAME[^>]*>(.*?)</PARTYLEDGERNAME>", v)
        items.append({
            "customer_name": html.unescape(party_m.group(1).strip()) if party_m else "",
            "invoice_ref":   ref,
            "amount":        amt,
        })

    _log_voucher_date_span(collection_id, len(vouchers), dates_seen)
    if skipped_cancelled:
        log.info("  Skipped %d cancelled voucher(s)", skipped_cancelled)
    if skipped_date:
        log.info(
            "  Skipped %d voucher(s) with date != %s (server-side date filter didn't fully narrow it down)",
            skipped_date, target_date.isoformat(),
        )
    return items


def _resolve_customer_ids(supa, items: list) -> int:
    """Stamp each item's customer_id UUID by name lookup. Returns matched count."""
    cust_id_map = {}
    offset = 0
    while True:
        batch = (
            supa.table("customers")
            .select("id, customer_name")
            .range(offset, offset + 999)
            .execute().data
        )
        for row in batch:
            cust_id_map[row["customer_name"].strip().lower()] = row["id"]
        if len(batch) < 1000:
            break
        offset += 1000

    matched = 0
    for item in items:
        cid = cust_id_map.get(item["customer_name"].strip().lower())
        item["customer_id"] = cid
        if cid:
            matched += 1
    return matched


# ── Step 9: Today's sales ──────────────────────────────────────────────────────

def sync_today_sales(dry_run: bool = False, target_date: "date | None" = None):
    """
    Fetch sales for target_date (defaults to today) using TDL Collection —
    one record per voucher, no ledger-entry ambiguity. Upserts to daily_sales.
    Caller should catch exceptions (non-fatal) — and must NOT treat a raised
    exception as "zero sales"; _fetch_vouchers_for_day raises rather than
    returning an empty list on a broken fetch, so nothing gets upserted here
    in that case at all.
    """
    today = target_date or date.today()
    log.info("Step 9 — Fetching sales for %s (TDL Collection)", today.isoformat())

    items       = _fetch_vouchers_for_day(today, "sales", "TodaySales")
    sales_count = len(items)
    sales_total = round(sum(i["amount"] for i in items), 2)

    log.info("  Today's sales: %d invoice(s), Rs %s", sales_count, f"{sales_total:,.2f}")
    has_detail = any(i["customer_name"] or i["invoice_ref"] for i in items)
    log.info(
        "  Per-invoice detail: %s",
        "available" if has_detail
        else "NOT available — PARTYLEDGERNAME/VOUCHERNUMBER absent from TDL Collection response",
    )

    if dry_run:
        log.info("  DRY RUN — daily_sales upsert skipped")
        return sales_count, sales_total

    supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

    matched = _resolve_customer_ids(supa, items)
    log.info(
        "  UUID resolved: %d / %d items (%d unmatched — name variation or new customer)",
        matched, len(items), len(items) - matched,
    )

    supa.table("daily_sales").upsert(
        {
            "sale_date":     today.isoformat(),
            "total_amount":  sales_total,
            "invoice_count": sales_count,
            "synced_at":     datetime.now(UTC).isoformat(),
            "items":         items,
        },
        on_conflict="sale_date",
    ).execute()
    log.info("  daily_sales upserted for %s", today.isoformat())
    return sales_count, sales_total


# ── Step 9b: Today's collections (Receipt vouchers) ───────────────────────────

def sync_today_collections(dry_run: bool = False, target_date: "date | None" = None):
    """
    Fetch Receipt vouchers for target_date (defaults to today) using the same
    TDL Collection approach as Step 9 (no more Day Book — that returned one
    row per ledger entry, ambiguous for multi-ledger receipts). Upserts to
    daily_collections. Caller should catch exceptions (non-fatal); see the
    note on sync_today_sales about never treating an exception as a real zero.
    """
    today = target_date or date.today()
    log.info("Step 9b — Fetching collections for %s (TDL Collection)", today.isoformat())

    items         = _fetch_vouchers_for_day(today, "receipt", "TodayCollections")
    receipt_count = len(items)
    collections_total = round(sum(i["amount"] for i in items), 2)

    log.info("  Today's collections: %d receipt(s), Rs %s", receipt_count, f"{collections_total:,.2f}")

    if dry_run:
        log.info("  DRY RUN — daily_collections upsert skipped")
        return receipt_count, collections_total

    supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

    matched = _resolve_customer_ids(supa, items)
    log.info("  UUID resolved: %d / %d items (%d unmatched)", matched, len(items), len(items) - matched)

    supa.table("daily_collections").upsert(
        {
            "sale_date":     today.isoformat(),
            "total_amount":  collections_total,
            "invoice_count": receipt_count,
            "synced_at":     datetime.now(UTC).isoformat(),
            "items":         items,
        },
        on_conflict="sale_date",
    ).execute()
    log.info("  daily_collections upserted for %s", today.isoformat())
    return receipt_count, collections_total


# ── Step 10: Sales Vouchers → sales_history ────────────────────────────────────
#
# 25-Sep-2026: re-fetching whole months every run was STILL keeping Tally busy
# 80-120s every 30 minutes even after the Phase 1 current+rotation scheme —
# the office reported it lagging every connected PC on the shared billing
# machine. Step 10 now has three modes:
#
#   - Full FY sweep — every SALES_HISTORY_FULL_SWEEP_DAYS, or whenever --full
#     is passed. The only mode that can detect deletions (an incremental
#     fetch, by date range OR AlterID, can only ever add/update — it has no
#     way to notice a voucher that's gone missing).
#   - AlterID incremental — once ALTERID_SYNC_ENABLED is confirmed True,
#     every run fetches only sales vouchers with Tally's AlterID greater than
#     the highest one seen last time. AlterID increases company-wide on
#     every create/edit, so this needs no date bound at all and picks up
#     edits/cancellations to old invoices for free, not just new invoices.
#   - Month-based fallback — used while ALTERID_SYNC_ENABLED is False. Same
#     current+rotation idea as before, but now rate-limited (current month at
#     most every FALLBACK_CURRENT_MONTH_HOURS, one older month at most every
#     FALLBACK_OLDER_MONTH_HOURS) instead of refetching both every single run.
#
# Upserts are always keyed on voucher_number; only the full sweep ever
# deletes, and only under the safety checks in
# _reconcile_sales_history_deletions.

def _fy_months(fy_start: date, today: date) -> list:
    """List of (year, month) tuples from fy_start's month through today's month, inclusive."""
    months = []
    y, m = fy_start.year, fy_start.month
    while date(y, m, 1) <= today:
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def _month_bounds(year: int, month: int, today: date) -> "tuple[date, date]":
    start      = date(year, month, 1)
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    end        = min(next_month - timedelta(days=1), today)
    return start, end


def _note_skip_example(examples: list, ref: str, raw_xml: str):
    """
    Phase 4 debugging aid — keep a small, CC--biased sample of vouchers that
    were skipped for "no parseable amount tag" so their raw structure can be
    inspected without guessing. Capped so this never grows unbounded.
    """
    is_cc      = bool(ref) and ref.startswith("CC-")
    cc_count    = sum(1 for e in examples if e["ref"].startswith("CC-"))
    other_count = len(examples) - cc_count
    if len(examples) >= 8:
        return
    if is_cc and cc_count >= 3:
        return
    if not is_cc and other_count >= 5:
        return
    examples.append({"ref": ref or "(no voucher number)", "raw_xml": raw_xml})


def _write_skip_examples_debug_file(examples: list):
    if not examples:
        return
    debug_path = BASE_DIR / "debug_skipped_vouchers.xml"
    lines = [
        f"<!-- {len(examples)} example(s) skipped in Step 10 for 'no parseable amount tag' -->",
        f"<!-- Captured {datetime.now().isoformat()} -->",
        "",
    ]
    for ex in examples:
        lines.append(f"<!-- voucher_number: {ex['ref']} -->")
        lines.append(ex["raw_xml"])
        lines.append("")
    debug_path.write_text("\n".join(lines), encoding="utf-8")
    log.warning(
        "  Saved %d skipped-voucher example(s) to %s for inspection",
        len(examples), debug_path.name,
    )


def _parse_qty(raw: str):
    n = re.sub(r"[^0-9.]", "", raw.strip().split(" ")[0])
    try:    return round(float(n), 3)
    except: return None  # noqa: E722


def _parse_rate(raw: str):
    n = re.sub(r"[^0-9.]", "", raw.strip().split("/")[0])
    try:    return round(float(n), 2)
    except: return None  # noqa: E722


def _extract_alterid(voucher_xml: str) -> "int | None":
    m = re.search(r"<ALTERID[^>]*>(.*?)</ALTERID>", voucher_xml)
    if not m:
        return None
    try:
        return int(float(m.group(1).strip()))
    except ValueError:
        return None


def _fetch_sales_month_chunk(year: int, month: int, today: date, skip_examples: list) -> dict:
    """
    Fetch + parse one month's Sales vouchers from Tally. Returns
    {"records": [...], "max_alterid": int, "ok": bool} — "ok" is False only
    when the request itself failed (timeout/connection error), so the caller
    can tell "genuinely zero vouchers this month" apart from "couldn't ask".
    A single bad chunk never raises — a timeout on one month must not abort
    the months still queued behind it.

    Confirmed live 24-Sep-2026: "no parseable amount tag" skips were all
    cancelled vouchers (IsCancelled=Yes, with empty party/amount/entries) —
    e.g. SBDC-56/26-27. Those are now filtered server-side (NOT $IsCancelled)
    and skipped client-side with a single aggregate count, not a per-voucher
    warning. The remaining "no parseable amount tag" path below should now
    only fire for a genuinely unexplained case — it still gets captured to
    debug_skipped_vouchers.xml since that would be new information.
    """
    from_date, to_date = _month_bounds(year, month, today)
    from_str = from_date.strftime("%Y%m%d")
    to_str   = to_date.strftime("%Y%m%d")
    log.info("  Chunk %s – %s", from_str, to_str)

    # Date RANGE filter — confirmed live (17-23 Sep returned exactly those
    # 578 vouchers in 12.8s). _log_voucher_date_span below remains as an
    # ongoing check that it keeps narrowing the response down to this month.
    type_formula = " OR ".join(f'$VoucherTypeName = "{t}"' for t in SALES_VOUCHER_TYPES)
    formula = (
        f'$Date >= $$Date:"{_tally_date_literal(from_date)}" AND '
        f'$Date <= $$Date:"{_tally_date_literal(to_date)}" AND '
        f'NOT $IsCancelled AND ({type_formula})'
    )
    xml_body = _build_voucher_collection_request(
        "SalesHistory",
        "Date, VoucherNumber, PartyLedgerName, Amount, VoucherTypeName, IsCancelled, StockItemName, ActualQty, Rate, AlterID",
        formula,
    )

    try:
        raw = _tally_post(xml_body, timeout=TALLY_COLLECTION_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        # Covers Timeout AND ConnectionError (incl. resets/refusals) — a prior
        # version only caught Timeout, so a mid-loop ConnectionResetError would
        # propagate out and abandon every month still queued behind it.
        log.warning("  Chunk %s–%s failed (%s) — skipping this month, will retry next rotation", from_str, to_str, exc)
        return {"records": [], "max_alterid": 0, "ok": False}

    xml = raw.decode("utf-8", errors="replace")
    _raise_on_tally_error(xml, f"SalesHistory chunk {from_str}-{to_str}")

    records           = []
    vouchers          = re.findall(r"<VOUCHER\b.*?</VOUCHER>", xml, re.DOTALL)
    raw_count         = len(vouchers)
    after_sales       = 0
    after_so          = 0
    skipped_cancelled = 0
    dates_seen        = []
    max_alterid       = 0
    synced_at         = datetime.now(UTC).isoformat()

    for v in vouchers:
        date_m  = re.search(r"<DATE[^>]*>(.*?)</DATE>", v)
        v_date  = _parse_tally_date(date_m.group(1)) if date_m else None
        if v_date:
            dates_seen.append(v_date)

        alterid = _extract_alterid(v)
        if alterid is not None:
            max_alterid = max(max_alterid, alterid)

        vtype_m = re.search(r"<VOUCHERTYPENAME[^>]*>(.*?)</VOUCHERTYPENAME>", v)
        if not (vtype_m and "SALES" in vtype_m.group(1).upper()):
            continue
        after_sales += 1
        ref_m = re.search(r"<VOUCHERNUMBER[^>]*>(.*?)</VOUCHERNUMBER>", v)
        ref   = ref_m.group(1).strip() if ref_m else None
        if not ref or ref.startswith("SO-"):
            continue
        after_so += 1

        if _is_voucher_cancelled(v):
            skipped_cancelled += 1
            continue

        party_m = re.search(r"<PARTYLEDGERNAME[^>]*>(.*?)</PARTYLEDGERNAME>", v)
        amt_m   = re.search(r"<AMOUNT[^>]*>(.*?)</AMOUNT>", v)
        stock_m = re.search(r"<STOCKITEMNAME[^>]*>(.*?)</STOCKITEMNAME>", v)
        qty_m   = re.search(r"<ACTUALQTY[^>]*>(.*?)</ACTUALQTY>", v)
        rate_m  = re.search(r"<RATE[^>]*>(.*?)</RATE>", v)

        sale_date = v_date.isoformat() if v_date else None

        amount = None
        if amt_m:
            try:    amount = round(abs(float(amt_m.group(1))), 2)
            except: pass  # noqa: E722
        if amount is None:
            log.warning("  Skipping %s — no parseable amount tag (not a known-cancelled voucher)", ref)
            _note_skip_example(skip_examples, ref, v)
            continue

        records.append({
            "voucher_number": ref,
            "sale_date":      sale_date,
            "customer_name":  html.unescape(party_m.group(1).strip()) if party_m else None,
            "amount":         amount,
            "stock_item":     html.unescape(stock_m.group(1).strip()) if stock_m else None,
            "quantity":       _parse_qty(qty_m.group(1))  if qty_m  else None,
            "rate":           _parse_rate(rate_m.group(1)) if rate_m else None,
            "voucher_type":   vtype_m.group(1).strip(),
            "synced_at":      synced_at,
        })

    _log_voucher_date_span(f"SalesHistory chunk {from_str}-{to_str}", raw_count, dates_seen)
    if skipped_cancelled:
        log.info("  Skipped %d cancelled voucher(s)", skipped_cancelled)
    log.info(
        "  Chunk: %d raw VOUCHER tags | %d after SALES filter | %d after SO- filter | %d added",
        raw_count, after_sales, after_so, len(records),
    )
    return {"records": records, "max_alterid": max_alterid, "ok": True}


def _fetch_sales_by_alterid(min_alterid: int, skip_examples: list) -> "tuple[list, int]":
    """
    Fetch sales vouchers with AlterID > min_alterid — no date bound needed at
    all, since AlterID tracks "what changed", not "what date it's dated for".
    This is what picks up an edit or cancellation to an old invoice: it keeps
    whatever AlterID it's given on the edit, so it always ends up in the next
    incremental fetch, whereas a date-range fetch would never look at that
    month again once its rotation had passed. Only used once
    ALTERID_SYNC_ENABLED is confirmed True (see its docstring). Returns
    (records, max_alterid_seen). Raises on request/TDL failure — the caller
    (sync_sales_history, via main()'s existing per-step try/except) treats
    that as a normal "Step 10 failed this run", same as any other Tally call.
    """
    type_formula = " OR ".join(f'$VoucherTypeName = "{t}"' for t in SALES_VOUCHER_TYPES)
    formula = f'$AlterID > {int(min_alterid)} AND NOT $IsCancelled AND ({type_formula})'
    xml_body = _build_voucher_collection_request(
        "SalesHistoryAlterID",
        "Date, VoucherNumber, PartyLedgerName, Amount, VoucherTypeName, IsCancelled, StockItemName, ActualQty, Rate, AlterID",
        formula,
    )

    raw = _tally_post(xml_body, timeout=TALLY_COLLECTION_TIMEOUT)
    xml = raw.decode("utf-8", errors="replace")
    _raise_on_tally_error(xml, "SalesHistoryAlterID")

    records           = []
    vouchers          = re.findall(r"<VOUCHER\b.*?</VOUCHER>", xml, re.DOTALL)
    max_alterid       = min_alterid
    skipped_cancelled = 0
    synced_at         = datetime.now(UTC).isoformat()

    for v in vouchers:
        alterid = _extract_alterid(v)
        if alterid is not None:
            max_alterid = max(max_alterid, alterid)

        vtype_m = re.search(r"<VOUCHERTYPENAME[^>]*>(.*?)</VOUCHERTYPENAME>", v)
        if not (vtype_m and "SALES" in vtype_m.group(1).upper()):
            continue
        ref_m = re.search(r"<VOUCHERNUMBER[^>]*>(.*?)</VOUCHERNUMBER>", v)
        ref   = ref_m.group(1).strip() if ref_m else None
        if not ref or ref.startswith("SO-"):
            continue

        if _is_voucher_cancelled(v):
            skipped_cancelled += 1
            continue

        date_m  = re.search(r"<DATE[^>]*>(.*?)</DATE>", v)
        v_date  = _parse_tally_date(date_m.group(1)) if date_m else None
        party_m = re.search(r"<PARTYLEDGERNAME[^>]*>(.*?)</PARTYLEDGERNAME>", v)
        amt_m   = re.search(r"<AMOUNT[^>]*>(.*?)</AMOUNT>", v)
        stock_m = re.search(r"<STOCKITEMNAME[^>]*>(.*?)</STOCKITEMNAME>", v)
        qty_m   = re.search(r"<ACTUALQTY[^>]*>(.*?)</ACTUALQTY>", v)
        rate_m  = re.search(r"<RATE[^>]*>(.*?)</RATE>", v)

        amount = None
        if amt_m:
            try:    amount = round(abs(float(amt_m.group(1))), 2)
            except: pass  # noqa: E722
        if amount is None:
            log.warning("  Skipping %s — no parseable amount tag (not a known-cancelled voucher)", ref)
            _note_skip_example(skip_examples, ref, v)
            continue

        records.append({
            "voucher_number": ref,
            "sale_date":      v_date.isoformat() if v_date else None,
            "customer_name":  html.unescape(party_m.group(1).strip()) if party_m else None,
            "amount":         amount,
            "stock_item":     html.unescape(stock_m.group(1).strip()) if stock_m else None,
            "quantity":       _parse_qty(qty_m.group(1))  if qty_m  else None,
            "rate":           _parse_rate(rate_m.group(1)) if rate_m else None,
            "voucher_type":   vtype_m.group(1).strip(),
            "synced_at":      synced_at,
        })

    if skipped_cancelled:
        log.info("  Skipped %d cancelled voucher(s)", skipped_cancelled)
    log.info(
        "  AlterID > %d: %d raw voucher(s), %d added, max AlterID seen %d",
        min_alterid, len(vouchers), len(records), max_alterid,
    )
    return records, max_alterid


def _upsert_sales_records(all_records: list, dry_run: bool) -> float:
    """
    Shared dedup + upsert + UUID-backfill tail used by every Step 10 mode
    (full sweep, AlterID incremental, month-based fallback). Returns today's
    total, for the Step9-vs-Step10 reconcile cross-check.
    """
    today = date.today()
    log.info("  Total records collected (before dedup): %d", len(all_records))
    if all_records:
        log.info("  Sample voucher numbers (first 8): %s", [r["voucher_number"] for r in all_records[:8]])

    # Deduplicate by voucher_number — last occurrence wins (handles chunk-boundary overlaps)
    seen = {}
    for rec in all_records:
        seen[rec["voucher_number"]] = rec
    all_records = list(seen.values())
    log.info("  After dedup: %d unique voucher numbers", len(all_records))

    if not all_records:
        log.warning("  No sales records found — skipping upsert")
        return 0.0

    today_iso   = today.isoformat()
    today_total = round(sum(r["amount"] for r in all_records if r.get("sale_date") == today_iso), 2)
    log.info("  Step 10 — today's slice total: Rs %s", f"{today_total:,.2f}")

    if dry_run:
        log.info("  DRY RUN — sales_history upsert skipped")
        return today_total

    supa = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SECRET_KEY"],
    )

    # Stamp customer_id UUID onto each record — same pattern as sync_today_sales.
    # Requires: ALTER TABLE sales_history ADD COLUMN IF NOT EXISTS customer_id uuid REFERENCES customers(id);
    cust_id_map = {}
    offset = 0
    while True:
        batch = (
            supa.table("customers")
            .select("id, customer_name")
            .range(offset, offset + 999)
            .execute().data
        )
        for row in batch:
            cust_id_map[row["customer_name"].strip().lower()] = row["id"]
        if len(batch) < 1000:
            break
        offset += 1000

    matched_uuid = 0
    for rec in all_records:
        cname = (rec.get("customer_name") or "").strip().lower()
        cid   = cust_id_map.get(cname)
        rec["customer_id"] = cid
        if cid:
            matched_uuid += 1
    log.info(
        "  UUID resolved: %d / %d records (%d unmatched — name variation or new customer)",
        matched_uuid, len(all_records), len(all_records) - matched_uuid,
    )

    upserted = 0
    for i in range(0, len(all_records), SUPABASE_BATCH):
        batch = all_records[i : i + SUPABASE_BATCH]
        supa.table("sales_history").upsert(batch, on_conflict="voucher_number").execute()
        upserted += len(batch)
        log.info("  Upserted %d / %d", upserted, len(all_records))
    log.info("  sales_history sync complete")

    # One-time backfill: stamp customer_id on any rows that were written before
    # UUID stamping was introduced (April/May history gap in the frontend chart).
    null_by_name: dict = {}
    null_offset = 0
    while True:
        null_batch = (
            supa.table("sales_history")
            .select("voucher_number, customer_name")
            .is_("customer_id", "null")
            .range(null_offset, null_offset + 999)
            .execute().data
        ) or []
        for r in null_batch:
            cname = (r.get("customer_name") or "").strip().lower()  # type: ignore[union-attr]
            null_by_name.setdefault(cname, []).append(r["voucher_number"])  # type: ignore[index]
        if len(null_batch) < 1000:
            break
        null_offset += 1000

    if null_by_name:
        total_null = sum(len(v) for v in null_by_name.values())
        log.info("  UUID backfill: %d null-customer_id row(s) across %d name(s)", total_null, len(null_by_name))
        backfill_updated = 0
        for cname_lower, vnums in null_by_name.items():
            cid = cust_id_map.get(cname_lower)
            if not cid:
                continue
            for i in range(0, len(vnums), 200):
                chunk = vnums[i : i + 200]
                supa.table("sales_history").update({"customer_id": cid}).in_("voucher_number", chunk).execute()
                backfill_updated += len(chunk)
        log.info("  UUID backfill: %d row(s) updated", backfill_updated)
    else:
        log.info("  UUID backfill: no null-customer_id rows — already clean")

    return today_total


def _reconcile_sales_history_deletions(fy_start: date, today: date, current_voucher_numbers: set, dry_run: bool) -> int:
    """
    Delete sales_history rows in [fy_start, today] whose voucher_number isn't
    in current_voucher_numbers (this run's freshly fetched, complete set for
    that range). A row missing from that set is treated as "shouldn't be
    there" whether the voucher was truly deleted in Tally or simply cancelled
    after being a real sale — both are excluded from every Step 10 fetch, so
    "not in the current set" is the correct signal either way.

    Refuses to delete anything if it would remove more than
    SALES_HISTORY_DELETE_SANITY_LIMIT of the range's previously-tracked rows
    — a second guard on top of the caller's own "skip if any chunk failed"
    check, in case the fetch was subtly wrong in a way that still reported
    itself as fully successful.
    """
    supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
    existing = set()
    offset = 0
    while True:
        batch = (
            supa.table("sales_history")
            .select("voucher_number")
            .gte("sale_date", fy_start.isoformat())
            .lte("sale_date", today.isoformat())
            .range(offset, offset + 999)
            .execute().data
        ) or []
        existing.update(r["voucher_number"] for r in batch)
        if len(batch) < 1000:
            break
        offset += 1000

    to_delete = existing - current_voucher_numbers
    if not to_delete:
        log.info("  Deletion reconciliation: nothing to remove (%d existing, all still present in Tally)", len(existing))
        return 0

    if existing and len(to_delete) / len(existing) > SALES_HISTORY_DELETE_SANITY_LIMIT:
        log.warning(
            "  Deletion reconciliation SKIPPED — would remove %d / %d (%.0f%%) of tracked vouchers, "
            "over the %.0f%% sanity limit. A broken fetch can look exactly like a wave of deletions — "
            "investigate before trusting this.",
            len(to_delete), len(existing), len(to_delete) / len(existing) * 100,
            SALES_HISTORY_DELETE_SANITY_LIMIT * 100,
        )
        return 0

    log.warning(
        "  Deletion reconciliation: %d voucher(s) no longer in Tally (deleted or cancelled) — removing from sales_history",
        len(to_delete),
    )
    if dry_run:
        log.info("  DRY RUN — deletion skipped")
        return len(to_delete)

    to_delete_list = list(to_delete)
    for i in range(0, len(to_delete_list), 200):
        chunk = to_delete_list[i : i + 200]
        supa.table("sales_history").delete().in_("voucher_number", chunk).execute()
    return len(to_delete)


def _full_sales_history_sweep(dry_run: bool) -> "tuple[float, int]":
    """
    Complete FY sweep, month by month — the only Step 10 mode that can catch
    deletions (see module docstring). Runs automatically every
    SALES_HISTORY_FULL_SWEEP_DAYS and whenever --full is passed.

    Deletion reconciliation is skipped (upserts still happen) if any month
    chunk failed to fetch this run — an incomplete "current" picture must
    never decide what's "missing", or one bad chunk could wipe out a whole
    month of real data. Returns (today_total, max_alterid_seen).
    """
    fy_start = _fy_start()
    today    = date.today()
    months   = _fy_months(fy_start, today)
    log.info("Step 10 — FULL FY sweep: %d month(s), %s → %s", len(months), fy_start, today)

    skip_examples   = []
    all_records     = []
    current_numbers = set()
    max_alterid     = 0
    all_chunks_ok   = True

    for (y, m) in months:
        result = _fetch_sales_month_chunk(y, m, today, skip_examples)
        if not result["ok"]:
            all_chunks_ok = False
        all_records.extend(result["records"])
        current_numbers.update(r["voucher_number"] for r in result["records"])
        max_alterid = max(max_alterid, result["max_alterid"])

    _write_skip_examples_debug_file(skip_examples)

    if not all_chunks_ok:
        log.warning("  One or more month chunks failed this sweep — deletion reconciliation SKIPPED (upserts still applied)")
    else:
        _reconcile_sales_history_deletions(fy_start, today, current_numbers, dry_run)

    today_total = _upsert_sales_records(all_records, dry_run)
    return today_total, max_alterid


def _sync_sales_history_alterid(dry_run: bool) -> "tuple[float, int, str]":
    """
    AlterID incremental Step 10 path — every run, no throttle needed, since
    by construction it only ever fetches what actually changed since the
    last seen AlterID. Only reached when ALTERID_SYNC_ENABLED is True.
    """
    state       = _load_state()
    min_alterid = state.get("last_alterid", 0)
    log.info("Step 10 — AlterID incremental: fetching sales vouchers with AlterID > %d", min_alterid)

    skip_examples       = []
    records, max_alterid = _fetch_sales_by_alterid(min_alterid, skip_examples)
    _write_skip_examples_debug_file(skip_examples)

    new_max = max(min_alterid, max_alterid)
    if not dry_run:
        state["last_alterid"] = new_max
        _save_state(state)

    today_total = _upsert_sales_records(records, dry_run)
    return today_total, new_max, "alterid"


def _sync_sales_history_fallback(dry_run: bool) -> "tuple[float | None, int, str]":
    """
    Month-based Step 10 path, used while ALTERID_SYNC_ENABLED is False.
    Fetches the current month at most every FALLBACK_CURRENT_MONTH_HOURS,
    and one older FY month (rotating; the previous month first for
    NEW_MONTH_CATCHUP_RUNS runs after a month rollover, to catch late
    entries) at most every FALLBACK_OLDER_MONTH_HOURS — instead of both
    every single run. Returns (today_total, max_alterid_seen, mode); mode is
    "skipped" and today_total is None if neither throttle was due this run,
    so the caller never feeds a stale/absent fetch into the Step9-vs-Step10
    reconcile cross-check as if it were a real number.
    """
    fy_start   = _fy_start()
    today      = date.today()
    all_months = _fy_months(fy_start, today)
    current    = (today.year, today.month)
    month_key  = f"{today.year:04d}-{today.month:02d}"

    state = _load_state()
    if state.get("last_seen_month") != month_key:
        state["last_seen_month"]     = month_key
        state["new_month_runs_done"] = 0

    months_to_fetch = []

    if _is_due(state.get("last_current_month_fetch_at"), FALLBACK_CURRENT_MONTH_HOURS):
        months_to_fetch.append(current)
        state["last_current_month_fetch_at"] = datetime.now(UTC).isoformat()

    if _is_due(state.get("last_older_month_fetch_at"), FALLBACK_OLDER_MONTH_HOURS):
        prev_month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        if (prev_month in all_months and prev_month != current
                and state.get("new_month_runs_done", 0) < NEW_MONTH_CATCHUP_RUNS):
            older_pick = prev_month
            state["new_month_runs_done"] = state.get("new_month_runs_done", 0) + 1
        else:
            older = [m for m in all_months if m != current]
            if older:
                idx = state.get("rotation_index", 0) % len(older)
                older_pick = older[idx]
                state["rotation_index"] = state.get("rotation_index", 0) + 1
            else:
                older_pick = None
        if older_pick and older_pick not in months_to_fetch:
            months_to_fetch.append(older_pick)
            state["last_older_month_fetch_at"] = datetime.now(UTC).isoformat()

    if not dry_run:
        _save_state(state)

    if not months_to_fetch:
        log.info(
            "Step 10 — SKIPPED this run (current month fetched < %dh ago, older month < %dh ago)",
            FALLBACK_CURRENT_MONTH_HOURS, FALLBACK_OLDER_MONTH_HOURS,
        )
        return None, state.get("last_alterid", 0), "skipped"

    log.info("Step 10 — fallback sync: %s", [f"{y}-{m:02d}" for y, m in months_to_fetch])

    skip_examples = []
    all_records   = []
    max_alterid   = state.get("last_alterid", 0)
    for (y, m) in months_to_fetch:
        result = _fetch_sales_month_chunk(y, m, today, skip_examples)
        all_records.extend(result["records"])
        max_alterid = max(max_alterid, result["max_alterid"])
    _write_skip_examples_debug_file(skip_examples)

    if not dry_run and max_alterid != state.get("last_alterid", 0):
        state = _load_state()
        state["last_alterid"] = max_alterid
        _save_state(state)

    today_total = _upsert_sales_records(all_records, dry_run)
    return today_total, max_alterid, "fallback"


def sync_sales_history(dry_run: bool = False, full: bool = False) -> "tuple[float | None, int, str]":
    """
    Step 10 orchestrator. Picks one of three paths per run — see the module
    docstring above Step 10. Returns (today_total, max_alterid_seen, mode);
    today_total is None only when mode == "skipped" (nothing fetched this
    run) — the caller must not treat that as a real zero for the
    Step9-vs-Step10 reconcile cross-check.
    """
    state          = _load_state()
    full_sweep_due = full or _is_due(state.get("last_full_sales_history_sweep"), SALES_HISTORY_FULL_SWEEP_DAYS * 24)

    if full_sweep_due:
        log.info("Step 10 mode: FULL SWEEP (%s)", "--full" if full else f"{SALES_HISTORY_FULL_SWEEP_DAYS}-day schedule")
        today_total, max_alterid = _full_sales_history_sweep(dry_run)
        if not dry_run:
            state = _load_state()
            state["last_full_sales_history_sweep"] = datetime.now(UTC).isoformat()
            state["last_alterid"] = max(state.get("last_alterid", 0), max_alterid)
            _save_state(state)
        return today_total, max_alterid, "full"

    if ALTERID_SYNC_ENABLED:
        log.info("Step 10 mode: ALTERID incremental")
        return _sync_sales_history_alterid(dry_run)

    log.info("Step 10 mode: month-based fallback (ALTERID_SYNC_ENABLED=False)")
    return _sync_sales_history_fallback(dry_run)


# ── Reconciliation ─────────────────────────────────────────────────────────────

def _fetch_tally_voucher_count(date_str: str) -> int:
    """
    Lightweight second Tally request — independent count of vouchers matching
    Step 9's filters: sales voucher type, real voucher number (not SO-),
    voucher DATE == date_str, not cancelled, parseable amount. Uses the same
    server-side date+type+not-cancelled FILTER as Step 9, with the same
    client-side checks kept as a safety net (see "Server-side TDL filtering").
    """
    target = datetime.strptime(date_str, "%Y%m%d").date()
    type_formula = " OR ".join(f'$VoucherTypeName = "{t}"' for t in SALES_VOUCHER_TYPES)
    formula = f'$Date = $$Date:"{_tally_date_literal(target)}" AND NOT $IsCancelled AND ({type_formula})'
    xml_body = _build_voucher_collection_request(
        "SalesCountCheck",
        "Date, VoucherNumber, VoucherTypeName, Amount, IsCancelled",
        formula,
    )

    raw = _tally_post(xml_body, timeout=TALLY_COLLECTION_TIMEOUT)
    xml = raw.decode("utf-8", errors="replace")
    _raise_on_tally_error(xml, "SalesCountCheck")

    vouchers   = re.findall(r"<VOUCHER\b.*?</VOUCHER>", xml, re.DOTALL)
    count      = 0
    dates_seen = []
    for v in vouchers:
        date_m     = re.search(r"<DATE[^>]*>(.*?)</DATE>", v)
        voucher_dt = _parse_tally_date(date_m.group(1)) if date_m else None
        if voucher_dt:
            dates_seen.append(voucher_dt)

        vtype_m = re.search(r"<VOUCHERTYPENAME[^>]*>(.*?)</VOUCHERTYPENAME>", v)
        if not (vtype_m and "SALES" in vtype_m.group(1).upper()):
            continue
        ref_m = re.search(r"<VOUCHERNUMBER[^>]*>(.*?)</VOUCHERNUMBER>", v)
        ref   = ref_m.group(1).strip() if ref_m else ""
        if not ref or ref.startswith("SO-"):
            continue
        if _is_voucher_cancelled(v):
            continue
        if voucher_dt != target:
            continue
        amt_m = re.search(r"<AMOUNT[^>]*>(.*?)</AMOUNT>", v)
        if not amt_m:
            continue
        try:
            float(amt_m.group(1))
        except ValueError:
            continue
        count += 1

    _log_voucher_date_span("SalesCountCheck", len(vouchers), dates_seen)
    return count


def reconcile_sync(sync_date: date, step9_total: float, step10_total: float, fetched_count: int) -> str:
    """
    Post-sync safety check (non-fatal — caller must catch exceptions):
    1. Voucher count: re-fetches count from Tally independently and compares
       against fetched_count. Retries once on mismatch.
    2. Step 9 vs Step 10 cross-check: if totals differ by more than ₹1,
       logs a per-voucher mismatch report pulling from daily_sales + sales_history.

    Status is tri-state, not just OK/MISMATCH — if Tally can't be reached for
    the independent count check, that is reported as UNKNOWN rather than
    silently defaulting to OK. That silent-default-to-OK was the actual bug:
    the old count_ok flag started True and an exception in the count fetch
    never flipped it, so a run where Tally was unreachable for this check
    still logged "STATUS: OK".

    Only raises (fatal to the caller's try/except, logged as a warning
    upstream) on a genuine confirmed MISMATCH — never on UNKNOWN, since not
    being able to reach Tally for a secondary check isn't itself a data
    problem.

    Log format:
      [RECONCILE] Date: {date} | Vouchers: {fetched}/{tally} | Step9: ₹{x} | Step10: ₹{y} | STATUS: OK/MISMATCH/UNKNOWN
    """
    date_str = sync_date.strftime("%Y%m%d")

    # ── 1. Voucher count check ────────────────────────────────────────────────
    tally_count  = None
    count_status = "unknown"   # "ok" | "mismatch" | "unknown"
    try:
        tally_count = _fetch_tally_voucher_count(date_str)
        if tally_count == fetched_count:
            count_status = "ok"
        else:
            log.warning(
                "[RECONCILE] Count mismatch — fetched %d, Tally reports %d — retrying",
                fetched_count, tally_count,
            )
            tally_count = _fetch_tally_voucher_count(date_str)
            count_status = "ok" if tally_count == fetched_count else "mismatch"
            if count_status == "mismatch":
                log.error(
                    "[RECONCILE] Count still mismatched after retry (fetched=%d, tally=%d)",
                    fetched_count, tally_count,
                )
    except Exception as exc:
        log.warning("[RECONCILE] Could not fetch Tally count — STATUS will be UNKNOWN, not OK: %s", exc)
        count_status = "unknown"

    # ── 2. Step 9 vs Step 10 total cross-check ───────────────────────────────
    diff     = abs(step9_total - step10_total)
    total_ok = diff <= 1.0

    if count_status == "unknown":
        status = "UNKNOWN"
    elif count_status == "mismatch" or not total_ok:
        status = "MISMATCH"
    else:
        status = "OK"

    log.info(
        "[RECONCILE] Date: %s | Vouchers: %d/%s | Step9: ₹%s | Step10: ₹%s | STATUS: %s",
        sync_date.isoformat(),
        fetched_count,
        str(tally_count) if tally_count is not None else "?",
        f"{step9_total:,.2f}",
        f"{step10_total:,.2f}",
        status,
    )

    if not total_ok:
        log.warning(
            "[RECONCILE] MISMATCH DETAIL — Step9 vs Step10 differ by ₹%s for %s",
            f"{diff:,.2f}", sync_date.isoformat(),
        )
        try:
            supa      = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
            s9_resp   = supa.table("daily_sales").select("items").eq("sale_date", sync_date.isoformat()).execute()
            s9_items  = (s9_resp.data[0] if s9_resp.data else {}).get("items") or []

            s10_resp     = supa.table("sales_history").select("voucher_number, amount").eq("sale_date", sync_date.isoformat()).execute()
            s10_by_ref   = {r["voucher_number"]: float(r["amount"] or 0) for r in (s10_resp.data or [])}

            s9_refs = set()
            for item in s9_items:
                ref    = item.get("invoice_ref") or ""
                s9_amt = float(item.get("amount") or 0)
                s9_refs.add(ref)
                s10_amt = s10_by_ref.get(ref)
                if s10_amt is None:
                    log.warning("[RECONCILE]   %s — Step9: ₹%s | Step10: NOT FOUND", ref, f"{s9_amt:,.2f}")
                elif abs(s9_amt - s10_amt) > 1:
                    log.warning(
                        "[RECONCILE]   %s — Step9: ₹%s | Step10: ₹%s | Diff: ₹%s",
                        ref, f"{s9_amt:,.2f}", f"{s10_amt:,.2f}", f"{abs(s9_amt - s10_amt):,.2f}",
                    )
            for ref, s10_amt in s10_by_ref.items():
                if ref not in s9_refs:
                    log.warning("[RECONCILE]   %s — Step9: NOT FOUND | Step10: ₹%s", ref, f"{s10_amt:,.2f}")
        except Exception as exc:
            log.warning("[RECONCILE] Could not fetch per-voucher detail: %s", exc)

    if count_status == "mismatch":
        raise RuntimeError(
            f"[RECONCILE] Voucher count mismatch for {sync_date}: "
            f"fetched={fetched_count}, tally={tally_count}"
        )

    return status


# ── Backfill ───────────────────────────────────────────────────────────────────

def backfill_mode(from_date: date, to_date: date, force: bool, dry_run: bool):
    """
    Backfill daily_sales and daily_collections for every date in [from_date, to_date].
    Skips dates that already have a daily_sales row unless force=True.
    Does NOT re-run Steps 1-8 (outstanding) or Step 10 (sales_history) — those
    are not date-scoped in the same way.
    """
    backfill_start = time.monotonic()
    total_days = (to_date - from_date).days + 1
    log.info(
        "[BACKFILL] Range: %s → %s | %d day(s) | force=%s | dry_run=%s",
        from_date.isoformat(), to_date.isoformat(), total_days, force, dry_run,
    )

    # Pre-fetch which dates already have daily_sales rows in one query
    supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
    resp = (
        supa.table("daily_sales")
        .select("sale_date")
        .gte("sale_date", from_date.isoformat())
        .lte("sale_date", to_date.isoformat())
        .execute()
    )
    existing = {r["sale_date"] for r in (resp.data or [])}
    log.info("[BACKFILL] %d date(s) already have daily_sales data", len(existing))

    synced = 0
    skipped = 0
    current = from_date

    while current <= to_date:
        date_str = current.isoformat()
        idx      = synced + skipped + 1

        if date_str in existing and not force:
            log.info(
                "[BACKFILL] %s (%d/%d) — SKIP (already synced; use --force to overwrite)",
                date_str, idx, total_days,
            )
            skipped += 1
            current += timedelta(days=1)
            continue

        log.info("[BACKFILL] %s (%d/%d) — syncing...", date_str, idx, total_days)

        try:
            count, total = sync_today_sales(dry_run=dry_run, target_date=current)
            log.info(
                "[BACKFILL] %s — sales: %d invoice(s), Rs %s",
                date_str, count, f"{total:,.2f}",
            )
        except Exception as exc:
            log.warning("[BACKFILL] %s — sales sync failed: %s", date_str, exc)

        try:
            sync_today_collections(dry_run=dry_run, target_date=current)
            log.info("[BACKFILL] %s — collections done", date_str)
        except Exception as exc:
            log.warning("[BACKFILL] %s — collections sync failed: %s", date_str, exc)

        synced += 1
        current += timedelta(days=1)

    log.info(
        "[BACKFILL] Finished in %.1fs — %d synced, %d skipped, %d total",
        time.monotonic() - backfill_start, synced, skipped, total_days,
    )


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SBDC Tally → Supabase sync")
    parser.add_argument("--dry-run",    action="store_true", help="Parse without DB writes")
    parser.add_argument("--from-local", action="store_true", help="Use tally_with_dates.xml instead of live Tally")
    parser.add_argument("--full",       action="store_true", help="Force a full FY sales_history sweep + deletion reconciliation now, instead of waiting for the weekly schedule")
    parser.add_argument("--backfill",   action="store_true", help="Backfill daily_sales/collections for a date range")
    parser.add_argument("--from",       dest="from_date", metavar="YYYY-MM-DD", help="Backfill start date (inclusive)")
    parser.add_argument("--to",         dest="to_date",   metavar="YYYY-MM-DD", help="Backfill end date (inclusive)")
    parser.add_argument("--force",      action="store_true", help="Overwrite existing rows in --backfill mode")
    args = parser.parse_args()

    dry_run    = args.dry_run
    from_local = args.from_local

    if not acquire_lock():
        # Another run is genuinely still in progress — exit cleanly, not a failure.
        # (Task Scheduler treats a non-zero exit as a failed run; overlap isn't one.)
        sys.exit(0)

    try:
        # ── Backfill mode ─────────────────────────────────────────────────────
        if args.backfill:
            if not args.from_date or not args.to_date:
                parser.error("--backfill requires --from YYYY-MM-DD and --to YYYY-MM-DD")
            try:
                from_date = date.fromisoformat(args.from_date)
                to_date   = date.fromisoformat(args.to_date)
            except ValueError as exc:
                parser.error(f"Invalid date: {exc}")
            if from_date > to_date:
                parser.error("--from date must be on or before --to date")

            log.info("=" * 60)
            log.info("SUPREME BALAJI — BACKFILL MODE")
            log.info("  Range : %s → %s", args.from_date, args.to_date)
            log.info("  Force : %s", args.force)
            if dry_run: log.info("  DRY RUN — no DB writes")
            log.info("  Run   : %s", RUN_TS)
            log.info("=" * 60)

            try:
                backfill_mode(from_date, to_date, force=args.force, dry_run=dry_run)
            except Exception as exc:
                log.exception("BACKFILL FAILED: %s", exc)
                sys.exit(1)
            return

        # ── Normal sync mode ──────────────────────────────────────────────────
        log.info("=" * 60)
        log.info("SUPREME BALAJI — TALLY OUTSTANDING SYNC")
        if from_local:  log.info("  MODE: FROM LOCAL FILE (no Tally connection)")
        if dry_run:     log.info("  MODE: DRY RUN (Supabase writes skipped)")
        if args.full:   log.info("  MODE: FULL FY sales_history sweep (--full)")
        log.info("  Run: %s", RUN_TS)
        log.info("=" * 60)

        # Per-step status — the honest record of what actually happened this run,
        # instead of a single "success" that used to be written even when Tally
        # was never reached for Steps 9/9b/10. "skipped" covers both
        # --from-local (Steps 9/9b/10 can't run — no Tally) and a heavy step
        # being intentionally throttled this run; either way it must not read
        # as "failed" or drag the overall status down to "partial".
        step_status = {
            "outstanding":   "pending",
            "today_sales":   "skipped",
            "collections":   "skipped",
            "sales_history": "skipped",
            "ledger_master": "skipped",   # informational only — never gates overall_status
        }
        reconcile_status = None
        run_start        = time.monotonic()

        try:
            bills = []
            inserted, skipped, unmatched, auto_inserted = 0, 0, {}, []
            outstanding_ran = False

            if from_local:
                local_xml = BASE_DIR / "tally_with_dates.xml"
                if not local_xml.exists():
                    raise FileNotFoundError(
                        "--from-local specified but tally_with_dates.xml not found in backend/"
                    )
                log.info("Steps 1-2 — SKIPPED (--from-local mode)")
                log.info("  Reading: %s", local_xml)
                xml_text = local_xml.read_text(encoding="utf-8", errors="replace")
                bills = parse_xml(xml_text)
                inserted, skipped, unmatched = reload_supabase(bills, dry_run=dry_run)
                step_status["outstanding"] = "success"
                outstanding_ran = True
            else:
                if not check_tally():
                    raise RuntimeError(
                        "Tally not reachable. Run from the office network with Tally open."
                    )

                # Step 2/3 (Bills Receivable) — only if it's actually been a
                # while. This is the single biggest Tally request in the whole
                # runner; re-running it every 30 minutes was a large part of
                # what was keeping the shared billing PC busy.
                prev_status      = _load_last_status()
                last_outstanding = prev_status.get("last_success", {}).get("outstanding")
                outstanding_due  = _is_due(last_outstanding, OUTSTANDING_THROTTLE_HOURS)

                if outstanding_due:
                    xml_text = fetch_tally_xml()
                    bills = parse_xml(xml_text)
                    inserted, skipped, unmatched = reload_supabase(bills, dry_run=dry_run)
                    step_status["outstanding"] = "success"
                    outstanding_ran = True
                else:
                    log.info(
                        "Step 2/3 SKIPPED — outstanding last synced %.1fh ago (throttle: %dh)",
                        _hours_since(last_outstanding), OUTSTANDING_THROTTLE_HOURS,
                    )
                    step_status["outstanding"] = "skipped"

                # Step 4.5/4.6 — its own daily throttle, independent of the
                # outstanding throttle above. 4.6 (contact refresh) doesn't
                # need bills, so it still runs on its own schedule; 4.5
                # (auto-insert) needs THIS run's bills to know what's new, so
                # it's a harmless no-op — not a failure — on a run where
                # Step 2/3 above was itself throttled.
                run_state         = _load_state()
                last_ledger_run   = run_state.get("last_ledger_master_run")
                ledger_master_due = _is_due(last_ledger_run, LEDGER_MASTER_THROTTLE_HOURS)

                if ledger_master_due:
                    try:
                        log.info("Step 4.5/4.6 — Fetching Tally ledger master")
                        ledger_data = _fetch_tally_ledger_master()
                        log.info("  Ledger master: %d records fetched", len(ledger_data))
                        if outstanding_ran:
                            auto_inserted = auto_insert_new_customers(bills, ledger_data=ledger_data, dry_run=dry_run)
                        else:
                            log.info("  Step 4.5 (auto-insert) skipped — no fresh bills this run (Step 2/3 was throttled)")
                        refresh_ledger_contacts(ledger_data, dry_run=dry_run)
                        step_status["ledger_master"] = "success"
                        if not dry_run:
                            run_state["last_ledger_master_run"] = datetime.now(UTC).isoformat()
                            _save_state(run_state)
                    except Exception as exc:
                        log.warning("Step 4.5/4.6 WARNING — Ledger sync failed (non-fatal): %s", exc)
                        step_status["ledger_master"] = "failed"
                else:
                    log.info(
                        "Step 4.5/4.6 SKIPPED — ledger master last refreshed %.1fh ago (throttle: %dh)",
                        _hours_since(last_ledger_run), LEDGER_MASTER_THROTTLE_HOURS,
                    )
                    step_status["ledger_master"] = "skipped"

            if unmatched:
                log.warning("  Sending skip alert for %d unmatched customer(s)", len(unmatched))
                _send_skip_alert_email(unmatched)

            step9_count, step9_total, step10_today, step10_mode = 0, 0.0, None, None
            if not from_local:
                try:
                    step9_count, step9_total = sync_today_sales(dry_run=dry_run)
                    step_status["today_sales"] = "success"
                except Exception as exc:
                    log.warning("Step 9 WARNING — Today's sales sync failed (non-fatal): %s", exc)
                    step_status["today_sales"] = "failed"

                try:
                    sync_today_collections(dry_run=dry_run)
                    step_status["collections"] = "success"
                except Exception as exc:
                    log.warning("Step 9b WARNING — Today's collections sync failed (non-fatal): %s", exc)
                    step_status["collections"] = "failed"

                try:
                    step10_today, _step10_alterid, step10_mode = sync_sales_history(dry_run=dry_run, full=args.full)
                    step_status["sales_history"] = "skipped" if step10_mode == "skipped" else "success"
                except Exception as exc:
                    log.warning("Step 10 WARNING — Sales history sync failed (non-fatal): %s", exc)
                    step_status["sales_history"] = "failed"
                    step10_today = None

                if not dry_run:
                    if step10_today is not None:
                        try:
                            reconcile_status = reconcile_sync(date.today(), step9_total, step10_today, step9_count)
                        except Exception as exc:
                            log.warning("RECONCILE WARNING — Post-sync check failed (non-fatal): %s", exc)
                            reconcile_status = "MISMATCH"
                    else:
                        log.info("RECONCILE — skipped (Step 10 fetched nothing fresh this run, so there's nothing to cross-check Step 9 against)")

            # ── Overall status ──────────────────────────────────────────────────
            # "success" if every step that was supposed to run this mode either
            # succeeded or was intentionally skipped (throttled, or --from-local
            # where a step simply can't run) — a skip on purpose must never read
            # as a failure or drag this to "partial". ledger_master is excluded
            # here deliberately: it was always fully non-fatal, even before
            # throttling existed. A confirmed reconcile MISMATCH still downgrades
            # to "partial" even if every individual step nominally succeeded,
            # since it means the data those steps wrote doesn't agree with itself.
            core_steps_ok = all(
                step_status[s] in ("success", "skipped")
                for s in ("outstanding", "today_sales", "collections", "sales_history")
            )
            if core_steps_ok and reconcile_status != "MISMATCH":
                overall_status = "success"
            else:
                overall_status = "partial"

            elapsed_s = time.monotonic() - run_start
            summary = {
                "bills_from_tally":    len(bills) if outstanding_ran else None,
                "loaded_to_supabase":  inserted if outstanding_ran else None,
                "skipped_no_match":    skipped if outstanding_ran else None,
                "new_customers_added": len(auto_inserted),
                "new_customer_names":  auto_inserted,
                "skipped_names":       [
                    {"name": n, "bills": c} for n, c in sorted(unmatched.items(), key=lambda x: -x[1])
                ],
                "reconcile_status":    reconcile_status,
                "sales_history_mode":  step10_mode,
                "duration_seconds":    round(elapsed_s, 1),
            }
            _write_status(overall_status, step_status, summary, dry_run=dry_run)

            log.info("=" * 60)
            log.info("SYNC %s%s", overall_status.upper(), " (DRY RUN)" if dry_run else "")
            log.info("  Duration               : %.1fs", elapsed_s)
            if outstanding_ran:
                log.info("  Bills from Tally       : %d", len(bills))
                log.info("  New customers added    : %d%s",
                         len(auto_inserted),
                         (" — " + ", ".join(auto_inserted)) if auto_inserted else "")
                log.info("  Loaded to Supabase     : %d", inserted)
                log.info("  Skipped (no match)     : %d", skipped)
            else:
                log.info("  Outstanding (Step 2/3) : SKIPPED this run (throttled)")
            if step10_mode:
                log.info("  Sales history mode     : %s", step10_mode)
            log.info("  Step status            : %s", step_status)
            if reconcile_status:
                log.info("  Reconcile              : %s", reconcile_status)
            log.info("  Log: %s", log_path)
            log.info("=" * 60)

        except Exception as exc:
            error_msg = str(exc)
            log.exception("SYNC FAILED: %s", error_msg)
            log.error("Log saved to: %s", log_path)
            step_status["outstanding"] = "failed"
            elapsed_s = time.monotonic() - run_start
            log.error("  Duration before failure: %.1fs", elapsed_s)
            _write_status(
                "failed", step_status,
                {"error": error_msg, "log": str(log_path), "duration_seconds": round(elapsed_s, 1)},
                dry_run=dry_run,
            )
            _send_failure_email(error_msg)
            sys.exit(1)

    finally:
        release_lock()


if __name__ == "__main__":
    main()
