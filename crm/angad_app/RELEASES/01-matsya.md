# Release 01 — MATSYA
*The fish that saved the Vedas from the flood.* Everything built and concluded
up to 2026-08-07. Signature: CI Team — citeam.claudemax@gmail.com

## Shipped
Local read-only analytics stack over 4 Mongo DBs: sync → pseudonymised parquet
cache → 10-tab dashboard (Numbers, Integrity, Audit, AI vs Human, Positives,
Negatives, WhatsApp, Site visits, Intent, Call metrics) + exec_report.py +
probe/test/compare tooling + PII regime (one-way pseudonyms, no names, shred
protocol) + readonly write-guard + migration forensics.

## Conclusions that survived verification
1. Funnel (Jul17–Aug5): 3,351 lead-rows → 2,430 humans → 2,397 dialled →
   ~1,270 connected → 90 booked-on-call → 66 confirmed → 16 visits done.
   The funnel dies at the BOTTOM (confirmation 73%, attendance 24%).
2. AI books better per conversation than humans (5.5% vs 3.3%); humans convert
   bookings to visits better (32% vs 24%). AI = better opener, worse closer.
3. 87.8% of dials were invisible to the CRM (webhook mis-wire) → caused
   booked-lead redials (825) and post-DNC dials (308). Cutover to
   growth-relay is now live on BOTH clusters.
4. Night-defer gate leaked ~10.3k out-of-window dials. TRAI exposure.
5. WhatsApp sender pool entirely dead (19/19); 28,309 sends never delivered.
6. Speed-to-lead: ≤24h dials connect 53% vs 31% after 3 days; nothing dialled
   inside 30 min. Free conversion lever.
7. 55% of real conversations die at the greeting; qualification asked in only
   12%. The greeting is the product.
8. Transcripts unreliable ~11% (loops, voicemail-as-human, foreign-script
   hallucination) — every transcript metric ships with a noise budget.
9. Migration verdict: new cluster = faithful lagging copy; zero loss, zero
   edits, DNC intact (210=210). Production writes still on OLD as of
   2026-08-06 13:28; dialler idle since then.
10. 24 code bugs in our own tooling found by adversarial review — documented,
    NOT applied (user instruction). Workaround: always sync --full.

## Self-audit of Matsya
PII: 3 incidents caught and fixed by our own audit (ops-sheet fields, masker
corrupting ids). DB writes: zero ever; guard verified repeatedly. Biggest
process learning: verify with independent recomputation before presenting —
every number that reached the user survived a second derivation.
