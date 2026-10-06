# ANGAD audit — round 3 responses

**From:** developer-side Claude, inside the ANGAD agent repo
**Date:** 2026-08-08
**Method:** live source at today's checkout + `git log` on the local mirror of the old repo history. No database access, no AWS, no portals.

Thank you for the persona-cutover numbers — that closed it cleanly, and Aug 7: 602/11 against the 08:47 IST push is a tighter confirmation than anything I could have produced from this side.

**Headline: B1 is the most important thing either of us has found, and I need to correct your inference before you act on it.** Your *observation* is right. Your *explanation* — "it lands in the mirror, not on the Call document" — is wrong, and the real cause changes what the fix is. Details in B1; everything else is shorter.

---

# A. Attendance capture

**A1. [decision — product, agreed]** Nothing to add technically; I have made the case twice and it is now Mukund's call, not mine. So just the two facts he needs to decide, and then I will stop raising it.

**Smallest viable version, costed honestly:** one authenticated `POST /api/site-visit/attended` taking `{phone, visit_date, attended: bool, source}`, writing three fields to `Lead` (`visitAttended`, `visitAttendedAt`, `visitAttendedSource`). **Roughly 30 lines plus a schema change.** Whoever maintains the ops sheet posts it once a day, or pastes into a form that posts it. No integration with Inncircles or Compass required to start — a daily human write is enough to make the funnel measurable, and it can be replaced by a real feed later without changing the read path.

**What it unlocks:** every question of the form "did that change help?" becomes answerable. Right now it is not answerable for anyone — you, me, or the owner.

I can write it whenever the decision lands. Staging first, then a PR, per the standing workflow.

---

# B. B1 — where `Call.extractedSlots` actually goes

## Verdict: your measurement is correct. Your explanation is not. The real cause is a skip, not a misdirected write.

### B1.1 — The write path exists and points at the right field

`plivo/media-stream.ts:1648-1676`, in teardown:

```js
await CallModel.updateOne({ _id: callDocId }, { $set: { ..., extractedSlots: mergedSlots, ... } });
```

where `mergedSlots = mergeExtractedSlots(persistedSlots, classification?.extracted_slots)` (line 1646). The Telnyx teardown is the same. **So there is no missing write and no wrong target.** The field is written on every completed call.

### B1.2 — The mirror is not a richer sink. It receives the *same* object, and slightly less of it

This is the part to correct. `mirrorCallFinalized` is called eleven lines later (`media-stream.ts:1748`) with:

```js
slots: classification?.extracted_slots ?? null,
```

Note it passes the **raw classifier output**, not `mergedSlots`. **So the mirror is fed from the identical source as the Call document, and is if anything strictly poorer** (it misses tool-written values that `mergeExtractedSlots` folds in). It is structurally impossible for `ASBLVoiceBot` to be richer than `angad.calls` for any call this agent wrote.

**And `call_transcript_intent_identification` is not ours at all.** I grepped the whole agent and dashboard for `intent_identification` and `call_transcript_intent`: **zero hits.** The complete list of collections this repo writes into `ASBLVoiceBot` is five, from `legacy-mirror.ts`:

`call_transcripts` (:240) · `lead_profiles` (:247) · `call_schedules` (:299, :418) · `call_recordings` (:346)

**`call_transcript_intent_identification` is written by a different system** — most likely the old Python agent, or your new Zoho Gemini-2.5-Pro pipeline (section D). That it holds timeline 3,894 / objections 2,095 while our field is empty is **not** evidence of a misrouted write in this agent. It is evidence of a second, independent extraction pipeline. Please do not point the planner at it on the assumption it is our output — it is somebody else's, on an unknown cadence, with unknown coverage.

### B1.3 — The actual cause: the classifier is *skipped* on almost every call

`plivo/media-stream.ts:1596-1601` — verified verbatim today:

```js
const needsSlotExtraction =
  preliminaryOutcome === 'PRE_SITE' ||
  preliminaryOutcome === 'VIRTUAL_TOUR' ||
  preliminaryOutcome === 'CALLBACK_REQUESTED';
const shouldClassify =
  (!preliminaryOutcome || needsSlotExtraction) && fullText.length > 0 && durationSeconds >= 10;
```

**If `preliminaryOutcome` is already set and is not one of those three, the classifier never runs.** `classification` stays `undefined`, `mergeExtractedSlots(undefined, undefined)` returns an object whose every field is `null`, and that all-null object is written to `extractedSlots`. The field is present; the values are null. That is exactly what "0 rows" looks like.

And `preliminaryOutcome` was set on nearly every connected call, because **the bot's own `hangup_call` tool sets it** (`toolOutcome`, line 1555).

### B1.4 — Three distinct data regimes. You are measuring across all of them as one.

This is the key to reconciling your numbers, and it is the thing I most want you to take away. `git log` on `agent/src/tools/registry.ts`:

| Regime | Dates | Tools on the sales slug | What populated `extractedSlots` |
|---|---|---|---|
| **1. Tools-write** | before **2026-07-16** (`be6176d`) | all: `save_qualification`, `schedule_site_visit`, `log_objection`, `save_buying_signal`, `send_whatsapp_details`, `hangup_call` | **Tools wrote fields directly mid-call** — `save-qualification.ts:103-115` does `$set: {'extractedSlots.budget_bucket': …}` etc. Classifier usually skipped (hangup_call set `toolOutcome`). |
| **2. Hangup-only — THE DEAD ZONE** | **2026-07-16 → 2026-08-07** | `hangup_call` only (v49 refactor moved everything post-call) | **Nothing.** The writing tools were removed, and `hangup_call` set `toolOutcome` → `preliminaryOutcome` → **classifier skipped** → all-null slots. The only survivors are `PRE_SITE` / `VIRTUAL_TOUR` / `CALLBACK_REQUESTED`, which force classification. |
| **3. Tools-off** | **2026-08-07 onward** (`3bdc50e`) | **none — `getToolsForSlug()` returns `[]`** for `lead_qualification_and_selling` and `ankit_male` (`registry.ts:48`) | No `hangup_call` → `toolOutcome` always null → `preliminaryOutcome` null on ordinary calls → **the classifier runs again.** |

**Regime 2 is a genuine, unintended data outage.** The v49 refactor's stated intent (`be6176d`) was *"qualification/objection/site-visit/whatsapp move post-call"* — the tools were removed on the understanding the classifier would pick the work up. **It did not, because the same commit left `hangup_call` in place, and `hangup_call` is precisely what short-circuits the classifier.** The two halves of the change were individually sound and mutually cancelling. That is the bug, and it is a good one — no single file review would have caught it.

### B1.5 — How this predicts your exact numbers

- **budget / timeline / config / objection = 0** → regime 2 covers the bulk of your 48,306 calls (July–August is the high-volume period at 697–1,444/day). In regime 2 nothing writes these fields at all. Regime 1 calls are a small minority, and the registry comment notes those tools carried a *"~1-2% fire-and-forget failure surface"* of their own.
- **`site_visit_requested` on 109** → `PRE_SITE` is one of the three outcomes that force classification, so bookings kept extracting throughout regime 2. 109 against ~176 bookings is the right order of magnitude, the shortfall being classifier failures and sub-10s calls.
- **callback = 0** → `CALLBACK_REQUESTED` *is* a real tool enum value (`hangup-call.ts:73`), so those calls did force classification. But the round-1 outcome snapshot showed **only 4 AUTO_CALLBACK outcomes in the whole system**. Near-zero is expected here and is not evidence of a bug.

### B1.6 — The falsifiable prediction, and the fix

**If I am right, the problem partly fixed itself on 7 August as an accidental side effect of switching tools off.** Test it:

```js
// Regime 3 — should be NON-ZERO if my explanation holds
db.calls.countDocuments({ 'extractedSlots.budget_bucket': {$ne: null}, createdAt: {$gte: ISODate('2026-08-07T03:17:00Z')} })
// Regime 2 — should be ZERO
db.calls.countDocuments({ 'extractedSlots.budget_bucket': {$ne: null},
  createdAt: {$gte: ISODate('2026-07-16'), $lt: ISODate('2026-08-07T03:17:00Z')} })
// Regime 1 — should be non-zero but sparse
db.calls.countDocuments({ 'extractedSlots.budget_bucket': {$ne: null}, createdAt: {$lt: ISODate('2026-07-16')} })
```

**If regime 3 comes back zero, I am wrong and you should tell me** — that would mean the classifier is failing for a different reason and I would want to look again at `fullText.length > 0 && durationSeconds >= 10`.

**Now, to your precise question — "is the visit-time parser fix cosmetic?"** No, but it is not sufficient either, and the ordering matters:

1. **The parser fix is still real** — `PRE_SITE` calls *did* keep classifying through regime 2, so bookings genuinely do arrive at the planner with a `site_visit_requested` object. The 89% failure is then the free-text date, exactly as diagnosed. Fixing it is not cosmetic.
2. **But first confirm `SITEVISIT_SCHEDULER_ENABLED`.** If it is off, no reminder is scheduled regardless of parsing, and the parser fix changes nothing. `db.scheduledcalls.countDocuments({kind:'sitevisit'})` settles it. **Check this before anyone spends effort on the parser.**
3. **The one-line fix that matters most is the `shouldClassify` gate itself** — it should not skip slot extraction just because the outcome is already known. Outcome resolution and slot extraction are different jobs and should not share a gate. Regime 3 masks this today, but the moment anyone re-enables `hangup_call` the outage returns silently.

**Recommended change, small and safe:** always run the classifier when there is a usable transcript, and keep using `preliminaryOutcome` to *override* the classifier's outcome (which the code already does at line 1620). Cost is one Gemini Flash text call per connected call — negligible against the ~35K-token prompt already billed on every dial.

**Do not** point the planner at `call_transcript_intent_identification`. It is another team's collection with unknown coverage and cadence, and you would be coupling the reminder path to a pipeline nobody in this repo controls.

---

# C. Already-coded, needs turning on

**C1. `TRANSCRIBE_POSTCALL`.** [live] No technical reason not to enable it — fully async, fires only once the recording URL exists, wrapped in try/catch, zero live-call impact. **Whether the Zoho pipeline is the intended replacement: UNKNOWN — I have no visibility into it (see D).** That is a Mukund question, and it is the right question: running two transcription pipelines over the same audio is duplicated spend for one answer.

**One caveat that matters more after B1:** enabling it will **not** improve outcomes or slots. Classification runs at teardown; re-transcription lands later and **never re-triggers classification**. So you get better readable transcripts and a better `fullText`, but the same extraction quality. If the goal is better extraction, the change is "re-run the classifier on `accurateTranscript` when it arrives" — which is a slightly bigger change and, in my view, the more valuable one. Enabling transcription alone is worth it, but expect readability, not accuracy in the data.

Also carry forward: it **overwrites `fullText` but not `transcript[]`**, so those two fields will disagree on newly processed calls. Check `transcriptSource` before comparing.

**C2. The relative-date asymmetry.** [live] Confirmed, and it is exactly one instruction. `classifier.ts:133` gives `preferred_callback_time` a full resolution rule — *"you MUST fill … as a concrete ISO 8601 datetime WITH the +05:30 offset, resolved relative to CURRENT_TIME_IST … Always resolve to the NEXT future occurrence"* — while `site_visit_requested` (`:113`) gets only a bare format hint `{"date": "YYYY-MM-DD", ...}` with no resolution rule and no reference to `CURRENT_TIME_IST`, **even though `CURRENT_TIME_IST` is already passed into every classifier call** (`:215`).

So the machinery is there and one field simply was not wired to it. The fix is a sibling rule in the same Rules block, e.g.:

> *SITE VISIT DATE — IMPORTANT: if the customer agrees to visit on any day ("Saturday", "kal", "this weekend", "Sunday morning"), you MUST resolve `site_visit_requested.date` to a concrete `YYYY-MM-DD` relative to CURRENT_TIME_IST, always the NEXT future occurrence. Never return a weekday name, a relative phrase, or null when the customer named a day.*

**This is a prompt-only change with no schema or code impact, and it is the highest value-per-line item on the board.** I can write it; it needs a staging test against real transcripts before prod, because classifier prompt edits can regress the outcome field.

---

# D. The new Zoho transcription pipeline

**D1, D2, D3 — UNKNOWN, all three, and I want to be clear this is a hard blind spot, not a soft one.**

I have no access to the 7 Aug WhatsApp thread, no access to the Zoho pipeline's repo, and **zero references to any of `call_transcripts` (as a Zoho-side collection), `crm_call_logs`, `crm_notes`, `cron_log`, `call_throttle`, or `audit_logs` exist in this codebase.** I cannot confirm or correct your collection names, the write cadence, whether it is now the system of record, or whether a backfill exists.

**One collision worth flagging before it bites you:** `call_transcripts` is **also** a collection this agent writes, in `ASBLVoiceBot` (`legacy-mirror.ts:240`, keyed on `call_sid`). If the Zoho pipeline writes a collection of the same name in a different database, you now have two same-named collections with different schemas and different writers. **Qualify it by database in every query and in your sync config**, or you will silently blend two pipelines.

On **D3** — the quoted line matches my independent finding exactly (round 1, H29: the code itself measures the Live side-transcript at <50% accuracy on 8kHz Indian multilingual audio, and the classifier runs on it). So the diagnosis is corroborated from two directions. **Whether there is a backfill: UNKNOWN, and it is the right thing to ask** — because if there is not, then all pre-switch extraction stays wrong, and combined with B1's regime-2 outage the practical position is that **structured intelligence before ~7 August is largely absent or unreliable.** I would treat that as a hard floor on any historical analysis rather than something to work around.

**Route D1–D3 to Mukund** — he owns that pipeline and can answer all three in a couple of lines.

---

# E. Retry policy

**E1. Who owns the relay's retry cadence: UNKNOWN** — not this repo, and I have no visibility into the relay's cron or campaign config. Confirming round 1, B9: **the agent has no retry policy whatsoever.** One `POST /api/schedule-call` produces exactly one dial. Those 30,255 calls to 805 leads are 30,255 separate requests the relay chose to send. **Route the ownership question to Mukund / the CRM team.**

Your numbers are the strongest single cost argument in the whole audit, and I have nothing to add to them except that they are consistent with everything I found on this side.

**One thing I can offer, and it is worth considering precisely because relay ownership is unclear:** the agent could enforce the cap defensively, without waiting for the relay. It already has the data and already has the exact place to put it — `POST /api/schedule-call` runs three suppression gates before dialling (DNC at `routes.ts:201`, visit-scheduled at `:232`, night-defer at `:256`), and this would be a fourth in the same style:

```js
// count prior dials to this phone; if >= MAX_ATTEMPTS_PER_LEAD and none ever connected,
// return 200 { success:false, error_code:'ATTEMPT_CAP_REACHED' }
```

Returning HTTP 200 with `success:false` is the established pattern here — the relay treats it as handled rather than retrying a 5xx. **Env-gated with the cap as a variable** (`MAX_ATTEMPTS_PER_LEAD`, unset = off) so it ships dark and can be tuned without a deploy, consistent with this codebase's convention.

**Two honest caveats.** First, it is a **backstop, not a fix** — the relay would still generate the requests and still burn its own cycles; only the telephony and Gemini spend is saved. The real fix is upstream. Second, **suppressed requests write no row** (round 1, B8), so switching this on will make your dial counts drop in a way that looks like a volume collapse. Log the suppressions or you will be debugging your own dashboard in a month.

**~25 lines. Not building it unless Mukund says go** — a cap that silently stops calling real leads is a business decision, not an engineering one.

---

# F. Small persistence asks

All four are genuinely small and I have checked each against the code. Assessment, then a single recommendation.

| # | Field | Feasibility | Where |
|---|---|---|---|
| **F1** | `prompt_version` on the Call doc | **~5 lines.** The Mongo `fileVersion` is already fetched by `prompt-store` when the session is built; stamp it onto the Call doc at creation. Use the **Mongo** value, not the code constant — Mongo is what actually ran (that is the whole `in_sync:false` lesson). | `Call.ts` schema + `gemini/client.ts` or the media-stream call-doc write |
| **F2** | `outcomeSource` on `cleanupStaleCalls` writes | **1 line**, the easiest win here. Add `outcomeSource:'cleanup-inferred'` to the existing `$set` at `index.ts:748-754`, and `'carrier'` / `'classifier'` / `'tool'` at the other three writers. Makes fabricated `NO_ANSWER`s separable forever. | `index.ts:748`, `telnyx/webhook.ts:167`, `media-stream.ts:1683` |
| **F3** | `hangupCause` | **~3 lines.** Both providers already parse it and log it — Plivo has `HangupCause`/`HangupCauseName` in scope at `index.ts:170-174`, Telnyx has `hangup_cause` at `webhook.ts:157`. It is thrown away one line later. Purely additive. | same two files + schema |
| **F4** | Rejected-`PRE_SITE` visit-gate record | **~3 lines.** The rejection branch at `media-stream.ts:896-913` already has `callDocId`, `customerTurns` and `callAge` in scope. Persist rather than log — `$inc: {visitGateRejections: 1}` plus a first-rejection timestamp — so it is queryable instead of buried in CloudWatch. | `media-stream.ts:896` |

**All four are additive schema fields with no read-path changes and no behavioural risk** — nothing existing reads them, so nothing can break. F2 and F3 in particular close blind spots that currently make two of your metrics uninterpretable.

**Recommendation: ship F1–F4 as one small PR, separately from anything behavioural.** Staging first, then prod through the normal flow. **I can write it today — the ship decision is Mukund's, not mine.** I would keep the B1 `shouldClassify` change and the C2 prompt edit in their own PRs, because those two alter behaviour and need real-call verification on staging, whereas F is inert.

---

# G. Withdrawn items

Acknowledged — raw ECS env dump, raw log excerpts, and ECS revision history are all off the list. The derivation queries in round 2 §1b are yours to re-run monthly; they read only `calls`, `scheduledcalls` and `dashboard_snapshots` and touch no customer fields.

---

# Confidence

**Verified today, high confidence:** B1.1–B1.4 (write path, mirror parity, `getToolsForSlug` returning `[]`, the three regimes and their commit dates), C2 (the asymmetry, quoted verbatim), F1–F4 feasibility, and that `call_transcript_intent_identification` is not written by this repo.

**Inference, flagged as such:** B1.5 — mapping the three regimes onto your specific counts. The mechanism is certain; the *proportions* are my reasoning about which regime dominates your 48,306 rows, and the three queries in B1.6 will confirm or refute it in about a minute. **Regime 3 returning zero would falsify me.**

**UNKNOWN, routed to Mukund:** all of D, ownership in E1, and the A1 decision.

---

# Redaction confirmation

Re-scanned before sending. **No phone numbers** — the only digit strings are line numbers, dates, commit hashes, and the counts you supplied. **No `@` signs, no email addresses.** **No credentials** — env vars named only, no values. **No connection strings or hostnames** beyond collection and field names. **No customer data was read, exported, or moved:** this round used source code, local read-only `git` commands, and nothing else — I did not connect to any database.
