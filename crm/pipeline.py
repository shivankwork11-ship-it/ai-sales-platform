"""Pipeline — lead pivot dashboard, ported from a standalone Node dashboard.

Reads analytics_db.lead_profiles (the same cache every other tab reads, kept warm
by the existing daily cron at /api/cron/refresh) for calls/WhatsApp, plus
Zoho_Database.leads for the CRM stage — no new cron, no change to any existing
route. Call-outcome bucketing mirrors the refined rules from the standalone
dashboard rather than this app's simpler CALL_* buckets (bulk_calls.py /
dashboard.py):
  - DNC_REQUESTED counts as "answered" (a human did pick up) and outranks a plain
    NOT_INTERESTED when classifying a lead's single best outcome.
  - VOICEMAIL is its own "not answered" category (a machine picking up isn't a
    real answer), instead of being uncategorized.
  - "Booked Site Visit" (CRM stage == Pre Site Visit) and "Post Site Visit" (CRM
    stage == Post Site Visit) are read per phone+project from Zoho_Database.leads
    via lead_identity — NOT the PRE_SITE/VIRTUAL_TOUR call outcome Booked Site
    Visit used to be. lead_profiles' embedded ai_calls.call_conversations carries
    no per-call project tag and is duplicated identically across every
    project-sibling profile for a phone, so the old rule credited one real
    site-visit call to every project that phone appeared under (overcount). The
    CRM's own stage is already one row per phone+project, so it can't bleed.

All aggregation (day x project x stage KPI totals) happens client-side in
PIPELINE_HTML's own JS from the flat per-lead payload — this module only
computes each lead's `achieved` flags, `status`, and normalized calls/whatsapp
arrays, mirroring the Node version's leads_pipeline.js division of responsibility.
"""
import re
from datetime import datetime, timezone

from flask import Blueprint, jsonify, render_template_string, session

import lead_identity

pipeline_bp = Blueprint("pipeline", __name__)
_client = None

KNOWN_PROJECTS = lead_identity.KNOWN_PROJECTS

# Business-confirmed buckets (see module docstring) — deliberately NOT the same as
# this app's own CALL_ANSWERED/CALL_NOT_ANSWERED/CALL_NOT_CONNECTED in dashboard.py.
ANSWERED_OUTCOMES = {"CONNECTED", "AUTO_CALLBACK", "PRE_SITE", "NOT_INTERESTED",
                     "DROPPED", "VIRTUAL_TOUR", "DNC_REQUESTED"}
CALLBACK_OUTCOMES = {"AUTO_CALLBACK"}

# "Booked Site Visit" is the CRM's own stage (lead_identity.is_pre_site_visit),
# NOT an AI-call outcome — the same field the Leads tab's Stage column shows.
#
# Deliberately "Pre Site Visit" ONLY, not Pre+Post: this card has to tie out exactly
# with the Leads tab's "Pre Site Visit" filter, which sales ops reconcile by eye.
# Including Post Site Visit is defensible on its own terms (the visit WAS booked, it
# has simply already happened) but it silently breaks that parity — don't, without
# also adding a Post Site Visit filter to the Leads tab.

STATUS_PRIORITY = ["postSiteVisit", "siteVisit", "callback", "dnc", "notInterested",
                    "wa", "waSent", "picked", "called", "leads"]


def _lead_id_display(doc):
    """Lead ID shown in place of the phone: Inncircles source id, else the PLID
    (this profile's id) with its "ZOHO-" prefix stripped (ZOHO-1734-SPECTRA →
    1734-SPECTRA). Mirrors the Leads-tab / export rule in dashboard.py."""
    if "inncircles" in (doc.get("lead_source") or "").lower() and doc.get("source_lead_id"):
        return str(doc.get("source_lead_id"))
    lid = str(doc.get("lead_id") or doc.get("_id") or "")
    return re.sub(r"^ZOHO-", "", lid, flags=re.I)


def init_app(app, client):
    """Register routes early (mirrors bulk_calls.py/crm_api.py/crm_v2.py). Must be
    paired with configure(SHARED_HEAD) once SHARED_HEAD exists (dashboard.py defines
    it much later in the file than where the other blueprints register) — the page
    route 404s with a clear message if configure() hasn't run yet, rather than
    silently serving a broken page."""
    global _client
    _client = client
    app.register_blueprint(pipeline_bp)


def configure(shared_head):
    """Call once, after dashboard.py's SHARED_HEAD is defined, to finish building
    PIPELINE_HTML. Split from init_app() purely because of that definition-order
    constraint — not a runtime/request-time concern."""
    global PIPELINE_HTML
    PIPELINE_HTML = _PIPELINE_HTML_TEMPLATE.replace("%%SHARED_HEAD%%", shared_head)


def _profiles():
    return _client["analytics_db"]["lead_profiles"]


def _stage_map():
    """``{"<last10phone>|<PROJECT>": stage}`` for every CRM lead, via lead_identity
    — the SAME canonical rules crm_v2.build_stage_map uses, so the Leads tab's
    Stage column and this can't disagree.

    Deliberately reads Zoho_Database.leads directly rather than calling
    crm_v2.build_stage_map(): that module imports ``requests``, which isn't
    installed on the Render refresh worker, so importing crm_v2 here would break
    the whole app at import time in that environment. lead_identity itself has
    zero dependencies and is safe to import anywhere.
    """
    out = {}
    for lead in _client["Zoho_Database"]["leads"].find(
        {}, {"phone": 1, "project": 1, "stage": 1, "prd_stage": 1, "stage_locked": 1}
    ):
        last10 = lead_identity.normalise_phone(lead.get("phone"))
        if not last10:
            continue
        proj = lead_identity.resolve_project(
            lead.get("project"), lead.get("_id"), id_is_bare_plid=True)
        stage = lead_identity.resolve_display_stage(
            lead.get("stage"), lead.get("prd_stage"), lead.get("stage_locked"))
        out[lead_identity.build_phone_project_key(last10, proj)] = stage
    return out


def _resolve_project(doc):
    """The project a lead_profiles doc belongs to, or None if unresolvable
    (lead_identity.resolve_project returns "" for that case; pipeline needs a
    droppable sentinel, so translate "" -> None here — dashboard.py's /api/leads
    keeps the "" as-is, since the Leads tab doesn't drop rows over project).

    Delegates to lead_identity.resolve_project — the canonical port of the Leads
    tab's `_leadProject` — so both tabs bucket the same lead under the same
    project. (Before that module existed, pipeline used `doc.project` alone and
    silently DROPPED every lead whose project field was blank; the Leads tab kept
    them via these fallbacks, so pipeline under-counted.)
    """
    project = lead_identity.resolve_project(
        doc.get("project"), doc.get("lead_id") or doc.get("_id"), doc.get("campaign_name"))
    return project or None


def _to_dt(v):
    """Normalize a Mongo datetime OR an ISO string (both shapes appear in
    lead_profiles depending on which upstream source populated the field) into
    an aware-UTC datetime, or None."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str) and v.strip():
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def _build_lead(doc, stage_map):
    """One analytics_db.lead_profiles document -> the shape PIPELINE_HTML's JS
    expects (mirrors the Node dashboard's per-lead object). Returns None for
    profiles missing a usable project or creation date (excluded, not zeroed)."""
    project_raw = _resolve_project(doc)
    if project_raw is None:
        return None
    born = _to_dt(doc.get("lead_created_at"))
    if not born:
        return None

    ai = doc.get("ai_calls") or {}
    calls = []
    for c in (ai.get("call_conversations") or []):
        at = _to_dt(c.get("started_at"))
        if not at:
            continue
        turns = c.get("transcript") or []
        transcript_text = "\n".join(
            f"{'Agent' if t.get('type') == 'outbound' else 'Customer'}: {t.get('message', '')}"
            for t in turns if t.get("message")
        )
        calls.append({
            "id": str(c.get("call_sid") or ""),
            "outcome": c.get("call_outcome"),
            "at": at,
            "durationSecs": c.get("duration_secs") or 0,
            "transcript": transcript_text,
            "recordingUrl": c.get("recording_url"),
        })
    calls.sort(key=lambda c: c["at"])

    wa = doc.get("whatsapp") or {}
    whatsapp = []
    for m in (wa.get("user_conversation_history") or []):
        at = _to_dt(m.get("date"))
        if not at:
            continue
        whatsapp.append({
            "direction": "received" if m.get("type") == "inbound" else "sent",
            "text": m.get("message", ""),
            "at": at,
        })
    whatsapp.sort(key=lambda m: m["at"])

    achieved = {"leads": True}
    if calls:
        achieved["called"] = True
    if any(c["outcome"] in ANSWERED_OUTCOMES for c in calls):
        achieved["picked"] = True
    phone10 = lead_identity.normalise_phone(doc.get("lead_phone_number"))
    key = lead_identity.build_phone_project_key(phone10, project_raw)
    crm_stage = stage_map.get(key)
    if lead_identity.is_pre_site_visit(crm_stage):
        achieved["siteVisit"] = True
    if lead_identity.is_post_site_visit(crm_stage):
        achieved["postSiteVisit"] = True
    if any(c["outcome"] in CALLBACK_OUTCOMES for c in calls):
        achieved["callback"] = True
    if (wa.get("outbound_count") or 0) > 0:
        achieved["waSent"] = True
    if (wa.get("user_conversation_depth") or 0) > 0:
        achieved["wa"] = True
    achieved["engaged"] = bool(achieved.get("picked") or achieved.get("wa"))

    # No call-outcome-based site-visit revocation any more: the CRM stage IS the
    # answer, and a lead that later declined already reads "Not Interested" there.
    # (STATUS_PRIORITY still lists dnc/notInterested — nothing sets them now, and
    # zero-count buckets simply don't render.)
    status = next((k for k in STATUS_PRIORITY if achieved.get(k)), "leads")

    return {
        "id": str(doc.get("lead_id") or doc.get("_id") or ""),
        "name": (doc.get("lead_name") or "").strip() or "Unnamed lead",
        "phone": doc.get("lead_phone_number") or "",       # kept for dedup keys only, not displayed
        "leadId": _lead_id_display(doc),
        "project": project_raw.lower(),
        "bornDate": born.isoformat(),
        "source": doc.get("lead_source") or "CRM",
        "agent": "ASBL Sales Ops",
        "achieved": achieved,
        "status": status,
        "calls": [{**c, "at": c["at"].isoformat()} for c in calls],
        "whatsapp": [{**m, "at": m["at"].isoformat()} for m in whatsapp],
    }


@pipeline_bp.route("/api/pipeline")
def api_pipeline():
    total = 0
    skipped = 0
    leads = []
    projection = {
        "lead_id": 1, "lead_name": 1, "lead_phone_number": 1, "project": 1,
        "lead_created_at": 1, "lead_source": 1, "source_lead_id": 1, "ai_calls": 1, "whatsapp": 1,
        "campaign_name": 1,        # only for _resolve_project's keyword fallback
    }
    stage_map = _stage_map()
    for doc in _profiles().find({"bulk_manual": {"$ne": True}}, projection):
        total += 1
        built = _build_lead(doc, stage_map)
        if built is None:
            skipped += 1
        else:
            leads.append(built)

    return jsonify({
        "leads": leads,
        "meta": {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "totalProfiles": total,
            "usableLeads": len(leads),
            "skippedLeads": skipped,
        },
    })


@pipeline_bp.route("/pipeline")
def pipeline_page():
    if PIPELINE_HTML is None:
        return "Pipeline not configured — configure(SHARED_HEAD) was never called.", 500
    return render_template_string(PIPELINE_HTML,
        user_name=session.get("name", ""),
        user_role=session.get("role", ""))


PIPELINE_HTML = None  # built in init_app() once SHARED_HEAD is available

_PIPELINE_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <title>ASBL · Pipeline</title>
  %%SHARED_HEAD%%
</head>
<body>
<style>
  /* Bridge: map this component library's CSS variable names onto the host app's
     own design tokens (SHARED_HEAD) so this page inherits its exact palette/fonts
     instead of carrying a second, competing color system. --bg/--surface/--accent
     etc. are deliberately NOT redefined here — they fall through to the host's
     :root unchanged, which is what makes this feel like one product, not a widget
     bolted on. */
  :root {
    --surface-alt: var(--surface-2);
    --ink: var(--text);
    --ink-muted: var(--text-muted);
    --ink-faint: var(--text-dim);
    --line: var(--border);
    --line-strong: var(--border-2);
    --accent-ink: #FFFFFF;
    --accent-soft: var(--accent-dim);
    --cyan: var(--blue);
    --success: var(--green);
    --success-soft: var(--green-dim);
    --warning: var(--accent);
    --warning-soft: var(--amber-dim);
    --critical: var(--red);
    --critical-soft: var(--red-dim);
    --proj-loft: #2E8C86;
    --proj-legacy: #8B5FA0;
    --proj-broadway: #A68A3D;
    --proj-spectra: #4E6FA3;
    --proj-landmark: #B35A63;
    --radius-s: 6px;
    --radius-m: 10px;
    --shadow-card: 0 1px 2px rgba(28,24,20,0.06), 0 4px 14px rgba(28,24,20,0.05);
  }

* { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--ink);
    font-size: 14px;
    line-height: 1.45;
    -webkit-font-smoothing: antialiased;
  }
  .app { max-width: 1360px; margin: 0 auto; padding: 28px 28px 60px; }

  /* ---------- Header ---------- */
  .topbar {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 24px;
    flex-wrap: wrap;
    margin-bottom: 22px;
  }
  .eyebrow {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.09em;
    text-transform: uppercase;
    color: var(--accent);
    margin: 0 0 6px;
  }
  h1 {
    font-size: 26px;
    font-weight: 650;
    letter-spacing: -0.01em;
    margin: 0;
    text-wrap: balance;
    color: var(--ink);
  }
  .subtitle {
    margin: 6px 0 0;
    color: var(--ink-muted);
    font-size: 13px;
    max-width: 60ch;
  }
  .meta {
    text-align: right;
    font-size: 12px;
    color: var(--ink-faint);
  }
  .meta strong { color: var(--ink-muted); font-weight: 600; }

  /* Page title block — its own layout so the text can't collide with the meta
     or the controls row (previously shared .topbar with the nav header). */
  .pagehead {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px 40px;
    flex-wrap: wrap;
    margin: 0 0 22px;
  }
  .headtext { flex: 1 1 360px; min-width: 0; }
  .pagehead .meta { flex: 0 0 auto; }

  /* ---------- Controls row ---------- */
  .controls {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 20px;
  }
  .chips { display: flex; gap: 8px; flex-wrap: wrap; }
  .daterange { display: flex; align-items: center; gap: 6px; }
  .dateinput {
    border: 1px solid var(--border, #d8d1c5);
    border-radius: 8px; padding: 6px 9px;
    font: inherit; font-size: 12px; color: var(--ink);
    background: var(--surface, #faf7f2); cursor: pointer;
  }
  .daterange-sep { color: var(--ink-faint); font-size: 12px; }
  #range-clear {
    border: none; background: none; color: var(--ink-faint);
    cursor: pointer; font-size: 14px; padding: 0 2px; line-height: 1;
  }
  #range-clear:hover { color: var(--ink); }
  .chip {
    appearance: none;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    font: inherit;
    font-size: 12.5px;
    font-weight: 600;
    padding: 6px 12px 6px 9px;
    border-radius: 999px;
    border: 1.4px solid var(--proj-color, var(--line-strong));
    background: var(--surface);
    color: var(--ink);
    opacity: 1;
    transition: opacity .15s ease, background .15s ease;
  }
  .chip .dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--proj-color);
    flex: none;
  }
  .chip.off { opacity: 0.38; }
  .chip.off .dot { background: var(--ink-faint); }

  /* ---------- KPI cards ---------- */
  .kpis {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(148px, 1fr));
    gap: 12px;
    margin-bottom: 22px;
  }
  .kpi {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius-m);
    padding: 16px 18px;
    box-shadow: var(--shadow-card);
    position: relative;
    overflow: hidden;
  }
  .kpi .stage-label {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink-faint);
    margin: 0 0 8px;
  }
  .kpi .value-row {
    display: flex;
    align-items: baseline;
    gap: 9px;
  }
  .kpi .value {
    font-size: 28px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.015em;
    color: var(--ink);
  }
  .kpi .delta {
    font-size: 12px;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 999px;
    font-variant-numeric: tabular-nums;
  }
  .kpi .delta.pos { background: var(--success-soft); color: var(--success); }
  .kpi .delta.neg { background: var(--critical-soft); color: var(--critical); }
  .kpi .conv-of {
    margin-top: 3px;
    font-size: 11.5px;
    color: var(--ink-faint);
  }
  .spark { position: absolute; right: 14px; bottom: 12px; opacity: 0.9; }

  /* ---------- Panel shell ---------- */
  .panel {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius-m);
    box-shadow: var(--shadow-card);
    overflow: hidden;
  }
  .panel-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 18px;
    border-bottom: 1px solid var(--line);
    gap: 12px;
    flex-wrap: wrap;
  }
  .panel-head h2 {
    font-size: 14.5px;
    font-weight: 650;
    margin: 0;
    color: var(--ink);
  }
  .panel-head .hint { font-size: 12px; color: var(--ink-faint); }
  .export-btn {
    appearance: none; border: 1px solid var(--line-strong); background: var(--surface);
    color: var(--ink); font: inherit; font-size: 12px; font-weight: 600;
    padding: 6px 12px; border-radius: var(--radius-s); cursor: pointer;
  }
  .export-btn:hover { background: var(--accent-soft); border-color: var(--accent); }
  .attempts-times { color: var(--ink-muted); font-weight: 500; }

  /* Shared table styling — used by the Call Attempts table (table.pivot is a
     misnomer now: the actual pivot table was removed, but the class stuck). */
  .table-scroll { overflow: auto; max-height: 560px; }
  table.pivot {
    border-collapse: separate;
    border-spacing: 0;
    width: 100%;
    font-variant-numeric: tabular-nums;
  }
  table.pivot th, table.pivot td {
    padding: 7px 11px;
    text-align: right;
    white-space: nowrap;
    font-size: 12.5px;
    border-bottom: 1px solid var(--line);
  }
  table.pivot td:first-child, table.pivot th:first-child {
    text-align: left;
    position: sticky;
    left: 0;
    background: var(--surface);
    z-index: 2;
    border-right: 1px solid var(--line-strong);
    font-weight: 600;
  }
  table.pivot thead th {
    position: sticky;
    background: var(--surface-alt);
    z-index: 3;
    font-weight: 700;
    color: var(--ink-muted);
  }
  table.pivot thead tr:first-child th { top: 0; }
  table.pivot thead tr:nth-child(2) th {
    top: 33px;
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    color: var(--proj-color, var(--ink-faint));
    border-bottom: 1.5px solid var(--line-strong);
  }
  table.pivot thead tr:first-child th:first-child,
  table.pivot thead tr:nth-child(2) th:first-child {
    z-index: 4;
    background: var(--surface-alt);
  }
  table.pivot tbody tr:nth-child(even) td { background: rgba(0,0,0,0.012); }
  :root[data-theme="dark"] table.pivot tbody tr:nth-child(even) td,
  @media (prefers-color-scheme: dark) { table.pivot tbody tr:nth-child(even) td { background: rgba(255,255,255,0.02); } }
  table.pivot tbody tr:hover td { background: var(--accent-soft) !important; }
  table.pivot tbody tr:nth-child(even) td:first-child,
  table.pivot tbody tr:hover td:first-child { background: inherit; }
  table.pivot tfoot td {
    position: sticky;
    bottom: 0;
    background: var(--surface-alt);
    font-weight: 700;
    border-top: 1.5px solid var(--line-strong);
    border-bottom: none;
    color: var(--ink);
  }
  footer {
    margin-top: 18px;
    display: flex;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    font-size: 11.5px;
    color: var(--ink-faint);
  }
  footer a { color: var(--cyan); }

  @media (max-width: 760px) {
    .kpis { grid-template-columns: repeat(2, 1fr); }
    .app { padding: 18px 14px 40px; }
  }

  /* ---------- Interactivity affordances ---------- */
  .kpi.clickable {
    cursor: pointer;
    transition: transform .12s ease, box-shadow .12s ease;
  }
  .kpi.clickable:hover { transform: translateY(-1px); box-shadow: 0 2px 4px rgba(22,40,63,0.1), 0 8px 20px rgba(22,40,63,0.09); }

  /* ---------- Drawer ---------- */
  .drawer-overlay {
    position: fixed; inset: 0;
    background: rgba(14,27,46,0.4);
    opacity: 0; pointer-events: none;
    transition: opacity .18s ease;
    z-index: 30;
  }
  .drawer-overlay.open { opacity: 1; pointer-events: auto; }
  .drawer {
    position: fixed; top: 0; right: 0; bottom: 0;
    width: min(460px, 92vw);
    background: var(--surface);
    border-left: 1px solid var(--line);
    box-shadow: -8px 0 30px rgba(22,40,63,0.18);
    transform: translateX(100%);
    transition: transform .2s ease;
    z-index: 31;
    display: flex; flex-direction: column;
  }
  .drawer.open { transform: translateX(0); }
  .drawer-head {
    display: flex; align-items: center; gap: 10px;
    padding: 14px 16px;
    border-bottom: 1px solid var(--line);
    flex: none;
  }
  .drawer-back {
    appearance: none; border: none; background: var(--surface-alt);
    color: var(--ink-muted); font: inherit; font-size: 12px; font-weight: 600;
    padding: 6px 10px; border-radius: var(--radius-s); cursor: pointer;
  }
  .drawer-back:hover { color: var(--ink); }
  .drawer-title { font-weight: 700; font-size: 13.5px; flex: 1; }
  .drawer-title .sub { display: block; font-weight: 500; font-size: 11.5px; color: var(--ink-faint); margin-top: 2px; }
  .drawer-close {
    appearance: none; border: none; background: transparent;
    color: var(--ink-faint); font-size: 20px; line-height: 1; cursor: pointer;
    padding: 2px 6px; border-radius: var(--radius-s);
  }
  .drawer-close:hover { color: var(--ink); background: var(--surface-alt); }
  .drawer-body { overflow-y: auto; padding: 8px 0; flex: 1; }

  .lead-row {
    display: flex; align-items: center; gap: 11px;
    padding: 10px 16px;
    border-bottom: 1px solid var(--line);
    cursor: pointer;
  }
  .lead-row:hover { background: var(--accent-soft); }
  .lead-avatar {
    width: 32px; height: 32px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 12px; font-weight: 700; color: var(--accent-ink);
    background: var(--proj-color); flex: none;
  }
  .lead-main { flex: 1; min-width: 0; }
  .lead-name { font-weight: 650; font-size: 13px; color: var(--ink); }
  .lead-sub {
    font-size: 11.5px; color: var(--ink-faint);
    font-variant-numeric: tabular-nums;
    display: flex; gap: 6px; align-items: center;
  }
  .status-pill {
    font-size: 10.5px; font-weight: 700;
    padding: 2px 8px; border-radius: 999px;
    white-space: nowrap; flex: none;
  }
  .status-pill.neutral { background: var(--surface-alt); color: var(--ink-muted); }
  .status-pill.info { background: rgba(37,107,121,0.14); color: var(--cyan); }
  .status-pill.success { background: var(--success-soft); color: var(--success); }
  .status-pill.warning { background: var(--warning-soft); color: var(--warning); }
  .status-pill.critical { background: var(--critical-soft); color: var(--critical); }
  .drawer-empty { padding: 40px 20px; text-align: center; color: var(--ink-faint); font-size: 13px; }

  /* ---------- Outcome sub-filters (Picked Up drill-down) ---------- */
  .outcome-filters {
    display: flex; flex-wrap: wrap; gap: 6px;
    padding: 12px 16px; border-bottom: 1px solid var(--line);
  }
  .outcome-chip {
    appearance: none; border: 1px solid var(--line-strong); background: var(--surface);
    color: var(--ink-muted); font: inherit; font-size: 11.5px; font-weight: 600;
    padding: 4px 10px; border-radius: 999px; cursor: pointer;
  }
  .outcome-chip:hover { color: var(--ink); }
  .outcome-chip.active { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }

  .lead-detail { padding: 18px 18px 6px; }
  .ld-head { display: flex; align-items: center; gap: 12px; margin-bottom: 4px; }
  .ld-avatar {
    width: 44px; height: 44px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 15px; font-weight: 700; color: var(--accent-ink);
    background: var(--proj-color); flex: none;
  }
  .ld-name { font-size: 16px; font-weight: 700; }
  .ld-phone { font-size: 12.5px; color: var(--ink-muted); font-variant-numeric: tabular-nums; }
  .ld-meta {
    display: flex; flex-wrap: wrap; gap: 7px;
    margin: 12px 0 6px;
  }
  .ld-meta .status-pill { font-size: 11px; padding: 3px 9px; }
  .ld-agent { font-size: 11.5px; color: var(--ink-faint); margin: 2px 0 14px; }
  .ld-section-label {
    font-size: 11px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;
    color: var(--ink-faint); margin: 6px 18px 10px;
  }
  /* ---------- Calls section ---------- */
  .calls-list { padding: 0 18px 6px; }
  .call-card {
    border: 1px solid var(--line); border-radius: var(--radius-m);
    padding: 12px 14px; margin-bottom: 12px;
  }
  .call-card-head {
    display: flex; align-items: center; justify-content: space-between;
    gap: 8px; flex-wrap: wrap; margin-bottom: 9px;
  }
  .call-meta { font-size: 11px; color: var(--ink-faint); font-variant-numeric: tabular-nums; }
  .call-card audio { width: 100%; height: 32px; margin-bottom: 9px; display: block; }
  .call-transcript {
    white-space: pre-wrap; font-size: 12px; line-height: 1.55; color: var(--ink-muted);
    background: var(--surface-alt); border-radius: 6px; padding: 10px 12px;
    max-height: 220px; overflow-y: auto;
  }
  .call-transcript-empty { color: var(--ink-faint); font-style: italic; }
  .call-visit-info {
    font-size: 12px; color: var(--success); background: var(--success-soft);
    border-radius: 6px; padding: 7px 10px; margin-bottom: 9px;
  }
  .call-recording-expired {
    font-size: 11.5px; color: var(--ink-faint); background: var(--surface-alt);
    border-radius: 6px; padding: 7px 10px; margin-bottom: 9px; font-style: italic;
  }

  /* ---------- WhatsApp chat view ---------- */
  .wa-thread { display: flex; flex-direction: column; gap: 9px; padding: 4px 18px 22px; }
  .wa-bubble { max-width: 78%; padding: 8px 12px; border-radius: 13px; font-size: 12.5px; line-height: 1.45; }
  .wa-bubble.sent { align-self: flex-end; background: var(--accent-soft); border-bottom-right-radius: 3px; }
  .wa-bubble.received { align-self: flex-start; background: var(--surface-alt); border-bottom-left-radius: 3px; }
  .wa-text { white-space: pre-wrap; word-break: break-word; }
  .wa-time { font-size: 10px; color: var(--ink-faint); margin-top: 4px; text-align: right; font-variant-numeric: tabular-nums; }
</style>

<header class="topbar">
    <div style="display:flex;align-items:center;gap:0;">
      <div class="topbar-logo" onclick="location.href='/'" style="cursor:pointer;" title="Go to Leads">
        <div class="logo-mark">AS</div>
        <div class="logo-text">
          <span class="logo-primary">ASBL</span>
          <span class="logo-sub">Lead Intelligence Platform <span style="font-size:9px;opacity:0.6;letter-spacing:0.05em;">build&nbsp;pipeline-leadid</span></span>
        </div>
      </div>
      <nav class="nav-links">
        <a href="/" class="nav-link">Leads</a>
        <a href="/bulk-calls" class="nav-link">Bulk Calls</a>
        <a href="/qa" class="nav-link">QA</a>
        <a href="/pipeline" class="nav-link active">Pipeline</a>
        <a href="/main-dashboard" class="nav-link">Main Dashboard</a>
      </nav>
    </div>
  </header>
  <div class="app">
  <div class="pagehead">
    <div class="headtext">
      <p class="eyebrow">Sales Ops · Live</p>
      <h1>ASBL Lead Pipeline</h1>
      <p class="subtitle">Daily lead flow from first contact through WhatsApp reply, split by project. Cohort view — for leads born on a given day, whether they ever reached each stage.</p>
    </div>
    <div class="meta">
      Loaded <strong id="gen-date">…</strong><br>
      Source: <strong>analytics_db.lead_profiles</strong>
    </div>
  </div>

  <div class="controls">
    <div class="daterange">
      <input type="date" id="range-from" class="dateinput" aria-label="From date" title="From (born date)">
      <span class="daterange-sep">→</span>
      <input type="date" id="range-to" class="dateinput" aria-label="To date" title="To (born date)">
      <button id="range-clear" type="button" title="Clear date range">✕</button>
    </div>
    <div class="chips" id="project-chips"></div>
  </div>

  <div class="kpis" id="kpi-row"></div>
  <p class="hint" id="load-status" style="margin:-14px 0 14px;"></p>

  <div class="panel" id="attempts-panel">
    <div class="panel-head">
      <h2>Call attempts by lead, by day</h2>
      <span class="hint" id="attempts-count-hint"></span>
      <button class="export-btn" id="export-attempts-btn">Export CSV</button>
    </div>
    <div class="table-scroll">
      <table class="pivot" id="attempts-table"></table>
    </div>
  </div>

  <footer>
    <span id="data-quality-note">Loading data quality notes…</span>
    <span>Click any number to see the leads behind it. Stages: Lead Count → Called → Picked Up → WhatsApp Sent → WhatsApp Reply → Site Visit / Callback</span>
  </footer>
</div>

<div class="drawer-overlay" id="drawer-overlay"></div>
<aside class="drawer" id="drawer">
  <div class="drawer-head">
    <button class="drawer-back" id="drawer-back" style="display:none;">← Back</button>
    <div class="drawer-title" id="drawer-title">Leads</div>
    <button class="export-btn" id="drawer-export-btn" style="display:none;">Export CSV</button>
    <button class="drawer-close" id="drawer-close" aria-label="Close">×</button>
  </div>
  <div class="drawer-body" id="drawer-body"></div>
</aside>

<script>
(function () {
  const PROJECTS = [
    { key: 'loft',     name: 'Loft',     color: 'var(--proj-loft)',     base: 5  },
    { key: 'legacy',   name: 'Legacy',   color: 'var(--proj-legacy)',   base: 4  },
    { key: 'broadway', name: 'Broadway', color: 'var(--proj-broadway)', base: 7  },
    { key: 'spectra',  name: 'Spectra',  color: 'var(--proj-spectra)',  base: 8  },
    { key: 'landmark', name: 'Landmark', color: 'var(--proj-landmark)', base: 4  },
  ];
  const PROJECT_BY_KEY = Object.fromEntries(PROJECTS.map(p => [p.key, p]));

  // convBase = which stage's total the "% of ___" conversion figure is measured against.
  // 'engaged' is a hidden helper stage (not its own column) — a real two-way conversation,
  // reached either by a picked-up call OR a WhatsApp reply. Site visit / callback asks can
  // come from either channel, so they're measured against this union, not "Picked Up" alone.
  const STAGES = [
    { key: 'leads',     label: 'Lead Count',        convBase: null },
    { key: 'called',    label: 'Called',             convBase: 'leads' },
    { key: 'picked',    label: 'Picked Up',          convBase: 'called' },
    { key: 'waSent',    label: 'WhatsApp Sent',      convBase: 'leads' },
    { key: 'wa',        label: 'WhatsApp Reply',     convBase: 'waSent' },
    { key: 'engaged',   label: 'Contacted (call or WhatsApp)', convBase: 'leads', hidden: true },
    { key: 'siteVisit', label: 'Booked Site Visit',  convBase: 'engaged' },
    { key: 'postSiteVisit', label: 'Post Site Visit', convBase: 'engaged' },
    { key: 'callback',  label: 'Asked for Callback', convBase: 'engaged' },
  ];
  const VISIBLE_STAGES = STAGES.filter(s => !s.hidden);
  const STAGE_BY_KEY = Object.fromEntries(STAGES.map(s => [s.key, s]));

  // a lead's displayed "status" is whichever of these it has reached, highest first — must
  // mirror the backend's STATUS_PRIORITY (leads_pipeline.js). dnc/notInterested are legacy —
  // nothing sets them since Booked Site Visit became CRM-stage-driven (the old call-outcome
  // revocation rule was their only source); zero-count buckets simply don't render.
  const STATUS_PRIORITY = ['postSiteVisit', 'siteVisit', 'callback', 'dnc', 'notInterested', 'wa', 'waSent', 'picked', 'called', 'leads'];
  const STATUS_LABEL = {
    leads: 'New Lead', called: 'Called', picked: 'Connected',
    waSent: 'WhatsApp Sent', wa: 'WhatsApp Replied',
    callback: 'Callback Requested', siteVisit: 'Site Visit Booked',
    postSiteVisit: 'Post Site Visit', dnc: 'DNC Requested', notInterested: 'Not Interested',
  };
  const STATUS_TONE = {
    leads: 'neutral', called: 'neutral', picked: 'info',
    waSent: 'neutral', wa: 'info', callback: 'warning', siteVisit: 'success',
    postSiteVisit: 'success', dnc: 'warning', notInterested: 'warning',
  };

  // local-time calendar-day key — deliberately avoids toISOString() so a
  // midnight-local bucket date and a mid-afternoon lead timestamp always agree
  function dayKey(d) {
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }

  let LEADS = [];
  let data = [];

  const state = {
    range: 'all',
    from: null,   // custom date range (overrides `range` when set)
    to: null,
    visible: new Set(PROJECTS.map(p => p.key)),
  };

  // ---- cohort aggregation: day x project x stage, derived straight from LEADS ----
  // "Born Date" cohort view — for leads created on a given day, how many ever reached each stage
  function buildCohortData() {
    const dayMap = new Map();
    LEADS.forEach(lead => {
      const key = dayKey(lead.bornDate);
      if (!dayMap.has(key)) {
        const stages = {};
        PROJECTS.forEach(p => { stages[p.key] = Object.fromEntries(STAGES.map(s => [s.key, 0])); });
        dayMap.set(key, { date: new Date(lead.bornDate.getFullYear(), lead.bornDate.getMonth(), lead.bornDate.getDate()), stages });
      }
      const bucket = dayMap.get(key).stages[lead.project];
      STAGES.forEach(s => { if (lead.achieved[s.key]) bucket[s.key] += 1; });
    });
    return Array.from(dayMap.values()).sort((a, b) => a.date - b.date);
  }

  async function loadDashboard() {
    const statusEl = document.getElementById('load-status');
    statusEl.textContent = 'Loading leads from MongoDB…';
    try {
      const res = await fetch('/api/pipeline');
      if (!res.ok) throw new Error(`API returned ${res.status}`);
      const payload = await res.json();

      LEADS = payload.leads.map(l => ({
        ...l,
        bornDate: new Date(l.bornDate),
        calls: l.calls.map(c => ({ ...c, at: new Date(c.at) })),
        whatsapp: l.whatsapp.map(m => ({ ...m, at: new Date(m.at) })),
      }));
      data = buildCohortData();

      document.getElementById('gen-date').textContent =
        new Date(payload.meta.generatedAt).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });

      const m = payload.meta;
      document.getElementById('data-quality-note').textContent =
        `${m.usableLeads} of ${m.totalProfiles} lead profiles usable (${m.skippedLeads} skipped — missing project/creation date). ` +
        `Reads the same analytics_db.lead_profiles cache as the Leads tab — refreshed by the existing daily cron.`;

      statusEl.textContent = '';
      render();
    } catch (err) {
      statusEl.textContent = `Failed to load: ${err.message}. Check /api/pipeline directly for the underlying error.`;
    }
  }

  // ---- project filter chips ----
  const chipRow = document.getElementById('project-chips');
  PROJECTS.forEach(p => {
    const b = document.createElement('button');
    b.className = 'chip';
    b.style.setProperty('--proj-color', p.color);
    b.innerHTML = '<span class="dot"></span>' + p.name;
    b.addEventListener('click', () => {
      if (state.visible.has(p.key)) { state.visible.delete(p.key); b.classList.add('off'); }
      else { state.visible.add(p.key); b.classList.remove('off'); }
      render();
    });
    chipRow.appendChild(b);
  });

  // No view-toggle any more: KPI cards + Call Attempts are the only two
  // sections, both always visible — nothing left to tab between.
  document.getElementById('export-attempts-btn').addEventListener('click', exportAttemptsCsv);

  // ---- Date-range picker (the sole range control) ----
  function _parseDateInput(v, endOfDay) {
    if (!v) return null;
    const [y, m, d] = v.split('-').map(Number);
    return endOfDay ? new Date(y, m - 1, d, 23, 59, 59, 999) : new Date(y, m - 1, d, 0, 0, 0, 0);
  }
  function onDateRangeChange() {
    state.from = _parseDateInput(document.getElementById('range-from').value, false);
    state.to   = _parseDateInput(document.getElementById('range-to').value, true);
    render();
  }
  document.getElementById('range-from').addEventListener('change', onDateRangeChange);
  document.getElementById('range-to').addEventListener('change', onDateRangeChange);
  document.getElementById('range-clear').addEventListener('click', () => {
    document.getElementById('range-from').value = '';
    document.getElementById('range-to').value = '';
    state.from = state.to = null;
    render();
  });

  function visibleProjects() { return PROJECTS.filter(p => state.visible.has(p.key)); }
  function rangedData() {
    // Custom date range wins over the preset when either bound is set.
    if (state.from || state.to) {
      const fromT = state.from ? state.from.getTime() : -Infinity;
      const toT   = state.to   ? state.to.getTime()   :  Infinity;
      return data.filter(e => { const t = e.date.getTime(); return t >= fromT && t <= toT; });
    }
    return data;  // no date range set → all
  }
  function rangeLabelText() {
    if (state.from || state.to) return `${state.from ? fmtDate(state.from) : '…'} – ${state.to ? fmtDate(state.to) : '…'}`;
    return 'all time';
  }
  function rangeSlug() {
    if (state.from || state.to) return `${state.from ? state.from.toISOString().slice(0,10) : 'start'}_to_${state.to ? state.to.toISOString().slice(0,10) : 'end'}`;
    return 'all';
  }
  function stageLabel(key) { return STAGE_BY_KEY[key].label; }
  function fmtDate(d) { return d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' }); }
  function fmtDateTime(d) {
    return d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' }) + ' · ' +
      d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' });
  }
  function fmtTime(d) { return d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' }); }

  // Same "answered is answered" bucket used everywhere else (backend ANSWERED_OUTCOMES) — a call
  // counts as "picked up" regardless of how the conversation went.
  const PICKED_UP_OUTCOMES = new Set(['CONNECTED', 'AUTO_CALLBACK', 'PRE_SITE', 'NOT_INTERESTED', 'DROPPED', 'VIRTUAL_TOUR', 'DNC_REQUESTED']);

  // One row per (lead, day) that had at least one call attempt — scoped to visible projects,
  // and to whichever days fall inside the currently selected date range (7d/30d/90d/All).
  function buildAttemptRows(projs) {
    const projSet = new Set(projs.map(p => p.key));
    const rangeRows = rangedData();
    const startKey = rangeRows.length ? dayKey(rangeRows[0].date) : null;
    const endKey = rangeRows.length ? dayKey(rangeRows[rangeRows.length - 1].date) : null;

    const out = [];
    LEADS.forEach(lead => {
      if (!projSet.has(lead.project) || !lead.calls.length) return;
      const byDay = new Map();
      lead.calls.forEach(c => {
        const dk = dayKey(c.at);
        if (startKey && (dk < startKey || dk > endKey)) return;
        if (!byDay.has(dk)) byDay.set(dk, { times: [], pickedTimes: [] });
        const bucket = byDay.get(dk);
        bucket.times.push(c.at);
        if (PICKED_UP_OUTCOMES.has(c.outcome)) bucket.pickedTimes.push(c.at);
      });
      for (const [dk, bucket] of byDay) {
        bucket.times.sort((a, b) => a - b);
        bucket.pickedTimes.sort((a, b) => a - b);
        out.push({
          lead, dayKey: dk, dateLabel: fmtDate(bucket.times[0]),
          count: bucket.times.length, times: bucket.times,
          pickedCount: bucket.pickedTimes.length, pickedTimes: bucket.pickedTimes,
        });
      }
    });
    out.sort((a, b) => b.dayKey.localeCompare(a.dayKey) || b.count - a.count || a.lead.name.localeCompare(b.lead.name));
    return out;
  }

  function renderAttempts(projs) {
    const table = document.getElementById('attempts-table');
    const attemptRows = buildAttemptRows(projs);

    const head = `<thead><tr>
      <th>Date</th><th style="text-align:left">Lead</th><th style="text-align:left">Lead ID</th>
      <th style="text-align:left">Project</th><th>Attempts</th><th style="text-align:left">Timestamps</th>
      <th>Picked Up</th><th style="text-align:left">Picked Up Timestamps</th>
    </tr></thead>`;

    const body = attemptRows.length ? attemptRows.map(r => `
      <tr class="drillable" data-lead="${r.lead.id}">
        <td>${r.dateLabel}</td>
        <td style="text-align:left">${escapeHtml(r.lead.name)}</td>
        <td style="text-align:left">${escapeHtml(r.lead.leadId)}</td>
        <td style="text-align:left"><span class="status-pill neutral">${PROJECT_BY_KEY[r.lead.project].name}</span></td>
        <td>${r.count}</td>
        <td style="text-align:left" class="attempts-times">${r.times.map(fmtTime).join(', ')}</td>
        <td>${r.pickedCount}</td>
        <td style="text-align:left" class="attempts-times">${r.pickedTimes.map(fmtTime).join(', ') || '—'}</td>
      </tr>
    `).join('') : '<tr><td colspan="8" style="text-align:center;padding:30px;color:var(--ink-faint);">No call attempts in the selected range/projects.</td></tr>';

    table.innerHTML = head + `<tbody>${body}</tbody>`;
    document.getElementById('attempts-count-hint').textContent =
      `${attemptRows.length} lead-day${attemptRows.length === 1 ? '' : 's'} · ${attemptRows.reduce((a, r) => a + r.count, 0)} total attempts · ${attemptRows.reduce((a, r) => a + r.pickedCount, 0)} picked up`;

    table.querySelectorAll('tr[data-lead]').forEach(tr => {
      tr.addEventListener('click', () => {
        const lead = LEADS.find(l => String(l.id) === tr.dataset.lead);
        if (lead) openLeadDetail(lead);
      });
    });
  }

  function csvEscape(v) { return `"${String(v).replace(/"/g, '""')}"`; }
  function downloadCsv(lines, filename) {
    const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  function exportAttemptsCsv() {
    const projs = visibleProjects();
    const attemptRows = buildAttemptRows(projs);
    const header = ['Date', 'Lead', 'Lead ID', 'Project', 'Attempts', 'Timestamps', 'Picked Up', 'Picked Up Timestamps'];
    const lines = [header.map(csvEscape).join(',')];
    attemptRows.forEach(r => {
      lines.push([
        r.dateLabel, r.lead.name, r.lead.leadId, PROJECT_BY_KEY[r.lead.project].name,
        r.count, r.times.map(fmtTime).join('; '),
        r.pickedCount, r.pickedTimes.map(fmtTime).join('; '),
      ].map(csvEscape).join(','));
    });
    downloadCsv(lines, `call-attempts-${rangeSlug()}-${new Date().toISOString().slice(0, 10)}.csv`);
  }

  // Exports whatever the drawer's lead list currently shows — respects every filter dimension
  // (project chips, date range, and any active outcome/status/svtype/called sub-filter chip).
  function exportLeadsListCsv() {
    const header = ['Name', 'Lead ID', 'Project', 'Born Date', 'Status', 'Calls', 'WhatsApp Messages'];
    const lines = [header.map(csvEscape).join(',')];
    currentVisibleLeads.forEach(l => {
      lines.push([
        l.name, l.leadId, PROJECT_BY_KEY[l.project].name, fmtDate(l.bornDate),
        STATUS_LABEL[l.status], l.calls.length, l.whatsapp.length,
      ].map(csvEscape).join(','));
    });
    downloadCsv(lines, `leads-${new Date().toISOString().slice(0, 10)}-${currentVisibleLeads.length}.csv`);
  }

  // ponytail: KPI cards count DISTINCT phones (a phone that's a lead in 2
  // projects = 1), OR-ing each stage flag across its projects. Drill-down rows
  // stay per (phone, project) — this dedup only affects the KPI card's number.
  // Fall back to lead id when phone is blank so unknown-phone leads don't merge.
  //
  // EXCEPT siteVisit/postSiteVisit: both come from the CRM stage, which is set
  // per (phone, project), so a phone can genuinely be Pre Site Visit for Spectra
  // and not for Legacy. Collapsing those to one row would both hide the project
  // the visit was actually booked for AND stop the card tying out with the Leads
  // tab's Stage filter, which counts one row per phone+project.
  function dedupeKeyFor(stageKey) {
    return (stageKey === 'siteVisit' || stageKey === 'postSiteVisit')
      ? (l => (l.phone || l.id) + '|' + l.project)
      : (l => l.phone || l.id);
  }
  function dedupeBy(leads, keyFn) {
    const seen = new Set(), out = [];
    for (const l of leads) { const k = keyFn(l); if (seen.has(k)) continue; seen.add(k); out.push(l); }
    return out;
  }

  function renderKpis(rows, projs) {
    const projKeys = projs.map(p => p.key);
    const totals = {};
    STAGES.forEach(s => {
      totals[s.key] = new Set(
        leadsFor({ stageKey: s.key, projectKeys: projKeys, rows }).map(dedupeKeyFor(s.key))
      ).size;
    });
    const el = document.getElementById('kpi-row');
    el.innerHTML = '';
    VISIBLE_STAGES.forEach(s => {
      const val = totals[s.key];
      const base = s.convBase ? totals[s.convBase] : null;
      const conv = base ? Math.round((val / base) * 100) : null;
      const card = document.createElement('div');
      card.className = 'kpi clickable';
      card.dataset.stage = s.key;
      card.innerHTML = `
        <p class="stage-label">${s.label}</p>
        <div class="value-row">
          <span class="value">${val.toLocaleString('en-IN')}</span>
          ${conv !== null ? `<span class="delta ${conv >= 50 ? 'pos' : 'neg'}">${conv}%</span>` : ''}
        </div>
        ${conv !== null ? `<p class="conv-of">of ${stageLabel(s.convBase).toLowerCase()}</p>` : `<p class="conv-of">baseline</p>`}
      `;
      el.appendChild(card);
    });
  }

  function render() {
    const rows = rangedData();
    const projs = visibleProjects();
    renderKpis(rows, projs);
    renderAttempts(projs);
  }

  // ================= Drill-down drawer =================
  const drawerEl = document.getElementById('drawer');
  const drawerOverlay = document.getElementById('drawer-overlay');
  const drawerTitle = document.getElementById('drawer-title');
  const drawerBody = document.getElementById('drawer-body');
  const drawerBack = document.getElementById('drawer-back');
  const drawerExportBtn = document.getElementById('drawer-export-btn');
  let currentListRenderer = null;
  let currentVisibleLeads = [];

  // per-call outcome labels/tones — used on individual call cards inside a lead's detail view
  const OUTCOME_LABEL = {
    CONNECTED: 'Connected', NOT_INTERESTED: 'Not Interested', AUTO_CALLBACK: 'Callback Requested',
    PRE_SITE: 'Pre Site', VIRTUAL_TOUR: 'Virtual Tour', DNC_REQUESTED: 'DNC Requested', DROPPED: 'Dropped',
    NO_ANSWER: 'No Answer', BUSY: 'Busy', SWITCHED_OFF: 'Switched Off', VOICEMAIL: 'Voicemail', NOT_CONNECTED: 'Not Connected',
  };
  const OUTCOME_TONE = {
    CONNECTED: 'success', NOT_INTERESTED: 'warning', AUTO_CALLBACK: 'info', PRE_SITE: 'success',
    VIRTUAL_TOUR: 'success', DNC_REQUESTED: 'warning', DROPPED: 'neutral',
    NO_ANSWER: 'neutral', BUSY: 'neutral', SWITCHED_OFF: 'neutral', VOICEMAIL: 'neutral', NOT_CONNECTED: 'critical',
  };

  // The "Picked Up" drill-down groups each lead into exactly ONE bucket — the single best
  // outcome it ever reached — rather than one chip per raw outcome (which let a lead appear
  // under multiple overlapping chips, e.g. both "Dropped" and "Connected"). Business-confirmed
  // priority, highest intent first. Pre Site / Virtual Tour are just two site-visit modalities,
  // so they're merged into one "Site Visit" bucket. DNC is a stronger, more specific version of
  // Not Interested (all DNC leads are also "not interested"), so it outranks plain Not Interested.
  const PICKED_BUCKET_ORDER = ['siteVisit', 'callback', 'dnc', 'notInterested', 'connected', 'dropped'];
  const PICKED_BUCKET_LABEL = {
    siteVisit: 'Site Visit', callback: 'Callback Requested', dnc: 'DNC Requested',
    notInterested: 'Not Interested', connected: 'Connected', dropped: 'Dropped',
  };
  function pickedBucketForLead(lead) {
    const outcomes = new Set(lead.calls.map(c => c.outcome));
    // authoritative — this is the CRM stage, not a call outcome, so trust it rather than
    // re-scanning raw outcomes (a PRE_SITE call whose lead never actually reached the
    // Pre/Post Site Visit stage in the CRM must NOT be bucketed as a site visit)
    if (lead.achieved.siteVisit) return 'siteVisit';
    if (outcomes.has('AUTO_CALLBACK')) return 'callback';
    if (outcomes.has('DNC_REQUESTED')) return 'dnc';
    if (outcomes.has('NOT_INTERESTED')) return 'notInterested';
    if (outcomes.has('CONNECTED')) return 'connected';
    if (outcomes.has('DROPPED')) return 'dropped';
    return null;
  }

  // "Called" top-level split: was the phone ever actually picked up, or never?
  const CALLED_TOP_ORDER = ['connected', 'notConnected'];
  const CALLED_TOP_LABEL = { connected: 'Connected', notConnected: 'Not Connected' };
  function calledTopBucket(lead) { return lead.achieved.picked ? 'connected' : 'notConnected'; }

  // "Not Connected" sub-categories — same mutually-exclusive-by-priority approach as Picked Up.
  // Voicemail ranks highest (closest to an actual answer — a machine still picked up), Not
  // Connected ranks lowest (never even reached the customer's phone).
  const NEVER_PICKED_ORDER = ['voicemail', 'busy', 'noAnswer', 'switchedOff', 'notConnected'];
  const NEVER_PICKED_LABEL = {
    voicemail: 'Voicemail', busy: 'Busy', noAnswer: 'No Answer', switchedOff: 'Switched Off', notConnected: 'Not Connected',
  };
  function neverPickedBucketForLead(lead) {
    const outcomes = new Set(lead.calls.map(c => c.outcome));
    if (outcomes.has('VOICEMAIL')) return 'voicemail';
    if (outcomes.has('BUSY')) return 'busy';
    if (outcomes.has('NO_ANSWER')) return 'noAnswer';
    if (outcomes.has('SWITCHED_OFF')) return 'switchedOff';
    if (outcomes.has('NOT_CONNECTED')) return 'notConnected';
    return null;
  }

  // No Physical/Virtual split on Booked Site Visit: that was derived from the
  // PRE_SITE vs VIRTUAL_TOUR call outcome, and the stage now comes from the CRM,
  // which carries no modality (a stage-driven lead may have no AI call at all).

  function escapeHtml(str) {
    return String(str == null ? '' : str).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  }
  function initials(name) { return name.split(' ').map(w => w[0]).filter(Boolean).slice(0, 2).join('').toUpperCase(); }
  function statusPill(key) { return `<span class="status-pill ${STATUS_TONE[key]}">${STATUS_LABEL[key]}</span>`; }
  function fmtDuration(secs) {
    if (!secs) return '';
    const m = Math.floor(secs / 60), s = Math.round(secs % 60);
    return m ? `${m}m ${s}s` : `${s}s`;
  }

  function leadsFor({ stageKey, projectKeys, dayKeyFilter, rows }) {
    return LEADS.filter(l => {
      if (!projectKeys.includes(l.project)) return false;
      if (!l.achieved[stageKey]) return false;
      if (dayKeyFilter) return dayKey(l.bornDate) === dayKeyFilter;
      if (rows && rows.length) {
        const lk = dayKey(l.bornDate);
        return lk >= dayKey(rows[0].date) && lk <= dayKey(rows[rows.length - 1].date);
      }
      return true;
    }).sort((a, b) => b.bornDate - a.bornDate);
  }

  function renderLeadRows(leadsToShow) {
    return leadsToShow.length ? leadsToShow.map(l => `
      <div class="lead-row" data-lead="${l.id}">
        <div class="lead-avatar" style="--proj-color:${PROJECT_BY_KEY[l.project].color}">${initials(l.name)}</div>
        <div class="lead-main">
          <div class="lead-name">${escapeHtml(l.name)}</div>
          <div class="lead-sub">${escapeHtml(l.leadId)} · ${PROJECT_BY_KEY[l.project].name} · born ${fmtDate(l.bornDate)}</div>
        </div>
        ${statusPill(l.status)}
      </div>
    `).join('') : `<div class="drawer-empty">No leads match this filter.</div>`;
  }

  // Lead Count's status breakdown — mutually exclusive, so counts always sum to the total
  const STATUS_ORDER = ['leads', 'called', 'picked', 'waSent', 'wa', 'callback', 'siteVisit', 'postSiteVisit', 'dnc', 'notInterested'];

  function renderFilterChips({ leads, dimension, order, labelFor, matches, active }) {
    const counts = order.map(key => ({ key, count: leads.filter(l => matches(l, key)).length })).filter(o => o.count > 0);
    return `<div class="outcome-filters">
      <button class="outcome-chip ${!active ? 'active' : ''}" data-${dimension}="">All (${leads.length})</button>
      ${counts.map(o => `<button class="outcome-chip ${active === o.key ? 'active' : ''}" data-${dimension}="${o.key}">${labelFor(o.key)} (${o.count})</button>`).join('')}
    </div>`;
  }

  // opts.withOutcomeFilters: breaks "Picked Up" down by each lead's single best outcome bucket.
  // opts.withStatusFilters: breaks "Lead Count" down by each lead's single current status.
  // opts.withCalledFilters: breaks "Called" down two levels — Connected/Not Connected, then each
  // side's own unique sub-categories. All dimensions are mutually exclusive by construction.
  function openLeadsList(leads, titleHtml, opts = {}) {
    currentListRenderer = () => openLeadsList(leads, titleHtml, opts);
    drawerBack.style.display = 'none';
    drawerTitle.innerHTML = titleHtml;

    let filterHtml = '';
    let visibleLeads = leads;
    if (opts.withOutcomeFilters) {
      const active = opts.activeOutcome || null;
      filterHtml = renderFilterChips({
        leads, dimension: 'outcome', order: PICKED_BUCKET_ORDER, labelFor: k => PICKED_BUCKET_LABEL[k],
        matches: (l, k) => pickedBucketForLead(l) === k, active,
      });
      if (active) visibleLeads = leads.filter(l => pickedBucketForLead(l) === active);
    } else if (opts.withStatusFilters) {
      const active = opts.activeStatus || null;
      filterHtml = renderFilterChips({
        leads, dimension: 'status', order: STATUS_ORDER, labelFor: k => STATUS_LABEL[k],
        matches: (l, k) => l.status === k, active,
      });
      if (active) visibleLeads = leads.filter(l => l.status === active);
    } else if (opts.withCalledFilters) {
      const activeTop = opts.activeCalledTop || null;
      const activeSub = opts.activeCalledSub || null;
      filterHtml = renderFilterChips({
        leads, dimension: 'called-top', order: CALLED_TOP_ORDER, labelFor: k => CALLED_TOP_LABEL[k],
        matches: (l, k) => calledTopBucket(l) === k, active: activeTop,
      });
      if (activeTop) {
        visibleLeads = leads.filter(l => calledTopBucket(l) === activeTop);
        const subOrder = activeTop === 'connected' ? PICKED_BUCKET_ORDER : NEVER_PICKED_ORDER;
        const subLabel = activeTop === 'connected' ? PICKED_BUCKET_LABEL : NEVER_PICKED_LABEL;
        const subBucketFn = activeTop === 'connected' ? pickedBucketForLead : neverPickedBucketForLead;
        filterHtml += renderFilterChips({
          leads: visibleLeads, dimension: 'called-sub', order: subOrder, labelFor: k => subLabel[k],
          matches: (l, k) => subBucketFn(l) === k, active: activeSub,
        });
        if (activeSub) visibleLeads = visibleLeads.filter(l => subBucketFn(l) === activeSub);
      }
    }

    currentVisibleLeads = visibleLeads;
    drawerExportBtn.style.display = '';
    drawerBody.innerHTML = filterHtml + renderLeadRows(visibleLeads);
    if (opts.withOutcomeFilters) {
      drawerBody.querySelectorAll('[data-outcome]').forEach(chip => {
        chip.addEventListener('click', () => openLeadsList(leads, titleHtml, { ...opts, activeOutcome: chip.dataset.outcome || null }));
      });
    } else if (opts.withStatusFilters) {
      drawerBody.querySelectorAll('[data-status]').forEach(chip => {
        chip.addEventListener('click', () => openLeadsList(leads, titleHtml, { ...opts, activeStatus: chip.dataset.status || null }));
      });
    } else if (opts.withCalledFilters) {
      drawerBody.querySelectorAll('[data-called-top]').forEach(chip => {
        chip.addEventListener('click', () => openLeadsList(leads, titleHtml, { ...opts, activeCalledTop: chip.dataset.calledTop || null, activeCalledSub: null }));
      });
      drawerBody.querySelectorAll('[data-called-sub]').forEach(chip => {
        chip.addEventListener('click', () => openLeadsList(leads, titleHtml, { ...opts, activeCalledSub: chip.dataset.calledSub || null }));
      });
    }

    drawerEl.classList.add('open');
    drawerOverlay.classList.add('open');
  }

  function openLeadDetail(lead) {
    drawerBack.style.display = '';
    drawerExportBtn.style.display = 'none';
    drawerTitle.innerHTML = `${escapeHtml(lead.name)} <span class="sub">Full lead history</span>`;
    const proj = PROJECT_BY_KEY[lead.project];

    const callsHtml = lead.calls.length ? lead.calls.map(c => `
      <div class="call-card">
        <div class="call-card-head">
          <span class="status-pill ${OUTCOME_TONE[c.outcome] || 'neutral'}">${OUTCOME_LABEL[c.outcome] || c.outcome}</span>
          <span class="call-meta">${fmtDateTime(c.at)}${c.durationSecs ? ' · ' + fmtDuration(c.durationSecs) : ''} · ${c.source === 'live' ? 'live bot' : 'backup bot'}</span>
        </div>
        ${c.recordingUrl && c.recordingExpired ? `<div class="call-recording-expired">🔇 Recording link expired (temporary link, not saved permanently) — not a bug in this dashboard, the live bot needs to store a permanent link instead.</div>` : ''}
        ${c.recordingUrl && !c.recordingExpired ? `<audio controls preload="none" src="${escapeHtml(c.recordingUrl)}"></audio>` : ''}
        ${c.visitDate ? `<div class="call-visit-info">📅 Site visit: ${escapeHtml(c.visitDate)}${c.visitSlot ? ', ' + escapeHtml(c.visitSlot) : ''}${c.venue ? ' · ' + escapeHtml(c.venue) : ''}</div>` : ''}
        ${c.transcript ? `<div class="call-transcript">${escapeHtml(c.transcript)}</div>` : (c.summary ? `<div class="call-transcript">${escapeHtml(c.summary)}</div>` : '<div class="call-transcript call-transcript-empty">No transcript or recording for this call.</div>')}
      </div>
    `).join('') : `<div class="drawer-empty">No calls yet.</div>`;

    const waHtml = lead.whatsapp.length ? `<div class="wa-thread">${lead.whatsapp.map(m => `
        <div class="wa-bubble ${m.direction}">
          <div class="wa-text">${escapeHtml(m.text)}</div>
          <div class="wa-time">${fmtDateTime(m.at)}</div>
        </div>
      `).join('')}</div>` : `<div class="drawer-empty">No WhatsApp messages yet.</div>`;

    drawerBody.innerHTML = `
      <div class="lead-detail">
        <div class="ld-head">
          <div class="ld-avatar" style="--proj-color:${proj.color}">${initials(lead.name)}</div>
          <div>
            <div class="ld-name">${escapeHtml(lead.name)}</div>
            <div class="ld-phone">${escapeHtml(lead.leadId)}</div>
          </div>
        </div>
        <div class="ld-meta">
          <span class="status-pill neutral">${proj.name}</span>
          ${statusPill(lead.status)}
        </div>
        <div class="ld-agent">Born ${fmtDateTime(lead.bornDate)} · source: ${escapeHtml(lead.source)} · handled by ${escapeHtml(lead.agent)}</div>
      </div>
      <div class="ld-section-label">Calls (${lead.calls.length})</div>
      <div class="calls-list">${callsHtml}</div>
      <div class="ld-section-label">WhatsApp (${lead.whatsapp.length})</div>
      ${waHtml}
    `;
  }

  function closeDrawer() {
    drawerEl.classList.remove('open');
    drawerOverlay.classList.remove('open');
  }

  drawerOverlay.addEventListener('click', closeDrawer);
  document.getElementById('drawer-close').addEventListener('click', closeDrawer);
  drawerBack.addEventListener('click', () => currentListRenderer && currentListRenderer());
  drawerExportBtn.addEventListener('click', exportLeadsListCsv);
  drawerBody.addEventListener('click', e => {
    const row = e.target.closest('.lead-row[data-lead]');
    if (!row) return;
    const lead = LEADS.find(l => String(l.id) === row.dataset.lead);
    if (lead) openLeadDetail(lead);
  });

  function filterOptsForStage(stage) {
    return {
      withOutcomeFilters: stage === 'picked',
      withStatusFilters: stage === 'leads',
      withCalledFilters: stage === 'called',
    };
  }

  document.getElementById('kpi-row').addEventListener('click', e => {
    const card = e.target.closest('.kpi[data-stage]');
    if (!card) return;
    const stageKey = card.dataset.stage;
    const rows = rangedData();
    const projs = visibleProjects().map(p => p.key);
    const leads = dedupeBy(leadsFor({ stageKey, projectKeys: projs, rows }), dedupeKeyFor(stageKey));
    const rangeLabel = rangeLabelText();
    openLeadsList(leads, `${stageLabel(stageKey)} <span class="sub">${leads.length} leads · ${rangeLabel}</span>`, filterOptsForStage(stageKey));
  });

  loadDashboard();
})();
</script>

"""
