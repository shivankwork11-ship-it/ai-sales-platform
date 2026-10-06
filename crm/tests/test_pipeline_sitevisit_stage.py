"""Booked Site Visit is driven by the CRM stage, not by AI call outcomes.

The regression this locks down: a PRE_SITE call on a lead whose CRM stage never
reached Pre Site Visit must NOT count, and one phone's site visit must be
credited only to the project whose CRM lead actually holds that stage (the old
call-outcome rule credited it to every project-sibling profile for the phone).
Also: pipeline's project resolution must match the Leads tab's `_leadProject`
exactly, or the same lead can land under a different project on each tab.
"""
from datetime import datetime, timezone


def _profile(client, lead_id, phone, project, call_outcome=None):
    """A lead_profiles doc, optionally with one AI call of the given outcome."""
    doc = {
        "lead_id": lead_id, "lead_name": lead_id, "lead_phone_number": phone,
        "project": project, "lead_created_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
        "lead_source": "Inncircles", "source_lead_id": lead_id,
    }
    if call_outcome:
        doc["ai_calls"] = {"call_conversations": [{
            "call_sid": lead_id + "-c1", "call_outcome": call_outcome,
            "started_at": datetime(2026, 7, 21, tzinfo=timezone.utc),
        }]}
    client["analytics_db"]["lead_profiles"].insert_one(doc)


def _crm_lead(client, _id, phone, project, stage):
    client["Zoho_Database"]["leads"].insert_one(
        {"_id": _id, "phone": phone, "project": project, "prd_stage": stage})


def _site_visit_ids(dash):
    c = dash.app.test_client()
    with c.session_transaction() as s:   # /api/pipeline requires a logged-in session
        s["user_email"] = "t@asbl.in"
    resp = c.get("/api/pipeline")
    assert resp.status_code == 200
    return {l["id"] for l in resp.get_json()["leads"] if l["achieved"].get("siteVisit")}


def _post_site_visit_ids(dash):
    c = dash.app.test_client()
    with c.session_transaction() as s:
        s["user_email"] = "t@asbl.in"
    resp = c.get("/api/pipeline")
    assert resp.status_code == 200
    return {l["id"] for l in resp.get_json()["leads"] if l["achieved"].get("postSiteVisit")}


def test_crm_stage_drives_site_visit(dash):
    """Pre Site Visit only — must tie out with the Leads tab's "Pre Site Visit"
    filter, which is Pre-only. Post Site Visit does NOT count (user decision:
    exact parity over the "visit was booked either way" argument)."""
    c = dash.client
    _profile(c, "L-PRE", "919000000001", "LOFT")
    _crm_lead(c, "1-LOFT", "919000000001", "LOFT", "Pre Site Visit")
    _profile(c, "L-POST", "919000000002", "LOFT")
    _crm_lead(c, "2-LOFT", "919000000002", "LOFT", "Post Site Visit")
    _profile(c, "L-NEW", "919000000003", "LOFT")
    _crm_lead(c, "3-LOFT", "919000000003", "LOFT", "New Lead")

    assert _site_visit_ids(dash) == {"L-PRE"}


def test_pre_site_call_without_crm_stage_does_not_count(dash):
    """The old rule's false positive: a PRE_SITE call outcome is no longer enough."""
    c = dash.client
    _profile(c, "L-CALL", "919000000004", "LOFT", call_outcome="PRE_SITE")
    _crm_lead(c, "4-LOFT", "919000000004", "LOFT", "Lead Initiated")

    assert _site_visit_ids(dash) == set()


def test_one_phone_counts_only_for_the_project_holding_the_stage(dash):
    """The overcount fix — same phone under two projects, one CRM stage."""
    c = dash.client
    phone = "919000000005"
    # Both profiles carry the SAME embedded PRE_SITE call (that duplication across
    # project-siblings is exactly what inflated the old count).
    _profile(c, "L-SPECTRA", phone, "SPECTRA", call_outcome="PRE_SITE")
    _profile(c, "L-LEGACY", phone, "LEGACY", call_outcome="PRE_SITE")
    _crm_lead(c, "5-SPECTRA", phone, "SPECTRA", "Pre Site Visit")
    _crm_lead(c, "5-LEGACY", phone, "LEGACY", "New Lead")

    assert _site_visit_ids(dash) == {"L-SPECTRA"}


def test_manual_stage_override_wins_when_locked(dash):
    """stage_locked => the manual `stage` mirror beats the relay's prd_stage,
    same rule as crm_v2._display_stage / the Leads tab Stage column."""
    c = dash.client
    _profile(c, "L-LOCKED", "919000000006", "LOFT")
    c["Zoho_Database"]["leads"].insert_one({
        "_id": "6-LOFT", "phone": "919000000006", "project": "LOFT",
        "prd_stage": "New Lead", "stage": "Pre Site Visit", "stage_locked": True})

    assert _site_visit_ids(dash) == {"L-LOCKED"}


def test_project_falls_back_to_plid_suffix_when_field_empty(dash):
    """CRM lead with no `project` field still keys off its PLID suffix, else its
    stage would file under "phone|" and silently never match."""
    c = dash.client
    _profile(c, "L-SUFFIX", "919000000007", "BROADWAY")
    _crm_lead(c, "7-BROADWAY", "919000000007", "", "Pre Site Visit")

    assert _site_visit_ids(dash) == {"L-SUFFIX"}


def test_lead_profiles_project_fallback_matches_leads_tab(dash):
    """A lead_profiles doc with a blank `project` field must resolve the SAME
    project the Leads tab's `_leadProject` would (via its ZOHO-<id>-PROJECT lead_id
    suffix) — otherwise this lead lands under a different project on the two tabs
    and the counts can never tie out. Before this fix, pipeline used `doc.project`
    alone and simply DROPPED such leads (undercounting vs. the Leads tab)."""
    c = dash.client
    c["analytics_db"]["lead_profiles"].insert_one({
        "lead_id": "ZOHO-1234-SPECTRA", "lead_name": "L-BLANKPROJ",
        "lead_phone_number": "919000000008", "project": "",
        "lead_created_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
        "lead_source": "Zoho",
    })
    _crm_lead(c, "1234-SPECTRA", "919000000008", "SPECTRA", "Pre Site Visit")

    assert _site_visit_ids(dash) == {"ZOHO-1234-SPECTRA"}


def test_non_zoho_lead_with_blank_project_defaults_to_loft(dash):
    """A website/loft lead (lead_id has no ZOHO- prefix) with a blank `project`
    field defaults to LOFT, matching `_leadProject`'s `!startsWith('ZOHO-') ->
    'LOFT'` fallback — not dropped."""
    c = dash.client
    c["analytics_db"]["lead_profiles"].insert_one({
        "lead_id": "L-WEBSITE", "lead_name": "L-WEBSITE",
        "lead_phone_number": "919000000009", "project": "",
        "lead_created_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
        "lead_source": "Website",
    })
    _crm_lead(c, "9-LOFT", "919000000009", "LOFT", "Pre Site Visit")

    assert _site_visit_ids(dash) == {"L-WEBSITE"}


def test_duplicate_lead_profiles_rows_for_same_phone_project_both_count(dash):
    """The Leads tab's default view (duplicate removal OFF) shows one row per
    lead_profiles doc, with no phone-level dedup — a genuine data-quality
    duplicate (two docs, same phone AND project) shows as two rows, both
    Pre Site Visit. Pipeline must match: no backend dedup collapses these."""
    c = dash.client
    _profile(c, "L-DUP-A", "919000000010", "LOFT")
    _profile(c, "L-DUP-B", "919000000010", "LOFT")
    _crm_lead(c, "10-LOFT", "919000000010", "LOFT", "Pre Site Visit")

    assert _site_visit_ids(dash) == {"L-DUP-A", "L-DUP-B"}


def test_bulk_manual_leads_excluded(dash):
    """bulk_manual (cold-call / Bulk Calls tab) leads must never appear in
    Pipeline, same exclusion /api/leads already applies."""
    c = dash.client
    c["analytics_db"]["lead_profiles"].insert_one({
        "lead_id": "L-BULK", "lead_name": "L-BULK", "lead_phone_number": "919000000011",
        "project": "LOFT", "lead_created_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
        "bulk_manual": True,
    })
    _crm_lead(c, "11-LOFT", "919000000011", "LOFT", "Pre Site Visit")

    assert _site_visit_ids(dash) == set()


def test_other_pipeline_metrics_unchanged_by_crm_stage(dash):
    """Called/Picked Up/WhatsApp must still be driven by ai_calls/whatsapp, not
    by CRM stage — this refactor only touches achieved.siteVisit."""
    c = dash.client
    c["analytics_db"]["lead_profiles"].insert_one({
        "lead_id": "L-CALLED", "lead_name": "L-CALLED", "lead_phone_number": "919000000012",
        "project": "LOFT", "lead_created_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
        "ai_calls": {"call_conversations": [{
            "call_sid": "c1", "call_outcome": "CONNECTED",
            "started_at": datetime(2026, 7, 21, tzinfo=timezone.utc)}]},
        "whatsapp": {"outbound_count": 2, "user_conversation_depth": 1,
                     "user_conversation_history": [{"type": "inbound", "message": "hi",
                                                     "date": datetime(2026, 7, 21, tzinfo=timezone.utc)}]},
    })
    _crm_lead(c, "12-LOFT", "919000000012", "LOFT", "New Lead")   # no site visit stage

    c2 = dash.app.test_client()
    with c2.session_transaction() as s:
        s["user_email"] = "t@asbl.in"
    lead = next(l for l in c2.get("/api/pipeline").get_json()["leads"] if l["id"] == "L-CALLED")
    assert lead["achieved"]["called"] is True
    assert lead["achieved"]["picked"] is True
    assert lead["achieved"]["waSent"] is True
    assert lead["achieved"]["wa"] is True
    assert "siteVisit" not in lead["achieved"] or lead["achieved"]["siteVisit"] is False


def test_leads_api_and_pipeline_api_agree_on_the_same_dataset(dash):
    """The actual parity requirement, exercised through the REAL backend paths
    both tabs use — not two hand-written copies of the same algorithm. /api/leads
    now returns resolved_stage computed by the same lead_identity module Pipeline
    uses, so this is a genuine cross-implementation check, not a tautology: if
    either endpoint's wiring to lead_identity broke independently, this fails.
    """
    c = dash.client
    cases = [
        ("P1", "919000000101", "LOFT", "Pre Site Visit"),
        ("P2", "919000000102", "SPECTRA", "Post Site Visit"),   # excluded from both
        ("P3", "919000000103", "BROADWAY", "New Lead"),
        ("P4", "919000000104", "LEGACY", "Pre Site Visit"),
        ("P5", "919000000105", "LANDMARK", "Lead Initiated"),
    ]
    for lead_id, phone, project, stage in cases:
        _profile(c, lead_id, phone, project)
        _crm_lead(c, f"{lead_id}-{project}", phone, project, stage)

    tc = dash.app.test_client()
    with tc.session_transaction() as s:
        s["user_email"] = "t@asbl.in"

    leads_rows = tc.get("/api/leads").get_json()
    leads_pre_site_ids = {r["lead_id"] for r in leads_rows if r.get("resolved_stage") == "Pre Site Visit"}

    pipeline_rows = tc.get("/api/pipeline").get_json()["leads"]
    pipeline_site_visit_ids = {l["id"] for l in pipeline_rows if l["achieved"].get("siteVisit")}

    assert leads_pre_site_ids == {"P1", "P4"}
    assert pipeline_site_visit_ids == {"P1", "P4"}
    assert leads_pre_site_ids == pipeline_site_visit_ids


def test_post_site_visit_is_its_own_stage_same_logic_as_pre_site(dash):
    """Post Site Visit is a separate KPI card from Booked Site Visit (Pre Site
    Visit) — same rule (CRM stage, per phone+project), different stage value.
    A lead is never both: the CRM stage is a single string per phone+project."""
    c = dash.client
    _profile(c, "L-POST", "919000000201", "LOFT")
    _crm_lead(c, "201-LOFT", "919000000201", "LOFT", "Post Site Visit")
    _profile(c, "L-PRE", "919000000202", "LOFT")
    _crm_lead(c, "202-LOFT", "919000000202", "LOFT", "Pre Site Visit")

    assert _post_site_visit_ids(dash) == {"L-POST"}
    assert _site_visit_ids(dash) == {"L-PRE"}


def test_post_site_visit_call_outcome_alone_does_not_count(dash):
    """Same false-positive guard as Pre Site Visit: a call outcome is not a
    substitute for the CRM stage actually reaching Post Site Visit."""
    c = dash.client
    _profile(c, "L-CALL", "919000000203", "LOFT", call_outcome="PRE_SITE")
    _crm_lead(c, "203-LOFT", "919000000203", "LOFT", "Pre Site Visit")   # not Post

    assert _post_site_visit_ids(dash) == set()


def test_post_site_visit_scoped_per_phone_project_like_pre_site(dash):
    """Same phone, two projects, only one holds Post Site Visit — same overcount
    guard as Booked Site Visit, exercised for the new stage."""
    c = dash.client
    phone = "919000000204"
    _profile(c, "L-A", phone, "SPECTRA")
    _profile(c, "L-B", phone, "LEGACY")
    _crm_lead(c, "204-SPECTRA", phone, "SPECTRA", "Post Site Visit")
    _crm_lead(c, "204-LEGACY", phone, "LEGACY", "New Lead")

    assert _post_site_visit_ids(dash) == {"L-A"}


def test_pre_site_visit_lead_is_never_also_post_site_visit(dash):
    """A lead whose CRM stage is Pre Site Visit must not appear in BOTH sets —
    the two stages are mutually exclusive values of the same field."""
    c = dash.client
    _profile(c, "L-PRE-ONLY", "919000000205", "LOFT")
    _crm_lead(c, "205-LOFT", "919000000205", "LOFT", "Pre Site Visit")

    assert "L-PRE-ONLY" in _site_visit_ids(dash)
    assert "L-PRE-ONLY" not in _post_site_visit_ids(dash)


def test_post_site_visit_lead_is_never_also_booked_site_visit(dash):
    """Same guarantee from the other direction — Post Site Visit must not leak
    into the Booked Site Visit (Pre Site Visit) count."""
    c = dash.client
    _profile(c, "L-POST-ONLY", "919000000206", "LOFT")
    _crm_lead(c, "206-LOFT", "919000000206", "LOFT", "Post Site Visit")

    assert "L-POST-ONLY" in _post_site_visit_ids(dash)
    assert "L-POST-ONLY" not in _site_visit_ids(dash)


def test_pipeline_kpi_count_matches_phone_project_dedup_of_drilldown(dash):
    """Structural guarantee behind requirement #9 (KPI count == drill-down count):
    both the KPI card and the drill-down click handler call the SAME JS function,
    dedupeKeyFor('siteVisit'/'postSiteVisit') — a (phone,project) key. This test
    can't execute that JS, but proves its precondition holds against real backend
    output: two lead_profiles docs that are a data-quality duplicate (same phone,
    same project) both come back from /api/pipeline with IDENTICAL phone+project
    fields, so a (phone,project) dedup of the drill-down rows is guaranteed to
    collapse them to one KPI count — there is no server-side path that could
    make the two client-side call sites disagree."""
    c = dash.client
    phone = "919000000207"
    _profile(c, "L-DUP-1", phone, "LOFT")
    _profile(c, "L-DUP-2", phone, "LOFT")   # data-quality duplicate, same phone+project
    _crm_lead(c, "207-LOFT", phone, "LOFT", "Pre Site Visit")

    tc = dash.app.test_client()
    with tc.session_transaction() as s:
        s["user_email"] = "t@asbl.in"
    rows = tc.get("/api/pipeline").get_json()["leads"]
    dup_rows = [r for r in rows if r["id"] in ("L-DUP-1", "L-DUP-2")]

    assert len(dup_rows) == 2                              # drill-down: both rows present
    assert all(r["achieved"].get("siteVisit") for r in dup_rows)
    dedup_keys = {(r["phone"], r["project"]) for r in dup_rows}
    assert len(dedup_keys) == 1               # same (phone,project) key => KPI counts 1, not 2


def test_pipeline_page_has_no_funnel_view_or_daily_funnel_grid(dash):
    """Rendered-HTML check: Funnel view tab/content and the Daily funnel by
    project grid must be completely absent, not just hidden."""
    c = dash.app.test_client()
    with c.session_transaction() as s:
        s["user_email"] = "t@asbl.in"
    html = c.get("/pipeline").get_data(as_text=True)

    assert "Funnel view" not in html
    assert "Conversion funnel by project" not in html
    assert "funnel-grid" not in html
    assert "Daily funnel by project" not in html
    assert "pivot-table" not in html
    assert "row-count-hint" not in html


def test_pipeline_page_has_no_empty_pivot_tab_and_keeps_call_attempts(dash):
    """No dead tab navigation, no empty panel — but Call Attempts survives,
    unhidden (it's the only remaining section, no tab toggle needed for it)."""
    c = dash.app.test_client()
    with c.session_transaction() as s:
        s["user_email"] = "t@asbl.in"
    html = c.get("/pipeline").get_data(as_text=True)

    assert "Pivot table" not in html
    assert 'id="view-toggle"' not in html   # not a bare "view-toggle" substring check —
    assert 'data-view="pivot"' not in html  # SHARED_HEAD has an unrelated #dedupe-view-toggle
    assert "Call attempts by lead, by day" in html
    assert 'id="attempts-panel" style="display:none;"' not in html   # no longer hidden-by-default


def test_post_site_visit_kpi_card_present_in_rendered_page(dash):
    c = dash.app.test_client()
    with c.session_transaction() as s:
        s["user_email"] = "t@asbl.in"
    html = c.get("/pipeline").get_data(as_text=True)

    assert "Post Site Visit" in html
    assert "Booked Site Visit" in html   # still present, unrenamed, unaffected


def test_exactly_8_visible_kpi_stages_plus_1_hidden_engaged_helper(dash):
    """Locks the exact required card count. Regression target: an earlier report
    on this feature miscounted the STAGES array as "8 entries, 7 visible" — the
    real array is 9 entries (8 visible + 1 hidden 'engaged' helper). This parses
    the STAGES array out of the actual rendered page (not a hand-copied literal)
    so a future edit that drops a required card, or un-hides 'engaged', fails
    this test rather than silently shipping."""
    import re as _re

    c = dash.app.test_client()
    with c.session_transaction() as s:
        s["user_email"] = "t@asbl.in"
    html = c.get("/pipeline").get_data(as_text=True)

    m = _re.search(r"const STAGES = \[(.*?)\];", html, _re.S)
    assert m, "STAGES array not found in rendered page"
    stage_objs = _re.findall(r"\{[^{}]*\}", m.group(1))
    assert len(stage_objs) == 9, f"expected 9 STAGES entries, found {len(stage_objs)}"

    hidden = [o for o in stage_objs if "hidden: true" in o]
    visible = [o for o in stage_objs if "hidden: true" not in o]
    assert len(hidden) == 1 and "'engaged'" in hidden[0]
    assert len(visible) == 8

    visible_labels = [_re.search(r"label: '([^']+)'", o).group(1) for o in visible]
    assert visible_labels == [
        "Lead Count", "Called", "Picked Up", "WhatsApp Sent", "WhatsApp Reply",
        "Booked Site Visit", "Post Site Visit", "Asked for Callback",
    ]
