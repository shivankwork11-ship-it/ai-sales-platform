"""Security regression tests for /api/cron/refresh authentication (audit: public
state-mutating endpoint). The endpoint must require a shared-secret header
(X-Cron-Key == env CRON_SECRET), reject missing/incorrect credentials, fail
closed when the server secret is unset, and never execute the refresh for an
unauthorised request. Internal admin force-recompute callers (_force_all_zoho)
are unaffected.

Pre-fix: the endpoint is public, so unauthorised requests return 200 and run →
the '401' assertions below FAIL on current code (proving the finding).
"""
SECRET = "test-cron-secret"


def _get(dash, headers=None):
    return dash.app.test_client().get("/api/cron/refresh", headers=headers or {})


def test_no_credential_rejected(dash, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", SECRET)
    r = _get(dash)
    assert r.status_code == 401, "no-credential request must be rejected"


def test_wrong_credential_rejected(dash, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", SECRET)
    r = _get(dash, {"X-Cron-Key": "wrong"})
    assert r.status_code == 401


def test_correct_credential_accepted(dash, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", SECRET)
    r = _get(dash, {"X-Cron-Key": SECRET})
    assert r.status_code == 200
    assert r.get_json().get("status") == "success"  # no-activity run == success


def test_missing_server_secret_fails_closed(dash, monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    r = _get(dash, {"X-Cron-Key": SECRET})
    assert r.status_code == 401, "must fail closed when server secret is unset"


def test_unauthorised_does_not_execute_refresh(dash, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", SECRET)
    # a lead that WOULD be refreshed if the logic ran
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    dash.client["asbl_loft"]["leads"].insert_one(
        {"phone": "9990001111", "name": "x", "source": {}, "createdAt": now})
    r = _get(dash)  # no credential
    assert r.status_code == 401
    # refresh must NOT have run: no success marker, no throttle stamp, no profile
    cm = dash.client["analytics_db"]["cron_meta"].find_one({"_id": "cron_refresh"})
    assert not (cm and cm.get("last_started")), "refresh side-effects ran for an unauthorised request"
    assert dash.client["analytics_db"]["lead_profiles"].count_documents({}) == 0


def test_secret_not_leaked_in_response(dash, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", SECRET)
    body = _get(dash, {"X-Cron-Key": "wrong"}).get_data(as_text=True)
    assert SECRET not in body


def test_worker_caller_still_works(dash, monkeypatch):
    """The repository's only HTTP caller (worker.py) must remain compatible."""
    monkeypatch.setenv("CRON_SECRET", SECRET)
    import worker
    code, _body = worker._run_refresh()
    assert code == 200, "authorised worker call must succeed after the fix"
