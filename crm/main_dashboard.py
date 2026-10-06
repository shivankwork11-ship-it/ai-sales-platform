"""
main_dashboard.py  —  "Main Dashboard" tab (ANGAD funnel dashboard)
===================================================================
NEW, ADDITIVE module. It does NOT touch any existing dashboard logic.

The ANGAD dashboard (in ./angad_app) is a self-contained stdlib `http.server`
app. We run it UNMODIFIED as a separate process bound to an internal loopback
port, and this Flask blueprint reverse-proxies it into the lead dashboard as a
new "/main-dashboard" tab, behind the app's existing login.

Why a proxy (and not an import): ANGAD is not Flask/WSGI — it has its own HTTP
server, its own security gate (loopback + per-run token), and serves a 176 KB
single-page UI whose JS fetches absolute `/api/...` URLs. We keep it exactly as
the dev shipped it and only:
  1. inject ANGAD's per-run token on every forwarded request (captured from its
     stdout by the container entrypoint — see Dockerfile/entrypoint.sh), and
  2. rewrite the served page's `/api/...` fetch paths to `/main-dashboard/api/...`
     so the browser's calls come back through this proxy.
No ANGAD business logic, markup or numbers are altered — only URL routing.

Auth: `/main-dashboard*` is NOT in dashboard.py's `_PUBLIC_ROUTES`, so the app's
`require_login` before_request already protects it (page → /login, api → 401).
ANGAD is GET-only, so the app's CSRF check (POST/PUT/... only) never applies.
"""

import os
from flask import Blueprint, request, Response

main_dashboard_bp = Blueprint("main_dashboard", __name__)

# Where the unmodified ANGAD server listens (loopback, inside the same container).
ANGAD_BASE = os.environ.get("ANGAD_INTERNAL_URL", "http://127.0.0.1:8777").rstrip("/")
PREFIX = "/main-dashboard"

# Request timeout for talking to the local ANGAD process. Its heavy endpoints
# (`/api/refresh`) return immediately and run sync.py in a background thread, so a
# generous-but-bounded timeout is plenty.
_TIMEOUT = int(os.environ.get("ANGAD_PROXY_TIMEOUT", "300"))

# Floating "back to the lead dashboard" link injected into ANGAD's page (see the
# HTML branch below). A plain anchor to "/" (the Leads tab) — no JS, inline style
# only (allowed by ANGAD's CSP). Fixed bottom-right so it never overlaps ANGAD's
# left sidebar or its top-right status badge.
_BACK_LINK = (
    '<a href="/" title="Back to Lead Profiles" style="position:fixed;right:18px;'
    'bottom:18px;z-index:2147483647;background:#9a7b3e;color:#fff;'
    'text-decoration:none;font:600 13px/1 system-ui,-apple-system,sans-serif;'
    'padding:10px 16px;border-radius:10px;box-shadow:0 2px 12px rgba(0,0,0,.28)">'
    '← Lead Profiles</a>'
)


def _angad_token():
    """ANGAD mints a random token per run and requires ?t=<token> on every
    request. The entrypoint captures it from ANGAD's stdout into env/file; we read
    it here. Never modifies ANGAD."""
    t = os.environ.get("ANGAD_TOKEN", "").strip()
    if t:
        return t
    for p in (os.environ.get("ANGAD_TOKEN_FILE"), "/tmp/angad_token"):
        if p and os.path.exists(p):
            try:
                with open(p) as fh:
                    return fh.read().strip()
            except Exception:
                pass
    return ""


# `strict_slashes=False` so both `/main-dashboard` and `/main-dashboard/` serve the
# page without a 308 redirect; the `<path:subpath>` route handles every /api/* call.
@main_dashboard_bp.route(PREFIX, defaults={"subpath": ""}, strict_slashes=False)
@main_dashboard_bp.route(PREFIX + "/<path:subpath>")
def main_dashboard_proxy(subpath):
    # Lazy import so a missing 'requests' never breaks app import; it's a dep here.
    import requests

    target = f"{ANGAD_BASE}/{subpath}"
    # Force the correct token; drop any (stale) client-supplied one.
    params = dict(request.args)
    params["t"] = _angad_token()

    # IMPORTANT: do NOT forward the browser's Origin/Referer/Sec-Fetch headers —
    # ANGAD's gate refuses cross-origin/cross-site. A clean server-side request
    # (no such headers) is treated as trusted loopback traffic.
    try:
        r = requests.get(target, params=params, timeout=_TIMEOUT)
    except requests.RequestException as e:
        return Response(
            "Main Dashboard backend is not reachable yet "
            f"({type(e).__name__}). If this persists, the ANGAD process/data may "
            "still be initialising.",
            status=502, content_type="text/plain; charset=utf-8")

    ctype = r.headers.get("Content-Type", "application/octet-stream")
    body = r.content
    if "text/html" in ctype.lower():
        # ANGAD's page.html fetches absolute '/api/...'; route those back through
        # this proxy. Pure URL rewrite — no markup/logic semantics changed.
        html = r.text.replace("/api/", PREFIX + "/api/")
        # ANGAD is a full-screen UI with no link back to the lead dashboard's nav
        # (and it can't be iframed — its CSP sets frame-ancestors 'none'). Inject a
        # small floating "back" link so users aren't stranded on this tab. Additive
        # overlay only; ANGAD's own markup/logic is untouched, and its CSP allows
        # this (style-src 'unsafe-inline'). No JS — a plain anchor to the app root.
        # ANGAD's page.html has no </body>/</html>, so append at the very end; a
        # position:fixed anchor renders correctly regardless of DOM position.
        if "</body>" in html:
            html = html.replace("</body>", _BACK_LINK + "</body>", 1)
        else:
            html = html + _BACK_LINK
        body = html.encode("utf-8")

    resp = Response(body, status=r.status_code, content_type=ctype)
    # Preserve ANGAD's own security headers (CSP etc.) for this response only.
    for h in ("Content-Security-Policy", "X-Content-Type-Options",
              "Referrer-Policy", "Cache-Control"):
        if h in r.headers:
            resp.headers[h] = r.headers[h]
    return resp


def init_app(app, client=None):
    """Register the Main Dashboard proxy. Mirrors pipeline/crm_v2 init_app so
    dashboard.py wires it identically. `client` is accepted for signature parity
    but unused — ANGAD runs as its own process."""
    app.register_blueprint(main_dashboard_bp)
