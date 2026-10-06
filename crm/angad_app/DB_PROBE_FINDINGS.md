# DB PROBE — what the database says, not what anyone claims
*2026-08-08. Every line measured directly against the read-only cache. Where a
first reading was wrong, the correction is kept visible.
Signature: CI Team — citeam.claudemax@gmail.com*

## FINDING 1 (critical) — the scheduler reads the wrong database

| Where | budget | timeline | objections | site-visit | config | callback |
|---|---:|---:|---:|---:|---:|---:|
| `angad.calls.extractedSlots` (what the planner reads) | **0** | **0** | **0** | **109** | **0** | **0** |
| `ASBLVoiceBot.call_transcript_intent_identification` | 246 | 3,894 | 2,095 | 3,894 | 485 | — |
| `ASBLVoiceBot.lead_profiles` | 354 | 319 | — | 54 | — | — |

Base: 48,306 calls / 21,654 intent rows / 4,355 profiles.

**First reading (WRONG, corrected same session):** "the classifier writes nothing."
**Correct reading:** the classifier runs and its output *is* persisted — into the
mirror database. The site-visit planner reads
`call.extractedSlots.site_visit_requested` on the **runtime** database, which is
empty on **99.8%** of calls.

**Consequence:** the 89% no-reminder gap is not a date-parsing edge case. The
scheduler cannot see data that already exists one database away. The prepared
parser patch (`prepared_patches/01`) is still useful but is **not** the root fix;
the root fix is writing the classifier result back onto the Call document, or
pointing the planner at the intent collection.

**Ask the dev team:** which write path is supposed to populate
`Call.extractedSlots`, and when did it last succeed? Do not accept "the
classifier extracts it" — that is true and irrelevant; ask where it *lands*.

## FINDING 2 — fields that exist but are never populated
`sentiment`, `closingScore`, `eventSlug` — 100% null across 48,306 calls. An
analyst will assume a quality score exists. It does not. (Matches the dev's own
J18 disclosure — confirmed true in data.)

## FINDING 3 — cached collections nobody has looked at yet
`lead_journeys` (4,394) · `follow_up_log` (402) · `whatsapp_sender_map` (1,046)
· `project_facts` (5) · `project_documents` (63) · `bot_settings` (10) ·
`learninginsights` (4) · `prompts` (9) · `stale_person_record` (572) ·
`dashboard_snapshots` (1) · `user_profiles` (138) · `workflow_queue` (2).
`angad.prompts` is worth reading first — it may date the persona swap.

## FINDING 4 — a stale input was starving the numbers
`Downloads/Daily angad performance/` (the folder the Zoho importer watches)
does not exist, so `Leads_2026_08_07.csv` sitting in `Downloads/` was never
imported. Imported manually this session: 4,174 rows, 4,168 joinable.
**Runbook fix:** keep exports in the watched folder, or pass the path.

## Probe method (repeatable)
1. List cached collections with row counts; flag any never referenced by the dashboard.
2. For every column on the main call table: count non-null and count meaningful (non-blank, non-'nan').
3. When a field is empty, search every other collection for the same concept before concluding it is missing.
4. State the first reading and the correction — a wrong first reading that is
   silently replaced is how a dashboard starts lying.
