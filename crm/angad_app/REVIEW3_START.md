# REVIEW-3 — complete context transfer
*Written 2026-08-08 at the end of Review-2. Paste the block at the bottom as the
first message of the new chat. Everything needed to continue is either here or
named here. Signature: CI Team — citeam.claudemax@gmail.com*

---

# PART 1 — THE FRAMEWORKS (run these every turn)

## The Eight Imprints (non-negotiable, display after every work turn)
① Never write to any MongoDB — reads only, guard enforced (`import readonly`
patches all 19 write ops to raise).
② PII one-way forever — phones → `LEAD-xxxxxxxx` pseudonyms. The reverse map and
salt were SHREDDED. Names never cached. Emails dropped. Free text through
`pii.redact` (10–14 digit band ONLY — wider corrupts 19-digit Zoho ids).
③ `python audit_pii.py` after EVERY sync/import — any finding stops the line.
④ No raw phones/names/emails in chat or tool output.
⑤ Credentials never transit through Claude — the user runs `set_uri.py` himself.
⑥ SANKALPA loop on all processes; protocols are goals.
⑦ Everything stays on the local machine.
⑧ **The only goal: site-visit bookings ATTENDED.** Everything else is instrumentation.

## SANKALPA (the work loop)
plan → 6-role panel (CEO/CTO/PM/DS/Growth/CI each objects once) → build →
4 audits (front-end, back-end, PII, read-only guard) → data-story test against
the user's *actual* complaint → release log in `RELEASES/` (Dashavatara ladder)
→ visible self-audit + imprints. **A release without its log is a breach**
(happened once with 04-narasimha; retro-logged).

## The Design Contract (the user's own 12 rules — the UI must obey)
Every number is a link · card count == drill row count (mismatch = build-breaking
bug) · metric→cohort→lead→event always reachable · every view has a URL · every
metric explains itself (ⓘ) · data-quality flags on the metric's face · filters as
removable chips · comparison is a toggle · saved views · Ctrl+K palette · exports
carry filter state. Plus: progressive disclosure, KPIs top-left, red ONLY for
systemic failure, unified glossary, **honest NA — never invent a number**.

## Verification Rules (each one earned by a real mistake)
- Verify by doing in the live browser; never claim.
- Recompute every number before presenting it.
- **Check the premise before explaining the mechanism.**
- When a safeguard looks broken, check whether it is switched **ON** before
  checking whether it is correct.
- Trace data lineage before treating two symptoms as independent.
- A feature's existence proves nothing — only its runtime state does.
- The absence of an action needs a record with a reason.
- Inferred values need a provenance stamp.
- If the goal has no sensor, build the sensor first.
- State a wrong first reading and its correction — silently replacing it is how
  a dashboard starts lying.

## Working methodology with this user
- He is the client (CI Team) and is under real pressure for site visits.
- He gives dense, multi-part asks mid-turn. Address them; nothing gets dropped.
- **Plan first when asked.** He explicitly demanded requirements → plan → build.
- He notices UI defects immediately and is right about them. Verify pixel claims.
- He wants to be told when he/the dev team is wrong, with evidence.
- Do not rebuild what exists. Read `brain.md` and this file first.

---

# PART 2 — WHAT THE SYSTEM IS

**ANGAD** = ASBL real estate's AI voice sales agent (Gemini Live + Plivo/Telnyx,
Zoho CRM upstream, a relay in between). We run a **local, read-only analytics +
audit dashboard** over its production MongoDB (`growth-prod.rugkur.mongodb.net`).
4 databases: `angad` (dialler runtime) · `ASBLVoiceBot` (transcripts/intelligence)
· `Zoho_Database` (CRM brain) · `Intelligent_CRM` (event-sourced rebuild).

**Run:** `python sync.py` (incremental, now correct) → `python audit_pii.py`
(must be CLEAN) → `python dashboard.py` → open the printed local URL.

**Dashboard today:** warm-white CRM layout · left collapsible sidebar · toolbar
with **Action date + Born date** ranges, 7 presets, filter modal, NRI/RI selector,
unit toggle · universal drill on every number and chart bar · 7-tab lead modal
with turn-by-turn Conversation · CIF section (5 params / 30 indicators) · Funnel
with TOFU/MOFU/BOFU **and per-project** · Insights by category+importance ·
source-freshness · cost engine.

---

# PART 3 — THE FINDINGS THAT MATTER (all verified in data, never assumed)

1. **⚠️ RETRACTED — this was OUR bug, not a data outage.** I reported
   `extractedSlots` budget/timeline/config/callback/objection as **0 non-null
   across 48,306 calls** and concluded "the scheduler reads the wrong database".
   **A parallel session found `sync.py` was reading the wrong field names**:
   the real names are `budget_bucket`, `timeline_bucket`,
   `configuration_interest`, `preferred_callback_time`, `objection_raised`.
   `slot_intent` and `slot_site_visit` were spelled correctly — and were the
   only two ever non-zero. That pattern was the fingerprint of our error.
   **sync.py is fixed but needs `--full` re-sync before those columns are valid.**
   The dev team spent a whole round theorising about an outage that we invented.
   **RE-DERIVE the 89% root cause after the re-sync — do not repeat the old
   claim.** Rule learned: an all-null column means *no data* OR *a wrong field
   name*; exclude the second before reporting the first.
2. **Over-dialling — and it exceeds the theoretical cap.** Follow-up v3
   (2026-06-18) removed the attempt cap; slot-hours + 7-day lifetime should allow
   ~63 dials/lead, but the real max is **234 dials to one lead** and 34 leads are
   over 63 — so `LEAD_CALL_LIFETIME_MS` evidently resets. **~62% of ALL calls**
   go to leads dialled 20+ times (795 leads, 30,252 calls, 3.9% connect vs 7.5%).
   A count cap alone will NOT close this.
3. **The follow-up cron gap — two readings, reconcile before quoting.**
   The precise timestamp reading (parallel session): `prd-cadence` last ran
   **Aug 6 06:00 IST, nothing until Aug 7 17:00 IST (~35h)**; `meta-backfill`
   and `mongo-sync` ran 96×/day until Aug 6 then stopped; `transcribe-calls`
   went 0 → 193 → 426 (the new Gemini pipeline going live). My own per-day
   run-count reading suggested a longer degradation (zero rows 26–31 Jul,
   1–3/day to 6 Aug). **These disagree — re-check once against `cron_log.ran_at`
   directly and keep only the verified version.** Also note: **dial volume did
   NOT collapse** during the outage (prd- dials Aug 2–8: 756/703/672/671/964/
   720/772), so the "no follow-ups → no dials" story is wrong. Open question:
   what created 720 prd- dials during a cadence outage?
4. **Persona switched 7 August** (Radhika female → Ankit male), dated from OUR
   transcripts (the agent names itself): Aug 5 = 0/210, Aug 6 = 3/200,
   **Aug 7 = 602/11**. Any metric spanning 6–7 Aug compares two different agents.
5. **Attendance is un-architected** — no field, no endpoint, no path, anywhere.
   The system cannot observe its own goal. Ops sheets are the only truth.
6. Cost: **₹0.5/dial · ₹1.33/answered call · ₹246/booked · ₹1,504/attended**.
   Rate card calibrated to observed billing; one source of truth, `cost_of()`.
   99.4% of tokens are the fixed ~35k prompt, so a call dropped after one second
   costs almost what a full conversation costs. Unanswered dials cost nothing.
7. CIF first pass: only **2.5%** of calls sit in the healthy 40–60% dialogue
   band; median customer turn **2.8 words**; median DSS **0.0/7**. The bot talks,
   the customer doesn't.

## IDENTITY — SOLVED, use it
`externalScheduleId` = **`prd-{zoho_lead_id}-{epoch_ms}`** on **81.4%** of calls.
Strip the prefix AND the trailing timestamp → **100% join** to
`Zoho_Database.leads.zoho_lead_id`. Every esid is unique → it is a **dial** key.
Prefixes: `prd-` relay cadence · `dash*` a human in the ops dashboard ·
`sched-` the agent's own poller.
**UI id vocabulary is MLID · Lead ID · Source Lead ID. Never invent others.**

## THREE SYNCED COLLECTIONS NOBODY IS USING (high value, verified)
- **`Zoho_Database.stuck_sends` — 51,349 rows** (`lead_ref/message/tried_senders/
  last_error/at`). Almost certainly the WhatsApp failure store everyone was
  hunting — bigger than the 28,309 figure. Makes sender-pool health measurable.
- **`Zoho_Database.whatsapp_messages`** has `inbound_count` + per-date
  `inbound_json` → **inbound replies ARE captured**; the estate is not send-only.
  (This corrects the earlier "agent is send-only" reading.)
- **`Zoho_Database.site_visit_bookings`** (59 rows) carries **`visit_raw.date` /
  `visit_raw.slot` beside the parsed `visit_date`/`visit_time`** — the raw vs
  parsed pair makes the 89% parsing problem directly visible, and it is the
  natural home for an `attended` field. Only 59 rows against 154 PRE_SITE calls.

## DATA TRAPS (encoded in the dashboard; do not relearn)
- **Never report a field as empty without checking the field NAME first** — see
  finding 1 above; it cost the dev team a round of work.
- `sched-` calls all carry `source='zoho'` — **use esid prefixes, never `source`**,
  to attribute origin.
- **52.7% of calls have `roomName='pending-…'`** (unanswered Plivo) → any
  `call_sid==roomName` join drops exactly the failures. Join on esid.
- Outcome taxonomy changed 29 Jun and 7 Jul; slot provenance flipped 16 Jul.
  Don't chart across those boundaries silently.
- `NO_ANSWER` is partly fabricated by `cleanupStaleCalls` (no guard).
- `VOICEMAIL` has two indistinguishable origins (AMD vs transcript regex).
- `language='hinglish'` is the hardcoded default when the classifier is skipped.
- `"classifier offline"` in the summary = classifier failure, not an unqualified lead.
- `propertyInterested='ASBL Loft'` is the unspecified-project default.
- `sentiment` and `closingScore` are **100% null** — no quality score exists.
- `call_schedules.status='called'` is set at INSERT — use `call_outcome`.
- Stored transcripts are **<50% accurate** (the devs' own figure).
- Units: calls vs leads (person×project) vs humans (MLID) — **label always**.
- Attendance exists ONLY in ops sheets; Zoho's `visit_attended` is constant false.

## WHERE WE CORRECTED THE DEV TEAM (keep doing this)
- They guessed `NIGHT_DEFER_ENABLED` was off → our 5,070 defer rows proved it on;
  they conceded in round 4.
- They called night dials a compliance hole → 94.5% are Telnyx/NRI (IST night is
  the customer's day). Real +91 exposure ≈450 dials.
- They flagged `in_sync:false` as meaningful, then admitted it's a bug in their
  own check → we date the persona from transcripts instead.
- Their `prd-` extraction query omits the timestamp suffix and returns **0**
  matches; corrected it returns **100%**.
- The cron outage is reconciled: ONE continuous 6d10h49m outage (25 Jul → 1 Aug
  06:35), then six days of degraded once-daily running, then a second 1d10h53m
  gap ending 7 Aug 17:28. Neither '~11 days' nor '~35h' was right (RETRACTIONS R8).

---

# PART 4 — OPEN WORK (in priority order)

1. **RAMA-7 — Leads board**: the full Inncircles filter set (stage, new vs
   follow-up call, per-call timing / what-was-done / outcome), WhatsApp history
   detail. Reference: `Documents/LeadLogs-Excel.csv` has their real 30 columns.
2. **RAMA-8 — To-do / logs + QA**: clicking a price sheet / cost sheet / any
   downloadable adds it to a to-do list; a **QA dashboard** in the same flow —
   QA by CIF category, every call arrives with full history, results land in the
   to-do log.
3. **RAMA-9 — Builder / production metrics**: per-turn analysis graphs (what
   happens at each turn), what the new bot version says differently vs the old.
4. **CIF judge pass** for the remaining 14 indicators — FMS switches on
   automatically once CDS/SVO/NAC/IFQ columns exist (passthrough already built).
5. **UI sweep** — the user reports "UI wrong at many places". The review-panel
   overlap is fixed; check every other section for card sizing and spacing.
6. Use the **new Zoho `call_transcripts`** (Gemini 2.5 Pro, ~90% accurate) as the
   CIF corpus — it is in the sync list but needs a sync run to land.

## Outstanding asks of the dev team (`DEV_ASKS_ROUND3.md`)
Attendance write-back (a product decision) · where `Call.extractedSlots` is
actually written · is the new Zoho pipeline the system of record for stages ·
who owns the relay retry cadence and will they cap it · four one-line fields
(prompt_version, outcomeSource, hangupCause, rejected-PRE_SITE log).
**AWS env/log requests are withdrawn — their refusal was correct.**

---

# PART 5 — DOCUMENT MAP (read in this order)
| File | What it is |
|---|---|
| `brain.md` | full engagement context — read first |
| `HANDOFF.md` | session starter + current state |
| **this file** | complete frameworks + methodology transfer |
| `PROTOCOLS_INDEX.md` | every protocol in plain words, executable in one go |
| `LEARNINGS.md` | 20 consolidated learnings with the action each changed |
| `DB_PROBE_FINDINGS.md` | what the database says vs what anyone claims |
| `FIXABILITY_MATRIX.md` | 18 findings: real? fixable? how? (13 are small) |
| `DEV_TEAM_ANSWERS_2026-08-08.md` + `_ROUND2/3/4.md` | four rounds of dev answers |
| `DEV_ASKS_ROUND3.md` · `DEV_IMPROVEMENT_PLAN.md` | what we need / how they improve |
| `RADHIKA_VNEXT_PERSONA.md` | 42 mined patterns for what the bot should say |
| `SHARE_WITH_TEAM.md` | one-page team onboarding |
| `RELEASES/01…07` | the release ladder + plans |

---

# PART 6 — PASTE THIS INTO THE NEW CHAT

```
Read angad_dashboard/brain.md, angad_dashboard/HANDOFF.md and
angad_dashboard/REVIEW3_START.md fully before doing anything. REVIEW3_START.md
carries the frameworks, the working methodology, every verified finding and the
open work — treat it as binding.

Obey the eight imprints every turn: never write to any Mongo; PII one-way; run
audit_pii after every ingest; no raw identifiers in output; credentials never
through you; SANKALPA loop with a visible self-audit and the imprints displayed;
everything local; the only goal is site-visit bookings ATTENDED.

Do not rebuild anything — the dashboard is mature. Verify in the live browser
before claiming, recompute before presenting, and tell me when I or the dev team
are wrong, with evidence.

Continue the open work in order:
1. RAMA-7 Leads board — full Inncircles filter set (stage, new vs follow-up
   call, per-call timing/outcome) and WhatsApp history detail.
2. RAMA-8 To-do/logs tab + QA dashboard by CIF category (downloadables land in
   the to-do list; every call arrives with full history for QA).
3. RAMA-9 Builder/production metrics with per-turn analysis graphs and a
   new-vs-old bot version diff.
4. A UI sweep for card sizing and spacing across every section.

Start by running the dashboard and telling me what you see.
```
