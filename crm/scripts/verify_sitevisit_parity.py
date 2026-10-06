"""Post-deploy parity check: hits the REAL /api/leads and /api/pipeline endpoints
(not a re-derivation of their logic) and confirms the Leads tab's canonical
Pre Site Visit count equals Pipeline's canonical Booked Site Visit count, on the
same database snapshot. Both endpoints compute resolved_stage/achieved.siteVisit
via the same lead_identity.py module — this script proves the two DEPLOYED
routes actually agree, not just that the shared code exists.

Usage:
    SESSION_COOKIE='session=...' python3 scripts/verify_sitevisit_parity.py \\
        --base-url https://leadprofile.goodcalls.in

SESSION_COOKIE: copy the `session` cookie from an authenticated browser tab
(both endpoints require a logged-in session; there is no header-based auth for
them). --base-url defaults to https://leadprofile.goodcalls.in.

Output is counts and per-project/mismatch-category diagnostics ONLY — no phone
numbers, names, or lead IDs are printed or logged.

Success == difference is 0. Anything else needs investigation before this is
considered verified in production.
"""
import argparse
import os
import sys

import requests


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="https://leadprofile.goodcalls.in")
    args = ap.parse_args()

    cookie = os.environ.get("SESSION_COOKIE")
    if not cookie:
        print("SESSION_COOKIE env var not set — export the `session` cookie from an "
              "authenticated browser tab first.", file=sys.stderr)
        sys.exit(1)
    headers = {"Cookie": cookie}

    leads_resp = requests.get(f"{args.base_url}/api/leads", headers=headers, timeout=60)
    leads_resp.raise_for_status()
    leads_rows = leads_resp.json()

    pipeline_resp = requests.get(f"{args.base_url}/api/pipeline", headers=headers, timeout=120)
    pipeline_resp.raise_for_status()
    pipeline_leads = pipeline_resp.json()["leads"]

    # Leads tab canonical count: resolved_stage == "Pre Site Visit", default view
    # (no dedup — every row counts, matching duplicate-removal-off).
    leads_by_project = {}
    for row in leads_rows:
        if row.get("resolved_stage") == "Pre Site Visit":
            proj = row.get("resolved_project") or "(unresolved)"
            leads_by_project[proj] = leads_by_project.get(proj, 0) + 1
    leads_total = sum(leads_by_project.values())

    # Pipeline canonical count: achieved.siteVisit, one row per (phone,project)
    # doc — /api/pipeline already returns one entry per lead_profiles doc, so no
    # further dedup is needed to match the Leads tab's default view.
    pipeline_by_project = {}
    for l in pipeline_leads:
        if l["achieved"].get("siteVisit"):
            # pipeline.py returns project lower-cased; leads_by_project keys are
            # upper-cased (lead_identity's convention) — normalise before comparing.
            proj = (l.get("project") or "(unresolved)").upper()
            pipeline_by_project[proj] = pipeline_by_project.get(proj, 0) + 1
    pipeline_total = sum(pipeline_by_project.values())

    print(f"base_url: {args.base_url}")
    print(f"date_range: none applied (full snapshot)")
    print(f"project_scope: all")
    print(f"leads_tab_pre_site_visit_count: {leads_total}")
    print(f"  by project: {dict(sorted(leads_by_project.items()))}")
    print(f"pipeline_booked_site_visit_count: {pipeline_total}")
    print(f"  by project: {dict(sorted(pipeline_by_project.items()))}")
    diff = pipeline_total - leads_total
    print(f"difference: {diff}")

    if diff == 0:
        print("PARITY CONFIRMED — difference is 0.")
    else:
        print("PARITY FAILED — non-zero difference. Non-PII mismatch categories:")
        all_projects = set(leads_by_project) | set(pipeline_by_project)
        for proj in sorted(all_projects):
            l, p = leads_by_project.get(proj, 0), pipeline_by_project.get(proj, 0)
            if l != p:
                print(f"  project={proj}: leads={l} pipeline={p} delta={p - l}")
        sys.exit(1)


if __name__ == "__main__":
    main()
