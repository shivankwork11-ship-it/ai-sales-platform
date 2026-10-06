"""Always-on lead-profile refresh worker (for Render free web service).

Runs the dashboard's OWN comprehensive refresh (/api/cron/refresh) on a loop, in
process, via Flask's test client — so it covers BOTH website (loft) and Zoho/
Inncircles leads, uses the batched (non-N+1) query path, and updates the
"last_successful_run" freshness marker. Unlike Vercel serverless there is no
request timeout here, so the refresh that kept timing out now runs to completion.

Render free only offers web services, so we also bind a port for the health check
(UptimeRobot keep-alive). Reuses dashboard.py as-is.
"""
import os
import time
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

import dashboard  # the Flask app; /api/cron/refresh + Mongo client live here

# ponytail: /api/cron/refresh is incremental (bounded active set + backlog cap), so
# each run is light. 5-min gap → near-real-time. Server-side 60s throttle protects
# against overlap with the UptimeRobot keep-alive (which hits "/" only, not this).
INTERVAL = int(os.environ.get("REFRESH_INTERVAL_SEC", "300"))

_last = {"ok": None, "at": None, "status": None, "errors": None, "info": None}


def _run_refresh():
    # Authenticate with the shared secret (same CRON_SECRET env the app reads).
    with dashboard.app.test_client() as c:
        r = c.get("/api/cron/refresh",
                  headers={"X-Cron-Key": os.environ.get("CRON_SECRET", "")})
        return r.status_code, (r.get_json() if r.is_json else None)


def _loop():
    while True:
        try:
            code, body = _run_refresh()
            b = body or {}
            # ok = the run completed (HTTP 200); status distinguishes success/partial/
            # failed so a partial run isn't reported as fully healthy (PARTIAL-SUCCESS-01).
            _last.update(ok=(code == 200), status=b.get("status"),
                         errors=b.get("errors"), at=time.time(), info=b)
            print("refresh: %s %s" % (code, body), flush=True)
        except Exception:
            _last.update(ok=False, status="failed", at=time.time(), info="exception")
            traceback.print_exc()
        time.sleep(INTERVAL)


class _Handler(BaseHTTPRequestHandler):
    def _respond(self, with_body):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if with_body:
            self.wfile.write(("last_ok=%s status=%s errors=%s at=%s info=%s"
                              % (_last["ok"], _last["status"], _last["errors"],
                                 _last["at"], _last["info"])).encode())

    def do_GET(self):
        self._respond(True)

    def do_HEAD(self):  # UptimeRobot pings with HEAD → must not 501
        self._respond(False)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    threading.Thread(target=_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), _Handler).serve_forever()
