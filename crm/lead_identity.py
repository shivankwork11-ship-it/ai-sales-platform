"""Canonical, dependency-free lead-normalisation rules.

The Leads tab (dashboard.py + crm_v2.py) and the Pipeline tab (pipeline.py) both
need to answer "what project is this lead" and "what CRM stage does it hold" —
these MUST be one implementation, not two that can silently drift. This module
is that one implementation.

Deliberately imports NOTHING beyond the stdlib: no Flask, no requests, no
pymongo, no dashboard.py/crm_v2.py/pipeline.py. That makes it safe to import
from anywhere, including the Render refresh worker (which lacks `requests` —
see crm_v2.py's module docstring) and any future non-web context.
"""
import re

KNOWN_PROJECTS = {"LOFT", "SPECTRA", "BROADWAY", "LANDMARK", "LEGACY"}

PRE_SITE_VISIT = "Pre Site Visit"
POST_SITE_VISIT = "Post Site Visit"


def normalise_phone(raw):
    """Last-10-digit phone key, or "" if fewer than 10 digits (can't key on it)."""
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits[-10:] if len(digits) >= 10 else ""


def resolve_project(project_field, lead_id, campaign_name="", *, id_is_bare_plid=False):
    """The project a lead belongs to. Canonical port of the Leads tab's
    `_leadProject` (dashboard.py JS) — same 4 fallbacks, same order:

    1. `project_field`, if it's already a known project name.
    2. The PROJECT suffix on `lead_id`'s "...-PROJECT" tail, if `lead_id` is
       Zoho-sourced (see `id_is_bare_plid` below for what that means here).
    3. A known project keyword found in `project_field` + `campaign_name`.
    4. "LOFT", if the lead isn't Zoho-sourced at all (a website/loft lead) —
       else unresolvable.

    `id_is_bare_plid`: lead_profiles rows carry `lead_id = "ZOHO-<plid>-PROJECT"`
    (the "ZOHO-" prefix marks "this is a Zoho lead"), so step 2/4 there gate on
    that literal prefix. Zoho_Database.leads rows are ALL Zoho leads by
    definition, but their own `_id` is the bare `"<plid>-PROJECT"` (crm_api.py's
    `_mirror_profile` is what ADDS the "ZOHO-" prefix when it copies the plid
    into lead_profiles) — no prefix to check there, so pass `id_is_bare_plid=True`
    to treat every id in that collection as Zoho-sourced unconditionally, rather
    than defaulting it to "LOFT" for lack of a prefix it was never going to have.

    Returns "" (never None) when unresolvable, matching the JS, which always
    returns a string.
    """
    stored = (project_field or "").strip().upper()
    if stored in KNOWN_PROJECTS:
        return stored
    lid = str(lead_id or "")
    is_zoho = id_is_bare_plid or lid.upper().startswith("ZOHO-")
    if is_zoho:
        suffix = lid.rsplit("-", 1)[-1].upper()
        if suffix in KNOWN_PROJECTS:
            return suffix
    hay = f"{project_field or ''} {campaign_name or ''}".upper()
    for k in sorted(KNOWN_PROJECTS):      # sorted: stable pick if two names collide
        if k in hay:
            return k
    return "" if is_zoho else "LOFT"


def resolve_display_stage(stage, prd_stage, stage_locked):
    """Which stage to SHOW for a lead. The relay owns `prd_stage` and rewrites it
    on every sync; a manual dashboard change survives in the `stage` mirror and
    sets `stage_locked`. For a locked lead, show `stage`; otherwise the live
    relay value `prd_stage` wins. (Same rule as crm_v2's original _display_stage
    — this is now that function's one implementation.)
    """
    if stage_locked:
        return stage or prd_stage
    return prd_stage or stage


def build_phone_project_key(phone10, project):
    return f"{phone10}|{project}"


def is_pre_site_visit(stage):
    return stage == PRE_SITE_VISIT


def is_post_site_visit(stage):
    return stage == POST_SITE_VISIT
