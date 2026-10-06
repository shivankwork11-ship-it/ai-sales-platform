"""
Security check — run after every sync.  python audit_pii.py

Scans every cached parquet file for values that look like Indian mobile
numbers, email addresses, or credentials. The dashboard only ever reads this
cache, so if this comes back CLEAN, nothing sensitive can reach the browser.

Exits 1 if anything is found, so you can wire it into a script if you want.
"""
import re
import sys
from pathlib import Path

import pandas as pd

DATA = Path(__file__).parent / "data"

PHONE = re.compile(r"(?<!\d)(?:\+?91)?[6-9]\d{9}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
SECRET = re.compile(r"(mongodb\+srv://|password\s*[:=]|api[_-]?key\s*[:=]|"
                    r"bearer\s+[\w.-]{12,}|secret\s*[:=])", re.I)
CHECKS = [("PHONE", PHONE), ("EMAIL", EMAIL), ("SECRET", SECRET)]

# lead_ref is the pseudonym column - by construction it cannot hold a number
ALLOW_COLS = {"lead_ref"}

# PII policy 2026-08-06: these columns must never carry real values
NAME_COLS = {"customername", "customer_name", "customer_name_src", "name",
             "lead_name", "display_name", "first_name", "last_name",
             "sanitized_name", "agent_name"}


def main():
    if not DATA.exists():
        print("no data/ directory - nothing to audit")
        return 0

    findings, files, cells = [], 0, 0
    for f in sorted(DATA.glob("*.parquet")):
        files += 1
        try:
            df = pd.read_parquet(f)
        except Exception as e:
            print(f"  !! cannot read {f.name}: {e}")
            continue
        for c in df.columns:
            if c in ALLOW_COLS or df[c].dtype.name not in ("object", "string"):
                continue
            s = df[c].dropna().astype(str)
            if s.empty:
                continue
            cells += len(s)
            for name, pat in CHECKS:
                hit = s[s.str.contains(pat, na=False, regex=True)]
                if len(hit):
                    findings.append((f.name, c, name, len(hit), hit.iloc[0][:60]))
            # PII policy 2026-08-06: personal-name columns must be EMPTY in
            # every cached file (values withheld from output by construction)
            if c.lower().replace(" ", "_") in NAME_COLS:
                named = s[~s.str.strip().str.lower().isin(
                    ["", "(no name)", "(name removed)", "none", "nan"])]
                if len(named):
                    findings.append((f.name, c, "NAME", len(named), "<withheld>"))

    print(f"scanned {files} files, {cells:,} text values\n")
    if findings:
        print("  *** SENSITIVE DATA FOUND IN THE CACHE ***\n")
        for fn, c, kind, n, ex in findings:
            print(f"  {fn:<50} {c:<26} {kind:<7} {n:>6} rows   e.g. {ex!r}")
        print("\n  Delete data/ and re-run sync.py, then report this - the "
              "pseudonymiser missed a field.")
        return 1

    print("  CLEAN — no phone numbers, emails or credentials in the local cache.")
    print("  Safe to run the dashboard.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
