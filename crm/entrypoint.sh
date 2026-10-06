#!/usr/bin/env sh
# Container entrypoint: start the UNMODIFIED ANGAD server on loopback, capture its
# per-run token for the Flask proxy, bootstrap/refresh its Parquet cache in the
# background, then run the lead dashboard via gunicorn in the foreground.
set -e

# TLS trust store (some slim images need certifi explicitly; see dev setup doc).
export SSL_CERT_FILE="${SSL_CERT_FILE:-$(python -c 'import certifi; print(certifi.where())')}"

DASH_PORT="${DASH_PORT:-8777}"
TOKEN_FILE="${ANGAD_TOKEN_FILE:-/tmp/angad_token}"

# ── 1. Start ANGAD (unmodified) on 127.0.0.1:$DASH_PORT ───────────────────────
echo "[entrypoint] starting ANGAD on loopback :$DASH_PORT"
python -u angad_app/dashboard.py > /tmp/angad.log 2>&1 &

# ── 2. Capture ANGAD's per-run token from its stdout (no code change) ─────────
TOKEN=""
i=0
while [ "$i" -lt 60 ]; do
  TOKEN=$(grep -oE '\?t=[A-Za-z0-9_-]+' /tmp/angad.log 2>/dev/null | head -1 | sed 's/?t=//')
  [ -n "$TOKEN" ] && break
  i=$((i + 1)); sleep 1
done
if [ -n "$TOKEN" ]; then
  printf '%s' "$TOKEN" > "$TOKEN_FILE"
  echo "[entrypoint] ANGAD token captured -> $TOKEN_FILE"
else
  echo "[entrypoint] WARNING: ANGAD token not captured; Main Dashboard tab may 403." >&2
fi

# ── 3. Data bootstrap + periodic refresh (background, so web starts fast) ─────
(
  # First run: if the Parquet cache is empty (no EFS data), pull it from Mongo.
  if [ -z "$(ls angad_app/data/*.parquet 2>/dev/null)" ]; then
    echo "[entrypoint] ANGAD cache empty — running sync.py --full (first run)"
    ( cd angad_app && python sync.py --full ) \
      || echo "[entrypoint] sync.py --full failed; tab shows 'no cache' until it succeeds." >&2
    [ -n "$TOKEN" ] && curl -s "http://127.0.0.1:${DASH_PORT}/api/refresh?full=1&t=${TOKEN}" >/dev/null 2>&1 || true
  fi
  # Ongoing refresh via ANGAD's OWN endpoint (runs sync + clears its cache).
  INTERVAL_H="${ANGAD_REFRESH_HOURS:-6}"
  while true; do
    sleep "$((INTERVAL_H * 3600))"
    [ -n "$TOKEN" ] && curl -s "http://127.0.0.1:${DASH_PORT}/api/refresh?full=1&t=${TOKEN}" >/dev/null 2>&1 || true
  done
) &

# ── 4. Foreground: lead dashboard via gunicorn ────────────────────────────────
# Long --timeout so the app's own /api/cron/refresh (the lead-profile freshness
# job) can run without being killed — the ECS equivalent of Vercel maxDuration.
echo "[entrypoint] starting lead dashboard (gunicorn :${PORT:-8080})"
exec gunicorn --bind "0.0.0.0:${PORT:-8080}" \
     --workers "${WEB_CONCURRENCY:-2}" \
     --threads "${WEB_THREADS:-4}" \
     --timeout "${WEB_TIMEOUT:-600}" \
     dashboard:app
