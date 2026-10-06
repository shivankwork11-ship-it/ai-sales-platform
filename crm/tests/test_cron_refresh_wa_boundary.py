"""Regression test for the WhatsApp date-boundary refresh bug (audit finding A7-05).

cron_refresh's "new WhatsApp activity" scan only queried the CURRENT UTC day's
`by_date` bucket; `yesterday_key` was computed but never used. So a lead whose
only recent signal is a WhatsApp message dated on the previous UTC day was never
picked up for recompute (overnight / gap-recovery runs silently skipped it).

This test isolates that path: an already-profiled ZOHO lead, sourced and old
(so it is NOT re-pulled by the "recent"/blank-source paths), with a WhatsApp doc
bucketed under YESTERDAY and NO `last_message_at` (so the latest-activity scan,
which keys on last_message_at, cannot mask the bug). The ONLY way this lead can
enter the refresh set is the by_date WhatsApp scan → the ZOHO phone-match path.

Fails before the fix (refreshed == 0), passes after (refreshed >= 1).
"""
from datetime import datetime, timezone, timedelta


def test_whatsapp_yesterday_bucket_gets_refreshed(dash, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "test-secret")  # endpoint now requires auth
    d = dash.client
    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    phone10 = "9998887770"

    # Source ZOHO lead (old + sourced → not "recent", not blank-source).
    d["Zoho_Database"]["leads"].insert_one({
        "_id": "9001-SPECTRA",
        "phone": "91" + phone10,
        "source": {"utmSource": "inncircles"},
        "createdAt": now - timedelta(days=10),
    })
    # Its existing profile (already profiled → covered).
    d["analytics_db"]["lead_profiles"].insert_one({
        "lead_id": "ZOHO-9001-SPECTRA",
        "lead_phone_number": phone10,
        "lead_source": "inncircles",
        "project": "SPECTRA",
        "updated_at": now - timedelta(days=5),
        "lead_created_at": now - timedelta(days=10),
    })
    # WhatsApp activity bucketed under YESTERDAY, deliberately no last_message_at.
    d["Zoho_Database"]["whatsapp_messages"].insert_one({
        "_id": "91" + phone10,
        "by_date": {yesterday: {"inbound": [{"message": "hi"}]}},
    })

    resp = dash.app.test_client().get(
        "/api/cron/refresh", headers={"X-Cron-Key": "test-secret"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body.get("refreshed", 0) >= 1, (
        "lead with WhatsApp activity dated yesterday (UTC) was not refreshed: %r" % body
    )
