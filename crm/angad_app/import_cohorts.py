"""
Import the cohort workbooks (Humans & AI Calling + the two SV files) into the
cache, so the AI-vs-Human comparison is a standing dashboard tab instead of a
hand-built table.  python import_cohorts.py

Auto-discovers the newest copy of each file in Downloads. Phones are
pseudonymised to lead_ref; free-text comment columns are redacted.
"""
from pathlib import Path

import pandas as pd
import pii

DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)
DL = Path("C:/Users/Tiwari/Downloads")

PHONE_COLS = {"Phone", "Phone Number"}
# hand-maintained sheets get phones/emails typed into arbitrary fields —
# these columns carry no analytical value and are dropped outright
DROP_COLS = {"Company", "Channel Partner Employee", "Email",
             # PII policy 2026-08-06: no personal names in the cache
             "Lead Name", "Name", "name", "Customer Name"}


def newest(pattern):
    cands = sorted(DL.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def clean(df):
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    for c in list(df.columns):
        if c in PHONE_COLS:
            df["lead_ref"] = df[c].map(pii.ref)
            df = df.drop(columns=[c])
        elif c in DROP_COLS:
            df = df.drop(columns=[c])
    # ops sheets are free-form: people type phones and emails into ANY field
    # (found in company and lead_name) — scrub every text column, not a list
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].map(lambda v: pii.redact(v) if isinstance(v, str) else v)
    # drop unnamed spacer columns
    df = df[[c for c in df.columns if not c.startswith(("Unnamed", " "))]]
    df.columns = [c.strip().lower().replace(" ", "_").replace("(", "").replace(")", "")
                  for c in df.columns]
    return df


def save(df, name):
    from sync import to_parquet_safe
    to_parquet_safe(df, DATA / name)
    print(f"  {name}: {len(df):,} rows")


def run():
    wrote = 0
    hb = newest("Humans & AI Calling*.xlsx")
    if hb:
        print(f"workbook: {hb.name}")
        sheets = {
            "AI SUmmary": "cohort_ai_daily.parquet",
            "AI Leads": "cohort_ai_leads.parquet",
            "AI sv booked": "cohort_ai_booked.parquet",
            "AI SV DONE": "cohort_ai_done.parquet",
            "Human Leads": "cohort_human_leads.parquet",
            "Human SV bookings": "cohort_human_booked.parquet",
            "Human site visits done": "cohort_human_done.parquet",
        }
        xl = pd.ExcelFile(hb)
        for sh, out in sheets.items():
            if sh in xl.sheet_names:
                save(clean(pd.read_excel(hb, sheet_name=sh)), out)
                wrote += 1

    cf = newest("Confirmed Site Visits Angad*.xlsx")
    if cf:
        print(f"confirmed: {cf.name}")
        xl = pd.ExcelFile(cf)
        for sh, out in {"PRE SITE VISIT ": "cohort_sv_confirmed.parquet",
                        "TRUE SITE VISIT": "cohort_sv_true.parquet",
                        "FALSE SITE VISIT ": "cohort_sv_false.parquet"}.items():
            match = next((x for x in xl.sheet_names if x.strip() == sh.strip()), None)
            if match:
                save(clean(pd.read_excel(cf, sheet_name=match)), out)
                wrote += 1

    cp = newest("Angad_ Total Site visits completed*.xlsx")
    if cp:
        print(f"completed: {cp.name}")
        save(clean(pd.read_excel(cp, sheet_name="Sheet1")), "cohort_sv_completed.parquet")
        wrote += 1

    print(f"done - {wrote} sheets cached")
    return wrote


if __name__ == "__main__":
    run()
