"""
Import a Zoho lead export (CSV) into the local cache.  python import_zoho.py [path]

Without a path it auto-picks the NEWEST Leads_*.csv in
  C:/Users/Tiwari/Downloads/Daily angad performance/
so the daily download is picked up automatically. sync.py also calls this
best-effort at the end of every sync, so pressing "Fetch fresh data from
Mongo" in the dashboard refreshes the Zoho slice too.

PII: the phone number is pseudonymised to the same LEAD-xxxxxxxx refs the rest
of the cache uses (so everything joins), and the raw number, emails and names
are NOT written to the parquet. Master Lead ID / Record Id are retained - the
data dictionary defines MLID as a non-PII surrogate key.
"""
import re
import sys
from pathlib import Path

import pandas as pd
import pii

DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)
OUT = DATA / "zoho_export.parquet"
DEFAULT_DIR = Path("C:/Users/Tiwari/Downloads/Daily angad performance")

# columns dropped entirely - personal data with no analytical value here
DROP = {"Phone Number", "Email", "First Name", "Last Name", "Lead Name",
        "Company", "Call History", "WhatsApp Chat History", "Social Lead ID",
        "Device Fingerprint", "Origin Session ID", "Google Click ID",
        "IP City", "Current Location"}

KEEP_HINT = [  # ordered: the audit-relevant columns come first in the parquet
    "Record Id", "Master Lead ID", "Project Lead ID", "Stage", "Lead Status",
    "PRD Stage", "PRD Status", "ASBL Project", "Lead Source", "UTM Source",
    "Campaign Name", "UTM Campaign", "Ad Name", "Ad Set Name",
    "Call Attempt Count", "SS Call Attempt Count", "Total Call Duration Secs",
    "Call Connected Flag", "Last Call Disposition", "Do Not Call",
    "Visit Attended", "Visit Date", "Site Visit Date", "Visit Slot",
    "Slot Proposed Flag", "Token Paid Flag", "Confirmed Via",
    "Lead Budget", "Budget", "Purchase Purpose", "Timeline",
    "Configuration Interest", "Intent", "Intent Captured", "High Intent Reason",
    "Not Interested Reason", "Qualification Score",
    "WhatsApp State", "WhatsApp Reply Count", "WA Replied Flag",
    "Brochure Sent", "Price Sheet Sent", "Chatbot Attempt Count",
    "Chatbot Follow-up Count", "IsBorn_Fresh", "IsReactivated",
    "IsBorn_InOtherProject", "Resubmission Count",
    "Lead Received At", "Born Date", "Created Time", "Last Action Time",
    "Stage Updated At", "Next Action At", "Next Action Type",
]


def snake(name):
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def pick_csv():
    # NOTE: no sys.argv here - sync.py imports and calls run(), and reading the
    # caller's argv turned "--only" into a file path once already
    if DEFAULT_DIR.exists():
        cands = sorted(DEFAULT_DIR.glob("Leads_*.csv"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if cands:
            return cands[0]
    return None


def run(path=None):
    src = Path(path) if path else pick_csv()
    if not src or not src.exists():
        print("  zoho import: no CSV found (pass a path or place Leads_*.csv "
              f"in {DEFAULT_DIR})")
        return False

    df = pd.read_csv(src, encoding="utf-8-sig", dtype=str, low_memory=False)
    n0 = len(df)

    # pseudonymise the phone BEFORE dropping it, same refs as the whole cache
    df["lead_ref"] = df.get("Phone Number", pd.Series(dtype=str)).map(pii.ref)
    df = df.drop(columns=[c for c in DROP if c in df.columns])

    ordered = ["lead_ref"] + [c for c in KEEP_HINT if c in df.columns] + \
              [c for c in df.columns if c not in KEEP_HINT and c != "lead_ref"]
    df = df[[c for c in dict.fromkeys(ordered)]]
    df.columns = [c if c == "lead_ref" else snake(c) for c in df.columns]

    # scrub any phone-shaped digits that leaked into free-text columns
    for c in ("high_intent_reason", "not_interested_reason",
              "last_customer_response"):
        if c in df.columns:
            df[c] = df[c].map(lambda v: pii.redact(v) if isinstance(v, str) else v)

    for c in df.columns:
        if c.endswith(("_time", "_at", "_date")) or c in ("born_date", "visit_date",
                                                          "site_visit_date"):
            df[c] = pd.to_datetime(df[c], errors="coerce")

    df.to_parquet(OUT, index=False)
    print(f"  zoho import: {src.name} -> {OUT.name}  ({n0:,} rows, "
          f"{df['lead_ref'].notna().sum():,} with a joinable lead_ref)")
    return True


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
