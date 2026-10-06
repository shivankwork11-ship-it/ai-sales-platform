# Main Dashboard (ANGAD) integration + ECS deployment

## What this is
The **ANGAD funnel dashboard** is now a **"Main Dashboard" tab** in the lead dashboard.
It is reused **exactly as the dev shipped it** — no ANGAD logic, markup, or numbers
were changed. It runs as its own process on an internal loopback port; the Flask
app reverse-proxies it in behind the existing login.

### Files added (all additive — no existing logic touched)
| File | Purpose |
|------|---------|
| `angad_app/` | The ANGAD dashboard, unmodified (its `data/` is git-ignored) |
| `main_dashboard.py` | Flask blueprint that reverse-proxies `/main-dashboard` → ANGAD |
| `Dockerfile`, `entrypoint.sh`, `.dockerignore` | ECS container (runs both apps) |
| `angad_app/requirements.txt` | ANGAD's pandas/pyarrow deps (kept out of root reqs) |

### Existing files touched (additive only)
- `dashboard.py` — one `try/except` block registering the blueprint + a nav `<a>` link in each nav bar.
- `pipeline.py` — the same nav `<a>` link.
- `.gitignore` — ignore `angad_app/data/`, `*.parquet`, `angad_app/.env`.

### How it works
1. `entrypoint.sh` starts `angad_app/dashboard.py` on `127.0.0.1:8777` and captures
   its per-run token from stdout into `/tmp/angad_token`.
2. `main_dashboard.py` forwards `/main-dashboard*` to ANGAD, injecting the token and
   rewriting the served page's `/api/...` fetch paths to `/main-dashboard/api/...`.
3. `/main-dashboard` is **login-protected** automatically (not in `_PUBLIC_ROUTES`).

## Why ECS (not Vercel)
ANGAD needs pandas/pyarrow (~150MB) + a ~300MB Parquet cache + a background ETL —
none of which fit Vercel's 250MB / 300s serverless limits. A long-lived container
runs it comfortably, and also fixes the lead dashboard's own cron timeout (gunicorn
`--timeout 600` lets `/api/cron/refresh` finish; run `compute_lead_profiles.py` as a
scheduled ECS task or in-container cron).

> The root `requirements.txt` is deliberately **unchanged** so any remaining Vercel
> build of the lead app is not bloated/broken by the heavy deps.

## Required environment variables (ECS task definition)
| Var | Value / note |
|-----|--------------|
| `MONGO_URI` | Same cluster string used by the lead app. Must have **read on all 4 DBs**: `angad`, `ASBLVoiceBot`, `Zoho_Database`, `Intelligent_CRM` (ANGAD's `sync.py` reads them). |
| `MONGO_DB` | `angad` (already defaulted in the Dockerfile) |
| `MONGO_URI_QA_RESULTS` | as today (QA tabs) |
| `SECRET_KEY` | optional; else auto-loaded from `analytics_db.app_config` |
| `ANGAD_REFRESH_HOURS` | ANGAD cache refresh interval (default `6`) |
| `WEB_TIMEOUT` | gunicorn worker timeout (default `600`) |
| `PORT` | `8080` (container listen port) |

## Build & push to ECR
```bash
aws ecr get-login-password --region <region> | docker login --username AWS --password-stdin <acct>.dkr.ecr.<region>.amazonaws.com
docker build -t asbl-dashboard .
docker tag asbl-dashboard:latest <acct>.dkr.ecr.<region>.amazonaws.com/asbl-dashboard:latest
docker push <acct>.dkr.ecr.<region>.amazonaws.com/asbl-dashboard:latest
```

## ECS service notes
- **Container port:** `8080`. Put it behind an ALB; target-group health check → `GET /login` (public, returns 200).
- **Persist ANGAD data:** mount an **EFS** volume at `/app/angad_app/data`. Without it, every new task re-runs `sync.py --full` (a few minutes) on first boot. The Parquet cache is NOT in the image.
- **Sizing:** ANGAD holds the Parquet frames in memory (pandas). Start with **~1–2 vCPU / 2–4 GB**; the 248MB `human_calls.parquet` drives memory — raise if OOM.
- **Mongo reachability:** the task's security group/subnet must reach the Mongo host `13.127.144.117:27017`.
- **First boot:** the web app comes up immediately; the Main Dashboard tab shows "no cache" until the first `sync.py --full` finishes (runs in the background).

## Run locally (smoke test)
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r angad_app/requirements.txt gunicorn
export MONGO_URI='...'; export MONGO_DB=angad
export SSL_CERT_FILE=$(python -c 'import certifi; print(certifi.where())')
./entrypoint.sh          # starts ANGAD + gunicorn; open http://localhost:8080
```
(For local ANGAD data without waiting on sync, copy an existing `angad_app/data/` in.)

## Known limitations / follow-ups
- The Main Dashboard tab shows ANGAD's own full-screen UI; use the browser Back button or another tab to return (ANGAD's page has no shared top-nav). A small "← Back to Leads" bar can be injected in the proxy later if desired.
- ANGAD's data is a **cached snapshot** refreshed every `ANGAD_REFRESH_HOURS`, not live per-request (by design — that's how ANGAD was built).
