"""
crm_v2.py — ASBL CRM dashboard backend (V1 display + manual control)
===================================================================
Flask Blueprint providing the dashboard's lead-display and manual-control
endpoints. The relay (asbl-crm-api) is the orchestration BRAIN + the sole sender
(calls/WhatsApp); the dashboard reads lead state and lets a human act.

> NOTE: the V2 orchestration engine (event intake, dual-status state machine,
> workflow queue + cron, posthook, ingest, T=0 fanout, and the dashboard's own
> senders) was REMOVED on 2026-06-26 to run pure V1 from the dashboard. It will
> be re-introduced when V2 is implemented. Lead state is owned by the relay and
> written via the ``/api/crm`` REST API (crm_api.py); this module is display +
> manual control only.

Registration (in dashboard.py):
    from crm_v2 import crm_v2_bp, init_app as crm_v2_init
    crm_v2_init(app, client)      # client = existing MongoClient

What this module now does
-------------------------
    • Display:  /api/v2/stages-map (homepage Stage column),
                /api/v2/crm-by-phone + /api/v2/lead/<plid>/crm (CRM tab)
    • Manual:   /api/v2/lead/<plid>/stage, /notes, /reminder (store-only),
                /bot-toggle, /trigger-call + /leads/bulk-trigger (route to relay),
                /api/v2/not-interested-reasons
    • Flags:    mark_not_interested_by_phone / reopen_lead (called by dashboard.py)

Display shows the relay's V1 fields (prd_stage / prd_status); set_stage keeps the
legacy `stage` mirror in sync. Manual call triggers route through the relay
(RELAY_CALL_URL + RELAY_API_TOKEN) — dormant until set; the dashboard never dials
the voicebot directly. All datetime reads go through _aware().
"""

from __future__ import annotations

import hmac
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from bson import ObjectId
from flask import Blueprint, jsonify, request, session

import lead_identity

# ── Logger ────────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)

# ── Blueprint ─────────────────────────────────────────────────────────────────
crm_v2_bp = Blueprint("crm_v2", __name__)

# ── Module-level MongoDB client (injected by init_app) ───────────────────────
_mongo_client = None


def init_app(app, mongo_client) -> None:
    """Register crm_v2_bp with *app* and store the shared Mongo client.

    Call this once from dashboard.py after creating the Flask app and the
    MongoClient::

        from crm_v2 import crm_v2_bp, init_app as crm_v2_init
        crm_v2_init(app, client)
    """
    global _mongo_client
    _mongo_client = mongo_client
    app.register_blueprint(crm_v2_bp)
    log.info("crm_v2: blueprint registered")


# ══════════════════════════════════════════════════════════════════════════════
#  Constants  (DUAL-STATUS MODEL — source of truth, do not regress to single status)
# ══════════════════════════════════════════════════════════════════════════════
#  Three INDEPENDENT fields on the lead (PRD §3, §6):
#    • stage          — funnel source of truth
#    • chatbot_status — WhatsApp workflow control  (NEVER set by a call event)
#    • call_status    — AI-calling workflow control (NEVER set by a chatbot event)

STAGES = ("New Lead", "Lead Initiated", "Pre Site Visit", "Post Site Visit", "Not Interested")
CHATBOT_STATUSES = ("NA", "CF", "SF")          # NA=trigger, CF=replied, SF=no-reply→loop
CALL_STATUSES = ("SS", "CS")                   # SS=system scheduled, CS=customer scheduled

# Reasons selectable when a lead is marked Not Interested. The reason is a
# SUB-PART of the Not Interested STAGE (not a flag). Only "Spam" still derives a
# review flag ("spam"); every other reason derives no flag (None) — Not Interested
# is a STAGE, shown in the Stage column, not a top-right review flag.
NOT_INTERESTED_REASONS = (
    "Not Responding",   # passive: lead never engaged (call + chatbot exhausted)
    "Declined",         # active: customer engaged and explicitly said "not interested"
    "Budget",           # customer budget does not match
    "Inventory",        # inventory mismatch
    "Spam",             # spam → flag shows Spam
    # Additional reasons (plain Not-Interested sub-reasons; no review flag).
    "Rural",
    "Not Looking for Property",
    "100% / Pre Launch",
    "Already Purchased",
    "Location",
    "Availability",
    "Handover Timeline",
    "Vastu",
    "Looking for Rent",
    "Looking for Open Plots",
    "Irrelevant Lead",
    "Others",
    "Looking for Job",
    "Exploring",
)


def flag_for_reason(reason: str | None) -> str | None:
    """Derive the review flag from a Not Interested reason.

    Spam → "spam"; anything else → None (Not Interested is a STAGE, not a flag).
    """
    return "spam" if (reason or "").strip().lower() == "spam" else None

# Allowed STAGE transitions (PRD §10). "Spam" is NOT a stage — it is a separate
# Lead_Status / mark-spam action handled outside this matrix.
STAGE_TRANSITIONS: dict[str | None, set[str]] = {
    None:              {"New Lead"},
    "New Lead":        {"Lead Initiated", "Pre Site Visit", "Not Interested"},
    "Lead Initiated":  {"Pre Site Visit", "Not Interested"},
    "Pre Site Visit":  {"Post Site Visit", "Not Interested"},   # visit completed
    "Post Site Visit": {"Not Interested"},                       # post-visit drop-off
    "Not Interested":  {"Lead Initiated"},   # manual reactivation only (PRD open item)
}

# Global overrides — highest priority, win from ANY state (PRD §6.4, flowchart 6)
GLOBAL_OVERRIDE_SITE_VISIT = "site_visit"
GLOBAL_OVERRIDE_NOT_INTERESTED = "not_interested"

# Call outcome → (stage_change_or_None, call_status_or_None). Per PRD §8.4/§8.5.
# A call NEVER touches chatbot_status (PRD guardrail §17).
CALL_OUTCOME_RULES: dict[str, tuple[str | None, str | None]] = {
    "CONNECTED":      ("Lead Initiated", None),   # call_status set later from LLM (CS if time, else SS)
    "PRE_SITE":       ("Pre Site Visit", None),
    "VIRTUAL_TOUR":   ("Pre Site Visit", None),
    "NOT_CONNECTED":  (None, "SS"),               # stay SS → SS Tree continues
    "BUSY":           (None, "SS"),
    "NO_ANSWER":      (None, "SS"),
    "NOT_INTERESTED": ("Not Interested", None),
}

# NEVER REMOVE — blast radius protection (see 2026-05-28 incident, 141 phones spammed)
CRON_HARD_CAP = 200

# Cadence defaults (PRD §8.2.1, §15) — overridable via Zoho_Database.bot_settings
DEFAULT_CONFIG = {
    "chatbot_no_response_minutes": 60,
    "chatbot_followup_intervals_min": [60, 120, 1440],   # +1h, +2h, +24h
    "chatbot_followup_count": 3,
    "ss_tree_max_attempts": 3,                            # PRD open item → default 3
    "ss_tree_intervals_min": [240],                       # PRD open item → default 4h
    "ai_call_working_hours": None,                        # PRD open item → no gate by default
    "llm_confidence_threshold": 0.6,                      # PRD open item → default 0.6
}
LEAD_SILENCE_DAYS = 7

PROJECT_KEYWORDS: dict[str, list[str]] = {
    "BROADWAY": ["broadway"],
    "SPECTRA":  ["spectra"],
    "LANDMARK": ["landmark"],
    "LEGACY":   ["legacy"],
    "LOFT":     ["loft"],   # default
}
_KNOWN_PROJECTS = set(PROJECT_KEYWORDS)

# Resubmission cooldown: if last action was within this window, skip T=0 fanout
RESUBMISSION_COOLDOWN_MINUTES = 30

# Bulk-trigger hard limit per request (separate from cron cap)
BULK_TRIGGER_MAX = 50

# Terminal stages — leads here are never auto-actioned by the queue worker.
TERMINAL_STAGES = ("Not Interested", "Pre Site Visit")

# Human-readable label per call outcome (for leads.call_status_label).
CALL_OUTCOME_TO_STATUS_LABEL: dict[str, str] = {
    "CONNECTED":      "Connected",
    "NOT_CONNECTED":  "Not Connected",
    "BUSY":           "Busy",
    "NO_ANSWER":      "No Answer",
    "PRE_SITE":       "Pre Site",
    "VIRTUAL_TOUR":   "Virtual Tour",
    "NOT_INTERESTED": "Not Interested",
}


# ══════════════════════════════════════════════════════════════════════════════
#  Generic helpers
# ══════════════════════════════════════════════════════════════════════════════

def normalise_phone(phone: Any) -> str | None:
    """Return canonical phone format ``91XXXXXXXXXX`` (12 digits, no + prefix).

    Strips spaces, dashes, brackets, dots.  Removes a leading ``+``.  If the
    result is 10 digits it prepends ``91``.  Returns ``None`` for anything that
    cannot be coerced into a valid 10- or 12-digit Indian mobile number.
    """
    if not phone:
        return None
    raw = str(phone).strip()
    raw = re.sub(r"[\s\-\(\)\.]", "", raw)
    raw = raw.lstrip("+")
    if raw.startswith("0"):
        raw = raw[1:]
    if raw.startswith("91") and len(raw) == 12:
        pass  # already canonical
    elif len(raw) == 10:
        raw = "91" + raw
    elif raw.startswith("91") and len(raw) != 12:
        return None
    else:
        return None
    if len(raw) != 12 or not raw.isdigit():
        return None
    return raw


def phone_variants(phone_norm: str) -> list[str]:
    """Return ``[canonical, 10-digit, +91version]`` for backward-compat lookups.

    Example: ``"919876543210"`` → ``["919876543210", "9876543210", "+919876543210"]``
    """
    if not phone_norm or len(phone_norm) != 12:
        return [phone_norm] if phone_norm else []
    ten_digit = phone_norm[2:]
    plus_version = "+" + phone_norm
    return [phone_norm, ten_digit, plus_version]


def _aware(dt: Any) -> datetime | None:
    """Coerce *dt* into a timezone-aware UTC datetime, or ``None``.

    Zoho wrote ALL datetime fields in ``Zoho_Database.leads`` as ISO-format
    STRINGS with MIXED timezones — some ``+00:00`` (UTC), some ``+05:30`` (IST).
    Our own writes use BSON ``datetime`` objects.  So this helper must accept:

      * ``datetime``  — naive → attach UTC; aware → convert to UTC
      * ``str``       — parse ISO (handles trailing ``Z`` and ``+05:30`` offset),
                        then normalise to UTC
      * anything else / unparseable / ``None`` → ``None``
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            return None
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _dt_iso(val: Any) -> Any:
    """Serialise a datetime to an ISO string for JSON; pass through otherwise."""
    aware = _aware(val)
    return aware.isoformat() if aware is not None else val


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def log_event(
    zoho_db,
    lead_id: str | None,
    event_name: str,
    source: str,
    payload: dict | None = None,
    actor: str = "system",
) -> None:
    """Append an immutable event to ``Zoho_Database.audit_logs``.

    Events are append-only.  LLM classification lives inside ``payload``.
    """
    try:
        zoho_db["audit_logs"].insert_one({
            "lead_id": lead_id,
            "event_name": event_name,
            "source": source,
            "payload": payload or {},
            "actor": actor,
            "created_at": _utcnow(),
        })
    except Exception:
        log.exception("log_event failed: lead_id=%s event=%s", lead_id, event_name)


# ══════════════════════════════════════════════════════════════════════════════
#  Module B — State engine (the heart)
# ══════════════════════════════════════════════════════════════════════════════
#
#  Three INDEPENDENT setters.  chatbot_status and call_status NEVER overwrite
#  each other (separate writers, never cross-write).  set_stage validates against
#  STAGE_TRANSITIONS.


def _stamp_last_action(update: dict, actor: str, desc: str) -> dict:
    now = _utcnow()
    update["last_action"] = f"{desc} by {actor}"
    update["last_action_time"] = now
    update["updated_at"] = now
    return update


def _display_stage(lead: dict):
    """The stage to SHOW for a lead — delegates to lead_identity.resolve_display_stage,
    the one canonical implementation shared with pipeline.py.

    (Trade-off, chosen deliberately: the relay/Zoho-sync owns `prd_stage` and
    rewrites it on every sync, so a manually-locked lead stops reflecting further
    relay progress — see lead_identity for the exact precedence rule.)
    """
    return lead_identity.resolve_display_stage(
        lead.get("stage"), lead.get("prd_stage"), lead.get("stage_locked"))


def _resolve_lead(zoho_db, plid, projection=None):
    """Find a lead doc by any of its identifiers, returning the doc (or None).

    Strategies, in order — handles every way a lead may have been inserted:
      1. ``_id == plid``           (relay-created leads: _id IS the PLID string)
      2. ``plid`` field == plid    (Zoho-mirror leads: numeric/ObjectId _id)
      3. parse ``MLID-PROJECT``    (e.g. "1753-BROADWAY" → mlid+project match)
      4. ``_id == ObjectId(plid)`` (legacy ObjectId-keyed leads passed as 24-hex)

    The returned doc's ``_id`` is the authoritative BSON key for any update.
    """
    leads = zoho_db["leads"]
    doc = leads.find_one({"_id": plid}, projection)
    if doc is None:
        doc = leads.find_one({"plid": plid}, projection)
    if doc is None and isinstance(plid, str) and "-" in plid:
        # MLID is the leading numeric token; PARTITION on the FIRST dash so a
        # project name that itself contains a dash (e.g. "NORTH-TOWER") stays whole.
        mlid_part, _, proj_part = plid.partition("-")
        if mlid_part and proj_part:
            doc = leads.find_one(
                {"mlid": mlid_part,
                 "project": {"$regex": f"^{re.escape(proj_part)}$", "$options": "i"}},
                projection,
            )
    if doc is None and isinstance(plid, str) and len(plid) == 24:
        try:
            doc = leads.find_one({"_id": ObjectId(plid)}, projection)
        except Exception:
            pass  # not a valid ObjectId hex — fall through to None
    return doc


def set_stage(
    zoho_db,
    plid: str,
    new_stage: str,
    actor: str = "system",
    reason: str | None = None,
    override: bool = False,
) -> tuple[bool, str | None]:
    """Validate against :data:`STAGE_TRANSITIONS` and write ``stage``.

    Independent of chatbot_status/call_status.  ``override=True`` skips the
    transition matrix — used for MANUAL admin/sales moves (PRD §19: a sales
    manager may move leads / override statuses).  Automated events keep
    ``override=False`` so the matrix is enforced.  Returns ``(ok, err)``.

    The dashboard mirrors Zoho: it just stores the stage.  The relay runs
    autonomously and stops on its own when it reads a lead whose stage is
    Not Interested / Spam (the dashboard's ``?active=1`` list excludes those).
    """
    if _mongo_client is None:
        return False, "MongoDB client not initialised"
    if new_stage not in STAGES:
        return False, f"Unknown stage: {new_stage!r}"
    try:
        leads = zoho_db["leads"]
        lead = _resolve_lead(zoho_db, plid,
                             {"stage": 1, "prd_stage": 1, "stage_locked": 1, "_id": 1})
        if lead is None:
            return False, f"Lead {plid!r} not found"
        actual_id = lead["_id"]  # preserve BSON type for the update_one filter
        current = lead.get("stage")
        allowed = STAGE_TRANSITIONS.get(current)
        if allowed is None and not override:
            return False, f"Unknown current stage: {current!r}"
        # Idempotent no-op only if the manual stage is already this value AND
        # already locked; otherwise fall through to (re)write `stage` + lock it.
        # prd_stage is the RELAY's field and is intentionally NOT considered here.
        if new_stage == current and lead.get("stage_locked"):
            return True, None  # idempotent no-op
        if not override and new_stage not in allowed:
            return (
                False,
                f"Transition {current!r} → {new_stage!r} not allowed. Permitted: {allowed}",
            )
        # The dashboard OWNS `stage` (pipeline stage); the relay OWNS `prd_stage`
        # (relay stage) — they are INDEPENDENT. stage_locked pins the display to the
        # manual `stage` via _display_stage, so a manual change shows through even as
        # the relay keeps rewriting prd_stage. We deliberately do NOT touch prd_stage:
        #   • never manually set  → display falls back to prd_stage (relay stage)
        #   • manually set (lock) → display shows `stage`; relay stage stays its own
        update: dict[str, Any] = {"stage": new_stage, "stage_locked": True}
        if reason is not None:
            update["not_interested_reason"] = reason
        _stamp_last_action(update, actor, f"Stage → {new_stage}")

        # TERMINAL stage (Not Interested): statuses are WORKFLOW CONTROLS — a
        # terminal lead must have NO status (nothing that could fire a trigger).
        # Clear both chatbot_status and call_status; only the reason remains.
        # (PRD §10/§8.8: "No further action" for both statuses on Not Interested.)
        if new_stage == "Not Interested":
            update["chatbot_status"] = None
            update["call_status"] = None
        else:
            # FIX A: moving OUT of Not Interested → clear the stale reason so a
            # reopened lead never keeps a leftover "Spam"/"Budget" reason.
            update["not_interested_reason"] = None

        result = leads.update_one({"_id": actual_id}, {"$set": update})
        if result.modified_count == 0 and result.matched_count == 0:
            log.error("set_stage: update_one matched 0 docs — actual_id=%r plid=%r", actual_id, plid)
            return False, f"Write matched 0 documents (id={actual_id!r})"
        # A stage change is "new activity" → bump the profile's last_event_at so the
        # lead re-highlights on the homepage (monotonic $max; best-effort).
        try:
            _mongo_client["analytics_db"]["lead_profiles"].update_one(
                {"lead_id": "ZOHO-" + str(actual_id)},
                {"$max": {"last_event_at": _utcnow()}})
        except Exception:
            pass
        return True, None
    except Exception as exc:
        log.exception("set_stage failed for plid=%s", plid)
        return False, str(exc)


def disable_bot_for_lead(zoho_db, plid: str, phone_norm: str | None = None) -> None:
    """Flip the WhatsApp bot kill-switch OFF for this lead.

    Sets ``bot_enabled=False`` on the lead and on the per-phone
    ``user_profiles`` doc (so inbound WhatsApp replies are silenced too).
    """
    now = _utcnow()
    try:
        zoho_db["leads"].update_one(
            {"_id": plid}, {"$set": {"bot_enabled": False, "updated_at": now}})
        if phone_norm:
            zoho_db["user_profiles"].update_many(
                {"_id": {"$in": phone_variants(phone_norm)}},
                {"$set": {"bot_enabled": False, "updated_at": now}})
    except Exception:
        log.exception("disable_bot_for_lead failed for plid=%s", plid)


def mark_not_interested(
    zoho_db,
    plid: str,
    reason: str = "Not Responding",
    disable_bot: bool = True,
    actor: str = "dashboard",
    override: bool = False,
) -> tuple[bool, str | None]:
    """Single source of truth for closing a lead as Not Interested.

    Drives the FULL terminal transition so every entry point (Pipeline Stage
    dropdown, top-right flag, spam-mark) behaves identically:
      1. stage → Not Interested  (set_stage also clears both statuses + cancels
         every pending queued trigger)
      2. store the reason
      3. (optional) disable the WhatsApp bot kill-switch

    Returns ``(ok, error)``.
    """
    # Resolve to the real BSON _id (PLID string, numeric Zoho id, or via plid /
    # mlid+project) so every {"_id": ...} write below hits the right document.
    lead = _resolve_lead(zoho_db, plid, {"phone": 1, "_id": 1})
    if lead is None:
        return False, f"Lead {plid!r} not found"
    actual_id = lead["_id"]
    if reason not in NOT_INTERESTED_REASONS:
        reason = "Not Responding"
    phone_norm = normalise_phone(lead.get("phone"))
    ok, err = set_stage(zoho_db, actual_id, "Not Interested", actor=actor,
                        reason=reason, override=override)
    if not ok:
        return False, err
    # set_stage is idempotent on stage, so if the lead was ALREADY Not Interested
    # its no-op skips the status-clearing block — write reason AND null both
    # workflow statuses here so a terminal lead can never retain a stale status
    # that could fire a trigger (PRD §10 "no further action").
    zoho_db["leads"].update_one(
        {"_id": actual_id},
        {"$set": {"not_interested_reason": reason, "chatbot_status": None,
                  "call_status": None, "updated_at": _utcnow()}})
    if disable_bot:
        disable_bot_for_lead(zoho_db, actual_id, phone_norm)
    # Sync the DERIVED review flag into analytics_db.lead_profiles: Spam → "spam";
    # every other reason → None, which UNSETS any stale flag (Not Interested is a
    # STAGE, not a flag).
    _sync_review_flag(phone_norm, flag_for_reason(reason))
    log_event(zoho_db, actual_id, "MARKED_NOT_INTERESTED", actor,
              {"reason": reason, "flag": flag_for_reason(reason),
               "bot_disabled": disable_bot}, actor=actor)
    return True, None


def _sync_review_flag(phone_norm: str | None, flag: str | None) -> None:
    """Set/unset ``analytics_db.lead_profiles.flag`` for the lead matching *phone_norm*.

    lead_profiles phone values are inconsistently formatted, so we match on the
    last 10 digits via regex (best-effort — the CRM stage is the source of truth).
    A truthy *flag* (e.g. "spam") is SET; a None/empty flag UNSETS any stale flag
    (a non-spam Not Interested is a STAGE, not a flag).
    """
    if not phone_norm or _mongo_client is None:
        return
    last10 = phone_norm[-10:]
    if len(last10) < 10:
        return
    try:
        update = {"$set": {"flag": flag}} if flag else {"$unset": {"flag": ""}}
        _mongo_client["analytics_db"]["lead_profiles"].update_many(
            {"lead_phone_number": {"$regex": last10 + "$"}},
            update)
    except Exception:
        # Mask the phone in logs (last10 is the full number) — show last 4 only.
        log.exception("_sync_review_flag failed for phone ***%s",
                      (last10[-4:] if last10 else ""))


def mark_not_interested_by_phone(
    phone: str,
    reason: str = "Not Interested",
    disable_bot: bool = True,
    actor: str = "dashboard",
) -> tuple[bool, str | None]:
    """Resolve a lead by phone and run :func:`mark_not_interested`.

    Used by the top-right review flag (which is keyed by lead_profile, not PLID)
    so flagging Not Interested / Spam drives the CRM lifecycle too.
    """
    if _mongo_client is None:
        return False, "MongoDB client not initialised"
    phone_norm = normalise_phone(phone)
    if not phone_norm:
        return False, "invalid phone"
    zoho_db = _mongo_client["Zoho_Database"]
    lead = zoho_db["leads"].find_one({"phone": {"$in": phone_variants(phone_norm)}}, {"_id": 1})
    if lead is None:
        return False, "lead not in CRM"
    plid = lead["_id"]
    migrate_lead_to_dual_status(zoho_db, zoho_db["leads"].find_one({"_id": plid}))
    return mark_not_interested(zoho_db, plid, reason, disable_bot, actor)


def reopen_lead(lead_id: str, phone: str | None = None,
                actor: str = "dashboard-flag") -> tuple[bool, str | None]:
    """Reopen a closed lead — used when the Spam flag is changed back to a
    positive review tag (Genuine/Test/Unclear). NO-OP unless the lead is
    currently ``Not Interested``.

    Resolves the EXACT project-lead: a ZOHO ``lead_id`` (``ZOHO-1735-BROADWAY``)
    maps straight to its PLID; otherwise falls back to phone. On reopen:
    stage → Lead Initiated (override), bot re-enabled, statuses restored to the
    active NA/SS baseline, and the stale not_interested_reason cleared
    (set_stage Fix A handles the reason).
    """
    if _mongo_client is None:
        return False, "MongoDB client not initialised"
    zoho_db = _mongo_client["Zoho_Database"]
    lead = None
    if lead_id and lead_id.startswith("ZOHO-"):
        lead = zoho_db["leads"].find_one({"_id": lead_id[5:]})
    if lead is None and phone:
        pn = normalise_phone(phone)
        if pn:
            lead = zoho_db["leads"].find_one({"phone": {"$in": phone_variants(pn)}})
    if lead is None:
        return False, "lead not in CRM"
    if lead.get("stage") != "Not Interested":
        return True, None  # not closed → nothing to reopen
    plid = lead["_id"]
    migrate_lead_to_dual_status(zoho_db, zoho_db["leads"].find_one({"_id": plid}))
    # Reopening sets a non-terminal stage, so the relay naturally picks the lead
    # back up (it only skips Not Interested / Spam).
    ok, err = set_stage(zoho_db, plid, "Lead Initiated", actor=actor, override=True)
    if not ok:
        return False, err
    zoho_db["leads"].update_one({"_id": plid}, {"$set": {
        "bot_enabled": True, "chatbot_status": "NA", "call_status": "SS",
        "updated_at": _utcnow()}})
    log_event(zoho_db, plid, "REOPENED", actor, {"from": "Not Interested"}, actor=actor)
    return True, None


def derive_dual_status(prd_status: str | None) -> tuple[str, str]:
    """Split a legacy single ``prd_status`` into ``(chatbot_status, call_status)``.

    Mapping:
        "NA" → (NA, SS)   "CF" → (CF, SS)   "SF" → (SF, SS)
        "SS" → (NA, SS)   "CS" → (NA, CS)
        anything else / None → (NA, SS)
    """
    mapping = {
        "NA": ("NA", "SS"),
        "CF": ("CF", "SS"),
        "SF": ("SF", "SS"),
        "SS": ("NA", "SS"),
        "CS": ("NA", "CS"),
    }
    return mapping.get((prd_status or "").upper(), ("NA", "SS"))


def migrate_lead_to_dual_status(zoho_db, lead: dict) -> dict:
    """Idempotently backfill dual-status fields from legacy single-status data.

    If the lead lacks ``chatbot_status``/``call_status`` they are derived from the
    legacy ``prd_status``; ``prd_stage`` is mapped to ``stage`` if ``stage`` is
    absent.  Persisted and reflected back into the returned (in-memory) lead.
    Idempotent: a lead already carrying dual fields is left untouched.
    """
    actual_id = lead["_id"]  # preserve BSON type; never use the plid string for _id filter
    update: dict[str, Any] = {}

    if lead.get("stage") not in STAGES:
        legacy_stage = lead.get("prd_stage")
        if legacy_stage in STAGES:
            update["stage"] = legacy_stage
        else:
            update["stage"] = "New Lead"

    effective_stage = update.get("stage", lead.get("stage"))

    if effective_stage == "Not Interested":
        # TERMINAL — no workflow statuses (only the reason matters). Clear both.
        if lead.get("chatbot_status") is not None:
            update["chatbot_status"] = None
        if lead.get("call_status") is not None:
            update["call_status"] = None
    else:
        needs_chatbot = lead.get("chatbot_status") not in CHATBOT_STATUSES
        needs_call = lead.get("call_status") not in CALL_STATUSES
        if needs_chatbot or needs_call:
            cb, cs = derive_dual_status(lead.get("prd_status"))
            if needs_chatbot:
                update["chatbot_status"] = cb
            if needs_call:
                update["call_status"] = cs

    if update:
        update["updated_at"] = _utcnow()
        try:
            zoho_db["leads"].update_one({"_id": actual_id}, {"$set": update})
        except Exception:
            log.exception("migrate_lead_to_dual_status failed for _id=%s", actual_id)
        lead.update(update)
    return lead


# ── first_engagement_channel helper ───────────────────────────────────────────

def fire_call(
    zoho_db,
    plid: str,
    phone_norm: str,
    name: str,
    project: str,
    mlid: str,
    attempt: int = 1,
) -> tuple[bool, str, str | None]:
    """Request a MANUAL AI call by routing through the RELAY (the sole sender).

    V1: the relay owns all call execution. A human clicking "Trigger Call" asks
    the relay to place the call — the dashboard never dials the voicebot directly
    (keeps voicebot creds off the dashboard; no second automated send path).

    Steps:
      1. Write an ``ASBLVoiceBot.call_schedules`` audit doc.
      2. POST the relay's manual-call endpoint (``RELAY_CALL_URL``) with a Bearer
         ``RELAY_API_TOKEN``. If either is unset → dormant (no send).
      3. On response, update call_schedules status/call_id and stamp the lead.

    Returns ``(ok, call_id, err)``.  Dormant → ``(False, "", "... not configured")``.
    """
    relay_url = os.environ.get("RELAY_CALL_URL", "").strip()
    relay_token = os.environ.get("RELAY_API_TOKEN", "").strip()
    now = _utcnow()
    schedule_id = f"{plid}-call-{attempt}"

    # 1. Persist the schedule doc first (always — gives us an audit row).
    schedule_doc = {
        "external_schedule_id": schedule_id,
        "external_customer_id": str(mlid),
        "customer_name": name,
        "to": "+" + phone_norm,
        "metadata": {
            "project": project,
            "plid": plid,
            "mlid": str(mlid),
            "customer_phone": phone_norm,
            "customer_name": name,
        },
        "status": "pending",
        "created_by": "cron",
        "scheduled_at": now,
        "created_at": now,
        "updated_at": now,
    }
    try:
        zoho_db_voicebot = _mongo_client["ASBLVoiceBot"]
        zoho_db_voicebot["call_schedules"].update_one(
            {"external_schedule_id": schedule_id},
            {"$set": schedule_doc},
            upsert=True,
        )
    except Exception:
        log.exception("fire_call: call_schedules write failed for %s", schedule_id)

    if not relay_url or not relay_token:
        return False, "", "RELAY_CALL_URL/RELAY_API_TOKEN not configured"

    payload = {
        "to": "+" + phone_norm,
        "customer_name": name,
        "external_schedule_id": schedule_id,
        "external_customer_id": str(mlid),
        "source": "manual_dashboard",
        "metadata": schedule_doc["metadata"],
    }
    headers = {"Authorization": f"Bearer {relay_token}"}

    endpoints = [relay_url]
    resp = None
    last_err = None
    for endpoint in endpoints:
        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=15)
            if resp.status_code == 404:
                resp = None
                continue  # try fallback endpoint
            break
        except Exception as exc:
            last_err = str(exc)
            resp = None

    if resp is None:
        log.exception("fire_call: relay request failed for %s", schedule_id)
        return False, "", last_err or "relay request failed"

    try:
        if not resp.ok:
            return False, "", f"relay HTTP {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
        ok_flag = data.get("ok") is True or data.get("success") is True
        call_id = data.get("requestUuid") or data.get("call_id") or ""
        if not ok_flag:
            err = data.get("error") or data.get("message") or "Unknown error from relay"
            return False, "", str(err)
    except Exception as exc:
        log.exception("fire_call: bad relay response for %s", schedule_id)
        return False, "", str(exc)

    # 3. Update schedule + stamp lead.
    try:
        _mongo_client["ASBLVoiceBot"]["call_schedules"].update_one(
            {"external_schedule_id": schedule_id},
            {"$set": {"status": "called", "call_id": str(call_id),
                      "called_at": now, "updated_at": now}},
        )
    except Exception:
        log.exception("fire_call: schedule status update failed for %s", schedule_id)
    try:
        zoho_db["leads"].update_one(
            {"_id": plid},
            {"$set": {"last_inhouse_call_id": str(call_id), "last_call_at": now,
                      "updated_at": now},
             "$inc": {"ss_call_attempt_count": 1}},
        )
    except Exception:
        log.exception("fire_call: lead stamp failed for plid=%s", plid)

    return True, str(call_id), None


@crm_v2_bp.route("/api/v2/lead/<plid>/stage", methods=["POST"])
def lead_set_stage(plid: str):
    """Update the pipeline stage for a lead via the validated state engine.

    Body: ``{stage: "Pre Site Visit", reason?: "..."}``
    """
    if _mongo_client is None:
        return jsonify({"error": "MongoDB client not initialised"}), 503
    body = request.get_json(force=True, silent=True) or {}
    target_stage = (body.get("stage") or "").strip()
    if not target_stage:
        return jsonify({"error": "stage is required"}), 400

    zoho_db = _mongo_client["Zoho_Database"]
    lead = _resolve_lead(zoho_db, plid)
    if lead is None:
        return jsonify({"ok": False, "error": f"Lead {plid!r} not found"}), 404
    # Pass the PLID if present, else the NATIVE _id (never str()-coerced — an
    # ObjectId/int _id stringified can't be re-found by the downstream lookup).
    resolved_plid = lead.get("plid") or lead["_id"]
    migrate_lead_to_dual_status(zoho_db, lead)

    actor = body.get("actor", "dashboard")
    # MANUAL dashboard move by an admin → override the transition matrix (PRD §19:
    # sales manager may move leads / override statuses). Automated paths still
    # enforce the matrix (override defaults False there).
    if target_stage == "Not Interested":
        ok, err = mark_not_interested(
            zoho_db, resolved_plid, reason=body.get("reason") or "Not Responding",
            disable_bot=True, actor=actor, override=True)
    else:
        ok, err = set_stage(zoho_db, resolved_plid, target_stage, actor=actor,
                            reason=body.get("reason"), override=True)
        if ok:
            log_event(zoho_db, resolved_plid, "MANUAL_STAGE_CHANGE", "dashboard",
                      {"stage": target_stage}, actor=actor)
    if ok:
        return jsonify({"ok": True, "stage": target_stage}), 200
    return jsonify({"ok": False, "error": err}), 400


@crm_v2_bp.route("/api/v2/not-interested-reasons", methods=["GET"])
def not_interested_reasons():
    """Return the selectable Not Interested reasons for the UI dropdown."""
    return jsonify({"reasons": list(NOT_INTERESTED_REASONS)}), 200


def build_stage_map(client):
    """``{"<last10phone>|<PROJECT>": {"stage": ..., "reason": ...}}`` for every
    CRM lead — the ONE canonical source for both the Leads tab's Stage column
    and (via lead_identity, not this function directly — see pipeline.py) the
    Pipeline tab's Booked Site Visit. Read live so callers always get the
    current state; no caching here (callers may cache if they need to).

    Reusable from anywhere that already holds a Mongo client (e.g. dashboard.py's
    /api/leads, to attach per-row resolved_stage/resolved_project server-side)
    without a second HTTP round-trip to /api/v2/stages-map.
    """
    out: dict[str, dict] = {}
    for lead in client["Zoho_Database"]["leads"].find(
        {}, {"phone": 1, "project": 1, "stage": 1, "prd_stage": 1,
             "stage_locked": 1, "not_interested_reason": 1}
    ):
        # LENIENT last-10-digit keying (matches the homepage exactly). A strict
        # normaliser that dropped malformed numbers like "17206973605" (stray
        # leading 1) would silently exclude those leads from the Stage column.
        last10 = lead_identity.normalise_phone(lead.get("phone"))
        if not last10:
            continue
        # Zoho_Database.leads._id is the BARE plid ("<num>-PROJECT"), not the
        # "ZOHO-<num>-PROJECT" form lead_profiles.lead_id uses — see
        # resolve_project's id_is_bare_plid doc for why this collection needs it.
        proj = lead_identity.resolve_project(
            lead.get("project"), lead.get("_id"), id_is_bare_plid=True)
        key = lead_identity.build_phone_project_key(last10, proj)
        out[key] = {
            "stage": _display_stage(lead),
            "reason": lead.get("not_interested_reason"),
        }
    return out


@crm_v2_bp.route("/api/v2/stages-map", methods=["GET"])
def stages_map():
    """Bulk map of every project-lead's stage, for the homepage Stage column.

    Keyed by ``"<last10phone>|<PROJECT>"`` so each row resolves its OWN
    project-lead's stage (a person with SPECTRA + LOFT can show different
    stages per row). Read live so the column is always current.
    """
    if _mongo_client is None:
        return jsonify({"error": "MongoDB client not initialised"}), 503
    try:
        return jsonify(build_stage_map(_mongo_client)), 200
    except Exception as exc:
        log.exception("stages_map failed")
        return jsonify({"error": str(exc)}), 500


@crm_v2_bp.route("/api/v2/lead/<plid>/trigger-call", methods=["POST"])
def lead_trigger_call(plid: str):
    """Trigger an AI call for a specific lead via :func:`fire_call`."""
    if _mongo_client is None:
        return jsonify({"error": "MongoDB client not initialised"}), 503
    body = request.get_json(force=True, silent=True) or {}

    zoho_db = _mongo_client["Zoho_Database"]
    lead = zoho_db["leads"].find_one({"_id": plid})
    if not lead:
        return jsonify({"error": f"Lead {plid!r} not found"}), 404

    phone_norm = normalise_phone(lead.get("phone", ""))
    if not phone_norm:
        return jsonify({"error": "Lead has invalid phone number"}), 400

    name = body.get("name") or (
        f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip()
        or lead.get("customer_name") or "Lead")
    project = body.get("project") or lead.get("project") or "LOFT"
    mlid = lead.get("mlid") or ""
    attempt = int(lead.get("ss_call_attempt_count", 0) or 0) + 1

    ok, call_id, err = fire_call(zoho_db, plid, phone_norm, name, project, mlid, attempt)
    log_event(zoho_db, plid, "MANUAL_CALL_TRIGGER", "dashboard",
              {"ok": ok, "call_id": call_id}, actor="dashboard")
    return jsonify({"ok": ok, "call_id": call_id, "error": err}), 200 if ok else 502


@crm_v2_bp.route("/api/v2/leads/bulk-trigger", methods=["POST"])
def leads_bulk_trigger():
    """Trigger AI calls for multiple leads (hard limit :data:`BULK_TRIGGER_MAX`).

    Admin only (API-V2-ROLE-GATING): this fires real outbound calls, so the
    role check is the first thing in the function — before touching the
    request body, the database, or fire_call. Only the server-side session
    role counts; the dashboard's own auth layer (require_login) already
    rejects unauthenticated requests before this view is even reached."""
    if session.get("role") != "admin":
        return jsonify({"error": "Admin only"}), 403
    if _mongo_client is None:
        return jsonify({"error": "MongoDB client not initialised"}), 503
    body = request.get_json(force=True, silent=True) or {}
    plids = body.get("plids", [])
    if not isinstance(plids, list) or len(plids) == 0:
        return jsonify({"error": "plids must be a non-empty list"}), 400
    if len(plids) > BULK_TRIGGER_MAX:
        return jsonify({"error": f"Bulk trigger limited to {BULK_TRIGGER_MAX} PLIDs per request; "
                                 f"received {len(plids)}"}), 400

    zoho_db = _mongo_client["Zoho_Database"]
    triggered = 0
    errors = []
    for plid in plids:
        try:
            lead = zoho_db["leads"].find_one({"_id": plid})
            if not lead:
                errors.append({"plid": plid, "error": "Lead not found"})
                continue
            phone_norm = normalise_phone(lead.get("phone", ""))
            if not phone_norm:
                errors.append({"plid": plid, "error": "Invalid phone number"})
                continue
            name = (f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip()
                    or lead.get("customer_name") or "Lead")
            project = lead.get("project") or "LOFT"
            mlid = lead.get("mlid") or ""
            attempt = int(lead.get("ss_call_attempt_count", 0) or 0) + 1
            ok, call_id, err = fire_call(zoho_db, plid, phone_norm, name, project, mlid, attempt)
            if ok:
                triggered += 1
            else:
                errors.append({"plid": plid, "error": err})
        except Exception as exc:
            log.exception("bulk_trigger: exception for plid=%s", plid)
            errors.append({"plid": plid, "error": str(exc)})
    return jsonify({"triggered": triggered, "errors": errors}), 200


def _parse_reminder_dt(val: str | None) -> datetime | None:
    """Parse a reminder datetime from the UI.

    Accepts ``YYYY-MM-DD`` (→ that date 09:00 UTC) or a full ISO
    ``YYYY-MM-DDTHH:MM`` (datetime-local input).  Returns aware UTC or None.
    """
    s = (val or "").strip()
    if not s:
        return None
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        # date-only → default to 09:00 UTC
        return datetime.strptime(s, "%Y-%m-%d").replace(hour=9, tzinfo=timezone.utc)
    except ValueError:
        return None


@crm_v2_bp.route("/api/v2/lead/<plid>/reminder", methods=["POST"])
def lead_set_reminder(plid: str):
    """Set follow-up reminders — INDEPENDENT per channel.

    Body::

        { "call_at": "YYYY-MM-DDTHH:MM"|null,   # schedule an AI call at this time
          "whatsapp_at": "YYYY-MM-DDTHH:MM"|null, # schedule a WhatsApp at this time
          "note": "..." }

    Each provided time enqueues a ONE-SHOT workflow_queue item
    (``reminder_call`` / ``reminder_whatsapp``).  Re-setting a channel REPLACES
    its prior pending reminder.  Passing null/empty for a channel clears that
    channel's pending reminder.  Legacy ``{date}`` is still accepted → treated
    as a passive ``follow_up_reminder`` note (no auto-fire).
    """
    if _mongo_client is None:
        return jsonify({"error": "MongoDB client not initialised"}), 503
    body = request.get_json(force=True, silent=True) or {}
    note = (body.get("note") or "").strip()
    zoho_db = _mongo_client["Zoho_Database"]

    lead = zoho_db["leads"].find_one({"_id": plid})
    if lead is None:
        return jsonify({"error": f"Lead {plid!r} not found"}), 404

    set_fields: dict[str, Any] = {"reminder_note": note, "updated_at": _utcnow()}
    scheduled: dict[str, str] = {}

    # Legacy passive date (kept for backward compat)
    legacy = _parse_reminder_dt(body.get("date"))
    if legacy:
        set_fields["follow_up_reminder"] = legacy

    now = _utcnow()
    for channel, action_type, field in (
        ("call_at", "reminder_call", "call_reminder_at"),
        ("whatsapp_at", "reminder_whatsapp", "whatsapp_reminder_at"),
    ):
        if channel not in body:
            continue
        raw = body.get(channel)
        due = _parse_reminder_dt(raw)
        # Reject a reminder in the PAST (only when a value was actually provided —
        # an empty value means "clear", which is allowed).
        if raw and (due is None or due <= now):
            return jsonify({"error": f"{channel} must be a valid future date/time"}), 400
        # V1: store the reminder date for display only. The relay owns automated
        # cadence — the dashboard does not auto-fire (no workflow queue in V1).
        if due:
            set_fields[field] = due
            scheduled[channel] = due.isoformat()
        else:
            set_fields[field] = None  # cleared

    try:
        zoho_db["leads"].update_one({"_id": plid}, {"$set": set_fields})
        log_event(zoho_db, plid, "REMINDER_SET", "dashboard",
                  {"scheduled": scheduled, "note": note}, actor="dashboard")
    except Exception as exc:
        log.exception("lead_set_reminder failed for plid=%s", plid)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, "scheduled": scheduled}), 200


@crm_v2_bp.route("/api/v2/lead/<plid>/notes", methods=["POST"])
def lead_set_notes(plid: str):
    """APPEND a sales note. Body: ``{notes: "..."}``.

    Each save adds a timestamped entry to ``sales_notes_log`` (a running log
    shown below the input). ``sales_notes`` keeps the latest text for backward
    compatibility / quick display.
    """
    if _mongo_client is None:
        return jsonify({"error": "MongoDB client not initialised"}), 503
    body = request.get_json(force=True, silent=True) or {}
    text = (body.get("notes") or "").strip()
    if not text:
        return jsonify({"error": "note is empty"}), 400
    now = _utcnow()
    entry = {"text": text, "at": now, "by": body.get("by") or "admin"}
    try:
        result = _mongo_client["Zoho_Database"]["leads"].update_one(
            {"_id": plid},
            {"$push": {"sales_notes_log": entry},
             "$set": {"sales_notes": text, "updated_at": now}})
        if result.matched_count == 0:
            return jsonify({"error": f"Lead {plid!r} not found"}), 404
    except Exception as exc:
        log.exception("lead_set_notes failed for plid=%s", plid)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True}), 200


@crm_v2_bp.route("/api/v2/lead/<plid>/bot-toggle", methods=["POST"])
def lead_bot_toggle(plid: str):
    """Enable/disable the bot for a lead (mirrors to user_profiles by phone).

    Body: ``{enabled: true|false}``
    """
    if _mongo_client is None:
        return jsonify({"error": "MongoDB client not initialised"}), 503
    body = request.get_json(force=True, silent=True) or {}
    if "enabled" not in body:
        return jsonify({"error": "enabled (bool) is required"}), 400

    bot_enabled = bool(body["enabled"])
    now = _utcnow()
    zoho_db = _mongo_client["Zoho_Database"]
    try:
        lead = zoho_db["leads"].find_one({"_id": plid}, {"phone": 1})
        if not lead:
            return jsonify({"error": f"Lead {plid!r} not found"}), 404
        zoho_db["leads"].update_one(
            {"_id": plid}, {"$set": {"bot_enabled": bot_enabled, "updated_at": now}})
        phone_norm = normalise_phone(lead.get("phone", ""))
        if phone_norm:
            zoho_db["user_profiles"].update_many(
                {"phone": {"$in": phone_variants(phone_norm)}},
                {"$set": {"bot_enabled": bot_enabled, "updated_at": now}})
    except Exception as exc:
        log.exception("lead_bot_toggle failed for plid=%s", plid)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, "bot_enabled": bot_enabled}), 200


@crm_v2_bp.route("/api/v2/lead/<plid>/crm", methods=["GET"])
def lead_crm_fields(plid: str):
    """Return the CRM-relevant dual-status fields for a lead."""
    if _mongo_client is None:
        return jsonify({"error": "MongoDB client not initialised"}), 503
    try:
        lead = _mongo_client["Zoho_Database"]["leads"].find_one({"_id": plid})
        if not lead:
            return jsonify({"error": f"Lead {plid!r} not found"}), 404
        lead = migrate_lead_to_dual_status(_mongo_client["Zoho_Database"], lead)
        return jsonify({
            "plid": plid,
            "stage": _display_stage(lead),
            "chatbot_status": lead.get("chatbot_status"),
            "call_status": lead.get("call_status"),
            "first_engagement_channel": lead.get("first_engagement_channel"),
            "not_interested_reason": lead.get("not_interested_reason"),
            "preferred_call_time": _dt_iso(lead.get("preferred_call_time")),
            "llm_confidence_latest": lead.get("llm_confidence_latest"),
            "sales_notes": lead.get("sales_notes", ""),
            "follow_up_reminder": _dt_iso(lead.get("follow_up_reminder")),
            "reminder_note": lead.get("reminder_note", ""),
            "bot_enabled": lead.get("bot_enabled", True),
            "last_call_at": _dt_iso(lead.get("last_call_at")),
            "last_action": lead.get("last_action"),
            "last_action_time": _dt_iso(lead.get("last_action_time")),
            "site_visit_date": _dt_iso(lead.get("site_visit_date")),
        }), 200
    except Exception as exc:
        log.exception("lead_crm_fields failed for plid=%s", plid)
        return jsonify({"error": str(exc)}), 500


@crm_v2_bp.route("/api/v2/crm-by-phone", methods=["GET"])
def crm_by_phone():
    """Resolve a lead's dual-status — the EXACT project-lead being viewed.

    Resolution order (so multi-project people aren't confused):
      1. ``?lead_id=ZOHO-<plid>`` → look up by that exact PLID (precise)
      2. ``?phone=...&project=...`` → match phone AND project
      3. ``?phone=...`` → first by phone (legacy fallback)
    """
    if _mongo_client is None:
        return jsonify({"error": "MongoDB client not initialised"}), 503
    zoho_db = _mongo_client["Zoho_Database"]

    lead = None
    lead_id = (request.args.get("lead_id") or "").strip()
    project = (request.args.get("project") or "").strip().upper()
    phone_norm = normalise_phone(request.args.get("phone", ""))

    # 1. Exact PLID from a ZOHO lead_id (e.g. "ZOHO-1729-SPECTRA" → "1729-SPECTRA")
    if lead_id.startswith("ZOHO-"):
        plid = lead_id[5:]
        lead = zoho_db["leads"].find_one({"_id": plid})

    # 2. phone + project
    if lead is None and phone_norm and project:
        lead = zoho_db["leads"].find_one({
            "phone": {"$in": phone_variants(phone_norm)},
            "project": {"$regex": f"^{re.escape(project)}$", "$options": "i"},
        })

    # 3. phone only (legacy fallback)
    if lead is None and phone_norm:
        lead = zoho_db["leads"].find_one({"phone": {"$in": phone_variants(phone_norm)}})

    if not lead:
        return jsonify({"exists": False}), 200
    lead = migrate_lead_to_dual_status(zoho_db, lead)

    def _iso(v):
        d = _aware(v)
        return d.isoformat() if d else None

    return jsonify({
        "exists": True,
        # str() so a numeric/ObjectId _id can't reach the browser as a JS number
        # (large ids lose precision as floats → wrong id POSTed back on edit).
        "plid": str(lead.get("plid") or lead.get("_id")),
        "mlid": lead.get("mlid"),
        "stage": _display_stage(lead),
        # Admin-only: the raw relay stage, so the CRM tab can show BOTH the
        # manual/effective stage and what the relay currently thinks. Performance/CI
        # get None — they only ever see the single (manual) stage. Enforced here so
        # the relay value is never even sent to non-admins.
        "relay_stage": (lead.get("prd_stage") if session.get("role") == "admin" else None),
        "prd_status": lead.get("prd_status"),
        "chatbot_status": lead.get("chatbot_status"),
        "call_status": lead.get("call_status"),
        "bot_enabled": lead.get("bot_enabled", True),
        "first_engagement_channel": lead.get("first_engagement_channel"),
        "not_interested_reason": lead.get("not_interested_reason"),
        "preferred_call_time": _iso(lead.get("preferred_call_time")),
        "call_status_label": lead.get("call_status_label") or lead.get("call_status_human"),
        "last_call_at": _iso(lead.get("last_call_at")),
        "total_call_duration_secs": lead.get("total_call_duration_secs"),
        "ss_call_attempt_count": lead.get("ss_call_attempt_count", 0),
        "sales_notes": lead.get("sales_notes", ""),
        "sales_notes_log": [
            {"text": e.get("text", ""), "at": _iso(e.get("at")), "by": e.get("by")}
            for e in (lead.get("sales_notes_log") or [])
        ],
        "follow_up_reminder": _iso(lead.get("follow_up_reminder")),
        "call_reminder_at": _iso(lead.get("call_reminder_at")),
        "whatsapp_reminder_at": _iso(lead.get("whatsapp_reminder_at")),
        "reminder_note": lead.get("reminder_note", ""),
        "last_action": lead.get("last_action"),
        "last_action_time": _iso(lead.get("last_action_time")),
    }), 200


@crm_v2_bp.route("/api/v2/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok", "version": "v2", "timestamp": _utcnow().isoformat()}), 200


# ══════════════════════════════════════════════════════════════════════════════
#  Workflow-queue cron
# ══════════════════════════════════════════════════════════════════════════════
