#!/usr/bin/env python3
"""
compute_lead_profiles.py

Computes and upserts lead profile documents into analytics_db.lead_profiles.
"""
from __future__ import annotations

import os
import re
import traceback
from datetime import datetime, timezone
from pymongo import MongoClient, UpdateOne

# ── Connection ────────────────────────────────────────────────────────────────
# Credentials are read ONLY from the MONGO_URI env var (provided by the GitHub
# Actions workflow via secrets.MONGO_URI). Never hard-code the connection string
# in source — this repo was public and a committed credential is treated as leaked.
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError(
        "MONGO_URI environment variable is not set. It must be provided via the "
        "GitHub Actions secret (or your shell) — the connection string is no "
        "longer hard-coded in source for security."
    )

client = MongoClient(MONGO_URI)

leads_col         = client["asbl_loft"]["leads"]
visitor_logs_col  = client["asbl_loft"]["visitorLogs"]
conversations_col = client["asbl_loft"]["conversations"]
events_col        = client["asbl_loft"]["events"]
whatsapp_col      = client["Zoho_Database"]["whatsapp_messages"]
call_transcripts_col = client["ASBLVoiceBot"]["call_transcripts"]
call_recordings_col  = client["ASBLVoiceBot"]["call_recordings"]
lead_profiles_col = client["analytics_db"]["lead_profiles"]

# ── Helpers ───────────────────────────────────────────────────────────────────
HTML_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    if not text:
        return text
    return HTML_TAG_RE.sub("", text).strip()


def normalise_phone(phone) -> str | None:
    """Return 10-digit Indian mobile number or None if invalid."""
    if not phone:
        return None
    s = str(phone).strip().replace(" ", "").replace("-", "")
    # strip leading +
    if s.startswith("+"):
        s = s[1:]
    # strip country code 91 if present and length > 10
    if s.startswith("91") and len(s) == 12:
        s = s[2:]
    if len(s) == 10 and s.isdigit():
        return s
    return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── WEBSITE ───────────────────────────────────────────────────────────────────

def compute_website(lead_id_str: str, lead_phone_norm: str | None) -> dict:
    """Return the `website` sub-document for a lead."""

    # 1. Find visitorId via visitorLogs
    visitor_log = visitor_logs_col.find_one({"leadId": lead_id_str})
    if not visitor_log:
        # Try ObjectId variant just in case the field is stored differently
        from bson import ObjectId
        try:
            visitor_log = visitor_logs_col.find_one({"leadId": ObjectId(lead_id_str)})
        except Exception:
            pass

    visitor_id = visitor_log.get("visitorId") if visitor_log else None

    # 2. Fetch conversations — two schemas
    conv_query = []
    if visitor_id:
        conv_query.append({"visitorId": visitor_id})
    # Always also try old schema with leadId
    from bson import ObjectId
    try:
        conv_query.append({"leadId": ObjectId(lead_id_str)})
    except Exception:
        pass
    conv_query.append({"leadId": lead_id_str})

    seen_conv_ids = set()
    all_convs = []
    for q in conv_query:
        for doc in conversations_col.find(q):
            doc_id = str(doc["_id"])
            if doc_id not in seen_conv_ids:
                seen_conv_ids.add(doc_id)
                all_convs.append(doc)

    cta_triggered = 0
    cta_details_set = set()
    message_log = []

    for conv in all_convs:
        turns = conv.get("conversationDepth") or []
        for turn in turns:
            # CTA
            if turn.get("isCtaTrigger") is True:
                cta_triggered += 1
                artifact = turn.get("artifact")
                if artifact:
                    cta_details_set.add(str(artifact))

            # Message log
            user_text = turn.get("userText") or turn.get("user") or ""
            bot_text  = strip_html(turn.get("botText") or turn.get("bot") or "")
            user_at   = turn.get("userAt") or turn.get("createdAt")
            bot_at    = turn.get("botAt")  or turn.get("updatedAt")

            message_log.append({
                "user_text": user_text,
                "bot_reply": bot_text,
                "user_at":   user_at,
                "bot_at":    bot_at,
            })

    # Sort message_log by user_at ascending (None last)
    def sort_key_ua(entry):
        v = entry.get("user_at")
        if isinstance(v, datetime):
            return v
        return datetime.min.replace(tzinfo=timezone.utc)

    message_log.sort(key=sort_key_ua)

    # 3. Time spent via events
    website_time_secs = 0.0
    if visitor_id:
        for evt in events_col.find({"visitorId": visitor_id}):
            started = evt.get("startedAt")
            ended   = evt.get("endedAt")
            if isinstance(started, datetime) and isinstance(ended, datetime):
                diff = (ended - started).total_seconds()
                if diff > 0:
                    website_time_secs += diff

    return {
        "cta_triggered":           cta_triggered,
        "cta_details":             list(cta_details_set),
        "messages_typed":          len(message_log),
        "message_log":             message_log,
        "website_time_spent_secs": round(website_time_secs, 2),
    }


# ── WHATSAPP ──────────────────────────────────────────────────────────────────

def compute_whatsapp(phone_norm: str | None) -> dict:
    empty = {"user_conversation_depth": 0, "user_conversation_history": []}
    if not phone_norm:
        return empty

    # _id is the 12-digit number with country code
    wa_id = "91" + phone_norm
    doc = whatsapp_col.find_one({"_id": wa_id})
    if not doc:
        # Also try with just the 10-digit
        doc = whatsapp_col.find_one({"_id": phone_norm})
    if not doc:
        return empty

    by_date = doc.get("by_date") or {}
    all_messages = []

    for date_key, day_data in by_date.items():
        if not isinstance(day_data, dict):
            continue
        for direction in ("inbound", "outbound"):
            for msg in day_data.get(direction, []):
                text = msg.get("msg") or msg.get("message") or msg.get("text") or ""
                ts   = msg.get("time") or msg.get("date")
                all_messages.append({
                    "message": text,
                    "type":    direction,
                    "date":    ts,
                })

    # Sort by date ascending — handle both datetime objects and ISO strings
    def parse_ts(v):
        if isinstance(v, datetime):
            return v
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except Exception:
                pass
        return datetime.min.replace(tzinfo=timezone.utc)

    all_messages.sort(key=lambda m: parse_ts(m.get("date")))

    depth = sum(1 for m in all_messages if m.get("type") == "inbound")

    return {
        "user_conversation_depth":   depth,
        "user_conversation_history": all_messages,
    }


# ── AI CALLS ──────────────────────────────────────────────────────────────────

ANSWERED_OUTCOMES    = {"CONNECTED", "NOT_INTERESTED", "AUTO_CALLBACK", "PRE_SITE"}
NOT_ANSWERED_OUTCOMES = {"NO_ANSWER", "NOT_CONNECTED"}


def compute_ai_calls(phone_norm: str | None) -> dict:
    empty = {
        "outbound_calls_triggered": 0,
        "calls_answered":           0,
        "calls_not_answered":       0,
        "pickup_ratio":             0.0,
        "average_call_duration_secs": 0.0,
        "call_conversations":       [],
    }
    if not phone_norm:
        return empty

    # phone_number stored as 12-digit with country code or with +
    phone_variants = [
        phone_norm,
        "91" + phone_norm,
        "+91" + phone_norm,
    ]

    # ── Step 1: gather call_transcripts ───────────────────────────────────────
    transcript_docs = []
    seen_transcript_ids: set[str] = set()
    seen_call_sids: set[str] = set()
    for variant in phone_variants:
        for doc in call_transcripts_col.find({"phone_number": variant}):
            doc_id = str(doc["_id"])
            if doc_id not in seen_transcript_ids:
                seen_transcript_ids.add(doc_id)
                transcript_docs.append(doc)
                if doc.get("call_sid"):
                    seen_call_sids.add(str(doc["call_sid"]))

    # ── Step 0: build call_sid → recording map ────────────────────────────────
    # Strategy A: look up by call_sid directly for transcripts we already found.
    #   This handles INBOUND calls where call_recordings.phone_number = ASBL's
    #   line (not the customer's number), so phone-based lookup misses them.
    # Strategy B: also search by customer phone_number to catch OUTBOUND calls
    #   whose recording exists but have no transcript (orphan recordings).
    recording_by_sid: dict[str, dict] = {}
    seen_rec_ids: set[str] = set()

    # A — fetch by call_sid (covers both inbound and outbound with transcripts)
    if seen_call_sids:
        for rec in call_recordings_col.find({"call_sid": {"$in": list(seen_call_sids)}}):
            rec_id = str(rec["_id"])
            if rec_id not in seen_rec_ids:
                seen_rec_ids.add(rec_id)
                sid = str(rec.get("call_sid") or "")
                if sid:
                    recording_by_sid[sid] = rec

    # B — fetch by customer phone (catches orphan outbound recordings)
    for variant in phone_variants:
        for rec in call_recordings_col.find({"phone_number": variant}):
            rec_id = str(rec["_id"])
            if rec_id not in seen_rec_ids:
                seen_rec_ids.add(rec_id)
                sid = str(rec.get("call_sid") or "")
                if sid:
                    recording_by_sid[sid] = rec

    # ── Step 2: gather call_recordings that have NO matching transcript ────────
    # These are outbound calls too short to produce a transcript (< 30s).
    orphan_recording_docs = []
    for sid, rec in recording_by_sid.items():
        if sid not in seen_call_sids:
            orphan_recording_docs.append(rec)
            seen_call_sids.add(sid)

    if not transcript_docs and not orphan_recording_docs:
        return empty

    outbound_count     = 0
    answered_count     = 0
    not_answered_count = 0
    call_convs         = []
    durations          = []

    # ── Process transcript docs (full conversation data) ──────────────────────
    for doc in transcript_docs:
        direction = doc.get("call_direction", "")
        outcome   = doc.get("call_outcome", "UNKNOWN") or "UNKNOWN"
        call_sid  = str(doc.get("call_sid") or doc.get("_id") or "")

        if direction == "outbound":
            outbound_count += 1
        if outcome in ANSWERED_OUTCOMES:
            answered_count += 1
        elif outcome in NOT_ANSWERED_OUTCOMES:
            not_answered_count += 1

        raw_transcript = doc.get("transcript") or []
        turns = []

        for turn in raw_transcript:
            ts_raw = turn.get("ts")
            if isinstance(ts_raw, datetime):
                ts = ts_raw
            elif isinstance(ts_raw, str):
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except Exception:
                    ts = None
            else:
                ts = None
            text = turn.get("text") or ""
            speaker = turn.get("speaker") or ""
            t_type = "outbound" if speaker == "Anandita" else "inbound"
            turns.append({"message": text, "type": t_type, "date": ts})

        # Sort turns chronologically
        turns.sort(key=lambda t: t["date"] if isinstance(t.get("date"), datetime)
                                 else datetime.min.replace(tzinfo=timezone.utc))
        ts_list_sorted = [t["date"] for t in turns if t.get("date")]
        started_at = ts_list_sorted[0]  if ts_list_sorted else None
        ended_at   = ts_list_sorted[-1] if ts_list_sorted else None
        duration   = 0.0
        if started_at and ended_at and len(ts_list_sorted) >= 2:
            duration = (ended_at - started_at).total_seconds()
            durations.append(duration)

        # attach recording if available
        rec = recording_by_sid.get(call_sid, {})
        call_convs.append({
            "call_sid":               call_sid,
            "call_direction":         direction,
            "call_outcome":           outcome,
            "started_at":             started_at,
            "ended_at":               ended_at,
            "duration_secs":          round(duration, 2),
            "transcript":             turns,
            "recording_url":          rec.get("recording_url") or None,
            "recording_duration_secs": rec.get("recording_duration_secs") or None,
        })

    # ── Process orphan recordings (no transcript = not answered / too short) ──
    for doc in orphan_recording_docs:
        direction  = doc.get("call_direction", "outbound")
        call_sid   = str(doc.get("call_sid") or doc.get("_id") or "")
        outcome    = "NO_ANSWER"   # no transcript → call not answered
        rec_dur    = float(doc.get("recording_duration_secs") or 0)

        # Parse created_at for timestamps
        created_raw = doc.get("created_at")
        if isinstance(created_raw, datetime):
            started_at = created_raw
        elif isinstance(created_raw, str):
            try:
                started_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except Exception:
                started_at = None
        else:
            started_at = None

        if direction == "outbound":
            outbound_count += 1
        not_answered_count += 1

        call_convs.append({
            "call_sid":               call_sid,
            "call_direction":         direction,
            "call_outcome":           outcome,
            "started_at":             started_at,
            "ended_at":               started_at,
            "duration_secs":          round(rec_dur, 2),
            "transcript":             [],
            "recording_url":          doc.get("recording_url") or None,
            "recording_duration_secs": round(rec_dur, 2) if rec_dur else None,
        })

    denom = answered_count + not_answered_count
    pickup_ratio = round(answered_count / denom, 2) if denom > 0 else 0.0
    avg_duration = round(sum(durations) / len(durations), 2) if durations else 0.0

    return {
        "outbound_calls_triggered":   outbound_count,
        "calls_answered":             answered_count,
        "calls_not_answered":         not_answered_count,
        "pickup_ratio":               pickup_ratio,
        "average_call_duration_secs": avg_duration,
        "call_conversations":         call_convs,
    }


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    leads = list(leads_col.find({}))
    total   = len(leads)
    upserted = 0
    errors   = 0

    print(f"Found {total} leads. Starting processing…\n")

    for idx, lead in enumerate(leads, start=1):
        lead_id_str = str(lead["_id"])
        name  = lead.get("name") or ""
        phone_raw = lead.get("phone") or ""
        phone_norm = normalise_phone(phone_raw)

        source_block  = lead.get("source") or {}
        lead_source   = source_block.get("utmSource")   if isinstance(source_block, dict) else None
        campaign_name = source_block.get("utmCampaign") if isinstance(source_block, dict) else None
        lead_created_at = lead.get("createdAt")

        # Do NOT log customer name/phone — these prints land in the GitHub Actions
        # log (PII exposure). Progress by counter only.
        print(f"Processing lead {idx}/{total}")

        try:
            website_data  = compute_website(lead_id_str, phone_norm)
            whatsapp_data = compute_whatsapp(phone_norm)
            ai_calls_data = compute_ai_calls(phone_norm)

            now = now_utc()

            profile = {
                "lead_id":           lead_id_str,
                "lead_name":         name,
                "lead_phone_number": phone_raw,
                "lead_source":       lead_source,
                "campaign_name":     campaign_name,
                "lead_created_at":   lead_created_at,
                "website":           website_data,
                "whatsapp":          whatsapp_data,
                "ai_calls":          ai_calls_data,
                "updated_at":        now,
            }

            result = lead_profiles_col.update_one(
                {"lead_id": lead_id_str},
                {
                    "$set": profile,
                    "$setOnInsert": {"computed_at": now},
                },
                upsert=True,
            )

            upserted += 1

        except Exception as exc:
            errors += 1
            print(f"  ERROR for lead {lead_id_str}: {exc}")
            traceback.print_exc()

    print("\n── Summary ──────────────────────────────")
    print(f"  Total leads processed : {total}")
    print(f"  Total upserted        : {upserted}")
    print(f"  Total errors          : {errors}")
    print("─────────────────────────────────────────")


if __name__ == "__main__":
    main()
