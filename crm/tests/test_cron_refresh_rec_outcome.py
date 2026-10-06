"""Regression test for REC-OUTCOME-01 (UnboundLocalError: 'rec_outcome').

In cron_refresh's recordings-only loop, the inbound branch never assigned
`rec_outcome`, so building the call record raised UnboundLocalError for any lead
whose recording-only call was inbound (no transcript). The per-lead try/except
caught it → that lead was skipped (errors:1). Synthetic data only — no PII.

Fails before the fix (errors >= 1, UnboundLocalError), passes after (errors == 0,
inbound call recorded with an outcome).
"""
from datetime import datetime, timezone, timedelta
from bson import ObjectId

SECRET = "test-secret"
HDR = {"X-Cron-Key": SECRET}


def _seed_lead_with_recording(dash, rec):
    """A recent loft lead + one call_recording (no transcript) so processing hits
    the recordings-only loop. Returns the loft lead _id (str)."""
    now = datetime.now(timezone.utc)
    lid = ObjectId()
    dash.client["asbl_loft"]["leads"].insert_one(
        {"_id": lid, "phone": "919990002220", "name": "x", "source": {}, "createdAt": now})
    rec = {**rec, "_id": ObjectId(), "phone_number": "919990002220",
           "created_at": now, "recording_duration_secs": rec.get("recording_duration_secs", 30)}
    dash.client["ASBLVoiceBot"]["call_recordings"].insert_one(rec)
    return str(lid)


def test_inbound_recording_does_not_crash(dash, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", SECRET)
    lid = _seed_lead_with_recording(
        dash, {"call_sid": "inb-1", "call_direction": "inbound", "call_status": "completed"})
    body = dash.app.test_client().get("/api/cron/refresh", headers=HDR).get_json()

    assert body.get("errors", 0) == 0, (
        "inbound recording crashed the lead: %r" % body.get("error_details"))
    prof = dash.client["analytics_db"]["lead_profiles"].find_one({"lead_id": lid})
    calls = (prof or {}).get("ai_calls", {}).get("call_conversations", [])
    inbound = [c for c in calls if c.get("call_direction") == "inbound"]
    assert inbound, "inbound call not recorded in profile"
    assert inbound[0].get("call_outcome"), "inbound call outcome is blank/None"


def test_outbound_completed_recording_still_connected(dash, monkeypatch):
    """Regression guard: existing outbound behaviour must be unchanged."""
    monkeypatch.setenv("CRON_SECRET", SECRET)
    lid = _seed_lead_with_recording(
        dash, {"call_sid": "out-1", "call_direction": "outbound", "call_status": "completed"})
    body = dash.app.test_client().get("/api/cron/refresh", headers=HDR).get_json()
    assert body.get("errors", 0) == 0
    prof = dash.client["analytics_db"]["lead_profiles"].find_one({"lead_id": lid})
    calls = (prof or {}).get("ai_calls", {}).get("call_conversations", [])
    out = [c for c in calls if c.get("call_direction") == "outbound"]
    assert out and out[0].get("call_outcome") == "CONNECTED"


def test_mixed_recordings_one_inbound_all_processed(dash, monkeypatch):
    """One inbound record must not prevent the lead (and its other calls) refreshing."""
    monkeypatch.setenv("CRON_SECRET", SECRET)
    now = datetime.now(timezone.utc)
    lid = ObjectId()
    dash.client["asbl_loft"]["leads"].insert_one(
        {"_id": lid, "phone": "919990002221", "name": "y", "source": {}, "createdAt": now})
    for cs, d, st in [("m-in", "inbound", "completed"), ("m-out", "outbound", "completed")]:
        dash.client["ASBLVoiceBot"]["call_recordings"].insert_one(
            {"_id": ObjectId(), "phone_number": "919990002221", "call_sid": cs,
             "call_direction": d, "call_status": st, "recording_duration_secs": 20, "created_at": now})
    body = dash.app.test_client().get("/api/cron/refresh", headers=HDR).get_json()
    assert body.get("errors", 0) == 0, body.get("error_details")
    prof = dash.client["analytics_db"]["lead_profiles"].find_one({"lead_id": str(lid)})
    calls = (prof or {}).get("ai_calls", {}).get("call_conversations", [])
    assert len(calls) >= 2 and all(c.get("call_outcome") for c in calls)
