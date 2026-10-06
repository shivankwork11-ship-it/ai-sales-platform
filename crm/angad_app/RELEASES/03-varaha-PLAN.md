# Release 03 — VARAHA (PLAN, approved 2026-08-07)
*The boar that lifted the earth from the waters* — lifting the buried data:
the Kaalbhojaditya era, the URL intelligence already in Zoho, latency without
developer help, and metrics that persist forever.
Signature: CI Team — citeam.claudemax@gmail.com

## Constraint accepted
No developer changes available. Every item below is a WORKAROUND from data we
already hold, PII-compliant by construction.

## Scope (Varaha-1)
1. **Era dimension — Kaalbhojaditya vs ANGAD.** Everything before 2026-07-17
   is the Kaalbhojaditya era; on and after is ANGAD. Named exactly so, as an
   era filter + side-by-side era comparison (same funnel, same metrics), in
   the same dashboard.
2. **URL / source intelligence** — from `Zoho_Database.leads` already in the
   cache (utm_source/medium/campaign, ad names, first/last page visited,
   referrer, lead_source, page-views/time-spent): which sources and which
   pages produce leads that CONNECT and BOOK, not just leads.
3. **Latency proxy (the workaround for the unpersisted metric)** — transcript
   turns carry timestamps: `first_agent_turn_ts − startedAt` ≈ time-to-first-
   agent-audio. Computed per call at sync. Honestly labelled a PROXY
   (transcript-timestamp resolution), but it finally lets us test the QA
   sheet's "10–15s latency kills calls" claim against 20k+ calls.
4. **Metrics history — keep ALL metrics stored.** Per-day, per-era headline
   metrics written to `data/metrics_history.parquet` on every sync (dials,
   connected, conversations, booked, DNC, out-of-window, webhook-fails,
   latency-proxy median). Append-forever record, independent of any cluster.

## Deferred to Varaha-2
Person journey page (lead_events timeline) · pre-ANGAD baseline from
Intelligent_CRM.calls · KB-vs-objections gap · QA scoring workflow.

## Next release (Narasimha) — queued, not started
The agent codebase at Bitbucket/0708 (verified present: agent + crm-api, 131
TS files): read-only quality audit first (media-stream, gemini client,
night-defer gate, transcription config), then improvement plan. The 2-lakh
human-call transcriptions become the framework corpus — NEED: file location.

## Panel minutes
- CTO: latency proxy acceptable ONLY with explicit proxy labelling + a
  validation cut (proxy vs drop rate) before anyone quotes it. ACCEPTED.
- DS: era comparison must hold provider mix constant where quoted (Telnyx
  share differs across eras) — show provider split alongside. ACCEPTED.
- CEO: era comparison is the "did ANGAD improve things" answer — lead with
  bookings per 1,000 dials. ACCEPTED.
- PM: four items, one increment, journey page stays Varaha-2. ACCEPTED.
- Growth: source intel must separate lead VOLUME from lead QUALITY. ACCEPTED.
- CI: store metrics per era so Kaalbhojaditya history survives every future
  migration. ACCEPTED.
**Verdict: APPROVED.**
