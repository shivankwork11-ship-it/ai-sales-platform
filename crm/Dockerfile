# ASBL Lead Dashboard + "Main Dashboard" (ANGAD) — single ECS container.
#
# Runs TWO processes (see entrypoint.sh):
#   1. the lead dashboard Flask app via gunicorn  (0.0.0.0:$PORT, public)
#   2. the UNMODIFIED ANGAD server                (127.0.0.1:$DASH_PORT, loopback)
# The Flask app reverse-proxies /main-dashboard -> ANGAD (see main_dashboard.py).
#
# ANGAD's ~300MB Parquet cache is NOT baked into the image; it is generated at
# runtime by angad_app/sync.py from Mongo (mount an EFS volume at
# /app/angad_app/data to persist it across restarts — see MAIN_DASHBOARD_ECS.md).
FROM python:3.12-slim

# curl: entrypoint uses it to trigger ANGAD's own /api/refresh.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first (better layer caching). Root deps = lead app; angad_app deps
# = pandas/pyarrow for the Main Dashboard; gunicorn = the production WSGI server.
COPY requirements.txt ./
COPY angad_app/requirements.txt ./angad_app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r angad_app/requirements.txt \
    && pip install --no-cache-dir gunicorn==26.0.0

# Application code (data/ is excluded via .dockerignore; regenerated at runtime).
COPY . .

# Run as non-root; ensure ANGAD's runtime dirs exist and are writable.
RUN useradd -m app \
    && mkdir -p angad_app/data angad_app/RELEASES \
    && chown -R app:app /app
USER app

ENV PORT=8080 \
    DASH_PORT=8777 \
    ANGAD_INTERNAL_URL=http://127.0.0.1:8777 \
    ANGAD_TOKEN_FILE=/tmp/angad_token \
    MONGO_DB=angad \
    WEB_TIMEOUT=600

EXPOSE 8080
ENTRYPOINT ["./entrypoint.sh"]
