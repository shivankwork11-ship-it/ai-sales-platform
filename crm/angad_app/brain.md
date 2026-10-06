# BRAIN — ANGAD Analytics engagement (full context)
*Read this first in any new session. Signature: CI Team — citeam.claudemax@gmail.com · Built with Claude (Anthropic). Last updated 2026-08-07 ~16:30 IST.*

## What this is
A local, read-only analytics + audit + improvement engine for **ANGAD**, ASBL
real estate's AI voice sales agent (persona "Radhika", Gemini Live + Plivo/
Telnyx, Zoho CRM upstream). Owner: the user (CI Team). **The only goal:
site-visit bookings ATTENDED.** Everything else is instrumentation.

## THE IMPRINTED RULES (non-negotiable, every turn — also in auto-memory)
1. **NEVER write to any MongoDB.** `import readonly` before pymongo everywhere
   (patches all 19 write ops to raise). Verified against 3 clusters.
2. **PII one-way forever**: phones → `LEAD-xxxxxxxx` (salted, last-10). The
   reverse map + salt were SHREDDED at user instruction; `pii.write_map`
   raises. Personal names never cached (blocked at ingest, audit-enforced).
   Emails dropped. Free text through `pii.redact` (10–14 digit band ONLY —
   wider corrupts 19-digit Zoho ids; this happened once).
3. **`python audit_pii.py` after EVERY sync/import** — any finding stops the line.
4. No raw phones/names/emails in chat or tool output. Ephemeral digests for
   identity comparisons.
5. **Credentials never transit through Claude** — user runs `set_uri.py` /
   `test_connection.py` themselves; Claude reads only redacted pastes.
6. **SANKALPA protocol** (PROTOCOL_SANKALPA.md): loop = plan → 6-role panel
   (CEO/CTO/PM/DS/Growth/CI) → develop → 4 audits (front-end, back-end, PII,
   guard) → data-story test → release log → **visible SELF-AUDIT then the
   imprinted rules displayed, every work turn**. Releases named on the
   Dashavatara ladder in RELEASES/. Loop engineering applies to ALL processes;
   protocols are goals.
7. Everything created stays on the local system.
8. Era naming: data before 2026-07-17 = **Kaalbhojaditya**; on/after = ANGAD.
   Label/filter only — user removed the old-vs-new comparison table.
9. Repo patches are PREPARED in the user's local copy only, never deployed.

## Environment
- Project dir: `C:\Users\Tiwari\Documents\Claude code\voice agent stage wise analysis\angad_dashboard\`
- Production Mongo: **growth-prod.rugkur.mongodb.net** (user `grow_team_read`,
  readAnyDatabase). `.env` holds MONGO_URI (+ MONGO_URI_OLD = original
  angad.3z3qbu cluster, historical). DBs: angad (dialler runtime),
  ASBLVoiceBot (transcripts/intelligence), Zoho_Database (legacy CRM brain),
  Intelligent_CRM (event-sourced rebuild).
- Agent codebase (Bitbucket pull): `C:\Users\Tiwari\Documents\USage\Angad\BItbucket\0708\`
  (agent + crm-api repos, 131 TS files).
- Human-call corpus: `data/human_calls.parquet` (296,662 transcriptions,
  Speaker 0=agent/1=customer, stage-labelled, from the 185MB xlsx in the
  parent folder).
- Clean AI-call corpus: `data/retranscripts.parquet` (3,862 rows: 9 dual
  bot-vs-audio, 331 site-visit re-transcripts, 3,522 new-prompt verification;
  paired 3,853 to stored calls via Source-Lead-ID→Zoho bridge — the CSVs'
  phone column is Excel-float-mangled, never use it).
- Old-cluster archive: `data_OLD_CLUSTER_2026-08-06/` (pre-migration truth,
  names scrubbed, refs permanently unlinkable — old salt destroyed).

## How to run
- Dashboard: `python dashboard.py` (127.0.0.1, per-run token, 13 tabs).
- Refresh data: dashboard "Fetch fresh data" button or `python sync.py --full`
  (ALWAYS --full — incremental has a documented unfixed watermark bug).
  Then `python audit_pii.py` (must be CLEAN).
- Exec one-pager: `python exec_report.py`. Guard proof: `python readonly.py`.
- Imports auto-run in sync: `import_zoho.py` (Downloads/Daily angad
  performance/Leads_*.csv), `import_cohorts.py` (Humans & AI workbook + SV
  files), `import_retranscripts.py` (the 3 clean-transcript CSVs).

## Dashboard tabs (13)
Today (cockpit: 9 click-through cards, daily digest auto-saved to
RELEASES/daily/, Call Room = audio + transcript one place) · Funnel board
(7 stages/lead incl. booked-then-cold rescue queue) · Numbers (daily, funnel,
mixes) · Integrity (**23 live rules**, drill-downs) · Audit (identity pyramid,
attempt curve, heatmap, speed-to-lead, pipeline-loss, Zoho recon) · AI vs
Human · Positives (+ sources/URL intel) · Negatives (+ drop last-words) ·
WhatsApp (+ hot-reply queue) · Site visits · Intent · Call metrics (+ latency
proxy). 22 multi-select filters; unit toggle; MLID+PLID shown beside every
pseudonym; charts click-to-filter on filterable dims.

## DEV-TEAM ANSWERS 2026-08-08 (DEV_TEAM_ANSWERS_2026-08-08.md — READ IT)
Their audit + OUR verification against the cache:
- **PERSONA SWAPPED IN PROD**: Mongo prompt = male 'ankit-v3.8-male-v6.0.0'
  pushed Aug 7 08:47 IST, overrides deployed Radhika code (in_sync:false);
  voice default now male 'Achird'. 'ankit-prod-' test esids in prod Aug 7.
  NEVER compare across Aug 5-7 without splitting.
- **J1: 52.7% of calls (24,379) have roomName='pending-…'** (unanswered Plivo)
  — call_sid joins drop exactly the failures; join on externalScheduleId.
- **Night window REFRAMED**: guard IS on (5,070 defer rows). Of 8,667
  out-of-window dials, 94.5% are Telnyx/NRI (IST night = customer's day, NOT
  TRAI); real +91 exposure = 450 dials + the /api/calls/initiate bypass.
- **Zombie confirmations = HUMAN**: all 20 post-Jul-17 confirmation calls have
  esid 'dash_s' = ops-dashboard dispatch UI. Slug dead in planner, still dialable.
- **TRANSCRIBE_POSTCALL OFF** (zero accurateTranscript in cache) — batch
  re-transcription shipped but disabled; every outcome/slot extracted from a
  <50%-accurate transcript. #1 cheap dev ask. Classifier prompt lacks
  relative-date resolution for visit dates (has it for callbacks) = #2.
- **Attendance is UN-ARCHITECTED** (no field/endpoint/path) — ops sheets only.
- Outcome traps encoded in Insights: NO_ANSWER partly fabricated by
  cleanupStaleCalls; VOICEMAIL dual-origin; 'hinglish' inflated default;
  'classifier offline' summary = failure not fact; propertyInterested='ASBL
  Loft' is the unspecified default; call_schedules.status 'called' ≠ answered;
  visit gate REJECTS sub-4-min bookings (invisible loss); tokenUsage → real ₹
  (cost: **₹0.5/dial · ₹1.33/answered call · ₹246/booked · ₹1,504/attended**,
  rate card calibrated to observed billing — one source of truth, `cost_of()`);
  webhookDeliveries[]+callbackUrl exist on Call docs but are NOT in our sync
  columns yet (add to sync when touching it next); metadata.source_lead_id
  likewise. Recordings: only answered calls; old Telnyx URLs may be dead links.

## Verified findings (each recomputed before presenting; sources labelled)
- Funnel (Jul17–Aug5): 3,351 lead-rows → 2,430 humans → 2,397 dialled → ~1,270
  connected → 90 booked-on-call → 66 confirmed → **16 visits attended**.
  Bottom-of-funnel is the leak: confirmation 73%, attendance 24%.
- **FU discipline (biggest actionable): 157 of 176 bookings (89%) never got a
  reminder scheduled** (planner needs parsed visit time; usually absent).
  Plus 5 zombie confirmation-slug calls after its Jul-17 removal.
- AI vs Human: AI books 5.5%/conversation vs human 3.3%; humans convert
  booked→done 32% vs AI 24%. AI = better opener, worse closer.
- Night-defer leak: ~10.3k out-of-window dials historically; reproduced on
  growth-prod day 1 (9/41). TRAI exposure. Fix lives in agent repo.
- WhatsApp sender pool 19/19 dead; 28,309 sends never delivered → all "sent"
  metrics overstate. Hot-reply queue exists (75 said "call me").
- Speed-to-lead: ≤24h dials connect 53% vs 31% at 3+ days; nothing dialled
  <30 min after birth.
- Latency PROXY (first-agent-turn − start; 15,277 calls): median 6.5s, p90
  11.4s; slow (≥8s) does NOT drop more (35.9% vs 38.0%) — BUT calls where the
  agent never spoke are invisible (survivorship; catastrophic latency hides there).
- **Stored transcripts are unreliable**: Gemini on-call memory garbles CUSTOMER
  turns; bot mostly acts correctly (user-confirmed). Divergence vs audio truth:
  median 0.79, 85.8% >0.5 (239 scored pairs). Conversation analytics must use
  `retranscripts`; ENGAGEMENT STATE is a DESIGNED internal prompt function
  (never spoken) — stripped at ingest.
- Prompt harness (913 agent-speech calls): visit-gate 99.9% PASS, zero
  internal-text spoken, qualification-before-pitch 90.9%; weak: inaudible-turn
  recovery (agent sometimes invents understanding), closing ritual (day
  confirmed; time/attendees often not).
- Follow-up logic in repo (sitevisit/planner.ts): reminders 12h+1h before
  visit (15m if imminent); +30m confirmation removed Jul-17.
- Migrations: original → growth-test (lagging copy) → growth-prod (CLEAN:
  zero loss/edits, DNC 210 intact, tail swept, webhook→growth-relay.asbl.in,
  indexes added). All forensics in MIGRATION_AUDIT.md.
- Kaalbhojaditya era: 3,545 dials/63d, 7.9 booked/1k; ANGAD: 42,409/22d,
  2.31/1k (12× volume). Night-leak is ANGAD-only.

## ⚑⚑⚑ RELEASE 08 KRISHNA 2026-08-08 — READ RELEASES/08-krishna.md FIRST
Supersedes anything below that conflicts. Post-`--full`-resync, audit CLEAN.
1. **THE COST MODEL WAS WRONG.** Old card ($3/$12 per 1M) was Pro-tier text list
   pricing we ASSUMED and mislabelled as the dev team's. It overstated model
   spend **5.9x**. New card $0.50/$2.50 per 1M + ₹0.60/started min (answered
   only) reproduces the dev team's OBSERVED ₹1.60-1.70/answered call to within
   2%. **₹0.5/dial · ₹1.33/answered · ₹246/booked · ₹1,504/attended.**
   All ₹ now flows through ONE function, `cost_of()`. Never re-implement it.
2. **Verified against the dev team's WhatsApp claims: they were right, we were
   wrong.** No cost before pickup (BUSY 0.0%, NOT_CONNECTED 0.0%, NO_ANSWER 0.2%
   open a session; 83.5% of dials cost zero). And corr(duration,tokens)=**0.12**
   with **99.4% of tokens being the fixed ~35k prompt** — a call dropped after
   one second costs almost what a full conversation costs.
3. **`reached` IS NOT "the line was answered".** It is False on ALL DROPPED and
   ALL VOICEMAIL — yet 5,428 of those opened a Gemini session. Three-tier truth:
   line opened **39.7%** · **a human picked up 20.9%** · conversation counted
   (what we report as "Connected") **7.4%**. We are NOT failing to reach people;
   two-thirds of the humans who answer hang up inside 19 seconds.
   `duration_sec` is NOT proof of pickup (44% of NO_ANSWER carry ~30s ring time).
4. **89% gap: BOTH prior explanations are retracted.** Not the wrong-DB theory
   (our field-name bug) and not the no-parsed-time theory either: the visit date
   IS parsed on **63.2%** of bookings, 53 leads (42.4%) have it and still get no
   follow-up, and 10 get a follow-up without it. `metadata.reason` across all
   48,097 schedules contains ONLY `fast_retry_*`/`daily_retry_*` — no reminder
   reason exists anywhere. The failure is DOWNSTREAM of parsing.
5. **Stuck stage fires the wrong dependency (confirmed).** 1,029 dials went out
   AFTER the booking call to 110 of 125 booked leads, **max 229 to one lead**.
   The retry cadence keys off stage; stage never advances; cadence treats a
   booked lead as un-contacted.
6. **Stage disagreement is one-way staleness, not noise.** 706 leads Zoho still
   calls "New Lead" after ANGAD dialled them a median 14 times / connected 361.
   The stage write-back into Zoho is not happening. Zoho `stage` null on 1,828
   of 4,212 rows. Daily Report carries a stage-system toggle because of this.
7. Shipped: **Daily Report** section (per-cell drill, parity verified live) and
   a **warmer theme** (body rgb(247,242,234)).
8. Open, NOT done: RAMA-7/8/9, **CIF enhancement (all 4 items user-approved)**,
   UI sizing sweep.

## ⚑⚑ CURRENT STATE 2026-08-08 (Release 07 RAMA) — READ HANDOFF.md FOR THE BRIEF
Dashboard is a warm-white CRM layout: left sidebar · toolbar with **Action date
AND Born date** ranges + 7 presets · filter modal · NRI/RI selector · unit
toggle. Universal drill (`/api/drill`, `/api/slice`) — every headline number and
chart bar opens the rows behind it with parity shown. Lead modal = 7 tabs incl.
turn-by-turn Conversation. CIF rebuilt from the team's REAL scoring dashboard
(5 params / 30 indicators / verbatim definitions / 16 computing). Funnel has a
per-project section. Comparison baselines include Kaalbhojaditya.

**FOUR ROUNDS of dev answers archived**: DEV_TEAM_ANSWERS_2026-08-08.md,
_ROUND2/3/4.md. Our open asks: DEV_ASKS_ROUND3.md. Probe log: DB_PROBE_FINDINGS.md.
Learnings: LEARNINGS.md. Protocols: PROTOCOLS_INDEX.md. Team: SHARE_WITH_TEAM.md.

**IDENTITY SOLVED**: `externalScheduleId` = `prd-{zoho_lead_id}-{epoch_ms}` on
81.4% of calls → strip prefix AND trailing timestamp → **100% join to
Zoho_Database.leads.zoho_lead_id**. Esids are unique = a DIAL key. Prefixes:
prd-=relay cadence, dash*=human, sched-=agent poller.

**TOP FINDINGS (each MEASURED, recomputed before being written here)**:
(1) **The site-visit reminder cron has never fired.** `pre-site-visit-reminders`:
987 runs, 35,291 records scanned, **0** fired_24h, **0** fired_3h, **0** errors.
It runs hourly, matches nothing, and reports success. Candidates exist (41 parsed
visit dates, 6 still future). THIS is the 89% gap;
(2) **A human picks up on 20.9% of dials**, not the 7.4% we report — `reached`
excludes DROPPED and VOICEMAIL, 5,428 of which opened a Gemini session. Two-thirds
of the humans who answer hang up inside 19 seconds;
(3) **60.4% of calls die between turn 1 and turn 2** (agent opens with 22.9 words,
customer gives 4.0, 56% give nothing). The loss is one exchange wide — the opening
line;
(4) over-dialling is a DATED REGRESSION — follow-up v3 (18 Jun) removed the attempt
cap; 62.6% of all calls go to 20+-attempt leads at 3.9% connect; max 234 dials to
one lead;
(5) **Zoho `stage` is not a maintained field** — the only jobs that write it ran 3
times ever (20–21 Jul), unscheduled; null on 95–100% of everything born since
29 Jul;
(6) persona switched **7 Aug**, dated from our transcripts — and a THIRD name
(Anandita) exists, so post-7-Aug is not one agent;
(7) attendance is un-architected anywhere — the goal still has no sensor.

**prd-cadence outage, reconciled (was two conflicting readings, both wrong — R8):**
one continuous outage of **6d10h49m** (25 Jul ~19:46 → 1 Aug 06:35), then six days
of degraded once-daily running, then a second gap of 1d10h53m ending 7 Aug 17:28,
then recovery. **Dial volume did NOT collapse during it** (756/703/672/671/964/
720/772 on Aug 2–8) — do not repeat that causal chain.

**Retracted claims live in RETRACTIONS.md and nowhere else. Do not re-annotate
them here.**

**WE CORRECTED THE DEV TEAM** on: NIGHT_DEFER being on (our 5,070 defer rows),
night dials being 94.5% NRI not a compliance hole, in_sync being meaningless,
and their prd- extraction query missing the timestamp suffix.

## PARASHURAMA (06) SHIPPED 2026-08-08 late — see RELEASES/06-parashurama.md
Story-order nav + Overview (fails/bands/cost strips) · Band ⚠ truth mark on
EVERY lead row · CIF merged (tab + modal chips + auto-rescore on refresh —
tomorrow's clean transcriptions need zero deploys, just import+refresh) ·
**drill pop-boxes: click any headline number → definition + exact constituent
rows + aggregate-vs-rows parity ("MATCH ✓") on the box** · cost lineage
explains flat-cost structure · operating frameworks in standing memory.
NEXT: LLM judge pass (CDS→FMS) when clean transcriptions land tomorrow
evening · drill coverage beyond headline KPIs · questionnaire answers
(env vars, relay counts, prompt diff, attendance, invoices) → update
FIXABILITY_MATRIX.md when they arrive.

## VAMANA V1–V5 SHIPPED 2026-08-08 (verified in browser, audit CLEAN)
Truth layer (`truth()` in dashboard.py: person/project/stage canonical joins,
1,061 Zoho-vs-CRM band disagreements SURFACED, 3 data bugs fixed incl.
parse_mixed_date for the 426-dropped-leads bug) · TOFU/MOFU/BOFU Funnel tab
(bands 1593/1213/13 + per-project daily stage table) · Inncircles-style lead
MODAL on every id click (7 tabs incl. Web History + Scoring green/purple/orange
from the July asks) · Insights tab (role lenses; red=failure only) + unified
glossary · NRI/RI audience filter (Telnyx/Plivo) + split time chart · unit
toggle now changes daily+funnel (was KPI-only) · Globe tab (canvas, drag/zoom,
NA cards for missing geo) · scope chips (Today/7d/ANGAD/Everything) · fixed-
window badges on Today/Eras/ops · feedback widget → RELEASES/feedback.jsonl ·
TEAM_SETUP.md for read-URI deployment. REMAINING from plan: V6 docs repair
(04-narasimha.md, schema_report.md, WORKLOG, exec_report) + V7 chase list.

## Release 05 VAMANA — read RELEASES/05-vamana-PLAN.md FIRST
The 2026-08-07 night audit (all chats + docs + the dev team's release notes)
produced the full recovery board there: 12 open complaints (B1–B12), 4 missed
pre-engagement asks (#67–70: colour scoring, web history, profiles, reactivation
signals), stale-doc repairs, and 13 data-semantics traps from the dev team's own
log (reminder scheduler NEVER shipped; outcome taxonomy broke 29 Jun/7 Jul; slot
provenance flipped 16 Jul; prod calling-hours guard OFF; DNC non-blocking before
~20 Jul). Work V1→V7 in order: truth layer → Inncircles-style lead modal →
TOFU/MOFU/BOFU board → interaction contract → questions map → docs repair →
external chase list.

## Work queue (exact state — updated 2026-08-07 evening)
- **Task #7 Narasimha-1: DONE.** Persona miners completed (3 agents, 307k tok):
  42 verified patterns → `RADHIKA_VNEXT_PERSONA.md` (openings/objections/closes
  + register + §5 automatable promises). Prepared patches written AND
  sandbox-verified (`git apply` clean): `prepared_patches/01` parse hardening,
  `02` null-visit fallback (THE 89% fix), `03` promised_callback/day-before/
  no-show planners. NOT applied — user applies per README.
- **Task #8 UX-2: DONE.** 15 tabs now: + **Review panel** (notebook questions
  answered live incl. dropped→recontacted→answered chain + ops daily table
  from the screenshot) · **MLID Profile** (per-human ledger: calls + per-call
  intents + slots + sched + WA + docs + visits + lead_events; search by
  MLID/PLID/pseudonym; every id cell everywhere is click-to-open) · **Eras**
  (dedicated sections) · provenance on KPIs · drop-stage chart click fixed.
- **Refresh button FIXED**: it was firing an incremental sync (= no-op due to
  stale-watermark bug); now button and /api/refresh both force --full.
  Restart dashboard.py to mint a fresh token after edits.
- **SV rescue lists** (2026-08-07, `RESCUE_SV/`): A never-dialled 228 ·
  B dialled-never-answered 2,951 · C no-show rebook 27 · D upcoming-2-days 5 ·
  E booked-on-call-no-visit-row 58. DNC/blocked (268) excluded.
- **IDENTITY SOLVED (2026-08-07 late): 6-digit ops MLIDs = Inncircles
  MASTERLEADID, found IN the DB** — no CSV needed:
  `call_transcript_intent_identification.master_lead_id` (per-call, via
  call_sid==roomName) + `Zoho_Database.reactivation_list` (direct
  lead_ref→masterleadid, wins on conflict). Source Lead ID from
  `ASBLVoiceBot.call_schedules` (45k, lead_ref-keyed) + zleads fallback.
  `master_maps()` in dashboard.py; add_ids/table() put MASTERLEADID +
  SourceLeadID beside every pseudonym everywhere; profile search resolves all
  keyspaces (992727 → LEAD-0109e331 verified). Coverage: 71% of dialled leads
  have MASTERLEADID, 52% SourceLeadID. RESCUE_SV CSVs regenerated with both
  (D 100%, C 96%, E 93%, B 57%, A 4% coverage). F_today_talked_over_1min.csv
  = daily >60s-talk list with recording URLs (26 on Aug 7, all recorded).
- User inputs awaited: telephony+Gemini rate card (₹/outcome tile). Fresh
  Zoho Leads CSV would still lift A/B-list MASTERLEADID coverage.
- Watch: growth-prod ramp still slow (Aug 7: 75 dials, 0 bookings, vs
  697–1,444/day the prior week) — FLAGGED, watch tomorrow. Post-cutover
  pipeline re-trace due ~Aug 14.

## Traps (learned the hard way — do not relearn)
- Units: calls vs leads (person×project) vs humans (MLID) — label ALWAYS
  (the 3,352-vs-2,941 confusion). Ops sheet "Successful Calls" = unique
  leads/day (Σ1,811), not call count.
- parse-before-write on patch scripts; never `replace(anchor, block+anchor)`
  when block ends with anchor (doubled-def bug, hit twice).
- to_parquet_safe stringifies mixed cols → re-coerce numerics after read.
- 24 verified toolchain bugs documented in WORKLOG Phase 8; UI five (B1,B3,
  B4,B7,B9) now FIXED at user request; SYNC-STALE-WATERMARK still unfixed →
  always sync --full.
- cohort workbooks/Zoho CSV re-imports must post-date the current salt
  (quarantine at `data/_stale_old_salt/` holds pre-shred imports).
- Attendance exists ONLY in ops sheets; visit_attended in Zoho export is
  constant false.
- posthook_payloads upstream deletes itself; our archive is the only history.
- `AI Leads` workbook + retranscript CSVs: phone columns masked/mangled —
  bridge via Master Lead ID / Source Lead ID through Zoho_Database.leads.
- Auto-memory (`~/.claude/.../memory/`) carries these rules across sessions;
  keep it and this file in sync.

## Key documents
PROTOCOL_SANKALPA.md · WORKLOG.md (14 phases) · RELEASES/01-matsya…04-narasimha
(+ daily/) · MIGRATION_AUDIT.md · ARCHITECTURE_AUDIT.md · TARGET_ARCHITECTURE.md
· exec_report.html · schema_report.md · HANDOFF.md (session starter).
