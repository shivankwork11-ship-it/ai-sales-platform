# ─────────────────────────────────────────────────────────────────────────────
#  ASBL Lead Intelligence Dashboard
#  Run:  python3 dashboard.py
#  Opens automatically at http://localhost:5000
# ─────────────────────────────────────────────────────────────────────────────

# ── Dependency check ──────────────────────────────────────────────────────────
try:
    import flask
except ImportError:
    import subprocess, sys
    print("Installing flask...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "flask"])
    import flask

try:
    import pymongo
except ImportError:
    import subprocess, sys
    print("Installing pymongo...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pymongo"])
    import pymongo

# ── Imports ───────────────────────────────────────────────────────────────────
import json, threading, webbrowser, time, io, re, hmac, secrets, base64, hashlib
import urllib.parse
from datetime import datetime, timezone
from bson import ObjectId
from flask import Flask, jsonify, request, render_template_string, send_file, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo import MongoClient

# ── DB Connection ─────────────────────────────────────────────────────────────
import os
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass
# The Mongo connection string (with credentials) must NEVER be hard-coded in
# source — this repo was public and a committed credential is treated as leaked.
# It is read ONLY from the MONGO_URI environment variable (set in Vercel project
# settings for Production/Preview/Development, and in your shell for local dev).
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError(
        "MONGO_URI environment variable is not set. Configure it in the Vercel "
        "project settings (or export it locally) — the DB connection string is "
        "no longer hard-coded in source for security."
    )
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=20000, connectTimeoutMS=20000, socketTimeoutMS=25000)
db = client["analytics_db"]
leads_col = db["lead_profiles"]

# ── Second MongoDB connection (Atlas - QA results) ────────────────────────────
MONGO_URI_QA = os.environ.get("MONGO_URI_QA_RESULTS", "")
try:
    # Verify the Atlas server certificate against trusted CA roots (certifi).
    # Previously used tlsAllowInvalidCertificates=True, which disabled cert
    # checking entirely (MITM risk) for no benefit — Atlas presents a valid cert.
    import certifi
    qa_client = MongoClient(
        MONGO_URI_QA,
        serverSelectionTimeoutMS=10000,
        tls=True,
        tlsCAFile=certifi.where(),
    ) if MONGO_URI_QA else None
    qa_db = qa_client["qa_results"] if qa_client else None
except Exception:
    # certifi missing / bad URI / unreachable → QA features degrade gracefully;
    # the main dashboard (analytics_db) is unaffected.
    qa_client = None
    qa_db = None

# ── Indexes (idempotent — safe to run on every cold start) ────────────────────
try:
    leads_col.create_index("lead_id",            unique=True, background=True)
    leads_col.create_index("computed_at",                     background=True)
    leads_col.create_index("flag",                            background=True)
    leads_col.create_index("lead_name",                       background=True)
    leads_col.create_index("lead_phone_number",               background=True)
    leads_col.create_index("lead_source",                     background=True)
    leads_col.create_index("campaign_name",                   background=True)
except Exception:
    pass  # never block startup if DB is temporarily unreachable

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)

def _resolve_secret_key():
    """Flask session-signing key. NEVER hardcode it in source (git) — a known key
    lets anyone forge an admin session cookie with no password. Order:
      1. SECRET_KEY env var (preferred, set it in Vercel when you can).
      2. A random key persisted once in the DB (analytics_db.app_config) — stable
         across restarts, not in git, and consistent across serverless instances.
      3. Last resort: a per-process random key (app still works; sessions just
         won't survive a cold start). Never a static git-committed default.
    """
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    import secrets as _secrets
    try:
        cfg = client["analytics_db"]["app_config"]
        doc = cfg.find_one({"_id": "flask_secret_key"})
        if not (doc and doc.get("value")):
            cfg.update_one({"_id": "flask_secret_key"},
                           {"$setOnInsert": {"value": _secrets.token_hex(32)}}, upsert=True)
            doc = cfg.find_one({"_id": "flask_secret_key"})  # re-read: first writer wins
        if doc and doc.get("value"):
            return doc["value"]
    except Exception:
        pass
    return _secrets.token_hex(32)

app.secret_key = _resolve_secret_key()

# Session-cookie hardening (H2):
#  • HttpOnly — JS can't read the cookie (limits XSS cookie theft).
#  • SameSite=Lax — cookie NOT sent on cross-site POSTs → blocks most CSRF against
#    session-authenticated endpoints; same-site UI fetches still send it.
#  • Secure — HTTPS only; gated on Vercel (always HTTPS) so local http dev works.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),
)

# ── CRM iframe embed: frame-ancestors CSP ─────────────────────────────────────
# /embed/leads (below) is meant to be embedded, via <iframe>, by exactly one
# external CRM origin — everything else must stay un-embeddable. CRM_FRAME_ORIGIN
# names that one trusted origin; validated hard on every read (cheap: a few
# string checks) rather than cached at import time, so a bad/missing value can
# never silently linger past its own request, and so it's straightforward to
# exercise in tests without reloading the module. Never logged — only ever
# turned into a header value or discarded.
def _validate_crm_frame_origin(raw):
    """A syntactically exact origin only — scheme + host[:port], nothing else.
    Returns the normalised "scheme://host[:port]" string, or None if raw is
    missing/invalid. None means "fail closed": callers must fall back to
    frame-ancestors 'self' only, never widen to '*' or skip the check."""
    if not raw or "*" in raw:
        return None
    try:
        parsed = urllib.parse.urlsplit(raw.strip())
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"

def _wants_crm_frame_ancestors():
    """Which HTML responses may additionally be framed by CRM_FRAME_ORIGIN:
    the embed route itself, /login (the auth step that route redirects
    unauthenticated CRM iframe loads to), and a lead's detail page specifically
    when opened from inside the embed (?embed=1) — the one "essential table
    interaction" (viewing a lead) the embed route is required to support.
    Everything else stays frame-ancestors 'self' only."""
    if request.path in ("/embed/leads", "/login"):
        return True
    return request.path.startswith("/lead/") and request.args.get("embed") == "1"

@app.after_request
def _frame_ancestors_csp(resp):
    try:
        if resp.mimetype == "text/html":
            extra = _validate_crm_frame_origin(os.environ.get("CRM_FRAME_ORIGIN", "")) \
                if _wants_crm_frame_ancestors() else None
            resp.headers["Content-Security-Policy"] = (
                f"frame-ancestors 'self' {extra}" if extra else "frame-ancestors 'self'")
    except Exception:
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    return resp

# ── CRM iframe embed: stateless token auth ────────────────────────────────────
# A cross-site iframe drops the session cookie, so the embed can't rely on it. The
# CRM instead mints a short-lived HS256 JWT (signed with EMBED_TOKEN_SECRET) and
# passes it as ?token= on the embed page load; the page then echoes it in the
# X-Embed-Token header on every API call. This verifier is intentionally minimal
# (stdlib only) and FAILS CLOSED on any problem. It only ever grants READ access —
# see require_login, which never sets role='admin' from a token.
def _verify_embed_token(token):
    """Return the JWT claims dict if `token` is a valid, unexpired HS256 token
    signed with EMBED_TOKEN_SECRET, else None. Rejects any other alg (blocks the
    'none'/alg-confusion attack) and enforces exp."""
    secret = os.environ.get("EMBED_TOKEN_SECRET", "")
    if not secret or not token or not isinstance(token, str) or token.count(".") != 2:
        return None
    try:
        h_b64, p_b64, s_b64 = token.split(".")
        def _b64d(seg):
            return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))
        header = json.loads(_b64d(h_b64))
        if header.get("alg") != "HS256":
            return None  # only HS256; never "none" or an asymmetric alg
        expected = base64.urlsafe_b64encode(
            hmac.new(secret.encode(), (h_b64 + "." + p_b64).encode(), hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        if not hmac.compare_digest(expected, s_b64):
            return None  # signature mismatch
        claims = json.loads(_b64d(p_b64))
        now = datetime.now(timezone.utc).timestamp()
        exp = claims.get("exp")
        if exp is None or float(exp) < now:
            return None  # require + enforce expiry (no non-expiring embed tokens)
        nbf = claims.get("nbf")
        if nbf is not None and float(nbf) > now + 60:  # 60s clock-skew grace
            return None
        return claims
    except Exception:
        return None  # malformed token → fail closed

# ── CRM v2 (Zoho replacement) — register blueprint ───────────────────────────
try:
    from crm_v2 import init_app as crm_v2_init
    crm_v2_init(app, client)
except Exception as _crm_v2_err:
    import logging as _logging
    _logging.getLogger(__name__).warning("crm_v2 not loaded: %s", _crm_v2_err)

# Zoho-replacement REST API (V1) — the relay points its zoho.ts here instead of zohoapis.in.
try:
    from crm_api import init_app as crm_api_init
    crm_api_init(app, client)
except Exception as _crm_api_err:
    import logging as _logging
    _logging.getLogger(__name__).warning("crm_api not loaded: %s", _crm_api_err)

# Bulk Manual Calling — cold-call leads that live only in ASBLVoiceBot.
try:
    from bulk_calls import init_app as bulk_calls_init
    bulk_calls_init(app, client)
except Exception as _bulk_err:
    import logging as _logging
    _logging.getLogger(__name__).warning("bulk_calls not loaded: %s", _bulk_err)

# Pipeline — lead funnel/pivot dashboard (ported from the standalone Node version).
# Registers routes now; pipeline.configure(SHARED_HEAD) runs later in this file,
# right after SHARED_HEAD is defined (pipeline.py can't import it directly without
# a circular import, since dashboard.py imports pipeline's init_app first).
try:
    import pipeline as _pipeline_mod
    _pipeline_mod.init_app(app, client)
except Exception as _pipeline_err:
    import logging as _logging
    _logging.getLogger(__name__).warning("pipeline not loaded: %s", _pipeline_err)

# Main Dashboard — ANGAD funnel dashboard, reverse-proxied in as a tab. ADDITIVE:
# ANGAD runs unmodified as its own loopback process; this only mounts /main-dashboard
# (see main_dashboard.py). No existing logic is touched; if it fails to load, the
# rest of the app is unaffected (the tab just won't be served).
try:
    from main_dashboard import init_app as main_dashboard_init
    main_dashboard_init(app, client)
except Exception as _main_dash_err:
    import logging as _logging
    _logging.getLogger(__name__).warning("main_dashboard not loaded: %s", _main_dash_err)

# ── Users ─────────────────────────────────────────────────────────────────────
# Logins live in analytics_db.dashboard_users (seeded in step 1). No passwords are
# hardcoded here anymore (C2). USERS is now only an optional ENV break-glass: if
# ADMIN_PASSWORD / CI_PASSWORD / PERFORMANCE_PASSWORD are set, they act as a
# fallback/recovery login (used only if the email isn't found in the DB). Empty by
# default — the real, unchanged passwords now live only in the DB.
USERS = {}
for _e, _role, _name, _envk in [
    ("admin@asbl.in",       "admin",       "Admin",            "ADMIN_PASSWORD"),
    ("ci@asbl.in",          "ci",          "CI Team",          "CI_PASSWORD"),
    ("performance@asbl.in", "performance", "Performance Team", "PERFORMANCE_PASSWORD"),
]:
    _pw = os.environ.get(_envk)
    if _pw:
        USERS[_e] = {"hash": generate_password_hash(_pw), "role": _role, "name": _name}

# ── Move logins OUT of the codebase into the DB (C2) — SAME passwords ───────────
# Password hashes live in analytics_db.dashboard_users (not readable from the repo).
# Step 1 (current): seed the DB once from USERS, and resolve logins DB-first with a
# fallback to USERS so nothing can break. Step 2 removes the hardcoded USERS values
# entirely once the DB is confirmed seeded — then the passwords exist only in the DB.
_dash_users = client["analytics_db"]["dashboard_users"]

def _seed_dashboard_users():
    try:
        if _dash_users.estimated_document_count() == 0:
            for _email, _u in USERS.items():
                _dash_users.update_one(
                    {"_id": _email},
                    {"$setOnInsert": {"hash": _u["hash"], "role": _u["role"], "name": _u["name"]}},
                    upsert=True)
    except Exception:
        pass

_seed_dashboard_users()

def _get_login_user(email):
    """Resolve a login by email: DB first (authoritative), then the in-code USERS
    fallback (kept only until Step 2 removes it)."""
    try:
        d = _dash_users.find_one({"_id": email})
        if d and d.get("hash"):
            return {"hash": d["hash"], "role": d.get("role"), "name": d.get("name")}
    except Exception:
        pass
    return USERS.get(email)

# ── Auth guard ────────────────────────────────────────────────────────────────
_PUBLIC_ROUTES = {"/login", "/api/login", "/api/cron/refresh",
                  "/api/v2/health", "/api/crm/health"}

@app.before_request
def require_login():
    if request.path in _PUBLIC_ROUTES:
        return
    # CRM API (Zoho-replacement): every route authenticates per-request inside the
    # handler (bearer RELAY_API_TOKEN OR admin session), so it manages its own auth.
    if request.path.startswith("/api/crm/"):
        return
    # Everything else — including /api/v2/* (these are the dashboard's OWN display +
    # manual-control endpoints, called by the logged-in UI; the former external v2
    # webhooks were removed) — REQUIRES a logged-in session. The previous blanket
    # /api/v2 exemption left CRM state-change + bulk-PII endpoints open to the
    # internet (unauthenticated read of every lead and unauthorized stage changes).
    if not session.get("user_email"):
        # CRM iframe embed: a valid embed token authenticates the request WITHOUT a
        # cookie (a cross-site iframe drops the session cookie). The CRM passes a
        # short-lived HS256 JWT as ?token= on the embed page load; the page echoes it
        # in X-Embed-Token on every API call. READ-ONLY: we never set role='admin'
        # here, so all admin/mutation endpoints (gated on role=='admin') stay closed;
        # only 'performance' is honoured (for its data redaction).
        _et = request.args.get("token", "") or request.headers.get("X-Embed-Token", "")
        _claims = _verify_embed_token(_et) if _et else None
        if _claims:
            session["user_email"] = str(_claims.get("sub") or "crm-embed")
            session["name"]       = str(_claims.get("name") or _claims.get("sub") or "CRM")
            session["role"]       = "performance" if _claims.get("role") == "performance" else ""
            session["_embed_only"] = True
        else:
            # Clean 401 for API calls (a fetch shouldn't be redirected to the login HTML);
            # browser page loads still get sent to the login screen.
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            next_path = request.path
            if request.query_string:
                next_path += "?" + request.query_string.decode()
            return redirect("/login?next=" + urllib.parse.quote(next_path, safe=""))

    # Embed-token sessions are read-only display only: even though the viewer can page
    # through leads in the UI, block bulk-PII export and the admin surface outright.
    if session.get("_embed_only") and (
            request.path == "/api/export" or request.path.startswith("/api/admin/")):
        return jsonify({"error": "forbidden"}), 403

    # ── CSRF (defense in depth) ────────────────────────────────────────────────
    # Mint a per-session token; it is mirrored into a JS-readable cookie by the
    # after_request below, and the page's fetch wrapper echoes it in X-CSRF-Token.
    if not session.get("csrf_token"):
        session["csrf_token"] = secrets.token_hex(32)
    # State-changing browser requests must present the matching token. SameSite=Lax
    # already blocks cross-site cookie-bearing POSTs; this closes the residual gap.
    # Server-to-server callers authenticate with a Bearer token (browsers can't
    # forge custom headers cross-site) and are exempt; /api/crm/* returned above.
    # ".../seen" is sent via navigator.sendBeacon (can't set a custom header) and
    # only clears a per-user "new activity" highlight — non-sensitive, so it is CSRF
    # exempt (still requires a logged-in session).
    if request.method not in ("GET", "HEAD", "OPTIONS", "TRACE"):
        if (not request.headers.get("Authorization", "").startswith("Bearer ")
                and not request.path.endswith("/seen")):
            _tok = session.get("csrf_token", "")
            _hdr = request.headers.get("X-CSRF-Token", "")
            if not _tok or not hmac.compare_digest(str(_hdr), str(_tok)):
                return jsonify({"error": "csrf_failed"}), 403

# ── Serialiser ────────────────────────────────────────────────────────────────
def serialize(obj):
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=timezone.utc)  # naive → UTC so JS converts to local time correctly
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")

def to_json(data):
    return json.dumps(data, default=serialize)

# ── Performance-team redactions (hide specific calls / WhatsApp messages) ──────
# ADMIN hides an item → it is stripped for the PERFORMANCE role only (CI + admin
# still see everything). Fully ADDITIVE: a separate collection + two OPTIONAL
# profile fields. A profile gets `ai_calls_performance`/`whatsapp_performance`
# ONLY when it has a hidden item; every read for the performance role falls back
# to the full `ai_calls`/`whatsapp` when those are absent — so until an admin
# hides something, nothing in the existing flow changes.
import hashlib
redactions_col = client["Zoho_Database"]["redactions"]
try:
    redactions_col.create_index([("item_id", 1)], background=True)
    redactions_col.create_index([("lead_id", 1)], background=True)
except Exception:
    pass

# Call-outcome buckets — SHARED by the cron tally and the performance re-tally so
# the two can never classify a call differently.
CALL_ANSWERED      = {"CONNECTED", "AUTO_CALLBACK", "PRE_SITE", "NOT_INTERESTED", "DROPPED", "VIRTUAL_TOUR"}
CALL_NOT_ANSWERED  = {"NO_ANSWER", "BUSY"}
CALL_NOT_CONNECTED = {"NOT_CONNECTED", "SWITCHED_OFF"}


def _wa_fp(phone, m):
    """Stable fingerprint for ONE WhatsApp message (messages have no native id).
    Computed from the {type,date,message} shape shared by the live view and the
    stored profile, so hide-time and read-time always agree."""
    key = "%s|%s|%s|%s" % (
        re.sub(r"[^0-9]", "", str(phone or ""))[-10:],
        m.get("type", ""), m.get("date", ""), m.get("message", ""))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _perf_rollup(ai_calls, whatsapp, phone, hidden_calls, hidden_wa):
    """Re-tally ai_calls + whatsapp for the VISIBLE subset (Performance view),
    excluding hidden items. Pure function of the stored detail arrays — used by
    BOTH the cron and the instant recompute so they can't drift. pickup + all
    counts are exact; avg duration = mean of visible outbound calls with a
    duration. Returns (ai_calls_perf, whatsapp_perf)."""
    convs = (ai_calls or {}).get("call_conversations") or []
    vis = [c for c in convs if str(c.get("call_sid", "")) not in hidden_calls]
    outb = [c for c in vis if (c.get("call_direction") or "").lower() != "inbound"]
    ans = sum(1 for c in outb if c.get("call_outcome") in CALL_ANSWERED)
    na  = sum(1 for c in outb if c.get("call_outcome") in CALL_NOT_ANSWERED)
    nc  = sum(1 for c in outb if c.get("call_outcome") in CALL_NOT_CONNECTED)
    inb = sum(1 for c in vis if (c.get("call_direction") or "").lower() == "inbound")
    durs = [c.get("duration_secs") for c in outb if (c.get("duration_secs") or 0) > 0]
    ai = {"outbound_calls_triggered": len(outb), "calls_answered": ans,
          "calls_not_answered": na, "calls_not_connected": nc, "inbound_calls": inb,
          "pickup_ratio": round(ans / (ans + na), 2) if (ans + na) else None,
          "average_call_duration_secs": round(sum(durs) / len(durs), 2) if durs else 0.0,
          "call_conversations": vis}
    msgs = (whatsapp or {}).get("user_conversation_history") or []
    vism = [m for m in msgs if _wa_fp(phone, m) not in hidden_wa]
    wa = {"user_conversation_depth": sum(1 for m in vism if m.get("type") == "inbound"),
          "outbound_count": sum(1 for m in vism if m.get("type") == "outbound"),
          "user_conversation_history": vism}
    return ai, wa


def _load_hidden_sets():
    """Global sets of currently-hidden call_sids and WhatsApp fingerprints."""
    calls, wa = set(), set()
    try:
        for r in redactions_col.find({"active": True}, {"item_type": 1, "item_id": 1}):
            if r.get("item_type") == "call":
                calls.add(str(r.get("item_id")))
            elif r.get("item_type") == "whatsapp":
                wa.add(str(r.get("item_id")))
    except Exception:
        pass
    return calls, wa


def _lead_has_hidden(profile, phone, hidden_calls, hidden_wa):
    convs = (profile.get("ai_calls") or {}).get("call_conversations") or []
    if any(str(c.get("call_sid", "")) in hidden_calls for c in convs):
        return True
    msgs = (profile.get("whatsapp") or {}).get("user_conversation_history") or []
    return any(_wa_fp(phone, m) in hidden_wa for m in msgs)


def _recompute_perf_for_lead(lead_id):
    """Rebuild (or clear) one lead's performance rollup right after a hide/unhide,
    so the change reflects immediately without waiting for the daily cron."""
    prof = leads_col.find_one({"lead_id": lead_id},
                              {"ai_calls": 1, "whatsapp": 1, "lead_phone_number": 1})
    if not prof:
        return
    hc, hw = _load_hidden_sets()
    phone = prof.get("lead_phone_number")
    if _lead_has_hidden(prof, phone, hc, hw):
        ai, wa = _perf_rollup(prof.get("ai_calls"), prof.get("whatsapp"), phone, hc, hw)
        leads_col.update_one({"lead_id": lead_id},
                             {"$set": {"ai_calls_performance": ai, "whatsapp_performance": wa}})
    else:
        leads_col.update_one({"lead_id": lead_id},
                             {"$unset": {"ai_calls_performance": "", "whatsapp_performance": ""}})


def _recompute_perf_for_phone(phone, fallback_lead_id=None):
    """A hidden call/message shows on EVERY project-row that shares the phone
    (calls & WhatsApp are matched by phone). So a hide/unhide must recompute the
    performance rollup for ALL same-phone leads — not just the one being viewed —
    or a sibling row still leaks the item to Performance."""
    p10 = re.sub(r"[^0-9]", "", str(phone or ""))[-10:]
    ids = []
    if len(p10) == 10:
        ids = [d["lead_id"] for d in leads_col.find(
            {"lead_phone_number": {"$regex": re.escape(p10) + "$"}}, {"lead_id": 1})
            if d.get("lead_id")]
    if not ids and fallback_lead_id:
        ids = [fallback_lead_id]
    for lid in ids:
        _recompute_perf_for_lead(lid)


# ── Helpers ───────────────────────────────────────────────────────────────────
def safe_get(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
        if d is None:
            return default
    return d

# ── API Routes ────────────────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    _nb = {"bulk_manual": {"$ne": True}}  # exclude bulk-manual (cold-call) leads
    total = leads_col.count_documents(_nb)
    with_website = leads_col.count_documents({**_nb, "$or": [{"website.messages_typed": {"$gt": 0}}, {"website.cta_triggered": {"$gt": 0}}]})
    with_wa = leads_col.count_documents({**_nb, "whatsapp.user_conversation_depth": {"$gt": 0}})
    with_calls = leads_col.count_documents({**_nb, "ai_calls.calls_answered": {"$gt": 0}})
    # Real cache-freshness signal: when the background refresh last SUCCEEDED (not the
    # client fetch time). Surfaced in the header so a frozen cache is visible at a glance.
    _cm = db["cron_meta"].find_one(
        {"_id": "cron_refresh"},
        {"last_successful_run": 1, "last_completed_run": 1, "last_status": 1, "last_errors": 1}) or {}
    _lsr = _cm.get("last_successful_run")
    if isinstance(_lsr, datetime) and _lsr.tzinfo is None:
        _lsr = _lsr.replace(tzinfo=timezone.utc)
    _lcr = _cm.get("last_completed_run")
    if isinstance(_lcr, datetime) and _lcr.tzinfo is None:
        _lcr = _lcr.replace(tzinfo=timezone.utc)
    return jsonify({"total": total, "with_website": with_website, "with_wa": with_wa,
                    "with_calls": with_calls,
                    "last_successful_run": _lsr.isoformat() if _lsr else None,
                    # last run's outcome so the UI can show a partial/failed sync
                    "last_completed_run": _lcr.isoformat() if _lcr else None,
                    "last_status": _cm.get("last_status"),
                    "last_errors": _cm.get("last_errors")})

@app.route("/api/leads")
def api_leads():
    q = request.args.get("q", "").strip()
    # Bulk-manual (cold-call) leads live in lead_profiles too but belong ONLY to
    # the Bulk Calls tab — keep them off the main Leads list.
    query = {"bulk_manual": {"$ne": True}}
    if q:
        query["$or"] = [
            {"lead_name": {"$regex": q, "$options": "i"}},
            {"lead_phone_number": {"$regex": q, "$options": "i"}},
            {"lead_source": {"$regex": q, "$options": "i"}},
            {"campaign_name": {"$regex": q, "$options": "i"}},
        ]
    projection = {
        "lead_id": 1, "lead_name": 1, "lead_phone_number": 1, "source_lead_id": 1,
        "lead_source": 1, "campaign_name": 1, "project": 1, "lead_created_at": 1,
        "last_activity_at": 1, "last_activity_type": 1, "last_event_at": 1,
        "website.messages_typed": 1, "website.cta_triggered": 1,
        "whatsapp.user_conversation_depth": 1, "whatsapp.outbound_count": 1,
        "ai_calls.outbound_calls_triggered": 1, "ai_calls.calls_answered": 1,
        "ai_calls.pickup_ratio": 1, "computed_at": 1, "flag": 1,
        "all_loft_ids": 1,
    }
    # Performance role sees the hidden-adjusted rollup (falls back to full when a
    # lead has nothing hidden). Every other role: projection + output unchanged.
    restricted = session.get("role") == "performance"
    if restricted:
        projection.update({
            "ai_calls_performance.outbound_calls_triggered": 1,
            "ai_calls_performance.calls_answered": 1,
            "ai_calls_performance.pickup_ratio": 1,
            "whatsapp_performance.user_conversation_depth": 1,
            "whatsapp_performance.outbound_count": 1,
        })
    # Sort by lead_created_at (born date) descending — newest leads first.
    # (Previously sorted by computed_at, an internal recompute timestamp, which
    # floated old inactive leads to the top whenever their profile was rebuilt.)
    #
    # The list page loads the whole set once and does all search / stats / filtering
    # client-side, so a low cap SILENTLY drops leads from search results AND from the
    # flag/engagement counts (undercount, not just a shorter list). The projection is
    # lean (no transcripts / message logs / call convos), so each row is ~a few hundred
    # bytes — 20k rows is a safe headroom well above the current dataset while still
    # bounding a pathological payload. If we ever hit it, the warning tells us it's
    # time for real server-side pagination.
    _LEADS_CAP = 20000
    docs = list(leads_col.find(query, projection)
                .sort("lead_created_at", -1).limit(_LEADS_CAP))
    if len(docs) >= _LEADS_CAP:
        app.logger.warning("api_leads hit the %d-row cap — results truncated; "
                           "add server-side pagination.", _LEADS_CAP)
    if restricted:
        for d in docs:
            if d.get("ai_calls_performance"):
                d["ai_calls"] = d["ai_calls_performance"]
            if d.get("whatsapp_performance"):
                d["whatsapp"] = d["whatsapp_performance"]
            d.pop("ai_calls_performance", None)
            d.pop("whatsapp_performance", None)

    # Canonical resolved_project/resolved_stage per row (lead_identity — the SAME
    # rules pipeline.py's Booked Site Visit uses), so the Stage column and its
    # filter/count stop independently redefining these in JS. Best-effort: if
    # crm_v2 didn't load (e.g. a `requests`-less environment), rows just come
    # back without these two fields, same as before this change existed.
    try:
        import lead_identity
        from crm_v2 import build_stage_map
        stage_map = build_stage_map(client)
        for d in docs:
            proj = lead_identity.resolve_project(
                d.get("project"), d.get("lead_id") or d.get("_id"), d.get("campaign_name"))
            d["resolved_project"] = proj
            phone10 = lead_identity.normalise_phone(d.get("lead_phone_number"))
            info = stage_map.get(lead_identity.build_phone_project_key(phone10, proj))
            d["resolved_stage"] = (info or {}).get("stage")
            d["resolved_reason"] = (info or {}).get("reason")
    except Exception:
        app.logger.warning("resolved_project/resolved_stage attach failed", exc_info=True)

    return app.response_class(to_json(docs), mimetype="application/json")

@app.route("/api/lead/<lead_id>")
def api_lead(lead_id):
    doc = leads_col.find_one({"lead_id": lead_id})
    if not doc:
        return jsonify({"error": "Not found"}), 404
    # Performance role: serve the hidden-adjusted call list + summary (the stored
    # perf rollup already excludes hidden calls). Falls back to full when absent.
    if session.get("role") == "performance":
        if doc.get("ai_calls_performance"):
            doc["ai_calls"] = doc["ai_calls_performance"]
        if doc.get("whatsapp_performance"):
            doc["whatsapp"] = doc["whatsapp_performance"]
        doc.pop("ai_calls_performance", None)
        doc.pop("whatsapp_performance", None)
    return app.response_class(to_json(doc), mimetype="application/json")

# ── "New activity" seen-tracking (per-user unread highlight) ───────────────────
lead_views_col = client["analytics_db"]["lead_views"]
try:
    lead_views_col.create_index("user_email", background=True)
except Exception:
    pass

@app.route("/api/lead/<lead_id>/seen", methods=["POST"])
def api_mark_seen(lead_id):
    """Mark a lead as viewed by the current user (clears its new-activity highlight
    until the next event). Called when the lead detail page opens."""
    user = session.get("user_email")
    if not user:
        return jsonify({"ok": False}), 401
    lead_views_col.update_one(
        {"_id": user + "|" + lead_id},
        {"$set": {"user_email": user, "lead_id": lead_id,
                  "seen_at": datetime.now(timezone.utc)}},
        upsert=True)
    return jsonify({"ok": True}), 200

@app.route("/api/lead-views")
def api_lead_views():
    """Return the current user's seen map + baseline. On first ever call we stamp a
    baseline of 'now' so pre-existing leads don't all light up — only activity after
    this moment (that the user hasn't opened) highlights."""
    user = session.get("user_email")
    if not user:
        return jsonify({"baseline": None, "seen": {}}), 401
    base_id = user + "|__baseline__"
    base = lead_views_col.find_one({"_id": base_id})
    if not base:
        _now = datetime.now(timezone.utc)
        lead_views_col.update_one({"_id": base_id},
            {"$setOnInsert": {"user_email": user, "seen_at": _now}}, upsert=True)
        base = {"seen_at": _now}
    seen = {}
    for v in lead_views_col.find({"user_email": user}, {"lead_id": 1, "seen_at": 1}):
        lid = v.get("lead_id")
        if lid and v.get("seen_at"):
            seen[lid] = v["seen_at"].isoformat() if hasattr(v["seen_at"], "isoformat") else v["seen_at"]
    b = base.get("seen_at")
    return jsonify({"baseline": b.isoformat() if hasattr(b, "isoformat") else b, "seen": seen}), 200

@app.route("/api/lead/<lead_id>/whatsapp-live")
def api_lead_whatsapp_live(lead_id):
    """Read the WhatsApp conversation LIVE from Zoho_Database.whatsapp_messages.

    The detail page calls this instead of using the cached lead_profiles copy,
    so the conversation is always current (inbound replies show instantly) and
    can never appear wiped by a recompute. Matched by the last 10 phone digits.
    """
    try:
        prof = leads_col.find_one({"lead_id": lead_id}, {"lead_phone_number": 1})
        raw_phone = (prof or {}).get("lead_phone_number", "")
        digits = re.sub(r"\D", "", str(raw_phone or ""))
        last10 = digits[-10:] if len(digits) >= 10 else None
        if not last10:
            return jsonify({"user_conversation_depth": 0, "user_conversation_history": []})

        wa = client["Zoho_Database"]["whatsapp_messages"].find_one(
            {"_id": {"$regex": re.escape(last10) + "$"}})
        msgs = []
        if wa:
            for _dk, dv in (wa.get("by_date") or {}).items():
                if not isinstance(dv, dict):
                    continue
                for direction in ("inbound", "outbound"):
                    for m in dv.get(direction, []):
                        if not isinstance(m, dict):
                            continue
                        _msg = {"message": m.get("msg", ""), "type": direction,
                                "date": m.get("time")}
                        _msg["fp"] = _wa_fp(last10, _msg)  # stable id for hide/unhide
                        msgs.append(_msg)

        # Performance role: drop messages an admin has hidden (depth/outbound are
        # recounted below on the filtered list, so they stay consistent).
        if session.get("role") == "performance":
            _hc, _hw = _load_hidden_sets()
            msgs = [m for m in msgs if m.get("fp") not in _hw]

        def _ts(v):
            # ALWAYS return a timezone-AWARE datetime so naive+aware never mix in sort().
            try:
                d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)
        msgs.sort(key=lambda x: _ts(x.get("date")))
        depth = len([m for m in msgs if m.get("type") == "inbound"])
        outbound = len([m for m in msgs if m.get("type") == "outbound"])
        return jsonify({"user_conversation_depth": depth, "outbound_count": outbound, "user_conversation_history": msgs})
    except Exception:
        _logging.getLogger(__name__).exception("whatsapp-live failed for %s", lead_id)
        # Graceful: return empty so the tab shows "no activity" instead of 500.
        return jsonify({"user_conversation_depth": 0, "user_conversation_history": []}), 200

@app.route("/api/lead/<lead_id>/flag", methods=["POST"])
def api_flag_lead(lead_id):
    if session.get("role") != "admin":
        return jsonify({"error": "Admin only"}), 403
    data  = request.get_json(force=True) or {}
    flag  = data.get("flag")  # "genuine", "spam", "test", "unclear", or None
    reason = data.get("reason")  # legacy; unused now
    if flag not in ("genuine", "spam", "test", "unclear", None):
        return jsonify({"error": "Invalid flag value"}), 400
    if flag:
        leads_col.update_one({"lead_id": lead_id}, {"$set": {"flag": flag}})
    else:
        leads_col.update_one({"lead_id": lead_id}, {"$unset": {"flag": ""}})

    # ── Link review flag ↔ CRM lifecycle (kept consistent both ways) ──────────
    #  • Spam            → CLOSE the lead (stage Not Interested, reason "Spam", bot off)
    #  • Genuine/Test/Unclear → REOPEN the lead IF it was closed (un-spam) so the
    #    flag and stage never contradict (set stage Lead Initiated, bot back on,
    #    reason cleared). No-op if the lead isn't currently closed.
    crm_result = None
    try:
        prof = leads_col.find_one({"lead_id": lead_id}, {"lead_phone_number": 1})
        phone = (prof or {}).get("lead_phone_number", "")
        if flag == "spam":
            from crm_v2 import mark_not_interested_by_phone
            if phone:
                ok, err = mark_not_interested_by_phone(phone, "Spam", disable_bot=True,
                                                       actor="dashboard-flag")
                crm_result = {"ok": ok, "error": err, "action": "closed", "reason": "Spam"}
        elif flag in ("genuine", "test", "unclear"):
            from crm_v2 import reopen_lead
            ok, err = reopen_lead(lead_id, phone, actor="dashboard-flag")
            crm_result = {"ok": ok, "error": err, "action": "reopen_if_closed"}
    except Exception as exc:
        _logging.getLogger(__name__).exception("flag→CRM link failed for %s", lead_id)
        crm_result = {"ok": False, "error": str(exc)}

    return jsonify({"ok": True, "lead_id": lead_id, "flag": flag, "crm": crm_result})

@app.route("/api/lead-count-check")
def api_lead_count_check():
    loft = client["asbl_loft"]
    # Count in both collections
    loft_count    = loft["leads"].count_documents({})
    profile_count = leads_col.count_documents({})

    # Get all lead IDs from both
    loft_ids    = set(str(d["_id"]) for d in loft["leads"].find({}, {"_id":1}))
    profile_ids = set(d["lead_id"] for d in leads_col.find({}, {"lead_id":1}) if d.get("lead_id"))

    # Find leads in loft but NOT in profiles
    missing_in_profiles = loft_ids - profile_ids

    # Get details of missing leads
    missing_details = []
    for lid in missing_in_profiles:
        try:
            lead = loft["leads"].find_one({"_id": ObjectId(lid)}, {"name":1, "phone":1, "createdAt":1})
            if lead:
                missing_details.append({
                    "lead_id":    lid,
                    "name":       lead.get("name"),
                    "phone":      lead.get("phone"),
                    "created_at": str(lead.get("createdAt"))
                })
        except Exception:
            pass

    missing_details.sort(key=lambda x: x.get("created_at",""), reverse=True)

    return jsonify({
        "loft_leads_count":    loft_count,
        "profile_leads_count": profile_count,
        "difference":          loft_count - profile_count,
        "missing_leads":       missing_details
    })

@app.route("/api/check-calls/<phone>")
def api_check_calls(phone):
    _voicebot = client["ASBLVoiceBot"]
    variants = [phone, "91"+phone, "+91"+phone, phone.lstrip("+").lstrip("91") if len(phone) > 10 else phone]
    variants = list(set(variants))
    result = {}
    transcripts, recordings = [], []
    for v in variants:
        for doc in _voicebot["call_transcripts"].find({"phone_number": v}, {"_id":1,"call_sid":1,"phone_number":1,"call_direction":1,"started_at":1}):
            transcripts.append({k: str(dv) for k, dv in doc.items()})
        for doc in _voicebot["call_recordings"].find({"phone_number": v}, {"_id":1,"call_sid":1,"phone_number":1,"call_direction":1,"call_status":1}):
            recordings.append({k: str(dv) for k, dv in doc.items()})
    result["variants_checked"] = variants
    result["transcripts_found"] = len(transcripts)
    result["recordings_found"]  = len(recordings)
    result["transcripts"] = transcripts
    result["recordings"]  = recordings
    return jsonify(result)

@app.route("/api/fix-phone-numbers")
def api_fix_phone_numbers():
    """One-time backfill: clean dirty lead_phone_number values in lead_profiles.
    Only touches analytics_db.lead_profiles — no source collections modified."""
    if session.get("role") != "admin":
        return jsonify({"error": "Admin only"}), 403
    import re
    analytics = client["analytics_db"]

    def clean_phone(raw):
        if not raw: return None
        s = re.sub(r"[\s\-\.\(\)]", "", str(raw))
        if s.startswith("+"): s = s[1:]
        if s.startswith("91") and len(s) == 12: s = s[2:]
        return s if len(s) == 10 and s.isdigit() else None

    fixed, skipped, already_clean = 0, 0, 0
    details = []

    for profile in analytics["lead_profiles"].find({}, {"lead_id":1,"lead_name":1,"lead_phone_number":1}):
        raw = str(profile.get("lead_phone_number","")).strip()
        cleaned = clean_phone(raw)

        # Already a clean 10-digit number? skip
        if raw == cleaned:
            already_clean += 1
            continue

        if cleaned:
            analytics["lead_profiles"].update_one(
                {"_id": profile["_id"]},
                {"$set": {"lead_phone_number": cleaned}}
            )
            details.append({"name": profile.get("lead_name"), "before": raw, "after": cleaned})
            fixed += 1
        else:
            # Can't clean (junk number like 000000... or 4-digit) — leave as-is
            details.append({"name": profile.get("lead_name"), "before": raw, "after": None, "status": "unfixable"})
            skipped += 1

    return jsonify({
        "status": "ok",
        "already_clean": already_clean,
        "fixed": fixed,
        "skipped_unfixable": skipped,
        "details": details
    })

@app.route("/api/fix-call-counts")
def api_fix_call_counts():
    """DEPRECATED — recomputed ai_calls from transcripts/recordings ONLY, which
    UNDERCOUNTS triggered calls (a placed call with no transcript/recording was
    invisible). The canonical recompute (cron_refresh) now counts triggers from
    call_schedules. Use /api/fix/backfill-missing instead."""
    return jsonify({"error": "deprecated — recomputed calls from outcomes only, undercounted triggers",
                    "use": "/api/fix/backfill-missing"}), 410
@app.route("/api/admin/fix-phone-formats")
def api_fix_phone_formats():
    """Re-normalise lead_phone_number for profiles where it isn't clean 10 digits.
    Fixes profiles created before normalisation was applied properly (e.g. +91 prefix stored)."""
    if session.get("role") != "admin":
        return jsonify({"error": "Admin only"}), 403

    import re as _re

    def _norm(p):
        if not p: return None
        s = _re.sub(r"[\s\-\.\(\)]", "", str(p))
        if s.startswith("+"): s = s[1:]
        if s.startswith("91") and len(s) == 12: s = s[2:]
        return s if len(s) == 10 and s.isdigit() else None

    fixed    = 0
    skipped  = 0  # already clean
    unfixable = 0  # bad data, can't normalize

    for doc in leads_col.find(
        {"lead_phone_number": {"$not": {"$regex": "^\\d{10}$"}}},
        {"lead_id": 1, "lead_phone_number": 1}
    ):
        raw = doc.get("lead_phone_number") or ""
        normed = _norm(raw)
        if normed:
            leads_col.update_one(
                {"lead_id": doc["lead_id"]},
                {"$set": {"lead_phone_number": normed}}
            )
            fixed += 1
        else:
            unfixable += 1

    return jsonify({"status": "ok", "fixed": fixed, "unfixable": unfixable, "skipped": skipped})


@app.route("/api/admin/fix-inbound-counts")
def api_fix_inbound_counts():
    """DEPRECATED — superseded by the canonical cron_refresh recompute, which
    counts calls from call_schedules (triggers) + transcripts/recordings (outcomes)."""
    return jsonify({"error": "deprecated", "use": "/api/fix/backfill-missing"}), 410


@app.route("/api/admin/repair-stage-divergence")
def api_repair_stage_divergence():
    """One-shot repair: align `stage` to `prd_stage` where they diverged.

    The dashboard displays `prd_stage` (Option A) but `set_stage` reads `stage`
    to detect a no-op.  Historic leads where the two fields drifted (e.g. stage
    "New Lead" but prd_stage "Lead Initiated") made manual changes appear to do
    nothing.  We now write both in lock-step on every change; this fixes the
    leads already in the diverged state.

    Dry-run by default (lists affected leads); pass ``?apply=1`` to write.
    Admin session required.
    """
    if session.get("role") != "admin":
        return jsonify({"error": "Admin only"}), 403
    valid_stages = {"New Lead", "Lead Initiated", "Pre Site Visit",
                    "Post Site Visit", "Not Interested"}
    leads = client["Zoho_Database"]["leads"]
    cursor = leads.find(
        {"prd_stage": {"$exists": True, "$nin": [None, ""]},
         "$expr": {"$ne": ["$prd_stage", "$stage"]}},
        {"_id": 1, "stage": 1, "prd_stage": 1})
    apply = request.args.get("apply") == "1"

    # SAFE: `stage` is null/empty/junk → fill it from the displayed `prd_stage`.
    # CONFLICT: `stage` holds a DIFFERENT valid stage → copying could reopen a
    #   closed lead or regress an advanced one.  NEVER auto-written; only listed.
    safe, conflicts, written = [], [], 0
    for d in cursor:
        prd, stg = d.get("prd_stage"), d.get("stage")
        row = {"_id": str(d["_id"]), "stage": stg, "prd_stage": prd}
        # SAFE only when `stage` is truly empty (null/""/missing). ANY non-empty
        # existing value — including "Spam" or other terminal/junk strings — is a
        # conflict and is NEVER overwritten (could reopen a closed lead).
        if stg not in (None, "") and stg != prd:
            conflicts.append(row)              # genuine disagreement — leave for review
            continue
        safe.append(row)
        if apply and prd in valid_stages:
            leads.update_one({"_id": d["_id"]}, {"$set": {"stage": prd}})
            written += 1
    return jsonify({
        "dry_run": not apply,
        "safe_count": len(safe),
        "conflict_count": len(conflicts),
        "repaired": written,
        "conflicts": conflicts,                # these need a deliberate decision
        "safe_sample": safe[:30],
        "hint": "add ?apply=1 to write the SAFE bucket (conflicts never auto-touched)"
                if not apply else f"wrote {written} safe; {len(conflicts)} conflicts left for review",
    }), 200


@app.route("/api/admin/redactions", methods=["POST"])
def api_redaction_hide():
    """Hide a call / WhatsApp message from the Performance team. Admin only.
    Body: {item_type:'call'|'whatsapp', item_id:<call_sid|wa_fp>, lead_id, phone?}"""
    if session.get("role") != "admin":
        return jsonify({"error": "Admin only"}), 403
    b = request.get_json(force=True, silent=True) or {}
    item_type = b.get("item_type")
    item_id = str(b.get("item_id") or "").strip()
    lead_id = b.get("lead_id")
    if item_type not in ("call", "whatsapp") or not item_id:
        return jsonify({"error": "item_type ('call'|'whatsapp') and item_id required"}), 400
    phone = b.get("phone")
    if not phone and lead_id:
        _p = leads_col.find_one({"lead_id": lead_id}, {"lead_phone_number": 1})
        phone = (_p or {}).get("lead_phone_number")
    redactions_col.update_one(
        {"item_type": item_type, "item_id": item_id},
        {"$set": {"active": True, "lead_id": lead_id, "phone": phone,
                  "hidden_by": session.get("user_email"), "hidden_at": datetime.now(timezone.utc)}},
        upsert=True)
    # Recompute ALL same-phone rows — the item appears on every project-row.
    _recompute_perf_for_phone(phone, lead_id)
    return jsonify({"ok": True, "hidden": True, "item_id": item_id}), 200


@app.route("/api/admin/redactions/unhide", methods=["POST"])
def api_redaction_unhide():
    """Reverse a hide. Admin only. Body: {item_type, item_id, lead_id?}"""
    if session.get("role") != "admin":
        return jsonify({"error": "Admin only"}), 403
    b = request.get_json(force=True, silent=True) or {}
    item_type = b.get("item_type")
    item_id = str(b.get("item_id") or "").strip()
    if item_type not in ("call", "whatsapp") or not item_id:
        return jsonify({"error": "item_type and item_id required"}), 400
    doc = redactions_col.find_one_and_update(
        {"item_type": item_type, "item_id": item_id},
        {"$set": {"active": False, "unhidden_by": session.get("user_email"),
                  "unhidden_at": datetime.now(timezone.utc)}})
    lead_id = b.get("lead_id") or (doc or {}).get("lead_id")
    phone = b.get("phone") or (doc or {}).get("phone")
    if not phone and lead_id:
        _p = leads_col.find_one({"lead_id": lead_id}, {"lead_phone_number": 1})
        phone = (_p or {}).get("lead_phone_number")
    _recompute_perf_for_phone(phone, lead_id)   # restore across all same-phone rows
    return jsonify({"ok": True, "hidden": False, "item_id": item_id}), 200


@app.route("/api/admin/redactions", methods=["GET"])
def api_redaction_list():
    """List currently-hidden item ids (for the admin UI to mark them). Admin only.
    Optional ?lead_id= to scope. Returns {calls:[...], whatsapp:[...]}."""
    if session.get("role") != "admin":
        return jsonify({"error": "Admin only"}), 403
    q = {"active": True}
    lead_id = request.args.get("lead_id")
    if lead_id:
        q["lead_id"] = lead_id
    calls, wa = [], []
    for r in redactions_col.find(q, {"item_type": 1, "item_id": 1, "lead_id": 1, "hidden_by": 1}):
        (calls if r.get("item_type") == "call" else wa).append(r.get("item_id"))
    return jsonify({"calls": calls, "whatsapp": wa}), 200


@app.route("/api/admin/backfill-last-action")
def api_backfill_last_action():
    """One-shot: compute last_activity_at + last_activity_type for EVERY existing
    profile from the call/WhatsApp data already stored on it (no re-profiling).
    Populates the "Last action" column for older leads not rebuilt since the
    feature shipped. Only sets those two display fields. Admin only."""
    if session.get("role") != "admin":
        return jsonify({"error": "Admin only"}), 403

    def _adt(v):
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        if isinstance(v, str) and v.strip():
            try:
                d = datetime.fromisoformat(v.replace("Z", "+00:00"))
                return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
            except Exception:
                return None
        return None

    scanned = written = 0
    for d in leads_col.find(
        {}, {"lead_id": 1, "ai_calls.call_conversations": 1,
             "whatsapp.user_conversation_history": 1}):
        scanned += 1
        last_at, last_type = None, None
        for c in ((d.get("ai_calls") or {}).get("call_conversations") or []):
            t = _adt(c.get("started_at"))
            if t and (last_at is None or t > last_at):
                last_at, last_type = t, "call"
        for m in ((d.get("whatsapp") or {}).get("user_conversation_history") or []):
            t = _adt(m.get("date"))
            if t and (last_at is None or t > last_at):
                last_at, last_type = t, "whatsapp"
        if last_at and d.get("lead_id"):
            leads_col.update_one({"lead_id": d["lead_id"]},
                                 {"$set": {"last_activity_at": last_at,
                                           "last_activity_type": last_type}})
            written += 1
    return jsonify({"scanned": scanned, "populated": written}), 200


@app.route("/api/admin/repair-missing-fields")
def api_repair_missing_fields():
    """Fill EMPTY fields on a profile:
      • name / source / campaign — copied from a same-phone SIBLING record
      • project — derived from the profile's OWN id (website → LOFT; ZOHO → the
        PLID suffix, or the record's own project field if the suffix is UNKNOWN)

    Never overwrites a field that already has a value. `project` is derived from
    the lead's own identity — never copied from a sibling. Dry-run by default;
    ?apply=1 writes. Admin only.
    """
    if session.get("role") != "admin":
        return jsonify({"error": "Admin only"}), 403
    zoho = client["Zoho_Database"]["leads"]
    KNOWN_PROJECTS = {"LOFT", "SPECTRA", "BROADWAY", "LANDMARK", "LEGACY"}

    def _l10(p):
        d = re.sub(r"\D", "", str(p or ""))
        return d[-10:] if len(d) >= 10 else ""

    def _project_for(lead_id):
        """Project from the lead's OWN identity (never a sibling)."""
        lid = str(lead_id or "")
        if not lid.startswith("ZOHO-"):
            return "LOFT"  # website (asbl_loft) leads are the LOFT project by definition
        suf = lid.split("-")[-1].upper()
        if suf in KNOWN_PROJECTS:
            return suf
        # PLID suffix is UNKNOWN → fall back to the record's own project field
        z = zoho.find_one({"_id": lid[5:]}, {"project": 1})
        pj = ((z or {}).get("project") or "").strip().upper()
        return pj if pj in KNOWN_PROJECTS else None

    apply = request.args.get("apply") == "1"
    # Candidates: profiles missing a source, a name, OR a project.
    cands = list(leads_col.find(
        {"$or": [{"lead_source": {"$in": [None, ""]}}, {"lead_source": {"$exists": False}},
                 {"lead_name": {"$in": [None, ""]}}, {"lead_name": {"$exists": False}},
                 {"project": {"$in": [None, ""]}}, {"project": {"$exists": False}}]},
        {"lead_id": 1, "lead_name": 1, "lead_source": 1, "campaign_name": 1,
         "lead_phone_number": 1, "project": 1}))
    CAP = 500
    filled, no_donor, changes = 0, 0, []
    for d in cands[:CAP]:
        lid = str(d.get("lead_id", ""))
        upd = {}

        # 1. project — from the lead's OWN id (no donor needed, never a sibling).
        if not (d.get("project") or "").strip():
            pj = _project_for(lid)
            if pj:
                upd["project"] = pj

        # 2. name / source / campaign — from a same-phone SIBLING that has them.
        need_donor = (not (d.get("lead_name") or "").strip()
                      or not (d.get("lead_source") or "").strip()
                      or not (d.get("campaign_name") or "").strip())
        if need_donor:
            p10 = _l10(d.get("lead_phone_number"))
            donor = None
            if p10:
                self_zid = lid[5:] if lid.startswith("ZOHO-") else None
                for s in zoho.find({"phone": {"$regex": re.escape(p10) + "$"}},
                                   {"_id": 1, "first_name": 1, "last_name": 1, "lead_source": 1,
                                    "utm_source": 1, "campaign_name": 1, "utm_campaign": 1}):
                    if self_zid and str(s["_id"]) == self_zid:
                        continue
                    if (s.get("utm_source") or s.get("lead_source") or "").strip():
                        donor = s
                        break
            if donor:
                if not (d.get("lead_name") or "").strip():
                    nm = f"{donor.get('first_name','') or ''} {donor.get('last_name','') or ''}".strip().strip(".").strip()
                    if nm:
                        upd["lead_name"] = nm
                if not (d.get("lead_source") or "").strip():
                    upd["lead_source"] = donor.get("utm_source") or donor.get("lead_source")
                if not (d.get("campaign_name") or "").strip():
                    c = donor.get("utm_campaign") or donor.get("campaign_name")
                    if c:
                        upd["campaign_name"] = c
            elif not upd:
                # no donor AND nothing to fill (not even project) → genuinely blank
                no_donor += 1
                continue

        if not upd:
            continue
        changes.append({"lead_id": d.get("lead_id"), "current_project": d.get("project"),
                        "fills": upd})
        if apply:
            leads_col.update_one({"lead_id": d["lead_id"]}, {"$set": upd})
            filled += 1
    return jsonify({
        "dry_run": not apply,
        "candidates_missing_fields": len(cands),
        "would_fill" if not apply else "filled": len(changes) if not apply else filled,
        "no_donor_found": no_donor,
        "sample": changes[:50],
        "hint": "add ?apply=1 to write (fills empty name/source/campaign from a "
                "sibling + project from the lead's own id; never overwrites)"
                if not apply else "done",
    }), 200


@app.route("/api/admin/inspect-phone")
def api_inspect_phone():
    """READ-ONLY deep dump for ONE phone across all three collections, plus a scan
    of every field for a token (default 'inncircles') so we can see EXACTLY where a
    source string lives and why it isn't surfacing. Admin only. Writes nothing.

    Usage: /api/admin/inspect-phone?phone=9390334081[&token=inncircles]
    """
    if session.get("role") != "admin":
        return jsonify({"error": "Admin only"}), 403
    p10 = re.sub(r"\D", "", request.args.get("phone", ""))[-10:]
    if len(p10) < 10:
        return jsonify({"error": "give ?phone=<10+ digits>"}), 400
    token = (request.args.get("token") or "inncircles").strip().lower()
    rx = {"$regex": re.escape(p10) + "$"}

    def scan(doc):
        """Return every field path whose string value contains the token."""
        hits = []
        def walk(o, path=""):
            if isinstance(o, dict):
                for k, v in o.items():
                    walk(v, f"{path}.{k}" if path else k)
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    walk(v, f"{path}[{i}]")
            elif isinstance(o, str) and token in o.lower():
                hits.append({"field": path, "value": o})
        walk(doc)
        return hits

    def _s(v):
        return str(v) if v is not None else None

    loft = client["asbl_loft"]["leads"]
    zoho = client["Zoho_Database"]["leads"]
    out = {"phone_last10": p10, "token": token, "token_hits": []}

    out["asbl_loft_leads"] = []
    for d in loft.find({"phone": rx}):
        out["asbl_loft_leads"].append({
            "_id": _s(d.get("_id")), "name": d.get("name"), "phone": d.get("phone"),
            "source": d.get("source"), "lead_source": d.get("lead_source"),
            "createdAt": _s(d.get("createdAt")),
        })
        for h in scan(d):
            out["token_hits"].append({"collection": "asbl_loft.leads", "_id": _s(d.get("_id")), **h})

    out["zoho_leads"] = []
    for d in zoho.find({"phone": rx}):
        out["zoho_leads"].append({
            "_id": _s(d.get("_id")), "phone": d.get("phone"),
            "lead_source": d.get("lead_source"), "campaign_name": d.get("campaign_name"),
            "project": d.get("project"), "utm_source": d.get("utm_source"),
            "first_name": d.get("first_name"), "last_name": d.get("last_name"),
        })
        for h in scan(d):
            out["token_hits"].append({"collection": "Zoho_Database.leads", "_id": _s(d.get("_id")), **h})

    out["lead_profiles"] = []
    for d in leads_col.find({"lead_phone_number": rx}):
        out["lead_profiles"].append({
            "lead_id": d.get("lead_id"), "lead_name": d.get("lead_name"),
            "lead_source": d.get("lead_source"), "campaign_name": d.get("campaign_name"),
            "project": d.get("project"), "all_loft_ids": d.get("all_loft_ids"),
        })
        for h in scan(d):
            out["token_hits"].append({"collection": "analytics_db.lead_profiles", "lead_id": d.get("lead_id"), **h})

    return jsonify(out), 200


@app.route("/api/admin/audit-blank-source")
def api_audit_blank_source():
    """READ-ONLY diagnostic: every lead profile with a blank/missing source, plus
    where its source COULD have come from — so we can tell a real bug (source
    exists but wasn't surfaced) from a genuinely source-less lead.

    For each blank-source profile it reports:
      • loft_lead_source      — source on the underlying asbl_loft website lead
      • zoho_same_phone_source — lead_source on any same-phone Zoho/relay record
      • siblings_with_source   — same-phone profiles that DO have a source
      • diagnosis              — FIXABLE (source exists elsewhere) vs GENUINELY_BLANK
    Admin only. Writes nothing.
    """
    if session.get("role") != "admin":
        return jsonify({"error": "Admin only"}), 403
    loft_leads = client["asbl_loft"]["leads"]
    zoho_leads = client["Zoho_Database"]["leads"]

    def _l10(p):
        d = re.sub(r"\D", "", str(p or ""))
        return d[-10:] if len(d) >= 10 else ""

    blanks = list(leads_col.find(
        {"$or": [{"lead_source": {"$in": [None, ""]}}, {"lead_source": {"$exists": False}}]},
        {"lead_id": 1, "lead_name": 1, "lead_phone_number": 1, "project": 1,
         "all_loft_ids": 1, "lead_created_at": 1},
    ))
    CAP = 200
    capped = len(blanks) > CAP
    cases, fixable, genuinely_blank = [], 0, 0
    for d in blanks[:CAP]:
        p10 = _l10(d.get("lead_phone_number"))
        entry = {
            "lead_id": d.get("lead_id"), "name": d.get("lead_name"),
            "phone": d.get("lead_phone_number"), "project": d.get("project"),
            "all_loft_ids": d.get("all_loft_ids", []),
        }
        loft_src = None
        zoho_src = None
        zoho_ids = []
        sibs = []
        if p10:
            rx = {"$regex": re.escape(p10) + "$"}
            for ll in loft_leads.find({"phone": rx}, {"source": 1}):
                s = ll.get("source")
                if isinstance(s, dict):
                    v = s.get("utmSource") or s.get("source") or s.get("channel")
                    if v:
                        loft_src = v
                        break
            for zz in zoho_leads.find({"phone": rx}, {"_id": 1, "lead_source": 1}):
                zoho_ids.append(str(zz["_id"]))
                if zz.get("lead_source") and not zoho_src:
                    zoho_src = zz.get("lead_source")
            for s in leads_col.find(
                {"lead_phone_number": rx, "lead_source": {"$nin": [None, ""]}},
                {"lead_id": 1, "lead_source": 1, "project": 1}):
                sibs.append({"lead_id": s.get("lead_id"), "source": s.get("lead_source"),
                             "project": s.get("project")})
        entry["loft_lead_source"] = loft_src
        entry["zoho_same_phone_ids"] = zoho_ids
        entry["zoho_same_phone_source"] = zoho_src
        entry["siblings_with_source"] = sibs
        if loft_src or zoho_src or sibs:
            entry["diagnosis"] = "FIXABLE — source exists elsewhere, not surfaced"
            fixable += 1
        else:
            entry["diagnosis"] = "GENUINELY_BLANK — no source anywhere for this phone"
            genuinely_blank += 1
        cases.append(entry)
    return jsonify({
        "total_blank_source": len(blanks),
        "examined": len(cases),
        "capped_at": CAP if capped else None,
        "fixable": fixable,
        "genuinely_blank": genuinely_blank,
        "cases": cases,
    }), 200


@app.route("/api/admin/dedup-leads")
def api_dedup_leads():
    """DEPRECATED & DISABLED — merged by phone ONLY, which wrongly collapsed
    same-person / different-project leads (e.g. 1729-SPECTRA + 1729-LOFT) into one.

    Use /api/admin/remove-true-duplicates instead — it is project-aware and only
    removes real duplicates (same phone AND same project)."""
    return jsonify({
        "error": "This endpoint is disabled. It merged by phone only and would "
                 "collapse legitimate multi-project leads. "
                 "Use /api/admin/remove-true-duplicates (project-aware) instead.",
        "disabled": True,
    }), 410

    # ── unreachable: original phone-only merge logic kept below for reference ──
    if session.get("role") != "admin":
        return jsonify({"error": "Admin only"}), 403

    import re as _re

    def _norm_p(p):
        p = _re.sub(r"[\s\-\.\(\)]", "", str(p or "")).lstrip("+")
        from datetime import datetime, timezone
        def _normalise_phone(phone):
            if not phone: return None
            import re as _re2
            s = _re2.sub(r"[\s\-\.\(\)]", "", str(phone))
            if s.startswith("+"): s = s[1:]
            if s.startswith("91") and len(s) == 12: s = s[2:]
            return s if len(s) == 10 and s.isdigit() else None
        return _normalise_phone(p) or p

    phone_map = {}
    for doc in leads_col.find({}, {
        "lead_id": 1, "lead_name": 1, "lead_source": 1, "campaign_name": 1,
        "lead_created_at": 1, "lead_phone_number": 1,
        "submission_history": 1, "all_loft_ids": 1,
    }):
        pn = _norm_p(doc.get("lead_phone_number", ""))
        if not pn: continue
        phone_map.setdefault(pn, []).append(doc)

    merged_groups = 0
    profiles_deleted = 0

    for pn, docs in phone_map.items():
        if len(docs) < 2:
            continue

        def _sort_key(d):
            t = d.get("lead_created_at")
            if isinstance(t, datetime): return t
            if isinstance(t, str):
                try: return datetime.fromisoformat(t.replace("Z", "+00:00"))
                except: pass
            return datetime.max.replace(tzinfo=timezone.utc)

        docs.sort(key=_sort_key)
        primary   = docs[0]
        primary_id = primary["lead_id"]
        secondaries = docs[1:]

        all_ids_to_add  = []
        history_to_add  = []
        seen_hist_ids   = {e.get("lead_id") for e in (primary.get("submission_history") or [])}

        for sec in secondaries:
            sec_id = sec["lead_id"]
            for lid in (sec.get("all_loft_ids") or [sec_id]):
                if lid and lid not in all_ids_to_add and lid != primary_id:
                    all_ids_to_add.append(lid)
            for entry in (sec.get("submission_history") or []):
                if entry.get("lead_id") not in seen_hist_ids:
                    history_to_add.append(entry)
                    seen_hist_ids.add(entry.get("lead_id"))
            if not sec.get("submission_history"):
                fallback = {
                    "lead_id":      sec_id,
                    "name":         sec.get("lead_name") or "",
                    "source":       sec.get("lead_source") or "",
                    "campaign":     sec.get("campaign_name") or "",
                    "submitted_at": sec.get("lead_created_at"),
                }
                if fallback["lead_id"] not in seen_hist_ids:
                    history_to_add.append(fallback)
                    seen_hist_ids.add(fallback["lead_id"])

        update_ops = {"$set": {}}
        if all_ids_to_add:
            update_ops["$addToSet"] = {"all_loft_ids": {"$each": all_ids_to_add}}
        if history_to_add:
            update_ops.setdefault("$push", {})["submission_history"] = {"$each": history_to_add}

        # Ensure primary has all_loft_ids initialized (may be missing on old profiles)
        init_ids = list({primary_id} | set(all_ids_to_add))
        if "$addToSet" not in update_ops:
            update_ops["$addToSet"] = {}
        update_ops["$addToSet"]["all_loft_ids"] = {"$each": init_ids}

        # Ensure primary has submission_history initialized
        if not primary.get("submission_history"):
            first_entry = {
                "lead_id":      primary_id,
                "name":         primary.get("lead_name") or "",
                "source":       primary.get("lead_source") or "",
                "campaign":     primary.get("campaign_name") or "",
                "submitted_at": primary.get("lead_created_at"),
            }
            if history_to_add:
                update_ops.setdefault("$push", {})["submission_history"] = {"$each": [first_entry] + history_to_add}
            else:
                update_ops["$set"]["submission_history"] = [first_entry]

        if not update_ops.get("$set"):
            update_ops.pop("$set", None)

        if update_ops:
            leads_col.update_one({"lead_id": primary_id}, update_ops)

        secondary_ids = [s["lead_id"] for s in secondaries]
        leads_col.delete_many({"lead_id": {"$in": secondary_ids}})
        profiles_deleted += len(secondary_ids)
        merged_groups += 1

    return jsonify({
        "status": "ok",
        "phone_groups_merged": merged_groups,
        "profiles_deleted": profiles_deleted,
    })


@app.route("/api/admin/remove-true-duplicates", methods=["POST"])
def api_remove_true_duplicates():
    """Remove REAL duplicates: profiles sharing the SAME phone AND SAME project.

    Same-phone / DIFFERENT-project leads (e.g. 1729-SPECTRA vs 1729-LOFT) are
    legitimate per-project leads and are NEVER touched.

    Keep rule: the flagged profile wins (preserves the sales decision); else the
    one with the most activity; else the most recent. The kept profile absorbs
    the duplicates' all_loft_ids + submission_history + flag (if it had none) so
    the hourly recompute routes future updates into it and never recreates the
    duplicate. The duplicate profile rows are then deleted.

    ``?dry=1`` → preview only (deletes nothing).
    """
    if session.get("role") != "admin":
        return jsonify({"error": "Admin only"}), 403
    dry = request.args.get("dry") in ("1", "true", "yes")

    KNOWN = ["LOFT", "SPECTRA", "BROADWAY", "LANDMARK", "LEGACY"]

    def _last10(s):
        d = re.sub(r"\D", "", str(s or ""))
        return d[-10:] if len(d) >= 10 else None

    def _project(doc):
        lid = str(doc.get("lead_id") or "")
        s = str(doc.get("project") or "").strip().upper()
        if s in KNOWN:
            return s
        if lid.startswith("ZOHO-"):
            suf = lid.split("-")[-1].upper()
            if suf in KNOWN:
                return suf
        hay = (str(doc.get("campaign_name") or "")).upper()
        for k in KNOWN:
            if k in hay:
                return k
        # asbl_loft (website, non-ZOHO) leads are the LOFT project by definition.
        if not lid.startswith("ZOHO-"):
            return "LOFT"
        return ""  # unknown project bucket

    def _activity(d):
        w = d.get("website") or {}
        wa = d.get("whatsapp") or {}
        ac = d.get("ai_calls") or {}
        return ((w.get("messages_typed") or 0) + (w.get("cta_triggered") or 0)
                + (wa.get("user_conversation_depth") or 0)
                + (ac.get("calls_answered") or 0) + (ac.get("outbound_calls_triggered") or 0))

    # Group by (last10 phone, project)
    groups = {}
    for doc in leads_col.find({}, {
        "lead_id": 1, "lead_name": 1, "lead_source": 1, "campaign_name": 1,
        "lead_created_at": 1, "lead_phone_number": 1, "flag": 1,
        "submission_history": 1, "all_loft_ids": 1,
        "website": 1, "whatsapp": 1, "ai_calls": 1,
    }):
        l10 = _last10(doc.get("lead_phone_number"))
        if not l10:
            continue
        groups.setdefault((l10, _project(doc)), []).append(doc)

    removed = 0
    merged_groups = 0
    preview = []

    for (l10, proj), docs in groups.items():
        if len(docs) < 2:
            continue
        # Keep rule: flagged first, then most activity, then most recent computed.
        def _key(d):
            return (1 if d.get("flag") else 0, _activity(d),
                    str(d.get("lead_created_at") or ""))
        docs.sort(key=_key, reverse=True)
        primary = docs[0]
        secondaries = docs[1:]
        primary_id = primary["lead_id"]

        preview.append({
            "phone": l10, "project": proj or "(unknown)",
            "keep": primary_id, "remove": [s["lead_id"] for s in secondaries],
        })

        if dry:
            removed += len(secondaries)
            merged_groups += 1
            continue

        # Fold secondaries' identity into the primary so recompute won't recreate them.
        add_ids, add_hist = [], []
        seen = {e.get("lead_id") for e in (primary.get("submission_history") or [])}
        take_flag = None if primary.get("flag") else next((s.get("flag") for s in secondaries if s.get("flag")), None)
        for sec in secondaries:
            for lid in (sec.get("all_loft_ids") or [sec["lead_id"]]):
                if lid and lid != primary_id:
                    add_ids.append(lid)
            for e in (sec.get("submission_history") or [{
                "lead_id": sec["lead_id"], "name": sec.get("lead_name") or "",
                "source": sec.get("lead_source") or "", "campaign": sec.get("campaign_name") or "",
                "submitted_at": sec.get("lead_created_at")}]):
                if e.get("lead_id") not in seen:
                    add_hist.append(e); seen.add(e.get("lead_id"))

        ops = {"$addToSet": {"all_loft_ids": {"$each": list({primary_id, *add_ids})}}}
        if add_hist:
            ops["$push"] = {"submission_history": {"$each": add_hist}}
        if take_flag:
            ops["$set"] = {"flag": take_flag}
        leads_col.update_one({"lead_id": primary_id}, ops)
        leads_col.delete_many({"lead_id": {"$in": [s["lead_id"] for s in secondaries]}})
        removed += len(secondaries)
        merged_groups += 1

    return jsonify({
        "status": "ok", "dry_run": dry,
        "duplicate_groups": merged_groups, "rows_removed": removed,
        "preview": preview[:100],
    })

@app.route("/api/fix/backfill-missing")
def api_backfill_missing():
    """
    Force-recompute all ZOHO leads (not just never-profiled ones) plus any
    loft leads not yet in lead_profiles. Safe to call multiple times.
    """
    if session.get("role") != "admin":
        return jsonify({"error": "Admin only"}), 403
    return cron_refresh(_force_all_zoho=True)

@app.route("/api/fix/recompute-zoho")
def api_recompute_zoho():
    """Alias for backfill-missing — recomputes all ZOHO leads."""
    if session.get("role") != "admin":
        return jsonify({"error": "Admin only"}), 403
    return cron_refresh(_force_all_zoho=True)


@app.route("/api/check-visitor-ids")
def api_check_visitor_ids():
    loft = client["asbl_loft"]
    lead_ids = [
        "69e8bcd8480d480f025a325e","69e8bda6633b0e612daad3b2","69e8eb51f525011080147f37",
        "69e9bc4203605a331f46bef5","69e9be5903605a331f46bf12","69e9be968ffe1df7c6c4315c",
        "69e9c5b78ffe1df7c6c431a6","69e9e675a0e7aaa3cbf2bffc","69e9e892ad3103741ef38f7b",
        "69e9ee239b0e902e4831f39a","69e9f7035bc4c6bd8202e26f","69e9ffc2d9436c63a20d47a3",
        "69ea25628f739cb78a63d6af","69ea2ec51ada962ebb499669","69eb507e9e80ac9c586ad4d8",
        "69eb8f591c0b1ff954c2acca","69ec264ee363bb018ff2a58b","69ec2db19b982aade086bf54",
        "69ec44dfc078d520d43426d5","69ec57f6a57914cf33c05897","69ec63aff872b221fd1b8209",
        "69ec692c70ee487af89d68b6","69ec780d8c1809faa6b9ae7d","69ed186bc5bd205bcc68e215",
        "69ed321cac03217e70b2db8e","69ed488e5e74a7037fd49acc","69ed4f5e2745407161666cf2",
        "69ed528e2745407161666e45","69eec18aa5945ed6d371a3b7","69eec8b761162a4cfbb931b0",
        "69eecb9ef79172aa659b73de","69f01a2ee060911f5d92a141","69f05d6206b175700b1c0917",
        "69f0748fafd29c4de8040575","69f074fd1a34a6d74b646d2d","69f079561a34a6d74b646e19",
        "69f07ba254bf1fcab1562bc1","69f0813f54bf1fcab1562eab","69f0890d6fabfad151e43922",
        "69f08ee44dcd78b9d7714a15","69f0a1dc50fccd6903d78392","69f0aa5b9c4078dcc128317e",
        "69f0ac2ef5dbd13e94b75d69","69f0b01bbe33272f832e454c","69f15759ec57dcff6bc03a7c",
        "69f15fd0599eb40576580aab","69f186249693b63d94caeab5","69f18d895f32e463cd526a88",
        "69f1998016919939fad4627d","69f19dbc06f4b0560a0c53ed"
    ]
    results = []
    for lid in lead_ids:
        try:
            lead = loft["leads"].find_one({"_id": ObjectId(lid)}, {"name":1, "phone":1, "visitorId":1})
            if lead:
                results.append({
                    "lead_id": lid,
                    "name": lead.get("name"),
                    "phone": lead.get("phone"),
                    "visitorId_in_leads": lead.get("visitorId"),
                })
        except Exception:
            pass
    has_visitor = [r for r in results if r.get("visitorId_in_leads")]
    no_visitor  = [r for r in results if not r.get("visitorId_in_leads")]
    return jsonify({
        "total_checked": len(results),
        "has_visitorId": len(has_visitor),
        "no_visitorId":  len(no_visitor),
        "with_visitor":  has_visitor,
        "without_visitor": no_visitor[:5],  # sample
        "leads_schema_fields": sorted(list(loft["leads"].find_one({}).keys())) if loft["leads"].find_one({}) else []
    })

@app.route("/api/trace/<lead_id>")
def api_trace(lead_id):
    loft = client["asbl_loft"]
    result = {}
    try:
        # 1. visitorLog by leadId
        vlog = loft["visitorLogs"].find_one({"leadId": ObjectId(lead_id)})
        result["vlog_by_leadId"] = {k: str(v) for k,v in vlog.items()} if vlog else None
        visitor_id = vlog.get("visitorId") if vlog else None

        # 2. conversations by leadId
        convs = list(loft["conversations"].find({"leadId": ObjectId(lead_id)}, {"_id":1,"visitorId":1}))
        result["convs_by_leadId"] = [{"_id": str(c["_id"]), "visitorId": c.get("visitorId")} for c in convs]

        # 3. visitorId from convs fallback
        conv_visitor_id = None
        for c in convs:
            if c.get("visitorId"):
                conv_visitor_id = c["visitorId"]
                break
        result["visitor_id_from_vlog"] = visitor_id
        result["visitor_id_from_conv"] = conv_visitor_id

        # 4. events count for each visitorId
        for vid_label, vid in [("vlog", visitor_id), ("conv", conv_visitor_id)]:
            if vid:
                cnt = loft["events"].count_documents({"visitorId": vid})
                sample = list(loft["events"].find({"visitorId": vid}).limit(2))
                result[f"events_count_{vid_label}"] = cnt
                result[f"events_sample_{vid_label}"] = [{k: str(v)[:80] for k,v in e.items()} for e in sample]
            else:
                result[f"events_count_{vid_label}"] = "N/A (no visitorId)"

        # 5. events by leadId
        cnt2 = loft["events"].count_documents({"leadId": ObjectId(lead_id)})
        result["events_count_by_leadId"] = cnt2

    except Exception as ex:
        result["error"] = str(ex)
    return jsonify(result)

@app.route("/api/recompute")
def api_recompute():
    if session.get("role") != "admin":
        return jsonify({"error": "Admin only"}), 403
    updated = 0
    skipped = 0
    errors  = 0
    loft = client["asbl_loft"]

    batch = int(request.args.get("batch", 50))

    # Only fetch leads that have interactions and time is still 0/missing
    query = {
        "$and": [
            {"$or": [{"website.cta_triggered": {"$gt": 0}}, {"website.messages_typed": {"$gt": 0}}]},
            {"$or": [{"website.website_time_spent_secs": {"$exists": False}}, {"website.website_time_spent_secs": 0.0}]},
        ]
    }
    for profile in leads_col.find(query, {"lead_id":1, "website.cta_triggered":1, "website.messages_typed":1}).limit(batch):
        lead_id_str = profile.get("lead_id")
        if not lead_id_str:
            skipped += 1
            continue

        try:
            # Collect all visitorIds for this lead:
            # 1. From visitorLogs
            visitor_ids = set()
            vlog = loft["visitorLogs"].find_one({"leadId": ObjectId(lead_id_str)}, {"visitorId":1})
            if vlog and vlog.get("visitorId"):
                visitor_ids.add(vlog["visitorId"])

            # 2. From all conversations (handles missing visitorLogs linkage)
            for c in loft["conversations"].find({"leadId": ObjectId(lead_id_str)}, {"visitorId":1}):
                if c.get("visitorId"):
                    visitor_ids.add(c["visitorId"])

            # Sum time across all events for all visitorIds
            time_secs = 0.0
            seen_evt  = set()

            event_queries = [{"visitorId": vid} for vid in visitor_ids]
            event_queries.append({"leadId": ObjectId(lead_id_str)})  # fallback by leadId

            for eq in event_queries:
                for evt in loft["events"].find(eq, {"_id":1, "startedAt":1, "endedAt":1}):
                    eid = str(evt["_id"])
                    if eid in seen_evt:
                        continue
                    seen_evt.add(eid)
                    s, e = evt.get("startedAt"), evt.get("endedAt")
                    if isinstance(s, datetime) and isinstance(e, datetime) and e > s:
                        time_secs += (e - s).total_seconds()

            # Fallback: if no events found, derive time from conversation message timestamps
            # (first message to last message = minimum time spent on site)
            if time_secs == 0.0:
                all_timestamps = []
                for c in loft["conversations"].find({"leadId": ObjectId(lead_id_str)}, {"conversationDepth":1}):
                    for turn in (c.get("conversationDepth") or []):
                        for ts_field in ("userAt", "botAt", "createdAt"):
                            ts = turn.get(ts_field)
                            if isinstance(ts, datetime):
                                all_timestamps.append(ts)
                if len(all_timestamps) >= 2:
                    all_timestamps.sort()
                    time_secs = (all_timestamps[-1] - all_timestamps[0]).total_seconds()

            leads_col.update_one(
                {"lead_id": lead_id_str},
                {"$set": {"website.website_time_spent_secs": round(time_secs, 2), "website.time_recomputed": True}}
            )
            updated += 1

        except Exception as ex:
            errors += 1

    return jsonify({"updated": updated, "skipped": skipped, "errors": errors})

# ── Excel Export Route ────────────────────────────────────────────────────────
@app.route("/api/export", methods=["GET", "POST"])
def api_export():
    try:
        return _api_export_impl()
    except Exception as _e:
        import traceback as _tb
        return jsonify({"error": f"{type(_e).__name__}: {_e}",
                        "trace": _tb.format_exc().splitlines()[-8:]}), 500

def _api_export_impl():
    try:
        import openpyxl
        from openpyxl.styles import Font, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        import subprocess, sys
        subprocess.check_call([sys.executable, "-m", "pip", "install", "openpyxl"])
        import openpyxl
        from openpyxl.styles import Font, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

    # If the dashboard POSTs the currently-visible lead_ids, export EXACTLY those
    # (in the same on-screen order). With no ids → export all leads (newest first).
    requested_ids = None
    if request.method == "POST":
        _body = request.get_json(silent=True) or {}
        requested_ids = [str(i) for i in (_body.get("lead_ids") or []) if i]
    if requested_ids:
        _by_id = {d.get("lead_id"): d for d in
                  leads_col.find({"lead_id": {"$in": requested_ids}})}
        docs = [_by_id[i] for i in requested_ids if i in _by_id]  # preserve screen order
    else:
        docs = list(leads_col.find({}).sort("lead_created_at", -1))

    # Performance role: export the hidden-adjusted rollups so the spreadsheet can't
    # leak calls/messages hidden from them (falls back to full when nothing hidden).
    if session.get("role") == "performance":
        for d in docs:
            if d.get("ai_calls_performance"):
                d["ai_calls"] = d["ai_calls_performance"]
            if d.get("whatsapp_performance"):
                d["whatsapp"] = d["whatsapp_performance"]

    wb = openpyxl.Workbook()

    thin   = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    def hcell(ws, row, col, value, width=None):
        c = ws.cell(row=row, column=col, value=value)
        c.font      = Font(bold=True, size=10, name="Calibri")
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border    = border
        if width:
            ws.column_dimensions[get_column_letter(col)].width = width
        return c

    _ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
    def dcell(ws, row, col, value, bold=False, align="left"):
        # openpyxl raises IllegalCharacterError on control chars that appear in
        # free-text (WhatsApp / call transcripts). Strip them defensively.
        if isinstance(value, str):
            value = _ILLEGAL_XML.sub("", value)
        c = ws.cell(row=row, column=col, value=value)
        c.font      = Font(bold=bold, size=10, name="Calibri")
        c.alignment = Alignment(horizontal=align, vertical="center", wrap_text=True)
        c.border    = border
        return c

    def fmt_duration(secs):
        if not secs or secs <= 0: return "—"
        total = round(secs)
        m, s = divmod(total, 60)
        return f"{m}m {s}s" if m else f"{s}s"

    def fmt_dt(val):
        if not val: return "—"
        if isinstance(val, datetime):
            return val.strftime("%d %b %Y  %H:%M")
        if isinstance(val, str):
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return dt.strftime("%d %b %Y  %H:%M")
            except Exception:
                return str(val)[:16]
        return str(val)[:16]

    _EXPORT_KNOWN_PROJECTS = {"LOFT", "SPECTRA", "BROADWAY", "LANDMARK", "LEGACY"}
    def derive_project(doc):
        """Same logic as the dashboard badge: stored project → PLID suffix → campaign keyword."""
        stored = str(doc.get("project") or "").strip().upper()
        if stored in _EXPORT_KNOWN_PROJECTS:
            return stored
        lid = str(doc.get("lead_id") or "")
        if lid.startswith("ZOHO-"):
            suf = lid.split("-")[-1].upper()
            if suf in _EXPORT_KNOWN_PROJECTS:
                return suf
        hay = (str(doc.get("project") or "") + " " + str(doc.get("campaign_name") or "")).upper()
        for k in _EXPORT_KNOWN_PROJECTS:
            if k in hay:
                return k
        return "—"

    def sort_key(v):
        """Parse any timestamp into a tz-AWARE datetime for sorting.
        Mixed naive/aware values otherwise raise TypeError on comparison."""
        d = None
        if isinstance(v, datetime):
            d = v
        elif isinstance(v, str):
            try: d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except Exception: d = None
        if d is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

    # ══════════════════════════════════════════════════════════════════════════
    # SHEET 1 — LEAD SUMMARY
    # ══════════════════════════════════════════════════════════════════════════
    ws1 = wb.active
    ws1.title = "Lead Summary"
    ws1.freeze_panes = "A2"

    # Lead ID for export: Inncircles source id, else the PLID with "ZOHO-" stripped.
    # Replaces the Phone column across all sheets (PII out, row still identifiable).
    def _export_lead_id(doc):
        src = (doc.get("lead_source") or "").lower()
        if "inncircles" in src and doc.get("source_lead_id"):
            return str(doc.get("source_lead_id"))
        lid = str(doc.get("lead_id") or doc.get("_id") or "")
        return re.sub(r"^ZOHO-", "", lid, flags=re.I) or "—"

    s1_cols = [
        ("Lead Name",          22), ("Lead ID",            14), ("Project",           13),
        ("Source",             12), ("Campaign",           26), ("Born Date",          18), ("Web Messages",      14),
        ("CTA Clicks",         12), ("CTA Artifacts",      22), ("Time on Site",      14),
        ("WA Messages",        14), ("Outbound Calls",     14), ("Calls Answered",    14),
        ("Calls Not Answered", 16), ("Pickup Ratio",       13), ("Avg Call Duration", 16),
        ("Last Updated",       18),
    ]
    for i, (label, width) in enumerate(s1_cols, 1):
        hcell(ws1, 1, i, label, width=width)
    ws1.row_dimensions[1].height = 28

    for r, doc in enumerate(docs, 2):
        w  = doc.get("website", {}) or {}
        wa = doc.get("whatsapp", {}) or {}
        ai = doc.get("ai_calls", {}) or {}
        pickup     = ai.get("pickup_ratio") or 0
        outbound_ct = ai.get("outbound_calls_triggered") or 0
        pickup_str = "N/A" if not outbound_ct else ("0%" if not pickup else f"{round(pickup * 100)}%")
        row_vals = [
            doc.get("lead_name") or "—",
            _export_lead_id(doc),
            derive_project(doc),
            doc.get("lead_source") or "—",
            doc.get("campaign_name") or "—",
            fmt_dt(doc.get("lead_created_at")),
            w.get("messages_typed") or 0,
            w.get("cta_triggered") or 0,
            ", ".join(w.get("cta_details") or []) or "—",
            fmt_duration(w.get("website_time_spent_secs")),
            wa.get("user_conversation_depth") or 0,
            ai.get("outbound_calls_triggered") or 0,
            ai.get("calls_answered") or 0,
            ai.get("calls_not_answered") or 0,
            pickup_str,
            fmt_duration(ai.get("average_call_duration_secs")),
            fmt_dt(doc.get("updated_at")),
        ]
        aligns = ["left","center","center","center","left","center","center","center","left",
                  "center","center","center","center","center","center","center","center"]
        for c, (val, align) in enumerate(zip(row_vals, aligns), 1):
            dcell(ws1, r, c, val, align=align)
        ws1.row_dimensions[r].height = 18

    # ══════════════════════════════════════════════════════════════════════════
    # SHEET 2 — WEBSITE CONVERSATIONS
    # ══════════════════════════════════════════════════════════════════════════
    ws2 = wb.create_sheet("Website Conversations")
    ws2.freeze_panes = "A2"

    ws2_cols = [("Lead Name",18),("Lead ID",16),("User Message",55),("Bot Reply",55),("User Time",18),("Bot Time",18)]
    for i,(label,width) in enumerate(ws2_cols, 1):
        hcell(ws2, 1, i, label, width=width)
    ws2.row_dimensions[1].height = 28

    r2 = 2
    for doc in docs:
        name  = doc.get("lead_name") or "—"
        lead_id_disp = _export_lead_id(doc)
        for msg in sorted((doc.get("website",{}) or {}).get("message_log",[]), key=lambda m: sort_key(m.get("user_at"))):
            dcell(ws2, r2, 1, name)
            dcell(ws2, r2, 2, lead_id_disp,                   align="center")
            dcell(ws2, r2, 3, msg.get("user_text","") or "")
            dcell(ws2, r2, 4, msg.get("bot_reply","")  or "")
            dcell(ws2, r2, 5, fmt_dt(msg.get("user_at")),     align="center")
            dcell(ws2, r2, 6, fmt_dt(msg.get("bot_at")),      align="center")
            ws2.row_dimensions[r2].height = 16
            r2 += 1

    # ══════════════════════════════════════════════════════════════════════════
    # SHEET 3 — WHATSAPP HISTORY
    # ══════════════════════════════════════════════════════════════════════════
    ws3 = wb.create_sheet("WhatsApp History")
    ws3.freeze_panes = "A2"

    ws3_cols = [("Lead Name",18),("Lead ID",16),("Type",12),("Message",65),("Timestamp",20)]
    for i,(label,width) in enumerate(ws3_cols, 1):
        hcell(ws3, 1, i, label, width=width)
    ws3.row_dimensions[1].height = 28

    r3 = 2
    for doc in docs:
        name  = doc.get("lead_name") or "—"
        lead_id_disp = _export_lead_id(doc)
        for msg in sorted((doc.get("whatsapp",{}) or {}).get("user_conversation_history",[]), key=lambda m: sort_key(m.get("date"))):
            msg_type = (msg.get("type") or "").upper()
            dcell(ws3, r3, 1, name)
            dcell(ws3, r3, 2, lead_id_disp,                  align="center")
            dcell(ws3, r3, 3, msg_type,                      align="center")
            dcell(ws3, r3, 4, msg.get("message","") or "")
            dcell(ws3, r3, 5, fmt_dt(msg.get("date")),       align="center")
            ws3.row_dimensions[r3].height = 16
            r3 += 1

    # ══════════════════════════════════════════════════════════════════════════
    # SHEET 4 — CALL TRANSCRIPTS
    # ══════════════════════════════════════════════════════════════════════════
    ws4 = wb.create_sheet("Call Transcripts")
    ws4.freeze_panes = "A2"

    ws4_cols = [("Lead Name",18),("Lead ID",16),("Call #",8),("Call Time",20),("Direction",12),("Outcome",16),("Speaker",14),("Message",65)]
    for i,(label,width) in enumerate(ws4_cols, 1):
        hcell(ws4, 1, i, label, width=width)
    ws4.row_dimensions[1].height = 28

    r4 = 2
    for doc in docs:
        name  = doc.get("lead_name") or "—"
        lead_id_disp = _export_lead_id(doc)
        for ci, call in enumerate(sorted((doc.get("ai_calls",{}) or {}).get("call_conversations",[]), key=lambda c: sort_key(c.get("started_at"))), 1):
            direction  = (call.get("call_direction") or "").upper()
            outcome    = call.get("call_outcome") or "—"
            call_time  = fmt_dt(call.get("started_at"))
            for turn in sorted(call.get("transcript", []), key=lambda t: sort_key(t.get("date"))):
                spk_type  = (turn.get("type") or "").lower()
                spk_label = (turn.get("speaker") or "Agent") if spk_type == "outbound" else "Lead"
                dcell(ws4, r4, 1, name)
                dcell(ws4, r4, 2, lead_id_disp,                   align="center")
                dcell(ws4, r4, 3, f"Call {ci}",                   align="center")
                dcell(ws4, r4, 4, call_time,                      align="center")
                dcell(ws4, r4, 5, direction,                      align="center")
                dcell(ws4, r4, 6, outcome,                        align="center")
                dcell(ws4, r4, 7, spk_label,                      align="center")
                dcell(ws4, r4, 8, turn.get("message","") or "")
                ws4.row_dimensions[r4].height = 16
                r4 += 1

    # ── Stream file ───────────────────────────────────────────────────────────
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f"ASBL_Lead_Report_{datetime.now().strftime('%d_%b_%Y')}.xlsx"
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename
    )


# ── Cron Refresh Route ────────────────────────────────────────────────────────
@app.route("/api/cron/refresh", methods=["GET", "POST"])
def cron_refresh(_force_all_zoho=False):
    """
    Called by Vercel Cron every hour.
    Only recomputes leads that have had new activity in the last 2 hours.
    Pass _force_all_zoho=True to recompute all ZOHO leads regardless of activity.
    """
    import re, traceback
    from bson import ObjectId

    # Throttle (H1): this route is public (Vercel cron + the Sync button), and each
    # run is a heavy full/partial recompute. Without a limit, anyone can hammer it
    # to burn DB + serverless cost (DoS). Allow at most one real run per window;
    # extra calls return a cheap 429. Admin force-recomputes (_force_all_zoho) skip
    # the throttle. Legit callers (daily cron, manual Sync) are far apart.
    _THROTTLE_SECONDS = 60
    if not _force_all_zoho:
        # AuthN for the PUBLIC HTTP trigger: require the shared secret. Accept it via
        # either the custom "X-Cron-Key" header (GitHub Action / manual curl) OR
        # "Authorization: Bearer <secret>" — the latter is what Vercel Cron sends
        # automatically when the CRON_SECRET env var is set. Never a query param, so
        # it can't leak via URLs/logs. Constant-time compare on both. FAIL CLOSED when
        # CRON_SECRET is unset — a misconfigured deploy must not leave this heavy,
        # data-mutating endpoint open to the internet. Internal admin force-recomputes
        # (_force_all_zoho=True) are already session-gated at their own routes and skip
        # this check (as they skip the throttle).
        _cron_secret = os.environ.get("CRON_SECRET")
        _hdr_key = str(request.headers.get("X-Cron-Key", ""))
        _auth = str(request.headers.get("Authorization", ""))
        _bearer = _auth[7:] if _auth.startswith("Bearer ") else ""
        _ok = bool(_cron_secret) and (
            hmac.compare_digest(_hdr_key, str(_cron_secret))
            or hmac.compare_digest(_bearer, str(_cron_secret)))
        if not _ok:
            return jsonify({"error": "unauthorized"}), 401
        _m = db["cron_meta"].find_one({"_id": "cron_refresh"}, {"last_started": 1})
        _ls = (_m or {}).get("last_started")
        if isinstance(_ls, datetime):
            _ls = _ls if _ls.tzinfo else _ls.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - _ls).total_seconds() < _THROTTLE_SECONDS:
                return jsonify({"status": "throttled",
                                "retry_after_seconds": _THROTTLE_SECONDS}), 429
        db["cron_meta"].update_one({"_id": "cron_refresh"},
                                   {"$set": {"last_started": datetime.now(timezone.utc)}},
                                   upsert=True)

    loft        = client["asbl_loft"]
    voicebot    = client["ASBLVoiceBot"]
    zoho        = client["Zoho_Database"]

    HTML_TAG_RE = re.compile(r"<[^>]+>")
    def strip_html(t): return HTML_TAG_RE.sub("", t).strip() if t else t

    def normalise_phone(phone):
        if not phone: return None
        import re
        s = re.sub(r"[\s\-\.\(\)]", "", str(phone))  # strip spaces, dashes, dots, brackets
        if s.startswith("+"): s = s[1:]
        if s.startswith("91") and len(s) == 12: s = s[2:]
        return s if len(s) == 10 and s.isdigit() else None

    def _parse_ts(v):
        if isinstance(v, datetime): return v
        if isinstance(v, str):
            try: return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except Exception: pass
        return datetime.min.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    from datetime import timedelta
    from bson import ObjectId as BsonObjectId

    # ── Last-run tracker: use saved timestamp as lookback so gaps are auto-covered ──
    # Cap at 24 hours to prevent overload after a long outage
    MAX_LOOKBACK = timedelta(hours=24)
    cron_meta = db["cron_meta"].find_one({"_id": "cron_refresh"})
    last_run = cron_meta.get("last_successful_run") if cron_meta else None
    if last_run and isinstance(last_run, datetime):
        # Ensure timezone-aware
        if last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=timezone.utc)
        gap = now - last_run
        lookback = last_run if gap <= MAX_LOOKBACK else now - MAX_LOOKBACK
    else:
        # First ever run — use 24-hour lookback to catch any missed calls
        lookback = now - MAX_LOOKBACK

    # ── Find phones with new call activity ────────────────────────────────────
    # FIX 1: Use _id ObjectId insertion time (not started_at string) so delayed
    # transcripts are always caught regardless of when started_at was recorded
    new_call_phones = set()
    for doc in voicebot["call_transcripts"].find(
        {"_id": {"$gte": BsonObjectId.from_datetime(lookback)}}, {"phone_number": 1}
    ):
        raw = re.sub(r"[\s\-\.\(\)]", "", str(doc.get("phone_number") or "")).lstrip("+")
        p = normalise_phone(raw) or (raw if raw else None)
        if p: new_call_phones.add(p)

    # Also pick up phones with new recordings (use _id insertion time for consistency)
    for doc in voicebot["call_recordings"].find(
        {"_id": {"$gte": BsonObjectId.from_datetime(lookback)}}, {"phone_number": 1}
    ):
        raw = re.sub(r"[\s\-\.\(\)]", "", str(doc.get("phone_number") or "")).lstrip("+")
        p = normalise_phone(raw)
        if p: new_call_phones.add(p)

    # ── Find visitor IDs with new conversations ───────────────────────────────
    new_conv_visitor_ids = set()
    for doc in loft["conversations"].find(
        {"createdAt": {"$gte": lookback}}, {"visitorId": 1, "leadId": 1}
    ):
        if doc.get("visitorId"): new_conv_visitor_ids.add(doc["visitorId"])

    # ── Find phones with new WhatsApp messages ────────────────────────────────
    new_wa_phones = set()
    today_key = now.strftime("%Y-%m-%d")
    yesterday_key = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    # Cover BOTH today's and yesterday's by_date buckets: MAX_LOOKBACK is 24h, so an
    # overnight / gap-recovery run's window spans the UTC midnight boundary. Querying
    # only today_key silently missed WhatsApp-only-active leads dated the previous day
    # (yesterday_key was computed but unused — audit A7-05). Additive: only widens the
    # active set, never shrinks it.
    for doc in zoho["whatsapp_messages"].find(
        {"$or": [{f"by_date.{today_key}": {"$exists": True}},
                 {f"by_date.{yesterday_key}": {"$exists": True}}]},
        {"_id": 1}
    ):
        p = normalise_phone(doc["_id"])
        if p: new_wa_phones.add(p)

    # ── Map visitor IDs → lead _ids ───────────────────────────────────────────
    active_lead_ids = set()
    for vid in new_conv_visitor_ids:
        vlog = loft["visitorLogs"].find_one({"visitorId": vid}, {"leadId": 1})
        if vlog and vlog.get("leadId"):
            active_lead_ids.add(str(vlog["leadId"]))

    # ── Find leads matching active phones ─────────────────────────────────────
    all_active_phones = new_call_phones | new_wa_phones
    phone_variants = []
    for p in all_active_phones:
        phone_variants += [p, "91"+p, "+91"+p, "+91 "+p]

    for lead in loft["leads"].find({"phone": {"$in": phone_variants}}, {"_id": 1}):
        active_lead_ids.add(str(lead["_id"]))

    # Also re-profile ZOHO leads that have new call/WA activity (they have no loft entry)
    for doc in leads_col.find({"lead_id": {"$regex": "^ZOHO-"}}, {"lead_id": 1, "lead_phone_number": 1}):
        raw = re.sub(r"[\s\-\.\(\)]", "", str(doc.get("lead_phone_number","") or "")).lstrip("+")
        pn = normalise_phone(raw) or raw
        if pn and pn in all_active_phones:
            active_lead_ids.add(doc["lead_id"])

    # Also include new leads created in lookback window
    for lead in loft["leads"].find({"createdAt": {"$gte": lookback}}, {"_id": 1}):
        active_lead_ids.add(str(lead["_id"]))

    # ── UNIFIED PHONE DEDUP PASS (covers all 4 population paths above) ─────────
    # Prevents secondary loft IDs from getting their own profiles, whether they
    # arrive via visitorId, phone match, lookback window, or never-profiled path.
    # ponytail: ONE pass over lead_profiles builds all three maps (existing_ids +
    # registered-loft-ids + phone→canonical). Was 3 separate full scans of the whole
    # profiles collection every run; behaviour is identical.
    existing_ids = set()
    all_registered_loft_ids = set()
    phone_to_canonical = {}
    for _doc in leads_col.find({}, {"lead_id": 1, "lead_phone_number": 1, "all_loft_ids": 1}):
        _lid = _doc.get("lead_id")
        if _lid:
            existing_ids.add(_lid)
            all_registered_loft_ids.add(_lid)
        for _sec in (_doc.get("all_loft_ids") or []):
            all_registered_loft_ids.add(_sec)
        _raw = re.sub(r"[\s\-\.\(\)]", "", str(_doc.get("lead_phone_number", "") or "")).lstrip("+")
        _pn = normalise_phone(_raw) or _raw
        if _pn and _lid:
            phone_to_canonical[_pn] = _lid
    all_loft_ids_set = set(str(l["_id"]) for l in loft["leads"].find({}, {"_id": 1}))
    never_profiled = all_loft_ids_set - existing_ids
    # ponytail: cap the never-profiled BACKLOG per run. When the full rebuild has been
    # down, this backlog is huge and re-processing all of it every run blows the Vercel
    # timeout → the run never commits → nothing refreshes (death spiral). Recent leads
    # still come via the lookback path above; the backlog drains _BACKLOG_CAP at a time
    # across runs (each processed lead leaves never_profiled next run) → self-heals.
    # ponytail: bump the cap / run the full rebuild on a box if the backlog is huge.
    _BACKLOG_CAP = 150
    if len(never_profiled) > _BACKLOG_CAP:
        never_profiled = set(list(never_profiled)[:_BACKLOG_CAP])
    active_lead_ids.update(never_profiled)

    # (all_registered_loft_ids + phone_to_canonical already built in the single pass above)
    _all_active_loft = [i for i in active_lead_ids if not i.startswith("ZOHO-")]
    if _all_active_loft:
        _active_docs = {str(l["_id"]): l for l in loft["leads"].find(
            {"_id": {"$in": [ObjectId(i) for i in _all_active_loft]}},
            {"_id": 1, "phone": 1, "name": 1, "source": 1, "createdAt": 1}
        )}
        for _lid in list(_all_active_loft):
            _d = _active_docs.get(_lid)
            if not _d: continue
            _raw = re.sub(r"[\s\-\.\(\)]", "", str(_d.get("phone", "") or "")).lstrip("+")
            _pn  = normalise_phone(_raw) or _raw
            if not _pn: continue
            if _pn in phone_to_canonical and phone_to_canonical[_pn] != _lid:
                # Same phone already has a canonical profile — merge/route to it
                _canonical = phone_to_canonical[_pn]
                if _lid not in all_registered_loft_ids:
                    # First time this loft ID is being registered — add as resubmission
                    _src = _d.get("source") or {}
                    _sub = {
                        "lead_id":      _lid,
                        "name":         _d.get("name") or "",
                        "source":       _src.get("utmSource", "") if isinstance(_src, dict) else "",
                        "campaign":     _src.get("utmCampaign", "") if isinstance(_src, dict) else "",
                        "submitted_at": _d.get("createdAt"),
                    }
                    leads_col.update_one(
                        {"lead_id": _canonical},
                        {"$addToSet": {"all_loft_ids": _lid}, "$push": {"submission_history": _sub},
                         "$max": {"last_event_at": now}}   # resubmission = new activity → highlight
                    )
                # Remove secondary from active set; ensure canonical gets refreshed
                active_lead_ids.discard(_lid)
                active_lead_ids.add(_canonical)
            else:
                # New or canonical lead — register its phone
                phone_to_canonical[_pn] = _lid

    # Coverage is keyed by (phone-last10, PROJECT) — NOT phone alone. A phone-only
    # key collapsed legitimate multi-project leads (same person inquiring about two
    # projects, e.g. 1734-BROADWAY + 1734-SPECTRA) into a single profile: once the
    # first project was profiled, the phone counted as "covered" and the second
    # project's Zoho lead was never ingested.
    KNOWN_PROJECTS = {"LOFT", "SPECTRA", "BROADWAY", "LANDMARK", "LEGACY"}
    def _l10(p):
        d = re.sub(r"\D", "", str(p or ""))
        return d[-10:] if len(d) >= 10 else None
    def _kw_project(text):
        u = str(text or "").upper()
        for k in KNOWN_PROJECTS:
            if k in u:
                return k
        return ""
    def _profile_project(doc):
        s = str(doc.get("project") or "").strip().upper()
        if s in KNOWN_PROJECTS:
            return s
        lid = str(doc.get("lead_id") or "")
        if lid.startswith("ZOHO-"):
            suf = lid.split("-")[-1].upper()
            if suf in KNOWN_PROJECTS:
                return suf
        kw = _kw_project(doc.get("campaign_name"))
        if kw:
            return kw
        # asbl_loft (website, non-ZOHO) leads are the LOFT project by definition
        # (the site is asblloft.com). Defaulting them prevents a same-phone Zoho
        # LOFT lead from being treated as a different (phone, project) → duplicate.
        return "" if lid.startswith("ZOHO-") else "LOFT"

    # (last10, project) pairs already covered by a loft lead OR an existing profile.
    covered_pp = set()
    for l in loft["leads"].find({}, {"phone": 1, "source": 1}):
        l10 = _l10(l.get("phone"))
        if l10:
            _camp = (l.get("source") or {}).get("utmCampaign") if isinstance(l.get("source"), dict) else ""
            covered_pp.add((l10, _kw_project(_camp) or "LOFT"))
    # ponytail: ONE pass over profiles builds covered_pp (profile side) AND
    # zoho_profile_meta (was 2 more full scans of lead_profiles). zoho_profile_meta
    # lets us re-pull a ZOHO lead whose source/enrichment landed AFTER first profiling
    # (birth-time race: profiled blank-source within seconds of creation, then frozen).
    ZOHO_REFRESH_WINDOW = timedelta(hours=48)
    zoho_profile_meta = {}  # lead_id -> (source_is_blank, created_at, updated_at)
    for d in leads_col.find({}, {"lead_phone_number": 1, "lead_id": 1, "project": 1,
                                 "campaign_name": 1, "lead_source": 1,
                                 "lead_created_at": 1, "updated_at": 1}):
        l10 = _l10(d.get("lead_phone_number"))
        if l10:
            covered_pp.add((l10, _profile_project(d)))
        _lid = d.get("lead_id") or ""
        if str(_lid).startswith("ZOHO-"):
            zoho_profile_meta[_lid] = (
                not (d.get("lead_source") or "").strip(),
                d.get("lead_created_at"),
                d.get("updated_at"),
            )

    # Latest activity time per phone (last-10) — calls AND WhatsApp. Lets us
    # re-pull ANY already-profiled lead whose newest call/WA is NEWER than its
    # last recompute, even outside the 48h creation window — so activity on an
    # older lead always attaches (previously it never showed until a forced
    # full recompute). Website activity is loft-keyed and handled separately by
    # the new-conversation detection above.
    latest_act_by_phone = {}
    def _bump_act(p10, t):
        if not p10 or t is None:
            return
        cur = latest_act_by_phone.get(p10)
        if cur is None or t > cur:
            latest_act_by_phone[p10] = t
    # ponytail: call_schedules has one doc PER CALL (grows unbounded) — scanning it in
    # full every run is the main cause of the serverless timeout. We only need RECENT
    # calls here (this map just detects activity NEWER than a profile's last refresh),
    # so bound by insertion time (_id) to the last 30 days. Older calls are already
    # reflected in profiles. ponytail: widen the window if very-stale profiles are missed.
    _ACT_CUTOFF = BsonObjectId.from_datetime(now - timedelta(days=30))
    for s in voicebot["call_schedules"].find({"_id": {"$gte": _ACT_CUTOFF}}, {"to": 1, "called_at": 1}):
        if s.get("called_at"):
            _bump_act(_l10(s.get("to")), _parse_ts(s.get("called_at")))
    for w in zoho["whatsapp_messages"].find({}, {"phone": 1, "last_message_at": 1}):
        if w.get("last_message_at"):
            _bump_act(_l10(w.get("phone") or w.get("_id")), _parse_ts(w.get("last_message_at")))

    for zdoc in zoho["leads"].find({}, {"_id": 1, "phone": 1}):
        raw_phone = re.sub(r"[\s\-\.\(\)]", "", str(zdoc.get("phone", "") or "")).lstrip("+")
        zoho_lead_id = f"ZOHO-{zdoc['_id']}"
        l10 = _l10(raw_phone)
        # Project is encoded in the PLID suffix: "1734-SPECTRA" → SPECTRA.
        zproj = str(zdoc["_id"]).split("-")[-1].upper()
        if zproj not in KNOWN_PROJECTS:
            zproj = ""
        # Covered ONLY if the SAME phone AND SAME project already exists — so a
        # different project for the same phone is still ingested (multi-project safe).
        pp_covered = bool(l10) and (l10, zproj) in covered_pp
        if raw_phone and not pp_covered:
            if zoho_lead_id not in existing_ids:
                active_lead_ids.add(zoho_lead_id)
        elif zoho_lead_id in existing_ids:
            # Already profiled — re-pull if forced, if its source is still blank
            # (enrichment may have arrived since), or if it is recent enough that
            # late enrichment could still land. Stable, sourced leads are skipped.
            blank_src, created, prof_updated = zoho_profile_meta.get(zoho_lead_id, (False, None, None))
            recent = False
            if isinstance(created, datetime):
                c = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
                recent = (now - c) <= ZOHO_REFRESH_WINDOW
            # NEW-ACTIVITY re-pull: a call or WhatsApp message landed after the
            # profile's last recompute. Fixes leads older than the 48h window
            # whose new call/WA activity never attached.
            new_activity = False
            la = latest_act_by_phone.get(l10)
            if la is not None and isinstance(prof_updated, datetime):
                pu = prof_updated if prof_updated.tzinfo else prof_updated.replace(tzinfo=timezone.utc)
                new_activity = la > pu
            if _force_all_zoho or blank_src or recent or new_activity:
                active_lead_ids.add(zoho_lead_id)

    if not active_lead_ids:
        return jsonify({"status": "success", "refreshed": 0, "errors": 0,
                        "checked_leads": 0, "message": "No new activity"})

    # Use the module-level buckets so the cron tally and the performance re-tally
    # can never classify a call differently.
    ANSWERED      = CALL_ANSWERED
    NOT_ANSWERED  = CALL_NOT_ANSWERED
    NOT_CONNECTED = CALL_NOT_CONNECTED

    # Load the hidden set once — used below to attach a per-lead performance rollup
    # ONLY for leads that actually have a hidden call/message (else the field is
    # omitted and reads fall back to the full data → existing flow unchanged).
    _hidden_calls, _hidden_wa = _load_hidden_sets()

    # ── BATCH PRE-FETCH everything — eliminates N+1 queries ──────────────────
    loft_ids  = [ObjectId(i) for i in active_lead_ids if not i.startswith("ZOHO-")]
    zoho_keys = [i[5:] for i in active_lead_ids if i.startswith("ZOHO-")]
    oids = loft_ids  # used by visitorLog / conversation lookups below

    # 1. All leads (loft + Zoho-only, normalised to same shape)
    leads_map = {str(l["_id"]): l for l in loft["leads"].find({"_id": {"$in": loft_ids}})}

    for zdoc in zoho["leads"].find({"_id": {"$in": zoho_keys}}):
        normed = {
            "_id":       f"ZOHO-{zdoc['_id']}",
            "name":      f"{zdoc.get('first_name','') or ''} {zdoc.get('last_name','') or ''}".strip().strip(".").strip(),
            "phone":     zdoc.get("phone", ""),
            "createdAt": _parse_ts(zdoc.get("created_at") or zdoc.get("lead_received_at") or ""),
            "source": {
                "utmSource":   zdoc.get("utm_source") or "",
                "utmCampaign": zdoc.get("utm_campaign") or zdoc.get("campaign_name") or "",
            },
            "visitorId":    None,
            "_zoho_source": zdoc.get("lead_source") or "",
            "source_lead_id": zdoc.get("source_lead_id") or "",  # Inncircles source id
            # Authoritative project from the Zoho record. The PLID suffix can be
            # "UNKNOWN" when the lead was minted before the project resolved, but
            # the `project` field is enriched afterward (same race as lead_source).
            "project":      zdoc.get("project") or "",
        }
        leads_map[f"ZOHO-{zdoc['_id']}"] = normed

    # Build secondary loft-ID → primary lead_id routing (for merged/deduped leads)
    secondary_oid_to_primary = {}
    extra_oids = []
    for _mdoc in leads_col.find(
        {"lead_id": {"$in": list(active_lead_ids)}, "all_loft_ids": {"$exists": True}},
        {"lead_id": 1, "all_loft_ids": 1}
    ):
        _primary = _mdoc.get("lead_id")
        for _sec in (_mdoc.get("all_loft_ids") or []):
            if _sec != _primary and not _sec.startswith("ZOHO-"):
                try:
                    extra_oids.append(ObjectId(_sec))
                    secondary_oid_to_primary[_sec] = _primary
                except Exception:
                    pass
    all_fetch_oids = oids + extra_oids

    # 2. All visitorLogs
    vlogs_map = {}  # lead_id_str -> visitorLog doc
    visitor_to_lead = {}  # visitorId -> lead_id_str
    for vl in loft["visitorLogs"].find({"leadId": {"$in": all_fetch_oids}}):
        lid = str(vl["leadId"])
        primary_lid = secondary_oid_to_primary.get(lid, lid)
        if primary_lid in active_lead_ids:
            vlogs_map[primary_lid] = vl
            vid = vl.get("visitorId")
            if vid: visitor_to_lead[vid] = primary_lid

    # 3. All conversations (by leadId + visitorId)
    all_visitor_ids = list(visitor_to_lead.keys())
    convs_by_lead = {i: [] for i in active_lead_ids}
    seen_conv_ids = set()
    conv_query = {"$or": [{"leadId": {"$in": all_fetch_oids}}]}
    if all_visitor_ids:
        conv_query = {"$or": [{"leadId": {"$in": all_fetch_oids}}, {"visitorId": {"$in": all_visitor_ids}}]}
    for c in loft["conversations"].find(conv_query):
        cid = str(c["_id"])
        if cid in seen_conv_ids: continue
        seen_conv_ids.add(cid)
        lid = str(c["leadId"]) if c.get("leadId") else None
        vid = c.get("visitorId")
        if lid: lid = secondary_oid_to_primary.get(lid, lid)
        target = lid if lid in convs_by_lead else (visitor_to_lead.get(vid) if vid else None)
        if target:
            convs_by_lead[target].append(c)
            if vid and target not in visitor_to_lead.values():
                visitor_to_lead[vid] = target

    # 4. All events
    events_by_lead = {i: [] for i in active_lead_ids}
    seen_evt_ids = set()
    evt_query = {"$or": [{"leadId": {"$in": all_fetch_oids}}]}
    if all_visitor_ids:
        evt_query = {"$or": [{"leadId": {"$in": all_fetch_oids}}, {"visitorId": {"$in": all_visitor_ids}}]}
    for evt in loft["events"].find(evt_query):
        eid = str(evt.get("_id",""))
        if eid in seen_evt_ids: continue
        seen_evt_ids.add(eid)
        lid = str(evt["leadId"]) if evt.get("leadId") else None
        vid = evt.get("visitorId")
        if lid: lid = secondary_oid_to_primary.get(lid, lid)
        target = lid if lid in events_by_lead else (visitor_to_lead.get(vid) if vid else None)
        if target: events_by_lead[target].append(evt)

    # 5. Collect all phone variants for batch call/WA queries
    phone_norm_map = {}  # lead_id_str -> phone_norm (10-digit Indian) or raw international
    all_phone_variants = []
    for lid, lead in leads_map.items():
        raw = re.sub(r"[\s\-\.\(\)]", "", str(lead.get("phone") or "")).lstrip("+")
        pn = normalise_phone(raw) or (raw if raw else None)  # fallback to raw for international
        phone_norm_map[lid] = pn
        if pn:
            if len(pn) == 10 and pn.isdigit():
                # Indian number — add standard variants
                all_phone_variants += [pn, "91"+pn, "+91"+pn]
            else:
                # International — use as-is and with + prefix
                all_phone_variants += [pn, "+"+pn]

    # 6. All call transcripts
    transcripts_by_lead = {i: [] for i in active_lead_ids}
    # A phone can belong to MULTIPLE project-leads (same person inquiring about two
    # projects, e.g. SPECTRA + LOFT). Map each phone variant to ALL such leads so the
    # call activity shows on every project row. Previously this was phone→single-lead,
    # so one project got the calls and the other showed 0.
    phone_to_leads = {}
    for lid, pn in phone_norm_map.items():
        if pn:
            variants = [pn, "91"+pn, "+91"+pn] if (len(pn) == 10 and pn.isdigit()) else [pn, "+"+pn]
            for v in variants:
                phone_to_leads.setdefault(v, []).append(lid)
    if all_phone_variants:
        for doc in voicebot["call_transcripts"].find({"phone_number": {"$in": all_phone_variants}}):
            pn = doc.get("phone_number","")
            for lid in phone_to_leads.get(pn, []):
                transcripts_by_lead[lid].append(doc)

    # 7. All call recordings (batch by call_sids + phones)
    all_transcript_sids = [str(d.get("call_sid") or "") for docs in transcripts_by_lead.values() for d in docs if d.get("call_sid")]
    recordings_by_lead = {i: {} for i in active_lead_ids}  # lead_id -> {call_sid: rec}
    seen_rec_ids = set()
    if all_transcript_sids:
        for rec in voicebot["call_recordings"].find({"call_sid": {"$in": all_transcript_sids}}):
            rid = str(rec["_id"])
            if rid in seen_rec_ids: continue
            seen_rec_ids.add(rid)
            sid = str(rec.get("call_sid") or "")
            pn = rec.get("phone_number","")
            for lid in phone_to_leads.get(pn, []):
                if sid: recordings_by_lead[lid][sid] = rec
    if all_phone_variants:
        for rec in voicebot["call_recordings"].find({"phone_number": {"$in": all_phone_variants}}):
            rid = str(rec["_id"])
            if rid in seen_rec_ids: continue
            seen_rec_ids.add(rid)
            pn = rec.get("phone_number","")
            sid = str(rec.get("call_sid") or "")
            for lid in phone_to_leads.get(pn, []):
                if sid: recordings_by_lead[lid][sid] = rec

    # 7b. Call SCHEDULES = the relay's AUTHORITATIVE trigger record (one doc per
    # placed call; schedule.call_id == transcript/recording call_sid). A call can be
    # triggered but produce NO transcript/recording (no-answer / failed / in-flight) —
    # counting only outcomes UNDERCOUNTS real triggers. Pull schedules so every
    # placed call is counted.
    schedules_by_lead = {i: [] for i in active_lead_ids}
    if all_phone_variants:
        for sch in voicebot["call_schedules"].find(
            {"to": {"$in": all_phone_variants}},
            {"to": 1, "call_id": 1, "status": 1, "external_schedule_id": 1,
             "created_at": 1, "scheduled_at": 1}):
            for lid in phone_to_leads.get(sch.get("to", ""), []):
                schedules_by_lead[lid].append(sch)

    # 8. All WhatsApp messages — match by LAST 10 DIGITS (format-agnostic).
    # whatsapp_messages _id is stored in mixed formats (12-digit 91XXXXXXXXXX,
    # 11-digit, 10-digit, +prefix, intl, …). Matching on exact format keys
    # missed ~94 docs and caused conversations to intermittently vanish. Keying
    # by the last 10 digits resolves every format to one stable bucket.
    def _last10(s):
        d = re.sub(r"\D", "", str(s or ""))
        return d[-10:] if len(d) >= 10 else None
    active_last10 = {x for x in (_last10(pn) for pn in phone_norm_map.values()) if x}
    wa_map = {}  # last10 -> doc
    if active_last10:
        _wa_regex = "(" + "|".join(re.escape(x) for x in active_last10) + ")$"
        for doc in zoho["whatsapp_messages"].find({"_id": {"$regex": _wa_regex}}):
            k = _last10(doc["_id"])
            if k:
                wa_map[k] = doc

    # ── Process each lead from pre-fetched data ───────────────────────────────
    refreshed = 0
    errors    = 0
    error_details = []  # capture which leads fail and why (surfaced in the response)

    _KNOWN_PROJECTS = {"LOFT", "SPECTRA", "BROADWAY", "LANDMARK", "LEGACY"}
    def _derive_project(lead_doc, lid):
        # 1. Authoritative project field (Zoho record, enriched post-mint).
        p = str(lead_doc.get("project") or "").strip().upper()
        if p in _KNOWN_PROJECTS:
            return p
        # 2. PLID suffix (ZOHO-1729-SPECTRA → SPECTRA).
        if isinstance(lid, str) and lid.startswith("ZOHO-"):
            suf = lid.split("-")[-1].upper()
            if suf in _KNOWN_PROJECTS:
                return suf
        # 3. Keyword in campaign / source.
        hay = (str(lead_doc.get("project") or "") + " " +
               str((lead_doc.get("source") or {}).get("utmCampaign") if isinstance(lead_doc.get("source"), dict) else "")).upper()
        for k in _KNOWN_PROJECTS:
            if k in hay:
                return k
        # 4. asbl_loft (website, non-ZOHO) leads default to LOFT (the site is asblloft.com).
        if not (isinstance(lid, str) and lid.startswith("ZOHO-")):
            return "LOFT"
        return ""

    for lead_id_str in active_lead_ids:
        try:
            lead = leads_map.get(lead_id_str)
            if not lead: continue

            phone_norm = phone_norm_map.get(lead_id_str)
            phone_raw  = lead.get("phone") or ""
            src        = lead.get("source") or {}
            name       = lead.get("name") or ""

            # ── website ──────────────────────────────────────────────────────
            all_convs = convs_by_lead.get(lead_id_str, [])
            cta_triggered, cta_set, message_log, cta_log = 0, set(), [], []
            for conv in all_convs:
                for turn in conv.get("conversationDepth") or []:
                    if turn.get("isCtaTrigger") is True:
                        cta_triggered += 1
                        artifact = turn.get("artifact","")
                        if artifact: cta_set.add(artifact)
                        cta_log.append({
                            "artifact":       artifact,
                            "artifact_label": turn.get("artifactLabel",""),
                            "user_text":      turn.get("userText",""),
                            "bot_reply":      strip_html(turn.get("botText","")),
                            "triggered_at":   turn.get("userAt"),
                            "bot_at":         turn.get("botAt"),
                        })
                        continue
                    ut = turn.get("userText",""); bt = strip_html(turn.get("botText",""))
                    if ut:
                        message_log.append({"user_text":ut,"bot_reply":bt,"user_at":turn.get("userAt"),"bot_at":turn.get("botAt")})
            message_log.sort(key=lambda x: x["user_at"] if isinstance(x.get("user_at"), datetime) else datetime.min)
            cta_log.sort(key=lambda x: x["triggered_at"] if isinstance(x.get("triggered_at"), datetime) else datetime.min)

            time_secs = 0.0
            # Cap each event at 30 min: a browser tab left open records an
            # endedAt hours/days after startedAt (seen: a single 72h "session"),
            # which otherwise inflates "time on site" to absurd values.
            _MAX_EVENT_SECS = 1800
            for evt in events_by_lead.get(lead_id_str, []):
                s, e = evt.get("startedAt"), evt.get("endedAt")
                if isinstance(s, datetime) and isinstance(e, datetime) and e > s:
                    time_secs += min((e - s).total_seconds(), _MAX_EVENT_SECS)

            # ── whatsapp ─────────────────────────────────────────────────────
            # Match by last-10 digits (format-agnostic — see wa_map build above).
            wa_doc = wa_map.get(_last10(phone_norm)) if phone_norm else None
            wa_msgs = []
            if wa_doc:
                for dk, dv in (wa_doc.get("by_date") or {}).items():
                    if not isinstance(dv, dict): continue
                    for direction in ("inbound","outbound"):
                        for m in dv.get(direction,[]):
                            wa_msgs.append({"message": m.get("msg",""), "type": direction, "date": m.get("time")})
            wa_msgs.sort(key=lambda x: _parse_ts(x.get("date")))
            wa_depth    = len([m for m in wa_msgs if m.get("type") == "inbound"])   # customer→us (lead messages)
            wa_outbound = len([m for m in wa_msgs if m.get("type") == "outbound"])  # us→customer (bot/agent)

            # SAFETY NET: if no WhatsApp doc matched this run but the lead already
            # has conversation history stored, PRESERVE it rather than overwriting
            # with empty — a conversation must never silently disappear.
            if not wa_msgs:
                _existing = leads_col.find_one(
                    {"lead_id": lead_id_str}, {"whatsapp": 1})
                _prev = ((_existing or {}).get("whatsapp") or {}).get("user_conversation_history") or []
                if _prev:
                    wa_msgs = _prev
                    wa_depth    = len([m for m in wa_msgs if m.get("type") == "inbound"])
                    wa_outbound = len([m for m in wa_msgs if m.get("type") == "outbound"])

            # ── calls ────────────────────────────────────────────────────────
            call_docs   = transcripts_by_lead.get(lead_id_str, [])
            rec_by_sid  = recordings_by_lead.get(lead_id_str, {})
            seen_calls  = {str(d.get("call_sid") or "") for d in call_docs if d.get("call_sid")}

            outbound_ct = answered = not_answered = not_connected = inbound_ct = 0
            durations, call_convs = [], []
            for doc in call_docs:
                direction    = doc.get("call_direction","")
                outcome      = doc.get("call_outcome","UNKNOWN") or "UNKNOWN"
                call_sid_str = str(doc.get("call_sid",""))
                if direction == "inbound":
                    inbound_ct += 1
                else:  # outbound
                    outbound_ct += 1
                    if outcome in ANSWERED:        answered += 1
                    elif outcome in NOT_ANSWERED:  not_answered += 1
                    elif outcome in NOT_CONNECTED: not_connected += 1
                raw = doc.get("transcript") or []
                turns = []
                for t in raw:
                    ts_raw = t.get("ts")
                    if isinstance(ts_raw, datetime): ts = ts_raw
                    elif isinstance(ts_raw, str):
                        try: ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                        except Exception: ts = None
                    else: ts = None
                    # The LEAD's turns carry their phone number as the speaker; the
                    # AGENT's turns carry a NAME (Anandita, Radhika, …). Attribute by
                    # letters-vs-digits — NOT a hardcoded agent name (mislabels other
                    # bots) and NOT phone length (malformed phones can be short).
                    _spk = str(t.get("speaker") or "")
                    _is_agent = any(c.isalpha() for c in _spk)
                    turns.append({"message":t.get("text",""),"type":"outbound" if _is_agent else "inbound","speaker":_spk if _is_agent else "","date":ts})
                turns.sort(key=lambda t: t["date"] if isinstance(t.get("date"), datetime) else datetime.min.replace(tzinfo=timezone.utc))
                ts_sorted = [t["date"] for t in turns if t.get("date")]
                s_at = ts_sorted[0]  if ts_sorted else None
                e_at = ts_sorted[-1] if ts_sorted else None
                # For unanswered/busy calls the transcript is empty → fall back to
                # the document-level started_at so the call card still shows a date
                if s_at is None:
                    raw_sat = doc.get("started_at")
                    if isinstance(raw_sat, datetime):
                        s_at = raw_sat if raw_sat.tzinfo else raw_sat.replace(tzinfo=timezone.utc)
                    elif isinstance(raw_sat, str):
                        try: s_at = datetime.fromisoformat(raw_sat.replace("Z", "+00:00"))
                        except Exception: pass
                dur  = (e_at-s_at).total_seconds() if s_at and e_at and len(ts_sorted)>=2 else 0.0
                rec  = rec_by_sid.get(call_sid_str, {})
                if dur == 0.0 and rec.get("recording_duration_secs"):
                    dur = float(rec["recording_duration_secs"])
                if dur > 0: durations.append(dur)
                call_convs.append({"call_sid":call_sid_str,"call_direction":direction,"call_outcome":outcome,"started_at":s_at,"ended_at":e_at,"duration_secs":round(dur,2),"transcript":turns,"recording_url":rec.get("recording_url") or None,"recording_duration_secs":rec.get("recording_duration_secs") or None})

            for sid, rec in rec_by_sid.items():
                if sid in seen_calls: continue
                rec_dir    = rec.get("call_direction","outbound")
                rec_status = (rec.get("call_status") or "").lower()
                rec_dur    = float(rec.get("recording_duration_secs") or 0)
                cr_raw = rec.get("created_at")
                if isinstance(cr_raw, datetime): r_at = cr_raw
                elif isinstance(cr_raw, str):
                    try: r_at = datetime.fromisoformat(cr_raw.replace("Z","+00:00"))
                    except Exception: r_at = None
                else: r_at = None
                if rec_dir == "inbound":
                    inbound_ct += 1
                    # A recording exists → the call connected. Inbound is counted
                    # separately (inbound_ct) and does NOT feed the outbound pickup
                    # counters or the duration average, but the call record still
                    # needs an outcome. Was previously left unassigned →
                    # UnboundLocalError at the call_convs.append below.
                    rec_outcome = "CONNECTED"
                else:  # outbound
                    outbound_ct += 1
                    if rec_status == "completed":
                        answered += 1; rec_outcome = "CONNECTED"
                        if rec_dur > 0: durations.append(rec_dur)
                    else:
                        not_answered += 1; rec_outcome = "NO_ANSWER"
                call_convs.append({"call_sid":sid,"call_direction":rec_dir,"call_outcome":rec_outcome,"started_at":r_at,"ended_at":r_at,"duration_secs":round(rec_dur,2),"transcript":[],"recording_url":rec.get("recording_url") or None,"recording_duration_secs":round(rec_dur,2) if rec_dur else None})

            # Triggered calls with NO transcript/recording (no-answer / failed / in-
            # flight) — counted from the relay's schedule record (schedule.call_id ==
            # call_sid, so dedupe against what transcripts/recordings already covered).
            # This fixes calls showing 0/0 despite being triggered.
            _counted = set(seen_calls) | set(rec_by_sid.keys())
            _SCHED_OUTCOME = {
                "completed":"CONNECTED","connected":"CONNECTED",
                "no_answer":"NO_ANSWER","no-answer":"NO_ANSWER","busy":"BUSY",
                "failed":"NOT_CONNECTED","switched_off":"SWITCHED_OFF","switchedoff":"SWITCHED_OFF",
                "called":"NOT_CONNECTED","initiated":"NOT_CONNECTED",
            }
            for sch in schedules_by_lead.get(lead_id_str, []):
                cid = str(sch.get("call_id") or "")
                status = (sch.get("status") or "").strip().lower()
                if status in ("", "pending", "cancelled"):
                    continue  # not actually placed
                if cid and cid in _counted:
                    continue  # already represented by a transcript/recording
                outbound_ct += 1
                outcome = _SCHED_OUTCOME.get(status, "NOT_CONNECTED")
                if outcome in ANSWERED:        answered += 1
                elif outcome in NOT_ANSWERED:  not_answered += 1
                elif outcome in NOT_CONNECTED: not_connected += 1
                s_at = _parse_ts(sch.get("created_at") or sch.get("scheduled_at"))
                call_convs.append({"call_sid":cid,"call_direction":"outbound","call_outcome":outcome,
                                   "started_at":s_at,"ended_at":s_at,"duration_secs":0.0,"transcript":[],
                                   "recording_url":None,"recording_duration_secs":None})
                if cid: _counted.add(cid)

            # pickup ratio = answered / TOTAL outbound triggered — kept consistent
            # with the displayed answered/triggered fraction so the % can never
            # contradict it (previously it divided by answered+not_answered, which
            # excluded not_connected and made e.g. 2/4 read as 100%).
            # Source/campaign for this profile. If a website (loft) profile has a
            # folded ZOHO record (a website↔CRM merge) and its own source is blank,
            # inherit source/campaign from that Zoho lead so the merged row is complete.
            _src_val  = (src.get("utmSource") if isinstance(src,dict) else None) or lead.get("_zoho_source") or None
            _camp_val = (src.get("utmCampaign") if isinstance(src,dict) else None)
            if not _src_val and not lead_id_str.startswith("ZOHO-"):
                _exist = leads_col.find_one({"lead_id": lead_id_str}, {"all_loft_ids": 1})
                _zid = next((i for i in (_exist or {}).get("all_loft_ids", []) if str(i).startswith("ZOHO-")), None)
                if _zid:
                    _zl = zoho["leads"].find_one({"_id": _zid[5:]}, {"lead_source": 1, "campaign_name": 1})
                    if _zl:
                        _src_val  = _src_val  or _zl.get("lead_source") or None
                        _camp_val = _camp_val or _zl.get("campaign_name") or None
            # ── Same-person sibling backfill (2026-07 fix) ─────────────────────
            # The SAME person (same phone / same MLID) can be born into multiple
            # projects. A sibling project-lead (e.g. 1762-LOFT) may arrive with
            # name/source/utm EMPTY while another (1762-BROADWAY) is fully filled.
            # Copy ONLY the empty descriptive fields from a filled same-phone
            # sibling — NEVER the project (each project-lead keeps its own, so we
            # don't collapse two projects into one). Source is derived the same way
            # the display does: prefer the sibling's utm_source (→ "inncircles").
            if (not (name or "").strip()) or (not _src_val) or (not _camp_val):
                _l = _l10(phone_norm) if phone_norm else _l10(phone_raw)
                _self_zid = lead_id_str[5:] if lead_id_str.startswith("ZOHO-") else None
                if _l:
                    for _sib in zoho["leads"].find(
                        {"phone": {"$regex": re.escape(_l) + "$"}},
                        {"_id": 1, "first_name": 1, "last_name": 1, "lead_source": 1,
                         "utm_source": 1, "campaign_name": 1, "utm_campaign": 1}):
                        if _self_zid and str(_sib["_id"]) == _self_zid:
                            continue  # skip the record itself
                        _sib_src = _sib.get("utm_source") or _sib.get("lead_source")
                        if not (_sib_src or "").strip():
                            continue  # a donor must actually have a source
                        if not (name or "").strip():
                            _nm = f"{_sib.get('first_name','') or ''} {_sib.get('last_name','') or ''}".strip().strip(".").strip()
                            if _nm:
                                name = _nm
                        if not _src_val:
                            _src_val = _sib_src
                        if not _camp_val:
                            _camp_val = _sib.get("utm_campaign") or _sib.get("campaign_name") or _camp_val
                        break  # same person → first filled sibling is enough

            # ── Last activity: most recent CALL or WhatsApp message (newest wins) —
            # feeds the homepage "Last action" column (date/time + type). Normalise
            # every timestamp to aware-UTC so naive/aware never mix in comparison.
            def _adt(v):
                if isinstance(v, datetime):
                    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
                if isinstance(v, str) and v.strip():
                    try:
                        _d = datetime.fromisoformat(v.replace("Z", "+00:00"))
                        return _d if _d.tzinfo else _d.replace(tzinfo=timezone.utc)
                    except Exception:
                        return None
                return None
            _last_at, _last_type = None, None
            for _c in call_convs:
                _t = _adt(_c.get("started_at"))
                if _t and (_last_at is None or _t > _last_at):
                    _last_at, _last_type = _t, "call"
            for _m in wa_msgs:
                _t = _adt(_m.get("date"))
                if _t and (_last_at is None or _t > _last_at):
                    _last_at, _last_type = _t, "whatsapp"

            profile = {
                "lead_id": lead_id_str, "lead_name": name,
                "source_lead_id": lead.get("source_lead_id") or "",  # Inncircles source id
                "lead_phone_number": phone_norm if phone_norm else phone_raw,
                "lead_source": _src_val,
                "campaign_name": _camp_val,
                "project": _derive_project(lead, lead_id_str),
                "lead_created_at": lead.get("createdAt"),
                "last_activity_at": _last_at,
                "last_activity_type": _last_type,
                "website": {"cta_triggered":cta_triggered,"cta_details":list(cta_set),"cta_log":cta_log,"messages_typed":len(message_log),"message_log":message_log,"website_time_spent_secs":round(time_secs,2)},
                "whatsapp": {"user_conversation_depth":wa_depth,"outbound_count":wa_outbound,"user_conversation_history":wa_msgs},
                "ai_calls": {"outbound_calls_triggered":outbound_ct,"calls_answered":answered,"calls_not_answered":not_answered,"calls_not_connected":not_connected,"inbound_calls":inbound_ct,"pickup_ratio":round(answered/(answered+not_answered),2) if (answered+not_answered) else None,"average_call_duration_secs":round(sum(durations)/len(durations),2) if durations else 0.0,"call_conversations":call_convs},
                "updated_at": now,
            }
            _first_src = profile.get("lead_source") or ""
            _first_camp = profile.get("campaign_name") or ""
            _src_entry = {
                "lead_id":      lead_id_str,
                "name":         name,
                "source":       _first_src,
                "campaign":     _first_camp,
                "submitted_at": lead.get("createdAt"),
            }
            # Performance rollup — attach ONLY when this lead has a hidden call/msg;
            # otherwise clear any stale copy so the read falls back to the full data.
            _perf_unset = None
            _phone_perf = profile["lead_phone_number"]
            if _lead_has_hidden(profile, _phone_perf, _hidden_calls, _hidden_wa):
                _pai, _pwa = _perf_rollup(profile["ai_calls"], profile["whatsapp"],
                                          _phone_perf, _hidden_calls, _hidden_wa)
                profile["ai_calls_performance"] = _pai
                profile["whatsapp_performance"] = _pwa
            else:
                _perf_unset = {"ai_calls_performance": "", "whatsapp_performance": ""}
            _update_ops = {
                "$set": profile,
                "$setOnInsert": {
                    "computed_at":        now,
                    "all_loft_ids":       [lead_id_str],
                    "submission_history": [_src_entry],
                },
            }
            if _perf_unset:
                _update_ops["$unset"] = _perf_unset
            # last_event_at drives the "new activity" row highlight. $max keeps it
            # MONOTONIC so external bumps (a manual stage change, a resubmission)
            # are never lowered by a later recompute of the call/WhatsApp time.
            if _last_at:
                _update_ops["$max"] = {"last_event_at": _last_at}
            leads_col.update_one({"lead_id": lead_id_str}, _update_ops, upsert=True)
            refreshed += 1
        except Exception as _e:
            errors += 1
            error_details.append({"lead_id": lead_id_str, "error": f"{type(_e).__name__}: {_e}"})

    # ── Run status — a partial refresh must NOT look fully healthy (PARTIAL-SUCCESS-01) ──
    #   success: no per-lead errors
    #   partial: some leads saved AND some failed  (successful work is still kept)
    #   failed : work was attempted but nothing saved
    _checked = len(active_lead_ids)
    if errors == 0:
        run_status = "success"
    elif refreshed > 0:
        run_status = "partial"
    else:
        run_status = "failed"

    # last_successful_run advances ONLY on full success (UNCHANGED) — else failed
    # leads would fall before the next lookback and be skipped permanently. Record
    # run metadata on EVERY completion so partial/failed runs are visible (via
    # /api/stats + worker health) without over-alarming the staleness banner.
    _meta = {"last_completed_run": now, "last_status": run_status,
             "last_errors": errors, "last_checked": _checked, "last_refreshed": refreshed}
    if errors == 0:
        _meta["last_successful_run"] = now
    db["cron_meta"].update_one({"_id": "cron_refresh"}, {"$set": _meta}, upsert=True)

    return jsonify({"status": run_status, "refreshed": refreshed, "errors": errors,
                    "checked_leads": _checked, "error_details": error_details[:20]})


# ── QA API Routes ─────────────────────────────────────────────────────────────

@app.route("/api/qa/overview")
def api_qa_overview():
    if qa_db is None:
        return jsonify({"error": "QA DB not configured"}), 503

    cb_col    = qa_db["chatbot_qa"]
    vc_col    = qa_db["voice_qa"]
    hs_col    = qa_db["health_scores"]

    cb_total  = cb_col.count_documents({})
    cb_pass   = cb_col.count_documents({"status": "PASS"})
    cb_fail   = cb_col.count_documents({"status": "FAIL"})

    vc_total  = vc_col.count_documents({})
    vc_pass   = vc_col.count_documents({"status": "PASS"})
    vc_fail   = vc_col.count_documents({"status": "FAIL"})

    # Latest health scores
    cb_health_doc = hs_col.find_one({"component": "chatbot"}, sort=[("recorded_at", -1)])
    vc_health_doc = hs_col.find_one({"component": "voice"},   sort=[("recorded_at", -1)])
    cb_health = (cb_health_doc or {}).get("score", 0)
    vc_health = (vc_health_doc or {}).get("score", 0)

    # Recent flags — pull from chatbot_qa and voice_qa where issues exist
    recent_flags = []
    for doc in cb_col.find({"issues": {"$exists": True, "$ne": []}}, {"issues": 1, "conversation_id": 1}).sort("conversation_date", -1).limit(20):
        for issue in (doc.get("issues") or [])[:2]:
            recent_flags.append({
                "type":     issue.get("type", "UNKNOWN"),
                "detail":   issue.get("detail", ""),
                "severity": issue.get("severity", "LOW"),
                "source":   "chatbot",
            })
        if len(recent_flags) >= 10:
            break
    for doc in vc_col.find({"issues": {"$exists": True, "$ne": []}}, {"issues": 1}).sort("conversation_date", -1).limit(10):
        for issue in (doc.get("issues") or [])[:2]:
            recent_flags.append({
                "type":     issue.get("type", "UNKNOWN"),
                "detail":   issue.get("detail", ""),
                "severity": issue.get("severity", "LOW"),
                "source":   "voice",
            })
        if len(recent_flags) >= 15:
            break

    # Health trend — last 20 records
    trend = []
    for doc in hs_col.find({}, {"_id": 0, "recorded_at": 1, "component": 1, "score": 1}).sort("recorded_at", -1).limit(20):
        trend.append({
            "recorded_at": doc.get("recorded_at").isoformat() if isinstance(doc.get("recorded_at"), datetime) else str(doc.get("recorded_at", "")),
            "component":   doc.get("component", ""),
            "score":       doc.get("score", 0),
        })

    return app.response_class(to_json({
        "chatbot": {"pass": cb_pass, "fail": cb_fail, "total": cb_total, "health": cb_health},
        "voice":   {"pass": vc_pass, "fail": vc_fail, "total": vc_total, "health": vc_health},
        "recent_flags": recent_flags[:15],
        "health_trend": trend,
    }), mimetype="application/json")


@app.route("/api/qa/chatbot")
def api_qa_chatbot():
    if qa_db is None:
        return jsonify({"error": "QA DB not configured"}), 503

    page      = max(1, int(request.args.get("page", 1)))
    limit     = min(200, max(1, int(request.args.get("limit", 50))))
    status    = request.args.get("status", "ALL").upper()
    date_from = request.args.get("dateFrom", "")
    date_to   = request.args.get("dateTo", "")

    query = {}
    if status in ("PASS", "FAIL"):
        query["status"] = status

    date_filter = {}
    if date_from:
        try: date_filter["$gte"] = datetime.fromisoformat(date_from)
        except: pass
    if date_to:
        try: date_filter["$lte"] = datetime.fromisoformat(date_to + "T23:59:59")
        except: pass
    if date_filter:
        query["$or"] = [
            {"conversation_date": date_filter},
            {"evaluated_at": date_filter},
        ]

    col = qa_db["chatbot_qa"]
    total = col.count_documents(query)
    skip  = (page - 1) * limit

    projection = {
        "conversation_id": 1, "status": 1, "score": 1, "turn_count": 1,
        "issues": 1, "user_flags": 1, "summary": 1,
        "conversation_date": 1, "evaluated_at": 1,
        "visitor_id": 1, "lead_id": 1, "phone_number": 1,
    }

    docs = list(col.find(query, projection)
                   .sort([("conversation_date", -1), ("evaluated_at", -1)])
                   .skip(skip).limit(limit))

    pass_count = col.count_documents({**query, "status": "PASS"})
    fail_count = col.count_documents({**query, "status": "FAIL"})

    return app.response_class(to_json({
        "docs":  docs,
        "total": total,
        "page":  page,
        "pages": max(1, (total + limit - 1) // limit),
        "stats": {"pass": pass_count, "fail": fail_count, "total": total},
    }), mimetype="application/json")


@app.route("/api/qa/voice")
def api_qa_voice():
    if qa_db is None:
        return jsonify({"error": "QA DB not configured"}), 503

    page      = max(1, int(request.args.get("page", 1)))
    limit     = min(200, max(1, int(request.args.get("limit", 50))))
    status    = request.args.get("status", "ALL").upper()
    date_from = request.args.get("dateFrom", "")
    date_to   = request.args.get("dateTo", "")

    query = {}
    if status in ("PASS", "FAIL"):
        query["status"] = status

    date_filter = {}
    if date_from:
        try: date_filter["$gte"] = datetime.fromisoformat(date_from)
        except: pass
    if date_to:
        try: date_filter["$lte"] = datetime.fromisoformat(date_to + "T23:59:59")
        except: pass
    if date_filter:
        query["$or"] = [
            {"conversation_date": date_filter},
            {"evaluated_at": date_filter},
        ]

    col = qa_db["voice_qa"]
    total = col.count_documents(query)
    skip  = (page - 1) * limit

    docs = list(col.find(query)
                   .sort([("conversation_date", -1), ("evaluated_at", -1)])
                   .skip(skip).limit(limit))

    pass_count = col.count_documents({**query, "status": "PASS"})
    fail_count = col.count_documents({**query, "status": "FAIL"})

    return app.response_class(to_json({
        "docs":  docs,
        "total": total,
        "page":  page,
        "pages": max(1, (total + limit - 1) // limit),
        "stats": {"pass": pass_count, "fail": fail_count, "total": total},
    }), mimetype="application/json")


@app.route("/api/qa/analytics")
def api_qa_analytics():
    if qa_db is None:
        return jsonify({"error": "QA DB not configured"}), 503

    run_doc = None
    if "analytics_runs" in qa_db.list_collection_names():
        run_doc = qa_db["analytics_runs"].find_one({}, sort=[("run_at", -1)])

    feedback_doc = None
    if "feedback" in qa_db.list_collection_names():
        feedback_doc = qa_db["feedback"].find_one({}, sort=[("created_at", -1)])

    hs_col = qa_db["health_scores"]
    cb_h = hs_col.find_one({"component": "chatbot"}, sort=[("recorded_at", -1)])
    vc_h = hs_col.find_one({"component": "voice"},   sort=[("recorded_at", -1)])

    flags  = (run_doc or {}).get("flags", [])
    health = {
        "chatbot": (cb_h or {}).get("score", 0),
        "voice":   (vc_h or {}).get("score", 0),
    }
    run_at = ""
    if run_doc and run_doc.get("run_at"):
        ra = run_doc["run_at"]
        run_at = ra.isoformat() if isinstance(ra, datetime) else str(ra)

    return app.response_class(to_json({
        "flags":    flags,
        "health":   health,
        "run_at":   run_at,
        "feedback": feedback_doc,
    }), mimetype="application/json")


@app.route("/api/qa/spam")
def api_qa_spam():
    if qa_db is None:
        return jsonify({"error": "QA DB not configured"}), 503

    page       = max(1, int(request.args.get("page", 1)))
    limit      = min(200, max(1, int(request.args.get("limit", 50))))
    type_filter = request.args.get("type", "")

    query = {"skip_reason": "SPAM_FILTERED"}
    if type_filter:
        query["spam_flags.type"] = type_filter

    col   = qa_db["chatbot_qa"]
    total = col.count_documents(query)
    skip  = (page - 1) * limit
    docs  = list(col.find(query).sort([("conversation_date", -1)]).skip(skip).limit(limit))

    # Type counts
    pipeline = [
        {"$match": {"skip_reason": "SPAM_FILTERED", "spam_flags": {"$exists": True, "$ne": []}}},
        {"$unwind": "$spam_flags"},
        {"$group": {"_id": "$spam_flags.type", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    type_counts = {}
    try:
        for row in col.aggregate(pipeline):
            type_counts[row["_id"]] = row["count"]
    except Exception:
        pass

    return app.response_class(to_json({
        "docs":        docs,
        "total":       total,
        "pages":       max(1, (total + limit - 1) // limit),
        "type_counts": type_counts,
    }), mimetype="application/json")


@app.route("/api/qa/feedback")
def api_qa_feedback():
    if qa_db is None:
        return jsonify({"error": "QA DB not configured"}), 503
    fb  = qa_db["feedback"].find_one({"source": "automated"}, sort=[("submitted_at", -1)])
    all_fb = list(qa_db["feedback"].find({"source": "automated"}, {"submitted_at":1,"problems_found":1,"high_problems":1}).sort("submitted_at",-1).limit(20))
    return app.response_class(to_json({
        "latest":   fb,
        "history":  all_fb,
    }), mimetype="application/json")


@app.route("/api/qa/recommendations")
def api_qa_recommendations():
    if qa_db is None:
        return jsonify({"error": "QA DB not configured"}), 503
    rec  = qa_db["recommendations"].find_one(sort=[("generated_at", -1)])
    all_rec = list(qa_db["recommendations"].find({}, {"generated_at":1,"priority":1}).sort("generated_at",-1).limit(20))
    return app.response_class(to_json({
        "latest":  rec,
        "history": all_rec,
    }), mimetype="application/json")


# ── Auth Routes ───────────────────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>ASBL — Sign In</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,500;1,300&family=DM+Sans:wght@300;400;500&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  *{box-sizing:border-box;margin:0;padding:0;}
  :root{--bg:#F2EDE4;--surface:#FAF7F2;--accent:#9A7B3E;--accent-dim:rgba(154,123,62,0.12);--text:#1C1814;--text-muted:#7A7067;--text-dim:#B8AFA6;--border:rgba(154,123,62,0.18);}
  body{background:var(--bg);font-family:'DM Sans',sans-serif;color:var(--text);min-height:100vh;display:flex;align-items:center;justify-content:center;}
  .wrap{width:100%;max-width:400px;padding:32px 20px;}
  .brand{text-align:center;margin-bottom:36px;}
  .brand-mark{width:48px;height:48px;background:var(--accent);color:#FAF7F2;border-radius:10px;display:inline-flex;align-items:center;justify-content:center;font-family:'DM Mono',monospace;font-size:14px;letter-spacing:0.08em;margin-bottom:14px;}
  .brand-name{font-family:'Cormorant Garamond',serif;font-size:22px;font-weight:400;letter-spacing:0.06em;color:var(--text);display:block;}
  .brand-sub{font-size:11px;letter-spacing:0.1em;text-transform:uppercase;color:var(--text-muted);margin-top:4px;display:block;}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:32px 28px;}
  .card-title{font-size:15px;font-weight:500;margin-bottom:24px;color:var(--text);}
  label{display:block;font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:var(--text-muted);margin-bottom:6px;}
  input{width:100%;background:#F5F1EB;border:1px solid var(--border);border-radius:8px;padding:11px 14px;font-family:'DM Sans',sans-serif;font-size:13px;color:var(--text);outline:none;transition:border-color 0.15s;}
  input:focus{border-color:var(--accent);}
  .field{margin-bottom:18px;}
  .btn-submit{width:100%;background:var(--accent);color:#FAF7F2;border:none;border-radius:8px;padding:12px;font-family:'DM Sans',sans-serif;font-size:13px;font-weight:500;letter-spacing:0.04em;cursor:pointer;transition:opacity 0.15s;margin-top:6px;}
  .btn-submit:hover{opacity:0.88;}
  .error{background:rgba(180,60,50,0.08);border:1px solid rgba(180,60,50,0.2);border-radius:8px;padding:10px 14px;font-size:12px;color:#9B2E24;margin-bottom:20px;}
  .footer{text-align:center;font-size:10px;letter-spacing:0.06em;color:var(--text-dim);margin-top:20px;text-transform:uppercase;}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">
    <div class="brand-mark">AS</div>
    <span class="brand-name">ASBL</span>
    <span class="brand-sub">Lead Intelligence Platform</span>
  </div>
  <div class="card">
    <div class="card-title">Sign in to continue</div>
    {% if error %}
    <div class="error">Invalid email or password. Please try again.</div>
    {% endif %}
    <form method="POST" action="/api/login">
      <input type="hidden" name="next" value="{{ next_url }}" />
      <div class="field">
        <label>Email Address</label>
        <input type="email" name="email" required autocomplete="email" placeholder="you@asbl.in" />
      </div>
      <div class="field">
        <label>Password</label>
        <input type="password" name="password" required autocomplete="current-password" placeholder="••••••••" />
      </div>
      <button class="btn-submit" type="submit">Sign In</button>
    </form>
  </div>
  <div class="footer">ASBL Lead Intelligence · Secure Access</div>
</div>
</body>
</html>
"""

def _safe_next(raw):
    """Only an internal, relative path may be used as a post-login redirect
    target — never an absolute/external URL (open-redirect guard). Checked
    against both the raw value and its percent-decoded form: browsers
    normalise backslashes to forward slashes for http(s) URLs (WHATWG
    "special scheme" parsing), so "/\\evil.example" can behave like the
    protocol-relative "//evil.example", and an encoded variant (e.g.
    "%2F%2Fevil.example") could still reach here undecoded depending on the
    caller — Werkzeug decodes request.args/request.form, but this function
    doesn't assume that's the only way it's ever invoked."""
    def _looks_internal(v):
        return (v.startswith("/") and not v.startswith("//")
                and "://" not in v and "\\" not in v)
    if not raw:
        return "/"
    try:
        decoded = urllib.parse.unquote(raw)
    except Exception:
        return "/"
    if _looks_internal(raw) and _looks_internal(decoded):
        return raw
    return "/"

@app.route("/login")
def login_page():
    next_url = _safe_next(request.args.get("next", ""))
    if session.get("user_email"):
        return redirect(next_url)
    error = request.args.get("error", "0") == "1"
    return render_template_string(LOGIN_HTML, error=error, next_url=next_url)

# ── Login brute-force throttle (H3) ────────────────────────────────────────────
# DB-backed (works across serverless instances). Per client IP: block after
# LOGIN_MAX failures within LOGIN_WINDOW seconds. Epoch floats avoid tz issues.
_login_throttle = client["analytics_db"]["login_throttle"]
LOGIN_MAX = 8
LOGIN_WINDOW = 900   # 15 minutes

def _client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    return (xff.split(",")[0].strip() if xff else (request.remote_addr or "unknown")) or "unknown"

def _login_blocked(ip):
    try:
        doc = _login_throttle.find_one({"_id": ip}, {"fails": 1})
        cutoff = datetime.now(timezone.utc).timestamp() - LOGIN_WINDOW
        return len([t for t in (doc or {}).get("fails", []) if t > cutoff]) >= LOGIN_MAX
    except Exception:
        return False   # fail-open on throttle-store error (don't lock everyone out)

def _record_login_fail(ip):
    try:
        _login_throttle.update_one(
            {"_id": ip},
            {"$push": {"fails": {"$each": [datetime.now(timezone.utc).timestamp()], "$slice": -20}}},
            upsert=True)
    except Exception:
        pass

def _clear_login_fails(ip):
    try:
        _login_throttle.delete_one({"_id": ip})
    except Exception:
        pass

@app.route("/api/login", methods=["POST"])
def api_login():
    ip = _client_ip()
    next_url = _safe_next(request.form.get("next", ""))
    if _login_blocked(ip):
        return redirect(f"/login?error=1&next={next_url}")   # too many attempts — reject without checking
    email    = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    user = _get_login_user(email)
    if user and check_password_hash(user["hash"], password):
        _clear_login_fails(ip)
        session["user_email"] = email
        session["role"]       = user["role"]
        session["name"]       = user["name"]
        return redirect(next_url)
    _record_login_fail(ip)
    return redirect(f"/login?error=1&next={next_url}")

@app.route("/api/logout")
def api_logout():
    session.clear()
    return redirect("/login")


# ── Page Routes ───────────────────────────────────────────────────────────────
@app.after_request
def _no_store_html(resp):
    # The page HTML carries the app's inline JS. Never let the browser serve a
    # cached bundle — stale JS made project badges (and other client logic) look
    # broken after a deploy even on refresh. Dynamic JSON APIs are covered too;
    # only static assets keep their own caching.
    try:
        if resp.mimetype == "text/html":
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
    except Exception:
        pass
    # Mirror the CSRF token into a JS-readable cookie (httponly=False) so the page's
    # fetch wrapper can echo it in the X-CSRF-Token header. The session cookie stays
    # HttpOnly; this mirror only carries the token, which is validated server-side.
    try:
        _t = session.get("csrf_token")
        if _t and request.cookies.get("csrf_token") != _t:
            resp.set_cookie("csrf_token", _t, samesite="Lax",
                            secure=bool(os.environ.get("VERCEL")), httponly=False)
    except Exception:
        pass
    return resp


@app.route("/")
def index():
    return render_template_string(INDEX_HTML,
        user_name=session.get("name",""),
        user_role=session.get("role",""), embed=False)

@app.route("/embed/leads")
def embed_leads():
    # Same template, same data source, same JS — embed=True just Jinja-gates
    # out the nav/Sync Now/Export and forces IS_ADMIN off (see INDEX_HTML).
    # No separate business logic, no separate query path.
    return render_template_string(INDEX_HTML,
        user_name=session.get("name",""),
        user_role=session.get("role",""), embed=True)

@app.route("/lead/<lead_id>")
def lead_detail(lead_id):
    embed = request.args.get("embed") == "1"
    return render_template_string(DETAIL_HTML, lead_id=lead_id,
        user_name=session.get("name",""),
        user_role=session.get("role",""), embed=embed)

BULK_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bulk Calls — ASBL</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#F2EDE4;--surface:#FAF7F2;--surface-2:#EDE8DF;--surface-3:#E4DDD3;
    --border:#D8D1C5;--border-2:#C5BAA9;--accent:#9A7B3E;--accent-dim:rgba(154,123,62,0.1);
    --text:#1C1814;--text-muted:#8A7F72;--text-dim:#B5A99A;
    --green:#2E7D52;--green-dim:rgba(46,125,82,0.1);--red:#8B2635;--red-dim:rgba(139,38,53,0.1);
    --blue:#4A6FA5;--blue-dim:rgba(74,111,165,0.1);
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;font-size:14px;line-height:1.5}
  .topbar{display:flex;align-items:center;gap:18px;padding:16px 28px;background:var(--surface);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:5}
  .logo{font-weight:700;font-size:16px;letter-spacing:-0.01em}
  .logo small{display:block;font-weight:400;font-size:10px;color:var(--text-muted);letter-spacing:0.05em}
  .nav-links{display:flex;gap:4px;margin-left:8px}
  .nav-link{text-decoration:none;color:var(--text-muted);font-size:13px;font-weight:500;padding:6px 12px;border-radius:7px;border:1px solid transparent}
  .nav-link.active{color:var(--accent);background:var(--accent-dim);border-color:rgba(154,123,62,0.25)}
  .spacer{flex:1}
  .btn{background:var(--accent);color:#fff;border:none;border-radius:8px;padding:9px 16px;font-family:inherit;font-size:13px;font-weight:600;cursor:pointer}
  .btn:hover{opacity:0.9}
  main{padding:24px 28px;max-width:1200px;margin:0 auto}
  .summary{color:var(--text-muted);font-size:13px;margin-bottom:14px}
  table{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
  th{text-align:left;font-size:10px;letter-spacing:0.08em;text-transform:uppercase;color:var(--text-muted);padding:12px 14px;border-bottom:1px solid var(--border);font-weight:600}
  td{padding:12px 14px;border-bottom:1px solid var(--border);font-size:13px}
  tbody tr{cursor:pointer}
  tbody tr:hover{background:var(--surface-2)}
  tbody tr:last-child td{border-bottom:none}
  .mono{font-family:'DM Mono',monospace}
  .pill{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;border:1px solid}
  .muted{color:var(--text-dim)}
  .modal-backdrop{position:fixed;inset:0;background:rgba(28,24,20,0.45);display:flex;align-items:flex-start;justify-content:center;padding:40px 16px;z-index:20;overflow:auto}
  .modal{background:var(--surface);border:1px solid var(--border);border-radius:14px;width:100%;max-width:720px;padding:24px}
  .modal h2{font-size:18px;margin-bottom:2px}
  .cards{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:16px 0}
  .card{background:var(--surface-2);border:1px solid var(--border);border-radius:10px;padding:12px}
  .card .lbl{font-size:9px;letter-spacing:0.08em;text-transform:uppercase;color:var(--text-muted)}
  .card .val{font-size:22px;font-weight:700;margin-top:4px}
  .row-ctrl{display:flex;align-items:center;gap:10px;margin:10px 0}
  select{background:var(--surface-2);border:1px solid var(--border);border-radius:7px;padding:7px 10px;font-family:inherit;font-size:13px;color:var(--text);cursor:pointer}
  .call{border:1px solid var(--border);border-radius:9px;padding:10px 12px;margin-bottom:8px;background:var(--surface-2)}
  .call .top{display:flex;align-items:center;gap:10px;font-size:12px}
  .close{float:right;cursor:pointer;color:var(--text-muted);font-size:20px;line-height:1}
  audio{height:30px;vertical-align:middle}
</style></head><body>
<script>
/* CSRF: attach the double-submit token to every mutating request. */
(function(){var _f=window.fetch;function _c(){var m=document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);return m?decodeURIComponent(m[1]):'';}
window.fetch=function(u,o){o=o||{};var me=(o.method||'GET').toUpperCase();if(me!=='GET'&&me!=='HEAD'){var t=_c();if(t)o.headers=Object.assign({},o.headers||{},{'X-CSRF-Token':t});}return _f.call(this,u,o);};})();
</script>
<header class="topbar">
  <div class="logo" onclick="location.href='/'" style="cursor:pointer;" title="Go to Leads">ASBL<small>Bulk Manual Calling</small></div>
  <nav class="nav-links">
    <a href="/" class="nav-link">Leads</a>
    <a href="/bulk-calls" class="nav-link active">Bulk Calls</a>
    <a href="/qa" class="nav-link">QA</a>
  <a href="/pipeline" class="nav-link">Pipeline</a>
      <a href="/main-dashboard" class="nav-link">Main Dashboard</a>
  </nav>
  <div class="spacer"></div>
  {% if user_role == 'admin' %}
  <button class="btn" onclick="document.getElementById('fileInput').click()">↑ Upload sheet</button>
  <input type="file" id="fileInput" accept=".xlsx,.xls" style="display:none" onchange="doUpload(this)">
  {% endif %}
</header>
<main>
  <div id="bulkFilters" style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px;">
    <input id="fSearch" type="text" placeholder="Search lead ID or name…" oninput="applyFilters()"
      style="flex:1;min-width:200px;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-family:'DM Sans',sans-serif;font-size:13px;color:var(--text);">
    <select id="fProject" onchange="applyFilters()" style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-size:13px;font-family:'DM Sans',sans-serif;color:var(--text);cursor:pointer;"><option value="">All projects</option></select>
    <select id="fOutcome" onchange="applyFilters()" style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-size:13px;font-family:'DM Sans',sans-serif;color:var(--text);cursor:pointer;"><option value="">All outcomes</option></select>
    <select id="fPickup" onchange="applyFilters()" style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-size:13px;font-family:'DM Sans',sans-serif;color:var(--text);cursor:pointer;">
      <option value="">All pickup</option><option value="picked">Picked up</option><option value="zero">0%</option><option value="na">N/A</option>
    </select>
    <span style="font-size:12px;color:var(--text-muted);">Min calls</span>
    <input id="fMinCalls" type="number" min="0" placeholder="0" oninput="applyFilters()"
      style="width:74px;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:13px;font-family:'DM Sans',sans-serif;color:var(--text);">
    <button onclick="clearFilters()" style="background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:8px 14px;font-size:13px;font-family:'DM Sans',sans-serif;color:var(--text-muted);cursor:pointer;">Clear</button>
  </div>
  <div class="summary" id="summary">Loading…</div>
  <table>
    <thead><tr>
      <th style="width:44px">#</th>
      <th>Lead ID</th><th>Project</th><th>Name</th><th>Calls</th><th>Pickup</th>
      <th>Last Outcome</th><!-- <th>Stage</th><th>Flag</th> -->
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
</main>
<script>
const IS_ADMIN = {{ 'true' if user_role == 'admin' else 'false' }};
const STAGES = ['New Lead','Lead Initiated','Pre Site Visit','Post Site Visit','Not Interested'];
const FLAGS = ['genuine','spam','test','unclear'];
let REASONS = ['Not Responding'];
let ROWS = [];
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function pct(r){return (r==null)?'N/A':Math.round(r*100)+'%';}
function stageColor(s){if(s==='Not Interested')return 'var(--red)';if(s==='Post Site Visit'||s==='Pre Site Visit')return 'var(--green)';if(s==='Lead Initiated')return 'var(--blue)';return 'var(--text-muted)';}
function flagPill(f){if(!f)return '<span class="muted">—</span>';var col=f==='spam'?'var(--red)':(f==='genuine'?'var(--green)':'var(--accent)');return `<span class="pill" style="color:${col};border-color:${col};background:transparent">${esc(f)}</span>`;}

async function load(){
  try{ const rs=await fetch('/api/v2/not-interested-reasons'); if(rs.ok){const j=await rs.json(); if(j.reasons&&j.reasons.length)REASONS=j.reasons;} }catch(e){}
  const r=await fetch('/api/bulk-calls'); const j=await r.json();
  ROWS=j.rows||[];
  populateFilterOptions();
  applyFilters();
  if(!ROWS.length) document.getElementById('summary').textContent='No bulk-call leads yet — upload a campaign sheet to begin.';
}

function renderRows(rows){
  const tb=document.getElementById('rows');
  tb.innerHTML = rows.map((x,i)=>{
    const st=x.stage||'New Lead';
    /* ── Stage & Flag columns hidden per request. To restore: uncomment this
       block, add back the two <td> in the row template, the two <th> in the
       header, and set colspan back to 9. ──
    const stageCell = IS_ADMIN
      ? `<select onclick="event.stopPropagation()" onchange="changeStage('${esc(x.id)}',this,null)">${STAGES.map(s=>`<option ${s===st?'selected':''}>${esc(s)}</option>`).join('')}</select>`
        + (st==='Not Interested'?` <select onclick="event.stopPropagation()" onchange="changeStage('${esc(x.id)}',null,this)" title="reason" style="margin-top:4px">${REASONS.map(rn=>`<option ${rn===x.stage_reason?'selected':''}>${esc(rn)}</option>`).join('')}</select>`:'')
      : `<span class="pill" style="color:${stageColor(st)};border-color:${stageColor(st)};background:transparent">${esc(st)}</span>${x.stage_reason?` <span class="muted mono">${esc(x.stage_reason)}</span>`:''}`;
    const flagCell = IS_ADMIN
      ? `<select onclick="event.stopPropagation()" onchange="changeFlag('${esc(x.id)}',this)"><option value="">—</option>${FLAGS.map(f=>`<option ${f===x.flag?'selected':''}>${esc(f)}</option>`).join('')}</select>`
      : flagPill(x.flag);
    */
    return `<tr onclick="location.href='/lead/'+encodeURIComponent('${esc(x.lead_id||x.id)}')" style="cursor:pointer">
      <td class="muted">${i+1}</td>
      <td class="mono">${esc(x.display_id||'—')}</td><td>${esc(x.project||'—')}</td><td>${esc(x.name||'—')}</td>
      <td>${x.calls}</td><td>${pct(x.pickup_ratio)} <span class="muted">(${x.answered}/${x.calls})</span></td>
      <td>${esc(x.last_outcome||'—')}</td>
      </tr>`;
  }).join('') ||
    `<tr><td colspan="7" class="muted" style="text-align:center;padding:28px">No matching leads.</td></tr>`;
}

function populateFilterOptions(){
  const projs=[...new Set(ROWS.map(x=>x.project).filter(Boolean))].sort();
  const outs =[...new Set(ROWS.map(x=>x.last_outcome).filter(Boolean))].sort();
  document.getElementById('fProject').innerHTML='<option value="">All projects</option>'+projs.map(p=>`<option>${esc(p)}</option>`).join('');
  document.getElementById('fOutcome').innerHTML='<option value="">All outcomes</option>'+outs.map(o=>`<option>${esc(o)}</option>`).join('');
}

function applyFilters(){
  const q=(document.getElementById('fSearch').value||'').trim().toLowerCase();
  const proj=document.getElementById('fProject').value;
  const oc=document.getElementById('fOutcome').value;
  const pk=document.getElementById('fPickup').value;
  const minC=parseInt(document.getElementById('fMinCalls').value||'0',10)||0;
  const rows=ROWS.filter(x=>{
    if(q && !((x.display_id||'').toLowerCase().includes(q) || (x.name||'').toLowerCase().includes(q) || (x.phone||'').toLowerCase().includes(q))) return false;
    if(proj && (x.project||'')!==proj) return false;
    if(oc && (x.last_outcome||'')!==oc) return false;
    if(minC && (x.calls||0)<minC) return false;
    if(pk){ const p=x.pickup_ratio;
      if(pk==='na' && p!=null) return false;
      if(pk==='zero' && p!==0) return false;
      if(pk==='picked' && !(p>0)) return false; }
    return true;
  });
  renderRows(rows);
  document.getElementById('summary').textContent = (rows.length===ROWS.length)
    ? (ROWS.length+' bulk-call lead'+(ROWS.length!==1?'s':''))
    : (rows.length+' of '+ROWS.length+' leads');
}

function clearFilters(){
  ['fSearch','fMinCalls','fProject','fOutcome','fPickup'].forEach(id=>{document.getElementById(id).value='';});
  applyFilters();
}
async function changeStage(id, stageSel, reasonSel){
  const body={id:id};
  if(stageSel){ body.stage=stageSel.value; if(stageSel.value==='Not Interested') body.reason=REASONS[0]; }
  else { body.stage='Not Interested'; if(reasonSel) body.reason=reasonSel.value; }
  const r=await fetch('/api/bulk-calls/stage',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(!r.ok){alert('Stage update failed');return;} await load();
}
async function changeFlag(id, sel){
  const r=await fetch('/api/bulk-calls/flag',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id,flag:sel.value})});
  if(!r.ok){alert('Flag update failed');return;} await load();
}
async function doUpload(inp){
  if(!inp.files||!inp.files[0])return;
  const fd=new FormData(); fd.append('file',inp.files[0]); inp.value='';
  document.getElementById('summary').textContent='Uploading…';
  const r=await fetch('/api/bulk-calls/upload',{method:'POST',body:fd});
  const j=await r.json();
  if(!r.ok){alert('Upload failed: '+(j.error||r.status));await load();return;}
  alert(`Added ${j.added}, updated ${j.updated}, skipped ${j.skipped}.`); await load();
}
load();
</script></body></html>"""

@app.route("/bulk-calls")
def bulk_calls_page():
    return render_template_string(BULK_HTML,
        user_name=session.get("name",""),
        user_role=session.get("role",""))

# ── Auto-open Browser (local only) ────────────────────────────────────────────
def open_browser():
    time.sleep(1.2)
    webbrowser.open("http://localhost:5000")

# ═════════════════════════════════════════════════════════════════════════════
#  HTML TEMPLATES
# ═════════════════════════════════════════════════════════════════════════════

# ── Shared styles & fonts ─────────────────────────────────────────────────────
SHARED_HEAD = """
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,500;0,600;1,300;1,400&family=DM+Sans:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:         #F2EDE4;
    --surface:    #FAF7F2;
    --surface-2:  #EDE8DF;
    --surface-3:  #E4DDD3;
    --border:     #D8D1C5;
    --border-2:   #C5BAA9;
    --accent:     #9A7B3E;
    --accent-dim: rgba(154,123,62,0.1);
    --accent-glow:rgba(154,123,62,0.05);
    --text:       #1C1814;
    --text-muted: #8A7F72;
    --text-dim:   #B5A99A;
    --green:      #2E7D52;
    --green-dim:  rgba(46,125,82,0.1);
    --red:        #8B2635;
    --red-dim:    rgba(139,38,53,0.1);
    --blue:       #4A6FA5;
    --blue-dim:   rgba(74,111,165,0.1);
    --amber-dim:  rgba(154,123,62,0.12);
  }

  html { scroll-behavior: smooth; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'DM Sans', sans-serif;
    font-size: 14px;
    line-height: 1.5;
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* Subtle paper grain overlay */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.65' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.025'/%3E%3C/svg%3E");
    pointer-events: none;
    z-index: 0;
    opacity: 0.5;
  }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 4px; height: 4px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border-2); border-radius: 2px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

  /* ── Typography ── */
  .font-syne { font-family: 'Cormorant Garamond', serif; }
  .font-mono  { font-family: 'DM Mono', monospace; }

  /* ── Nav / Header ── */
  .topbar {
    position: sticky;
    top: 0;
    z-index: 100;
    background: rgba(242,237,228,0.95);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border-bottom: 1px solid var(--border);
    padding: 0 28px;
    height: 60px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }

  .topbar-logo {
    display: flex;
    align-items: center;
    gap: 14px;
  }

  .logo-mark {
    width: 32px; height: 32px;
    background: var(--text);
    border-radius: 4px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: 'Cormorant Garamond', serif;
    font-weight: 600;
    font-size: 14px;
    color: var(--bg);
    letter-spacing: 0.02em;
    flex-shrink: 0;
  }

  .logo-text {
    display: flex;
    flex-direction: column;
    gap: 0;
  }

  .logo-primary {
    font-family: 'Cormorant Garamond', serif;
    font-weight: 600;
    font-size: 17px;
    letter-spacing: 0.1em;
    color: var(--text);
    line-height: 1.1;
  }

  .logo-sub {
    font-family: 'DM Sans', sans-serif;
    font-size: 10px;
    color: var(--text-muted);
    letter-spacing: 0.14em;
    text-transform: uppercase;
    line-height: 1.2;
  }

  /* ── Stat Pills ── */
  .stat-strip {
    display: flex;
    gap: 0;
    padding: 0 28px;
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
    background: var(--surface);
  }

  .stat-pill {
    display: flex;
    align-items: center;
    gap: 12px;
    background: transparent;
    border: none;
    border-right: 1px solid var(--border);
    border-radius: 0;
    padding: 14px 24px;
    min-width: 150px;
    position: relative;
    overflow: hidden;
    transition: background 0.2s;
  }

  .stat-pill::before {
    content: '';
    position: absolute;
    bottom: 0; left: 24px; right: 24px;
    height: 2px;
    background: var(--accent);
    transform: scaleX(0);
    transform-origin: left;
    transition: transform 0.3s ease;
  }

  .stat-pill:hover { background: var(--surface-2); }
  .stat-pill:hover::before { transform: scaleX(1); }

  .stat-pill-value {
    font-family: 'DM Sans', sans-serif;
    font-weight: 400;
    font-size: 28px;
    color: var(--accent);
    line-height: 1;
  }

  .stat-pill-label {
    font-size: 10px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    line-height: 1.3;
    font-weight: 500;
  }

  /* ── Search bar ── */
  .search-wrap {
    padding: 14px 28px;
    border-bottom: 1px solid var(--border);
    position: relative;
    background: var(--bg);
  }

  .search-input {
    width: 100%;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 11px 16px 11px 42px;
    color: var(--text);
    font-family: 'DM Sans', sans-serif;
    font-size: 14px;
    outline: none;
    transition: border-color 0.2s, box-shadow 0.2s;
  }

  .search-input::placeholder { color: var(--text-dim); }
  .search-input:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-dim);
  }

  .search-icon {
    position: absolute;
    left: 42px;
    top: 50%;
    transform: translateY(-50%);
    color: var(--text-dim);
    pointer-events: none;
    font-size: 16px;
  }

  /* ── Table ── */
  .table-wrap {
    overflow-x: auto;
    flex: 1;
  }

  table {
    width: 100%;
    border-collapse: collapse;
  }

  thead {
    position: sticky;
    top: 0;
    z-index: 10;
    background: var(--surface-2);
  }

  th {
    padding: 11px 16px;
    text-align: left;
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.16em;
    color: var(--text-muted);
    font-family: 'DM Sans', sans-serif;
    font-weight: 600;
    border-bottom: 1px solid var(--border-2);
    white-space: nowrap;
    user-select: none;
  }

  th:first-child { padding-left: 28px; }
  th:last-child { padding-right: 28px; }

  tbody tr {
    border-bottom: 1px solid var(--border);
    cursor: pointer;
    transition: background 0.15s;
  }

  tbody tr:hover { background: var(--surface); }
  tbody tr:hover td:last-child .view-btn { opacity: 1; transform: translateX(0); }

  /* New-activity highlight — subtle amber tint + left accent bar on the row. */
  tbody tr.lead-row-new td { background: rgba(154,123,62,0.08); }
  tbody tr.lead-row-new:hover td { background: rgba(154,123,62,0.13); }
  tbody tr.lead-row-new td:first-child { box-shadow: inset 3px 0 0 var(--accent, #9a7b3e); }

  td {
    padding: 13px 16px;
    vertical-align: middle;
  }

  td:first-child { padding-left: 28px; }
  td:last-child { padding-right: 28px; }

  .td-name {
    font-weight: 500;
    color: var(--text);
    white-space: nowrap;
    max-width: 260px;
  }
  /* Truncate only the NAME; keep the project badge always visible after it.
     Previously the whole cell clipped at 180px, so a long name hid the badge. */
  .td-name .name-text {
    display: inline-block;
    max-width: 150px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    vertical-align: middle;
  }

  .td-phone {
    font-family: 'DM Mono', monospace;
    font-size: 12px;
    color: var(--blue);
    white-space: nowrap;
  }

  .td-num {
    font-family: 'DM Mono', monospace;
    font-size: 13px;
    color: var(--text);
    text-align: right;
  }

  .td-dim { color: var(--text-dim); }

  /* ── Badges ── */
  .badge {
    display: inline-flex;
    align-items: center;
    padding: 3px 9px;
    border-radius: 100px;
    font-size: 10px;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    font-weight: 500;
    white-space: nowrap;
    max-width: 140px;
    overflow: hidden;
    text-overflow: ellipsis;
    font-family: 'DM Sans', sans-serif;
  }

  .badge-source {
    background: var(--blue-dim);
    color: var(--blue);
    border: 1px solid rgba(74,111,165,0.2);
  }

  .badge-campaign {
    background: var(--surface-2);
    color: var(--text-muted);
    border: 1px solid var(--border);
    text-transform: none;
    font-size: 11px;
    letter-spacing: 0;
    max-width: 160px;
    border-radius: 4px;
  }

  .badge-green {
    background: var(--green-dim);
    color: var(--green);
    border: 1px solid rgba(46,125,82,0.2);
  }

  .badge-amber {
    background: var(--amber-dim);
    color: var(--accent);
    border: 1px solid rgba(154,123,62,0.25);
  }

  .badge-red {
    background: var(--red-dim);
    color: var(--red);
    border: 1px solid rgba(139,38,53,0.2);
  }

  .badge-gray {
    background: var(--surface-2);
    color: var(--text-muted);
    border: 1px solid var(--border);
  }

  .badge-outline {
    background: transparent;
    color: var(--text-muted);
    border: 1px solid var(--border);
    text-transform: none;
    font-size: 11px;
    letter-spacing: 0;
    border-radius: 4px;
  }

  /* ── Flag filter bar ── */
  /* ── Pickup filter popover rows ── */
  .pf-row {
    display:flex; align-items:center; gap:9px;
    padding:7px 8px; border-radius:7px; cursor:pointer;
    font-size:13px; color:var(--text); font-weight:400;
    transition:background 0.12s; margin-bottom:2px;
  }
  .pf-row:hover { background:var(--accent-dim); }
  .pf-row input[type=checkbox] {
    width:14px; height:14px; accent-color:var(--accent); cursor:pointer; flex-shrink:0;
  }
  @keyframes fadeIn { from{opacity:0;transform:translateY(-4px)} to{opacity:1;transform:translateY(0)} }

  .flag-bar { display:flex; gap:6px; padding: 14px 28px 8px; align-items:center; flex-wrap:wrap; }
  .flag-summary { font-size:12px; color:var(--text-muted); padding: 0 28px 14px; font-family:'DM Mono',monospace; min-height:20px; }
  /* Bulk-select cells: hidden until the table enters "selecting" mode */
  #leads-table .bulk-cell { display: none; }
  #leads-table.selecting .bulk-cell { display: table-cell; }
  #bulk-select-toggle.active, #dedupe-view-toggle.active { background: var(--accent); color: #FAF7F2; border-color: var(--accent); }
  .flag-btn { font-family:'DM Sans',sans-serif; font-size:12px; padding:5px 14px; border-radius:20px; border:1px solid rgba(154,123,62,0.22); background:transparent; color:var(--text-muted); cursor:pointer; transition:all 0.15s; }
  .flag-btn:hover { background:var(--accent-dim); color:var(--accent); }
  .flag-btn.active { background:var(--accent); color:#FAF7F2; border-color:var(--accent); }
  .flag-btn.active-genuine { background:rgba(39,120,59,0.12); color:#1a6b30; border-color:rgba(39,120,59,0.3); }
  .flag-btn.active-spam { background:rgba(180,60,50,0.1); color:#9B2E24; border-color:rgba(180,60,50,0.25); }
  .flag-btn.active-test { background:rgba(100,80,180,0.1); color:#4a3a9a; border-color:rgba(100,80,180,0.25); }
  .flag-btn.active-unclear { background:rgba(160,120,0,0.1); color:#7a5c00; border-color:rgba(160,120,0,0.25); }

  /* ── Flag badges ── */
  .flag-genuine { display:inline-flex; align-items:center; gap:4px; font-size:10px; font-family:'DM Mono',monospace; padding:2px 8px; border-radius:20px; background:rgba(39,120,59,0.12); color:#1a6b30; border:1px solid rgba(39,120,59,0.2); cursor:pointer; white-space:nowrap; }
  .flag-spam    { display:inline-flex; align-items:center; gap:4px; font-size:10px; font-family:'DM Mono',monospace; padding:2px 8px; border-radius:20px; background:rgba(180,60,50,0.1); color:#9B2E24; border:1px solid rgba(180,60,50,0.2); cursor:pointer; white-space:nowrap; }
  .flag-test    { display:inline-flex; align-items:center; gap:4px; font-size:10px; font-family:'DM Mono',monospace; padding:2px 8px; border-radius:20px; background:rgba(100,80,180,0.1); color:#4a3a9a; border:1px solid rgba(100,80,180,0.2); cursor:pointer; white-space:nowrap; }
  .flag-unclear        { display:inline-flex; align-items:center; gap:4px; font-size:10px; font-family:'DM Mono',monospace; padding:2px 8px; border-radius:20px; background:rgba(160,120,0,0.1); color:#7a5c00; border:1px solid rgba(160,120,0,0.2); cursor:pointer; white-space:nowrap; }
  .flag-none           { display:inline-flex; align-items:center; gap:4px; font-size:10px; font-family:'DM Mono',monospace; padding:2px 8px; border-radius:20px; background:transparent; color:var(--text-dim); border:1px dashed rgba(154,123,62,0.2); cursor:pointer; white-space:nowrap; opacity:0.5; }
  .flag-none:hover, .flag-genuine:hover, .flag-spam:hover, .flag-test:hover, .flag-unclear:hover { opacity:1; }

  /* ── View Button ── */
  .view-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 12px;
    background: var(--accent-dim);
    border: 1px solid rgba(154,123,62,0.25);
    border-radius: 100px;
    color: var(--accent);
    font-size: 11px;
    font-weight: 500;
    opacity: 0;
    transform: translateX(4px);
    transition: opacity 0.2s, transform 0.2s, background 0.2s;
    white-space: nowrap;
  }

  .view-btn:hover { background: rgba(154,123,62,0.18); }

  /* ── Button ── */
  .btn {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 8px 16px;
    background: var(--surface);
    border: 1px solid var(--border-2);
    border-radius: 4px;
    color: var(--text-muted);
    font-family: 'DM Sans', sans-serif;
    font-size: 13px;
    cursor: pointer;
    transition: all 0.2s;
  }

  .btn:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-dim); }

  .btn-accent {
    background: var(--text);
    border-color: var(--text);
    color: var(--surface);
    font-weight: 500;
  }

  .btn-accent:hover { background: var(--accent); border-color: var(--accent); color: var(--surface); }

  /* ── Empty state ── */
  .empty-state {
    padding: 64px 28px;
    text-align: center;
  }

  .empty-state-icon {
    font-size: 36px;
    margin-bottom: 12px;
    opacity: 0.2;
  }

  .empty-state-text {
    color: var(--text-muted);
    font-size: 15px;
  }

  /* ── Loading ── */
  .loading-row td {
    padding: 64px 28px;
    text-align: center;
    color: var(--text-muted);
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }

  .loading-dot {
    display: inline-block;
    animation: pulse 1.2s ease-in-out infinite;
  }
  .loading-dot:nth-child(2) { animation-delay: 0.2s; }
  .loading-dot:nth-child(3) { animation-delay: 0.4s; }

  /* ── Tabs ── */
  .tabs-nav {
    display: flex;
    gap: 0;
    padding: 0 28px;
    border-bottom: 1px solid var(--border);
    margin-top: 20px;
  }

  .tab-btn {
    padding: 11px 20px;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text-muted);
    font-family: 'DM Sans', sans-serif;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
    margin-bottom: -1px;
    letter-spacing: 0.04em;
  }

  .tab-btn:hover { color: var(--text); }

  .tab-btn.active {
    color: var(--accent);
    border-bottom-color: var(--accent);
  }

  .tab-content { display: none; padding: 24px 28px; }
  .tab-content.active { display: block; }

  /* ── Stat Cards ── */
  .stat-cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }

  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 20px;
    position: relative;
    overflow: hidden;
  }

  .stat-card::after {
    content: '';
    position: absolute;
    bottom: 0; right: 0;
    width: 60px; height: 60px;
    background: radial-gradient(circle, var(--accent-dim) 0%, transparent 70%);
    pointer-events: none;
  }

  .stat-card-label {
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--text-muted);
    margin-bottom: 10px;
    font-weight: 600;
  }

  .stat-card-value {
    font-family: 'DM Sans', sans-serif;
    font-weight: 400;
    font-size: 32px;
    color: var(--accent);
    line-height: 1;
  }

  .stat-card-sub {
    font-size: 11px;
    color: var(--text-dim);
    margin-top: 5px;
  }

  /* ── Chat bubbles ── */
  .chat-log {
    display: flex;
    flex-direction: column;
    gap: 10px;
    max-height: 600px;
    overflow-y: auto;
    padding: 4px;
  }

  .chat-row {
    display: flex;
    flex-direction: column;
    gap: 2px;
    max-width: 72%;
  }

  .chat-row.right { align-self: flex-end; align-items: flex-end; }
  .chat-row.left  { align-self: flex-start; align-items: flex-start; }

  .chat-label {
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-dim);
    padding: 0 8px;
    font-weight: 500;
  }

  .bubble {
    padding: 10px 14px;
    border-radius: 8px;
    font-size: 13.5px;
    line-height: 1.55;
    word-break: break-word;
  }

  .bubble-user {
    background: var(--amber-dim);
    border: 1px solid rgba(154,123,62,0.18);
    color: var(--text);
    border-bottom-right-radius: 2px;
  }

  .bubble-bot {
    background: var(--surface-2);
    border: 1px solid var(--border);
    color: var(--text);
    border-bottom-left-radius: 2px;
  }

  .bubble-ts {
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    color: var(--text-dim);
    padding: 0 8px;
  }

  .date-sep {
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 12px 0;
    color: var(--text-dim);
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    font-weight: 500;
  }

  .date-sep::before,
  .date-sep::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--border);
  }

  /* ── Call cards ── */
  .call-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    margin-bottom: 10px;
    overflow: hidden;
  }

  .call-card-header {
    padding: 14px 18px;
    display: flex;
    align-items: center;
    gap: 12px;
    cursor: pointer;
    user-select: none;
    transition: background 0.15s;
  }

  .call-card-header:hover { background: var(--surface-2); }

  .call-card-num {
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    color: var(--text-dim);
    min-width: 28px;
  }

  .call-card-meta {
    flex: 1;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }

  .call-card-date {
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    color: var(--text-muted);
    margin-left: auto;
  }

  .call-expand-icon {
    color: var(--text-dim);
    font-size: 14px;
    transition: transform 0.25s;
    flex-shrink: 0;
  }

  .call-card.open .call-expand-icon { transform: rotate(180deg); }

  .call-transcript {
    display: none;
    padding: 0 18px 18px;
    border-top: 1px solid var(--border);
    margin-top: 0;
  }

  .call-card.open .call-transcript { display: block; padding-top: 16px; }

  /* ── Recording Player ── */
  .recording-player {
    display: flex;
    align-items: center;
    gap: 12px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px 16px;
    margin-bottom: 16px;
  }
  .recording-player-icon {
    font-size: 18px;
    flex-shrink: 0;
    color: var(--accent);
  }
  .recording-player-info {
    flex: 1;
    min-width: 0;
  }
  .recording-player-label {
    font-size: 9px;
    font-weight: 600;
    letter-spacing: 0.12em;
    color: var(--text-muted);
    text-transform: uppercase;
    margin-bottom: 6px;
  }
  .recording-player audio {
    width: 100%;
    height: 32px;
    accent-color: var(--accent);
    outline: none;
    border-radius: 4px;
  }
  .recording-player-dur {
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    color: var(--text-dim);
    flex-shrink: 0;
  }
  .rec-dur-info-btn {
    display: inline-flex; align-items: center; justify-content: center;
    width: 14px; height: 14px; border-radius: 50%;
    background: rgba(154,123,62,0.12); color: #9a7b3e;
    font-size: 9px; font-weight: 700; font-style: normal;
    cursor: default; border: 1px solid rgba(154,123,62,0.22);
    line-height: 1; flex-shrink: 0; transition: background 0.15s;
  }
  .rec-dur-info-btn:hover { background: rgba(154,123,62,0.28); }
  .rec-dur-tooltip {
    display: none; position: absolute;
    right: 0; bottom: calc(100% + 8px);
    background: #2c2416; color: #c8bfaf;
    font-size: 11px; line-height: 1.5;
    padding: 8px 12px; border-radius: 7px;
    white-space: nowrap; z-index: 100; pointer-events: none;
    box-shadow: 0 4px 16px rgba(0,0,0,0.25);
  }
  .rec-dur-tooltip::after {
    content: ''; position: absolute; top: 100%; right: 10px;
    border: 5px solid transparent; border-top-color: #2c2416;
  }
  .rec-dur-tip-wrap:hover .rec-dur-tooltip { display: block; }

  /* ── Artifact Pills ── */
  .artifact-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 8px;
  }

  .artifact-pill {
    padding: 4px 12px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 100px;
    font-size: 11px;
    color: var(--text-muted);
  }

  /* ── Website active card ── */
  .website-active-card {
    box-shadow: inset 0 -2px 0 var(--accent) !important;
    background: rgba(154,123,62,0.06) !important;
  }
  .website-view-card:hover { background: rgba(154,123,62,0.04); }

  /* ── CTA log items ── */
  .cta-log-item {
    border: 1px solid var(--border);
    border-radius: 10px;
    margin-bottom: 12px;
  }
  .cta-log-header {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    padding: 10px 14px;
    background: var(--surface-2);
    border-radius: 10px;
  }
  .cta-log-item:has(.cta-log-body) .cta-log-header {
    border-bottom: 1px solid var(--border);
    border-radius: 10px 10px 0 0;
  }
  .cta-log-label {
    font-size: 12px;
    color: var(--text-muted);
    flex: 1;
  }
  .cta-log-time {
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    color: var(--text-dim);
    margin-left: auto;
  }
  .cta-log-body {
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .cta-log-sender {
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-muted);
  }

  /* ── Back link ── */
  .back-link {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--text-muted);
    font-size: 13px;
    text-decoration: none;
    padding: 20px 28px 0;
    transition: color 0.2s;
    letter-spacing: 0.02em;
  }

  .back-link:hover { color: var(--accent); }

  /* ── Lead header ── */
  .lead-header {
    padding: 20px 28px 0;
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    gap: 16px;
    justify-content: space-between;
  }

  .lead-name {
    font-family: 'Cormorant Garamond', serif;
    font-weight: 500;
    font-size: 34px;
    color: var(--text);
    letter-spacing: 0.01em;
    line-height: 1.1;
  }

  .lead-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
    margin-top: 8px;
  }

  .lead-phone-display {
    font-family: 'DM Mono', monospace;
    font-size: 13px;
    color: var(--blue);
    background: var(--blue-dim);
    border: 1px solid rgba(74,111,165,0.2);
    padding: 4px 10px;
    border-radius: 100px;
  }

  .lead-since {
    font-size: 12px;
    color: var(--text-muted);
    font-family: 'DM Mono', monospace;
  }

  .divider {
    color: var(--border-2);
    font-size: 14px;
  }

  /* ── No activity ── */
  .no-activity {
    padding: 40px 0;
    text-align: center;
    color: var(--text-dim);
    font-size: 14px;
  }

  /* ── Outcome badges (dynamic coloring via JS) ── */
  .outcome-badge {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 3px 9px;
    border-radius: 100px;
  }

  /* ── Refresh spinner ── */
  @keyframes spin {
    to { transform: rotate(360deg); }
  }
  .spinning { animation: spin 0.7s linear infinite; }

  /* ── Fade in ── */
  @keyframes fadeInUp {
    from { opacity: 0; transform: translateY(8px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  .fade-in { animation: fadeInUp 0.3s ease forwards; }

  /* ── Index layout ── */
  .page-index {
    display: flex;
    flex-direction: column;
    height: 100vh;
  }

  .table-container {
    flex: 1;
    overflow: auto;
  }

  /* ── Topbar bottom rule ── */
  .topbar::after {
    content: '';
    position: absolute;
    bottom: -1px; left: 0; right: 0;
    height: 1px;
    background: linear-gradient(90deg, transparent, var(--accent), transparent);
    opacity: 0.4;
  }
  .topbar { position: relative; }

  /* ── Accent border on stat cards ── */
  .stat-card-accent { border-left: 2px solid var(--accent); }
  .stat-card-green  { border-left: 2px solid var(--green); }
  .stat-card-blue   { border-left: 2px solid var(--blue); }
  .stat-card-red    { border-left: 2px solid var(--red); }

  /* ── Progress mini bar ── */
  .mini-bar-wrap {
    height: 2px;
    background: var(--border);
    border-radius: 2px;
    margin-top: 8px;
    overflow: hidden;
  }
  .mini-bar-fill {
    height: 100%;
    border-radius: 2px;
    transition: width 0.6s ease;
  }

  /* ── Nav links (used by QA pages) ── */
  .nav-links { display: flex; align-items: center; gap: 4px; margin-left: 24px; }
  .nav-link {
    font-family: 'DM Sans', sans-serif; font-size: 12px; font-weight: 500;
    letter-spacing: 0.08em; text-transform: uppercase;
    color: var(--text-muted); text-decoration: none;
    padding: 6px 14px; border-radius: 4px;
    border: 1px solid transparent;
    transition: all 0.15s;
  }
  .nav-link:hover { color: var(--accent); background: var(--accent-dim); border-color: rgba(154,123,62,0.2); }
  .nav-link.active { color: var(--accent); background: var(--accent-dim); border-color: rgba(154,123,62,0.25); }

  /* ── QA-specific styles ── */
  .qa-page { padding: 28px; max-width: 1400px; }
  .qa-filter-bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 20px; }
  .qa-filter-btn { font-family: 'DM Sans', sans-serif; font-size: 12px; padding: 5px 16px; border-radius: 20px; border: 1px solid var(--border-2); background: var(--surface); color: var(--text-muted); cursor: pointer; transition: all 0.15s; }
  .qa-filter-btn:hover { background: var(--accent-dim); color: var(--accent); border-color: rgba(154,123,62,0.3); }
  .qa-filter-btn.active { background: var(--accent); color: #FAF7F2; border-color: var(--accent); }
  .qa-date-input { font-family: 'DM Mono', monospace; font-size: 12px; padding: 5px 12px; border: 1px solid var(--border-2); border-radius: 4px; background: var(--surface); color: var(--text); outline: none; }
  .qa-date-input:focus { border-color: var(--accent); }
  .qa-paginator { display: flex; align-items: center; gap: 12px; padding: 16px 0; font-size: 13px; color: var(--text-muted); }
  .qa-paginator button { font-family: 'DM Sans', sans-serif; font-size: 12px; padding: 5px 14px; border: 1px solid var(--border-2); border-radius: 4px; background: var(--surface); color: var(--text-muted); cursor: pointer; transition: all 0.15s; }
  .qa-paginator button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
  .qa-paginator button:disabled { opacity: 0.4; cursor: default; }
  .expand-row { display: none; background: var(--surface); }
  .expand-row.open { display: table-row; }
  .expand-cell { padding: 16px 28px !important; border-bottom: 2px solid var(--border-2); }
  .expand-cell-inner { display: flex; flex-wrap: wrap; gap: 24px; }
  .expand-section { min-width: 200px; }
  .expand-label { font-size: 9px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--text-muted); margin-bottom: 6px; font-weight: 600; }
  .expand-value { font-family: 'DM Mono', monospace; font-size: 12px; color: var(--text); word-break: break-all; }
  .issue-list { display: flex; flex-direction: column; gap: 6px; margin-top: 4px; }
  .issue-item { display: flex; align-items: flex-start; gap: 8px; font-size: 12px; padding: 6px 10px; background: var(--surface-2); border-radius: 4px; border-left: 2px solid var(--border-2); }
  .issue-item.sev-HIGH   { border-left-color: var(--red); }
  .issue-item.sev-MEDIUM { border-left-color: var(--accent); }
  .issue-item.sev-LOW    { border-left-color: var(--border-2); }
  .health-score-card { border-radius: 8px; padding: 20px 24px; display: flex; align-items: center; gap: 20px; border: 1px solid var(--border); }
  .health-score-num { font-family: 'DM Sans', sans-serif; font-size: 48px; font-weight: 400; line-height: 1; }
  .health-score-green { color: var(--green); background: var(--green-dim); }
  .health-score-amber { color: var(--accent); background: var(--amber-dim); }
  .health-score-red   { color: var(--red);   background: var(--red-dim); }
  .spam-type-pill { display: inline-flex; align-items: center; gap: 6px; padding: 5px 14px; border-radius: 20px; border: 1px solid var(--border-2); background: var(--surface); color: var(--text-muted); font-size: 11px; cursor: pointer; transition: all 0.15s; font-family: 'DM Sans', sans-serif; }
  .spam-type-pill:hover { background: var(--accent-dim); color: var(--accent); border-color: rgba(154,123,62,0.3); }
  .spam-type-pill.active { background: var(--accent); color: #FAF7F2; border-color: var(--accent); }
  .spam-type-count { font-family: 'DM Mono', monospace; font-size: 10px; opacity: 0.8; }

  {% if embed %}
  /* ═══ EMBED SKIN — match the Intelligent CRM palette (shadcn HSL tokens) ═══
     Applied ONLY in embed mode (the CRM iframe). The standalone dashboard keeps its
     warm theme. Placed last in <style> so these override the base :root by source
     order. Light is the default; the inline script below flips to dark via
     ?theme=dark or the viewer's prefers-color-scheme. Recolor only — layout intact. */
  :root {
    --bg:hsl(210 40% 98%); --surface:hsl(0 0% 100%); --surface-2:hsl(210 28% 95%); --surface-3:hsl(210 29% 91%);
    --border:hsl(214 25% 89%); --border-2:hsl(214 22% 83%);
    --accent:hsl(189 90% 36%); --accent-dim:hsl(189 90% 36% / 0.10); --accent-glow:hsl(189 90% 36% / 0.05); --amber-dim:hsl(189 90% 36% / 0.12);
    --text:hsl(222 38% 12%); --text-muted:hsl(215 16% 44%); --text-dim:hsl(215 16% 62%);
    --primary-fg:hsl(0 0% 100%);
    --green:#1c7a4d; --green-dim:rgba(28,122,77,0.10); --red:#b23a4b; --red-dim:rgba(178,58,75,0.10); --blue:hsl(189 90% 36%); --blue-dim:hsl(189 90% 36% / 0.10);
  }
  :root[data-embed-theme="dark"] {
    --bg:hsl(224 32% 6%); --surface:hsl(222 22% 11%); --surface-2:hsl(220 16% 16%); --surface-3:hsl(220 16% 21%);
    --border:hsl(220 14% 18%); --border-2:hsl(220 14% 27%);
    --accent:hsl(189 88% 47%); --accent-dim:hsl(189 88% 47% / 0.14); --accent-glow:hsl(189 88% 47% / 0.06); --amber-dim:hsl(189 88% 47% / 0.16);
    --text:hsl(213 25% 95%); --text-muted:hsl(218 13% 63%); --text-dim:hsl(218 12% 48%);
    --primary-fg:hsl(200 65% 7%);
    --green:hsl(151 52% 56%); --green-dim:hsl(151 52% 56% / 0.14); --red:hsl(352 74% 67%); --red-dim:hsl(352 74% 67% / 0.15); --blue:hsl(189 78% 62%); --blue-dim:hsl(189 78% 62% / 0.14);
  }
  body { font-family:'Hanken Grotesk','DM Sans',sans-serif; }
  body::before { display:none; }            /* drop the paper-grain overlay for a clean CRM look */
  .btn { color:var(--primary-fg); }
  /* a few elements hardcode the warm palette — repoint them at the themed vars so
     dark mode is consistent (the topbar had a baked-in beige background). */
  .topbar { background:var(--surface); }
  tbody tr.lead-row-new td,
  tbody tr.lead-row-new:hover td { background:var(--accent-dim); }
  {% endif %}

</style>
{% if embed %}
<link href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet"/>
<script>
  /* Embed theme picker: ?theme=dark|light from the CRM wins; otherwise follow the
     viewer's OS (prefers-color-scheme) and keep tracking changes. Runs in <head>
     before <body> paints, so no flash. Default (no attribute) is CRM light (CSS). */
  (function(){
    try{
      var d=document.documentElement;
      var t=(new URLSearchParams(location.search).get('theme')||'').toLowerCase();
      var mm=window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)');
      var dark = t ? (t==='dark') : (mm && mm.matches);
      d.setAttribute('data-embed-theme', dark ? 'dark' : 'light');
      if(!t && mm && mm.addEventListener){
        mm.addEventListener('change', function(e){ d.setAttribute('data-embed-theme', e.matches?'dark':'light'); });
      }
    }catch(e){}
  })();
</script>
{% endif %}
"""

# Finish configuring the Pipeline tab now that SHARED_HEAD exists (see the early
# pipeline.init_app(app, client) call above for why this is split in two).
try:
    _pipeline_mod.configure(SHARED_HEAD)
except NameError:
    pass  # pipeline module failed to import above; already logged there

# ═════════════════════════════════════════════════════════════════════════════
#  INDEX PAGE
# ═════════════════════════════════════════════════════════════════════════════
INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <title>ASBL · Lead Intelligence</title>
  """ + SHARED_HEAD + """
</head>
<body>
<div class="page-index">

  <!-- ── Top Bar ── -->
  <header class="topbar">
    <div style="display:flex;align-items:center;gap:0;">
      <div class="topbar-logo" onclick="location.href='/'" style="cursor:pointer;" title="Go to Leads">
        <div class="logo-mark">AS</div>
        <div class="logo-text">
          <span class="logo-primary">ASBL</span>
          <span class="logo-sub">Lead Intelligence Platform <span style="font-size:9px;opacity:0.6;letter-spacing:0.05em;">build&nbsp;worker-prep</span></span>
        </div>
      </div>
      {% if not embed %}
      <nav class="nav-links">
        <a href="/" class="nav-link active">Leads</a>
        <a href="/bulk-calls" class="nav-link">Bulk Calls</a>
        <a href="/qa" class="nav-link">QA</a>
      <a href="/pipeline" class="nav-link">Pipeline</a>
      <a href="/main-dashboard" class="nav-link">Main Dashboard</a>
      </nav>
      {% endif %}
    </div>
    <div style="display:flex;align-items:center;gap:10px;">
      <span id="last-updated" style="font-family:'DM Mono',monospace;font-size:10px;color:var(--text-dim);"></span>
      <button class="btn" id="refresh-btn" onclick="loadData(true)">
        <span id="refresh-icon" style="font-size:14px;">↻</span>
        Refresh
      </button>
      {% if not embed %}
      <button class="btn btn-accent" id="sync-btn" onclick="syncNow()" style="position:relative;">
        <span id="sync-icon" style="font-size:14px;">⟳</span>
        <span id="sync-label">Sync Now</span>
      </button>
      <a href="/api/export" id="export-btn" class="btn" style="text-decoration:none;" onclick="handleExport(event)">
        <span style="font-size:14px;">↓</span>
        Download Excel
      </a>
      {% endif %}

      <span style="font-size:11px;color:var(--text-muted);letter-spacing:0.04em;">{{ user_name }}</span>
      <a href="/api/logout" class="btn" style="text-decoration:none;">Logout</a>
    </div>
  </header>

  <!-- ── Stat Strip ── -->
  <div class="stat-strip" id="stat-strip">
    <div class="stat-pill"><span class="stat-pill-value" id="s-total">—</span><span class="stat-pill-label">Total Leads</span></div>
    <div class="stat-pill"><span class="stat-pill-value" id="s-web">—</span><span class="stat-pill-label">Website Active</span></div>
    <div class="stat-pill"><span class="stat-pill-value" id="s-wa">—</span><span class="stat-pill-label">WhatsApp</span></div>
    <div class="stat-pill"><span class="stat-pill-value" id="s-calls">—</span><span class="stat-pill-label">Calls Answered</span></div>
  </div>

  <!-- ── Search ── -->
  <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
    <div class="search-wrap" style="flex:1;min-width:220px;">
      <span class="search-icon">⌕</span>
      <input class="search-input" id="search" type="text"
        placeholder="Search by name, phone, source, campaign…"
        oninput="filterTable()" />
    </div>
    <div style="display:flex;align-items:center;gap:6px;white-space:nowrap;">
      <span style="font-size:11px;letter-spacing:0.08em;text-transform:uppercase;color:var(--text-muted);">Born</span>
      <input type="date" id="born-from" onchange="filterTable()"
        style="background:var(--surface);border:1px solid var(--border);border-radius:7px;padding:7px 10px;font-family:'DM Sans',sans-serif;font-size:12px;color:var(--text);outline:none;cursor:pointer;" />
      <span style="font-size:11px;color:var(--text-dim);">→</span>
      <input type="date" id="born-to" onchange="filterTable()"
        style="background:var(--surface);border:1px solid var(--border);border-radius:7px;padding:7px 10px;font-family:'DM Sans',sans-serif;font-size:12px;color:var(--text);outline:none;cursor:pointer;" />
      <button onclick="document.getElementById('born-from').value='';document.getElementById('born-to').value='';filterTable();"
        style="background:none;border:1px solid var(--border);border-radius:7px;padding:7px 10px;font-size:11px;color:var(--text-muted);cursor:pointer;letter-spacing:0.04em;">Clear</button>
    </div>
  </div>

  <!-- ── Flag Filter Bar ── -->
  <div class="flag-bar">
    <button class="flag-btn active" id="flag-filter-all"     onclick="setFlagFilter('all')">All</button>
    <button class="flag-btn"        id="flag-filter-genuine"    onclick="setFlagFilter('genuine')">🟢 Genuine</button>
    <button class="flag-btn"        id="flag-filter-spam"       onclick="setFlagFilter('spam')">🔴 Spam</button>
    <button class="flag-btn"        id="flag-filter-test"       onclick="setFlagFilter('test')">🔵 Test</button>
    <button class="flag-btn"        id="flag-filter-unclear"        onclick="setFlagFilter('unclear')">🟡 Unclear</button>
    <button class="flag-btn"        id="flag-filter-unreviewed"     onclick="setFlagFilter('unreviewed')">○ Unreviewed</button>
  </div>
  <div class="flag-summary" id="flag-summary"></div>

  <!-- ── Select toggle (admin only — reveals selection checkboxes) ── -->
  <div style="margin:0 0 14px;">
    <button id="bulk-select-toggle" onclick="toggleSelectMode()" style="display:none;background:var(--surface);border:1px solid var(--border);border-radius:7px;padding:8px 16px;font-family:'DM Sans',sans-serif;font-size:13px;color:var(--text);cursor:pointer;letter-spacing:0.03em;">☑ Select</button>
    <button id="dedupe-view-toggle" onclick="toggleDedupeView()" style="background:var(--surface);border:1px solid var(--border);border-radius:7px;padding:8px 16px;font-family:'DM Sans',sans-serif;font-size:13px;color:var(--text);cursor:pointer;letter-spacing:0.03em;margin-left:8px;" title="Collapse same-phone leads into one row">⧉ Remove Duplicate</button>
  </div>

  <!-- ── Bulk Action Bar (shown only in select mode) ── -->
  <div id="bulk-bar" style="display:none;align-items:center;gap:14px;margin:0 0 14px;padding:10px 16px;background:var(--surface);border:1px solid var(--accent);border-radius:8px;animation:fadeIn 0.15s ease;">
    <span id="bulk-count" style="font-size:13px;color:var(--text);font-weight:500;letter-spacing:0.03em;">0 selected</span>
    <button id="bulk-trigger-btn" onclick="bulkTriggerCalls()"
      style="background:var(--accent);color:#FAF7F2;border:none;border-radius:7px;padding:8px 18px;font-family:'DM Sans',sans-serif;font-size:13px;font-weight:500;letter-spacing:0.04em;cursor:pointer;transition:opacity 0.15s;"
      onmouseover="this.style.opacity=0.85" onmouseout="this.style.opacity=1">◎ Trigger Calls (0)</button>
    <button onclick="clearBulkSelection()"
      style="background:none;border:1px solid var(--border);border-radius:7px;padding:8px 14px;font-size:12px;color:var(--text-muted);cursor:pointer;letter-spacing:0.04em;">Clear</button>
  </div>

  <!-- ── Table ── -->
  <div class="table-container">
    <table id="leads-table">
      <thead>
        <tr>
          <th id="bulk-th" class="bulk-cell" style="width:30px;text-align:center;">
            <input type="checkbox" id="bulk-select-all" onclick="toggleSelectAll(this)" title="Select all visible" style="cursor:pointer;">
          </th>
          <th style="width:36px;color:var(--text-dim);font-size:10px;">#</th>
          <th>Name</th>
          <th>Lead ID</th>
          <th>Source</th>
          <th>Campaign</th>
          <th>Born Date</th>
          <th>Last Action</th>
          <th style="text-align:right;">Web Msgs</th>
          <th style="text-align:right;">CTAs</th>
          <th style="text-align:right;">WA Depth</th>
          <th style="text-align:right;">Calls</th>
          <th style="text-align:right;position:relative;">
            <span style="display:inline-flex;align-items:center;gap:5px;">
              Pickup
              <span id="pickup-filter-icon" onclick="togglePickupFilter(event)" title="Filter by call status"
                style="cursor:pointer;font-size:12px;color:var(--text-muted);transition:color 0.15s;user-select:none;">⊟</span>
              <span id="pickup-clear-icon" onclick="clearPickupFilter()" title="Clear filter"
                style="display:none;cursor:pointer;font-size:11px;color:var(--accent);font-weight:600;user-select:none;">✕</span>
            </span>
            <!-- Pickup filter popover -->
            <div id="pickup-popover" style="display:none;position:absolute;top:calc(100% + 8px);right:0;z-index:200;
              background:var(--surface);border:1px solid var(--border);border-radius:10px;
              box-shadow:0 8px 24px rgba(0,0,0,0.10);padding:14px 16px 12px;min-width:210px;
              text-align:left;animation:fadeIn 0.15s ease;">
              <div style="font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:var(--text-muted);margin-bottom:10px;font-weight:500;">Call Status</div>
              <label id="pf-triggered"    class="pf-row"><input type="checkbox" value="triggered">    Call triggered</label>
              <label id="pf-notriggered"  class="pf-row"><input type="checkbox" value="notriggered">  No call triggered</label>
              <label id="pf-pickedup"     class="pf-row"><input type="checkbox" value="pickedup">     Picked up</label>
              <label id="pf-neverpicked"  class="pf-row"><input type="checkbox" value="neverpicked">  Never picked up</label>
              <div style="margin-top:12px;border-top:1px solid var(--border);padding-top:10px;">
                <button onclick="applyPickupFilter()" style="width:100%;background:var(--accent);color:#FAF7F2;border:none;
                  border-radius:7px;padding:8px 0;font-family:'DM Sans',sans-serif;font-size:12px;font-weight:500;
                  letter-spacing:0.04em;cursor:pointer;transition:opacity 0.15s;" onmouseover="this.style.opacity=0.85" onmouseout="this.style.opacity=1">
                  Apply
                </button>
              </div>
            </div>
          </th>
          <th style="text-align:center;position:relative;">
            <span style="display:inline-flex;align-items:center;gap:5px;">
              Stage
              <span id="stage-filter-icon" onclick="toggleStageFilter(event)" title="Filter by stage"
                style="cursor:pointer;font-size:12px;color:var(--text-muted);user-select:none;">⊟</span>
              <span id="stage-clear-icon" onclick="clearStageFilter()" title="Clear filter"
                style="display:none;cursor:pointer;font-size:11px;color:var(--accent);font-weight:600;user-select:none;">✕</span>
            </span>
            <!-- Stage filter popover -->
            <div id="stage-popover" style="display:none;position:absolute;top:calc(100% + 8px);left:50%;transform:translateX(-50%);z-index:200;
              background:var(--surface);border:1px solid var(--border);border-radius:10px;
              box-shadow:0 8px 24px rgba(0,0,0,0.10);padding:14px 16px 12px;min-width:190px;
              text-align:left;animation:fadeIn 0.15s ease;">
              <div style="font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:var(--text-muted);margin-bottom:10px;font-weight:500;">Stage</div>
              <label class="pf-row"><input type="checkbox" value="New Lead"> New Lead</label>
              <label class="pf-row"><input type="checkbox" value="Lead Initiated"> Lead Initiated</label>
              <label class="pf-row"><input type="checkbox" value="Pre Site Visit"> Pre Site Visit</label>
              <label class="pf-row"><input type="checkbox" value="Post Site Visit"> Post Site Visit</label>
              <label class="pf-row"><input type="checkbox" value="Not Interested"> Not Interested</label>
              <label class="pf-row"><input type="checkbox" value="__none"> No stage</label>
              <div style="margin-top:12px;border-top:1px solid var(--border);padding-top:10px;">
                <button onclick="applyStageFilter()" style="width:100%;background:var(--accent);color:#FAF7F2;border:none;
                  border-radius:7px;padding:8px 0;font-family:'DM Sans',sans-serif;font-size:12px;font-weight:500;
                  letter-spacing:0.04em;cursor:pointer;">Apply</button>
              </div>
            </div>
          </th>
          <th style="text-align:center;">Flag</th>
          <th></th>
        </tr>
      </thead>
      <tbody id="leads-body">
        <tr class="loading-row">
          <td colspan="16">
            <span class="loading-dot">●</span>
            <span class="loading-dot"> ●</span>
            <span class="loading-dot"> ●</span>
            &nbsp; Loading leads…
          </td>
        </tr>
      </tbody>
    </table>
  </div>

</div>

<script>
/* CSRF: attach the double-submit token (JS-readable cookie) to every mutating
   request. Defense in depth on top of the SameSite=Lax session cookie. */
(function(){
  var _f = window.fetch;
  function _csrf(){ var m = document.cookie.match(/(?:^|; *)csrf_token=([^;]+)/); return m ? decodeURIComponent(m[1]) : ''; }
  // Embed auth: capture the CRM's ?token= from the iframe URL once, then echo it in
  // X-Embed-Token on every API call so the embed authenticates without cookies
  // (a cross-site iframe drops the session cookie). Absent on the normal dashboard.
  var _embedTok = '';
  try { _embedTok = new URLSearchParams(window.location.search).get('token') || ''; } catch(e){}
  window.fetch = function(u, o){
    o = o || {};
    var meth = (o.method || 'GET').toUpperCase();
    if (meth !== 'GET' && meth !== 'HEAD') {
      var t = _csrf();
      if (t) o.headers = Object.assign({}, o.headers || {}, {'X-CSRF-Token': t});
    }
    if (_embedTok) o.headers = Object.assign({}, o.headers || {}, {'X-Embed-Token': _embedTok});
    return _f.call(this, u, o);
  };
})();
let allLeads = [];
let flagFilter = 'all';

// Escape untrusted lead data before it goes into innerHTML. Lead name/source/
// campaign come from external sources (Meta, website forms, WhatsApp, Inncircles)
// and are attacker-influenced → without this, a name like "<img src=x onerror=…>"
// runs script in every staff browser (stored XSS). Escapes &,<,>,",' so it's safe
// in both element and attribute contexts.
function escHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}

async function loadStats() {
  try {
    const r = await fetch('/api/stats?_=' + Date.now());
    const d = await r.json();
    document.getElementById('s-total').textContent  = d.total  ?? '—';
    document.getElementById('s-web').textContent    = d.with_website ?? '—';
    document.getElementById('s-wa').textContent     = d.with_wa ?? '—';
    document.getElementById('s-calls').textContent  = d.with_calls ?? '—';
    // Real cache-freshness (when the background sync last succeeded). Red if stale
    // (>2h) so a frozen cache — like the 2-month silent cron outage — is obvious.
    const el = document.getElementById('last-updated');
    if (el) {
      const lsr = d.last_successful_run ? new Date(d.last_successful_run) : null;
      if (!lsr) { el.textContent = 'Sync status unknown'; el.style.color = 'var(--red)'; }
      else {
        const mins = Math.max(0, Math.round((Date.now() - lsr.getTime()) / 60000));
        const ago = mins < 1 ? 'just now' : mins < 60 ? mins + 'm ago'
                  : Math.floor(mins/60) + 'h ' + (mins%60) + 'm ago';
        const stale = mins > 120;
        // Surface a partial/failed LAST run so it isn't silently shown as healthy.
        const errs = d.last_errors || 0;
        const partial = (d.last_status && d.last_status !== 'success') || errs > 0;
        el.textContent = (stale || partial ? '⚠ ' : '') + 'Leads synced ' + ago
                       + (partial ? ' · ' + errs + ' error' + (errs === 1 ? '' : 's') + ' last run' : '');
        el.style.color = stale ? 'var(--red)' : partial ? 'var(--amber, #b8860b)' : 'var(--text-dim)';
        el.title = stale ? 'The lead cache has not refreshed in over 2 hours — the background sync may be down.'
                 : partial ? ('Last sync completed with errors (status: ' + (d.last_status || '?') + ') — some leads may not be up to date.')
                 : 'When the lead cache was last rebuilt by the background sync';
      }
    }
  } catch(e) { console.error('Stats error', e); }
}

async function fetchWithRetry(url, retries=3, delayMs=2000) {
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 25000); // 25s timeout per attempt
      const r = await fetch(url, { signal: ctrl.signal });
      clearTimeout(timer);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r;
    } catch(e) {
      if (attempt === retries) throw e;
      // Show retry status
      document.getElementById('leads-body').innerHTML =
        `<tr class="loading-row"><td colspan="16" style="padding:32px 28px;">` +
        `<span class="loading-dot">●</span><span class="loading-dot"> ●</span><span class="loading-dot"> ●</span>` +
        `&nbsp; Retrying… (attempt ${attempt+1}/${retries})</td></tr>`;
      await new Promise(res => setTimeout(res, delayMs));
    }
  }
}

// Leads this browser has opened, persisted in localStorage so it SURVIVES a full
// page reload (F5 / the Refresh button / Sync Now). Without this, a reload wipes
// in-memory state and the row re-lights before/unless the server "seen" is read.
// The server "seen" is still recorded (sendBeacon) as the cross-device source.
function _localSeenMap() {
  try { return JSON.parse(localStorage.getItem('leadSeenMap') || '{}'); } catch (e) { return {}; }
}
function _markLocalSeen(lid) {
  try { const m = _localSeenMap(); m[lid] = new Date().toISOString(); localStorage.setItem('leadSeenMap', JSON.stringify(m)); } catch (e) {}
}

// Effective "last seen" time for a lead = the latest of: server seen, this
// browser's local seen (localStorage), and the rollout baseline (floor).
function _effSeen(lid, local) {
  const v = window._views || {};
  let best = null;
  const consider = t => { if (t && (!best || new Date(t) > new Date(best))) best = t; };
  if (v.seen) consider(v.seen[lid]);
  consider((local || _localSeenMap())[lid]);
  consider(v.baseline);
  return best;
}

function openLead(lid) {
  // Record "seen" server-side reliably during navigation (sendBeacon isn't
  // cancelled on unload) AND in localStorage so it stays cleared across reloads.
  try { navigator.sendBeacon('/api/lead/' + encodeURIComponent(lid) + '/seen'); } catch (e) {}
  _markLocalSeen(lid);
  window.location = '/lead/' + lid + (EMBED ? '?embed=1' : '');
}

// Returning to the list (incl. back-forward cache restore) → just RE-RENDER from
// the in-memory seen map (which already holds the lead we just opened). No fetch
// here: an async re-fetch could momentarily return before the server "seen" write
// settles and re-light the row ("off then on" flicker). The server write happens
// via sendBeacon in openLead(), and a full reload / the 5-min poll re-syncs it.
window.addEventListener('pageshow', function () {
  if (allLeads && allLeads.length) { filterTable(); }
});

async function loadData(isRefresh=false) {
  if (isRefresh) {
    const icon = document.getElementById('refresh-icon');
    icon.classList.add('spinning');
    setTimeout(() => icon.classList.remove('spinning'), 800);
    document.getElementById('leads-body').innerHTML =
      '<tr class="loading-row"><td colspan="16"><span class="loading-dot">●</span><span class="loading-dot"> ●</span><span class="loading-dot"> ●</span>&nbsp; Loading leads…</td></tr>';
  }
  try {
    const r = await fetchWithRetry('/api/leads');
    // resolved_project/resolved_stage/resolved_reason now come attached to each
    // row from /api/leads itself (lead_identity.py, server-side) — no separate
    // /api/v2/stages-map fetch needed for the Stage column any more.
    allLeads = await r.json();
    // Per-user "seen" map for the new-activity row highlight (best-effort).
    try {
      const vr = await fetch('/api/lead-views?t=' + Date.now(), { cache: 'no-store' });
      if (vr.ok) window._views = await vr.json();
    } catch (e) { /* no highlight if unavailable */ }
    // NOTE: #last-updated shows CACHE freshness (last successful sync), set by
    // loadStats() — not the client fetch time, which hid the frozen-cache outage.
    filterTable();  // handles updateFlagCounts + updateStatsFromLeads internally
  } catch(e) {
    const isTimeout = e.name === 'AbortError';
    document.getElementById('leads-body').innerHTML =
      `<tr><td colspan="16" style="padding:32px 28px;color:var(--red);">` +
      `⚠ ${isTimeout ? 'Request timed out — server may be waking up.' : 'Failed to load leads.'} ` +
      `<span style="cursor:pointer;text-decoration:underline;opacity:0.7;" onclick="loadData(true)">Retry</span>` +
      `</td></tr>`;
  }
}

function escRe(s) {
  return s.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
}

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
}

// ── Last-action activity popup (combined calls + WhatsApp, newest first) ───────
function _durStr(s) { s = Math.round(s || 0); if (!s) return ''; const m = Math.floor(s/60), x = s%60; return m ? `${m}m ${x}s` : `${x}s`; }

function closeActivity() { const m = document.getElementById('act-modal'); if (m) m.style.display = 'none'; }

function _actModal() {
  let m = document.getElementById('act-modal');
  if (!m) {
    m = document.createElement('div');
    m.id = 'act-modal';
    m.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.45);display:none;align-items:flex-start;justify-content:center;z-index:9999;padding:40px 16px;';
    m.addEventListener('click', e => { if (e.target === m) closeActivity(); });
    m.innerHTML = `<div style="width:100%;max-width:560px;background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden;max-height:82vh;display:flex;flex-direction:column;">
        <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--border);">
          <div><div id="act-name" style="font-size:15px;font-weight:600;color:var(--text);"></div>
            <div id="act-sub" style="font-size:12px;color:var(--text-muted);font-family:'DM Mono',monospace;margin-top:2px;"></div></div>
          <span onclick="closeActivity()" style="cursor:pointer;font-size:22px;color:var(--text-muted);line-height:1;">&times;</span>
        </div>
        <div id="act-body" style="overflow-y:auto;"></div>
      </div>`;
    document.body.appendChild(m);
  }
  return m;
}

function _renderActBody(items) {
  const body = document.getElementById('act-body');
  if (!items.length) { body.innerHTML = '<div style="padding:28px;text-align:center;color:var(--text-muted);">No calls or WhatsApp activity.</div>'; return; }
  const esc = s => String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  let html = '', lastDay = null;
  items.forEach(it => {
    const d = new Date(it.at);
    const day = d.toLocaleDateString('en-IN', { day:'numeric', month:'short', year:'numeric' });
    const tm  = d.toLocaleTimeString('en-IN', { hour:'2-digit', minute:'2-digit', hour12:true });
    if (day !== lastDay) { html += `<div style="padding:8px 18px;font-size:10px;letter-spacing:0.06em;text-transform:uppercase;color:var(--text-dim);background:var(--surface-2);">${day}</div>`; lastDay = day; }
    const isCall = it.t === 'call';
    const bd = isCall ? '#EF9F27' : '#5DCAA5', bg = isCall ? '#FAEEDA' : '#E1F5EE', fg = isCall ? '#854F0B' : '#0F6E56';
    const detail = isCall
      ? `<div style="font-size:13px;font-weight:500;color:var(--text);">${it.outcome}</div>` + (it.sub ? `<div style="font-size:12px;color:var(--text-muted);margin-top:2px;">${it.sub}</div>` : '')
      : `<div style="font-size:12px;font-weight:500;color:var(--text-muted);">${it.dir}</div><div style="font-size:13px;color:var(--text);margin-top:2px;line-height:1.5;">${esc(it.text)}</div>`;
    html += `<div style="display:flex;gap:12px;padding:12px 18px;border-left:3px solid ${bd};border-bottom:1px solid var(--border);">
        <div style="flex:0 0 74px;"><span style="font-size:10px;font-weight:600;padding:2px 8px;border-radius:11px;background:${bg};color:${fg};">${isCall ? 'Call' : 'WhatsApp'}</span></div>
        <div style="flex:1;min-width:0;">${detail}</div>
        <div style="flex:0 0 auto;font-family:'DM Mono',monospace;font-size:11px;color:var(--text-dim);white-space:nowrap;">${tm}</div>
      </div>`;
  });
  body.innerHTML = html;
}

async function openActivity(lid) {
  const m = _actModal();
  const lead = (allLeads || []).find(x => (x.lead_id || x._id) === lid) || {};
  document.getElementById('act-name').textContent = lead.lead_name || 'Lead';
  document.getElementById('act-sub').textContent = (lead.lead_phone_number || '') + ' · full activity, newest first';
  document.getElementById('act-body').innerHTML = '<div style="padding:28px;text-align:center;color:var(--text-muted);">Loading…</div>';
  m.style.display = 'flex';
  const ANS = ['CONNECTED','AUTO_CALLBACK','PRE_SITE','NOT_INTERESTED','DROPPED','VIRTUAL_TOUR'];
  const NA  = ['NO_ANSWER','BUSY'];
  let items = [];
  try {
    const [dRes, wRes] = await Promise.all([
      fetch('/api/lead/' + encodeURIComponent(lid)),
      fetch('/api/lead/' + encodeURIComponent(lid) + '/whatsapp-live')
    ]);
    const d = dRes.ok ? await dRes.json() : {};
    const w = wRes.ok ? await wRes.json() : {};
    ((d.ai_calls && d.ai_calls.call_conversations) || []).forEach(c => {
      const oc = c.call_outcome || '';
      const outcome = ANS.includes(oc) ? 'Answered' : (NA.includes(oc) ? 'Not answered' : 'Not connected');
      const sub = [];
      if (c.call_direction) sub.push(c.call_direction.toLowerCase() === 'inbound' ? 'Inbound' : 'Outbound');
      if (c.duration_secs) sub.push(_durStr(c.duration_secs));
      if (c.recording_url) sub.push('recording available');
      items.push({ t:'call', at:c.started_at, outcome, sub:sub.join(' · ') });
    });
    ((w.user_conversation_history) || []).forEach(msg => {
      items.push({ t:'wa', at:msg.date, dir:(msg.type||'').toLowerCase() === 'inbound' ? 'Lead' : 'Anandita', text:msg.message || '' });
    });
  } catch (e) { /* render whatever we have */ }
  items = items.filter(x => x.at).sort((a, b) => new Date(b.at) - new Date(a.at));
  _renderActBody(items);
}

function updateFlagCounts(leads) {
  const src = leads || allLeads;
  const total        = src.length;
  const genuine      = src.filter(l => l.flag === 'genuine').length;
  const spam         = src.filter(l => l.flag === 'spam').length;
  const test         = src.filter(l => l.flag === 'test').length;
  const unclear      = src.filter(l => l.flag === 'unclear').length;
  const unreviewed   = src.filter(l => !l.flag).length;
  document.getElementById('flag-filter-all').textContent            = `All (${total})`;
  document.getElementById('flag-filter-genuine').textContent        = `🟢 Genuine (${genuine})`;
  document.getElementById('flag-filter-spam').textContent           = `🔴 Spam (${spam})`;
  document.getElementById('flag-filter-test').textContent           = `🔵 Test (${test})`;
  document.getElementById('flag-filter-unclear').textContent        = `🟡 Unclear (${unclear})`;
  document.getElementById('flag-filter-unreviewed').textContent     = `○ Unreviewed (${unreviewed})`;
}

// Holds the date+search filtered set so updateFlagSummary stays in sync with filterTable
let _filteredForStats = null;

// isFiltered=true means a date/search filter is active — only then overwrite the
// server-accurate total that loadStats() already placed in s-total.
function updateStatsFromLeads(leads, isFiltered) {
  const withWeb   = leads.filter(l => (l.website?.messages_typed || 0) > 0 || (l.website?.cta_triggered || 0) > 0).length;
  const withWa    = leads.filter(l => (l.whatsapp?.user_conversation_depth || 0) > 0).length;
  const withCalls = leads.filter(l => (l.ai_calls?.calls_answered || 0) > 0).length;
  if (isFiltered) {
    // Show filtered count in total pill so the user can see how many leads match
    document.getElementById('s-total').textContent = leads.length;
  }
  // Always update the activity stats from the current (possibly filtered) set
  document.getElementById('s-web').textContent   = withWeb;
  document.getElementById('s-wa').textContent    = withWa;
  document.getElementById('s-calls').textContent = withCalls;
}

function updateFlagSummary() {
  // Use the date+search filtered set so counts match what the filter buttons show
  const src     = _filteredForStats || allLeads;
  const genuine = src.filter(l => l.flag === 'genuine').length;
  const spam    = src.filter(l => l.flag === 'spam').length;
  const test    = src.filter(l => l.flag === 'test').length;
  const el = document.getElementById('flag-summary');
  if (!el) return;
  if (flagFilter === 'all') { el.textContent = ''; return; }
  if (flagFilter === 'test') {
    el.textContent = `${test} test lead${test !== 1 ? 's' : ''}`;
    return;
  }
  if (flagFilter === 'unclear') {
    const unclear = src.filter(l => l.flag === 'unclear').length;
    el.textContent = `${unclear} unclear lead${unclear !== 1 ? 's' : ''} — reviewed but insufficient data to decide`;
    return;
  }
  if (flagFilter === 'unreviewed') {
    const unreviewed = src.filter(l => !l.flag).length;
    el.textContent = `${unreviewed} unreviewed lead${unreviewed !== 1 ? 's' : ''}`;
    return;
  }
  const realTotal = src.filter(l => l.flag !== 'test').length;
  const count = flagFilter === 'genuine' ? genuine : spam;
  const ratio = realTotal > 0 ? ((count / realTotal) * 100).toFixed(1) : '0.0';
  const label = flagFilter === 'genuine' ? 'genuine' : 'spam';
  el.textContent = `${count} ${label} lead${count !== 1 ? 's' : ''} out of ${realTotal} real leads · ${ratio}% ${label} ratio`;
}

function setFlagFilter(f) {
  flagFilter = f;
  document.querySelectorAll('.flag-btn').forEach(b => { b.className = 'flag-btn'; });
  const btn = document.getElementById('flag-filter-' + f);
  if (btn) {
    btn.classList.add(f === 'genuine' ? 'active-genuine' : f === 'spam' ? 'active-spam' : f === 'test' ? 'active-test' : f === 'unclear' ? 'active-unclear' : 'active');

  }
  updateFlagSummary();
  filterTable();
}

function flagLead(event, leadId, currentFlag) {
  event.stopPropagation();
  const next = currentFlag === 'genuine' ? 'spam' : currentFlag === 'spam' ? 'test' : currentFlag === 'test' ? 'unclear' : currentFlag === 'unclear' ? null : 'genuine';
  fetch(`/api/lead/${leadId}/flag`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({flag: next})
  }).then(r => r.json()).then(data => {
    // update in allLeads array
    const lead = allLeads.find(l => (l.lead_id || l._id) === leadId);
    if (lead) { lead.flag = next || undefined; }
    updateFlagSummary();
    filterTable();  // handles updateFlagCounts + updateStatsFromLeads internally
  });
}

const EMBED = {{ 'true' if embed else 'false' }};
// embed is a hard override, not another role check: the CRM iframe must never
// see admin affordances even when the underlying session really is an admin.
const IS_ADMIN = {{ 'true' if (user_role == 'admin' and not embed) else 'false' }};

// ── Bulk-trigger (admin only) ────────────────────────────────────────────────
// Selection is OFF by default. The "Select" button toggles selection mode,
// which reveals the per-row checkboxes (via the .selecting class on the table).
if (IS_ADMIN) {
  document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('bulk-select-toggle');
    if (btn) btn.style.display = '';
  });
}

function toggleSelectMode() {
  const table = document.getElementById('leads-table');
  const btn = document.getElementById('bulk-select-toggle');
  if (!table) return;
  const on = !table.classList.contains('selecting');
  table.classList.toggle('selecting', on);
  if (btn) {
    btn.classList.toggle('active', on);
    btn.textContent = on ? '✕ Cancel' : '☑ Select';
  }
  if (!on) clearBulkSelection();   // leaving select mode → clear + hide bar
}

// ── "Remove Duplicate" view toggle (NON-destructive) ────────────────────────
// ON  → collapse to one row per phone (project badge hidden).
// OFF → normal: same phone + different projects shown as separate rows.
let _dedupeView = false;
function toggleDedupeView() {
  _dedupeView = !_dedupeView;
  const btn = document.getElementById('dedupe-view-toggle');
  if (btn) {
    btn.classList.toggle('active', _dedupeView);
    btn.textContent = _dedupeView ? '⧉ Show All (ungroup)' : '⧉ Remove Duplicate';
  }
  filterTable();   // re-render with/without phone collapsing
}

function selectedBulkPhones() {
  return Array.from(document.querySelectorAll('.bulk-row-cb:checked'))
    .map(cb => cb.getAttribute('data-phone'))
    .filter(p => p && p !== '—');
}

function updateBulkBar() {
  const bar = document.getElementById('bulk-bar');
  if (!bar) return;
  const phones = selectedBulkPhones();
  const n = phones.length;
  bar.style.display = n > 0 ? 'flex' : 'none';
  const cnt = document.getElementById('bulk-count');
  if (cnt) cnt.textContent = n + ' selected';
  const btn = document.getElementById('bulk-trigger-btn');
  if (btn) btn.textContent = '◎ Trigger Calls (' + n + ')';
}

function toggleSelectAll(master) {
  document.querySelectorAll('.bulk-row-cb').forEach(cb => { cb.checked = master.checked; });
  updateBulkBar();
}

function clearBulkSelection() {
  document.querySelectorAll('.bulk-row-cb').forEach(cb => { cb.checked = false; });
  const master = document.getElementById('bulk-select-all');
  if (master) master.checked = false;
  updateBulkBar();
}

// The homepage table is keyed by loft _id, but bulk-trigger needs plids.
// Resolve each selected phone → plid via the live crm-by-phone endpoint,
// then POST the collected plids to /api/v2/leads/bulk-trigger.
async function bulkTriggerCalls() {
  const phones = selectedBulkPhones();
  if (!phones.length) return;
  const btn = document.getElementById('bulk-trigger-btn');
  if (btn) { btn.disabled = true; btn.style.opacity = '0.6'; btn.textContent = 'Resolving…'; }

  const plids = [];
  let notInCrm = 0;
  await Promise.all(phones.map(async (ph) => {
    try {
      const r = await fetch('/api/v2/crm-by-phone?phone=' + encodeURIComponent(ph));
      const d = await r.json();
      if (d && d.exists && d.plid) plids.push(d.plid);
      else notInCrm++;
    } catch (e) { notInCrm++; }
  }));

  if (!plids.length) {
    alert('None of the selected leads are in the CRM yet, so no calls were triggered.');
    if (btn) { btn.disabled = false; btn.style.opacity = '1'; }
    updateBulkBar();
    return;
  }

  if (btn) btn.textContent = 'Triggering…';
  try {
    const r = await fetch('/api/v2/leads/bulk-trigger', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ plids })
    });
    const d = await r.json();
    const triggered = (d && d.triggered != null) ? d.triggered : 0;
    const errs = (d && Array.isArray(d.errors)) ? d.errors : [];
    let msg = triggered + ' call' + (triggered === 1 ? '' : 's') + ' triggered.';
    if (notInCrm) msg += ' ' + notInCrm + ' lead' + (notInCrm === 1 ? '' : 's') + ' not in CRM (skipped).';
    if (errs.length) {
      const joined = errs.map(e => typeof e === 'string' ? e : (e.error || JSON.stringify(e))).join('; ');
      if (/not configured/i.test(joined)) msg += ' Calling not yet live (activates at cutover).';
      else msg += ' ' + errs.length + ' error' + (errs.length === 1 ? '' : 's') + ': ' + joined;
    }
    alert(msg);
  } catch (e) {
    alert('Bulk trigger failed: ' + e.message);
  }
  if (btn) { btn.disabled = false; btn.style.opacity = '1'; }
  clearBulkSelection();
}

function renderFlagCell(lid, flag) {
  if (!IS_ADMIN) {
    // read-only for non-admin
    if (flag === 'genuine')      return `<span class="flag-genuine"      style="cursor:default;">🟢 Genuine</span>`;
    if (flag === 'spam')         return `<span class="flag-spam"         style="cursor:default;">🔴 Spam</span>`;
    if (flag === 'test')         return `<span class="flag-test"         style="cursor:default;">🔵 Test</span>`;
    if (flag === 'unclear')      return `<span class="flag-unclear"      style="cursor:default;">🟡 Unclear</span>`;
    return `<span class="flag-none" style="cursor:default;pointer-events:none;">—</span>`;
  }
  if (flag === 'genuine')      return `<span class="flag-genuine"      onclick="flagLead(event,'${lid}','genuine')"      title="Click to mark Spam">🟢 Genuine</span>`;
  if (flag === 'spam')         return `<span class="flag-spam"         onclick="flagLead(event,'${lid}','spam')"         title="Click to mark Test">🔴 Spam</span>`;
  if (flag === 'test')         return `<span class="flag-test"         onclick="flagLead(event,'${lid}','test')"         title="Click to mark Unclear">🔵 Test</span>`;
  if (flag === 'unclear')      return `<span class="flag-unclear"      onclick="flagLead(event,'${lid}','unclear')"      title="Click to clear">🟡 Unclear</span>`;
  return `<span class="flag-none" onclick="flagLead(event,'${lid}',null)" title="Click to mark Genuine">Flag</span>`;
}

// ── Pickup filter ────────────────────────────────────────────────────────────
let pickupFilter = [];  // applied selections
let pickupPending = []; // in-popover selections before Apply

function togglePickupFilter(e) {
  e.stopPropagation();
  const pop = document.getElementById('pickup-popover');
  if (pop.style.display === 'none') {
    // sync pending checkboxes to current applied state
    pickupPending = [...pickupFilter];
    pop.querySelectorAll('input[type=checkbox]').forEach(cb => {
      cb.checked = pickupPending.includes(cb.value);
    });
    pop.style.display = 'block';
  } else {
    pop.style.display = 'none';
  }
}

function applyPickupFilter() {
  const pop = document.getElementById('pickup-popover');
  pickupFilter = [];
  pop.querySelectorAll('input[type=checkbox]').forEach(cb => {
    if (cb.checked) pickupFilter.push(cb.value);
  });
  pop.style.display = 'none';
  updatePickupIcons();
  filterTable();
}

function clearPickupFilter() {
  pickupFilter = [];
  pickupPending = [];
  const pop = document.getElementById('pickup-popover');
  pop.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = false);
  pop.style.display = 'none';
  updatePickupIcons();
  filterTable();
}

function updatePickupIcons() {
  const icon  = document.getElementById('pickup-filter-icon');
  const clear = document.getElementById('pickup-clear-icon');
  if (pickupFilter.length > 0) {
    icon.style.color = 'var(--accent)';
    icon.textContent = '◼';
    clear.style.display = 'inline';
  } else {
    icon.style.color = 'var(--text-muted)';
    icon.textContent = '⊟';
    clear.style.display = 'none';
  }
}

// Close popover on outside click
document.addEventListener('click', function(e) {
  const pop = document.getElementById('pickup-popover');
  if (pop && pop.style.display !== 'none') {
    if (!pop.contains(e.target) && e.target.id !== 'pickup-filter-icon') {
      // discard pending, revert to last applied
      pop.style.display = 'none';
    }
  }
  const sp = document.getElementById('stage-popover');
  if (sp && sp.style.display !== 'none') {
    if (!sp.contains(e.target) && e.target.id !== 'stage-filter-icon') sp.style.display = 'none';
  }
});

// ── Stage filter (header popover, mirrors the Pickup filter) ───────────────────
let stageFilter = [];  // applied stage selections (union)
function toggleStageFilter(e) {
  e.stopPropagation();
  const pop = document.getElementById('stage-popover');
  if (pop.style.display === 'none') {
    pop.querySelectorAll('input[type=checkbox]').forEach(cb => { cb.checked = stageFilter.includes(cb.value); });
    pop.style.display = 'block';
  } else {
    pop.style.display = 'none';
  }
}
function applyStageFilter() {
  const pop = document.getElementById('stage-popover');
  stageFilter = [];
  pop.querySelectorAll('input[type=checkbox]').forEach(cb => { if (cb.checked) stageFilter.push(cb.value); });
  pop.style.display = 'none';
  updateStageIcons();
  filterTable();
}
function clearStageFilter() {
  stageFilter = [];
  const pop = document.getElementById('stage-popover');
  if (pop) { pop.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = false); pop.style.display = 'none'; }
  updateStageIcons();
  filterTable();
}
function updateStageIcons() {
  const icon = document.getElementById('stage-filter-icon');
  const clear = document.getElementById('stage-clear-icon');
  if (!icon) return;
  if (stageFilter.length > 0) { icon.style.color = 'var(--accent)'; icon.textContent = '◼'; clear.style.display = 'inline'; }
  else { icon.style.color = 'var(--text-muted)'; icon.textContent = '⊟'; clear.style.display = 'none'; }
}

// Derive a lead's project the SAME way the row builder does (used by the Stage
// column render AND the Stage filter, so they can never disagree).
// Canonical values, computed server-side by lead_identity.py (the SAME rules
// pipeline.py's Booked Site Visit uses) and attached to each /api/leads row as
// resolved_project/resolved_stage — this is no longer an independent client-side
// definition of the project/stage rule, just a read of what the backend decided.
function _leadProject(l){ return l.resolved_project || ''; }
function _leadStage(l){ return l.resolved_stage || null; }

function filterTable() {
  const q = document.getElementById('search').value.trim().toLowerCase();
  const bornFrom = document.getElementById('born-from').value;  // "YYYY-MM-DD"
  const bornTo   = document.getElementById('born-to').value;
  const body = document.getElementById('leads-body');

  // Phone match is digit-normalized: stored phones are last-10 digits, so a search
  // that includes the country code (+91.../91...) is reduced to its last-10 too.
  const qDigits = q.replace(/\\D/g,'');
  const qL10 = qDigits.slice(-10);
  let filtered = q ? allLeads.filter(l => {
    const phoneDigits = (l.lead_phone_number||'').replace(/\\D/g,'');
    const phoneMatch = qDigits && (phoneDigits.slice(-10).includes(qL10) || phoneDigits.includes(qDigits));
    return (l.lead_name||'').toLowerCase().includes(q) ||
      phoneMatch ||
      (l.lead_source||'').toLowerCase().includes(q) ||
      (l.campaign_name||'').toLowerCase().includes(q) ||
      // Lead ID match: Inncircles source id, or the PLID (ZOHO- stripped) shown as Lead ID.
      ((l.lead_source||'').toLowerCase().includes('inncircles') && (l.source_lead_id||'').toLowerCase().includes(q)) ||
      String(l.lead_id || l._id || '').replace(/^ZOHO-/i,'').toLowerCase().includes(q);
  }) : allLeads;

  if (bornFrom) {
    const from = new Date(bornFrom);
    filtered = filtered.filter(l => l.lead_created_at && new Date(l.lead_created_at) >= from);
  }
  if (bornTo) {
    const to = new Date(bornTo); to.setHours(23,59,59,999);
    filtered = filtered.filter(l => l.lead_created_at && new Date(l.lead_created_at) <= to);
  }

  // Update stat cards and flag button counts based on search+date filtered set
  // (before flag/pickup filter so counts always reflect the date-scoped universe)
  const isFiltered = !!(q || bornFrom || bornTo);
  _filteredForStats = filtered;  // cache for updateFlagSummary
  updateFlagCounts(filtered);
  updateStatsFromLeads(filtered, isFiltered);

  if (flagFilter === 'genuine')           filtered = filtered.filter(l => l.flag === 'genuine');
  else if (flagFilter === 'spam')         filtered = filtered.filter(l => l.flag === 'spam');
  else if (flagFilter === 'test')         filtered = filtered.filter(l => l.flag === 'test');
  else if (flagFilter === 'unclear')      filtered = filtered.filter(l => l.flag === 'unclear');
  else if (flagFilter === 'unreviewed')   filtered = filtered.filter(l => !l.flag);

  // Stage filter — union of checked stages, resolved live (same as the column).
  if (stageFilter.length > 0) {
    filtered = filtered.filter(l => {
      const s = _leadStage(l);
      return stageFilter.some(sel => sel === '__none' ? !s : (s === sel));
    });
  }

  // Pickup filter — union of all checked options
  if (pickupFilter.length > 0) {
    filtered = filtered.filter(l => {
      const triggered = (l.ai_calls?.outbound_calls_triggered || 0) > 0;
      const answered  = (l.ai_calls?.calls_answered || 0) > 0;
      return pickupFilter.some(f => {
        if (f === 'triggered')   return triggered;
        if (f === 'notriggered') return !triggered;
        if (f === 'pickedup')    return answered;
        if (f === 'neverpicked') return triggered && !answered;
        return false;
      });
    });
  }

  // "Remove Duplicate" view: collapse to ONE row per phone (last 10 digits).
  // Non-destructive — just a display grouping. Keeps the best row per phone
  // (flagged first, then most activity). Project badge is hidden in this view.
  if (_dedupeView) {
    const _l10 = (s) => { const d = String(s||'').replace(/\\D/g,''); return d.length>=10 ? d.slice(-10) : null; };
    const _act = (l) => ((l.website?.messages_typed||0)+(l.website?.cta_triggered||0)+(l.whatsapp?.user_conversation_depth||0)+(l.ai_calls?.calls_answered||0)+(l.ai_calls?.outbound_calls_triggered||0));
    const byPhone = new Map();
    for (const l of filtered) {
      const k = _l10(l.lead_phone_number) || ('__'+(l.lead_id||Math.random()));
      const cur = byPhone.get(k);
      if (!cur) { byPhone.set(k, l); continue; }
      // pick the better representative
      const better = ((l.flag?1:0) - (cur.flag?1:0)) || (_act(l) - _act(cur));
      if (better > 0) byPhone.set(k, l);
    }
    filtered = Array.from(byPhone.values());
  }

  // Remember exactly what's visible (in screen order) so the Excel export can
  // download the filtered set. When nothing is filtered this equals all leads.
  window._visibleLeadIds = filtered.map(l => l.lead_id || l._id).filter(Boolean);
  window._exportFiltered = !!(q || bornFrom || bornTo || flagFilter !== 'all'
                              || (pickupFilter && pickupFilter.length > 0)
                              || (stageFilter && stageFilter.length > 0) || _dedupeView);

  if (filtered.length === 0) {
    body.innerHTML = `
      <tr><td colspan="16">
        <div class="empty-state">
          <div class="empty-state-icon">◎</div>
          <div class="empty-state-text">No leads match your search</div>
        </div>
      </td></tr>`;
    return;
  }

  const _lsm = _localSeenMap();   // parse localStorage once per render, not per row
  body.innerHTML = filtered.map((l, i) => {
    const lid    = l.lead_id || l._id || '';
    // Lead ID: Inncircles source id, else the PLID stripped of its "ZOHO-" prefix
    // (ZOHO-1734-SPECTRA → 1734-SPECTRA). Every lead now shows one — no more "—".
    const leadIdDisplay = ((l.lead_source||'').toLowerCase().includes('inncircles') && l.source_lead_id)
      ? escHtml(String(l.source_lead_id))
      : escHtml(String(l.lead_id || l._id || '').replace(/^ZOHO-/i, ''));
    const name   = escHtml(l.lead_name || '—');
    const phone  = escHtml(l.lead_phone_number || '—');
    const source    = escHtml(l.lead_source || '');
    const camp      = escHtml(l.campaign_name || '');
    // Derive the project (shared with the Stage filter via _leadProject so the
    // Stage column and the filter always agree).
    const project = _leadProject(l);
    const projBadge = (project && !_dedupeView)
      ? `<span class="badge" style="background:rgba(80,110,200,0.1);color:#3a52a0;margin-left:8px;font-size:9px;padding:2px 7px;vertical-align:middle;">${project}</span>`
      : '';
    // Stage cell — canonical resolved_stage/resolved_reason from the backend
    // (lead_identity.py), not a client-side stages-map lookup.
    const stageCell = (() => {
      const st = l.resolved_stage;
      if (!st) return '<span class="td-dim">—</span>';
      const C = {
        'New Lead':        ['var(--surface-2)','var(--text-muted)','var(--border)'],
        'Lead Initiated':  ['rgba(80,110,200,0.12)','#3a52a0','rgba(80,110,200,0.3)'],
        'Pre Site Visit':  ['rgba(40,167,69,0.1)','#1a7a34','rgba(40,167,69,0.3)'],
        'Post Site Visit': ['rgba(39,120,59,0.18)','#15592a','#1a6b30'],
        'Not Interested':  ['rgba(180,60,50,0.1)','#9B2E24','rgba(180,60,50,0.3)'],
      };
      const c = C[st] || C['New Lead'];
      const rs = (st === 'Not Interested' && l.resolved_reason) ? (' · ' + l.resolved_reason) : '';
      return `<span style="display:inline-block;padding:3px 10px;border-radius:20px;background:${c[0]};color:${c[1]};border:1px solid ${c[2]};font-size:10px;font-weight:600;white-space:nowrap;">${st}${rs}</span>`;
    })();
    const createdAt = l.lead_created_at ? (() => {
      const d = new Date(l.lead_created_at);
      const date = d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
      const time = d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: true });
      return `${date}, ${time}`;
    })() : '—';

    // Last action: most recent call/WhatsApp — type pill + date/time, clickable → popup.
    const lastActionCell = (l.last_activity_at && l.last_activity_type)
      ? (() => {
          const isCall = l.last_activity_type === 'call';
          const bg = isCall ? '#FAEEDA' : '#E1F5EE', fg = isCall ? '#854F0B' : '#0F6E56';
          const ic = isCall ? 'call' : 'whatsapp', lbl = isCall ? 'Call' : 'WhatsApp';
          const d = new Date(l.last_activity_at);
          const dt = d.toLocaleDateString('en-IN', { day:'numeric', month:'short' }) + ', ' +
                     d.toLocaleTimeString('en-IN', { hour:'2-digit', minute:'2-digit', hour12:true });
          return `<span class="last-action" onclick="event.stopPropagation();openActivity('${lid}')" title="View full activity" style="display:inline-flex;align-items:center;gap:7px;cursor:pointer;">
              <span style="font-size:10px;font-weight:600;letter-spacing:0.02em;padding:2px 8px;border-radius:11px;background:${bg};color:${fg};white-space:nowrap;">${lbl}</span>
              <span style="font-size:11px;color:var(--text-muted);white-space:nowrap;">${dt}</span>
            </span>`;
        })()
      : '<span class="td-dim">—</span>';

    const webMsgs = l.website?.messages_typed;
    const ctas    = l.website?.cta_triggered;
    const waDepth = l.whatsapp?.user_conversation_depth;
    const waOut   = l.whatsapp?.outbound_count;
    const callsAns= l.ai_calls?.calls_answered;
    const callsTot= l.ai_calls?.outbound_calls_triggered;
    const pickup  = l.ai_calls?.pickup_ratio;

    const fmt = v => (v == null || v === 0) ? '<span class="td-dim">—</span>' : v;
    const fmtPickup = (p, triggered) => {
      if (p == null) return '<span class="td-dim">N/A</span>';  // no answered+not_answered base
      if (p === 0) return '<span class="badge badge-red">0%</span>';  // real 0% (rang, none picked up)
      const pct = Math.round(p * 100);
      const cls = p >= 0.7 ? 'badge-green' : p >= 0.3 ? 'badge-amber' : 'badge-red';
      return `<span class="badge ${cls}">${pct}%</span>`;
    };

    const callsDisplay = (callsAns != null || callsTot != null)
      ? `<span class="font-mono" style="font-size:12px;">${callsAns ?? 0}<span style="color:var(--text-dim)">/${callsTot ?? 0}</span></span>`
      : '<span class="td-dim">—</span>';

    // WA Depth as inbound / outbound (customer→us / us→customer), NOT a ratio.
    const waDisplay = (waDepth != null || waOut != null)
      ? `<span class="font-mono" style="font-size:12px;" title="inbound (customer) / outbound (sent)">${waDepth ?? 0}<span style="color:var(--text-dim)">/${waOut ?? 0}</span></span>`
      : '<span class="td-dim">—</span>';

    const srcBadge  = source ? `<span class="badge badge-source">${source}</span>` : '<span class="td-dim">—</span>';
    const resubCount = l.all_loft_ids ? Math.max(0, l.all_loft_ids.length - 1) : 0;
    const resubBadge = resubCount > 0
      ? `<span title="Resubmitted ${resubCount} time${resubCount>1?'s':''}" style="display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;background:var(--accent);color:var(--surface);font-size:9px;font-weight:700;margin-left:5px;vertical-align:middle;cursor:default;flex-shrink:0;">${resubCount}</span>`
      : '';
    const campBadge = camp
      ? `<span class="badge badge-campaign" title="${camp}">${camp.length > 22 ? camp.slice(0,22)+'…' : camp}</span>${resubBadge}`
      : (resubCount > 0 ? `<span class="td-dim">—</span>${resubBadge}` : '<span class="td-dim">—</span>');

    const bulkCell = IS_ADMIN
      ? `<td class="bulk-cell" style="text-align:center;" onclick="event.stopPropagation();">
           <input type="checkbox" class="bulk-row-cb" data-phone="${phone}" onclick="event.stopPropagation();updateBulkBar();" style="cursor:pointer;">
         </td>`
      : '';
    // New-activity highlight: the lead's latest event is newer than the last time
    // this user opened it (or the rollout baseline, if never opened).
    let _isNew = false;
    const _evt = l.last_event_at;
    if (_evt) {
      const _eff = _effSeen(lid, _lsm);
      _isNew = _eff ? (new Date(_evt) > new Date(_eff)) : true;
    }
    return `<tr class="${_isNew ? 'lead-row-new' : ''}" onclick="openLead('${lid}')" style="animation:fadeInUp 0.2s ease ${Math.min(i,30)*0.015}s both;">
      ${bulkCell}
      <td style="font-family:'DM Mono',monospace;font-size:11px;color:var(--text-dim);text-align:right;padding-right:10px;">${i+1}</td>
      <!-- NOTE: name MUST stay in its own .name-text span and the badge OUTSIDE it.
           The cell truncates; if the badge is placed inside the truncating element,
           long names clip it off-screen (the "project not showing" bug, Jun 2026). -->
      <td class="td-name"><span class="name-text">${name}</span>${projBadge}</td>
      <td style="font-family:'DM Mono',monospace;font-size:11px;color:var(--text-muted);white-space:nowrap;">${leadIdDisplay || '<span class="td-dim">—</span>'}</td>
      <td>${srcBadge}</td>
      <td>${campBadge}</td>
      <td style="font-size:12px;color:var(--text-muted);white-space:nowrap;">${createdAt}</td>
      <td style="white-space:nowrap;">${lastActionCell}</td>
      <td class="td-num">${fmt(webMsgs)}</td>
      <td class="td-num">${fmt(ctas)}</td>
      <td class="td-num">${waDisplay}</td>
      <td class="td-num">${callsDisplay}</td>
      <td style="text-align:right;">${fmtPickup(pickup, callsTot)}</td>
      <td style="text-align:center;">${stageCell}</td>
      <td style="text-align:center;">${renderFlagCell(lid, l.flag)}</td>
      <td><span class="view-btn">View →</span></td>
    </tr>`;
  }).join('');
}

async function syncNow() {
  const btn   = document.getElementById('sync-btn');
  const icon  = document.getElementById('sync-icon');
  const label = document.getElementById('sync-label');

  // Loading state
  btn.disabled = true;
  btn.style.opacity = '0.7';
  icon.style.display = 'inline-block';
  icon.style.animation = 'spin 1s linear infinite';
  label.textContent = 'Syncing…';

  try {
    const res  = await fetch('/api/cron/refresh?_=' + Date.now());
    const data = await res.json();

    // Success state
    icon.style.animation = '';
    icon.textContent = '✓';
    label.textContent = `Synced ${data.refreshed ?? ''} leads`;
    btn.style.opacity = '1';
    btn.style.background = 'var(--accent)';
    btn.style.color = 'var(--surface)';

    // Reload table after sync
    await loadData(true);
    await loadStats();

    // Reset after 4 seconds
    setTimeout(() => {
      icon.textContent = '⟳';
      label.textContent = 'Sync Now';
      btn.style.background = '';
      btn.style.color = '';
      btn.disabled = false;
    }, 4000);

  } catch(e) {
    icon.style.animation = '';
    icon.textContent = '✕';
    label.textContent = 'Failed — retry';
    btn.style.opacity = '1';
    btn.disabled = false;
    setTimeout(() => {
      icon.textContent = '⟳';
      label.textContent = 'Sync Now';
    }, 3000);
  }
}

async function handleExport(e) {
  if (e) e.preventDefault();
  const btn = document.getElementById('export-btn');
  const restore = () => {
    btn.innerHTML = '<span style="font-size:14px;">↓</span> Download Excel';
    btn.style.pointerEvents = '';
    btn.style.opacity = '';
  };
  btn.textContent = '⏳ Generating…';
  btn.style.pointerEvents = 'none';
  btn.style.opacity = '0.7';
  try {
    const isFiltered = !!window._exportFiltered;
    const ids = window._visibleLeadIds || [];
    // Filtered view → send the exact visible leads. No filter → download all.
    const resp = (isFiltered && ids.length)
      ? await fetch('/api/export', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ lead_ids: ids }),
        })
      : await fetch('/api/export');
    if (!resp.ok) throw new Error('server returned ' + resp.status);
    const blob = await resp.blob();
    const cd = resp.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename="?([^"]+)"?/);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = m ? m[1] : 'ASBL_Lead_Report.xlsx';
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    alert('Download failed: ' + (err && err.message ? err.message : err));
  } finally {
    restore();
  }
}

// Refresh is now driven by an external always-on worker (Render), NOT the browser.
loadStats();
loadData();

// Auto-reload leads when user returns to tab (e.g. after visiting a detail page)
// This prevents stale allLeads from causing wrong flag/data results
let _lastFocus = Date.now();
let _pageshoFired = false;  // prevents double-load when pageshow + visibilitychange both fire on bfcache restore

document.addEventListener('visibilitychange', function() {
  if (document.visibilityState === 'visible') {
    if (_pageshoFired) { _pageshoFired = false; return; }  // pageshow already handled this navigation
    const elapsed = Date.now() - _lastFocus;
    if (elapsed > 30000) {  // reload if away for >30 seconds (avoids scroll-reset on quick tab switches)
      loadData(false);
    }
  } else {
    _lastFocus = Date.now();
  }
});
// Also handle browser back-button (bfcache restore) — visibilitychange may not fire
window.addEventListener('pageshow', function(event) {
  if (event.persisted) {
    _pageshoFired = true;  // tell visibilitychange to skip so we don't reload twice
    loadData(false);
  }
});

// Also auto-refresh the VIEW every 5 minutes (silent) — just re-reads the cache
// (which the external worker keeps fresh); does not trigger the backend refresh.
setInterval(function() {
  if (document.visibilityState === 'visible') {
    loadData(false);
    loadStats();
  }
}, 5 * 60 * 1000);

</script>
</body>
</html>
"""

# ═════════════════════════════════════════════════════════════════════════════
#  DETAIL PAGE
# ═════════════════════════════════════════════════════════════════════════════
DETAIL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <title>ASBL · Lead Detail</title>
  """ + SHARED_HEAD + """
  <style>
    body { min-height: 100vh; }
  </style>
</head>
<body>

  <!-- ── Top Bar ── -->
  <header class="topbar">
    <div style="display:flex;align-items:center;gap:0;">
      <div class="topbar-logo" onclick="location.href='/'" style="cursor:pointer;" title="Go to Leads">
        <div class="logo-mark">AS</div>
        <div class="logo-text">
          <span class="logo-primary">ASBL</span>
          <span class="logo-sub">Lead Intelligence Platform <span style="font-size:9px;opacity:0.6;letter-spacing:0.05em;">build&nbsp;worker-prep</span></span>
        </div>
      </div>
      {% if not embed %}
      <nav class="nav-links">
        <a href="/" class="nav-link active">Leads</a>
        <a href="/bulk-calls" class="nav-link">Bulk Calls</a>
        <a href="/qa" class="nav-link">QA</a>
      <a href="/pipeline" class="nav-link">Pipeline</a>
      <a href="/main-dashboard" class="nav-link">Main Dashboard</a>
      </nav>
      {% endif %}
    </div>
    <div style="display:flex;align-items:center;gap:10px;">
      <a href="{{ '/embed/leads' if embed else '/' }}" class="btn" style="text-decoration:none;">← All Leads</a>

      <span style="font-size:11px;color:var(--text-muted);letter-spacing:0.04em;">{{ user_name }}</span>
      <a href="/api/logout" class="btn" style="text-decoration:none;">Logout</a>
    </div>
  </header>

  <!-- ── Lead Header ── -->
  <div id="lead-header" style="min-height:80px;display:flex;align-items:center;padding:24px 28px 0;">
    <div style="color:var(--text-dim);">
      <span class="loading-dot">●</span>
      <span class="loading-dot"> ●</span>
      <span class="loading-dot"> ●</span>
      &nbsp;Loading lead…
    </div>
  </div>

  <!-- ── Tabs ── -->
  <div class="tabs-nav" id="tabs-nav" style="display:none;">
    <button class="tab-btn active" id="tabbtn-website" onclick="switchTab('website',this)">
      <span style="margin-right:6px;">⬡</span> Website
    </button>
    <button class="tab-btn" id="tabbtn-whatsapp" onclick="switchTab('whatsapp',this)">
      <span style="margin-right:6px;">✉</span> WhatsApp
    </button>
    <button class="tab-btn" id="tabbtn-calls" onclick="switchTab('calls',this)">
      <span style="margin-right:6px;">◎</span> AI Calls
    </button>
    <button class="tab-btn" id="tabbtn-crm" onclick="switchTab('crm',this)">
      <span style="margin-right:6px;">⚑</span> CRM
    </button>
  </div>
  <script>
  // Bulk-manual (cold-call) leads have only AI-call data — hide the other tabs
  // and open AI Calls by default so the detail matches the Bulk Calls use-case.
  if (typeof LEAD_ID === 'string' && LEAD_ID.indexOf('BULK-') === 0) {
    document.addEventListener('DOMContentLoaded', function(){
      ['tabbtn-website','tabbtn-whatsapp','tabbtn-crm'].forEach(function(id){
        var b=document.getElementById(id); if(b) b.style.display='none';
      });
      var cb=document.getElementById('tabbtn-calls');
      if(cb){ try{ switchTab('calls', cb); }catch(e){} }
    });
  }
  </script>

  <div id="tab-website" class="tab-content active"></div>
  <div id="tab-whatsapp" class="tab-content"></div>
  <div id="tab-calls" class="tab-content"></div>
  <div id="tab-crm" class="tab-content"></div>

<script>
/* CSRF: attach the double-submit token (JS-readable cookie) to every mutating
   request. Defense in depth on top of the SameSite=Lax session cookie. */
(function(){
  var _f = window.fetch;
  function _csrf(){ var m = document.cookie.match(/(?:^|; *)csrf_token=([^;]+)/); return m ? decodeURIComponent(m[1]) : ''; }
  // Embed auth: capture the CRM's ?token= from the iframe URL once, then echo it in
  // X-Embed-Token on every API call so the embed authenticates without cookies
  // (a cross-site iframe drops the session cookie). Absent on the normal dashboard.
  var _embedTok = '';
  try { _embedTok = new URLSearchParams(window.location.search).get('token') || ''; } catch(e){}
  window.fetch = function(u, o){
    o = o || {};
    var meth = (o.method || 'GET').toUpperCase();
    if (meth !== 'GET' && meth !== 'HEAD') {
      var t = _csrf();
      if (t) o.headers = Object.assign({}, o.headers || {}, {'X-CSRF-Token': t});
    }
    if (_embedTok) o.headers = Object.assign({}, o.headers || {}, {'X-Embed-Token': _embedTok});
    return _f.call(this, u, o);
  };
})();
const LEAD_ID = {{ lead_id|tojson }};
const EMBED = {{ 'true' if embed else 'false' }};
const IS_ADMIN = {{ 'true' if (user_role == 'admin' and not embed) else 'false' }};

function switchTab(name, btn) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
  if (name === 'crm' && !window._crmLoaded) {
    window._crmLoaded = true;
    renderCRM();
  }
}

// ── CRM Tab ──────────────────────────────────────────────────────────────────
const CRM_STAGES = ['New Lead', 'Lead Initiated', 'Pre Site Visit', 'Post Site Visit', 'Not Interested'];

function crmStageColor(stage) {
  if (stage === 'Lead Initiated')   return { bg: 'var(--blue-dim)',  fg: 'var(--blue)',  bd: 'var(--blue)' };
  if (stage === 'Pre Site Visit')   return { bg: 'var(--green-dim)', fg: 'var(--green)', bd: 'var(--green)' };
  if (stage === 'Post Site Visit')  return { bg: 'rgba(39,120,59,0.18)', fg: '#15592a', bd: '#1a6b30' };  // deeper green — visit done
  if (stage === 'Not Interested')   return { bg: 'var(--red-dim)',   fg: 'var(--red)',   bd: 'var(--red)' };
  return { bg: 'var(--surface-2)', fg: 'var(--text-muted)', bd: 'var(--border-2)' };
}

function crmEsc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function crmChip(label, value, color) {
  const v = (value == null || value === '') ? '—' : value;
  return `<div style="display:inline-flex;flex-direction:column;gap:3px;padding:8px 12px;background:var(--surface-2);border:1px solid var(--border);border-radius:6px;min-width:90px;">
    <span style="font-size:9px;letter-spacing:0.1em;text-transform:uppercase;color:var(--text-dim);">${label}</span>
    <span style="font-size:13px;font-weight:500;color:${color || 'var(--text)'};font-family:'DM Mono',monospace;">${crmEsc(v)}</span>
  </div>`;
}

function crmSection(title, inner) {
  return `<div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:18px 20px;margin-bottom:16px;">
    <div style="font-family:'Syne',sans-serif;font-size:13px;font-weight:600;letter-spacing:0.04em;color:var(--text);margin-bottom:14px;text-transform:uppercase;">${title}</div>
    ${inner}
  </div>`;
}

// Readable summary of the currently-scheduled reminders (shown below the pickers).
function crmFmtRem(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  const date = d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
  const time = d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: true });
  return `${date}, ${time}`;
}
function crmScheduledSummary(data) {
  const rows = [];
  const cr = crmFmtRem(data.call_reminder_at);
  const wr = crmFmtRem(data.whatsapp_reminder_at);
  if (cr) rows.push(`<span style="color:var(--accent);">📞 Call</span> &nbsp;${crmEsc(cr)}`);
  if (wr) rows.push(`<span style="color:var(--accent);">✉ WhatsApp</span> &nbsp;${crmEsc(wr)}`);
  if (!rows.length) {
    return `<div style="font-size:12px;color:var(--text-dim);padding:8px 0;">No reminder scheduled yet.</div>`;
  }
  return `<div style="margin-top:2px;padding:10px 14px;background:var(--accent-dim);border:1px solid var(--border);border-radius:7px;">
    <div style="font-size:10px;letter-spacing:0.08em;text-transform:uppercase;color:var(--text-dim);margin-bottom:6px;">Scheduled</div>
    ${rows.map(r => `<div style="font-size:13px;color:var(--text);font-family:'DM Mono',monospace;padding:2px 0;">${r}</div>`).join('')}
  </div>`;
}

function crmToast(msg, ok) {
  let t = document.getElementById('crm-toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'crm-toast';
    t.style.cssText = 'position:fixed;bottom:28px;left:50%;transform:translateX(-50%);z-index:9999;padding:12px 22px;border-radius:8px;font-family:\\'DM Sans\\',sans-serif;font-size:13px;font-weight:500;letter-spacing:0.03em;box-shadow:0 8px 24px rgba(0,0,0,0.15);transition:opacity 0.3s;';
    document.body.appendChild(t);
  }
  t.style.background = ok ? 'var(--green)' : 'var(--red)';
  t.style.color = '#FAF7F2';
  t.textContent = msg;
  t.style.opacity = '1';
  clearTimeout(window._crmToastTimer);
  window._crmToastTimer = setTimeout(() => { t.style.opacity = '0'; }, 3200);
}

async function renderCRM() {
  const host = document.getElementById('tab-crm');
  if (!window._leadPhone) {
    host.innerHTML = `<div style="text-align:center;color:var(--text-muted);padding:48px 0;font-size:14px;">No phone number on this lead — can't look up CRM record.</div>`;
    return;
  }
  host.innerHTML = `<div style="text-align:center;color:var(--text-dim);padding:48px 0;">
    <span class="loading-dot">●</span><span class="loading-dot"> ●</span><span class="loading-dot"> ●</span>&nbsp; Loading CRM…</div>`;

  let data;
  try {
    // Pass lead_id (exact PLID for ZOHO leads) + derived project so the RIGHT
    // project-lead is resolved — a person with SPECTRA + LOFT must not collide.
    const _knownP = ['LOFT','SPECTRA','BROADWAY','LANDMARK','LEGACY'];
    let _proj = '';
    if (typeof LEAD_ID === 'string' && LEAD_ID.startsWith('ZOHO-')) {
      const s = LEAD_ID.split('-').pop().toUpperCase();
      if (_knownP.includes(s)) _proj = s;
    }
    const q = '/api/v2/crm-by-phone?phone=' + encodeURIComponent(window._leadPhone || '')
            + '&lead_id=' + encodeURIComponent(LEAD_ID || '')
            + '&project=' + encodeURIComponent(_proj);
    const r = await fetch(q);
    data = await r.json();
  } catch (e) {
    host.innerHTML = `<div style="text-align:center;color:var(--red);padding:48px 0;font-size:14px;">Failed to load CRM data.</div>`;
    return;
  }

  if (!data || !data.exists) {
    host.innerHTML = `<div style="text-align:center;color:var(--text-muted);padding:48px 0;">
      <div style="font-size:30px;margin-bottom:12px;opacity:0.5;">⚑</div>
      <div style="font-size:14px;">This lead isn't in the CRM yet.</div>
    </div>`;
    return;
  }

  // For ZOHO leads the reliable PLID is encoded in the URL ("ZOHO-1753-BROADWAY" → "1753-BROADWAY").
  // data.plid may be a large Zoho numeric ID that loses precision as a JS float.
  const plid = (typeof LEAD_ID === 'string' && LEAD_ID.startsWith('ZOHO-'))
      ? LEAD_ID.slice(5)
      : (data.plid ? String(data.plid) : '');
  const stage = data.stage || 'New Lead';
  const sc = crmStageColor(stage);

  // ── Pipeline Stage (with reason when Not Interested) ──
  const niReason = data.not_interested_reason || '';
  const isClosed = (stage === 'Not Interested');
  const stagePill = `<span style="display:inline-block;padding:6px 16px;border-radius:20px;background:${sc.bg};color:${sc.fg};border:1px solid ${sc.bd};font-size:13px;font-weight:600;letter-spacing:0.03em;">${crmEsc(stage)}</span>`;
  const reasonBadge = niReason
    ? `<span style="display:inline-flex;align-items:center;gap:5px;padding:5px 12px;border-radius:20px;background:var(--accent-dim);color:var(--accent);border:1px solid var(--border);font-size:12px;font-weight:500;font-family:'DM Mono',monospace;">⌖ ${crmEsc(niReason)}</span>`
    : `<span style="font-size:12px;color:var(--text-dim);">No reason set</span>`;
  // Small text-style "change" link shown right AFTER the current reason.
  const changeReasonLink = `<a onclick="crmChangeStage('${plid}','Not Interested')" title="Change reason"
       style="font-size:11px;color:var(--text-muted);text-decoration:underline;cursor:pointer;">change</a>`;
  let stageInner;
  if (IS_ADMIN) {
    const opts = CRM_STAGES.map(s => `<option value="${crmEsc(s)}" ${s === stage ? 'selected' : ''}>${crmEsc(s)}</option>`).join('');
    // Row 1: stage pill + stage dropdown.  Row 2 (only when closed): reason + small change link.
    stageInner = `
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
        ${stagePill}
        <select id="crm-stage-select" onchange="crmChangeStage('${plid}', this.value)"
          style="background:var(--surface-2);border:1px solid var(--border);border-radius:7px;padding:8px 12px;font-family:'DM Sans',sans-serif;font-size:13px;color:var(--text);cursor:pointer;">
          ${opts}
        </select>
      </div>
      ${data.relay_stage ? `<div style="display:flex;align-items:center;gap:8px;margin-top:10px;">
        <span style="font-size:10px;letter-spacing:0.08em;text-transform:uppercase;color:var(--text-dim);">Relay stage</span>
        <span style="font-size:12px;color:var(--text-muted);font-family:'DM Mono',monospace;">${crmEsc(data.relay_stage)}</span>
      </div>` : ''}
      ${isClosed ? `<div style="display:flex;align-items:center;gap:8px;margin-top:12px;">
        <span style="font-size:10px;letter-spacing:0.08em;text-transform:uppercase;color:var(--text-dim);">Reason</span>
        ${reasonBadge}${changeReasonLink}
      </div>` : ''}`;
  } else {
    stageInner = `<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
      ${stagePill}${isClosed ? reasonBadge : ''}
    </div>`;
  }

  // ── Status indicators ──
  // TERMINAL (Not Interested): a closed lead has NO workflow statuses — they're
  // workflow controls and could imply a trigger. Show ONLY the reason.
  let statusInner;
  if (data.stage === 'Not Interested') {
    statusInner = `
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
        <span style="font-size:13px;color:var(--text-muted);">Closed —</span>
        <span style="font-size:13px;font-weight:600;color:var(--red);">${crmEsc(data.not_interested_reason || 'Not Interested')}</span>
      </div>
      <div style="margin-top:8px;font-size:11px;color:var(--text-dim);">No active workflow. Chatbot &amp; call statuses cleared — nothing will trigger for this lead.</div>`;
  } else {
    statusInner = `<div style="display:flex;gap:10px;flex-wrap:wrap;">
      ${crmChip('Status', data.prd_status || 'NA')}
      ${data.first_engagement_channel ? crmChip('First Channel', data.first_engagement_channel) : ''}
      ${data.bot_enabled != null ? crmChip('Bot', data.bot_enabled ? 'ON' : 'OFF', data.bot_enabled ? 'var(--green)' : 'var(--text-muted)') : ''}
    </div>`;
  }

  // ── AI Call — just the Trigger Call button (metrics removed) ──
  let callInner = '';
  if (IS_ADMIN) {
    callInner = `<button id="crm-call-btn" onclick="crmTriggerCall('${plid}')"
      style="background:var(--accent);color:#FAF7F2;border:none;border-radius:7px;padding:9px 18px;font-family:'DM Sans',sans-serif;font-size:13px;font-weight:500;letter-spacing:0.04em;cursor:pointer;transition:opacity 0.15s;"
      onmouseover="this.style.opacity=0.85" onmouseout="this.style.opacity=1">◎ Trigger Call</button>
    <span id="crm-call-result" style="margin-left:12px;font-size:12px;color:var(--text-muted);"></span>`;
  } else {
    callInner = `<div style="font-size:12px;color:var(--text-dim);">Call actions are admin-only.</div>`;
  }

  // ── Sales Notes (running log: input box on top, saved notes below) ──
  const noteLog = Array.isArray(data.sales_notes_log) ? data.sales_notes_log
                : (data.sales_notes ? [{text: data.sales_notes, at: null}] : []);
  const fmtNoteTs = (iso) => {
    if (!iso) return '';
    const d = new Date(iso); if (isNaN(d.getTime())) return '';
    return d.toLocaleDateString('en-IN',{day:'numeric',month:'short',year:'numeric'})
         + ', ' + d.toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit',hour12:true});
  };
  // Newest first
  const logHtml = noteLog.length
    ? `<div style="margin-top:14px;display:flex;flex-direction:column;gap:8px;">
        ${[...noteLog].reverse().map(e => `
          <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;">
            <div style="font-size:13px;color:var(--text);line-height:1.5;white-space:pre-wrap;">${crmEsc(e.text || '')}</div>
            ${e.at ? `<div style="font-size:10px;color:var(--text-dim);margin-top:6px;font-family:'DM Mono',monospace;">${crmEsc(fmtNoteTs(e.at))}${e.by ? ' · ' + crmEsc(e.by) : ''}</div>` : ''}
          </div>`).join('')}
      </div>`
    : `<div style="margin-top:12px;font-size:12px;color:var(--text-dim);">No notes yet.</div>`;
  let notesInner;
  if (IS_ADMIN) {
    notesInner = `<textarea id="crm-notes" rows="3" placeholder="Add a note…"
      style="width:100%;background:var(--surface-2);border:1px solid var(--border);border-radius:7px;padding:10px 12px;font-family:'DM Sans',sans-serif;font-size:13px;color:var(--text);resize:vertical;line-height:1.5;"></textarea>
      <button onclick="crmSaveNotes('${plid}')"
        style="margin-top:10px;background:var(--surface-2);color:var(--text);border:1px solid var(--border-2);border-radius:7px;padding:8px 18px;font-family:'DM Sans',sans-serif;font-size:12px;font-weight:500;letter-spacing:0.04em;cursor:pointer;">Add Note</button>
      ${logHtml}`;
  } else {
    notesInner = logHtml;
  }

  // ── Follow-up Reminder — INDEPENDENT per channel (Call / WhatsApp) ──
  // datetime-local needs "YYYY-MM-DDTHH:MM"; trim the seconds/zone off the ISO.
  const _dtLocal = (iso) => iso ? String(iso).slice(0, 16) : '';
  const callRem = _dtLocal(data.call_reminder_at);
  const waRem   = _dtLocal(data.whatsapp_reminder_at);
  const remNote = data.reminder_note || '';
  // Minimum selectable = now (local), so past date/times can't be picked.
  const _nowLocal = (() => {
    const n = new Date();
    n.setMinutes(n.getMinutes() - n.getTimezoneOffset());
    return n.toISOString().slice(0, 16);
  })();
  const _inp = "background:var(--surface-2);border:1px solid var(--border);border-radius:7px;padding:8px 12px;font-family:'DM Sans',sans-serif;font-size:13px;color:var(--text);";
  const _btn = "background:var(--surface-2);color:var(--text);border:1px solid var(--border-2,var(--border));border-radius:7px;padding:8px 16px;font-family:'DM Sans',sans-serif;font-size:12px;font-weight:500;letter-spacing:0.04em;cursor:pointer;";
  let remInner;
  if (IS_ADMIN) {
    remInner = `
      <div style="display:flex;flex-direction:column;gap:12px;">
        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
          <span style="font-size:12px;color:var(--text-muted);min-width:120px;">📞 Call reminder</span>
          <input type="datetime-local" id="crm-rem-call" value="${crmEsc(callRem)}" min="${_nowLocal}" style="${_inp}">
          <button onclick="crmSetReminder('${plid}','call')" style="${_btn}">Set Call</button>
          ${callRem ? `<button onclick="crmClearReminder('${plid}','call')" style="${_btn}opacity:0.7;">Clear</button>` : ''}
        </div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
          <span style="font-size:12px;color:var(--text-muted);min-width:120px;">✉ WhatsApp reminder</span>
          <input type="datetime-local" id="crm-rem-wa" value="${crmEsc(waRem)}" min="${_nowLocal}" style="${_inp}">
          <button onclick="crmSetReminder('${plid}','whatsapp')" style="${_btn}">Set WhatsApp</button>
          ${waRem ? `<button onclick="crmClearReminder('${plid}','whatsapp')" style="${_btn}opacity:0.7;">Clear</button>` : ''}
        </div>
        <input type="text" id="crm-rem-note" value="${crmEsc(remNote)}" placeholder="Note (optional, shared)"
          style="${_inp}width:100%;">
        ${crmScheduledSummary(data)}
        <div style="font-size:11px;color:var(--text-dim);">Each channel fires automatically at its set time. Set independently — one, both, or neither.</div>
      </div>`;
  } else {
    const parts = [];
    if (callRem) parts.push('📞 Call: ' + crmEsc(callRem.replace('T', ' ')));
    if (waRem)   parts.push('✉ WhatsApp: ' + crmEsc(waRem.replace('T', ' ')));
    remInner = `<div style="font-size:13px;color:${parts.length ? 'var(--text)' : 'var(--text-dim)'};">${parts.length ? parts.join('<br>') + (remNote ? '<br><span style="color:var(--text-muted);">' + crmEsc(remNote) + '</span>' : '') : 'No reminder set.'}</div>`;
  }

  // ── WhatsApp Bot toggle ──
  let botInner;
  const botOn = !!data.bot_enabled;
  if (IS_ADMIN) {
    botInner = `<div style="display:flex;align-items:center;gap:14px;">
      <span style="font-size:13px;color:var(--text);">Bot is <strong style="color:${botOn ? 'var(--green)' : 'var(--text-muted)'};">${botOn ? 'ENABLED' : 'DISABLED'}</strong></span>
      <button onclick="crmToggleBot('${plid}', ${botOn ? 'false' : 'true'})"
        style="background:${botOn ? 'var(--red-dim)' : 'var(--green-dim)'};color:${botOn ? 'var(--red)' : 'var(--green)'};border:1px solid ${botOn ? 'var(--red)' : 'var(--green)'};border-radius:7px;padding:8px 18px;font-family:'DM Sans',sans-serif;font-size:12px;font-weight:500;letter-spacing:0.04em;cursor:pointer;">${botOn ? 'Disable Bot' : 'Enable Bot'}</button>
    </div>`;
  } else {
    botInner = `<div style="font-size:13px;color:var(--text);">Bot is <strong style="color:${botOn ? 'var(--green)' : 'var(--text-muted)'};">${botOn ? 'ENABLED' : 'DISABLED'}</strong></div>`;
  }

  // ── Last action footer ──
  let footer = '';
  if (data.last_action) {
    footer = `<div style="font-size:11px;color:var(--text-dim);margin-top:4px;text-align:right;">Last action: ${crmEsc(data.last_action)}${data.last_action_time ? ' · ' + fmtDate(data.last_action_time) + ' ' + fmtTs(data.last_action_time) : ''}</div>`;
  }

  host.innerHTML = `<div style="max-width:760px;">
    ${crmSection('Pipeline Stage', stageInner)}
    ${crmSection('Status', statusInner)}
    ${crmSection('AI Call', callInner)}
    ${crmSection('Sales Notes', notesInner)}
    ${crmSection('Follow-up Reminder', remInner)}
    ${crmSection('WhatsApp Bot', botInner)}
    ${footer}
  </div>`;
}

function crmReload() { renderCRM(); }

// Reason picker modal — resolves to the chosen reason, or null if cancelled.
const NOT_INTERESTED_REASONS = ['Not Responding','Declined','Budget','Inventory','Spam','Rural','Not Looking for Property','100% / Pre Launch','Already Purchased','Location','Availability','Handover Timeline','Vastu','Looking for Rent','Looking for Open Plots','Irrelevant Lead','Others','Looking for Job','Exploring'];
function crmAskReason() {
  return new Promise((resolve) => {
    const ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(20,15,8,0.45);display:flex;align-items:center;justify-content:center;z-index:9999;';
    const opts = NOT_INTERESTED_REASONS.map(r => `<option value="${r}">${r}</option>`).join('');
    ov.innerHTML = `
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px 26px;width:360px;max-width:90vw;box-shadow:0 12px 40px rgba(0,0,0,0.25);">
        <div style="font-family:'Syne',sans-serif;font-size:15px;font-weight:700;color:var(--text);margin-bottom:6px;">Mark as Not Interested</div>
        <div style="font-size:12px;color:var(--text-muted);margin-bottom:16px;">Select a reason — this closes the lead and stops all triggers.</div>
        <select id="crm-reason-pick" style="width:100%;background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-family:'DM Sans',sans-serif;font-size:13px;color:var(--text);margin-bottom:18px;">${opts}</select>
        <div style="display:flex;gap:10px;justify-content:flex-end;">
          <button id="crm-reason-cancel" style="background:transparent;color:var(--text-muted);border:1px solid var(--border);border-radius:8px;padding:8px 16px;font-family:'DM Sans',sans-serif;font-size:13px;cursor:pointer;">Cancel</button>
          <button id="crm-reason-ok" style="background:var(--red,#9B2E24);color:#fff;border:none;border-radius:8px;padding:8px 18px;font-family:'DM Sans',sans-serif;font-size:13px;font-weight:600;cursor:pointer;">Confirm</button>
        </div>
      </div>`;
    document.body.appendChild(ov);
    const close = (val) => { ov.remove(); resolve(val); };
    ov.querySelector('#crm-reason-cancel').onclick = () => close(null);
    ov.querySelector('#crm-reason-ok').onclick = () => close(ov.querySelector('#crm-reason-pick').value);
    ov.onclick = (e) => { if (e.target === ov) close(null); };
  });
}

async function crmChangeStage(plid, stage) {
  let reason = null;
  if (stage === 'Not Interested') {
    reason = await crmAskReason();
    if (reason === null) { crmReload(); return; }  // cancelled → revert dropdown
  }
  try {
    const r = await fetch('/api/v2/lead/' + encodeURIComponent(plid) + '/stage', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(reason ? { stage, reason } : { stage })
    });
    const d = await r.json();
    if (!r.ok || (d && d.ok === false)) { crmToast((d && d.error) || 'Failed to change stage', false); }
    else { crmToast(stage === 'Not Interested' ? ('Closed — ' + reason) : 'Stage updated', true); }
  } catch (e) { crmToast('Failed to change stage', false); }
  crmReload();
}

async function crmTriggerCall(plid) {
  const btn = document.getElementById('crm-call-btn');
  const res = document.getElementById('crm-call-result');
  if (btn) { btn.disabled = true; btn.style.opacity = '0.6'; }
  if (res) res.textContent = 'Triggering…';
  try {
    const r = await fetch('/api/v2/lead/' + encodeURIComponent(plid) + '/trigger-call', { method: 'POST' });
    const d = await r.json();
    if (d && d.ok) {
      if (res) { res.textContent = 'Call triggered'; res.style.color = 'var(--green)'; }
      crmToast('Call triggered', true);
    } else {
      const err = (d && d.error) || 'Failed to trigger call';
      if (/not configured/i.test(err)) {
        if (res) { res.textContent = 'Calling not yet live (activates at cutover)'; res.style.color = 'var(--text-muted)'; }
        crmToast('Calling not yet live (activates at cutover)', false);
      } else {
        if (res) { res.textContent = err; res.style.color = 'var(--red)'; }
        crmToast(err, false);
      }
    }
  } catch (e) {
    if (res) { res.textContent = 'Failed to trigger call'; res.style.color = 'var(--red)'; }
    crmToast('Failed to trigger call', false);
  }
  if (btn) { btn.disabled = false; btn.style.opacity = '1'; }
  setTimeout(crmReload, 900);
}

async function crmSaveNotes(plid) {
  const ta = document.getElementById('crm-notes');
  const notes = ta ? ta.value : '';
  if (!plid) { crmToast('Cannot save — lead not resolved', false); return; }
  let saved = false;
  try {
    const r = await fetch('/api/v2/lead/' + encodeURIComponent(plid) + '/notes', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ notes })
    });
    const d = await r.json().catch(() => ({}));
    saved = r.ok && d && d.ok === true;   // require a real 200 + {ok:true}
    crmToast(saved ? 'Notes saved' : (d.error || ('Failed to save notes (HTTP ' + r.status + ')')), saved);
  } catch (e) { crmToast('Failed to save notes', false); }
  if (saved) crmReload();   // only reload on a confirmed save
}

async function crmSetReminder(plid, channel) {
  // channel = 'call' | 'whatsapp' — sends ONLY that channel's datetime so the
  // other channel's existing reminder is left untouched.
  const noteEl = document.getElementById('crm-rem-note');
  const note = noteEl ? noteEl.value : '';
  const body = { note };
  // Reject past date/times immediately (browsers don't always block the time
  // portion of datetime-local min). Compare the picked LOCAL time against now.
  const _isPast = (v) => {
    if (!v) return false;
    const picked = new Date(v);          // datetime-local parses as local time
    return isNaN(picked.getTime()) || picked.getTime() <= Date.now();
  };
  if (channel === 'call') {
    const el = document.getElementById('crm-rem-call');
    if (!el || !el.value) { crmToast('Pick a call date/time first', false); return; }
    if (_isPast(el.value)) { crmToast('Call reminder must be in the future', false); return; }
    body.call_at = el.value;
  } else {
    const el = document.getElementById('crm-rem-wa');
    if (!el || !el.value) { crmToast('Pick a WhatsApp date/time first', false); return; }
    if (_isPast(el.value)) { crmToast('WhatsApp reminder must be in the future', false); return; }
    body.whatsapp_at = el.value;
  }
  try {
    const r = await fetch('/api/v2/lead/' + encodeURIComponent(plid) + '/reminder', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    });
    const d = await r.json();
    crmToast(d && d.ok === false ? (d.error || 'Failed to set reminder')
             : (channel === 'call' ? 'Call reminder set' : 'WhatsApp reminder set'), !(d && d.ok === false));
  } catch (e) { crmToast('Failed to set reminder', false); }
  crmReload();
}

async function crmClearReminder(plid, channel) {
  // Send empty string for that channel → backend cancels its pending queue item.
  const body = {};
  if (channel === 'call') body.call_at = '';
  else body.whatsapp_at = '';
  try {
    const r = await fetch('/api/v2/lead/' + encodeURIComponent(plid) + '/reminder', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    });
    const d = await r.json();
    crmToast(d && d.ok === false ? (d.error || 'Failed to clear') : 'Reminder cleared', !(d && d.ok === false));
  } catch (e) { crmToast('Failed to clear reminder', false); }
  crmReload();
}

async function crmToggleBot(plid, enabled) {
  try {
    const r = await fetch('/api/v2/lead/' + encodeURIComponent(plid) + '/bot-toggle', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled })
    });
    const d = await r.json();
    crmToast(d && d.ok === false ? (d.error || 'Failed to toggle bot') : ('Bot ' + (enabled ? 'enabled' : 'disabled')), !(d && d.ok === false));
  } catch (e) { crmToast('Failed to toggle bot', false); }
  crmReload();
}

function fmtDuration(secs) {
  if (!secs || secs <= 0) return '—';
  const total = Math.round(secs);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function fmtTs(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' });
}

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
}

function fmtDateKey(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toDateString();
}

function pickupBadge(p, triggered) {
  // p is null when no answered+not_answered calls (denominator 0) → N/A.
  // p === 0 is a REAL 0% (calls rang, none picked up).
  if (p == null) return '<span class="badge badge-gray" style="font-size:18px;padding:4px 12px;">N/A</span>';
  if (p === 0) return '<span class="badge badge-red" style="font-size:18px;padding:4px 12px;">0%</span>';
  const pct = Math.round(p * 100);
  const cls = p >= 0.7 ? 'badge-green' : p >= 0.3 ? 'badge-amber' : 'badge-red';
  return `<span class="badge ${cls}" style="font-size:18px;padding:4px 12px;">${pct}%</span>`;
}

function outcomeBadge(outcome) {
  if (!outcome) return '';
  const ANSWERED_O      = ['CONNECTED','AUTO_CALLBACK','PRE_SITE','NOT_INTERESTED','DROPPED','VIRTUAL_TOUR'];
  const NOT_ANSWERED_O  = ['NO_ANSWER','BUSY'];
  const NOT_CONNECTED_O = ['NOT_CONNECTED','SWITCHED_OFF'];
  const LABELS = {CONNECTED:'Connected',AUTO_CALLBACK:'Auto callback',PRE_SITE:'Pre site visit',NOT_INTERESTED:'Not interested',DROPPED:'Dropped',VIRTUAL_TOUR:'Virtual tour',NO_ANSWER:'No answer',BUSY:'Busy',NOT_CONNECTED:'Not connected',SWITCHED_OFF:'Switched off'};
  let cls = 'badge-gray';
  if (ANSWERED_O.includes(outcome))      cls = 'badge-green';
  else if (NOT_ANSWERED_O.includes(outcome))  cls = 'badge-amber';
  else if (NOT_CONNECTED_O.includes(outcome)) cls = 'badge-red';
  return `<span class="badge ${cls} outcome-badge">${LABELS[outcome] || outcome}</span>`;
}

function dirBadge(dir) {
  const cls = (dir||'').toLowerCase() === 'outbound' ? 'badge-blue' : 'badge-gray';
  return `<span class="badge ${cls}">${(dir||'—').toUpperCase()}</span>`;
}

// The outcome BUCKET (Answered / Not answered / Not connected / Inbound) shown
// beside OUTBOUND, matching the filter-chip wording. Sits before the granular
// status badge (Connected / No answer / …).
function bucketBadge(b) {
  const M = {
    'answered':      ['Answered','badge-green'],
    'not-answered':  ['Not answered','badge-amber'],
    'not-connected': ['Not connected','badge-red'],
    'inbound':       ['Inbound','badge-gray'],
  };
  const m = M[b] || ['—','badge-gray'];
  return `<span class="badge ${m[1]}" style="font-weight:500;">${m[0]}</span>`;
}

function renderWebsite(data) {
  const w = data.website || {};
  const log = w.message_log || [];
  const ctaLog = w.cta_log || [];
  const ctas = w.cta_details || [];

  // ── Submission dates (form submissions / resubmissions) ──
  const subsRaw = [];
  if (data.lead_created_at) subsRaw.push({at: data.lead_created_at, src: data.lead_source || ''});
  (data.submission_history || []).forEach(s => { if (s && s.submitted_at) subsRaw.push({at: s.submitted_at, src: s.source || ''}); });
  subsRaw.sort((a,b) => new Date(a.at) - new Date(b.at));
  const _seenDay = new Set(); const subs = [];
  subsRaw.forEach(s => { const k = fmtDateKey(s.at); if (k && k !== 'Invalid Date' && !_seenDay.has(k)) { _seenDay.add(k); subs.push(s); } });
  const subsHtml = subs.length ? `
    <div style="margin-bottom:16px;padding:12px 14px;background:var(--surface-2);border:1px solid var(--border);border-radius:10px;">
      <div style="font-size:10px;letter-spacing:0.08em;text-transform:uppercase;color:var(--text-muted);margin-bottom:8px;">Submissions (${subs.length})</div>
      <div style="display:flex;flex-wrap:wrap;gap:8px;">
        ${subs.map((s,i) => `<span style="display:inline-flex;align-items:center;gap:6px;font-size:12px;background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:5px 11px;">
          <b style="color:var(--accent);">#${i+1}</b> ${fmtDate(s.at)}${s.src ? ` · <span style="color:var(--text-dim);">${escHtml(s.src)}</span>` : ''}</span>`).join('')}
      </div>
    </div>` : '';

  // Build messages chat HTML
  function buildMsgChat() {
    if (log.length === 0) return '<div class="no-activity">No typed messages recorded</div>';
    let html = '<div class="chat-log">';
    let lastDateKey = null;
    log.forEach(msg => {
      const userTs = msg.user_at || null;
      const botTs  = msg.bot_at  || null;
      const dateKey = fmtDateKey(userTs || botTs);
      if (dateKey && dateKey !== lastDateKey) {
        html += `<div class="date-sep">${fmtDate(userTs || botTs)}</div>`;
        lastDateKey = dateKey;
      }
      if (msg.user_text) {
        html += `<div class="chat-row right">
          <div class="chat-label">Lead</div>
          <div class="bubble bubble-user">${escHtml(msg.user_text)}</div>
          ${userTs ? `<div class="bubble-ts">${fmtTs(userTs)}</div>` : ''}
        </div>`;
      }
      if (msg.bot_reply) {
        html += `<div class="chat-row left">
          <div class="chat-label">Anandita</div>
          <div class="bubble bubble-bot">${escHtml(msg.bot_reply)}</div>
          ${botTs ? `<div class="bubble-ts">${fmtTs(botTs)}</div>` : ''}
        </div>`;
      }
    });
    html += '</div>';
    return html;
  }

  // Build CTA chat HTML
  function buildCtaChat() {
    if (ctaLog.length === 0) return '<div class="no-activity">No CTA clicks recorded</div>';
    let html = '<div class="chat-log">';
    let lastDateKey = null;
    ctaLog.forEach(c => {
      const ts = c.triggered_at || null;
      const dateKey = fmtDateKey(ts);
      if (dateKey && dateKey !== lastDateKey) {
        html += `<div class="date-sep">${fmtDate(ts)}</div>`;
        lastDateKey = dateKey;
      }
      html += `<div class="cta-log-item">
        <div class="cta-log-header">
          ${c.artifact ? `<span class="artifact-pill">${escHtml(c.artifact)}</span>` : ''}
          ${c.artifact_label ? `<span class="cta-log-label">${escHtml(c.artifact_label)}</span>` : ''}
          ${ts ? `<span class="cta-log-time">${fmtTs(ts)}</span>` : ''}
        </div>
        ${(c.user_text || c.bot_reply) ? `<div class="cta-log-body">
          ${c.user_text ? `
            <div class="cta-log-sender">Lead</div>
            <div class="chat-row right" style="margin:0 0 10px;">
              <div class="bubble bubble-user">${escHtml(c.user_text)}</div>
              ${ts ? `<div class="bubble-ts">${fmtTs(ts)}</div>` : ''}
            </div>` : ''}
          ${c.bot_reply ? `
            <div class="cta-log-sender">Anandita</div>
            <div class="bubble bubble-bot">${escHtml(c.bot_reply)}</div>
            ${c.bot_at ? `<div class="bubble-ts">${fmtTs(c.bot_at)}</div>` : ''}` : ''}
        </div>` : ''}
      </div>`;
    });
    html += '</div>';
    return html;
  }

  const statsHtml = `
    <div class="stat-cards" id="website-stat-cards">
      <div class="stat-card stat-card-accent website-view-card" data-view="cta" onclick="switchWebsiteView('cta')" style="cursor:pointer;">
        <div class="stat-card-label">CTA Clicks</div>
        <div class="stat-card-value">${w.cta_triggered ?? '—'}</div>
        ${ctas.length ? `<div class="artifact-pills">${ctas.map(c=>`<span class="artifact-pill">${c}</span>`).join('')}</div>` : ''}
      </div>
      <div class="stat-card stat-card-accent website-view-card website-active-card" data-view="msg" onclick="switchWebsiteView('msg')" style="cursor:pointer;">
        <div class="stat-card-label">Messages Typed</div>
        <div class="stat-card-value">${w.messages_typed ?? '—'}</div>
      </div>
      <div class="stat-card stat-card-accent">
        <div class="stat-card-label">Website Time</div>
        <div class="stat-card-value">${fmtDuration(w.website_time_spent_secs)}</div>
      </div>
    </div>
    <div id="website-view-msg" class="website-view">${buildMsgChat()}</div>
    <div id="website-view-cta" class="website-view" style="display:none;">${buildCtaChat()}</div>`;

  document.getElementById('tab-website').innerHTML = subsHtml + statsHtml;
}

function switchWebsiteView(view) {
  document.querySelectorAll('.website-view-card').forEach(c => c.classList.remove('website-active-card'));
  document.querySelectorAll('.website-view').forEach(v => v.style.display = 'none');
  document.querySelector(`.website-view-card[data-view="${view}"]`).classList.add('website-active-card');
  document.getElementById(`website-view-${view}`).style.display = '';
}

async function renderWhatsApp(data) {
  // LIVE read: pull the conversation straight from whatsapp_messages so it's
  // always current (inbound shows instantly) and never appears wiped. Falls
  // back to the cached profile copy if the live fetch fails.
  let wa = data.whatsapp || {};
  try {
    const r = await fetch('/api/lead/' + encodeURIComponent(LEAD_ID) + '/whatsapp-live');
    if (r.ok) {
      const live = await r.json();
      if (live && Array.isArray(live.user_conversation_history)) wa = live;
    }
  } catch (e) { /* keep cached fallback */ }
  window._lastWa = wa;               // cached so a hide/unhide can re-render live
  renderWhatsAppFrom(wa);
}

function renderWhatsAppFrom(wa) {
  wa = wa || {};
  const history = wa.user_conversation_history || [];

  let depthHtml = `
    <div style="margin-bottom:20px;display:flex;align-items:center;gap:12px;">
      <span style="font-size:13px;color:var(--text-muted);">Conversation Depth</span>
      <span class="font-syne" style="font-weight:800;font-size:22px;color:var(--accent);">${wa.user_conversation_depth ?? 0}</span>
      <span style="font-size:12px;color:var(--text-dim);">messages from lead</span>
    </div>`;

  let chatHtml = '';
  if (history.length === 0) {
    chatHtml = '<div class="no-activity">No WhatsApp activity recorded</div>';
  } else {
    chatHtml = '<div class="chat-log">';
    let lastDateKey = null;
    history.forEach(msg => {
      const iso = msg.date || msg.timestamp || null;
      const dateKey = fmtDateKey(iso);
      if (dateKey && dateKey !== lastDateKey) {
        chatHtml += `<div class="date-sep">${fmtDate(iso)}</div>`;
        lastDateKey = dateKey;
      }
      // ADMIN-only per-message hide control — a clear button (not tiny text), so
      // it's obvious and clickable. Uses the server-supplied fingerprint.
      const fp = String(msg.fp || '');
      const waHidden = IS_ADMIN && fp && window._hiddenWa && window._hiddenWa.has(fp);
      const waHideBtn = (IS_ADMIN && fp) ? `<button onclick="toggleHideWa('${fp}', event)" title="${waHidden ? 'Show to Performance again' : 'Hide from the Performance team'}" style="font-size:9px;padding:2px 9px;margin-top:3px;border:1px solid var(--border);border-radius:12px;background:${waHidden ? 'var(--red-dim)' : 'var(--surface-2)'};color:${waHidden ? 'var(--red)' : 'var(--text-muted)'};cursor:pointer;">${waHidden ? 'Unhide' : 'Hide from Perf'}</button>` : '';
      const rowStyle = waHidden ? ' style="opacity:0.5;"' : '';
      const isInbound = (msg.type || '').toLowerCase() === 'inbound';
      if (isInbound) {
        chatHtml += `
          <div class="chat-row right"${rowStyle}>
            <div class="chat-label">Lead</div>
            <div class="bubble bubble-user">${escHtml(msg.message || '')}</div>
            ${iso ? `<div class="bubble-ts">${fmtTs(iso)}</div>` : ''}
            ${waHideBtn}
          </div>`;
      } else {
        chatHtml += `
          <div class="chat-row left"${rowStyle}>
            <div class="chat-label">Anandita</div>
            <div class="bubble bubble-bot">${escHtml(msg.message || '')}</div>
            ${iso ? `<div class="bubble-ts">${fmtTs(iso)}</div>` : ''}
            ${waHideBtn}
          </div>`;
      }
    });
    chatHtml += '</div>';
  }

  document.getElementById('tab-whatsapp').innerHTML = depthHtml + chatHtml;
}

function renderCalls(data) {
  const ac = data.ai_calls || {};
  const convos = ac.call_conversations || [];

  const notConnected = ac.calls_not_connected ?? 0;
  const inboundCalls = ac.inbound_calls ?? 0;
  let statsHtml = `
    <div class="stat-cards">
      <div class="stat-card stat-card-accent">
        <div class="stat-card-label">Outbound Triggered</div>
        <div class="stat-card-value">${ac.outbound_calls_triggered ?? '—'}</div>
      </div>
      <div class="stat-card stat-card-green">
        <div class="stat-card-label">Calls Answered</div>
        <div class="stat-card-value">${ac.calls_answered ?? '—'}</div>
      </div>
      <div class="stat-card stat-card-red">
        <div class="stat-card-label">Not Answered</div>
        <div class="stat-card-value">${ac.calls_not_answered ?? '—'}</div>
      </div>
      ${inboundCalls > 0 ? `
      <div class="stat-card" style="border-left:2px solid var(--accent-teal, #4ecdc4);">
        <div class="stat-card-label">Inbound Calls</div>
        <div class="stat-card-value">${inboundCalls}</div>
      </div>` : ''}
      <div class="stat-card stat-card-accent">
        <div class="stat-card-label">Pickup Ratio</div>
        <div style="margin-top:4px;">${pickupBadge(ac.pickup_ratio, ac.outbound_calls_triggered ?? 0)}</div>
      </div>
      <div class="stat-card stat-card-accent">
        <div class="stat-card-label">Avg Duration</div>
        <div class="stat-card-value">${fmtDuration(ac.average_call_duration_secs)}</div>
      </div>
    </div>`;

  const ANSWERED_SET     = new Set(['CONNECTED','AUTO_CALLBACK','PRE_SITE','NOT_INTERESTED','DROPPED','VIRTUAL_TOUR']);
  const NOT_ANSWERED_SET = new Set(['NO_ANSWER','BUSY']);
  const NOT_CONNECTED_SET= new Set(['NOT_CONNECTED','SWITCHED_OFF']);
  const isOutbound = c => (c.call_direction || '').toLowerCase() !== 'inbound';
  function bucketOf(c) {
    if (!isOutbound(c)) return 'inbound';
    // 'answered' ONLY for recognized answered outcomes — matches the metric-card
    // counting. An unknown/None outcome must NOT default to answered (that showed
    // a false green ANSWERED for calls whose outcome wasn't recorded yet).
    if (ANSWERED_SET.has(c.call_outcome))      return 'answered';
    if (NOT_ANSWERED_SET.has(c.call_outcome))  return 'not-answered';
    return 'not-connected';
  }
  const DOT = {answered:'#2E7D52','not-answered':'#C8881A','not-connected':'#8B2635',inbound:'#2A9D8F'};
  const tsv = v => { const d = new Date(v); return isNaN(d) ? 0 : d.getTime(); };

  // ONE chronological Call History — newest first.
  const allCalls = convos.slice().sort((a,b) => tsv(b.started_at) - tsv(a.started_at));
  const counts = {answered:0,'not-answered':0,'not-connected':0,inbound:0};
  allCalls.forEach(c => counts[bucketOf(c)]++);

  function buildBody(c) {
    const transcript = c.transcript || [];
    let txHtml = '';
    if (transcript.length) {
      txHtml = '<div class="chat-log" style="max-height:360px;">';
      let lastDateKey = null;
      transcript.forEach(t => {
        const iso = t.date || null;
        const dateKey = fmtDateKey(iso);
        if (dateKey && dateKey !== lastDateKey && dateKey !== 'Invalid Date') {
          txHtml += `<div class="date-sep">${fmtDate(iso)}</div>`; lastDateKey = dateKey;
        }
        const isInbound = (t.type || '').toLowerCase() === 'inbound';
        txHtml += isInbound
          ? `<div class="chat-row right"><div class="chat-label">Lead</div><div class="bubble bubble-user">${escHtml(t.message||'')}</div>${iso?`<div class="bubble-ts">${fmtTs(iso)}</div>`:''}</div>`
          : `<div class="chat-row left"><div class="chat-label">${escHtml(t.speaker||'Agent')}</div><div class="bubble bubble-bot">${escHtml(t.message||'')}</div>${iso?`<div class="bubble-ts">${fmtTs(iso)}</div>`:''}</div>`;
      });
      txHtml += '</div>';
    }
    let recHtml = '';
    if (c.recording_url) {
      const recDur = c.recording_duration_secs ? fmtDuration(c.recording_duration_secs) : '';
      recHtml = `<div class="recording-player"><span class="recording-player-icon">◉</span><div class="recording-player-info"><div class="recording-player-label">Call Recording</div><audio controls preload="none" src="${c.recording_url}"></audio></div>${recDur?`<span class="recording-player-dur">${recDur}</span>`:''}</div>`;
    }
    const note = (!transcript.length && !c.recording_url)
      ? '<div style="color:var(--text-dim);font-size:13px;padding:8px 2px;">No recording &middot; no transcript for this call.</div>' : '';
    return recHtml + txHtml + note;
  }

  function callNode(c, i) {
    const b = bucketOf(c);
    const cardId = 'h-' + i;
    const dur = c.duration_secs ? `<span class="badge badge-gray" style="font-size:9px;padding:2px 7px;">${fmtDuration(c.duration_secs)}</span>` : '';
    const rec = c.recording_url ? '<span class="badge badge-green" style="font-size:9px;padding:2px 7px;letter-spacing:0.05em;">● REC</span>' : '';
    // ADMIN-only: hide this call from the Performance team (CI + admin still see it).
    const sid = String(c.call_sid || '');
    const isHidden = IS_ADMIN && sid && window._hiddenCalls && window._hiddenCalls.has(sid);
    const hiddenBadge = isHidden ? '<span class="badge" style="font-size:9px;padding:2px 7px;background:var(--red-dim);color:var(--red);letter-spacing:0.04em;">HIDDEN · PERF</span>' : '';
    const hideBtn = (IS_ADMIN && sid) ? `<button onclick="toggleHideCall('${sid}', event)" title="${isHidden ? 'Show to Performance again' : 'Hide from the Performance team'}" style="font-size:9px;padding:2px 9px;border:1px solid var(--border);border-radius:12px;background:${isHidden ? 'var(--red-dim)' : 'var(--surface-2)'};color:${isHidden ? 'var(--red)' : 'var(--text-muted)'};cursor:pointer;margin-left:6px;">${isHidden ? 'Unhide' : 'Hide'}</button>` : '';
    return `
      <div class="call-node" data-bucket="${b}"${isHidden ? ' style="opacity:0.55;"' : ''}>
        <span class="call-dot" style="background:${DOT[b]};"></span>
        <div class="call-card" id="call-card-${cardId}">
          <div class="call-card-header" onclick="toggleCall('${cardId}')">
            <span class="call-card-num">#${i+1}</span>
            <div class="call-card-meta">${dirBadge(c.call_direction)}${bucketBadge(b)}${outcomeBadge(c.call_outcome)}${dur}${rec}${hiddenBadge}</div>
            <span class="call-card-date">${c.started_at ? fmtDate(c.started_at) + ' ' + fmtTs(c.started_at) : '—'}</span>
            ${hideBtn}
            <span class="call-expand-icon">▾</span>
          </div>
          <div class="call-transcript">${buildBody(c)}</div>
        </div>
      </div>`;
  }

  const chip = (key,label,n) => `<button class="call-chip${key==='all'?' on':''}" data-bucket="${key}" onclick="filterCalls('${key}')">${label} <span style="opacity:0.6;">(${n})</span></button>`;
  const chipsHtml = `
    <div class="call-chips">
      ${chip('all','All', allCalls.length)}
      ${chip('answered','Answered', counts.answered)}
      ${chip('not-answered','Not answered', counts['not-answered'])}
      ${counts.inbound > 0 ? chip('inbound','Inbound', counts.inbound) : ''}
    </div>`;

  const STYLE = `<style>
    .call-chips{display:flex;gap:8px;margin:16px 0 14px;flex-wrap:wrap;}
    .call-chip{padding:6px 14px;border:1px solid var(--border);border-radius:20px;background:var(--surface);cursor:pointer;font-size:12px;font-family:'DM Sans',sans-serif;color:var(--text);}
    .call-chip.on{background:var(--accent);color:#fff;border-color:var(--accent);}
    .call-timeline{position:relative;padding-left:30px;margin-top:4px;}
    .call-timeline::before{content:'';position:absolute;left:12px;top:10px;bottom:10px;width:2px;background:var(--border);}
    .call-node{position:relative;margin-bottom:10px;}
    .call-node .call-card{margin-bottom:0;}
    .call-dot{position:absolute;left:-23px;top:16px;width:13px;height:13px;border-radius:50%;border:2px solid var(--bg);z-index:1;}
  </style>`;

  const listHtml = `<div class="call-timeline" id="call-timeline">${allCalls.map(callNode).join('')}</div>`;
  document.getElementById('tab-calls').innerHTML = STYLE + statsHtml + (convos.length === 0 ? '<div class="no-activity">No call conversations recorded</div>' : chipsHtml + listHtml);
}

function toggleCall(id) {
  const card = document.getElementById('call-card-' + id);
  if (card) card.classList.toggle('open');
}

function filterCalls(bucket) {
  document.querySelectorAll('#call-timeline .call-node').forEach(n => {
    n.style.display = (bucket === 'all' || n.dataset.bucket === bucket) ? '' : 'none';
  });
  document.querySelectorAll('.call-chip').forEach(ch => ch.classList.toggle('on', ch.dataset.bucket === bucket));
}

// ── Admin: hide/unhide a call or WhatsApp message from the Performance team ─────
async function loadHiddenSet() {
  window._hiddenCalls = new Set();
  window._hiddenWa = new Set();
  try {
    // Global set (NOT scoped to this lead_id): a call/message is hidden by its
    // id everywhere it appears, so every same-phone project-row must mark it.
    const r = await fetch('/api/admin/redactions');
    if (r.ok) {
      const d = await r.json();
      (d.calls || []).forEach(x => window._hiddenCalls.add(String(x)));
      (d.whatsapp || []).forEach(x => window._hiddenWa.add(String(x)));
    }
  } catch (e) { /* non-fatal — markers just won't show */ }
}

async function _toggleHide(itemType, itemId, setRef, ev) {
  if (ev) ev.stopPropagation();
  const has = setRef && setRef.has(String(itemId));
  const url = has ? '/api/admin/redactions/unhide' : '/api/admin/redactions';
  try {
    const r = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item_type: itemType, item_id: String(itemId),
                             lead_id: LEAD_ID, phone: window._leadPhone || '' })
    });
    if (!r.ok) { alert('Failed — ' + r.status); return false; }
    if (has) setRef.delete(String(itemId)); else setRef.add(String(itemId));
    return true;
  } catch (e) { alert('Failed to update — network error'); return false; }
}

async function toggleHideCall(callSid, ev) {
  if (await _toggleHide('call', callSid, window._hiddenCalls, ev)) {
    if (window._lastLeadData) renderCalls(window._lastLeadData);   // refresh markers live
  }
}

async function toggleHideWa(fp, ev) {
  if (await _toggleHide('whatsapp', fp, window._hiddenWa, ev)) {
    renderWhatsAppFrom(window._lastWa);
  }
}

function escHtml(str) {
  return String(str)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;')
    .replace(/'/g,'&#039;');
}

async function loadLead() {
  // Opening the lead marks it "seen" → clears its new-activity highlight on the
  // homepage until the next call / WhatsApp / stage change / resubmission.
  try { fetch('/api/lead/' + encodeURIComponent(LEAD_ID) + '/seen', { method: 'POST' }); } catch (e) {}
  try {
    const r = await fetch('/api/lead/' + encodeURIComponent(LEAD_ID));
    if (!r.ok) throw new Error('Not found');
    const data = await r.json();

    // Render header — escape all external lead data (stored XSS, same class as the
    // list). window._leadPhone stays RAW (used in fetch URLs, not HTML).
    const name   = escHtml(data.lead_name || 'Unknown Lead');
    const phone  = escHtml(data.lead_phone_number || '—');
    window._leadPhone = data.lead_phone_number || '';
    const source   = escHtml(data.lead_source || '');
    const camp     = escHtml(data.campaign_name || '—');
    const leadSince = data.lead_created_at ? fmtDate(data.lead_created_at) : '—';
    const srcBadge = source ? `<span class="badge badge-source">${source}</span>` : '';
    // Lead ID replaces the phone in the header: Inncircles source id; for bulk
    // leads the id captured from the upload sheet (never the BULK-<phone>-<proj>
    // composite, which embeds the phone); else the PLID with "ZOHO-" stripped.
    const leadIdDisplay = ((data.lead_source||'').toLowerCase().includes('inncircles') && data.source_lead_id)
      ? escHtml(String(data.source_lead_id))
      : String(LEAD_ID || '').indexOf('BULK-') === 0
        ? escHtml(String(data.bulk_lead_id || ''))
        : escHtml(String(LEAD_ID || '').replace(/^ZOHO-/i, ''));

    const IS_ADMIN_DETAIL = {{ 'true' if (user_role == 'admin' and not embed) else 'false' }};
    window._currentFlag = data.flag || null;
    window._currentLeadId = LEAD_ID;

    function renderDetailFlag() {
      const f = window._currentFlag;
      if (!IS_ADMIN_DETAIL) {
        if (f === 'genuine')       return `<span class="flag-genuine"      style="cursor:default;font-size:12px;padding:4px 12px;">🟢 Genuine</span>`;
        if (f === 'spam')          return `<span class="flag-spam"         style="cursor:default;font-size:12px;padding:4px 12px;">🔴 Spam</span>`;
        if (f === 'test')          return `<span class="flag-test"         style="cursor:default;font-size:12px;padding:4px 12px;">🔵 Test</span>`;
        if (f === 'unclear')       return `<span class="flag-unclear"      style="cursor:default;font-size:12px;padding:4px 12px;">🟡 Unclear</span>`;
        return '';
      }
      if (f === 'genuine')       return `<span class="flag-genuine"      style="font-size:12px;padding:4px 12px;" onclick="cycleFlag()" title="Click to mark Spam">🟢 Genuine</span>`;
      if (f === 'spam')          return `<span class="flag-spam"         style="font-size:12px;padding:4px 12px;" onclick="cycleFlag()" title="Click to mark Test">🔴 Spam</span>`;
      if (f === 'test')          return `<span class="flag-test"         style="font-size:12px;padding:4px 12px;" onclick="cycleFlag()" title="Click to mark Unclear">🔵 Test</span>`;
      if (f === 'unclear')       return `<span class="flag-unclear"      style="font-size:12px;padding:4px 12px;" onclick="cycleFlag()" title="Click to clear">🟡 Unclear</span>`;
      return `<span class="flag-none" style="font-size:12px;padding:4px 12px;" onclick="cycleFlag()" title="Click to mark Genuine">⚑ Mark Lead</span>`;
    }

    window.cycleFlag = async function() {
      // Cycle: none → genuine → spam → test → unclear → none.
      // "Not Interested" is a CRM STAGE, not a flag. "Spam" stays a flag AND
      // still closes the CRM lead (the backend marks it Not Interested / Spam).
      const cur = window._currentFlag;
      const next = cur === 'genuine' ? 'spam'
                 : cur === 'spam' ? 'test'
                 : cur === 'test' ? 'unclear'
                 : cur === 'unclear' ? null
                 : 'genuine';
      const r = await fetch(`/api/lead/${LEAD_ID}/flag`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ flag: next })
      });
      if (r.ok) {
        window._currentFlag = next;
        document.getElementById('detail-flag-cell').innerHTML = renderDetailFlag();
        // Marking Spam closes the CRM lead — refresh the CRM tab if it's open.
        if (next === 'spam' && typeof crmReload === 'function') {
          window._crmLoaded = false;
          const t = document.getElementById('tab-crm');
          if (t && t.classList.contains('active')) crmReload();
        }
      }
    };

    document.getElementById('lead-header').innerHTML = `
      <div class="fade-in" style="width:100%;">
        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:16px;">
          <div>
            <div class="lead-name">${escHtml(name)}</div>
            <div class="lead-meta">
              <span class="lead-phone-display">${leadIdDisplay || '—'}</span>
              ${srcBadge}
              <span class="divider">|</span>
              <span style="font-size:12px;color:var(--text-muted);">Campaign:</span>
              <span class="badge badge-campaign" title="${escHtml(camp)}">${escHtml(camp.length > 35 ? camp.slice(0,35)+'…' : camp)}</span>
              <span class="divider">|</span>
              <span class="lead-since">Born: ${leadSince}</span>
            </div>
          </div>
          <div id="detail-flag-cell" style="padding-top:6px;flex-shrink:0;">${renderDetailFlag()}</div>
        </div>
      </div>`;

    // Resubmission history strip (shown only when lead has submitted more than once)
    const _subHist = data.submission_history || [];
    if (_subHist.length > 1) {
      const _subCards = _subHist.map((s, idx) => {
        const _dt = s.submitted_at ? (() => {
          const _d = new Date(s.submitted_at);
          return _d.toLocaleDateString('en-IN', {day:'numeric',month:'short',year:'numeric'}) + ', ' +
                 _d.toLocaleTimeString('en-IN', {hour:'2-digit',minute:'2-digit',hour12:true});
        })() : '—';
        const _isFirst = idx === 0;
        return `<div style="display:flex;align-items:flex-start;gap:8px;padding:10px 12px;border-radius:8px;background:${_isFirst?'color-mix(in srgb,var(--accent) 8%,transparent)':'var(--surface-2, var(--surface))'};border:1px solid ${_isFirst?'color-mix(in srgb,var(--accent) 25%,transparent)':'var(--border)'};flex:1;min-width:160px;max-width:320px;">
          <div style="width:7px;height:7px;border-radius:50%;background:${_isFirst?'var(--accent)':'var(--text-dim)'};flex-shrink:0;margin-top:3px;"></div>
          <div style="min-width:0;flex:1;">
            <div style="font-size:10px;font-weight:600;color:${_isFirst?'var(--accent)':'var(--text-dim)'};letter-spacing:.05em;margin-bottom:4px;">${_isFirst?'FIRST SUBMISSION':'RESUBMITTED'}</div>
            <div style="font-size:11px;color:var(--text-muted);margin-bottom:5px;">${escHtml(_dt)}</div>
            <div style="display:flex;gap:5px;flex-wrap:wrap;">
              ${s.source ? `<span class="badge badge-source" style="font-size:10px;padding:2px 7px;">${escHtml(s.source)}</span>` : ''}
              ${s.campaign ? `<span class="badge badge-campaign" style="font-size:10px;padding:2px 7px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escHtml(s.campaign)}">${escHtml(s.campaign.length>28?s.campaign.slice(0,28)+'…':s.campaign)}</span>` : ''}
              ${!s.source && !s.campaign ? `<span style="font-size:10px;color:var(--text-dim);">No source</span>` : ''}
            </div>
          </div>
        </div>`;
      }).join('');
      const _subSection = document.createElement('div');
      _subSection.style.cssText = 'margin:10px 0 4px 0;padding:12px 16px;background:var(--surface);border:1px solid var(--border);border-radius:12px;';
      _subSection.innerHTML = `<div style="font-size:10px;font-weight:600;color:var(--text-dim);letter-spacing:.08em;margin-bottom:10px;">SUBMISSION HISTORY &nbsp;·&nbsp; ${_subHist.length} SUBMISSIONS</div><div style="display:flex;gap:8px;flex-wrap:wrap;">${_subCards}</div>`;
      document.getElementById('lead-header').after(_subSection);
    }
    document.getElementById('tabs-nav').style.display = 'flex';

    window._lastLeadData = data;                 // cached so hide/unhide can re-render
    if (IS_ADMIN) { await loadHiddenSet(); }     // which calls/msgs are hidden from Perf
    renderWebsite(data);
    renderWhatsApp(data);
    renderCalls(data);

  } catch(e) {
    document.getElementById('lead-header').innerHTML =
      `<div style="color:var(--red);padding:24px 28px;">⚠ Could not load lead data: ${e.message}</div>`;
  }
}

loadLead();
</script>
</body>
</html>
"""


# ═════════════════════════════════════════════════════════════════════════════
#  QA HTML TEMPLATES
# ═════════════════════════════════════════════════════════════════════════════

QA_OVERVIEW_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>ASBL · QA Overview</title>""" + SHARED_HEAD + """</head>
<body>
<header class="topbar">
  <div style="display:flex;align-items:center;gap:0;">
    <div class="topbar-logo" onclick="location.href='/'" style="cursor:pointer;" title="Go to Leads">
      <div class="logo-mark">AS</div>
      <div class="logo-text">
        <span class="logo-primary">ASBL</span>
        <span class="logo-sub">Intelligence Platform</span>
      </div>
    </div>
    <nav class="nav-links">
      <a href="/" class="nav-link">Leads</a>
      <a href="/bulk-calls" class="nav-link">Bulk Calls</a>
      <a href="/qa" class="nav-link active">QA</a>
    <a href="/pipeline" class="nav-link">Pipeline</a>
      <a href="/main-dashboard" class="nav-link">Main Dashboard</a>
    </nav>
  </div>
  <div style="display:flex;align-items:center;gap:10px;">
    <a href="/qa/chatbot" class="btn" style="text-decoration:none;font-size:12px;">Chatbot QA</a>
    <a href="/qa/voice"   class="btn" style="text-decoration:none;font-size:12px;">Voice QA</a>
    <a href="/qa/analytics" class="btn" style="text-decoration:none;font-size:12px;">Analytics</a>
    <a href="/qa/spam"    class="btn" style="text-decoration:none;font-size:12px;">Spam</a>
    <a href="/qa/feedback" class="btn" style="text-decoration:none;font-size:12px;">Feedback</a>
    <a href="/qa/recommendations" class="btn" style="text-decoration:none;font-size:12px;">Recommendations</a>
    <span style="font-size:11px;color:var(--text-muted);">{{ user_name }}</span>
    <a href="/api/logout" class="btn" style="text-decoration:none;">Logout</a>
  </div>
</header>

<div id="stat-strip" class="stat-strip">
  <div class="stat-pill"><span class="stat-pill-value" id="s-cb-total">—</span><span class="stat-pill-label">Total Chatbot</span></div>
  <div class="stat-pill"><span class="stat-pill-value" id="s-cb-rate">—</span><span class="stat-pill-label">CB Pass Rate</span></div>
  <div class="stat-pill"><span class="stat-pill-value" id="s-vc-total">—</span><span class="stat-pill-label">Total Voice</span></div>
  <div class="stat-pill"><span class="stat-pill-value" id="s-vc-rate">—</span><span class="stat-pill-label">VC Pass Rate</span></div>
  <div class="stat-pill"><span class="stat-pill-value" id="s-health">—</span><span class="stat-pill-label">Health Score</span></div>
</div>

<div class="qa-page">
  <div id="health-cards" style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:28px;max-width:700px;"></div>
  <div style="font-size:13px;font-weight:500;color:var(--text);letter-spacing:0.04em;margin-bottom:12px;text-transform:uppercase;font-size:10px;letter-spacing:0.14em;">Recent Flags</div>
  <div style="overflow-x:auto;">
    <table id="flags-table" style="width:100%;">
      <thead><tr>
        <th>Severity</th><th>Type</th><th>Detail</th><th>Source</th>
      </tr></thead>
      <tbody id="flags-body"><tr class="loading-row"><td colspan="4"><span class="loading-dot">●</span><span class="loading-dot"> ●</span><span class="loading-dot"> ●</span> Loading…</td></tr></tbody>
    </table>
  </div>
</div>

<script>
function severityBadge(s) {
  if (!s) return '';
  const cls = s === 'HIGH' ? 'badge-red' : s === 'MEDIUM' ? 'badge-amber' : 'badge-gray';
  return `<span class="badge ${cls}">${s}</span>`;
}

async function load() {
  try {
    const r = await fetch('/api/qa/overview');
    if (!r.ok) { document.getElementById('flags-body').innerHTML = '<tr><td colspan="4" style="padding:32px 28px;color:var(--red);">QA DB not configured or unavailable.</td></tr>'; return; }
    const d = await r.json();
    const cb = d.chatbot || {}; const vc = d.voice || {};
    document.getElementById('s-cb-total').textContent = cb.total ?? '—';
    document.getElementById('s-cb-rate').textContent  = cb.total ? Math.round(cb.pass / cb.total * 100) + '%' : '—';
    document.getElementById('s-vc-total').textContent = vc.total ?? '—';
    document.getElementById('s-vc-rate').textContent  = vc.total ? Math.round(vc.pass / vc.total * 100) + '%' : '—';
    const avgHealth = ((cb.health || 0) + (vc.health || 0)) / 2;
    document.getElementById('s-health').textContent = avgHealth.toFixed(1);

    function healthCard(label, score) {
      const cls = score >= 8 ? 'health-score-green' : score >= 6 ? 'health-score-amber' : 'health-score-red';
      return `<div class="health-score-card ${cls}">
        <div class="health-score-num">${(score||0).toFixed(1)}</div>
        <div>
          <div style="font-weight:600;font-size:14px;margin-bottom:4px;">${label}</div>
          <div style="font-size:11px;opacity:0.75;">out of 10</div>
        </div>
      </div>`;
    }
    document.getElementById('health-cards').innerHTML = healthCard('Chatbot Health', cb.health || 0) + healthCard('Voice Health', vc.health || 0);

    const flags = d.recent_flags || [];
    if (!flags.length) {
      document.getElementById('flags-body').innerHTML = '<tr><td colspan="4" style="padding:32px 28px;text-align:center;color:var(--text-dim);">No flags found</td></tr>';
      return;
    }
    document.getElementById('flags-body').innerHTML = flags.map(f => `<tr>
      <td>${severityBadge(f.severity)}</td>
      <td><span class="badge badge-gray" style="font-family:'DM Mono',monospace;">${f.type || '—'}</span></td>
      <td style="font-size:12px;max-width:400px;color:var(--text-muted);">${(f.detail||'').slice(0,120)}</td>
      <td><span class="badge badge-source">${f.source || '—'}</span></td>
    </tr>`).join('');
  } catch(e) {
    document.getElementById('flags-body').innerHTML = '<tr><td colspan="4" style="padding:32px 28px;color:var(--red);">Error loading QA data.</td></tr>';
  }
}
load();
</script>
</body></html>
"""


QA_CHATBOT_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>ASBL · Chatbot QA</title>""" + SHARED_HEAD + """</head>
<body>
<header class="topbar">
  <div style="display:flex;align-items:center;gap:0;">
    <div class="topbar-logo" onclick="location.href='/'" style="cursor:pointer;" title="Go to Leads">
      <div class="logo-mark">AS</div>
      <div class="logo-text">
        <span class="logo-primary">ASBL</span>
        <span class="logo-sub">Intelligence Platform</span>
      </div>
    </div>
    <nav class="nav-links">
      <a href="/" class="nav-link">Leads</a>
      <a href="/bulk-calls" class="nav-link">Bulk Calls</a>
      <a href="/qa" class="nav-link active">QA</a>
    <a href="/pipeline" class="nav-link">Pipeline</a>
      <a href="/main-dashboard" class="nav-link">Main Dashboard</a>
    </nav>
  </div>
  <div style="display:flex;align-items:center;gap:10px;">
    <a href="/qa" class="btn" style="text-decoration:none;font-size:12px;">Overview</a>
    <a href="/qa/voice" class="btn" style="text-decoration:none;font-size:12px;">Voice QA</a>
    <a href="/qa/analytics" class="btn" style="text-decoration:none;font-size:12px;">Analytics</a>
    <a href="/qa/spam" class="btn" style="text-decoration:none;font-size:12px;">Spam</a>
    <a href="/qa/feedback" class="btn" style="text-decoration:none;font-size:12px;">Feedback</a>
    <a href="/qa/recommendations" class="btn" style="text-decoration:none;font-size:12px;">Recommendations</a>
    <span style="font-size:11px;color:var(--text-muted);">{{ user_name }}</span>
    <a href="/api/logout" class="btn" style="text-decoration:none;">Logout</a>
  </div>
</header>

<div class="stat-strip">
  <div class="stat-pill"><span class="stat-pill-value" id="s-total">—</span><span class="stat-pill-label">Total</span></div>
  <div class="stat-pill"><span class="stat-pill-value" id="s-pass">—</span><span class="stat-pill-label">Pass</span></div>
  <div class="stat-pill"><span class="stat-pill-value" id="s-fail">—</span><span class="stat-pill-label">Fail</span></div>
  <div class="stat-pill"><span class="stat-pill-value" id="s-rate">—</span><span class="stat-pill-label">Pass Rate</span></div>
</div>

<div class="qa-page">
  <div class="qa-filter-bar">
    <button class="qa-filter-btn active" id="f-all"  onclick="setStatus('ALL')">All</button>
    <button class="qa-filter-btn"        id="f-pass" onclick="setStatus('PASS')">Pass</button>
    <button class="qa-filter-btn"        id="f-fail" onclick="setStatus('FAIL')">Fail</button>
    <span style="font-size:11px;color:var(--text-muted);margin-left:8px;">From</span>
    <input class="qa-date-input" type="date" id="date-from" onchange="loadPage(1)" />
    <span style="font-size:11px;color:var(--text-muted);">To</span>
    <input class="qa-date-input" type="date" id="date-to" onchange="loadPage(1)" />
  </div>

  <div style="overflow-x:auto;">
    <table>
      <thead><tr>
        <th style="width:40px;">#</th>
        <th>Status</th><th>Score</th><th>Visitor ID</th>
        <th>Lead ID</th><th>Conversation ID</th><th>Issues</th><th>Turns</th><th>Date</th>
      </tr></thead>
      <tbody id="qa-body"><tr class="loading-row"><td colspan="9"><span class="loading-dot">●</span><span class="loading-dot"> ●</span><span class="loading-dot"> ●</span> Loading…</td></tr></tbody>
    </table>
  </div>
  <div class="qa-paginator">
    <button id="btn-prev" onclick="loadPage(currentPage-1)" disabled>← Prev</button>
    <span id="page-info">—</span>
    <button id="btn-next" onclick="loadPage(currentPage+1)" disabled>Next →</button>
  </div>
</div>

<script>
let currentPage = 1, totalPages = 1, statusFilter = 'ALL';

function setStatus(s) {
  statusFilter = s;
  ['all','pass','fail'].forEach(x => document.getElementById('f-'+x).classList.toggle('active', x === s.toLowerCase()));
  loadPage(1);
}

function statusBadge(s) {
  if (s === 'PASS') return '<span class="badge badge-green">PASS</span>';
  if (s === 'FAIL') return '<span class="badge badge-red">FAIL</span>';
  return `<span class="badge badge-gray">${s||'—'}</span>`;
}

function fmtDate(iso) {
  if (!iso) return '—';
  try { return new Date(iso).toLocaleDateString('en-IN', {day:'numeric',month:'short',year:'numeric'}); } catch(e){ return iso; }
}

function escHtml(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

async function loadPage(p) {
  p = Math.max(1, p);
  const dateFrom = document.getElementById('date-from').value;
  const dateTo   = document.getElementById('date-to').value;
  let url = `/api/qa/chatbot?page=${p}&limit=50&status=${statusFilter}`;
  if (dateFrom) url += `&dateFrom=${dateFrom}`;
  if (dateTo)   url += `&dateTo=${dateTo}`;

  document.getElementById('qa-body').innerHTML = '<tr class="loading-row"><td colspan="9"><span class="loading-dot">●</span><span class="loading-dot"> ●</span><span class="loading-dot"> ●</span></td></tr>';
  try {
    const r = await fetch(url);
    if (!r.ok) { document.getElementById('qa-body').innerHTML = '<tr><td colspan="9" style="padding:32px 28px;color:var(--red);">QA DB not configured.</td></tr>'; return; }
    const d = await r.json();
    currentPage = d.page; totalPages = d.pages;
    const st = d.stats || {};
    document.getElementById('s-total').textContent = st.total ?? '—';
    document.getElementById('s-pass').textContent  = st.pass  ?? '—';
    document.getElementById('s-fail').textContent  = st.fail  ?? '—';
    document.getElementById('s-rate').textContent  = st.total ? Math.round(st.pass / st.total * 100) + '%' : '—';
    document.getElementById('page-info').textContent = `Page ${currentPage} of ${totalPages} (${d.total} records)`;
    document.getElementById('btn-prev').disabled = currentPage <= 1;
    document.getElementById('btn-next').disabled = currentPage >= totalPages;

    if (!d.docs || !d.docs.length) {
      document.getElementById('qa-body').innerHTML = '<tr><td colspan="9" style="padding:32px 28px;text-align:center;color:var(--text-dim);">No records found</td></tr>';
      return;
    }

    const offset = (currentPage - 1) * 50;
    document.getElementById('qa-body').innerHTML = d.docs.map((doc, i) => {
      const rowId = 'r' + i;
      const issues = doc.issues || [];
      const issueCount = issues.length;
      const issueSummary = issueCount ? `<span class="badge badge-red">${issueCount} issue${issueCount>1?'s':''}</span>` : '<span class="badge badge-green">Clean</span>';
      const identityRows = [
        doc.visitor_id   ? `<div class="expand-section"><div class="expand-label">Visitor ID</div><div class="expand-value">${escHtml(doc.visitor_id)}</div></div>` : '',
        doc.lead_id      ? `<div class="expand-section"><div class="expand-label">Lead ID</div><div class="expand-value">${escHtml(doc.lead_id)}</div></div>` : '',
        doc.conversation_id ? `<div class="expand-section"><div class="expand-label">Conv ID</div><div class="expand-value">${escHtml(doc.conversation_id)}</div></div>` : '',
        doc.evaluated_at ? `<div class="expand-section"><div class="expand-label">Evaluated At</div><div class="expand-value">${fmtDate(doc.evaluated_at)}</div></div>` : '',
        doc.summary      ? `<div class="expand-section" style="flex:1;min-width:300px;"><div class="expand-label">Summary</div><div style="font-size:12px;color:var(--text-muted);">${escHtml(doc.summary)}</div></div>` : '',
      ].filter(Boolean).join('');

      const issueHtml = issueCount ? `<div class="expand-section" style="width:100%;"><div class="expand-label">Issues</div><div class="issue-list">${
        issues.map(iss => `<div class="issue-item sev-${iss.severity||'LOW'}"><span class="badge ${iss.severity==='HIGH'?'badge-red':iss.severity==='MEDIUM'?'badge-amber':'badge-gray'}" style="flex-shrink:0;">${iss.severity||'?'}</span><span style="font-size:11px;font-family:'DM Mono',monospace;color:var(--text-muted);margin-right:6px;">${escHtml(iss.type||'')}</span><span style="font-size:12px;">${escHtml(iss.detail||'')}</span></div>`).join('')
      }</div></div>` : '';

      const userFlagsHtml = (doc.user_flags && doc.user_flags.length) ? `<div class="expand-section"><div class="expand-label">User Flags</div><div style="display:flex;flex-wrap:wrap;gap:4px;">${doc.user_flags.map(f=>`<span class="badge badge-amber">${escHtml(f)}</span>`).join('')}</div></div>` : '';

      return `
        <tr onclick="toggleExpand('${rowId}')" style="cursor:pointer;">
          <td style="font-family:'DM Mono',monospace;font-size:11px;color:var(--text-dim);">${offset+i+1}</td>
          <td>${statusBadge(doc.status)}</td>
          <td style="font-family:'DM Mono',monospace;font-size:13px;">${doc.score != null ? doc.score.toFixed ? doc.score.toFixed(1) : doc.score : '—'}</td>
          <td style="font-family:'DM Mono',monospace;font-size:11px;color:var(--text-dim);max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escHtml(doc.visitor_id||'')}">${escHtml((doc.visitor_id||'—').slice(0,16))}…</td>
          <td style="font-family:'DM Mono',monospace;font-size:11px;color:var(--text-dim);max-width:100px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escHtml(doc.lead_id||'')}">${escHtml((doc.lead_id||'—').slice(0,14))}</td>
          <td style="font-family:'DM Mono',monospace;font-size:11px;color:var(--text-dim);max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escHtml(doc.conversation_id||'')}">${escHtml((doc.conversation_id||'—').slice(0,16))}…</td>
          <td>${issueSummary}</td>
          <td style="font-family:'DM Mono',monospace;font-size:12px;">${doc.turn_count ?? '—'}</td>
          <td style="font-size:12px;color:var(--text-muted);">${fmtDate(doc.conversation_date || doc.evaluated_at)}</td>
        </tr>
        <tr class="expand-row" id="${rowId}">
          <td class="expand-cell" colspan="9">
            <div class="expand-cell-inner">${identityRows}${issueHtml}${userFlagsHtml}</div>
          </td>
        </tr>`;
    }).join('');
  } catch(e) {
    document.getElementById('qa-body').innerHTML = '<tr><td colspan="9" style="padding:32px 28px;color:var(--red);">Error loading data.</td></tr>';
  }
}

function toggleExpand(id) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle('open');
}

loadPage(1);
</script>
</body></html>
"""


QA_VOICE_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>ASBL · Voice QA</title>""" + SHARED_HEAD + """</head>
<body>
<header class="topbar">
  <div style="display:flex;align-items:center;gap:0;">
    <div class="topbar-logo" onclick="location.href='/'" style="cursor:pointer;" title="Go to Leads">
      <div class="logo-mark">AS</div>
      <div class="logo-text">
        <span class="logo-primary">ASBL</span>
        <span class="logo-sub">Intelligence Platform</span>
      </div>
    </div>
    <nav class="nav-links">
      <a href="/" class="nav-link">Leads</a>
      <a href="/bulk-calls" class="nav-link">Bulk Calls</a>
      <a href="/qa" class="nav-link active">QA</a>
    <a href="/pipeline" class="nav-link">Pipeline</a>
      <a href="/main-dashboard" class="nav-link">Main Dashboard</a>
    </nav>
  </div>
  <div style="display:flex;align-items:center;gap:10px;">
    <a href="/qa" class="btn" style="text-decoration:none;font-size:12px;">Overview</a>
    <a href="/qa/chatbot" class="btn" style="text-decoration:none;font-size:12px;">Chatbot QA</a>
    <a href="/qa/analytics" class="btn" style="text-decoration:none;font-size:12px;">Analytics</a>
    <a href="/qa/spam" class="btn" style="text-decoration:none;font-size:12px;">Spam</a>
    <a href="/qa/feedback" class="btn" style="text-decoration:none;font-size:12px;">Feedback</a>
    <a href="/qa/recommendations" class="btn" style="text-decoration:none;font-size:12px;">Recommendations</a>
    <span style="font-size:11px;color:var(--text-muted);">{{ user_name }}</span>
    <a href="/api/logout" class="btn" style="text-decoration:none;">Logout</a>
  </div>
</header>

<div class="stat-strip">
  <div class="stat-pill"><span class="stat-pill-value" id="s-total">—</span><span class="stat-pill-label">Total</span></div>
  <div class="stat-pill"><span class="stat-pill-value" id="s-pass">—</span><span class="stat-pill-label">Pass</span></div>
  <div class="stat-pill"><span class="stat-pill-value" id="s-fail">—</span><span class="stat-pill-label">Fail</span></div>
  <div class="stat-pill"><span class="stat-pill-value" id="s-rate">—</span><span class="stat-pill-label">Pass Rate</span></div>
</div>

<div class="qa-page">
  <div class="qa-filter-bar">
    <button class="qa-filter-btn active" id="f-all"  onclick="setStatus('ALL')">All</button>
    <button class="qa-filter-btn"        id="f-pass" onclick="setStatus('PASS')">Pass</button>
    <button class="qa-filter-btn"        id="f-fail" onclick="setStatus('FAIL')">Fail</button>
    <span style="font-size:11px;color:var(--text-muted);margin-left:8px;">From</span>
    <input class="qa-date-input" type="date" id="date-from" onchange="loadPage(1)" />
    <span style="font-size:11px;color:var(--text-muted);">To</span>
    <input class="qa-date-input" type="date" id="date-to" onchange="loadPage(1)" />
  </div>

  <div style="overflow-x:auto;">
    <table>
      <thead><tr>
        <th style="width:40px;">#</th>
        <th>Status</th><th>Score</th><th>Call SID</th>
        <th>Language</th><th>Lang Compliance</th><th>Issues</th><th>Call Date</th>
      </tr></thead>
      <tbody id="qa-body"><tr class="loading-row"><td colspan="8"><span class="loading-dot">●</span><span class="loading-dot"> ●</span><span class="loading-dot"> ●</span> Loading…</td></tr></tbody>
    </table>
  </div>
  <div class="qa-paginator">
    <button id="btn-prev" onclick="loadPage(currentPage-1)" disabled>← Prev</button>
    <span id="page-info">—</span>
    <button id="btn-next" onclick="loadPage(currentPage+1)" disabled>Next →</button>
  </div>
</div>

<script>
let currentPage = 1, totalPages = 1, statusFilter = 'ALL';

function setStatus(s) {
  statusFilter = s;
  ['all','pass','fail'].forEach(x => document.getElementById('f-'+x).classList.toggle('active', x === s.toLowerCase()));
  loadPage(1);
}

function statusBadge(s) {
  if (s === 'PASS') return '<span class="badge badge-green">PASS</span>';
  if (s === 'FAIL') return '<span class="badge badge-red">FAIL</span>';
  return `<span class="badge badge-gray">${s||'—'}</span>`;
}

function fmtDate(iso) {
  if (!iso) return '—';
  try { return new Date(iso).toLocaleDateString('en-IN', {day:'numeric',month:'short',year:'numeric'}); } catch(e){ return iso; }
}

function escHtml(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

async function loadPage(p) {
  p = Math.max(1, p);
  const dateFrom = document.getElementById('date-from').value;
  const dateTo   = document.getElementById('date-to').value;
  let url = `/api/qa/voice?page=${p}&limit=50&status=${statusFilter}`;
  if (dateFrom) url += `&dateFrom=${dateFrom}`;
  if (dateTo)   url += `&dateTo=${dateTo}`;

  document.getElementById('qa-body').innerHTML = '<tr class="loading-row"><td colspan="8"><span class="loading-dot">●</span><span class="loading-dot"> ●</span><span class="loading-dot"> ●</span></td></tr>';
  try {
    const r = await fetch(url);
    if (!r.ok) { document.getElementById('qa-body').innerHTML = '<tr><td colspan="8" style="padding:32px 28px;color:var(--red);">QA DB not configured.</td></tr>'; return; }
    const d = await r.json();
    currentPage = d.page; totalPages = d.pages;
    const st = d.stats || {};
    document.getElementById('s-total').textContent = st.total ?? '—';
    document.getElementById('s-pass').textContent  = st.pass  ?? '—';
    document.getElementById('s-fail').textContent  = st.fail  ?? '—';
    document.getElementById('s-rate').textContent  = st.total ? Math.round(st.pass / st.total * 100) + '%' : '—';
    document.getElementById('page-info').textContent = `Page ${currentPage} of ${totalPages} (${d.total} records)`;
    document.getElementById('btn-prev').disabled = currentPage <= 1;
    document.getElementById('btn-next').disabled = currentPage >= totalPages;

    if (!d.docs || !d.docs.length) {
      document.getElementById('qa-body').innerHTML = '<tr><td colspan="8" style="padding:32px 28px;text-align:center;color:var(--text-dim);">No records found</td></tr>';
      return;
    }

    const offset = (currentPage - 1) * 50;
    document.getElementById('qa-body').innerHTML = d.docs.map((doc, i) => {
      const rowId = 'r' + i;
      const issues = doc.issues || [];
      const issueCount = issues.length;
      const issueSummary = issueCount ? `<span class="badge badge-red">${issueCount} issue${issueCount>1?'s':''}</span>` : '<span class="badge badge-green">Clean</span>';
      const langComp = doc.language_compliance;
      const langBadge = langComp === true ? '<span class="badge badge-green">Yes</span>' : langComp === false ? '<span class="badge badge-red">No</span>' : '<span class="badge badge-gray">—</span>';

      const expandContent = [
        doc.started_at  ? `<div class="expand-section"><div class="expand-label">Call Started</div><div class="expand-value">${fmtDate(doc.started_at)}</div></div>` : '',
        doc.evaluated_at? `<div class="expand-section"><div class="expand-label">Evaluated At</div><div class="expand-value">${fmtDate(doc.evaluated_at)}</div></div>` : '',
        doc.call_sid    ? `<div class="expand-section"><div class="expand-label">Call SID</div><div class="expand-value">${escHtml(doc.call_sid)}</div></div>` : '',
        doc.summary     ? `<div class="expand-section" style="flex:1;min-width:300px;"><div class="expand-label">Summary</div><div style="font-size:12px;color:var(--text-muted);">${escHtml(doc.summary)}</div></div>` : '',
      ].filter(Boolean).join('');

      const issueHtml = issueCount ? `<div class="expand-section" style="width:100%;"><div class="expand-label">Issues</div><div class="issue-list">${
        issues.map(iss => `<div class="issue-item sev-${iss.severity||'LOW'}"><span class="badge ${iss.severity==='HIGH'?'badge-red':iss.severity==='MEDIUM'?'badge-amber':'badge-gray'}" style="flex-shrink:0;">${iss.severity||'?'}</span><span style="font-size:11px;font-family:'DM Mono',monospace;color:var(--text-muted);margin-right:6px;">${escHtml(iss.type||'')}</span><span style="font-size:12px;">${escHtml(iss.detail||'')}</span></div>`).join('')
      }</div></div>` : '';

      return `
        <tr onclick="toggleExpand('${rowId}')" style="cursor:pointer;">
          <td style="font-family:'DM Mono',monospace;font-size:11px;color:var(--text-dim);">${offset+i+1}</td>
          <td>${statusBadge(doc.status)}</td>
          <td style="font-family:'DM Mono',monospace;font-size:13px;">${doc.score != null ? (doc.score.toFixed ? doc.score.toFixed(1) : doc.score) : '—'}</td>
          <td style="font-family:'DM Mono',monospace;font-size:11px;color:var(--text-dim);max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escHtml((doc.call_sid||'—').slice(0,18))}</td>
          <td><span class="badge badge-gray">${escHtml(doc.language||'—')}</span></td>
          <td>${langBadge}</td>
          <td>${issueSummary}</td>
          <td style="font-size:12px;color:var(--text-muted);">${fmtDate(doc.conversation_date || doc.evaluated_at)}</td>
        </tr>
        <tr class="expand-row" id="${rowId}">
          <td class="expand-cell" colspan="8">
            <div class="expand-cell-inner">${expandContent}${issueHtml}</div>
          </td>
        </tr>`;
    }).join('');
  } catch(e) {
    document.getElementById('qa-body').innerHTML = '<tr><td colspan="8" style="padding:32px 28px;color:var(--red);">Error loading data.</td></tr>';
  }
}

function toggleExpand(id) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle('open');
}

loadPage(1);
</script>
</body></html>
"""


QA_ANALYTICS_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>ASBL · QA Analytics</title>""" + SHARED_HEAD + """</head>
<body>
<header class="topbar">
  <div style="display:flex;align-items:center;gap:0;">
    <div class="topbar-logo" onclick="location.href='/'" style="cursor:pointer;" title="Go to Leads">
      <div class="logo-mark">AS</div>
      <div class="logo-text">
        <span class="logo-primary">ASBL</span>
        <span class="logo-sub">Intelligence Platform</span>
      </div>
    </div>
    <nav class="nav-links">
      <a href="/" class="nav-link">Leads</a>
      <a href="/bulk-calls" class="nav-link">Bulk Calls</a>
      <a href="/qa" class="nav-link active">QA</a>
    <a href="/pipeline" class="nav-link">Pipeline</a>
      <a href="/main-dashboard" class="nav-link">Main Dashboard</a>
    </nav>
  </div>
  <div style="display:flex;align-items:center;gap:10px;">
    <a href="/qa" class="btn" style="text-decoration:none;font-size:12px;">Overview</a>
    <a href="/qa/chatbot" class="btn" style="text-decoration:none;font-size:12px;">Chatbot QA</a>
    <a href="/qa/voice" class="btn" style="text-decoration:none;font-size:12px;">Voice QA</a>
    <a href="/qa/spam" class="btn" style="text-decoration:none;font-size:12px;">Spam</a>
    <a href="/qa/feedback" class="btn" style="text-decoration:none;font-size:12px;">Feedback</a>
    <a href="/qa/recommendations" class="btn" style="text-decoration:none;font-size:12px;">Recommendations</a>
    <span style="font-size:11px;color:var(--text-muted);">{{ user_name }}</span>
    <a href="/api/logout" class="btn" style="text-decoration:none;">Logout</a>
  </div>
</header>

<div class="qa-page">
  <div id="run-info" style="font-family:'DM Mono',monospace;font-size:11px;color:var(--text-muted);margin-bottom:20px;"></div>
  <div id="health-cards" style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:28px;max-width:600px;"></div>
  <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.14em;color:var(--text-muted);margin-bottom:12px;">Flags from Latest Run</div>
  <div style="overflow-x:auto;">
    <table>
      <thead><tr><th>Severity</th><th>Type</th><th>Source</th><th>Detail</th></tr></thead>
      <tbody id="flags-body"><tr class="loading-row"><td colspan="4"><span class="loading-dot">●</span><span class="loading-dot"> ●</span><span class="loading-dot"> ●</span> Loading…</td></tr></tbody>
    </table>
  </div>
</div>

<script>
function severityBadge(s) {
  if (!s) return '';
  const cls = s === 'HIGH' ? 'badge-red' : s === 'MEDIUM' ? 'badge-amber' : 'badge-gray';
  return `<span class="badge ${cls}">${s}</span>`;
}

function healthCard(label, score) {
  const cls = score >= 8 ? 'health-score-green' : score >= 6 ? 'health-score-amber' : 'health-score-red';
  return `<div class="health-score-card ${cls}">
    <div class="health-score-num">${(score||0).toFixed(1)}</div>
    <div><div style="font-weight:600;font-size:14px;margin-bottom:4px;">${label}</div><div style="font-size:11px;opacity:0.75;">out of 10</div></div>
  </div>`;
}

function escHtml(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function load() {
  try {
    const r = await fetch('/api/qa/analytics');
    if (!r.ok) { document.getElementById('flags-body').innerHTML = '<tr><td colspan="4" style="padding:32px;color:var(--red);">QA DB not configured.</td></tr>'; return; }
    const d = await r.json();
    if (d.run_at) document.getElementById('run-info').textContent = 'Latest analytics run: ' + new Date(d.run_at).toLocaleString('en-IN');
    const h = d.health || {};
    document.getElementById('health-cards').innerHTML = healthCard('Chatbot Health', h.chatbot || 0) + healthCard('Voice Health', h.voice || 0);
    const flags = d.flags || [];
    if (!flags.length) {
      document.getElementById('flags-body').innerHTML = '<tr><td colspan="4" style="padding:32px;text-align:center;color:var(--text-dim);">No flags in latest run</td></tr>';
      return;
    }
    document.getElementById('flags-body').innerHTML = flags.map(f => `<tr>
      <td>${severityBadge(f.severity)}</td>
      <td><span class="badge badge-gray" style="font-family:'DM Mono',monospace;">${escHtml(f.type||'—')}</span></td>
      <td><span class="badge badge-source">${escHtml(f.source||'—')}</span></td>
      <td style="font-size:12px;color:var(--text-muted);max-width:400px;">${escHtml((f.detail||'').slice(0,150))}</td>
    </tr>`).join('');
  } catch(e) {
    document.getElementById('flags-body').innerHTML = '<tr><td colspan="4" style="padding:32px;color:var(--red);">Error loading analytics.</td></tr>';
  }
}
load();
</script>
</body></html>
"""


QA_SPAM_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>ASBL · QA Spam</title>""" + SHARED_HEAD + """</head>
<body>
<header class="topbar">
  <div style="display:flex;align-items:center;gap:0;">
    <div class="topbar-logo" onclick="location.href='/'" style="cursor:pointer;" title="Go to Leads">
      <div class="logo-mark">AS</div>
      <div class="logo-text">
        <span class="logo-primary">ASBL</span>
        <span class="logo-sub">Intelligence Platform</span>
      </div>
    </div>
    <nav class="nav-links">
      <a href="/" class="nav-link">Leads</a>
      <a href="/bulk-calls" class="nav-link">Bulk Calls</a>
      <a href="/qa" class="nav-link active">QA</a>
    <a href="/pipeline" class="nav-link">Pipeline</a>
      <a href="/main-dashboard" class="nav-link">Main Dashboard</a>
    </nav>
  </div>
  <div style="display:flex;align-items:center;gap:10px;">
    <a href="/qa" class="btn" style="text-decoration:none;font-size:12px;">Overview</a>
    <a href="/qa/chatbot" class="btn" style="text-decoration:none;font-size:12px;">Chatbot QA</a>
    <a href="/qa/voice" class="btn" style="text-decoration:none;font-size:12px;">Voice QA</a>
    <a href="/qa/analytics" class="btn" style="text-decoration:none;font-size:12px;">Analytics</a>
    <span style="font-size:11px;color:var(--text-muted);">{{ user_name }}</span>
    <a href="/api/logout" class="btn" style="text-decoration:none;">Logout</a>
  </div>
</header>

<div class="qa-page">
  <div id="type-pills" style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px;"></div>
  <div style="overflow-x:auto;">
    <table>
      <thead><tr>
        <th style="width:40px;">#</th>
        <th>Conversation ID</th><th>Turn</th><th>Flag Type</th><th>Layer</th><th>Message</th>
      </tr></thead>
      <tbody id="spam-body"><tr class="loading-row"><td colspan="6"><span class="loading-dot">●</span><span class="loading-dot"> ●</span><span class="loading-dot"> ●</span> Loading…</td></tr></tbody>
    </table>
  </div>
  <div class="qa-paginator">
    <button id="btn-prev" onclick="loadPage(currentPage-1)" disabled>← Prev</button>
    <span id="page-info">—</span>
    <button id="btn-next" onclick="loadPage(currentPage+1)" disabled>Next →</button>
  </div>
</div>

<script>
let currentPage = 1, totalPages = 1, typeFilter = '';

function escHtml(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function setType(t) {
  typeFilter = (typeFilter === t) ? '' : t;
  document.querySelectorAll('.spam-type-pill').forEach(p => p.classList.toggle('active', p.dataset.type === typeFilter));
  loadPage(1);
}

async function loadPage(p) {
  p = Math.max(1, p);
  let url = `/api/qa/spam?page=${p}&limit=50`;
  if (typeFilter) url += `&type=${encodeURIComponent(typeFilter)}`;

  document.getElementById('spam-body').innerHTML = '<tr class="loading-row"><td colspan="6"><span class="loading-dot">●</span><span class="loading-dot"> ●</span><span class="loading-dot"> ●</span></td></tr>';
  try {
    const r = await fetch(url);
    if (!r.ok) { document.getElementById('spam-body').innerHTML = '<tr><td colspan="6" style="padding:32px;color:var(--red);">QA DB not configured.</td></tr>'; return; }
    const d = await r.json();
    currentPage = p; totalPages = d.pages || 1;
    document.getElementById('page-info').textContent = `Page ${currentPage} of ${totalPages} (${d.total} records)`;
    document.getElementById('btn-prev').disabled = currentPage <= 1;
    document.getElementById('btn-next').disabled = currentPage >= totalPages;

    // Render type pills
    const tc = d.type_counts || {};
    const KNOWN_TYPES = ['LINK_URL','PROMO_SCAM','SOCIAL_VIRAL','RELIGIOUS','AUTO_REPLY','JOB_QUERY','VULGAR_ABUSIVE','KEYSMASH','PERSONAL_IRRELEVANT'];
    const allTypes = [...new Set([...KNOWN_TYPES, ...Object.keys(tc)])];
    document.getElementById('type-pills').innerHTML = allTypes.map(t =>
      `<span class="spam-type-pill${typeFilter===t?' active':''}" data-type="${t}" onclick="setType('${t}')">${t} <span class="spam-type-count">${tc[t]||0}</span></span>`
    ).join('');

    if (!d.docs || !d.docs.length) {
      document.getElementById('spam-body').innerHTML = '<tr><td colspan="6" style="padding:32px;text-align:center;color:var(--text-dim);">No spam records found</td></tr>';
      return;
    }

    const offset = (currentPage - 1) * 50;
    document.getElementById('spam-body').innerHTML = d.docs.map((doc, i) => {
      const flags = doc.spam_flags || [];
      if (!flags.length) {
        return `<tr>
          <td style="font-family:'DM Mono',monospace;font-size:11px;color:var(--text-dim);">${offset+i+1}</td>
          <td style="font-family:'DM Mono',monospace;font-size:11px;color:var(--text-dim);">${escHtml((doc.conversation_id||'—').slice(0,20))}</td>
          <td>—</td><td>—</td><td>—</td>
          <td style="font-size:12px;color:var(--text-muted);">${escHtml((doc.summary||'Spam filtered').slice(0,100))}</td>
        </tr>`;
      }
      return flags.map((flag, fi) => `<tr>
        <td style="font-family:'DM Mono',monospace;font-size:11px;color:var(--text-dim);">${fi===0?offset+i+1:''}</td>
        <td style="font-family:'DM Mono',monospace;font-size:11px;color:var(--text-dim);">${fi===0?escHtml((doc.conversation_id||'—').slice(0,20)):''}</td>
        <td style="font-family:'DM Mono',monospace;font-size:11px;">${escHtml(flag.turn ?? '—')}</td>
        <td><span class="badge badge-amber" style="font-family:'DM Mono',monospace;">${escHtml(flag.type||'—')}</span></td>
        <td><span class="badge badge-gray">${escHtml(flag.layer||'—')}</span></td>
        <td style="font-size:12px;color:var(--text-muted);max-width:350px;">${escHtml((flag.message||flag.detail||'').slice(0,120))}</td>
      </tr>`).join('');
    }).join('');
  } catch(e) {
    document.getElementById('spam-body').innerHTML = '<tr><td colspan="6" style="padding:32px;color:var(--red);">Error loading spam data.</td></tr>';
  }
}

loadPage(1);
</script>
</body></html>
"""


QA_FEEDBACK_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>ASBL · QA Feedback</title>""" + SHARED_HEAD + """</head>
<body>
<header class="topbar">
  <div style="display:flex;align-items:center;gap:0;">
    <div class="topbar-logo" onclick="location.href='/'" style="cursor:pointer;" title="Go to Leads">
      <div class="logo-mark">AS</div>
      <div class="logo-text">
        <span class="logo-primary">ASBL</span>
        <span class="logo-sub">Intelligence Platform</span>
      </div>
    </div>
    <nav class="nav-links">
      <a href="/" class="nav-link">Leads</a>
      <a href="/bulk-calls" class="nav-link">Bulk Calls</a>
      <a href="/qa" class="nav-link active">QA</a>
    <a href="/pipeline" class="nav-link">Pipeline</a>
      <a href="/main-dashboard" class="nav-link">Main Dashboard</a>
    </nav>
  </div>
  <div style="display:flex;align-items:center;gap:10px;">
    <a href="/qa" class="btn" style="text-decoration:none;font-size:12px;">Overview</a>
    <a href="/qa/chatbot" class="btn" style="text-decoration:none;font-size:12px;">Chatbot QA</a>
    <a href="/qa/voice" class="btn" style="text-decoration:none;font-size:12px;">Voice QA</a>
    <a href="/qa/analytics" class="btn" style="text-decoration:none;font-size:12px;">Analytics</a>
    <a href="/qa/spam" class="btn" style="text-decoration:none;font-size:12px;">Spam</a>
    <a href="/qa/recommendations" class="btn" style="text-decoration:none;font-size:12px;">Recommendations</a>
    <span style="font-size:11px;color:var(--text-muted);">{{ user_name }}</span>
    <a href="/api/logout" class="btn" style="text-decoration:none;">Logout</a>
  </div>
</header>

<div class="qa-page">
  <div id="run-info" style="font-family:'DM Mono',monospace;font-size:11px;color:var(--text-muted);margin-bottom:20px;"></div>
  <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.14em;color:var(--text-muted);margin-bottom:12px;">Identified Problems</div>
  <div id="problems-container"></div>
</div>

<script>
function urgencyBadge(u) {
  const cls = u==='HIGH'?'badge-red':u==='MEDIUM'?'badge-amber':'badge-gray';
  return `<span class="badge ${cls}">${u||'—'}</span>`;
}
function escHtml(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function load() {
  const c = document.getElementById('problems-container');
  try {
    const r = await fetch('/api/qa/feedback');
    if (!r.ok) { c.innerHTML = '<p style="color:var(--red);">QA DB not configured.</p>'; return; }
    const d = await r.json();
    const fb = d.latest;
    if (!fb) { c.innerHTML = '<p style="color:var(--text-dim);padding:32px 0;">No feedback data yet.</p>'; return; }
    document.getElementById('run-info').textContent = 'Latest run: ' + new Date(fb.submitted_at).toLocaleString('en-IN');
    const problems = fb.problems || [];
    if (!problems.length) {
      c.innerHTML = '<div style="padding:40px 0;text-align:center;color:var(--text-dim);font-size:14px;">No problems identified — system is healthy.</div>';
      return;
    }
    c.innerHTML = problems.map(p => `
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px 24px;margin-bottom:16px;">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
          ${urgencyBadge(p.urgency)}
          <span style="font-size:14px;font-weight:500;">${escHtml(p.title||'Untitled')}</span>
        </div>
        <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px;"><strong>What's wrong:</strong> ${escHtml(p.what_is_wrong||'—')}</div>
        ${p.what_is_not_wrong?`<div style="font-size:12px;color:var(--text-muted);margin-bottom:8px;"><strong>What's not wrong:</strong> ${escHtml(p.what_is_not_wrong)}</div>`:''}
        ${(p.evidence||[]).length?`<div style="font-size:11px;font-family:'DM Mono',monospace;color:var(--text-dim);margin-top:8px;">${(p.evidence||[]).map(e=>`• ${escHtml(e)}`).join('<br>')}</div>`:''}
      </div>`).join('');
  } catch(e) {
    c.innerHTML = '<p style="color:var(--red);">Error loading feedback.</p>';
  }
}
load();
</script>
</body></html>
"""


QA_RECOMMENDATIONS_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>ASBL · QA Recommendations</title>""" + SHARED_HEAD + """</head>
<body>
<header class="topbar">
  <div style="display:flex;align-items:center;gap:0;">
    <div class="topbar-logo" onclick="location.href='/'" style="cursor:pointer;" title="Go to Leads">
      <div class="logo-mark">AS</div>
      <div class="logo-text">
        <span class="logo-primary">ASBL</span>
        <span class="logo-sub">Intelligence Platform</span>
      </div>
    </div>
    <nav class="nav-links">
      <a href="/" class="nav-link">Leads</a>
      <a href="/bulk-calls" class="nav-link">Bulk Calls</a>
      <a href="/qa" class="nav-link active">QA</a>
    <a href="/pipeline" class="nav-link">Pipeline</a>
      <a href="/main-dashboard" class="nav-link">Main Dashboard</a>
    </nav>
  </div>
  <div style="display:flex;align-items:center;gap:10px;">
    <a href="/qa" class="btn" style="text-decoration:none;font-size:12px;">Overview</a>
    <a href="/qa/chatbot" class="btn" style="text-decoration:none;font-size:12px;">Chatbot QA</a>
    <a href="/qa/voice" class="btn" style="text-decoration:none;font-size:12px;">Voice QA</a>
    <a href="/qa/analytics" class="btn" style="text-decoration:none;font-size:12px;">Analytics</a>
    <a href="/qa/spam" class="btn" style="text-decoration:none;font-size:12px;">Spam</a>
    <a href="/qa/feedback" class="btn" style="text-decoration:none;font-size:12px;">Feedback</a>
    <span style="font-size:11px;color:var(--text-muted);">{{ user_name }}</span>
    <a href="/api/logout" class="btn" style="text-decoration:none;">Logout</a>
  </div>
</header>

<div class="qa-page">
  <div id="run-info" style="font-family:'DM Mono',monospace;font-size:11px;color:var(--text-muted);margin-bottom:20px;"></div>
  <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.14em;color:var(--text-muted);margin-bottom:12px;">Recommended Fixes</div>
  <div id="fixes-container"></div>
</div>

<script>
function escHtml(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function load() {
  const c = document.getElementById('fixes-container');
  try {
    const r = await fetch('/api/qa/recommendations');
    if (!r.ok) { c.innerHTML = '<p style="color:var(--red);">QA DB not configured.</p>'; return; }
    const d = await r.json();
    const rec = d.latest;
    if (!rec) { c.innerHTML = '<p style="color:var(--text-dim);padding:32px 0;">No recommendations yet.</p>'; return; }
    document.getElementById('run-info').textContent = 'Generated: ' + new Date(rec.generated_at).toLocaleString('en-IN');
    const fixes = rec.fixes || [];
    if (!fixes.length) {
      c.innerHTML = '<div style="padding:40px 0;text-align:center;color:var(--text-dim);font-size:14px;">No fixes recommended — system is healthy.</div>';
      return;
    }
    c.innerHTML = fixes.map((f,i) => `
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px 24px;margin-bottom:16px;">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
          <span style="background:var(--accent);color:#FAF7F2;border-radius:50%;width:24px;height:24px;display:inline-flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;">${f.rank||i+1}</span>
          <span style="font-size:14px;font-weight:500;">${escHtml(f.problem||'Untitled')}</span>
        </div>
        <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px;"><strong>Fix:</strong> ${escHtml(f.fix||'—')}</div>
        ${f.where?`<div style="font-size:12px;color:var(--text-muted);margin-bottom:8px;"><strong>Where:</strong> <span style="font-family:'DM Mono',monospace;">${escHtml(f.where)}</span></div>`:''}
        ${f.expected_outcome?`<div style="font-size:12px;color:var(--text-dim);"><strong>Expected outcome:</strong> ${escHtml(f.expected_outcome)}</div>`:''}
      </div>`).join('');
  } catch(e) {
    c.innerHTML = '<p style="color:var(--red);">Error loading recommendations.</p>';
  }
}
load();
</script>
</body></html>
"""

# ── QA Page Routes ────────────────────────────────────────────────────────────
@app.route("/qa")
def qa_overview():
    return render_template_string(QA_OVERVIEW_HTML,
        user_name=session.get("name",""),
        user_role=session.get("role",""))

@app.route("/qa/chatbot")
def qa_chatbot():
    return render_template_string(QA_CHATBOT_HTML,
        user_name=session.get("name",""),
        user_role=session.get("role",""))

@app.route("/qa/voice")
def qa_voice():
    return render_template_string(QA_VOICE_HTML,
        user_name=session.get("name",""),
        user_role=session.get("role",""))

@app.route("/qa/analytics")
def qa_analytics():
    return render_template_string(QA_ANALYTICS_HTML,
        user_name=session.get("name",""),
        user_role=session.get("role",""))

@app.route("/qa/spam")
def qa_spam():
    return render_template_string(QA_SPAM_HTML,
        user_name=session.get("name",""),
        user_role=session.get("role",""))

@app.route("/qa/feedback")
def qa_feedback():
    return render_template_string(QA_FEEDBACK_HTML,
        user_name=session.get("name",""),
        user_role=session.get("role",""))

@app.route("/qa/recommendations")
def qa_recommendations():
    return render_template_string(QA_RECOMMENDATIONS_HTML,
        user_name=session.get("name",""),
        user_role=session.get("role",""))

# ═════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print()
    print("  ┌──────────────────────────────────────────────┐")
    print("  │   ASBL Lead Intelligence Dashboard           │")
    print("  │   Opening at http://localhost:5000           │")
    print("  │   Press Ctrl+C to stop                       │")
    print("  └──────────────────────────────────────────────┘")
    print()
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
