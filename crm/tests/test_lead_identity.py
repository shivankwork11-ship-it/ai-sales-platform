"""Unit tests for lead_identity.py — the one canonical implementation of
phone/project/stage resolution shared by dashboard.py, crm_v2.py and
pipeline.py. Pure functions, no Flask/Mongo — these run standalone.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lead_identity as li


def test_normal_project_field():
    assert li.resolve_project("Spectra", "ZOHO-1-SPECTRA") == "SPECTRA"


def test_plid_suffix_fallback_for_lead_profiles_style_id():
    """lead_profiles.lead_id carries the "ZOHO-" prefix — suffix fallback applies."""
    assert li.resolve_project("", "ZOHO-1234-BROADWAY") == "BROADWAY"


def test_plid_suffix_fallback_for_bare_crm_id():
    """Zoho_Database.leads._id has NO "ZOHO-" prefix (crm_api.py's _mirror_profile
    is what adds it when copying into lead_profiles) — id_is_bare_plid=True is
    required for the suffix fallback to apply here, or every blank-project CRM
    lead silently defaults to LOFT regardless of its real suffix."""
    assert li.resolve_project("", "1234-BROADWAY", id_is_bare_plid=True) == "BROADWAY"
    # Without the flag, a bare (unprefixed) id is NOT treated as Zoho-sourced —
    # this is the exact bug this test suite caught during the refactor.
    assert li.resolve_project("", "1234-BROADWAY") == "LOFT"


def test_campaign_name_fallback():
    assert li.resolve_project("", "ZOHO-99", "URL-Legacy-Launch") == "LEGACY"


def test_non_zoho_lead_defaults_to_loft():
    assert li.resolve_project("", "L-WEBSITE-42") == "LOFT"


def test_unresolvable_project_returns_empty_string_not_none():
    """A Zoho-sourced id whose suffix isn't a known project, with no project
    field and no campaign keyword, is genuinely unresolvable — "" (not "LOFT"),
    matching the JS, which never returns null/undefined."""
    assert li.resolve_project("", "ZOHO-1234-XYZ") == ""


def test_stage_locked_true_prefers_manual_stage_mirror():
    assert li.resolve_display_stage("Pre Site Visit", "New Lead", True) == "Pre Site Visit"


def test_stage_locked_false_prefers_relay_prd_stage():
    assert li.resolve_display_stage("New Lead", "Pre Site Visit", False) == "Pre Site Visit"


def test_stage_missing_falls_back_to_prd_stage_even_when_locked():
    assert li.resolve_display_stage(None, "Pre Site Visit", True) == "Pre Site Visit"


def test_is_pre_site_visit():
    assert li.is_pre_site_visit("Pre Site Visit") is True
    assert li.is_pre_site_visit("Post Site Visit") is False
    assert li.is_pre_site_visit(None) is False


def test_is_post_site_visit():
    assert li.is_post_site_visit("Post Site Visit") is True
    assert li.is_post_site_visit("Pre Site Visit") is False
    assert li.is_post_site_visit(None) is False


def test_normalise_phone_strips_non_digits_and_keeps_last_10():
    assert li.normalise_phone("+91 90000-00001") == "9000000001"


def test_normalise_phone_short_number_returns_empty():
    assert li.normalise_phone("12345") == ""
