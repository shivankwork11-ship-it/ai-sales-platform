# ANGAD — round 3 request to the developer side

**From:** the CI/analytics team (read-only Mongo access, local audit dashboard)
**Date:** 2026-08-08
**Replaces:** all earlier question lists. Rounds 1 and 2 were answered well — this
document does not re-ask what you already answered. It asks for the things only
you can give us, and above all for **definitions**.

---

## 0. READ FIRST

### 0.1 What we will NOT ask you for, ever

Your round-2 refusals were correct and we are adopting them as standing policy.
**Do not send us any of the following, even if a question below seems to invite it:**

- No credentials of any kind — API keys, tokens, passwords, webhook secrets,
  connection strings, or a raw environment dump. A raw env block is a secrets
  blob; hand-redacting one is exactly how a leak happens.
- No customer personal data — no phone numbers, names, emails, or addresses.
  Not one example, not partially masked.
- No raw production log excerpts. You showed us the log stream carries customer
  numbers on adjacent lines (`plivo/outbound.ts:93`, `telnyx/outbound.ts:111`,
  `plivo/media-stream.ts:1114`). Scrubbing that by hand is a PII export with a
  manual step in the middle. We do not want it.
- No account identifiers, invoices, billing account numbers, or AWS account ids.
- No customer data exported, copied, or moved anywhere in order to answer this.

**Where a value is a secret, the only acceptable answer is "set" or "not set".**
Field *names*, counts, booleans, enum lists, and structural facts are what we
need — and they are enough.

If any question below looks like it needs one of the above to answer, that is our
drafting error: say so and answer the safe half.

### 0.2 How to answer

- Answer from live code, live git, or a live endpoint — not memory. Tag every
  answer `[live]`, `[docs]`, or `[UNKNOWN]`, and cite `file:line`, a commit, or
  the endpoint you probed.
- "UNKNOWN" is a complete and welcome answer. A guess dressed as a fact is the
  one thing that hurts us, because we build on it.
- Where something is not yours to know (relay, CRM, ops, provider portal), say
  **who owns it** so we can route the question instead of chasing it.
- Plain language. Assume the reader has not read your code.
- Return **one markdown document** using these section numbers.

### 0.3 What we already accept as settled — do not re-answer

From rounds 1 and 2, we treat these as verified and closed:

- `GREETING_PROTECT_MS = 7000` is a compile-time constant in both provider
  paths, not env-tunable; its purpose is barge-in dead-air prevention, not
  latency. `VISIT_GATE_MIN_MS`, `VISIT_GATE_MIN_TURNS` and
  `CUSTOMER_SILENCE_MS` **are** env-tunable.
- `angad` and `ASBLVoiceBot` are necessarily on the same cluster (`useDb` on one
  connection).
- The agent has no retry policy and no volume control; re-dial cadence and
  campaign pacing are upstream.
- Attendance is un-architected on the agent side: no field, no endpoint, no
  inbound path.
- Unanswered Plivo calls keep `roomName = "pending-<uuid>"`; join on
  `externalScheduleId` instead.
- `cleanupStaleCalls` fabricates `NO_ANSWER`; `VOICEMAIL` has two origins;
  `"classifier offline"` in `summary` marks all-null slots; `hinglish` is a
  silent default; `propertyInterested` defaults to a literal project string.
- The Bitbucket token never entered git history on any ref (local working tree
  only). Rotation is hygiene, not an incident.
- The prompt collection keeps one document per slug and Mongo beats deployed
  code at runtime.

### 0.4 Corrections from our side — we were wrong about two things

In fairness, before we ask you anything:

1. **There was no Aug-7 volume collapse.** The "75 dials" and our own "343" were
   both mid-sync snapshots. After a full re-sync, **Aug 7 = 2,390 dials — the
   busiest day since Jul 30.** Please disregard every question we sent about a
   volume drop. We still ask about campaign pacing below, but not as an incident.
2. **The night-dialling exposure is much smaller than we said.** Splitting
   out-of-window dials (21:00–06:00 IST) by provider gives **Telnyx 7,688 vs
   Plivo 47**. The bulk are NRI numbers dialled during their own local daytime.
   The `+91` exposure is on the order of ~47 dials, and the night guard is
   demonstrably live (5,077 `night_defer` rows, still firing on Aug 7–8). Our
   earlier "10.3k TRAI exposure" framing was wrong and we have retracted it
   internally.

---

## 1. THE MAIN ASK — a collection and field dictionary

This is the core of round 3 and the thing we most need. We read four databases
and roughly 190,000 documents, and for a large fraction of the fields we are
**guessing** at the meaning. Every guess becomes a number on a dashboard that
someone makes a decision from.

**Format we would like, one row per field:**

| field | who writes it | at which moment | type / allowed values | is it authoritative? | gotchas |

If a whole collection is dead or abandoned, one line saying so is a complete
answer and saves us weeks.

### 1.1 `angad.calls` — the system of record

**(a) The extracted-slot family — highest priority.**
Fields: `extractedSlots.site_visit_requested.date`, `.slot`,
`extractedSlots.site_visit_date`, `extractedSlots.budget`, `.timeline`,
`.objections`/`objection_raised`, `.configuration_interest`,
`.preferred_callback_time`.

What we measured on our cache (49,116 calls) and cannot explain:

| slot | non-null |
|---|---|
| site_visit | 109 (0.2%) — but **100 of the 154 PRE_SITE calls have it** |
| intent | 324 |
| budget, timeline, callback, config, objection | **0 — all of them, across every call** |

Meanwhile `ASBLVoiceBot.call_transcript_intent_identification` holds the same
kind of intelligence at volume (timeline ~3,894, objections ~2,095, budget ~246).

**Questions:** Which write path populates `extractedSlots` on `angad.calls`, and
is it expected to carry budget/timeline/objections at all — or are those
deliberately only in the intent collection? Is the classifier writing them and
something dropping them, or does it never write them? (Note our own caveat: our
cache flattens these columns, so a flattening bug on our side is possible —
please tell us what *should* be there and we will re-verify.)

**Why it matters:** the site-visit planner reads
`call.extractedSlots.site_visit_requested` on the runtime database. If that
object is thin, the "89% of bookings get no reminder" problem is not only a
date-parsing edge case, and our prepared parser patch is not the root fix.

**(b) The date contract.** `site_visit_requested.date` is a schema `String` with
no validation. What is the accepted format, what should happen on an
unparseable value, and — the asymmetry we keep coming back to — why does
`preferred_callback_time` get a "resolve relative dates against
CURRENT_TIME_IST" instruction in the classifier prompt while
`site_visit_requested.date` does not? Is closing that asymmetry acceptable?

**(c) Outcome and status vocabulary.** The authoritative meaning of all 12
`outcome` values and all 4 `status` values: who sets each, in which code path,
whether it is terminal, and whether it can be overwritten afterwards. Please
include the fabricated-`NO_ANSWER` case explicitly so we can document it.

**(d) Classification and provenance.** `source` (`zoho` vs `goodcalls` vs the
never-assigned `test`), `slug`, `agentPersonaId`, `eventSlug` (100% null in our
cache — dead?), `voiceEngine`, `transcriptSource`, `accurateTranscript`,
`fullText` vs `transcript[]` — and the rule for when the last two legitimately
disagree on the same document.

**(e) Fields that exist but are never populated.** We measured `sentiment` and
`closingScore` as 100% null; you named `objectionsHandled` and
`handoffTriggered` too. Please confirm each is permanently unpopulated and say
whether it will be implemented or removed. An analyst will reasonably assume a
quality score exists.

**(f) Webhook and identity metadata** — the fields we do not yet sync and want
to: `webhookDeliveries[]` (`event_id`, `event_type`, `status`, `attempts`,
`lastStatusCode`, `lastError`, `sentAt`), `callbackUrl`,
`metadata.source_lead_id`, `metadata.zoho_lead_id`, `metadata.project`,
`metadata.venue`, `metadata.party_size`, `metadata.visit_notes`,
`metadata.contact_number`. Are these stable enough to build on?

**(g) `tokenUsage.*`** — confirm what each of the eight counters bills as, and
which maps to which invoice line, so we can multiply exact counts by
finance-confirmed rates instead of documented estimates.

**(h) Missing fields you yourself proposed.** `hangupCause`, AMD result,
per-call latency, prompt version, provider, and an `outcomeSource` enum
(`carrier` | `classifier` | `cleanup` | `tool`). For each: will it be added, and
if so, when? These six would close more of our uncertainty than anything else in
the schema.

### 1.2 `angad.scheduledcalls`

Define every `kind` value (we see `night_defer` 5,077 and `sitevisit` 30 — what
else exists?) and every `status`/`sched_status` value. Please state in writing
that `status='done'` means *the reminder call was answered*, not that a customer
confirmed anything — we need that quotable.

**And the sharpest single question in this document:**

> Only **30** `sitevisit` rows exist in total, and the most recent has
> `dueAt = 2026-07-27`. Nothing since. Meanwhile 154 bookings exist, and the
> `night_defer` writer in the same process is still producing rows daily
> (Aug 7 and Aug 8). **So the process is alive but the site-visit scheduler has
> produced nothing for twelve days.** Is `SITEVISIT_SCHEDULER_ENABLED` off, did
> something change around Jul 27, or has every booking since then failed to
> parse? This decides whether the fix is a flag, a deploy, or a parser.

### 1.3 `angad.leads`

Which fields are actually written vs schema-only (`email`, `income`, `timeline`,
`leadScore`, `notes`, `source` all look unwritten). Exact suppression semantics
of `status='visit-scheduled'` and a future `siteVisitScheduledAt`. Confirmation
that `propertyInterested` is single-valued, overwritten, and defaults to a
literal project name — so it must never be read as expressed interest.

### 1.4 `angad.prompts`, `angad.bot_settings`, `angad.callstates`, `angad.webhooks`

- **prompts:** the full slug list — which are live, which are dead-but-dialable,
  which are staging-only. (We found a staging-looking male slug in prod
  `scheduledcalls`; please confirm whether staging slugs can reach prod.)
- **bot_settings:** which keys are live runtime switches vs vestigial, and which
  can override code-level behaviour. The persona swap arrived Mongo-over-code,
  so this collection can silently change production; we need to know its surface.
- **callstates:** confirm the 2-hour TTL and that the AMD flag is therefore
  never durable.
- **webhooks:** which consumer each row targets after the relay cutover, and
  whether `roomName` is ever upgraded on the hangup webhook (today the
  `pending-` form is never upgraded, which is what produces our 52.7% join bias).

### 1.5 `ASBLVoiceBot` — the legacy mirror database

We sync it and barely understand it. Please give us **one line per collection**:
what it is, who writes it, live or abandoned.

Specifically: `call_schedules` (47,099 rows — confirm it is a historical dial
log, not a queue; define `status` vs `call_outcome`; name its external
consumers), `call_transcripts` (22,230), `call_transcript_intent_identification`
(21,654 — **this is currently our primary identity bridge**, so we need its full
field dictionary, its writer, its write moment, and the provenance of
`master_lead_id`), `lead_profiles` (4,355 — is `site_visit_agreed` a booking or
an inclination? why is there no `visitAt`?), `lead_journeys` (4,394),
`call_recordings` (19,667), `kb_store`, `follow_up_log`, `whatsapp_sender_map`,
`stale_person_record`, `user_profiles`, `workflow_queue`, `learninginsights`,
`bot_settings`, `dashboard_snapshots`, `project_facts`, `project_documents`.

### 1.6 `Intelligent_CRM`

The complete event-type vocabulary and stage vocabulary for `lead_events`
(28,669 rows — we expose this as per-lead history), what a `site_visits` row
represents and who writes it, and which dials are *expected* to arrive here at
all. Today ~88% never arrive and we cannot distinguish suppression from loss.

### 1.7 `Zoho_Database`

`leads` / `zleads` / `reactivation_list`: is `visit_attended` deprecated (it is
constantly false)? Semantics and refresh cadence of the web-behaviour fields we
surface as Web History (`first_page_visited`, `last_page_visited`,
`time_spent_minutes`, `total_page_views`). Is `Modified_Time` meaningless
because hourly crons touch every row? And the authoritative direction of
`reactivation_list`'s `lead_ref → masterleadid` mapping, which currently wins on
conflict in our joins.

Also the canonical value lists for lead source, UTM, campaign, and funnel
stage — and which system is the authoritative writer of STAGE. We currently
surface **1,061 leads sitting in different funnel bands** between Zoho and
Intelligent_CRM; we show the disagreement rather than averaging it, but the root
fix needs the owner of the stage writers.

### 1.8 The new Zoho-side pipeline (created 2026-08-07 night)

Six collections appeared that nobody told us about: `call_transcripts`,
`crm_call_logs`, `crm_notes`, `cron_log`, `call_throttle`, `audit_logs`.

Per collection: writer, schedule, model used, retention, and whether output
flows back into `angad.calls` — or whether it duplicates/replaces
`TRANSCRIBE_POSTCALL`. **`call_throttle` especially:** if that gates dial
volume, it is a control surface we have never been told about and it changes how
we read every volume metric.

### 1.9 The identity spine

Define each keyspace and the intended join path: `MASTERLEADID` (Inncircles
6-digit) — what assigns it, when, can one human hold two, what is the
authoritative mapping collection; `source_lead_id`; `zoho_lead_id`;
`external_schedule_id`; `external_customer_id`; `lead_ref`/phone; `roomName`.

Two concrete findings to react to:

- **`ASBLVoiceBot.call_schedules.source_lead_id` has 87.8% coverage** going back
  to 2026-05-11, versus our inferred MASTERLEADID bridge at 71%. Is that the
  intended first-party join? Should we re-base on it?
- **The `esid` prefix convention is load-bearing for us** — we read `dash*` as
  human-dispatched, `sched-` as poller, `prd-` as relay. Please confirm and give
  us the **full** prefix list; we also see `LQ-ro`, `resub`, `manua`, `prodt`,
  `wa-ca` and blanks.

### 1.10 The field that does not exist yet — attendance

We need its name, type, allowed values, and write path. This is the single most
important undefined field in the system, and section 3.1 is about it.

---

## 2. QUESTIONS ONLY THE UPSTREAM SIDE CAN ANSWER

You have already told us these are not yours. We are not asking you to answer
them — we are asking you to **name the owner** so we can route each one.

1. **The relay (`growth-relay.asbl.in`)**: what calls it, what it forwards,
   where it logs delivery failures, whether it feeds `Intelligent_CRM`, and its
   per-day dial-**request** and **suppression** counts. Suppressed requests
   write no row on your side, so the true ask count exists only there.
2. **Why `Intelligent_CRM` misses ~88% of dials** (and ~66% of booked calls);
   whether it is live, frozen, or deprecated.
3. **Who owns re-dial cadence.** We measure **62.6% of all calls (30,255 calls
   across 805 leads) going to leads already dialled 20+ times**, connecting at
   3.9% vs 7.5% overall. Since the agent has no retry policy, this entire
   pattern is an upstream decision that nobody has yet claimed. Is there a cap?
   What stops the loop? Given cost is near-flat per dial, this is the single
   biggest cost lever in the system.
4. **Zoho lead ingest**: push/poll/webhook/CSV, frequency, what dedupes,
   what creates a person×project row, what populates `reactivation_list`.
5. **Does Zoho actually enforce calling hours?** `limits.ts` states in writing
   that compliance is delegated to the CRM. If the CRM is not enforcing it
   either, then nobody is. We would like that answered in writing by whoever
   owns it — not because the numbers are alarming (they are not, see 0.4) but
   because "delegated to X" should mean X knows.
6. **Which system sent the ~28,000 undelivered WhatsApp messages?** It is not
   the agent — `whatsappSent` is true on only 58 calls ever, and a 401 cannot
   set that flag. Name the collection and the code path. If a second WhatsApp
   sender exists in the estate, our hot-reply queue (75 leads who asked to be
   called) may be sitting on a broken channel.
7. **Is any process still holding a Mongo connection to the old cluster or
   growth-test** — a stale task revision, an old service, a dev machine, a copy
   job? A live second writer would silently split our history.
8. **The ops dashboard (`web/`)** is a second writer to the same databases. What
   exactly does it write, who can use it, and is there an audit trail of who
   dispatched what? It is the confirmed origin of 20 of our 25 zombie
   confirmation-slug calls.

---

## 3. THINGS WE WOULD LIKE BUILT — and our answer to your two offers

### 3.1 Attendance capture — the one that decides everything

The stated goal of this system is site visits **attended**. Nothing anywhere
records attendance, and nothing can receive it. Every metric either of us
produces is measured against **bookings** — a proxy one step removed from the
goal, and one a pushy agent can inflate without moving the real number.

We are not asking you to decide this. We are asking:

- Does anyone (site ops, Compass, Inncircles, a security register, a sales app)
  record check-ins digitally **today**? If yes: which system, which key?
- If not, who owns building the one field or one endpoint, and by when?
- **We will bring the join keys.** A daily ops-sheet write-back to a single
  `Lead` field is enough to start. It does not need to be a project.

### 3.2 Your two offers — yes to both, and here is why

You offered a **`GET /api/config-summary`** endpoint (booleans and set/not-set
only, no secret ever emitted, works identically on staging) and a **`latency`
sub-document on `Call`** (six numbers the agent already computes and discards).

**Both are exactly right, and they are better than what we originally asked
for.** The config endpoint permanently solves the env-visibility problem without
anyone handling a secrets blob, and it lets us diff prod against stage
ourselves. The latency document gives us full-population coverage instead of
five sampled log lines, with no PII and no log access.

We understand the ship decision is not yours. Please treat this as our formal
"yes, we want them" so it can go to whoever decides, and tell us what you need
from us to make that easy.

**One addition we would ask for in the same PR if it is cheap:** log or persist
one line per **rejected** `PRE_SITE` attempt (visit-gate refusals). Today a
genuine booking on a fast sub-4-minute call is blocked and leaves no trace at
all, so the cost of that guardrail is unmeasurable by construction. Our
human-call corpus shows the opposite pattern wins — the best human closers pivot
to the visit **early** with logistics (24% win vs 7% stall). We are not asking
you to change the threshold; we are asking to be able to measure it before
anyone does.

### 3.3 Status and dates for the small fixes

For each of the following, tell us: done / scheduled (with a date) / rejected
(with a reason). Several are one line or one flag.

1. `TRANSCRIBE_POSTCALL` enabled — **and separately**, re-running classification
   on the accurate transcript. You flagged that enabling transcription alone
   will not retroactively improve outcomes, which we think makes the second half
   the more valuable one.
2. The classifier prompt rule giving `site_visit_requested.date` the same
   "resolve to the next future occurrence against `CURRENT_TIME_IST`" treatment
   that `preferred_callback_time` already has.
3. Upgrading `pending-` `roomName` on the hangup webhook (fixes our 52.7% join
   bias at the source).
4. `outcomeSource` on `cleanupStaleCalls` writes.
5. Persisting `hangupCause`, AMD result, provider, and prompt version on `Call`.
6. Retiring or gating the `site_visit_confirmation` slug. Your recommendation to
   **gate rather than delete** is the right one — a gate returns a clean error
   without breaking any relay still passing the slug.
7. Fixing the `"nothing to schedule (visit past/imminent)"` log line, which
   names the wrong cause and misdirects anyone debugging the reminder gap.
8. Telugu / Tamil / Kannada DNC patterns. Your framing is ours too: this is a
   live compliance gap, not a metrics artefact, and it is a small change.
9. `CALL_WINDOW` on the prod task — **downgraded in priority** given our
   correction in 0.4, but still the one change that closes every dial path at
   once, including the `/api/calls/initiate` bypass.

---

## 4. CONFIRM OR CORRECT — our working assumptions

Short answers are fine. Each of these is currently a caveat on a dashboard tile;
a yes/no from you either removes the caveat or corrects a number.

1. `status='in-progress'` is the most reliable "a human picked up" signal.
2. Every accepted `/api/schedule-call` writes a `Call` row **before** dialling,
   so BUSY/NO_ANSWER/NOT_CONNECTED rows are real dials — while DNC-suppressed,
   visit-scheduled-suppressed, invalid-phone, unknown-slug and rate-limited
   requests write **nothing**.
3. Provider is recoverable as: `phoneNumber` starts `+91` → Plivo, else Telnyx —
   because that is literally the routing rule, not a proxy.
4. `call_schedules.status` is set to `'called'` at insert before the phone
   rings, so `call_outcome` is the field to read.
5. Recording exists only for answered calls, by construction.
6. Every legacy-mirror write is inside a silent catch, so `ASBLVoiceBot` can be
   incomplete relative to `angad` with no error anywhere.
7. **Webhook health, measured on our side:** 28,430 calls (58.9%) never had a
   webhook sent — all `status=failed`, i.e. never-connected dials, which we read
   as correct by design. **All 154 booked calls did fire one, but 15 failed
   delivery.** Is ~10% webhook failure on bookings expected, and does a
   permanent 4xx there mean those bookings are simply lost downstream?
8. **Persona crossover, measured on our side:** zero transcripts name "Ankit"
   before Aug 6; 3 on Aug 6; **601 on Aug 7, alongside 645 still naming
   Radhika**. That matches your `mongo_updated_at` of Aug 7 08:47 IST and
   confirms a mid-day switch. We will split every analysis at that boundary —
   please confirm no earlier undocumented persona changes exist.
9. **134 of 154 booked calls have no `siteOfficeSentAt`** (87%). Using your
   proxy, that suggests the site-office WhatsApp is failing or not being
   attempted on the large majority of bookings. Is that reading right?
10. The 5 poller-prefixed confirmation-slug calls after Jul 17 (of 25 total, 20
    dashboard-human): were those queued rows created before the +30m removal, or
    can the poller still emit that slug? Your code reading said it cannot, so
    these 5 contradict it.

---

## 5. FOR THE HUMANS — please just name the owner

We are not asking you to chase these; we will. Just tell us who to ask.

- **Prompt governance:** both prompt texts (the pre-Aug-5 Radhika line and the
  current Ankit line), plain text, any commercial numbers redacted; whether the
  swap is permanent, an A/B, or a rollback candidate; who approves a push; and a
  dated change log of every prompt/model/voice change since 2026-06-01. Without
  a dated list, every before/after comparison silently mixes two different
  agents.
- **Recording retention** on both telephony accounts, and whether older Telnyx
  URLs (the 10-minute presigned era) are permanently dead. Recordings are the
  only ground truth for the transcript-accuracy problem — if a retention window
  is quietly expiring, the evidence base for every conversation metric goes with
  it. Can we get a bulk export before it closes?
- **Rates only, from finance** — per-minute telephony and the Gemini Live
  pricing lines, plus the USD→INR rate in use. **No invoices, no account
  numbers.** We have exact per-call token counts already; we only lack the
  multipliers. Also: is the $20/day cap on the Telnyx outbound profile still
  set, and has it ever been hit? That is a silent throughput ceiling on
  international dials, not just a cost question.
- **WhatsApp:** is any sender in the pool alive today?
- **CloudWatch:** log-group name and retention setting only — no log contents.
  We want to know how long the `[timing]` reports survive, because that decides
  whether the latency sub-document in 3.2 is urgent or merely useful.
- **Security:** has the Bitbucket token been rotated, and what was decided about
  the unauthenticated `/health`, `/api/health` and `/api/prompt-version`
  endpoints? Your assessment that `/health` is harmless and load-bearing reads
  right to us; we raise it only to close the loop.
- **The visit gate:** were 4 minutes and 11 turns deliberate values, or a first
  guess?

---

## 6. HOW TO CLOSE

Please end your document with:

- **Sourcing:** every answer tagged `[live]` / `[docs]` / `[UNKNOWN]`.
- **Least-confident list:** the answers you would least want us to build on.
- **Ownership routing:** for everything outside your repo, who owns it.
- **A redaction confirmation:** that you re-scanned for phone numbers, emails,
  credentials, and account identifiers — and that no customer data was exported,
  copied, or moved in preparing the answers.
- **Your own log:** the surprises, the places code and reality disagreed, and
  the dead ends. Round 1's work log was genuinely as useful as the answers.

We verify every claim against the live database on our side. Mismatches are
completely fine when flagged — they are only a problem when hidden.

---

*CI Team — citeam.claudemax@gmail.com · The only goal is site-visit bookings
ATTENDED; everything else is instrumentation.*
