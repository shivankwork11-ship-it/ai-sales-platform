"""
crm_api.py — Zoho-replacement REST API (V1).

The dashboard exposes Zoho-shaped lead operations so the relay (asbl-crm-api)
can point its ``zoho.ts`` at the dashboard instead of ``zohoapis.in``. This is the
"dashboard IS the CRM" cutover: same operations Zoho gave us (find / get / create /
update lead, create note, create call log, list active leads for the cron), backed
by ``Zoho_Database.leads`` in Mongo.

Design
------
* The API speaks **Zoho field names** on the wire (``PRD_Stage``, ``Mobile``,
  ``Last_Inhouse_Call_ID`` …) so the relay's swap is minimal — change the base URL
  + auth, keep the field names. Internally it maps to the snake_case the dashboard
  already stores/reads, so the display side is untouched.
* Auth: ``Authorization: Bearer <RELAY_API_TOKEN>`` (env). An admin dashboard
  session is also accepted (for testing / internal use). If the token env is unset,
  bearer auth is rejected — only an admin session works — so it is safe to deploy
  dormant before cutover.
* Single shared MongoClient injected via init_app().
"""

import os
import re
import hmac
import logging
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, session

log = logging.getLogger(__name__)

crm_api_bp = Blueprint("crm_api", __name__)
_client = None


def init_app(app, mongo_client) -> None:
    global _client
    _client = mongo_client
    app.register_blueprint(crm_api_bp)
    # Indexes the relay's lookups rely on (exact-match → indexable). Phone search
    # uses a last-10 suffix regex (collection scan; fine at current scale — revisit
    # with a stored last-10 field if volume grows).
    try:
        leads = mongo_client["Zoho_Database"]["leads"]
        leads.create_index("last_inhouse_call_id", background=True, sparse=True)
        leads.create_index("last_arrowhead_call_id", background=True, sparse=True)
        leads.create_index("prd_stage", background=True)
        leads.create_index("plid", background=True, sparse=True)
        mongo_client["Zoho_Database"]["crm_notes"].create_index("lead_id", background=True)
        mongo_client["Zoho_Database"]["crm_call_logs"].create_index("lead_id", background=True)
    except Exception as e:
        log.warning("crm_api index creation skipped: %s", e)


def _leads():
    return _client["Zoho_Database"]["leads"]


def _notes():
    return _client["Zoho_Database"]["crm_notes"]


def _calls():
    return _client["Zoho_Database"]["crm_call_logs"]


# ── Field mapping: Zoho field name (wire) → Mongo field name (stored) ──────────
WIRE_TO_DB = {
    "id": "_id",
    "First_Name": "first_name", "Last_Name": "last_name",
    "Mobile": "phone", "Phone": "phone", "Email": "email",
    "Lead_Source": "lead_source",
    "Master_Lead_ID": "mlid", "Project_Lead_ID": "plid", "Source_Lead_ID": "source_lead_id",
    "ASBL_Project": "project",
    "Campaign_Name": "campaign_name", "Ad_Set_Name": "ad_set_name", "Ad_Name": "ad_name",
    "UTM_Source": "utm_source", "UTM_Medium": "utm_medium", "UTM_Campaign": "utm_campaign",
    "UTM_Content": "utm_content", "UTM_Term": "utm_term",
    "Lead_Received_At": "lead_received_at", "Born_Date": "born_date",
    "Lead_Budget": "lead_budget", "Size_Preference": "size_preference",
    "Floor_Preference": "floor_preference", "Possession_Timeline": "possession_timeline",
    "Purchase_Purpose": "purchase_purpose", "Lead_Comments": "lead_comments",
    "First_Page_Visited": "first_page_visited", "Last_Page_Visited": "last_page_visited",
    "Referrer_URL": "referrer_url", "Total_Page_Views": "total_page_views",
    "Time_Spent_Minutes": "time_spent_minutes",
    # PRD state machine
    "PRD_Stage": "prd_stage", "PRD_Status": "prd_status",
    "PRD_Last_Action": "prd_last_action", "PRD_Last_Action_Time": "prd_last_action_time",
    # Cadence counters
    "Chatbot_Attempt_Count": "chatbot_attempt_count",
    "Chatbot_Follow_up_Count": "chatbot_follow_up_count",
    "SS_Call_Attempt_Count": "ss_call_attempt_count",
    "Call_Attempt_Count": "call_attempt_count",
    # Call tracking
    "Call_Status": "call_status", "Call_Duration": "call_duration",
    "Total_Call_Duration_Secs": "total_call_duration_secs",
    "Last_Inhouse_Call_ID": "last_inhouse_call_id",
    "Last_Arrowhead_Call_ID": "last_arrowhead_call_id",
    "Last_Recording_URL": "last_recording_url", "Last_Call_At": "last_call_at",
    "Lead_Status": "lead_status",
    # Engagement / outcome
    "Site_Visit_Date": "site_visit_date", "Last_Intent": "last_intent",
    "Whatsapp_Replied": "whatsapp_replied", "Last_Customer_Response": "last_customer_response",
    "Intent_Captured": "intent_captured", "Not_Interested_Reason": "not_interested_reason",
    "Preferred_Call_Time": "preferred_call_time",
    # Resubmission
    "Resubmission_Count": "resubmission_count", "Resubmission_History": "resubmission_history",
    "Last_Resubmission_At": "last_resubmission_at", "Last_Resubmission_Source": "last_resubmission_source",
}
DB_TO_WIRE = {}
for _w, _d in WIRE_TO_DB.items():
    DB_TO_WIRE.setdefault(_d, _w)   # first wins (Mobile beats Phone for `phone`)

# snake_case aliases the relay may send in its NATIVE push payload. Inbound ONLY —
# deliberately NOT in WIRE_TO_DB, so DB_TO_WIRE (the read/serialise map) keeps
# emitting the canonical Zoho names (e.g. lead_budget → "Lead_Budget", not "budget").
INBOUND_ALIASES = {
    "mobile": "phone", "budget": "lead_budget", "created_time": "lead_created_at",
}

# Stages that end the workflow — excluded from the cron's active working set.
TERMINAL_STAGES = {"Not Interested", "Spam"}


def _to_db(wire_fields: dict) -> dict:
    """Zoho-cased (or snake-aliased) request body → Mongo snake_case doc.

    Unknown keys pass through as-is. Canonical Zoho names win; snake_case aliases
    are a fallback so the relay's native payload lands in the right db fields.
    """
    out = {}
    for k, v in (wire_fields or {}).items():
        out[WIRE_TO_DB.get(k) or INBOUND_ALIASES.get(k) or k] = v
    # Option A: the dashboard displays prd_stage (the V1 field the relay writes).
    # Keep the V2 `stage` mirror in lock-step so any reader of either field agrees
    # and there's no silent divergence between the relay and the dashboard UI.
    if "prd_stage" in out and out["prd_stage"]:
        out["stage"] = out["prd_stage"]
    return out


def _to_wire(doc: dict) -> dict:
    """Mongo doc → Zoho-cased response. Always emits `id`, `Mobile` and `Phone`."""
    if not doc:
        return {}
    out = {}
    for k, v in doc.items():
        if k == "_id":
            out["id"] = str(v)
            continue
        out[DB_TO_WIRE.get(k, k)] = v
    ph = doc.get("phone")
    if ph is not None:
        out["Mobile"] = ph
        out["Phone"] = ph
    return out


def _last10(s) -> str:
    d = re.sub(r"\D", "", str(s or ""))
    return d[-10:] if len(d) >= 10 else ""


def _authed() -> bool:
    """Bearer token (relay) OR admin dashboard session (internal/testing)."""
    token = os.environ.get("RELAY_API_TOKEN", "")
    if token:
        auth = request.headers.get("Authorization", "") or ""
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), token):
            return True
    return session.get("role") == "admin"


def _reject():
    return jsonify({"error": "unauthorized"}), 401


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(v):
    """ISO string / datetime → aware UTC datetime, else None."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def _mirror_profile(plid, doc) -> None:
    """Make a freshly-created relay lead appear on the homepage list immediately.

    The homepage reads ``analytics_db.lead_profiles``, which is otherwise rebuilt
    only by the DAILY cron — so without this a new relay lead is invisible on the
    main list for up to a day. We upsert a minimal profile keyed EXACTLY as
    ``cron_refresh`` does (``lead_id = "ZOHO-<plid>"``); the cron later overwrites it
    with full call/WhatsApp enrichment.

    Identity fields go in ``$set``; the empty engagement blocks + bookkeeping go in
    ``$setOnInsert`` so a re-create (idempotent upsert) NEVER wipes cron-computed
    counts. Best-effort: a failure here must not fail the lead write.
    """
    try:
        lead_id = f"ZOHO-{plid}"
        name = f"{doc.get('first_name','') or ''} {doc.get('last_name','') or ''}".strip().strip(".").strip()
        now_dt = datetime.now(timezone.utc)
        set_fields = {"updated_at": now_dt}
        if name:                     set_fields["lead_name"] = name
        if doc.get("phone"):         set_fields["lead_phone_number"] = doc["phone"]
        if doc.get("lead_source"):   set_fields["lead_source"] = doc["lead_source"]
        if doc.get("campaign_name"): set_fields["campaign_name"] = doc["campaign_name"]
        if doc.get("project"):       set_fields["project"] = doc["project"]
        born = _parse_iso(doc.get("lead_received_at") or doc.get("born_date")
                          or doc.get("lead_created_at"))
        if born:                     set_fields["lead_created_at"] = born
        _client["analytics_db"]["lead_profiles"].update_one(
            {"lead_id": lead_id},
            {"$set": set_fields,
             "$setOnInsert": {
                 "computed_at": now_dt,
                 "all_loft_ids": [lead_id],
                 "submission_history": [],
                 # Empty engagement blocks → homepage shows 0 counts until the cron
                 # enriches them. setOnInsert so a re-create can't clobber real data.
                 "website": {}, "whatsapp": {}, "ai_calls": {},
             }},
            upsert=True,
        )
    except Exception as e:
        log.warning("crm_api _mirror_profile failed for %s: %s", plid, e)


# ── READ: get one lead by id (PLID / zoho_lead_id) ─────────────────────────────
@crm_api_bp.route("/api/crm/leads/<lead_id>", methods=["GET"])
def crm_get_lead(lead_id):
    if not _authed():
        return _reject()
    doc = _leads().find_one({"$or": [{"_id": lead_id}, {"plid": lead_id}, {"zoho_lead_id": lead_id}]})
    return jsonify({"data": [_to_wire(doc)] if doc else []}), 200


# ── READ: search by phone / call-id / phone+project ────────────────────────────
@crm_api_bp.route("/api/crm/leads/search", methods=["GET"])
def crm_search_leads():
    if not _authed():
        return _reject()
    q = request.args
    inhouse = (q.get("inhouse_call_id") or "").strip()
    arrow   = (q.get("arrowhead_call_id") or "").strip()
    mobile  = (q.get("mobile") or q.get("phone") or "").strip()
    project = (q.get("project") or "").strip()

    if inhouse:
        docs = list(_leads().find({"last_inhouse_call_id": inhouse}).limit(5))
    elif arrow:
        docs = list(_leads().find({"last_arrowhead_call_id": arrow}).limit(5))
    elif mobile:
        last10 = _last10(mobile)
        if not last10:
            return jsonify({"data": []}), 200
        query = {"phone": {"$regex": re.escape(last10) + "$"}}
        if project:
            query["project"] = {"$regex": f"^{re.escape(project)}$", "$options": "i"}
        docs = list(_leads().find(query).limit(10))
    else:
        return jsonify({"error": "no search criteria (mobile / inhouse_call_id / arrowhead_call_id)"}), 400

    return jsonify({"data": [_to_wire(d) for d in docs]}), 200


# ── READ: active (non-terminal) leads for the cadence cron, paginated ──────────
@crm_api_bp.route("/api/crm/leads", methods=["GET"])
def crm_list_active_leads():
    if not _authed():
        return _reject()
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(500, max(1, int(request.args.get("per_page", 200))))
    except ValueError:
        page, per_page = 1, 200
    # Explicit allow-list so `?active=0` / `?active=false` does NOT silently apply
    # the terminal-stage filter (truthy-string bug); only an opt-in value filters.
    _active = (request.args.get("active") or "").strip().lower() in ("1", "true", "yes")
    query = {"prd_stage": {"$nin": list(TERMINAL_STAGES)}} if _active else {}
    cur = (_leads().find(query)
           .sort("prd_last_action_time", -1)
           .skip((page - 1) * per_page)
           .limit(per_page + 1))
    docs = list(cur)
    more = len(docs) > per_page
    docs = docs[:per_page]
    return jsonify({"data": [_to_wire(d) for d in docs],
                    "info": {"page": page, "per_page": per_page, "more_records": more}}), 200


# ── WRITE: create lead (ingest) ────────────────────────────────────────────────
@crm_api_bp.route("/api/crm/leads", methods=["POST"])
def crm_create_lead():
    if not _authed():
        return _reject()
    body = request.get_json(silent=True) or {}
    rows = body.get("data") if isinstance(body.get("data"), list) else [body]
    out = []
    for row in rows:
        doc = _to_db(row)
        lead_id = doc.pop("_id", None) or doc.get("plid")
        if not lead_id:
            out.append({"status": "error", "message": "missing id/Project_Lead_ID"})
            continue
        doc["updated_at"] = _now_iso()
        _leads().update_one({"_id": lead_id}, {"$set": doc, "$setOnInsert": {"created_at": _now_iso()}}, upsert=True)
        # Bootstrap the homepage row immediately (homepage reads lead_profiles,
        # rebuilt only by the daily cron). Best-effort; never blocks the lead write.
        _mirror_profile(lead_id, doc)
        out.append({"status": "success", "id": lead_id})
    return jsonify({"data": out}), 200


# ── WRITE: update lead fields (generic patch) ──────────────────────────────────
@crm_api_bp.route("/api/crm/leads/<lead_id>", methods=["PATCH", "PUT"])
def crm_update_lead(lead_id):
    if not _authed():
        return _reject()
    body = request.get_json(silent=True) or {}
    # Accept either a bare {fields} body or Zoho-style {"data": [{id, ...fields}]}.
    if isinstance(body.get("data"), list) and body["data"]:
        fields = dict(body["data"][0])
        fields.pop("id", None)
    else:
        fields = dict(body)
        fields.pop("id", None)
    doc = _to_db(fields)
    if not doc:
        return jsonify({"error": "no fields to update"}), 400
    doc["updated_at"] = _now_iso()
    res = _leads().update_one(
        {"$or": [{"_id": lead_id}, {"plid": lead_id}, {"zoho_lead_id": lead_id}]},
        {"$set": doc})
    if res.matched_count == 0:
        return jsonify({"data": [], "error": "lead not found"}), 404
    return jsonify({"data": [{"status": "success", "id": lead_id}]}), 200


# ── WRITE: create a Note on a lead (Zoho Notes module replacement) ─────────────
@crm_api_bp.route("/api/crm/leads/<lead_id>/notes", methods=["POST"])
def crm_create_note(lead_id):
    if not _authed():
        return _reject()
    body = request.get_json(silent=True) or {}
    note = {
        "lead_id": lead_id,
        "title": body.get("Note_Title") or body.get("title") or "",
        "content": body.get("Note_Content") or body.get("content") or "",
        "created_at": _now_iso(),
    }
    ins = _notes().insert_one(note)
    return jsonify({"data": [{"status": "success", "id": str(ins.inserted_id)}]}), 200


# ── WRITE: create a Call log on a lead (Zoho Calls module replacement) ─────────
@crm_api_bp.route("/api/crm/leads/<lead_id>/calls", methods=["POST"])
def crm_create_call(lead_id):
    if not _authed():
        return _reject()
    body = request.get_json(silent=True) or {}
    call = {
        "lead_id": lead_id,
        "subject": body.get("Subject") or body.get("subject") or "",
        "call_type": body.get("Call_Type") or body.get("call_type") or "Outbound",
        "call_status": body.get("Call_Status") or body.get("call_status") or "",
        "call_result": body.get("Call_Result") or body.get("call_result") or "",
        "call_duration": body.get("Call_Duration") or body.get("call_duration") or "",
        "description": body.get("Description") or body.get("description") or "",
        "call_start_time": body.get("Call_Start_Time") or body.get("call_start_time") or "",
        "created_at": _now_iso(),
    }
    ins = _calls().insert_one(call)
    return jsonify({"data": [{"status": "success", "id": str(ins.inserted_id)}]}), 200


# ── Health / readiness (no auth) ───────────────────────────────────────────────
@crm_api_bp.route("/api/crm/health", methods=["GET"])
def crm_health():
    return jsonify({"ok": True, "service": "crm-api", "token_configured": bool(os.environ.get("RELAY_API_TOKEN"))}), 200
