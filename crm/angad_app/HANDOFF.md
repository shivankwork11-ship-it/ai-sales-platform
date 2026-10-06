# HANDOFF — paste this to start a new chat
*Last updated 2026-08-08 (Release 08 KRISHNA shipped). Read brain.md first, then
this. The engagement is deep — do NOT rebuild anything; read before you touch.*

## ⚠⚠ READ `RELEASES/08-krishna.md`, then `RETRACTIONS.md`
Every claim this engagement has withdrawn lives in `RETRACTIONS.md` and nowhere
else. If you find a superseded number in a live document, delete it — do not
annotate it. Verified state as of 2026-08-08:
1. **Cost: ₹0.5/dial · ₹1.33/answered call · ₹246/booked · ₹1,504/attended.**
   Rate card $0.50/$2.50 per 1M + ₹0.60/started minute (answered only),
   calibrated against the dev team's observed figure to within 2%. One source of
   truth: `cost_of()`. Nothing else may compute ₹.
2. **`reached` is NOT "the line was answered."** False on every DROPPED and
   VOICEMAIL row, 5,428 of which opened a Gemini session. **A human picks up on
   20.9% of dials**; the dashboard's "Connected" reports 7.4%. Two-thirds of the
   humans who answer hang up inside 19 seconds.
3. **The 89% reminder gap is the cron's fire condition.**
   `pre-site-visit-reminders`: 987 runs, 35,291 scanned, **0 fired, 0 errors.**
   `prepared_patches/02` does NOT fix this — it patches the agent's planner, a
   different system, and was written against a retracted theory.
4. **A stuck stage fires the wrong dependency** — 1,029 dials went out *after*
   the booking call, max 229 to one lead, because the retry cadence keys off a
   stage that never advances. Zoho `stage` is not a maintained field: the only
   jobs that write it ran 3 times ever, unscheduled.
5. **Attendance now has a sensor** (`/api/attendance`, RELEASES/attendance.jsonl).
   165 bookings visible, **7 with any verdict, 158 unknown — 4.2% coverage.**
   Until that rises, every improvement claim stays provisional.
6. **`Intelligent_CRM` has stopped being written** — lead_events, calls,
   crm_leads, persons, mlid_registry all ~3.2 days stale, messages and
   site_visits 5.1d. The funnel's CRM-stage side reads from it, so those numbers
   lag and part of the "Zoho vs CRM disagreement" is that lag, not disagreement.
   Check `Database health` before quoting any CRM-stage figure.
7. **Start on `Still pending`** — it counts everything left undone, live, and
   routes to the rows. It is the fastest way into this engagement.

## STATE IN ONE PARAGRAPH
A local, read-only analytics + audit dashboard over ANGAD's production MongoDB.
Warm-white CRM layout: left sidebar, toolbar with permanently-visible **Action
date + Born date** ranges and 7 presets, filter modal, NRI/RI selector. Every
headline number and chart bar drills into the rows behind it. Every lead opens a
7-tab modal including a turn-by-turn Conversation view. CIF is rebuilt from the
team's real scoring dashboard (5 parameters, 30 indicators, verbatim
definitions, 16 computing). Four rounds of developer answers are archived and
verified in-repo.

## RUN IT
`python sync.py` (now incremental & correct) → `python audit_pii.py` (must be
CLEAN) → `python dashboard.py`. Share with the team via `SHARE_WITH_TEAM.md`.

## THE FINDINGS THAT MATTER (each MEASURED and recomputed before writing)
1. **The site-visit reminder cron has never fired a single reminder.**
   `pre-site-visit-reminders`: **987 runs · 35,291 scanned · 0 fired_24h ·
   0 fired_3h · 0 errors.** It runs hourly, matches nothing, reports success —
   so it is green on every dashboard the dev team owns. Candidates exist (41
   parsed visit dates, 6 still in the future). **This is the 89% gap.** Highest
   -priority dev ask: the fire condition.
2. **Over-dialling is a dated regression.** Follow-up v3 (2026-06-18) removed
   the attempt cap. 62.6% of ALL calls go to leads already dialled 20+ times:
   3.9% connect. Max observed: 234 dials to one lead.
3. **prd-cadence outage, reconciled:** one continuous outage of **6d10h49m**
   (25 Jul ~19:46 → 1 Aug 06:35), then six days of degraded once-daily running,
   then a second gap of 1d10h53m ending 7 Aug 17:28, then recovery.
   **Dial volume did NOT collapse** (Aug 2–8: 756/703/672/671/964/720/772).
4. **Persona switched 7 Aug** (Radhika→Ankit), dated from our own transcripts:
   Aug 5 0/210, Aug 6 3/200, **Aug 7 602/11**. Any metric spanning 6–7 Aug
   compares two different agents.
5. **Attendance is un-architected** — no field, no endpoint, anywhere. The
   system cannot observe its own goal. Ops sheets are the only truth.

## IDENTITY — SOLVED, use this
`externalScheduleId` = **`prd-{zoho_lead_id}-{epoch_ms}`** on 81.4% of calls.
Strip the prefix AND the trailing timestamp → **100% join to
`Zoho_Database.leads.zoho_lead_id`**. Every esid is unique, so it is a DIAL key
(the dev's round-4 warning that it might be a lead key is disproven).
esid prefixes: `prd-`=relay cadence · `dash*`=human · `sched-`=agent poller.
UI id vocabulary is **MLID · Lead ID · Source Lead ID** — never invent others.

## WHERE WE CORRECTED THE DEV TEAM (keep doing this)
- They guessed `NIGHT_DEFER_ENABLED` was off; our 5,070 defer rows proved it on
  — they conceded in round 4.
- They said night dials were a compliance hole; 94.5% are Telnyx/NRI where IST
  night is the customer's day. Real exposure ≈450 +91 dials.
- They flagged `in_sync:false` as meaningful, then said it is a bug in their own
  check. We now date the persona from transcripts instead.
- Their `prd-` extraction query omits the timestamp suffix and returns 0 matches.

## OPEN WORK (in order)
1. **RAMA-7** Leads board: full Inncircles filter set (stage, new vs follow-up
   call, per-call timing/outcome), WhatsApp history detail.
2. **RAMA-8** To-do / logs tab: clicking a price sheet / cost sheet / any
   downloadable adds it to a to-do list; **QA dashboard** in the same flow —
   QA by CIF category, every call arrives with full history, results to to-do.
3. **RAMA-9** Builder/production metrics: per-turn analysis graphs, what the new
   bot version says differently vs the old.
4. CIF **judge pass** for the remaining 14 indicators (FMS switches on
   automatically once CDS/SVO/NAC/IFQ columns exist — the passthrough is built).
5. UI polish: user reports "UI wrong at many places" — sweep every section for
   card sizing/overlap after the review-panel fix.

## DEV ASKS OUTSTANDING (`DEV_ASKS_ROUND3.md`)
Attendance write-back (product decision) · where `Call.extractedSlots` is
written · is the new Zoho Gemini pipeline the system of record · who owns the
relay retry cadence / will they cap it · four one-line persistence fields
(prompt_version, outcomeSource, hangupCause, rejected-PRE_SITE log).
AWS env/log requests are **withdrawn** — their refusal was correct.

---
*(original starter prompt below)*

# HANDOFF — paste this to start a new chat

Copy the block below as the first message of a new Claude Code session opened
in `C:\Users\Tiwari\Documents\Claude code\voice agent stage wise analysis`:

---

Read `angad_dashboard/brain.md` fully before doing anything — it is the
complete context of our engagement (ANGAD voice-agent analytics; goal:
site-visit bookings ATTENDED). Follow every imprinted rule in it at every
turn: never write any Mongo, one-way PII, audit_pii after every ingest, no raw
identifiers, credentials never through you, SANKALPA loop with per-turn
self-audit + imprints displayed, everything local, Kaalbhojaditya era naming,
repo patches prepared-only. Auto-memory carries the same rules — obey both.

Then continue the work queue exactly where brain.md says we left off:
1. Resume the persona-mining workflow (resumeFromRunId wf_4823b0eb-acb) if
   the session limit has reset; fold results into a Radhika v-next persona
   spec with DO/DON'T exemplars (names as [name]).
2. Build UX-2: the MLID Profile page (per-human transaction ledger with
   per-call intents, extracted slots, WhatsApp, visits, stage), dedicated
   Kaalbhojaditya and ANGAD sections, the Questions map, provenance labels on
   every metric, click-to-open-leads everywhere.
3. Write the prepared planner patches (prepared_patches/) led by the
   visit-time-parsing fix for the 89% booking-without-reminder gap.
4. Keep the daily watch: growth-prod dial ramp + bookings (Aug 7 was 75 dials
   / 0 bookings — flag if the trend holds), and the post-cutover pipeline
   re-trace due around Aug 14.

Verify the environment first (read-only): `python test_connection.py` output
is mine to run if credentials are involved — ask me. `python dashboard.py`
should show the Today tab current after a `sync.py --full`.

---

*Signature: CI Team — citeam.claudemax@gmail.com · Built with Claude (Anthropic)*
