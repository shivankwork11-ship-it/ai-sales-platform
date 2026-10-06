# FIELD CONTRACTS (P3 — the semantics gate)
*Every field entering an analysis gets one line HERE before first use. Not what
the field is called, not what it was intended to mean — what it was **verified**
to mean, by which query, on which date.*

*A field with no row here has no contract. Using it is a P3 breach: the CTO veto
is "this field has no verified contract."*

*Seeded 2026-08-08 from the Traps section of brain.md plus every semantics
retraction on record. Owner: CTO.*

| Field | What it ACTUALLY means | Verifying query | Date | Verifier |
|---|---|---|---|---|
| `angad.calls.reached` | **NOT "the line was answered."** True only for the answered-and-counted family (CONNECTED / NOT_INTERESTED / AUTO_CALLBACK / DNC_REQUESTED / PRE_SITE / VIRTUAL_TOUR). **False on every DROPPED and every VOICEMAIL row**, 5,428 of which opened a Gemini session. Use for "conversation counted", never for pickup or billing. | `groupby(outcome).agg(reached.sum(), (tok_total>0).sum())` | 2026-08-08 | CI |
| `angad.calls.duration_sec` | **Not proof of pickup.** 44% of NO_ANSWER rows carry a median 30s — that is ring time. Only trustworthy as talk time on rows that also opened a session. | `groupby(outcome)[(sec>0).mean(), median(sec)]` | 2026-08-08 | CI |
| `angad.calls.tok_total / tok_prompt / tok_response` | Exact per-call Gemini token counts. **99.4% of all tokens are prompt tokens**; corr(duration, tokens) = 0.12. Zero on 83.5% of dials — no session opens before pickup. | `corr(duration_sec, tok_total)`; `groupby(outcome)[(tok_total>0).mean()]` | 2026-08-08 | CI |
| `angad.calls.outcome = NO_ANSWER` | Partly **fabricated** by `cleanupStaleCalls` with no guard. Not a clean telephony signal. | dev team round 1 + 44%-with-ring-time check | 2026-08-08 | DS |
| `angad.calls.outcome = VOICEMAIL` | **Dual origin** — AMD detection OR a transcript regex. The two are indistinguishable in the data. 9,129 of 9,331 carry duration, 514 opened a session. | `groupby(outcome)` non-null profile | 2026-08-08 | DS |
| `angad.calls.sentiment`, `.closingScore` | **100% null across all 49,319 calls.** No quality score exists anywhere. This is a DATA problem for the dev team, not a scoring backlog. | `notna().sum()` = 0 | 2026-08-08 | DS |
| `angad.calls.language = 'hinglish'` | The **hardcoded default** when the classifier is skipped — not a measured language. | dev team round 2 | 2026-08-08 | DS |
| `angad.calls.summary` containing "classifier offline" | Classifier **failure**, not an unqualified lead. Never count as a negative outcome. | dev team round 2 | 2026-08-08 | DS |
| `angad.leads.propertyInterested = 'ASBL Loft'` | The **unspecified-project default**, not a stated preference. | dev team round 2 | 2026-08-08 | DS |
| `externalScheduleId` | `prd-{zoho_lead_id}-{epoch_ms}` on 81.4% of calls. Strip prefix AND trailing timestamp → 100% join to `Zoho_Database.leads.zoho_lead_id`. **Unique per dial → it is a DIAL key, not a lead key.** Prefixes: `prd-` relay cadence · `dash*` human · `sched-` agent poller. | uniqueness + join test on 39,965 relay dials | 2026-08-08 | CTO |
| `angad.calls.source` | **Unusable for attribution** — all `sched-` calls carry `source='zoho'`. Use the esid prefix instead. | prefix vs source crosstab | 2026-08-08 | CTO |
| `angad.calls.roomName` | `'pending-…'` on **52.7%** (unanswered Plivo). Any `call_sid == roomName` join silently drops exactly the failures. Join on esid. | null/pending share | 2026-08-08 | CTO |
| `ASBLVoiceBot.call_schedules.status = 'called'` | Set at **INSERT**, not at answer. Means "we queued it". Use `call_outcome` for what happened. | dev team log + value distribution | 2026-08-08 | DS |
| `ASBLVoiceBot.call_schedules.metadata.reason` | Only ever `fast_retry_*` / `daily_retry_*` across all 48,097 rows. **There is no reminder reason anywhere in the collection.** | `value_counts()` on 48,097 rows | 2026-08-08 | CI |
| `Zoho_Database.leads.stage` | **Not a maintained field.** Written only by `reconcile-stage-from-status` (2 runs, 21 Jul) and `backfill-presite-stage` (1 run, 20 Jul), neither scheduled. Null on 95–100% of everything born since 29 Jul. Prefer `prd_stage`, or show both. | null-rate by born-date + `cron_log` last-run per task | 2026-08-08 | DS |
| `Zoho_Database.leads.prd_stage` | The stage ANGAD's own relay maintains. Populated on ~all rows. Disagrees with `stage` on 50.6% of leads that carry both. | crosstab(stage, prd_stage) | 2026-08-08 | DS |
| `Zoho_Database.leads.visit_attended` | **Constant false.** Attendance exists ONLY in the ops sheets. | `value_counts()` | 2026-08-07 | DS |
| `Zoho_Database.leads.site_visit_date` | **Null on all 4,219 rows.** The reminder job cannot be reading this and finding anything. | `notna().sum()` = 0 | 2026-08-08 | CI |
| `Zoho_Database.site_visit_bookings.visit_date` | Real, parsed, populated on **41 of 59** rows, with `visit_raw.date` beside it. 6 dates still in the future. This is where visit dates actually live. | `notna()` + future-date count | 2026-08-08 | CI |
| `cron_log.result.fired_24h / fired_3h` | The reminder job's own fire counter. **Sum = 0 across 987 runs.** `errors_count` is also 0, so the job reports success while doing nothing. | `sum()` over task=`pre-site-visit-reminders` | 2026-08-08 | CI |
| `in_sync` (bot_settings) | **Meaningless** — the dev team confirmed it is a bug in their own check. Date the persona from transcripts instead. | dev team round 4 | 2026-08-08 | Growth |
| `retranscripts.call_date` | **Null on 3,853 of 3,862 rows.** Any per-day or era segmentation of that corpus is mostly "undated". Also: never build a dedup key by concatenating nullable columns — pandas propagates NA and collapses every such row to one. | `isna().sum()` | 2026-08-08 | DS |
| CIF `EBR` / `RDI` | v1.1 names. **v1.0 called them `DB` / `RD`.** A stale call site using the old names crashed the lead modal with `KeyError: 'DB'` for every lead that had a score. Read CIF codes defensively, never by bare subscript. | modal round-trip on a scored lead | 2026-08-08 | CTO |

---

## How to add a row
Before a field's first use in any analysis:
1. Run a query that distinguishes *what it is called* from *what it contains*.
2. Write the row: name · actual meaning · the query · the date · the role.
3. If the query cannot distinguish them, the field is **not contracted** and may
   not carry a claim.

## What this file is not
It is not a data dictionary. A dictionary records **intent** — what the field was
meant to hold. This records **verification** — what it was observed to hold, and
by which query. Every semantics retraction on record (R3) happened because intent
was trusted in place of verification.
