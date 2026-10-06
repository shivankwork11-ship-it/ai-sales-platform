"""Regression tests for PARTIAL-SUCCESS-01.

cron_refresh returned status:"ok" regardless of per-lead errors, and the worker
set last_ok from the HTTP 200 alone — so a partial refresh (some leads saved,
some failed) looked fully healthy. These tests pin the intended semantics:
  success  -> errors == 0
  partial  -> errors > 0 and refreshed > 0   (successful leads still saved)
  failed   -> refreshed == 0
Deterministic failures are injected by wrapping leads_col.update_one for chosen
lead_ids only (no real bug, no PII). Fails on current code (status is "ok").
"""
from datetime import datetime, timezone

SECRET = "test-secret"
HDR = {"X-Cron-Key": SECRET}


def _seed_zoho(dash, plid, phone):
    dash.client["Zoho_Database"]["leads"].insert_one(
        {"_id": plid, "phone": phone, "source": {"utmSource": "inncircles"},
         "createdAt": datetime.now(timezone.utc)})


def _fail_on(dash, monkeypatch, fail_ids):
    """Make leads_col.update_one raise for the given lead_ids only (per-lead
    failure), passing every other write (incl. cron_meta) through untouched."""
    orig = dash.leads_col.update_one

    def wrapper(flt, *a, **k):
        if isinstance(flt, dict) and flt.get("lead_id") in fail_ids:
            raise RuntimeError("injected failure")
        return orig(flt, *a, **k)
    monkeypatch.setattr(dash.leads_col, "update_one", wrapper)


def test_status_success(dash, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", SECRET)
    _seed_zoho(dash, "9100-SPECTRA", "919100000001")
    body = dash.app.test_client().get("/api/cron/refresh", headers=HDR).get_json()
    assert body["status"] == "success" and body["errors"] == 0 and body["refreshed"] >= 1
    cm = dash.client["analytics_db"]["cron_meta"].find_one({"_id": "cron_refresh"})
    assert cm.get("last_status") == "success" and cm.get("last_errors") == 0
    assert cm.get("last_successful_run") is not None


def test_status_partial(dash, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", SECRET)
    _seed_zoho(dash, "9100-SPECTRA", "919100000001")   # succeeds
    _seed_zoho(dash, "9101-SPECTRA", "919100000002")   # fails
    _fail_on(dash, monkeypatch, {"ZOHO-9101-SPECTRA"})
    body = dash.app.test_client().get("/api/cron/refresh", headers=HDR).get_json()
    assert body["status"] == "partial", "partial refresh must not report full success"
    assert body["errors"] >= 1 and body["refreshed"] >= 1  # some failed, some saved
    # successful work preserved
    assert dash.leads_col.find_one({"lead_id": "ZOHO-9100-SPECTRA"}) is not None
    cm = dash.client["analytics_db"]["cron_meta"].find_one({"_id": "cron_refresh"})
    assert cm.get("last_status") == "partial" and cm.get("last_errors") >= 1
    # freshness marker must NOT advance on partial (failed leads not skipped next run)
    assert cm.get("last_successful_run") is None


def test_status_failed(dash, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", SECRET)
    _seed_zoho(dash, "9100-SPECTRA", "919100000001")
    _fail_on(dash, monkeypatch, {"ZOHO-9100-SPECTRA"})
    body = dash.app.test_client().get("/api/cron/refresh", headers=HDR).get_json()
    assert body["status"] == "failed" and body["refreshed"] == 0 and body["errors"] >= 1
    cm = dash.client["analytics_db"]["cron_meta"].find_one({"_id": "cron_refresh"})
    assert cm.get("last_status") == "failed"
    assert cm.get("last_successful_run") is None


def test_worker_records_partial(dash, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", SECRET)
    _seed_zoho(dash, "9100-SPECTRA", "919100000001")
    _seed_zoho(dash, "9101-SPECTRA", "919100000002")
    _fail_on(dash, monkeypatch, {"ZOHO-9101-SPECTRA"})
    import worker
    code, body = worker._run_refresh()  # the worker loop records body['status'] into _last verbatim
    assert code == 200
    assert body.get("status") == "partial"
    assert (body.get("errors") or 0) >= 1


def test_stats_exposes_last_run_status(dash, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", SECRET)
    _seed_zoho(dash, "9100-SPECTRA", "919100000001")
    _seed_zoho(dash, "9101-SPECTRA", "919100000002")
    _fail_on(dash, monkeypatch, {"ZOHO-9101-SPECTRA"})
    c = dash.app.test_client()
    with c.session_transaction() as s:   # /api/stats requires a logged-in session
        s["user_email"] = "t@asbl.in"
    c.get("/api/cron/refresh", headers=HDR)
    stats = c.get("/api/stats").get_json()
    assert stats.get("last_status") == "partial"
    assert stats.get("last_errors") >= 1
