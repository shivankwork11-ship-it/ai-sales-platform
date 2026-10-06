# Release 05 — VAMANA (recovery + full-funnel truth) — PLAN
*Written 2026-08-07 late night. Input: full sweep of all 5 session transcripts + all 17 docs
+ the dev team's own release notes (asbl-release-notes.html, 405 items, v0–v10).
Signature: CI Team — citeam.claudemax@gmail.com · Built with Claude (Anthropic).*

This file is the SANKALPA loop step 1 (plan) for the recovery release, and the
permanent record of the 2026-08-07 "what have we missed" audit. The user's open
complaints all live here until each is closed with a verified record.

---

## PART 1 — What the sweep found (the honest ledger)

### Delivered and verified (46 of 70 documented asks)
Foundation (read-only Mongo, PII regime, sync, audits) · Integrity engine (23 rules)
· Today cockpit + Call Room · Funnel board · AI-vs-Human cohorts · era naming ·
divergence corpus + prompt harness · persona mining → RADHIKA_VNEXT_PERSONA.md ·
prepared planner patches (3, sandbox-verified, NOT applied by design) · refresh-button
root-cause fix · identity solve (MASTERLEADID 71% / SourceLeadID 52% coverage) ·
RESCUE_SV lists (6 CSVs) · Review panel + MLID Profile + Eras tabs.

### The 12 open complaints (all NOT DONE — the defect board)
| ID | Complaint | Diagnosed cause (verified) |
|---|---|---|
| B1 | TOFU/MOFU/BOFU funnel | never built; stage vocab exists in Zoho + Intelligent_CRM |
| B2 | Per-lead modal like Inncircles (Web/Call/WA/Lead/Stage History + Scoring tabs) | MLID Profile is a flat page, not the tabbed modal; Web History + Scoring need July asks #67/#68 never carried in |
| B3 | Clicks go to the wrong place | 3 inconsistent click semantics (bars filter, cards navigate, ids open profile), no visual distinction |
| B4 | Transaction DB not visible | Intelligent_CRM.lead_events (28,669 rows) cached but never exposed as a surface |
| B5 | Date filters don't apply everywhere | Today/Eras/ops-daily ignore filters, unlabelled |
| B6 | Call-attempts graph wrong | LEADS-MERGE-FANOUT bug + 18% multi-project humans fan out joins |
| B7 | Charts missized/misaligned | SVG width:100% scales type with viewport |
| B8 | Data not true vs Zoho/Intelligent_CRM | (a) 568/3,214 humans (18%) multi-project + naive drop_duplicates picks arbitrary rows; (b) upstream contradictions (1002-BROADWAY row says project=LOFT); (c) Jun→Jul outcome-taxonomy break (DROPPED 94→5,117, VOICEMAIL 97→8,314) charted across the boundary with no rule; (d) 3 unfixed high-severity bugs: SYNC-STALE-WATERMARK, COHORT-BORNDATE-COERCE (silently drops 426 leads), LEADS-MERGE-FANOUT |
| B9 | Focus on regularly-updated fields | no field-volatility analysis exists; same principle as the watermark bug |
| B10 | Data-story flow / UX principles | never designed as a flow; per-release "data-story test" was self-asserted |
| B11 | ₹/outcome rate card | zero cost implementation; blocked on rate card never chased |
| B12 | Questions map | Review panel answers the sheet, but no question→answer→provenance map |

### Missed from OLD chats (pre-engagement, July sessions — never carried in)
| # | Ask | Note |
|---|---|---|
| 67 | Lead colour scoring model (green=qualified, purple=talked WA w/ campaign mgr, orange=high campaign reaction) | = the Scoring tab of the Inncircles modal |
| 68 | Pages visited + time spent per lead | fields ARE in Zoho_Database.leads cache (first/last_page_visited, time_spent_minutes, total_page_views) = the Web History tab |
| 69 | Behavioural profiles/cohorts per lead | MLID Profile is a ledger, not a profile model |
| 70 | Reactivation signal analysis (what made "not interested" leads reactivate) | reactivation_list synced, question never answered |

### Broken promises / stale records (from our own docs)
- LIVE LOG event feed (Kurma scope 6) — silently dropped.
- Source × time × topic cut (Kurma scope 4) — never appeared.
- Varaha-2 deferred list (journey page · pre-ANGAD baseline · KB-vs-objections gap · QA scoring) — no completion record.
- RELEASES/04-narasimha.md missing (Narasimha declared done with no release log — SANKALPA breach).
- WORKLOG.md stale (stops before UX-2/identity/rescue work) · schema_report.md MISSING though named as a deliverable · exec_report.html stale (pre-Varaha).
- Daily digest actions never tracked to closure; only 2 daily files exist.
- 19 of 24 verified toolchain bugs still unfixed (incl. the 3 data-severity ones above).
- Prepared patches: only 3 of the 6 promised follow-up additions have artifacts (WA hot-reply 15-min, fresh-lead ≤30min, visit-morning WA pin have none).
- Standing external asks untracked: leaked write-credential rotation · night-defer TRAI fix · dead WA sender pool · attendance write-back · per-call latency/cost persistence.
- Aug-14 post-cutover pipeline re-trace: pending, no owner.

### From the dev team's own release notes (data-semantics traps to encode)
1. **Reminder scheduler was QUEUED, never shipped** (only a manual API exists) — independently confirms our 89% finding; cache shows only 55 reminder + 16 confirmation rows ever vs 176 bookings.
2. **CRM webhook sends PRE_SITE without visit slots** (classifier skipped when hangup tool self-reports) → CRM re-calls booked leads; documented lead re-called 3 days straight.
3. **Outcome taxonomy changed 29 Jun** (+AUTO_CALLBACK, +DROPPED, historical NOT_CONNECTED→DROPPED backfill MUTATED old rows) and 7 Jul (VOICEMAIL 0-min via AMD). Pre-July outcomes not comparable.
4. **Slot provenance flips 16 Jul**: before = mid-call tool writes; after = post-call classifier. (v49 single-tool change.)
5. **Calling-hours guard is OFF in prod** (CALL_WINDOW unset since 17 Jul; "defers to CRM") — night dials are expected until the CRM gates them.
6. **DNC didn't block until ~20-23 Jul** (55 leads backfilled; one customer got 7 calls post-DNC).
7. Zoho Modified_Time is meaningless (hourly crons touch every lead).
8. ASBLVoiceBot is a best-effort mirror — expect gaps vs angad runtime.
9. Duration semantics: 45s minimum-wall pre-16-Jul inflates short declines; <8s floor after.
10. Prompt/model version is NOT persisted per call — cohort by date only.
11. Per-call latency/cost/AMD/carrier-cause are logged, never in DB — the latency tile can only ever be a proxy.
12. visitAt is not stored in ASBLVoiceBot (only site_visit_agreed boolean) — queued.
13. staging `ankit_male` slug found leaked in prod scheduledcalls (1 row).

---

## PART 2 — VAMANA scope (ordered; each item closes with a verified record)

**V1. Truth layer (kills B8/B6/B9)** — one canonical join module:
person-level identity (MLID collapse), PLID-level project truth (never drop_duplicates
on lead_ref), stage from Intelligent_CRM (runtime truth) reconciled with Zoho stage
(CRM truth) with disagreements SHOWN, era/taxonomy boundary rules (no chart crosses
29-Jun/7-Jul silently), fix the 3 data bugs (watermark → sync uses updatedAt where
available; cohort born-date coercion; merge fan-out), field-volatility report driving
which fields we trust (B9).

**V2. Lead modal like Inncircles (kills B2, absorbs #67/#68/#69)** — click ANY lead
anywhere → modal (not tab-jump) with tabs: Overview · Call History (ledger + intents
+ recordings) · WhatsApp History · Web History (pages/time from zleads) · Lead History
(lead_events feed = the transaction DB, B4) · Stage History (stage_change events both
DBs) · Scoring (colour model #67: green/purple/orange + components).

**V3. Funnel board TOFU/MOFU/BOFU (kills B1)** — three bands with per-project daily
stage table like Inncircles Growth Reports (New Lead / Lead Initiated / Pre SV /
Post SV / Sales Closure / Not Interested / Flat Blocked), counts clickable → lead
lists → modal. Sourced from truth layer only.

**V4. Interaction contract (kills B3/B5/B7/B10)** — ONE rule: click a lead = modal;
click a chart bar = filter (with visible chip animation); click a KPI = drill list.
Cursor + hover affordances distinguish them. Date filters apply to EVERY tab or the
tab carries a visible "fixed window" badge. Charts get max-width + fixed type size.
Tab order tells the story: Today → Funnel (TOFU/MOFU/BOFU) → Leads → Review → deep tabs.

**V5. Questions map (kills B12)** — table: notebook question → tab/panel that answers
it → source collection → freshness → confidence. Lives on the Review tab top.

**V6. Docs spine repair** — write RELEASES/04-narasimha.md retro log · regenerate
schema_report.md against growth-prod · bring WORKLOG.md current · regenerate
exec_report.html · digest-action tracker.

**V7. Chase list (external)** — rate card (B11) · fresh Zoho Leads CSV (A-list ids)
· credential rotation · Aug-14 re-trace · attendance write-back · scheduler
ship-status confirmation with dev team.

Loop: plan (this file) → panel review → V1..V5 develop with per-item audits →
data-story test on the REAL complaints above → release log 05-vamana.md.
