"""Tests for the CRM-embeddable /embed/leads route and its frame-ancestors CSP.

/embed/leads reuses the exact same INDEX_HTML template and backend data source
as the main Leads tab (/) — no duplicated business logic, no separate query
path — it is rendered with embed=True, which Jinja-gates out the nav bar, Sync
Now, and Export, and forces IS_ADMIN off regardless of the session's actual
role (embed is a hard override, not merely another role check).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _login(client, role="viewer"):
    with client.session_transaction() as s:
        s["user_email"] = "t@asbl.in"
        s["role"] = role


def test_embed_requires_authentication(dash):
    c = dash.app.test_client()
    r = c.get("/embed/leads")
    assert r.status_code == 302
    assert r.headers["Location"].startswith("/login")


def test_unauthenticated_embed_redirect_preserves_return_path(dash):
    """Login must carry the visitor back to /embed/leads, not the full
    dashboard — otherwise every logged-out CRM iframe load lands on the full
    dashboard (nav, Sync Now, everything) right after signing in."""
    c = dash.app.test_client()
    r = c.get("/embed/leads")
    assert r.headers["Location"] == "/login?next=%2Fembed%2Fleads"


def test_login_then_post_redirects_back_to_embed_route(dash):
    c = dash.app.test_client()
    dash.client["analytics_db"]["dashboard_users"].insert_one({
        "_id": "t@asbl.in",
        "hash": dash.generate_password_hash("pw123456"),
        "role": "viewer", "name": "Tester",
    })
    r = c.post("/api/login", data={"email": "t@asbl.in", "password": "pw123456",
                                    "next": "/embed/leads"})
    assert r.headers["Location"] == "/embed/leads"


def test_login_ignores_external_next_url(dash):
    """Open-redirect guard: an absolute/external next must be ignored."""
    c = dash.app.test_client()
    dash.client["analytics_db"]["dashboard_users"].insert_one({
        "_id": "t2@asbl.in",
        "hash": dash.generate_password_hash("pw123456"),
        "role": "viewer", "name": "Tester2",
    })
    for bad_next in ["https://evil.example/", "//evil.example/", "http://evil.example"]:
        r = c.post("/api/login", data={"email": "t2@asbl.in", "password": "pw123456",
                                        "next": bad_next})
        assert r.headers["Location"] == "/", bad_next


def test_safe_next_accepts_valid_internal_path(dash):
    assert dash._safe_next("/embed/leads") == "/embed/leads"


def test_safe_next_rejects_full_attack_matrix(dash):
    """Every category from the pre-push review: absolute URLs, protocol-
    relative, backslash tricks (WHATWG normalises \\ to / for http(s), so
    "/\\evil.example" can behave like "//evil.example" in a real browser),
    embedded credentials, and malformed values — all must fail closed to "/".
    """
    bad_values = [
        "https://evil.example",
        "http://evil.example",
        "//evil.example",
        "/\\evil.example",          # backslash-based external path
        "\\\\evil.example",
        "\\/evil.example",
        "/%5Cevil.example",         # encoded backslash
        "/%2F%2Fevil.example",      # encoded protocol-relative
        "https://user:pass@evil.example",   # embedded credentials
        "/@evil.example\\@x",
        "not a url at all \x00\x01",  # malformed / control characters
        "",
        None,
    ]
    for bad in bad_values:
        assert dash._safe_next(bad) == "/", f"should fail closed for: {bad!r}"


def test_safe_next_handles_werkzeug_decoded_encoded_protocol_relative(dash):
    """End-to-end through the real route: Werkzeug decodes the query string
    before _safe_next ever sees it, so a %2F%2F-encoded value arrives already
    resolved to //evil.example — confirm the full request path still rejects it."""
    c = dash.app.test_client()
    r = c.get("/login?next=%2F%2Fevil.example")
    body = r.get_data(as_text=True)
    assert 'value="/"' in body or 'value=""' in body or "//evil.example" not in body


def test_authenticated_embed_returns_leads_table(dash):
    c = dash.app.test_client()
    _login(c)
    r = c.get("/embed/leads")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'id="leads-table"' in body
    assert 'id="leads-body"' in body
    assert 'id="search"' in body


def test_embed_excludes_bulk_calls_nav(dash):
    c = dash.app.test_client()
    _login(c)
    body = c.get("/embed/leads").get_data(as_text=True)
    assert 'href="/bulk-calls"' not in body
    assert 'class="nav-links"' not in body


def test_embed_excludes_qa_nav(dash):
    c = dash.app.test_client()
    _login(c)
    body = c.get("/embed/leads").get_data(as_text=True)
    assert 'href="/qa"' not in body


def test_embed_excludes_pipeline_nav(dash):
    c = dash.app.test_client()
    _login(c)
    body = c.get("/embed/leads").get_data(as_text=True)
    assert 'href="/pipeline"' not in body


def test_embed_excludes_sync_now(dash):
    c = dash.app.test_client()
    _login(c)
    body = c.get("/embed/leads").get_data(as_text=True)
    assert 'id="sync-btn"' not in body


def test_embed_excludes_export(dash):
    c = dash.app.test_client()
    _login(c)
    body = c.get("/embed/leads").get_data(as_text=True)
    assert '<a href="/api/export" id="export-btn"' not in body


def test_embed_excludes_admin_controls_even_for_admin_session(dash):
    """embed forces IS_ADMIN off — this must hold even when the logged-in
    session role really is admin, since the CRM iframe should never expose
    dashboard-admin affordances regardless of who's on the other end."""
    c = dash.app.test_client()
    _login(c, role="admin")
    body = c.get("/embed/leads").get_data(as_text=True)
    assert "const IS_ADMIN = false" in body
    assert "const IS_ADMIN = true" not in body


def test_root_route_unchanged(dash):
    """Regression guard: / must still show everything the embed route hides."""
    c = dash.app.test_client()
    _login(c, role="admin")
    body = c.get("/").get_data(as_text=True)
    assert 'href="/bulk-calls"' in body
    assert 'href="/qa"' in body
    assert 'href="/pipeline"' in body
    assert 'id="sync-btn"' in body
    assert "const IS_ADMIN = true" in body


def test_bulk_calls_qa_pipeline_routes_unaffected(dash):
    c = dash.app.test_client()
    _login(c)
    assert c.get("/bulk-calls").status_code == 200
    assert c.get("/qa").status_code == 200
    assert c.get("/pipeline").status_code == 200


def test_embed_frame_ancestors_includes_configured_crm_origin(dash, monkeypatch):
    monkeypatch.setenv("CRM_FRAME_ORIGIN", "https://crm.example.test")
    c = dash.app.test_client()
    _login(c)
    r = c.get("/embed/leads")
    assert r.headers.get("Content-Security-Policy") == "frame-ancestors 'self' https://crm.example.test"


def test_crm_frame_origin_with_explicit_https_port_preserved(dash, monkeypatch):
    monkeypatch.setenv("CRM_FRAME_ORIGIN", "https://crm.example.com:8443")
    c = dash.app.test_client()
    _login(c)
    r = c.get("/embed/leads")
    assert r.headers.get("Content-Security-Policy") == "frame-ancestors 'self' https://crm.example.com:8443"


def test_embed_frame_ancestors_never_wildcard(dash, monkeypatch):
    monkeypatch.setenv("CRM_FRAME_ORIGIN", "https://crm.example.test")
    c = dash.app.test_client()
    _login(c)
    r = c.get("/embed/leads")
    assert "*" not in r.headers.get("Content-Security-Policy", "")


def test_missing_crm_frame_origin_fails_closed(dash, monkeypatch):
    monkeypatch.delenv("CRM_FRAME_ORIGIN", raising=False)
    c = dash.app.test_client()
    _login(c)
    r = c.get("/embed/leads")
    assert r.headers.get("Content-Security-Policy") == "frame-ancestors 'self'"


def test_invalid_crm_frame_origin_fails_closed(dash, monkeypatch):
    bad_values = [
        "https://crm.example.test/some/path",
        "https://crm.example.test?x=1",
        "https://crm.example.test#frag",
        "https://*.example.test",
        "*",
        "http://crm.example.test",
        "https://user:pass@crm.example.test",
        "not-a-url",
    ]
    c = dash.app.test_client()
    _login(c)
    for bad in bad_values:
        monkeypatch.setenv("CRM_FRAME_ORIGIN", bad)
        r = c.get("/embed/leads")
        assert r.headers.get("Content-Security-Policy") == "frame-ancestors 'self'", f"should fail closed for: {bad!r}"


def test_unrelated_routes_stay_self_only(dash, monkeypatch):
    monkeypatch.setenv("CRM_FRAME_ORIGIN", "https://crm.example.test")
    c = dash.app.test_client()
    _login(c)
    for path in ["/", "/bulk-calls", "/qa", "/pipeline"]:
        r = c.get(path)
        assert r.headers.get("Content-Security-Policy") == "frame-ancestors 'self'", path


def test_frame_ancestors_ignores_hostile_headers(dash, monkeypatch):
    """Referer/Origin/X-Forwarded-Host are attacker-controlled on any request
    — the allowed frame ancestor must come only from CRM_FRAME_ORIGIN."""
    monkeypatch.setenv("CRM_FRAME_ORIGIN", "https://crm.example.test")
    c = dash.app.test_client()
    _login(c)
    r = c.get("/embed/leads", headers={
        "Referer": "https://attacker.example",
        "Origin": "https://attacker.example",
        "X-Forwarded-Host": "attacker.example",
    })
    assert r.headers.get("Content-Security-Policy") == "frame-ancestors 'self' https://crm.example.test"


def test_frame_ancestors_ignores_query_parameters(dash, monkeypatch):
    monkeypatch.setenv("CRM_FRAME_ORIGIN", "https://crm.example.test")
    c = dash.app.test_client()
    _login(c)
    r = c.get("/embed/leads?crm_frame_origin=https://attacker.example")
    assert r.headers.get("Content-Security-Policy") == "frame-ancestors 'self' https://crm.example.test"


def test_login_page_allows_configured_crm_origin(dash, monkeypatch):
    """/login must also be embeddable by the CRM — it's the auth step the
    embed flow redirects to when the session isn't logged in yet."""
    monkeypatch.setenv("CRM_FRAME_ORIGIN", "https://crm.example.test")
    c = dash.app.test_client()
    r = c.get("/login")
    assert r.headers.get("Content-Security-Policy") == "frame-ancestors 'self' https://crm.example.test"


def test_no_conflicting_x_frame_options_on_embed_route(dash):
    c = dash.app.test_client()
    _login(c)
    r = c.get("/embed/leads")
    assert "X-Frame-Options" not in r.headers


def test_embed_search_markup_present(dash):
    """Regression guard: confirm the embed wrap didn't accidentally swallow the
    search input along with the nav/buttons it's meant to hide."""
    c = dash.app.test_client()
    _login(c)
    body = c.get("/embed/leads").get_data(as_text=True)
    assert 'id="search"' in body


def test_embed_lead_detail_view_reachable_and_still_hides_nav(dash):
    """The one interaction explicitly required inside the embed: opening a
    lead's detail. Must work, and must not reintroduce the excluded nav."""
    c = dash.app.test_client()
    _login(c)
    dash.client["analytics_db"]["lead_profiles"].insert_one({
        "lead_id": "EMBED-TEST-1", "lead_name": "Embed Test Lead",
    })
    r = c.get("/lead/EMBED-TEST-1?embed=1")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'href="/bulk-calls"' not in body
    assert 'class="nav-links"' not in body


def test_embed_lead_detail_frame_ancestors_allows_crm(dash, monkeypatch):
    monkeypatch.setenv("CRM_FRAME_ORIGIN", "https://crm.example.test")
    c = dash.app.test_client()
    _login(c)
    dash.client["analytics_db"]["lead_profiles"].insert_one({
        "lead_id": "EMBED-TEST-2", "lead_name": "Embed Test Lead 2",
    })
    r = c.get("/lead/EMBED-TEST-2?embed=1")
    assert r.headers.get("Content-Security-Policy") == "frame-ancestors 'self' https://crm.example.test"


def test_unembedded_lead_detail_still_shows_nav(dash):
    c = dash.app.test_client()
    _login(c)
    dash.client["analytics_db"]["lead_profiles"].insert_one({
        "lead_id": "PLAIN-TEST-1", "lead_name": "Plain Test Lead",
    })
    r = c.get("/lead/PLAIN-TEST-1")
    assert r.status_code == 200
    assert 'href="/bulk-calls"' in r.get_data(as_text=True)


def test_embed_route_not_public_no_data_leak_unauthenticated(dash):
    c = dash.app.test_client()
    r = c.get("/embed/leads")
    assert r.status_code == 302
    assert "leads-table" not in r.get_data(as_text=True)
    assert "/embed/leads" not in dash._PUBLIC_ROUTES
