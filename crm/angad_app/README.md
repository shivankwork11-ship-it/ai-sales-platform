# ANGAD funnel dashboard — local only

A dashboard that runs on your laptop, reads the ASBL MongoDB cluster, and answers
the questions from your notebook page. Nothing is exposed to the network.

## Run it

```bash
python dashboard.py
```

Opens in your browser. That's the whole thing. Filters are at the top; the
**Fetch fresh data from Mongo** button re-pulls the cluster when you want newer data.

If the cache is ever wiped, rebuild it first with `python sync.py --full` (~15 min).

## Security — what "no incidents" means here, concretely

| Risk | What stops it |
|---|---|
| Someone on your wifi / the internet reaching it | Binds `127.0.0.1` only, never `0.0.0.0`. Verified with `netstat`: `TCP 127.0.0.1:8777 LISTENING`. |
| Another website in another tab silently querying `localhost` | A random token is minted per run and required on every request. No token → `403`. Verified. |
| Cross-site request forgery | Any request carrying a foreign `Origin`/`Referer`, a non-loopback `Host`, or `Sec-Fetch-Site: cross-site` is refused before reaching a handler. No CORS headers are ever sent. Verified. |
| Anything writing to production | **Enforced, not promised.** `readonly.py` patches pymongo itself so every write method raises `WriteBlocked` — inserts, updates, deletes, bulk writes, `find_one_and_*`, index and collection changes, `drop_database`, aggregate pipelines containing `$out`/`$merge`, and any non-read `db.command()`. It is imported before any client is created in `sync.py`, `probe.py` and `test_connection.py`, so it applies process-wide. Verify with `python readonly.py` — 19/19 blocked, reads unaffected. |
| Your Mongo password leaking into the page | The URI lives only in `.env`, read only by the Python process. It is not in the HTML, the JS, or any API response. |
| Phone numbers / PII reaching the browser | `sync.py` pseudonymises every number to `LEAD-xxxxxxxx` *before* it touches the cache, redacts phone-shaped text inside transcripts, drops emails and names-as-keys, and never pulls `apikeys` / `dashboard_users` / any `secret` field. |
| Cache silently gaining PII later | `python audit_pii.py` scans every cached file. Last run: **44 files, 722,400 values, clean.** |
| Supply-chain / phone-home | No CDN, no external fetch, no telemetry. The page works with the network off. |
| Method abuse | GET only. POST/PUT/DELETE return `405`. Strict CSP, `nosniff`, `no-referrer`, `no-store`. |

Run this after every sync:

```bash
python audit_pii.py
```

And to re-prove the database can never be written to:

```bash
python readonly.py
```

**One thing outside this tool's control:** your data dictionary says the
`asbl-growth-write` credential has leaked into chat, and it holds `atlasAdmin`
over all 18 databases. Ask your devs to rotate it and issue you a **read-only**
user scoped to the four databases you report on. This dashboard never writes,
but the credential itself is over-privileged.

## Where the data comes from

`angad` is only the dialer runtime — its `extractedSlots`, `buyingSignals` and
WhatsApp fields are genuinely empty. The answers live across four databases,
joined on phone (normalised to the last 10 digits, then pseudonymised):

| Your question | Source |
|---|---|
| Daily call trigger count, outcomes, drops | `angad.calls` (44,680) |
| Budget, timeline, intent, objections, language | `ASBLVoiceBot.call_transcripts` (21,150) |
| Project, campaign, UTM, attribution | `Zoho_Database.leads` (4,041) |
| Who was texted, what was sent | `Zoho_Database.whatsapp_messages` (1,326), `doc_send_log` |
| CRM stage funnel | `Intelligent_CRM.crm_leads` (3,912) via `persons` |
| Site visits | `Intelligent_CRM.site_visits` (55) |

## The tabs

- **Numbers** — daily trigger count by outcome, dial funnel, outcome mix, CRM stage, attempts per lead
- **Audit** — identity pyramid (dials → numbers → humans → project-leads), calls-per-human, connect rate by attempt number, daily connect trend, **Zoho ↔ dialler reconciliation** (explains 3,351 export rows vs 2,941 dialled numbers exactly), Zoho-status-vs-dialler crosstab, CRM sync gaps
- **Positives** — who was called with full profile, dial-hour vs connect rate, objections / budgets / timelines, projects / campaigns / UTM, searchable transcripts
- **Negatives** — who was missed, who dropped and *where in the call*, recontact chain, rejections & DNC
- **WhatsApp** — messages in/out, reply rate, what documents went to whom, what those people said on the call
- **Site visits** — every booking (55), by project/type/day, current Zoho status of booked leads, and the attendance gap (zero visit_done events exist anywhere)
- **Intent** — the LLM post-call analysis (`call_transcript_intent_identification`, auto-synced): conversation stage funnel, caller intent, sentiment, engagement, talk balance, urgency, objections, BHK/budget, agent scorecard, hot-lead list
- **Call metrics** — duration, turns, talk share, first-reply time, tokens, and **transcript-quality flags** (hallucinated scripts, voicemail-as-customer, bot loops…) with a matching filter

**Daily Zoho export**: drop `Leads_*.csv` into `Downloads/Daily angad performance/` — every sync picks the newest one up automatically (or run `python import_zoho.py`). Phone numbers are pseudonymised on import; names/emails dropped.

Every table has a row filter and a **Download CSV** button.

## What the data still can't tell you

Called out inline in the dashboard, not hidden here:

- **"Technical or call failure?"** — the carrier hangup cause and the AMD
  human/machine result are logged by the agent but never written to Mongo. The
  drop *stage* shown is inferred from transcript shape and duration. Persisting
  those two fields is the highest-value schema change.
- **Per-call latency** (the 6–7s signal) and **cost in ₹** are console-log /
  runtime-computed only. Tokens stand in for cost.
- **`customerName = "Sir"`** is a Zoho placeholder; treated as "no name".

## Files

| File | Role |
|---|---|
| `dashboard.py` | the dashboard + hardened local server |
| `page.html` | the UI (charts, filters, tables) |
| `sync.py` | Mongo → local parquet cache, all four databases |
| `readonly.py` | hard read-only guard — makes every Mongo write raise |
| `pii.py` | phone pseudonymisation and text redaction |
| `audit_pii.py` | post-sync security check |
| `probe.py` | full-collection schema/fill-rate profiler → `schema_report.md` |
| `test_connection.py` | connection diagnostics |
| `set_uri.py` | writes `.env` from the Compass clipboard string |

## Command reference

```bash
python sync.py --full                      # rebuild the whole cache
```
```bash
python sync.py                             # incremental, new calls only
```
```bash
python sync.py --only call_transcripts      # re-pull one collection
```
```bash
python sync.py --full --since 2026-06-01    # date-limited pull
```
```bash
python sync.py --full --with-pii            # also write data/phone_map.csv (local lookup, gitignored)
```
