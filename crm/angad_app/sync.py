"""
STEP 2 — Sync Mongo -> local parquet cache.

Pulls every collection, derives every metric once, writes local parquet. The
dashboard reads only the parquet.

PII: phone numbers are NEVER written to the cache. Each is replaced with a
deterministic pseudonym (LEAD-7f3a91c2) so per-lead history and cross-collection
joins still work. Anything phone-shaped inside transcripts/summaries is redacted
to [number]. Email is dropped. See pii.py.

    python sync.py --full                       # everything
    python sync.py --full --since 2026-06-01    # createdAt filter
    python sync.py --full --since 2026-06-01 --until 2026-07-31
    python sync.py                              # incremental since last run
"""
import argparse
import json
import os
import re
from datetime import date as dt_date, datetime, timedelta, timezone
from pathlib import Path

import readonly  # noqa: F401 - patches pymongo to refuse every write
import pandas as pd
import pii
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
URI = os.environ["MONGO_URI"]
DB = os.getenv("MONGO_DB", "angad")

DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)
STATE = DATA / "state.json"

IST = timezone(timedelta(hours=5, minutes=30))

# Collections holding plaintext secrets / credentials - never pulled.
SKIP = {"apikeys", "dashboard_users", "users", "login_attempts"}

# The other three databases on this cluster, per ASBL_MONGO_DATA_DICTIONARY.md.
# angad alone cannot answer the reporting questions: it is the dialer runtime.
# The conversation slots, the CRM funnel and the WhatsApp history live here.
OTHER_DBS = {
    # the dialer's intelligence store - THE source for budget/timeline/objections
    "ASBLVoiceBot": [
        "call_transcripts",                        # ~21,149 - all extracted slots
        "call_transcript_intent_identification",   # ~21,316 - post-call intent
        "call_schedules",                          # ~43,827 - schedule + outcome ledger
        "call_recordings",                         # ~18,408 - recording index
        "lead_journeys",                           # ~4,225  - per-phone call rollup
        "lead_profiles",                           # ~4,186  - per-phone AI profile
        "kb_store",
    ],
    # the legacy CRM brain - project, attribution, WhatsApp, site visits
    "Zoho_Database": [
        "leads",                  # ~4,039 - project, UTM, budget, call_status
        "posthook_payloads",      # ~3,362 - richest per-call source
        "whatsapp_messages",      # ~1,326 - per-phone aggregate
        "site_visit_bookings",    # 55
        "callback_log",           # 55
        "doc_send_log",           # 134 - which doc went to whom
        "follow_up_log",          # 402
        "user_profiles",          # 138 - AI lead profile
        "project_documents",      # 63
        "project_facts",          # 5
        "whatsapp_sender_map",    # 922
        "reactivation_list",      # ~2,999
        # NEW 2026-08-08: Gemini 2.5 Pro transcription written INSIDE Zoho.
        # Per the dev: the agent now only ships the recording; transcription,
        # stage transition and follow-ups fire from here (~90% accurate).
        # This is the corpus that replaces the agent's weak live transcript.
        "call_transcripts", "crm_call_logs", "crm_notes", "cron_log",
        "call_throttle", "audit_logs",
        "call_block_list", "bot_settings", "workflow_queue",
        "mlid_registry", "plid_registry", "dead_senders",
        "stuck_sends",            # ~28,270 - the send backlog
    ],
    # the event-sourced rebuild - stage funnel + full per-lead timeline
    "Intelligent_CRM": [
        "crm_leads",              # ~3,910 - stage projection, the funnel
        "lead_events",            # ~28,657 - immutable timeline
        "calls",                  # ~7,175
        "messages",               # ~7,980
        "persons",                # ~3,110
        "site_visits",            # 55
        "stale_person_record",    # ~571
        "mlid_registry",
    ],
}

OUTCOME_GROUP = {
    "PRE_SITE": "Booked", "VIRTUAL_TOUR": "Booked",
    "AUTO_CALLBACK": "Engaged", "CONNECTED": "Engaged",
    "NOT_INTERESTED": "Rejected", "DNC_REQUESTED": "Rejected",
    "DROPPED": "Dropped",
    "NO_ANSWER": "Not reached", "NOT_CONNECTED": "Not reached",
    "BUSY": "Not reached", "SWITCHED_OFF": "Not reached",
    "VOICEMAIL": "Not reached",
}
REACHED = {"Booked", "Engaged", "Rejected"}

CALL_PROJECTION = {
    "roomName": 1, "phoneNumber": 1, "direction": 1, "status": 1, "outcome": 1,
    "startedAt": 1, "endedAt": 1, "durationSeconds": 1, "createdAt": 1,
    "updatedAt": 1, "slug": 1, "eventSlug": 1, "source": 1,
    "voiceEngine": 1, "externalScheduleId": 1, "externalCustomerId": 1,
    "agentPersonaId": 1, "callbackUrl": 1, "leadId": 1,
    "transcript": 1, "summary": 1,
    "extractedSlots": 1, "userSignals": 1, "buyingSignals": 1,
    "leadTemperature": 1, "objectionsHandled": 1, "sentiment": 1,
    "closingScore": 1, "handoffTriggered": 1, "recordingUrl": 1,
    "whatsappSent": 1, "whatsappSentAt": 1, "whatsappProject": 1,
    "whatsappDocs": 1, "siteOfficeSentAt": 1,
    "dncTriggered": 1, "dncVerbatim": 1, "tokenUsage": 1, "metadata": 1,
    "webhookDeliveries": 1,
}


def to_parquet_safe(df, path):
    """Write parquet, surviving mixed-type columns.

    These collections are not schema-enforced: the same field is a string in
    some docs and a datetime in others (created_at, updated_at). Arrow refuses
    to guess. Datetime-only columns become real timestamps; anything mixed is
    coerced to text so the column still arrives instead of the whole
    collection being dropped."""
    df = df.copy()
    for c in df.columns:
        if df[c].dtype != object:
            continue
        vals = df[c].dropna()
        if vals.empty:
            df[c] = df[c].astype("string")
            continue
        sample = vals.head(3000)
        # Booleans first: a bool/None column is 'mixed' to the naive check and
        # would be stringified to "True"/"False", which silently breaks every
        # boolean test downstream (is_qualified == True stopped matching).
        if all(isinstance(v, bool) for v in sample):
            df[c] = df[c].astype("boolean")
            continue
        all_dt = all(isinstance(v, datetime) for v in sample)
        # datetime.date is NOT a datetime instance - check it separately, or
        # the enriched "date" column silently becomes text and every date
        # comparison downstream breaks.
        all_date = (not all_dt) and all(isinstance(v, dt_date) for v in sample)
        all_str = all(isinstance(v, str) for v in sample)
        if all_dt:
            df[c] = pd.to_datetime(df[c], errors="coerce", utc=True)
        elif all_date:
            df[c] = pd.to_datetime(df[c], errors="coerce")
        elif all_str:
            df[c] = df[c].astype("string")
        else:
            df[c] = df[c].map(lambda v: None if v is None else str(v)).astype("string")
    try:
        df.to_parquet(path, index=False)
    except Exception:
        for c in df.columns:
            if df[c].dtype == object:
                df[c] = df[c].map(lambda v: None if v is None else str(v)).astype("string")
        df.to_parquet(path, index=False)


def get(doc, path, default=None):
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur if cur is not None else default


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(s):
    STATE.write_text(json.dumps(s, indent=2, default=str))


# ---------------------------------------------------------------- transcripts

def derive_transcript(turns, started_at):
    out = {
        "turns_total": 0, "turns_lead": 0, "turns_agent": 0,
        "chars_lead": 0, "chars_agent": 0, "lead_talk_share": None,
        "first_lead_reply_sec": None, "last_turn_sec": None,
        "first_agent_sec": None,
        "last_speaker": None, "lead_said": "", "agent_said": "",
        "lead_ever_spoke": False, "has_transcript": False,
    }
    if not isinstance(turns, list) or not turns:
        return out
    out["has_transcript"] = True

    lead_txt, agent_txt = [], []
    first_lead_ts = last_ts = first_agent_ts = None

    for t in turns:
        if not isinstance(t, dict):
            continue
        spk = str(t.get("speaker", "")).lower()
        txt = (t.get("text") or "").strip()
        ts = t.get("timestamp")
        out["turns_total"] += 1
        if isinstance(ts, datetime):
            last_ts = ts
        if spk == "lead":
            out["turns_lead"] += 1
            out["chars_lead"] += len(txt)
            lead_txt.append(txt)
            if first_lead_ts is None and isinstance(ts, datetime):
                first_lead_ts = ts
        elif spk == "agent":
            out["turns_agent"] += 1
            out["chars_agent"] += len(txt)
            agent_txt.append(txt)
            if first_agent_ts is None and isinstance(ts, datetime):
                first_agent_ts = ts
        out["last_speaker"] = spk

    tot = out["chars_lead"] + out["chars_agent"]
    if tot:
        out["lead_talk_share"] = out["chars_lead"] / tot
    out["lead_ever_spoke"] = out["turns_lead"] > 0
    # phone numbers spoken aloud get redacted out of the stored text
    out["lead_said"] = pii.clean_text(" | ".join(lead_txt))[:4000]
    # ENGAGEMENT STATE is a DESIGNED internal prompt function (user-confirmed):
    # never spoken on the call, but it enters Gemini's memory and therefore the
    # stored transcription. Strip it at ingest so transcript metrics stay clean.
    agent_join = re.sub(r"\[ENGAGEMENT STATE[^\]]*\]\.?", " ",
                        " | ".join(agent_txt))
    out["agent_said"] = pii.clean_text(agent_join)[:4000]

    if isinstance(started_at, datetime):
        if first_lead_ts:
            out["first_lead_reply_sec"] = (first_lead_ts - started_at).total_seconds()
        if last_ts:
            out["last_turn_sec"] = (last_ts - started_at).total_seconds()
        # VARAHA latency PROXY: first agent turn timestamp - call start.
        # The true ws->first-audio latency is console-logged and lost; this is
        # the closest measurable stand-in and must always be labelled a proxy.
        if first_agent_ts:
            out["first_agent_sec"] = (first_agent_ts - started_at).total_seconds()
    return out


def drop_stage(row):
    """WHERE the call died. Inferred - the carrier hangup cause is not stored."""
    dur = row.get("duration_sec") or 0
    if not row.get("has_transcript"):
        return "0. No transcript at all"
    if not row.get("lead_ever_spoke"):
        return "1. Customer never spoke"
    if row.get("turns_lead", 0) <= 1:
        return "2. One reply then gone"
    if dur < 30:
        return "3. Died in greeting (<30s)"
    if dur < 90:
        return "4. Died in pitch (30-90s)"
    if dur < 180:
        return "5. Died in qualification (90-180s)"
    return "6. Died late (>180s)"


# ---------------------------------------------------------------- flatten

def flatten_call(d):
    started = d.get("startedAt")
    row = {
        "_id": str(d.get("_id")),
        "roomName": d.get("roomName"),
        "lead_ref": pii.ref(d.get("phoneNumber")),   # <- no raw number, ever
        "leadId": str(d["leadId"]) if d.get("leadId") else None,
        "direction": d.get("direction"),
        "status": d.get("status"),
        "outcome": d.get("outcome"),
        "createdAt": d.get("createdAt"),
        "updatedAt": d.get("updatedAt"),
        "startedAt": started,
        "endedAt": d.get("endedAt"),
        "durationSeconds": d.get("durationSeconds"),
        "customerName": None,  # PII policy 2026-08-06: names never cached
        "slug": d.get("slug"),
        "eventSlug": d.get("eventSlug"),
        "source": d.get("source"),
        "voiceEngine": d.get("voiceEngine"),
        "externalScheduleId": d.get("externalScheduleId"),
        "externalCustomerId": d.get("externalCustomerId"),
        "agentPersonaId": d.get("agentPersonaId"),
        "summary": pii.clean_text(d.get("summary") or "")[:2000],
        "sentiment": d.get("sentiment"),
        "closingScore": d.get("closingScore"),
        "handoffTriggered": bool(d.get("handoffTriggered")),
        "leadTemperature": d.get("leadTemperature"),
        "has_recording": bool(d.get("recordingUrl")),
        # Slot field names: the agent writes budget_bucket / timeline_bucket /
        # configuration_interest / preferred_callback_time / objection_raised
        # (confirmed against Intelligent_CRM.calls and the classifier contract).
        # We previously read budget/timeline/config/callback/objection, which do
        # not exist - every one of those columns came back null, and we reported
        # that emptiness upstream as a data outage. It was our bug. Read the real
        # names first and keep the short names as a fallback for schema drift.
        "slot_intent": get(d, "extractedSlots.intent"),
        "slot_budget": (get(d, "extractedSlots.budget_bucket")
                        or get(d, "extractedSlots.budget")),
        "slot_timeline": (get(d, "extractedSlots.timeline_bucket")
                          or get(d, "extractedSlots.timeline")),
        "slot_config": (get(d, "extractedSlots.configuration_interest")
                        or get(d, "extractedSlots.config")),
        "slot_callback": (get(d, "extractedSlots.preferred_callback_time")
                          or get(d, "extractedSlots.callback")),
        "slot_site_visit": get(d, "extractedSlots.site_visit_requested"),
        "slot_objection": (get(d, "extractedSlots.objection_raised")
                           or get(d, "extractedSlots.objection")),
        "is_qualified": get(d, "userSignals.is_qualified"),
        "is_broker": get(d, "userSignals.is_likely_broker"),
        "language": get(d, "userSignals.language_detected"),
        "whatsappSent": bool(d.get("whatsappSent")),
        "whatsappSentAt": d.get("whatsappSentAt"),
        "whatsappProject": d.get("whatsappProject"),
        "siteOfficeSentAt": d.get("siteOfficeSentAt"),
        "dncTriggered": bool(d.get("dncTriggered")),
        "dncVerbatim": pii.clean_text(d.get("dncVerbatim") or "") or None,
        "tok_total": get(d, "tokenUsage.total"),
        "tok_prompt": get(d, "tokenUsage.prompt"),
        "tok_response": get(d, "tokenUsage.response"),
    }

    docs = d.get("whatsappDocs")
    row["whatsappDocs"] = ", ".join(map(str, docs)) if isinstance(docs, list) and docs else None
    obj = d.get("objectionsHandled")
    row["objections"] = ", ".join(map(str, obj)) if isinstance(obj, list) and obj else None

    wh = d.get("webhookDeliveries")
    if isinstance(wh, list) and wh:
        row["webhook_count"] = len(wh)
        row["webhook_status"] = ", ".join(
            sorted({str(w.get("status")) for w in wh if isinstance(w, dict)}))
        row["webhook_attempts"] = sum(int(w.get("attempts") or 0)
                                      for w in wh if isinstance(w, dict))
    else:
        row["webhook_count"], row["webhook_status"], row["webhook_attempts"] = 0, None, 0

    sig = d.get("buyingSignals")
    if isinstance(sig, list) and sig:
        row["buying_signal_count"] = len(sig)
        row["buying_signals"] = " | ".join(
            f"{s.get('signal')}({s.get('strength')})" for s in sig if isinstance(s, dict))[:1000]
        row["strong_signals"] = sum(1 for s in sig if isinstance(s, dict)
                                    and s.get("strength") == "strong")
    else:
        row["buying_signal_count"], row["buying_signals"], row["strong_signals"] = 0, None, 0

    row.update(derive_transcript(d.get("transcript"), started))
    return row


def flatten_lead(d):
    return {
        "_id": str(d.get("_id")),
        "lead_ref": pii.ref(d.get("phone")),
        "name": None,  # PII policy 2026-08-06: names never cached
        "lead_status": d.get("status"),
        "lead_source": d.get("source"),
        "propertyInterested": d.get("propertyInterested"),
        "leadScore": d.get("leadScore"),
        "lead_timeline": d.get("timeline"),
        "doNotCall": bool(d.get("doNotCall")),
        "doNotCallReason": d.get("doNotCallReason"),
        "siteVisitScheduledAt": d.get("siteVisitScheduledAt"),
        "lead_createdAt": d.get("createdAt"),
        "lead_updatedAt": d.get("updatedAt"),
        # email deliberately not carried
    }


def flatten_sched(d):
    return {
        "_id": str(d.get("_id")),
        "lead_ref": pii.ref(d.get("phone")),
        "leadId": str(d["leadId"]) if d.get("leadId") else None,
        "slug": d.get("slug"),
        "kind": d.get("kind"),
        "sched_status": d.get("status"),
        "dueAt": d.get("dueAt"),
        "visitAt": d.get("visitAt"),
        "attempts": d.get("attempts"),
        "label": d.get("label"),
        "sourceCallId": str(d.get("sourceCallId")) if d.get("sourceCallId") else None,
        "dialedCallId": d.get("dialedCallId"),
        "sched_createdAt": d.get("createdAt"),
    }


# --- generic flattener, used for every collection except angad.calls ---------

# any key whose value is a phone number -> becomes lead_ref, number discarded
PHONE_KEYS = {"phone", "phoneNumber", "phone_number", "phone_key", "to",
              "customer_phone", "customerPhone", "msisdn", "number"}
# never leaves Mongo
SECRET_KEYS = {"secret", "key", "apiKey", "keyHash", "token", "password_hash",
               "password", "email", "emails", "first_name", "last_name",
               # PII policy 2026-08-06: no personal names in the cache
               "name", "lead_name", "customer_name", "customerName",
               "customer_name_src", "Lead Name", "sanitized_name"}
# huge blobs that would bloat the cache with no analytical value
BLOB_KEYS = {"kb_text", "facts_text", "zoho", "payload", "body", "raw",
             "customerContext", "deferredRequest", "before", "after"}


def flatten_generic(d, depth=0, prefix=""):
    """Flatten one document to scalar columns.

    Nested objects expand to dotted columns (2 levels). Arrays become a _count
    plus a truncated JSON sample. Phones become lead_ref. Secrets are dropped.
    """
    out = {}
    for k, v in (d or {}).items():
        if k == "__v" or k in SECRET_KEYS:
            continue
        if k in PHONE_KEYS and isinstance(v, (str, int)):
            r = pii.ref(v)
            if r:
                out["lead_ref"] = r
            continue
        # Several collections key documents BY PHONE NUMBER (whatsapp_messages,
        # mlid_registry, reactivation_list, whatsapp_sender_map all use
        # _id = the phone). Catch any all-digit id that looks like a number.
        if k == "_id" and depth == 0 and re.fullmatch(r"\d{10,15}", str(v)):
            out["lead_ref"] = pii.ref(v)
            continue
        name = f"{prefix}{k}"
        if k in BLOB_KEYS:
            out[f"{name}_present"] = v is not None and v != {} and v != []
            continue
        if isinstance(v, str) and (k.endswith(("_id", "_sid", "Id", "ID"))
                                   or k in ("call_sid", "sid", "roomName",
                                            "external_schedule_id", "externalScheduleId")):
            out[name] = v[:200]
            continue
        if isinstance(v, dict):
            if depth < 2:
                out.update(flatten_generic(v, depth + 1, f"{name}."))
            else:
                out[f"{name}_json"] = json.dumps(v, default=str)[:800]
        elif isinstance(v, list):
            out[f"{name}_count"] = len(v)
            if v and not isinstance(v[0], (dict, list)):
                out[name] = ", ".join(map(str, v))[:600]
            elif v:
                out[f"{name}_json"] = json.dumps(v[:3], default=str)[:800]
        elif isinstance(v, str):
            # clean_text = redact() + addressed-name stripping. The Zoho Gemini
            # transcripts open with "Good evening, <Name>." and redact() alone
            # let that straight into the cache; audit_pii.py cannot see a name,
            # so it reported CLEAN over a real imprint-2 breach (found 2026-08-08).
            out[name] = pii.clean_text(v)[:3000]
        else:
            out[name] = str(v) if k == "_id" else v
    return out


# ---------------------------------------------------------------- fetch

def fetch(coll, flat, projection=None, match=None, label=""):
    q = dict(match or {})
    n = coll.count_documents(q) if q else coll.estimated_document_count()
    print(f"  {label}: fetching {n:,} docs...", flush=True)
    if n == 0:
        return pd.DataFrame()
    rows = []
    for i, d in enumerate(coll.find(q, projection).batch_size(200), 1):
        rows.append(flat(d))
        if i % 5000 == 0:
            print(f"    {i:,}/{n:,}", flush=True)
    df = pd.DataFrame(rows)
    assert_fields_resolve(df, label)
    return df


# ---------------------------------------------------------------- P2 gate
# NAMING GATE. Every derived column that reads a NESTED source path must
# resolve in at least one document of its collection. If it never resolves,
# the column is 100% null and we cannot tell "the field is empty" from "we
# spelled the field wrong" — which is exactly how the extractedSlots claim
# (RETRACTIONS R2) reached the dev team and cost them a full round of work.
#
# This does not guess. It reports the offending column names and refuses to
# let a run finish quietly, so the ambiguity is resolved at ingest, once,
# instead of surfacing as a false finding three turns later.
# Keyed by a SIGNATURE column, not by the label. The label "calls" is used for
# both angad.calls and Intelligent_CRM.calls, and keying on it made the gate
# fire against the wrong collection on its first real run — a false positive,
# which is the fastest way to teach everyone to ignore a gate. A gate must
# identify its own subject.
NAMED_FIELD_GATE = [
    {
        "signature": "externalScheduleId",     # only angad.calls carries this
        "subject": "angad.calls",
        "want": ["slot_intent", "slot_budget", "slot_timeline", "slot_config",
                 "slot_callback", "slot_site_visit", "slot_objection",
                 "tok_total", "tok_prompt", "tok_response"],
    },
]
_GATE_FAILURES = []


def assert_fields_resolve(df, label):
    if df is None or df.empty:
        return
    rule = next((r for r in NAMED_FIELD_GATE if r["signature"] in df.columns), None)
    if not rule:
        return
    want, label = rule["want"], rule["subject"]
    dead = []
    for cname in want:
        if cname not in df.columns:
            dead.append(f"{cname} (column absent)")
        elif df[cname].notna().sum() == 0:
            dead.append(f"{cname} (present, 0 non-null)")
    if dead:
        _GATE_FAILURES.append((label, dead))
        print(f"  !! NAMING GATE: {label} — these never resolved: "
              f"{', '.join(dead)}", flush=True)
        print("     A 100%-null column is EITHER no data OR a wrong field name. "
              "Resolve which before reporting the field empty anywhere.",
              flush=True)


def naming_gate_report():
    """Printed at the end of a sync so a failure cannot scroll past unnoticed."""
    if not _GATE_FAILURES:
        print("  naming gate: PASS — every watched field resolved in >=1 document")
        return True
    print("\n  !! NAMING GATE FAILED")
    for label, dead in _GATE_FAILURES:
        print(f"     {label}: {', '.join(dead)}")
    print("     Do NOT report any of these as 'empty' until the name is verified "
          "against the live document (RETRACTIONS R2).")
    return False


# ---------------------------------------------------------------- enrich

def enrich(calls):
    if calls.empty:
        return calls

    for c in ("createdAt", "startedAt", "endedAt", "whatsappSentAt", "siteOfficeSentAt"):
        calls[c] = pd.to_datetime(calls[c], errors="coerce", utc=True)

    calls["ts"] = calls["startedAt"].fillna(calls["createdAt"])
    calls["ts_ist"] = calls["ts"].dt.tz_convert(IST)
    calls["date"] = calls["ts_ist"].dt.date
    calls["hour"] = calls["ts_ist"].dt.hour
    calls["weekday"] = calls["ts_ist"].dt.day_name()
    calls["month"] = calls["ts_ist"].dt.strftime("%Y-%m")

    # Duration. durationSeconds is written on ~39% of calls only. endedAt minus
    # startedAt is NOT a safe fallback: on failed dials those stamps are hours
    # or days apart (observed max 687,241s = 8 days), which poisons every
    # median. So: trust durationSeconds when sane; fall back to wall-clock only
    # when it is plausible AND the call actually produced a transcript;
    # otherwise mark the duration unknown rather than inventing one.
    MAX_SANE = 3600  # a real sales call never runs an hour
    dur = pd.to_numeric(calls["durationSeconds"], errors="coerce")
    wall = (calls["endedAt"] - calls["startedAt"]).dt.total_seconds()
    dur_ok = dur.where(dur.between(0, MAX_SANE))
    wall_ok = wall.where(wall.between(0, MAX_SANE) & calls["has_transcript"])
    known = dur_ok.fillna(wall_ok)
    calls["duration_known"] = known.notna()
    calls["duration_sec"] = known.fillna(0)
    calls["duration_is_derived"] = dur_ok.isna() & wall_ok.notna()
    calls["duration_implausible"] = dur.notna() & ~dur.between(0, MAX_SANE)

    calls["outcome_group"] = calls["outcome"].map(OUTCOME_GROUP).fillna("Unclassified")
    calls["reached"] = calls["outcome_group"].isin(REACHED)
    calls["is_dropped"] = calls["outcome"].eq("DROPPED")
    calls["is_booked"] = calls["outcome_group"].eq("Booked")
    calls["call_failed"] = calls["status"].eq("failed")

    nm = calls["customerName"].fillna("").str.strip()
    calls["has_real_name"] = ~nm.str.lower().isin(
        ["", "sir", "madam", "customer", "none", "null", "na", "n/a"])
    calls["display_name"] = nm.where(calls["has_real_name"], "(no name)")

    # A voicemail greeting is transcribed as the LEAD speaking, so
    # lead_ever_spoke alone overstates human contact (761 VOICEMAIL calls have
    # "lead" turns). This is the honest "a human actually talked to us" flag.
    calls["is_voicemail"] = calls["outcome"].eq("VOICEMAIL")
    calls["human_conversation"] = (calls["lead_ever_spoke"] & ~calls["is_voicemail"]
                                   & calls["turns_lead"].ge(1))

    calls["drop_stage"] = calls.apply(
        lambda r: drop_stage(r) if r["is_dropped"] else None, axis=1)

    # ---- transcript-quality flags. The ASR output is demonstrably unreliable
    # (hallucinated Korean on Hyderabad calls, voicemail prompts transcribed as
    # the customer, bots stuck repeating one line). These flags quantify it per
    # call so every "what the customer said" metric can disclose its noise.
    a_txt = calls["agent_said"].fillna("")
    l_txt = calls["lead_said"].fillna("")
    dur = calls["duration_sec"]
    known = calls.get("duration_known", pd.Series(True, index=calls.index))

    calls["tq_no_transcript_connected"] = (
        calls["outcome"].eq("CONNECTED") & ~calls["has_transcript"])
    calls["tq_agent_monologue"] = (
        (pd.to_numeric(calls["lead_talk_share"], errors="coerce") < 0.05)
        & (dur > 60) & known & (a_txt.str.len() > 0))

    def _max_repeat(txt):
        # split on turn separator + sentence punctuation, count the most
        # repeated normalised sentence (>=15 chars)
        best, seen = 0, {}
        for part in re.split(r"[|.!?]+", txt.lower()):
            part = part.strip()
            if len(part) >= 15:
                seen[part] = seen.get(part, 0) + 1
                if seen[part] > best:
                    best = seen[part]
        return best
    calls["tq_repeated_line"] = a_txt.map(_max_repeat) >= 3

    chars = calls["chars_agent"].fillna(0) + calls["chars_lead"].fillna(0)
    cps = chars / dur.replace(0, pd.NA)
    calls["tq_impossible_density"] = (
        (cps > 25) & (dur >= 30) & known & calls["has_transcript"]).fillna(False)

    # Hangul / Thai / CJK / Cyrillic / Arabic in the LEAD channel = the ASR
    # hallucinating a language on line noise; never seen in the agent channel
    calls["tq_foreign_script"] = l_txt.str.contains(
        "[\uac00-\ud7af\u0e00-\u0e7f\u4e00-\u9fff\u0400-\u04ff\u0600-\u06ff]",
        regex=True)

    IVR = ("finished recording|voice ?mail|after the tone|after the beep|"
           "record your message|not reachable|switched off|is busy on another")
    calls["tq_ivr_as_customer"] = l_txt.str.lower().str.contains(IVR, regex=True)

    frs = pd.to_numeric(calls["first_lead_reply_sec"], errors="coerce")
    calls["tq_timing_broken"] = ((frs < 0) | (known & (frs > dur))).fillna(False)

    calls["tq_transcript_on_unanswered"] = (
        calls["outcome"].isin(["BUSY", "NOT_CONNECTED", "NO_ANSWER"])
        & calls["has_transcript"])

    TQ = [c for c in calls.columns if c.startswith("tq_")]
    calls["tq_any"] = calls[TQ].any(axis=1)

    calls = calls.sort_values(["lead_ref", "ts"]).reset_index(drop=True)
    g = calls.groupby("lead_ref", sort=False)
    calls["attempt_no"] = g.cumcount() + 1
    calls["total_attempts"] = g["roomName"].transform("size")
    calls["next_call_at"] = g["ts"].shift(-1)
    calls["next_outcome"] = g["outcome"].shift(-1)
    calls["next_outcome_group"] = g["outcome_group"].shift(-1)
    calls["prev_call_at"] = g["ts"].shift(1)
    calls["prev_outcome"] = g["outcome"].shift(1)
    calls["hours_to_next_call"] = (calls["next_call_at"] - calls["ts"]).dt.total_seconds() / 3600
    calls["hours_since_prev_call"] = (calls["ts"] - calls["prev_call_at"]).dt.total_seconds() / 3600
    calls["was_recontacted"] = calls["next_call_at"].notna()
    calls["recontact_reached"] = calls["next_outcome_group"].isin(REACHED)

    reached_ts = calls["ts"].where(calls["reached"])
    calls["first_reached_at"] = reached_ts.groupby(calls["lead_ref"]).transform("min")
    calls["hours_to_first_answer"] = (
        (calls["first_reached_at"] - calls["ts"]).dt.total_seconds() / 3600)
    calls["ever_reached"] = g["reached"].transform("max")

    # VARAHA: the era dimension - everything before 2026-07-17 belongs to the
    # Kaalbhojaditya era and is referred to by that name everywhere.
    ERA_CUT = pd.Timestamp("2026-07-17").date()
    calls["era"] = calls["date"].map(
        lambda d_: "Kaalbhojaditya" if pd.notna(d_) and d_ < ERA_CUT else "ANGAD")

    calls = calls.sort_values("ts", ascending=False).reset_index(drop=True)

    # VARAHA: keep ALL metrics stored - per-day per-era history, rebuilt
    # deterministically from the full log on every sync and persisted forever.
    try:
        g = calls.groupby(["date", "era"])
        hist = pd.DataFrame({
            "dials": g.size(),
            "connected_calls": g["reached"].sum(),
            "conversations": g["human_conversation"].sum(),
            "booked_leads": g.apply(
                lambda x: x.loc[x["outcome"].isin(["PRE_SITE", "VIRTUAL_TOUR"]),
                                "lead_ref"].nunique(), include_groups=False),
            "dnc": g["dncTriggered"].apply(lambda x: x.astype(bool).sum()),
            "out_of_window": g.apply(
                lambda x: int(((x["hour"] < 9) | (x["hour"] >= 21)).sum()),
                include_groups=False),
            "latency_proxy_median_s": g["first_agent_sec"].median()
            if "first_agent_sec" in calls.columns else None,
        }).reset_index()
        hist["snapshot_at"] = pd.Timestamp.now(tz="UTC").isoformat()
        to_parquet_safe(hist, DATA / "metrics_history.parquet")
        print(f"  metrics history: {len(hist):,} day-era rows stored")
    except Exception as ex:
        print(f"  metrics history skipped: {type(ex).__name__}: {str(ex)[:90]}")

    return calls


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--since", default=None, help="createdAt >= YYYY-MM-DD")
    ap.add_argument("--until", default=None, help="createdAt <= YYYY-MM-DD")
    ap.add_argument("--with-pii", action="store_true",
                    help="also write data/phone_map.csv (ref -> real number)")
    ap.add_argument("--angad-only", action="store_true",
                    help="skip the other three databases")
    ap.add_argument("--max-docs", type=int, default=60000,
                    help="skip any collection larger than this")
    ap.add_argument("--reenrich", action="store_true",
                    help="recompute the derived columns from the existing raw "
                         "cache without re-downloading anything")
    ap.add_argument("--only", default=None,
                    help="comma-separated collection names to re-pull; skips "
                         "angad.calls entirely (use to retry failures)")
    args = ap.parse_args()
    only = {x.strip() for x in args.only.split(",")} if args.only else None

    if args.reenrich:
        raw = DATA / "calls_raw.parquet"
        if not raw.exists():
            raise SystemExit("no calls_raw.parquet - run: python sync.py --full")
        df = pd.read_parquet(raw)
        print(f"re-enriching {len(df):,} cached calls (no download)...")
        to_parquet_safe(enrich(df), DATA / "calls.parquet")
        print("done. now run:  python dashboard.py")
        return

    state = {} if args.full else load_state()

    match = {}
    rng = {}
    if args.since:
        rng["$gte"] = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    if args.until:
        rng["$lte"] = datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc)
    if args.days and not rng:
        rng["$gte"] = datetime.now(timezone.utc) - timedelta(days=args.days)
    # ---- INCREMENTAL LOAD (watermark / high-water-mark pattern) -----------
    # The classic mistake — and the bug this codebase shipped with — is to
    # watermark on CREATION time. A call is created at dial and MUTATED at
    # hangup (outcome, duration, transcript all land later), so a createdAt
    # watermark permanently freezes every row that changed after it was first
    # fetched. Correct: watermark on the MODIFICATION time, and re-fetch a
    # small overlap window so in-flight writes and clock skew cannot slip
    # through the gap.
    #   OVERLAP_MIN — how far back before the watermark we re-read.
    # Cost is one small window per run; the payoff is a sync that stays cheap
    # AND correct, so --full stops being mandatory.
    OVERLAP_MIN = 90
    wm = state.get("calls_watermark")
    wm_field = state.get("calls_watermark_field")
    if wm and not rng:
        cut = pd.to_datetime(wm).to_pydatetime() - timedelta(minutes=OVERLAP_MIN)
        # updatedAt exists on mutated docs; fall back to createdAt for any doc
        # that has never been updated, so nothing is missed either way.
        match["$or"] = [{"updatedAt": {"$gte": cut}}, {"createdAt": {"$gte": cut}}]
        print(f"  incremental since {cut.isoformat()} "
              f"(updatedAt OR createdAt, {OVERLAP_MIN}-min overlap; "
              f"previous watermark {wm} on {wm_field or 'createdAt'})")
    elif rng:
        match["createdAt"] = rng

    client = MongoClient(URI, serverSelectionTimeoutMS=30000, socketTimeoutMS=900000)
    db = client[DB]
    print(f"connected to '{DB}'")
    if match:
        print(f"  date filter: {match['createdAt']}")

    calls_path, raw_path = DATA / "calls.parquet", DATA / "calls_raw.parquet"
    new_calls = (pd.DataFrame() if only
                 else fetch(db.calls, flatten_call, CALL_PROJECTION, match, "calls"))

    if not new_calls.empty:
        if raw_path.exists() and not args.full:
            merged = pd.concat([pd.read_parquet(raw_path), new_calls], ignore_index=True)
            merged = merged.drop_duplicates(subset="_id", keep="last")
        else:
            merged = new_calls
        to_parquet_safe(merged, raw_path)
        to_parquet_safe(enrich(merged), calls_path)
        # advance the watermark on the MODIFICATION clock when we have it
        wm_src = "updatedAt" if "updatedAt" in merged.columns else "createdAt"
        wm_series = pd.to_datetime(merged[wm_src], utc=True, errors="coerce")
        if wm_series.isna().all():
            wm_src = "createdAt"
            wm_series = pd.to_datetime(merged["createdAt"], utc=True, errors="coerce")
        state["calls_watermark"] = wm_series.max().isoformat()
        state["calls_watermark_field"] = wm_src
        print(f"  calls: {len(merged):,} rows cached")
    elif calls_path.exists():
        print("  calls: cache kept" + (" (--only)" if only else " (no new docs)"))
    else:
        raise SystemExit("  calls: nothing fetched - check the date filter")

    for cname, flat, out in [("leads", flatten_lead, "leads"),
                             ("scheduledcalls", flatten_sched, "scheduled")]:
        if only and cname not in only:
            continue
        d = fetch(db[cname], flat, None, None, cname)
        if not d.empty:
            to_parquet_safe(d, DATA / f"{out}.parquet")
            print(f"  {cname}: {len(d):,} rows")

    # ---- the other three databases (see ASBL_MONGO_DATA_DICTIONARY.md).
    # angad is only the dialer runtime; the conversation intelligence, the CRM
    # funnel and the WhatsApp history all live elsewhere on the same cluster.
    if not args.angad_only:
        for dbname, colls in OTHER_DBS.items():
            print(f"\n  --- database '{dbname}' ---")
            odb = client[dbname]
            try:
                present = set(odb.list_collection_names())
            except Exception as e:
                print(f"  cannot read '{dbname}': {str(e)[:120]}")
                continue
            for c in colls:
                if only and c not in only:
                    continue
                if c not in present:
                    print(f"  {c}: not present, skipped")
                    continue
                try:
                    n = odb[c].estimated_document_count()
                    if n > args.max_docs:
                        print(f"  {c}: {n:,} docs > --max-docs {args.max_docs:,}, skipped")
                        continue
                    d = fetch(odb[c], flatten_generic, None, None, c)
                    if not d.empty:
                        to_parquet_safe(d, DATA / f"{dbname}__{c}.parquet")
                        print(f"  {c}: {len(d):,} rows -> {dbname}__{c}.parquet")
                except Exception as e:
                    print(f"  {c}: skipped - {type(e).__name__}: {str(e)[:110]}")

    # everything else inside angad
    others = [c for c in db.list_collection_names()
              if c not in SKIP | {"calls", "leads", "scheduledcalls"}
              and (not only or c in only)]
    for c in others:
        try:
            if db[c].estimated_document_count() > args.max_docs:
                continue
            d = fetch(db[c], flatten_generic, None, None, c)
            if not d.empty:
                to_parquet_safe(d, DATA / f"angad__{c}.parquet")
                print(f"  {c}: {len(d):,} rows")
        except Exception as e:
            print(f"  {c}: skipped - {type(e).__name__}: {str(e)[:100]}")

    if args.with_pii:
        print("\n  --with-pii is DISABLED (PII policy 2026-08-06): the phone map "
              "and its builder were removed at the user's instruction. "
              "Pseudonyms are permanently one-way.")

    # pick up the newest Zoho CSV export automatically, best-effort
    try:
        import import_zoho
        import_zoho.run()
    except Exception as e:
        print(f"  zoho import skipped: {type(e).__name__}: {str(e)[:80]}")
    try:
        import import_cohorts
        import_cohorts.run()
    except Exception as e:
        print(f"  cohort import skipped: {type(e).__name__}: {str(e)[:80]}")
    # clean re-transcriptions — the CIF corpus. This MUST run inside sync so a
    # new batch reaches the dashboard without any code change: sync imports it,
    # then cif.py (run by the refresh chain) rescores every indicator.
    try:
        import import_retranscripts
        import_retranscripts.run()
    except Exception as e:
        print(f"  retranscript import skipped: {type(e).__name__}: {str(e)[:80]}")

    if not only:
        state["last_sync"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    client.close()
    naming_gate_report()          # P2 — a naming failure must not scroll past
    print(f"\ndone. cache in {DATA}\nnow run:  python dashboard.py")


if __name__ == "__main__":
    main()
