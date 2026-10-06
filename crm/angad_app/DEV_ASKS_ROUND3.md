# What we still need from the developer side — round 3
*2026-08-08, after their round-2 responses. Their AWS/log refusals are ACCEPTED —
the reasoning (a raw ECS env dump is a secrets dump; the log stream carries
customer phone numbers) is correct and matches our own PII rules. We are not
asking again. Everything below is either derivable-by-them-only, or a decision.
Signature: CI Team — citeam.claudemax@gmail.com*

## Already closed by round 2 — thank you, no action needed
- The three env vars that genuinely cannot be derived → we accept the proposed
  small `/api/effective-config` endpoint (booleans and numbers only, no secrets).
- Persona dating → **we solved it with your method and our data**: the agent
  names itself in the greeting, so we counted it. Aug 5: 0 Ankit / 210 Radhika ·
  Aug 6: 3 / 200 · **Aug 7: 602 Ankit / 11 Radhika**. Clean cutover on 7 Aug,
  matching your Mongo push at 08:47 IST. We are no longer relying on `in_sync`.
- `in_sync: false` → understood as a bug in your own check. Dropped from our board.

## A. The one that blocks everything else (product decision, not code)
**A1. Attendance capture.** Still the only number that matters and still
un-architected. We are not asking you to build a system — we are asking for a
decision on the smallest version: one endpoint or one field that the ops sheet
writes back to. Until it exists, every improvement any of us ships is
unfalsifiable. **Owner: product, not engineering.**

## B. The finding that reframes the 89% gap — please confirm or correct
**B1.** We probed the database directly: `angad.calls.extractedSlots` is
**empty** — budget / timeline / config / callback / objection = **0 rows across
48,306 calls**; `site_visit_requested` present on **109** (0.2%). Meanwhile the
same intelligence *does* exist in `ASBLVoiceBot.call_transcript_intent_identification`
(timeline 3,894 · objections 2,095 · budget 246 · site-visit outcome 3,894) and
in `lead_profiles`.

So the classifier runs and its output is persisted — **into the mirror, not onto
the Call document the planner reads**. `schedule-on-presite.ts` reads
`call.extractedSlots.site_visit_requested`, which is empty on 99.8% of calls.

**Question, precisely:** which write path is supposed to populate
`Call.extractedSlots`, and when did it last succeed? We are not asking "does the
classifier extract the date" — we accept that it does. We are asking **where it
lands**. If the answer is "it was never written back", then the visit-time
parser fix is cosmetic and the real fix is one write (or pointing the planner at
the intent collection).

## C. Cheap, high-value, already-coded — just needs turning on
**C1.** `TRANSCRIBE_POSTCALL` — accurate re-transcription exists and is off.
Now doubly relevant since your new Zoho Gemini-2.5-Pro pipeline shows the same
idea works. Any reason not to enable it on the agent side too, or is the Zoho
pipeline the intended replacement? (If the latter, say so and we will stop
asking.)
**C2.** The classifier prompt resolves relative dates for `preferred_callback_time`
but **not** for `site_visit_requested.date`. One instruction, same pattern.

## D. New Zoho transcription pipeline (from the 7 Aug WhatsApp thread)
**D1.** Confirm the collection name(s) and the write cadence — we have added
`call_transcripts`, `crm_call_logs`, `crm_notes`, `cron_log`, `call_throttle`,
`audit_logs` to our read-only sync. Correct us if the names are wrong.
**D2.** Is this pipeline now the **system of record** for stage transitions and
follow-ups, replacing the agent-side classifier? If yes, our funnel should read
stages from there, not from the agent.
**D3.** Your line — *"calling agent galat transcript bhejta tha isliye followups
misfire hote the"* — matches our finding exactly. Is there a backfill for the
period before the switch, or does the old data stay wrong?

## E. Retry policy — the biggest cost lever we can see
**E1.** 62.6% of ALL calls go to leads already dialled 20+ times: 805 leads,
30,255 calls, **3.9% connect** (vs 7.5% overall), 42 bookings, ~₹35,776. The mix
is BUSY / VOICEMAIL / NOT_CONNECTED — unreachable numbers being hammered. You
confirmed the agent has **no retry policy** (round 1, B9), so the relay is asking
for every one of these. **Who owns the relay's retry cadence, and is anyone
willing to cap it?** A cap at ~8 attempts would be the single biggest saving on
the board.

## F. Small persistence asks (each one line of code, each closes a blind spot)
| Field | Why |
|---|---|
| `prompt_version` on the Call doc | so a persona/prompt change is never invisible again |
| `outcomeSource` on `cleanupStaleCalls` writes | so fabricated NO_ANSWERs are separable from carrier truth |
| `hangupCause` | who ended the call and why — the most valuable missing field |
| rejected-`PRE_SITE` log line (visit gate) | so we can measure what the 4-minute gate costs |

## G. Things we no longer need
- Raw ECS env dump — withdrawn.
- Raw log excerpts — withdrawn.
- ECS revision history — withdrawn.
We will use your derivation queries instead and re-run them ourselves monthly.
