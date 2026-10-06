"""Regression tests for /api/v2/leads/bulk-trigger's admin-only enforcement
(API-V2-ROLE-GATING). Before this fix, any logged-in session — not just
admin — could trigger real outbound AI calls via this endpoint: the frontend
hid the button, but the backend never checked role. fire_call is
monkeypatched to a MagicMock in every test here — this suite must never
place a real outbound call.

Every session-authenticated POST below sends a dummy `Authorization: Bearer`
header. That's unrelated to this task — it only exists to satisfy
dashboard.py's pre-existing CSRF check (a non-GET/HEAD request needs either a
matching X-CSRF-Token or a Bearer-prefixed Authorization header, treated as a
server-to-server caller). Without it every POST here would 403 on
"csrf_failed" regardless of role, which would silently mask whatever the
role check itself is doing — this bit us once already while writing this
file. It carries no real credential and must not be confused with the actual
role-spoofing attempts below (those use X-Role/X-User-Role instead, which
the fix must ignore).

Note on why every "must not be called" assertion uses MagicMock.assert_not_
called() rather than a raising sentinel: leads_bulk_trigger wraps the
fire_call call in a broad `except Exception`, so a sentinel that raises
would have its exception silently swallowed and logged as a per-lead error
— the test would pass even if fire_call really was invoked. Only checking
the mock's call count after the request is reliable here.
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crm_v2

_CSRF_BYPASS = {"Authorization": "Bearer test-csrf-bypass-not-a-real-credential"}


def _login(client, role="viewer", email="viewer@asbl.in"):
    with client.session_transaction() as s:
        s["user_email"] = email
        s["role"] = role


def _mock_fire_call(monkeypatch):
    mock = MagicMock(return_value=(True, "fake-call-id", None))
    monkeypatch.setattr(crm_v2, "fire_call", mock)
    return mock


def _seed_lead(dash, plid="1-LOFT"):
    dash.client["Zoho_Database"]["leads"].insert_one({
        "_id": plid, "phone": "9110000001", "first_name": "Synthetic",
        "last_name": "Lead", "project": "LOFT",
    })


def test_unauthenticated_request_rejected(dash, monkeypatch):
    mock = _mock_fire_call(monkeypatch)
    c = dash.app.test_client()
    r = c.post("/api/v2/leads/bulk-trigger", json={"plids": ["1-LOFT"]})
    assert r.status_code == 401
    mock.assert_not_called()


def test_viewer_receives_403(dash, monkeypatch):
    mock = _mock_fire_call(monkeypatch)
    c = dash.app.test_client()
    _login(c, role="viewer")
    r = c.post("/api/v2/leads/bulk-trigger", json={"plids": ["1-LOFT"]}, headers=_CSRF_BYPASS)
    assert r.status_code == 403
    mock.assert_not_called()


def test_admin_reaches_existing_handler(dash, monkeypatch):
    mock = _mock_fire_call(monkeypatch)
    _seed_lead(dash)
    c = dash.app.test_client()
    _login(c, role="admin")
    r = c.post("/api/v2/leads/bulk-trigger", json={"plids": ["1-LOFT"]}, headers=_CSRF_BYPASS)
    assert r.status_code == 200
    assert r.get_json()["triggered"] == 1
    mock.assert_called_once()
    assert mock.call_args[0][1] == "1-LOFT"   # (zoho_db, plid, ...) — plid is arg index 1


def test_fire_call_not_invoked_for_unauthenticated(dash, monkeypatch):
    mock = _mock_fire_call(monkeypatch)
    _seed_lead(dash)
    c = dash.app.test_client()
    c.post("/api/v2/leads/bulk-trigger", json={"plids": ["1-LOFT"]})
    mock.assert_not_called()


def test_fire_call_not_invoked_for_viewer(dash, monkeypatch):
    """Seed a real, resolvable lead — otherwise this would pass even with no
    role check at all, simply because "Lead not found" short-circuits before
    fire_call regardless of who's asking. The proof needs a lead fire_call
    actually *could* reach if nothing stopped it first."""
    mock = _mock_fire_call(monkeypatch)
    _seed_lead(dash)
    c = dash.app.test_client()
    _login(c, role="viewer")
    c.post("/api/v2/leads/bulk-trigger", json={"plids": ["1-LOFT"]}, headers=_CSRF_BYPASS)
    mock.assert_not_called()


def test_malformed_viewer_request_cannot_bypass_role_check(dash, monkeypatch):
    """Garbage body, missing plids, wrong types, even non-JSON — none of it
    may let a viewer past the role gate; 403 must win before any body
    validation or parsing is attempted."""
    mock = _mock_fire_call(monkeypatch)
    c = dash.app.test_client()
    _login(c, role="viewer")
    for payload in [{}, {"plids": "not-a-list"}, {"plids": None}, {"garbage": True}]:
        r = c.post("/api/v2/leads/bulk-trigger", json=payload, headers=_CSRF_BYPASS)
        assert r.status_code == 403, payload
    r = c.post("/api/v2/leads/bulk-trigger", data="not json at all",
               content_type="application/json", headers=_CSRF_BYPASS)
    assert r.status_code == 403
    mock.assert_not_called()


def test_spoofed_role_in_json_body_ignored(dash, monkeypatch):
    mock = _mock_fire_call(monkeypatch)
    c = dash.app.test_client()
    _login(c, role="viewer")
    r = c.post("/api/v2/leads/bulk-trigger", json={"plids": ["1-LOFT"], "role": "admin"},
               headers=_CSRF_BYPASS)
    assert r.status_code == 403
    mock.assert_not_called()


def test_spoofed_role_in_query_params_ignored(dash, monkeypatch):
    mock = _mock_fire_call(monkeypatch)
    c = dash.app.test_client()
    _login(c, role="viewer")
    r = c.post("/api/v2/leads/bulk-trigger?role=admin", json={"plids": ["1-LOFT"]},
               headers=_CSRF_BYPASS)
    assert r.status_code == 403
    mock.assert_not_called()


def test_spoofed_role_in_headers_ignored(dash, monkeypatch):
    mock = _mock_fire_call(monkeypatch)
    c = dash.app.test_client()
    _login(c, role="viewer")
    headers = dict(_CSRF_BYPASS, **{"X-Role": "admin", "X-User-Role": "admin"})
    r = c.post("/api/v2/leads/bulk-trigger", json={"plids": ["1-LOFT"]}, headers=headers)
    assert r.status_code == 403
    mock.assert_not_called()


def test_existing_admin_validation_behaviour_unchanged(dash, monkeypatch):
    """Pre-existing body validation (empty plids, over-limit) must still
    behave exactly as before, for an admin session."""
    mock = _mock_fire_call(monkeypatch)
    c = dash.app.test_client()
    _login(c, role="admin")
    r = c.post("/api/v2/leads/bulk-trigger", json={"plids": []}, headers=_CSRF_BYPASS)
    assert r.status_code == 400
    r2 = c.post("/api/v2/leads/bulk-trigger",
                json={"plids": ["x"] * (crm_v2.BULK_TRIGGER_MAX + 1)}, headers=_CSRF_BYPASS)
    assert r2.status_code == 400
    mock.assert_not_called()
