# Release 03 — VARAHA (SHIPPED 2026-08-07)
Signature: CI Team — citeam.claudemax@gmail.com

## Shipped
1. **Era dimension** — Kaalbhojaditya (< 2026-07-17) named and filterable;
   era comparison on Numbers. Finding: Kaalbhojaditya 7.9 booked/1k dials at
   56 dials/day vs ANGAD 2.31/1k at 12x volume; night-leak is ANGAD-era only.
2. **URL/source intelligence** from Zoho_Database.leads already in cache:
   connect%/book% by source, UTM, campaign, first page, referrer.
3. **Latency proxy** (first-agent-turn − call-start; 15,277 calls carry it):
   median 6.5s, p90 11.4s. **Finding that reverses the anecdote**: among calls
   where the agent spoke, slow starts (≥8s, 25.3%) drop at 35.9% vs 38.0% for
   fast — no penalty. CAVEAT (on-screen): calls where the agent never spoke
   are excluded by construction — catastrophic latency remains invisible;
   the QA sheet's dead-air anecdotes likely live in that blind spot.
4. **Metrics history** — data/metrics_history.parquet: 85 day-era rows,
   rebuilt each sync, survives any future migration.

## Gates
PII CLEAN (53 files) · guard 19/19 · payload verified · era totals reconcile
(3,545 + 42,409 = 45,954) · proxy fill 15,277 · history 63+22 rows.
Data-story test: the era table and the latency finding each state their own
conclusion — and the latency one honestly states its blind spot. PASS.

## Next
Varaha-2 (journey page, pre-ANGAD baseline, KB-vs-objections, QA scoring) ·
Narasimha (agent codebase audit at Bitbucket/0708 — transcription quality
fix lives there) · awaiting: 2-lakh human-transcription corpus location.
