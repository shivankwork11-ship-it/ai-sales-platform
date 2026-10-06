# Release 08 — KRISHNA (release log, 2026-08-08)
*Everything below was recomputed on the post-re-sync cache or verified in the
running dashboard before being written down. Where a number replaces one we
previously published, the old value is shown so the change is auditable.
Signature: CI Team — citeam.claudemax@gmail.com · Built with Claude (Anthropic).*

## 0. Housekeeping
- `python sync.py --full` run (the slot field-name fix needs it). Confirmed the
  fix was genuinely in `sync.py:308-319` **before** spending the re-sync.
- `python audit_pii.py` → **CLEAN** (66 files, 468,583 text values).
- Four stale `dashboard.py` processes from the previous session were still
  holding port 8777, which is why valid tokens were being refused. Killed.

## 1. THE COST MODEL WAS WRONG — corrected (the headline of this release)

Trigger: a WhatsApp exchange in which the dev team stated the observed cost as
**₹1.60–1.70 Gemini + ₹0.60 Plivo per answered Indian call**, and stated that
**nothing is charged before pickup**. Both were tested against the cache.

**Their claims — verdict:**

| Claim | Verdict | Evidence |
|---|---|---|
| No cost before pickup | **TRUE** | BUSY 3/18,051 open a session (0.0%); NOT_CONNECTED 1/8,411 (0.0%); NO_ANSWER 6/2,537 (0.2%). 83.5% of all 49,319 dials consume zero tokens. |
| Pick up + drop at 1s still costs | **TRUE, and understated** | corr(duration, tokens) = **0.12**. DROPPED: median 23s / 35,482 tokens. PRE_SITE: median 83s / 37,635 tokens. 4x the call, ~6% the cost. **99.4% of all tokens are prompt tokens.** |
| ₹1.60–1.70 per answered call | **TRUE — ours was wrong** | see below |

**Our error.** The dashboard used **$3 / $12 per 1M** — Pro-tier *text* list
pricing that we assumed and labelled "the dev team's documented rates". It was
never confirmed. Against our own exact token counts (mean 32,753 prompt + 190
response over 8,149 answered calls) it produced **₹9.65/answered call**, 5.9x
the observed figure.

**Recalibration.** At **$0.50 / $2.50 per 1M** (USD→INR 96) our token counts
reproduce their observed bill at **mean ₹1.62 / median ₹1.74** — inside their
stated ₹1.60–1.70 band, to within 2%. Two independent sources agreeing that
closely is the strongest evidence available, so those are now the rates.
Telephony is **₹0.60 per started minute, 1-minute minimum, answered calls only**.

**A second bug found while fixing the first.** The old model billed telephony on
`reached`. But `reached` is False on **all** DROPPED and **all** VOICEMAIL rows —
yet **5,428 of them opened a Gemini session** (median 19s of audio), so the
handset was physically up and the carrier billed it. Costing on `reached`
silently dropped the largest spend bucket. Billing now keys on evidence the line
opened, not on the outcome label. `duration_sec` alone is **not** used as that
evidence: 44% of NO_ANSWER rows carry a median 30s, which is ring time.

**Published numbers that change (Jul 17 → Aug 8 window):**

| Metric | Was | Now |
|---|---|---|
| ₹ per call (all dials) | ~₹1.7 | **₹0.5** |
| ₹ per answered call | (not published) | **₹1.33** |
| ₹ per booked human | ₹639 | **₹246** |
| ₹ per attended visit | ₹4,995 | **₹1,504** |
| Share of spend on bookings | ~2% | **1.8%** |
| Largest spend bucket | — | **DROPPED, 46.5%**; VOICEMAIL 26.3% |

The rate card is now a single module-level block (`cost_of()` in dashboard.py);
nothing else may re-implement ₹. The Insights cost card now **computes from it
live** instead of carrying hardcoded percentages, which is how the old text
became false the moment the card changed.

**Still unverified, stated on the card:** we hold no carrier invoice, so any
per-attempt carrier fee is not captured; Telnyx/NRI telephony is understated
(rate unknown).

## 2. THE CONNECT RATE IS UNDERSTATED — three-tier pickup

Falling straight out of the `reached` bug. All-time, n = 49,319:

| | calls | % of dials |
|---|---|---|
| Line opened (billable, incl. answering machines) | 19,571 | **39.7%** |
| **A human picked up** (reached + DROPPED with duration) | **10,332** | **20.9%** |
| Conversation counted — what the dashboard reports as "Connected" | 3,661 | 7.4% |

**We are not failing to reach people. One in five dials reaches a human — and
two-thirds of those hang up inside the first 19 seconds.** That reframes the
funnel: the bottleneck is the opening, not the dialling.

## 3. THE 89% REMINDER GAP — re-derived, previous explanation RETRACTED

The retraction of "the scheduler reads the wrong database" was correct (it was
our field-name bug). But the *replacement* theory — "the planner has no parsed
visit time" — **is also false**, and is retracted here:

| booked leads (n=125) | |
|---|---|
| Booking call carried a **parsed visit date** | **79 (63.2%)** |
| Got a site-visit follow-up scheduled | 36 (28.8%) |
| Got only generic retry schedules | 71 (56.8%) |
| Got nothing scheduled at all | 18 (14.4%) |
| **Parsed date present AND still no follow-up** | **53 (42.4%)** |
| Follow-up fired *without* a parsed date | 10 |

`slot_site_visit` holds clean `{'date': …, 'slot': …}` structures. The input
exists on two-thirds of bookings and is ignored; 10 leads got a follow-up
without one, so it is not even a hard requirement. **The failure is downstream
of parsing.** Corroboration: across all 48,097 schedule rows `metadata.reason`
contains only `fast_retry_*` and `daily_retry_*` — there is no reminder reason
anywhere in the collection.

## 4. STUCK STAGE → WRONG DEPENDENCY FIRES (the user's hypothesis, confirmed)

**After a lead agrees to a site visit, the system keeps cold-retry dialling
them.** 1,029 dials went out *after* the booking call to **110 of 125 booked
leads** — median 4 more dials, **max 229 to one lead**. 453 are `prd-` relay
cadence. Outcomes: BUSY 236, DROPPED 198, NOT_CONNECTED 181.

Mechanism: the retry cadence keys off the lead's **stage**; the stage never
advances; so the cadence reads an already-booked lead as un-contacted and fires
the wrong branch.

## 5. THE STAGE DISAGREEMENT IS ONE-DIRECTIONAL STALENESS

50.6% of leads carrying both stages disagree (1,206 of 2,383). Not noise:

| Zoho says | ANGAD says | leads | ever dialled | ever connected | median dials |
|---|---|---|---|---|---|
| New Lead | Lead Initiated | **706** | 631 | 361 | **14** |
| New Lead | Not Interested | 310 | 309 | 126 | 1 |
| Pre Site Visit | Lead Initiated | 18 | 18 | 18 | 14 |

Zoho still calls 706 leads "New Lead" after ANGAD dialled them a median of 14
times and connected on 361. **The stage write-back into Zoho is not happening** —
same root as §4. Zoho `stage` is also null on 1,828 of 4,212 lead rows.

## 6. FIRST STAGE-DEPENDENCY LEDGER (ANGAD era)

| Dependency | met | unmet |
|---|---|---|
| Zoho lead → ever dialled | 2,927 | 319 |
| dialled → ever connected | 1,448 | 1,700 |
| connected → ever qualified (classifier) | **97** | **1,351** |
| qualified → ever booked | 34 | 63 |

The classifier line is **not** reported as a 93% failure: with `sentiment` and
`closingScore` 100% null across all 49,319 calls, "qualified" is an unvalidated
sensor. Validate the sensor before quoting the rate.

Also: **`site_visit_date` is null on all 4,212 Zoho lead rows.** Attendance still
has no sensor anywhere in the estate.

## 7. SHIPPED IN THE UI

- **Daily Report section** (RAMA-3 from the 07 plan): Date · Week · Project ×
  [New Lead · Lead Initiated · Pre Site Visit · Post Site Visit · Sales Closure ·
  Not Interested · Flat Blocked · Unstaged · Total · ⚠ disagree], one row per
  born-date × project, sticky TOTAL row. **Every count cell drills to the leads
  behind it** with Total Count, search, Download and a parity line. Verified
  live: a cell reading 11 opened 11 rows, **MATCH ✓**.
  Carries a **stage-system toggle** (Zoho / ANGAD) because of §5 — the report
  states which system it is reading rather than quietly picking a winner.
  Grain is the lead (person × project), dated by **birth, not dial**.
- **Warmer theme** — verified live, body `rgb(247, 242, 234)`: more amber in the
  paper, ink browned off black, all six categorical colours desaturated toward
  earth for long reading sessions.

## 7b. PII BREACH FOUND AND CLOSED (imprint 2)

`Zoho_Database.call_transcripts` was caching customer first names: the agent
opens with *"Good evening, &lt;Name&gt;."* **`audit_pii.py` reported CLEAN over
it** — it looks for phones, emails and credentials, and cannot see a name. One
confirmed leak in 187 rows.

Fixed: `pii.strip_names()` / `pii.clean_text()` redact the vocative slot and
honorific+name; `sync.py` now routes **every** free-text field through
`clean_text()` instead of `redact()`. Collection re-pulled; zero leaks remain.

Two mistakes made while fixing it, both caught before they shipped:
- A module-level `re.I` made `[A-Z][a-z]` match anything, so *"Hi, this is
  Anandita"* was redacted to *"Hi, [name] is"* — destroying the
  agent-self-introduction signal we use to date the persona switch. The flag is
  now scoped to the greeting alternation only.
- Honorifics had to run BEFORE the vocative, or *"Good morning, Mr. Sharma"*
  redacted "Mr" and left "Sharma" in the clear.

**A THIRD PERSONA EXISTS.** The Zoho corpus has the agent introducing itself as
**Anandita** (3 calls) alongside Ankit (1). The Radhika→Ankit story is
incomplete — do not treat post-7-Aug as one agent.

## 7c. CIF v2 — all four user-approved changes shipped
1. **Corpus**: Zoho Gemini-2.5-Pro transcripts (tier A, ~90%) preferred; the
   audio re-transcriptions (tier B) retained rather than discarded — 669 calls
   you can segment beats 113 you cannot. Every score carries its tier.
2. **Substance gate**: conversation indicators compute ONLY where the customer
   actually spoke — **308 of 781 (39.4%)**. Before this, a call where the
   customer grunted once produced a confident-looking balance score.
3. **Persona era**: every indicator segmented; pooling three agents averaged
   them into nothing.
4. **NA reasons**: judge-backlog vs empty-upstream-column vs too-few-substantive
   calls are now distinguished on the face of each indicator.

Two bugs found and fixed inside this work, both by comparing against the
previous run's count:
- Dedup on `(lead_ref, day)` collapsed 3,862 rows to 212 — a lead is dialled
  several times a day and each dial is its own conversation.
- `call_date` is **NULL on 3,853 of 3,862** re-transcript rows and pandas
  string-concat **propagates NA**, so an NA-containing dedup key made every such
  row identical and left exactly ONE survivor. Never build a dedup key by
  concatenating nullable columns. Now keyed on lead + transcript hash: 781 rows.

## 7d. RAMA-7 Leads board — shipped
One row per lead with the Inncircles Lead-Logs column shape (MLID · Lead ID ·
Source Lead ID · Born date · Project · both stages · Call type · Contacted ·
Dials/Answered/Connected/Booked · Talk · Response gap · Last outcome · Last dial
· WA sent/replies/replied · Source · Status), with the board's own filter row:
call type (**New / Follow-up / Never dialled**), contacted, either stage system,
project, last outcome, attempt band, source, WA replied, plus id search.
Verified live: 4,219 leads → **3,523** on the Follow-up filter, matching the
backend exactly. 238 never dialled · 3,140 contacted · 173 with a WhatsApp reply.

`Contacted` uses the corrected pickup test, not `reached`.

**Performance bug fixed:** the first build took **59 seconds** — a
`groupby.apply` that re-sorted inside every one of ~4,000 groups. Sort-once then
`.last()` gives the same answer in **0.37s**.

**Ad-blocker trap:** the endpoint was originally `/api/leadsboard`. That path
contains the substring **"ads"**, which ad-blocker filter lists match, and the
browser refused it with `ERR_BLOCKED_BY_CLIENT` before it ever reached the
server. Renamed `/api/board`. Never put "ads" in a URL path.

## 7e. UI containment pass — APPLIED BUT NOT VISUALLY VERIFIED
Standard cause of "the UI is wrong in many places": a flex/grid child defaults
to `min-width:auto`, so one wide table or chart pushes the whole column past the
viewport and every section inherits a horizontal scrollbar. Applied
`min-width:0` / `max-width:100%` containment, `overflow:hidden` on cards, and
self-scrolling for any table that escaped `.tblwrap`.

**This is written from the structural cause, not from what I saw.** The browser
pane would not composite (`window.innerWidth` reported 0), so every layout
measurement I took was invalid and I discarded them. The earlier "every section
overflows by N px" readings in this session are NOT evidence. Re-check on a real
screen before believing this is fixed.

## 7f. ⚠⚠ THE 89% GAP — FOUND. The reminder cron has never fired.

Found by running the loop properly: the Daily Report screenshot showed 25 of 25
leads "Unstaged", which sent me to the cron ledger instead of to the UI.

`Zoho_Database.cron_log`, task `pre-site-visit-reminders`:

| | |
|---|---|
| Runs since 26 June | **987** (24×/day, still running today) |
| Records scanned | **35,291** |
| `fired_24h` | **0** |
| `fired_3h` | **0** |
| `errors_count` | **0** |

**The job runs every hour, scans thousands of rows, fires nothing, and reports
success.** It is green on every dashboard the dev team has.

It is not starving for candidates: `site_visit_bookings` holds 41 parsed
`visit_date` values and **6 visits are still in the future** (out to 30 Aug).
`scanned` grows monotonically (72 → 84/run) as bookings grow, so it is finding
rows and its firing condition never matches.

**This supersedes all three earlier explanations**, including my own from
earlier today:
1. ~~"the scheduler reads the wrong database"~~ — our sync field-name bug.
2. ~~"the planner has no parsed visit time"~~ — it is parsed on 63.2%.
3. ~~"the failure is downstream of parsing"~~ — directionally right, no mechanism.
4. **The reminder cron's firing condition never evaluates true.** ← runtime record.

**Dev ask, highest priority:** the fire condition in `pre-site-visit-reminders`.
It scans, it matches nothing, it logs success. One log line inside the match
block would have shown this months ago.

## 7g. Zoho `stage` is not a maintained field — it is a hand-run backfill

Same investigation. The only two jobs that write it have run **three times in
total, ever**: `backfill-presite-stage` once (20 Jul), `reconcile-stage-from-status`
twice (21 Jul). Neither is scheduled. `workflow-queue-v2` has **0 runs ever**.

Consequence, dated precisely — `stage` null by born-date:

| Born | % with no Zoho stage |
|---|---|
| 17–27 Jul | 18–51% |
| **29 Jul onward** | **95–100%** |
| 6, 7, 8 Aug | **100%, 100%, 100%** |

`prd_stage` is populated on all of them. So the "50.6% disagreement" reported in
§5 understates it: for everything born since 29 July there is nothing to
disagree WITH. Any funnel or daily report reading Zoho stage is reading a field
nobody updates. The Daily Report's stage toggle exists for exactly this.

Also confirmed: `meta-backfill` and `mark-unique-leads` both stopped on 6 Aug.

## 7h. RAMA-9 — per-turn analysis (shipped) and the opening-line finding

4,080 transcripts, turn by turn:

| Turn | Calls alive | Survival | Agent words | Customer words | Customer replied |
|---|---|---|---|---|---|
| 1 | 1,400 | 100% | **22.9** | **4.0** | **43.7%** |
| 2 | 555 | 39.6% | 18.6 | 4.6 | 75.1% |
| 3 | 407 | 29.1% | 18.4 | 5.4 | 80.1% |
| 4 | 312 | 22.3% | 20.3 | 5.7 | 85.6% |

**60.4% of all calls die between turn 1 and turn 2** — the largest single drop
anywhere in this system. The agent opens with 22.9 words; the customer answers
with 4.0, and 56% do not answer at all. Everyone who survives that first
exchange behaves far better (reply rate 75% → 86%).

This joins up with §2: 20.9% of dials reach a human but only 7.4% become a
"conversation". **The loss is one exchange wide, and it is the opening line.**
That is a prompt change, not an infrastructure project — the highest-leverage
fix on the board.

Version diff (grouped by the name the agent gives itself — the only per-call
version evidence that exists): Radhika 479 calls / 24.0 agent words per turn ·
Anandita 3 / 9.9 · Ankit 1 / 10.0. The newer versions are far less talkative,
but with 3 and 1 calls those rows are directional only and are labelled as such
on the page.

## 7i. RAMA-8 — QA queue, document ledger, to-do (shipped)
- **QA queue by CIF category** (5 categories): worst calls first, ranked by
  combined percentile across that category's indicators, DER inverted. Only
  substantive calls are queued — QA-ing a call nobody spoke on is not QA. Every
  row carries its corpus and accuracy tier so you know how far to trust the
  transcript before judging the agent. Verified live: 317 rows, 5 categories.
- **Document ledger**: 134 requests (price_sheet 53 · brochure 31 · unit_plan 22
  · floor_plan 22), **48 blocked with the bot's own reason**, against a 63-item
  library. This is the "downloadables" surface asked for.
- **To-do**: append-only local journal, `RELEASES/todo.jsonl`. No database write.

**PII limit stated, not papered over.** Free-text notes pass through
`pii.clean_text()`, which strips phones, emails and greeting-slot names. A name
typed freely ("call Priya back") **cannot** be reliably detected. Tested with a
deliberate leak: the phone was redacted, the name was not. The test row was
deleted and the UI now carries an explicit warning to use MLID/Lead ID instead.
Do not claim this input is name-safe.

## 7j. UI — the actual defect, fixed and measured
`.cap` had **no margin and no line-height**. The trailing caption under the
Daily Report table sat flush against the table's bottom border and ran as one
dense full-width line — that is the "text at the bottom looks wrong".

Fixed and verified by computed style (not by eye — the pane still would not
composite): `margin-top` 0 → **12px**, `line-height` → **19.2px**, measure
capped at **595px (92ch)**, colour `--muted`, and `section>*:last-child` gets
**28px** bottom margin so nothing touches the viewport edge. KPI tiles now
precede the explanatory note (contract: KPIs top-left) — DOM order verified as
`h2 → tools → kpis → note → table → cap`.

## 7k. X2 CLEARED — the goal finally has a sensor

Attendance had no field, no endpoint and no path anywhere. `/api/attendance` is
now the sensor: one row per booking (call outcome + Zoho booking rows,
de-duplicated on lead × visit date), a recordable verdict
(`attended · no_show · rescheduled · cancelled · unknown`), appended to a local
journal. No database write.

**165 bookings visible · 7 with any verdict · 158 unknown · 4.2% coverage.**
UNKNOWN is a first-class state — a sensor that guesses is not a sensor. That
4.2% is the honest measure of how provisional every claim in this engagement
remains.

## 7l. Day-by-day and per-project panels (X2 was blocking these)
- **Day by day**: 23 days × 17 measures. Volume chart (dials / picked up / not
  picked up) plus a tick-to-overlay chart of 8 rate-and-per-call series —
  pickup %, median & average turns, customer talk share, customer/agent turns,
  NRI and RI pickup. Rates and counts are separated deliberately: plotting them
  on one axis is how a busy day gets mistaken for a good day.
- **By project**: leads born · fresh leads called same-day · older leads called
  (follow-up) · calls triggered · picked up · pickup % · median & average turns
  · WhatsApp sent · booked on call · Zoho booking rows · visits DONE. Every cell
  opens the leads behind it.

**Finding from the project panel:** pickup rate varies **2.2x between projects
on the same engine** — LOFT **59.0%**, BROADWAY 47.2%, SPECTRA 30.0%, LANDMARK
28.0%, LEGACY **26.7%**. But LEGACY books **75** on-call against LOFT's 20. The
project that answers least books most. Worth the dev team's attention: same bot,
same script, opposite outcomes.

## 7m. P1 EXPORT GATE — built, so it is a gate and not a habit
`export_gate()` scans any artefact bound for a human and **refuses the write** —
hard fail, not a warning — if it carries a superseded cost figure or a retracted
claim (7 patterns, keyed to RETRACTIONS). Wired into `exec_report.py`. Tested
both directions: clean text passes, `₹1.7/call` and "scheduler reads the wrong
database" are refused. This closes the kill-criterion that was still open.

## 7n. Cognitive-load pass — measured before and after
| | before | after |
|---|---|---|
| Sidebar | 22 flat items, 1 group | **5 groups of 5/4/5/3/5**, all 25 items kept |
| Accent-bordered notes | 52 (all of them) | explanatory notes quiet; accent reserved for `.note.warn` |
| Chart heights | 62–426px, 21 distinct | **110–322px, 17 distinct** |
| Positives tab | 18 cards visible | **7 visible**, 11 behind one control |
| Review / Intent | 13 / 13 visible | **6 / 8 visible** |
| Cards that can state what they count | 9 of ~79 | **84 of 84 (0 unresolved)** |

Nothing was deleted — folding is reversible in one click and states the count.

Collapsed rail fixed: group headings became hairline rules instead of vanishing,
so the five chunks survive without their words; rail 60px → 52px, icons centred.

## 7o. The naming gate caught its first failure — and it was the gate's own bug
On its first real run `assert_fields_resolve` reported all ten watched fields
absent. **False positive:** the label `"calls"` is used for both `angad.calls`
and `Intelligent_CRM.calls`, and it fired against the wrong one. Fixed to
self-scope on a signature column (`externalScheduleId`) rather than trust a
label. Re-tested three ways: real frame PASSES, wrong collection is IGNORED, a
genuinely all-null field is still CAUGHT. A gate that cries wolf teaches everyone
to ignore it, so this mattered more than the original catch would have.

## 7p. Lead card — RI vs NRI kept separate
The lead modal now shows calling metrics split by audience (dials, picked up,
pickup %, connected, booked, median turns, median talk) because Telnyx/NRI and
Plivo/RI are different worlds — IST night is the customer's day for one of them —
and pooling them inside a lead hides the only comparison that matters.

## 8. OPEN — carried forward, NOT done in this release
## 7q. "Still pending" — one page for everything left undone
Live counts, recomputed from the cache on every load rather than carried forward
from a note. Current state:

| | | |
|---|---|---|
| **SYSTEM** | Reminder cron runs that fired nothing | **990** |
| **THE GOAL** | Bookings with no attendance verdict | **158** |
| **DATA** | Key fields absent or 100% null | **3** |
| PIPELINE | Dialled but never once answered | 1,609 |
| PIPELINE | Leads never dialled | 238 |
| DATA | Source collections gone stale | 21 |
| MEASUREMENT | CIF indicators awaiting the judge | 14 |
| OUR DEBT | Prepared patches not applied | 3 |

Each row routes to the section holding the rows behind it.

## 7r. Database health — and a whole database has stopped being written
71 collections · **727,903 rows** · 21 stale sources · 3 dead key fields.

Per-collection: rows, size, **newest record in the SOURCE** (not our file mtime —
a source that stopped being written is invisible if you only check whether the
file is fresh), age, count of 100%-null columns, and a state flag.

**NEW FINDING — `Intelligent_CRM` is stale across the board:** `lead_events`
3.2d · `calls` 3.2d · `crm_leads` 3.2d · `persons` 3.2d · `mlid_registry` 3.2d ·
`messages` 5.1d · `site_visits` 5.1d. That is the event-sourced rebuild, and the
funnel's CRM-stage side reads from it — so every CRM-stage number is at least
three days behind, and the Zoho-vs-CRM disagreement figure is measuring partly
that lag rather than a real disagreement. Also long dead: `project_facts` 84d,
`workflow_queue` 53d, `crm_call_logs` and `crm_notes` 43d, `follow_up_log` 60d.

The key-field audit names the three fields the dashboard depends on that hold
nothing: `calls.sentiment`, `calls.closingScore`, `Zoho_Database.leads.site_visit_date`.

**Still open, and owned:**
- **[CTO] Rewrite `prepared_patches/02`** against the real cause (the cron fire
  condition). As written it patches the agent's planner and cannot fix the gap.
  `01` and `03` remain `git apply`-clean and unapplied — user applies, per
  imprint 9.
- **[PM] Attendance coverage 4.2%.** The sensor exists; it needs ops verdicts
  entered and a dev write-back field to stop being local.
- **[CI] CIF judge pass** — 14 indicators still NA. FMS switches on
  automatically once CDS/SVO/NAC land; the passthrough is built.
- **[CTO] Contract depth** — 84 of 84 cards answer, but only ~14 are FULL. The
  rest are PARTIAL: sourced, not yet verified against a query.
- **[Growth] Register the scheduled sync** — one command, user runs it.
- **[all] Visual verification.** The browser pane has never composited in this
  session. Every UI claim above rests on computed style, DOM structure and
  measured distributions — not on seeing the page. That is the standing caveat
  on this entire release.
- **CIF judge pass** for the 14 judge indicators — the passthrough is built and
  FMS switches on automatically once CDS/SVO/NAC land.
- **Validate the `qualified` sensor** before anyone quotes the 97-of-1,448 rate.
- **Fix the `call_date` gap** in the re-transcript import (null on 99.8% of
  rows) — it is why 252 of 308 substantive CIF calls sit in an "undated" era.

**Ask of the dev team, new:** confirm the Gemini tier and whether the 35k prompt
is context-cached. If it is not, caching it is the single largest cost lever
available and it is a configuration change, not a rewrite.
