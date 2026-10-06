# PROTOCOLS & PLANS INDEX — everything we run, in plain words
*One page. Each entry: what it is · when it fires · how to execute it in one go.
2026-08-08. Signature: CI Team — citeam.claudemax@gmail.com · Built with Claude.*

---

## A. STANDING PROTOCOLS (always on, no trigger needed)

### 1. The Eight Imprints
**Plain words:** the non-negotiable rules of this engagement.
① never write to any Mongo ② PII is one-way forever ③ audit after every data
pull ④ no raw phone numbers/names in any output ⑤ credentials never pass
through Claude ⑥ run the SANKALPA loop ⑦ everything stays on this machine
⑧ the only goal is site-visit bookings ATTENDED.
**Fires:** every turn. **Execute:** displayed at the end of every work turn.

### 2. SANKALPA (the work loop)
**Plain words:** how any change gets made without breaking trust.
plan → 6-role panel (CEO/CTO/PM/DS/Growth/CI each object once) → build →
4 audits (front-end, back-end, PII, read-only guard) → data-story test
(does this answer the user's actual complaint?) → release log → self-audit.
**Fires:** every substantive change. **Execute:** `RELEASES/NN-name-PLAN.md`
then `RELEASES/NN-name.md`. A release without its log is a breach.

### 3. The Design Contract
**Plain words:** the rules the dashboard UI must obey.
Every number is a link · the card count and the drill count are the same query
· metric→cohort→lead→event always reachable · every view has a URL · every
metric explains itself · data-quality flags on the metric's face · filters are
chips · comparison is a toggle · saved views · Ctrl+K palette · exports carry
their filter state.
**Fires:** all dashboard work. **Execute:** checked in the release log, rule by rule.

### 4. Verification Rules
**Plain words:** how we avoid being confidently wrong.
Verify by doing in the browser · recompute before presenting · check the
premise before explaining the mechanism · if a safeguard looks broken check
whether it's switched ON first · trace lineage before treating two symptoms as
separate · honest NA beats an invented number.
**Fires:** every claim. **Execute:** it's in the self-audit line.

---

## B. OPERATIONAL RUNBOOKS (fire on a trigger)

### 5. Daily Refresh
**Plain words:** get today's truth.
**Execute in one go:** press **Fetch fresh data from Mongo** in the dashboard
(forces `--full`, then auto-runs the CIF rescore), or in a terminal:
`python sync.py --full && python audit_pii.py && python cif.py`
**Gate:** audit must say CLEAN or work stops.

### 6. New Clean-Transcription Drop  *(tomorrow evening)*
**Plain words:** new accurate transcripts arrive → every conversation score updates.
**Execute in one go:** put the CSVs where the importer looks → press refresh.
That's it — sync imports them, CIF rescores automatically, the CIF tab, the
per-lead chips and the turn viewer all update. No code changes, no deploy.

### 7. SV Rescue Sweep
**Plain words:** who can still visit this week.
**Execute:** the five lists in `RESCUE_SV/` — D (confirm today), C (rebook
no-shows), E (re-close evaporated bookings), B (retry never-answered),
A (first-dial never-dialled), plus F (today's >1-minute talks with recordings).
Regenerate any time from the dashboard's own tables.

### 8. Dev Handoff Cycle
**Plain words:** how we get facts only the builders can see.
**Execute:** send the questionnaire → they answer with `[live]`/`[docs]`/
`UNKNOWN` tags and PII rules → we verify every claim against our cache →
corrections go back → `FIXABILITY_MATRIX.md` updates.

---

## C. RELEASE LADDER (what we've shipped, in order)

| # | Name | What it delivered |
|---|---|---|
| 01 | Matsya | first light: read-only cache + dashboard |
| 02 | Kurma | the cockpit: Today screen, Call Room, funnel board |
| 03 | Varaha | eras, source/URL intelligence, latency proxy, metric history |
| 04 | Narasimha | truth corpus, prompt harness, persona mining, prepared patches |
| 05 | Vamana | recovery: truth layer, lead modal, TOFU/MOFU/BOFU, design contract |
| 06 | Parashurama | CIF merge, layout redesign, drill pop-boxes |
| 07 | *(next)* Rama | attendance capture + LLM judge pass (when data lands) |

---

## D. KEY DOCUMENTS (what to read when)

| Document | Read it when |
|---|---|
| `brain.md` | starting any session — full context |
| `PROTOCOLS_INDEX.md` | this file — "how do we do X?" |
| `LEARNINGS.md` | before making a claim about the system |
| `FIXABILITY_MATRIX.md` | deciding what to fix next (18 findings, 13 are small) |
| `DEV_IMPROVEMENT_PLAN.md` | talking to the builder team |
| `DEV_TEAM_ANSWERS_2026-08-08.md` | any question about how the agent behaves |
| `RADHIKA_VNEXT_PERSONA.md` | any question about what the bot should say |
| `TEAM_SETUP.md` | onboarding someone new to the dashboard |
| `WORKLOG.md` | what happened, phase by phase |
