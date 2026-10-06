# ANGAD audit — round 2 responses for the CI/analytics team

**From:** developer-side Claude, inside the ANGAD agent repo
**Date:** 2026-08-08
**Repo:** `bitbucket.org/asbl-dev/angad`, branch `work/stage-ci`
**Live probes today:** `growth-angad.asbl.in` and `growth-angad.stage.asbl.in` (public HTTP endpoints only)

---

## ⚠️ READ FIRST — the AWS asks are declined, and here is what you get instead

**Section 1 in full, the ECS revision history in Section 2, and the raw log excerpts in Section 3 will not be provided.** This is a deliberate decision on our side, not an oversight. Two reasons, both concrete:

**1. A raw ECS environment dump is a secrets dump.** That container's environment holds `GOOGLE_API_KEY`, `PLIVO_AUTH_TOKEN`, `TELNYX_API_KEY`, `PERISKOPE_API_KEY`, `ASBL_VOICEBOT_API_KEY`, `INHOUSE_POSTHOOK_SECRET`, `WS_AUTH_SECRET` and the full `MONGODB_URI` with embedded credentials. Redacting a list like that by hand, line by line, is exactly the operation that leaks one. Your own brief says never paste credentials — the safest way to honour that is not to handle the object at all.

**2. The production log stream contains customer phone numbers.** Verified in code, not assumed: `plivo/outbound.ts:93` logs `to=<phone>` on every dial, `telnyx/outbound.ts:111` does the same, and `plivo/media-stream.ts:1114` logs `customer=<phone>` on every stream start. The `[timing]` lines you want sit interleaved with those in the same stream. Pulling a window of logs and hand-scrubbing it is a data export of customer PII with a manual redaction step in the middle. We are not doing that.

Separately and independently: **there is no AWS CLI on this workstation** (`aws: command not found`), so even setting the policy aside I could not have run those commands today.

**What follows instead.** For nearly every fact you asked for, there is a way to get it that does not touch AWS and does not touch PII — usually a query against the database you already have read-only access to. **Section 1 below is a full mapping: for each environment variable you named, whether you can derive its effective value yourself, and the exact query.** Several of them you can answer better this way than from the env, because the data shows what actually happened rather than what was configured. Three genuinely cannot be derived, and for those I propose a small, safe code change that exposes them without exposing anything else.

I would rather give you a method you can re-run next month than a snapshot you have to trust.

---

# 1. Production environment variables

**[declined — see above]** for the raw list, prod and staging both.

### 1a. Answered right now from code — no AWS needed, and these are definitive

You asked which of the hardcoded timings are env-configurable versus compile-time constants. **This is a pure source-code question, so here is the complete, authoritative answer.** All line numbers are current as of today's checkout.

| Constant | Value | Env-configurable? | Location |
|---|---|---|---|
| `GREETING_PROTECT_MS` | **7000** | **NO — compile-time** | `plivo/media-stream.ts:522`, `telnyx/media-stream.ts:276` |
| `CUSTOMER_SILENCE_MS` | 10000 | **YES** → `CUSTOMER_SILENCE_MS` | `plivo/media-stream.ts:179`, `telnyx/media-stream.ts:100` |
| `DEAD_AIR_TIMEOUT_MS` | 45000 | **NO — compile-time** | `plivo/media-stream.ts:170`, `telnyx/media-stream.ts:98` |
| `HANGUP_FLOOR_MS` | 8000 | **NO — compile-time**, function-scoped | `plivo/media-stream.ts:929` |
| `VISIT_GATE_MIN_MS` | 240000 | **YES** → `VISIT_GATE_MIN_MS` | `plivo/media-stream.ts:192` |
| `VISIT_GATE_MIN_TURNS` | 11 | **YES** → `VISIT_GATE_MIN_TURNS` | `plivo/media-stream.ts:193` |
| `SAME_ERROR_THRESHOLD_MS` | 8000 | NO | `plivo/media-stream.ts:126` |
| `DNC_GOODBYE_TIMEOUT_MS` | 8000 | NO | `plivo/media-stream.ts:128` |
| `HANGUP_GOODBYE_TIMEOUT_MS` | 3000 | NO | `plivo/media-stream.ts:130` |

**So your specific question — "GREETING_PROTECT_MS, the 7s value, is it overridden?" — has a definitive answer: it cannot be. It is a `const` declared inside the connection handler with no `process.env` read anywhere near it, in both provider paths. The value in production is 7000ms. No env var can change it; only a code change can.** That is one item off your list with certainty, and it did not need AWS at all.

Note also that the visit gate **is** env-tunable (both the 4-minute and 11-turn thresholds), which matters for your Section 8 question about it.

### 1b. Derivable by you, from the database you already read

| Variable | Derivable? | Query / method |
|---|---|---|
| `NIGHT_DEFER_ENABLED` | **YES — decisive** | `db.scheduledcalls.countDocuments({kind:'night_defer'})`. The night guard's *only* action is to write such a row. **0 = it has never fired once = off.** Also check `max(createdAt)` on those rows to date when it was last active. |
| `CALL_WINDOW` | **YES — by contradiction** | `checkDialAllowed()` runs *before* any Call document is created, on both dial paths, and returns HTTP 429 with no row written. You already measured ~10.3k out-of-window dials **that exist as rows**. Those rows could not exist if `CALL_WINDOW` were set to a window excluding them. **Therefore it is unset, or set to a window that permits them.** |
| `SITEVISIT_SCHEDULER_ENABLED` | **YES — decisive** | `db.scheduledcalls.countDocuments({kind:'sitevisit'})` plus `max(createdAt)`. Cross-check: `db.calls.countDocuments({externalScheduleId:/^sched-/})` — that prefix is generated only by the site-visit poller. Both zero = off. |
| `TRANSCRIBE_POSTCALL` | **YES — decisive** | `db.calls.countDocuments({accurateTranscript:{$exists:true}})` and `db.calls.distinct('transcriptSource')`. Non-empty = on. |
| `PLIVO_AMD_ENABLED` | **YES — strong proxy** | AMD-origin voicemails hang up at ~4s *before any conversation*, so they have **no transcript**. Transcript-regex voicemails always have text. Query: `{outcome:'VOICEMAIL', phoneNumber:/^\+91/, durationSeconds:{$lt:15}, $or:[{fullText:''},{fullText:{$exists:false}}]}`. Near-zero on Plivo while the equivalent non-`+91` (Telnyx, portal-side AMD) query is non-zero → Plivo AMD is off. |
| `MAX_CALLS_PER_DAY` | **YES** | Group calls by day and take the max. You have observed days of ~1,444. A cap at or below that would have been impossible. **So it is unset or >1444.** |
| `MAX_CONCURRENT_CALLS` | **YES** | Compute maximum overlap of `[startedAt, endedAt]` intervals. If the observed peak is an arbitrary-looking number, unlimited; if it flat-tops at a round value across many days, that value is the cap. |
| `RUN_SCHEDULERS` | **YES — decisive** | The dashboard-snapshot job runs **only** when `RUN_SCHEDULERS=true` (`index.ts:835-852`) and writes one document to the **`dashboard_snapshots`** collection every ~3 min (`dashboard-snapshot.ts:201`). A recent `updatedAt` there = true on at least one task. |
| `ASBL_CRM_CALLBACK_URL` | **YES — and better than the env** | `db.calls.distinct('callbackUrl')` gives you the **actual hostnames used per call**, not just the fallback default. Group by host and by month and you will see the relay cutover happen in your own data. The env var is only consulted when a call carries no `callback_url`. |
| `MONGODB_URI` (host) | **self-answering** | You are connected to it. |
| `MONGO_DB_NAME` | **self-answering** | Whichever database your `calls` collection lives in — `angad`. |
| `LEGACY_DB_NAME` | **self-answering** | `ASBLVoiceBot`, if it is receiving recent writes. Structural note from round 1: the mirror uses `mongoose.connection.useDb`, so it is **always the same cluster** as `angad` — that cannot diverge. |
| `PERISKOPE_SENDERS` (count) | **YES — lower bound** | `db.wa_senders.distinct('sender').length`. This is the count of senders ever successfully used, so it is a floor on pool size, not the configured count. **Those values are ASBL business numbers — count them, do not list or export them.** |

### 1c. NOT derivable from data — these three need a code change

`GEMINI_VOICE`, `GEMINI_LIVE_MODEL`, `GEMINI_TEMPERATURE`, `GEMINI_VAD_SILENCE_MS`, `TEAMS_WEBHOOK_URL`, `ALERT_EMAIL_WEBHOOK_URL`, `WS_AUTH_ENABLED`, `VERIFY_WEBHOOKS` leave **no trace in the database**. The model and voice are not recorded per call; the alerting webhooks either fire or silently do not.

**Proposed workaround — a redacted config-summary endpoint.** A small addition to the agent:

```
GET /api/config-summary        (Bearer-authenticated, same token as /api/schedule-call)
{
  "flags":  { "nightDefer": false, "sitevisitScheduler": true, "transcribePostCall": false,
              "plivoAmd": false, "runSchedulers": true, "verifyWebhooks": false, "wsAuth": false },
  "gemini": { "model": "...", "voice": "...", "temperature": 0.7, "vadSilenceMs": 700,
              "languageCode": "en-IN", "classifierModel": "...", "transcribeModel": "..." },
  "limits": { "callWindow": null, "maxConcurrentCalls": 0, "maxCallsPerDay": 0 },
  "hosts":  { "mongoHost": "<hostname only>", "crmCallbackHost": "<hostname only>" },
  "secretsPresent": { "googleApiKey": true, "plivo": true, "telnyx": true,
                      "periskope": true, "crmApiKey": true, "posthookSecret": true,
                      "teamsWebhook": false, "alertEmailWebhook": false },
  "periskopeSenderCount": 19,
  "promptVersion": { ...existing /api/prompt-version payload... }
}
```

Booleans and non-secret scalars only. Every secret rendered as `true`/`false` for presence. URLs reduced to hostname. **No value that is itself a credential is ever emitted, so this is safe to expose and safe to re-read whenever you want** — which solves the snapshot-goes-stale problem too. It works identically on staging, giving you the prod-vs-stage diff you asked for.

**I can implement this.** It is ~40 lines in `agent/src/index.ts`. It would go to staging first for verification, then to prod through the normal PR flow — that is this project's standing workflow and the deploy decision belongs to Mukund, not to me. **Say the word and I will write it.**

### 1d. Staging task definition

**[declined — same reasons.]** Once the config-summary endpoint is live you can diff prod against stage yourself by calling the same path on both hosts, which is a better artefact than a one-time paste.

---

# 2. Deploy and prompt history around Aug 5–7

### 2a. Last 10 ECS task-definition revisions

**[declined — AWS.]** No data-side workaround exists for revision history; it lives only in ECS.

**Route it:** anyone with a role in the ASBL AWS account (ap-south-1) can produce this in one command, and the output is infrastructure metadata with no customer data in it — so **this is a reasonable thing to request directly from the platform team rather than through us.** Ask for revision number, `registeredAt`, and image tag; ask them to omit the `environment` block.

### 2b. The Ankit prompt push

**[live — probed `GET https://growth-angad.asbl.in/api/prompt-version` today]**

```
slug:                lead_qualification_and_selling
code_file_version:   2026-08-02-radhika-v3.8.1-facts-restore-staging
mongo_file_version:  2026-08-05-ankit-v3.8-male-v6.0.0-four-endings
mongo_version_number: 3047
mongo_content_length: 117703
mongo_updated_at:    2026-08-07T08:47:21.807Z
in_sync:             false
```

**[live — git]** There is a matching commit: `532426d` *"Point the production slug at the Ankit prompt"*, plus `3bdc50e` *"Ankit's call handling is now simply the call handling"*. So this was an intended production switch, not an accident.

**Who pushed it, why, A/B or permanent, rollback plan: UNKNOWN.** Those are decisions held by Mukund / the product owner, not recorded anywhere in code or database. **Route to them.** What I can tell you is the mechanism: `agent/scripts/push-prompt-to-mongo.ts` writes the Mongo copy, Mongo wins over code at runtime, and rollback is therefore a re-push of the previous content — **but see the next paragraph, because rollback has a trap.**

**Is there a record of which calls ran which persona? Not directly — and the obvious approach fails.** There is no prompt-version field on the Call document, and the `Prompt` collection stores **one document per slug, overwritten in place with no history** (`db/models/Prompt.ts`; `version` is a bare counter, currently 3047). **Critically, the slug did NOT change** — both personas run under `lead_qualification_and_selling` — so `calls.slug` cannot distinguish them. Anyone reaching for slug will get a null result and may wrongly conclude nothing changed.

**Workaround, and I think this one is genuinely good: classify persona from the transcript.** The agent introduces itself by name in the greeting on every call, and that name is in the text:

```js
db.calls.countDocuments({ fullText: /\bAnkit\b/i })
db.calls.countDocuments({ fullText: /\bRadhika\b/i })
```

Bucket those by `createdAt` and you get a per-call persona label, empirically derived, covering the whole history — including any earlier switches nobody documented. Two caveats: transcript accuracy is <50% (round 1, H29), so expect some misses and a small overlap band; and the batch transcriber's prompt explicitly names both "Radhika" and "Ankit" as expected speakers (`transcribe-recording.ts:37`), so on re-transcribed calls the name is well-recognised. **Treat the crossover date as approximate, but the buckets as sound.** Cross-check the crossover against `mongo_updated_at` above.

Also worth knowing: the persona switch changed the agent's **gender** (male voice `Achird` is the code default). Any analysis spanning early August is comparing two different agents.

### 2c. Dial volume drop Aug 6–7

**UNKNOWN — the agent does not control dial volume.** Both volume caps default to unlimited and the agent is purely reactive; volume is set by the relay's cron and campaign config. Nothing in this repo changed on Aug 6–7 that could reduce it.

The one dated fact I have is the prompt push on Aug 7 at 08:47 IST — which **cannot** explain fewer dials (a prompt does not gate dialling) but sits suspiciously close to your "0 bookings". With only 75 dials, 0 bookings at a ~2–3% rate is unremarkable noise, so I would not read much into the bookings figure alone.

**Suggested split, all runnable by you:** on those 75 rows check (i) `status`/`outcome` spread — all `initiated`/`failed` with no `in-progress` points at a dial-side failure, a normal spread points at "the relay only asked for 75"; (ii) `externalScheduleId` prefixes — a missing bulk prefix means a campaign was paused upstream. My leading hypothesis remains an upstream campaign change, because it is the only cause that naturally produces both a ~95% volume collapse *and* zero bookings.

---

# 3. Logs and monitoring

### 3a. Log group name, retention, access

**UNKNOWN — no AWS access, and declined regardless.** The agent logs to stdout via Fastify's pino logger, so on ECS Fargate these land in CloudWatch Logs; group name, retention and IAM are account settings. **Route to the platform team** — group name and retention are infrastructure metadata with no PII, so that request is safe to make directly.

### 3b. Five sample `[timing]` lines

**[declined.]** As set out at the top: the log stream carries customer phone numbers on adjacent lines from every dial and every stream start (`plivo/outbound.ts:93`, `telnyx/outbound.ts:111`, `plivo/media-stream.ts:1114`). Extracting a window and hand-scrubbing it is a PII export with a manual redaction step, and manual redaction is where leaks happen.

**Better workaround — persist the metrics instead of exporting the logs.** The agent already computes exactly what you want and throws it away (round 1, C14). At teardown it has `turns`, `avg`, `p50`, `p90`, `max`, and `slow(>2.5s)` count in memory (`plivo/media-stream.ts:1503-1511`), plus the call-open breakdown. **Adding a `latency` sub-document to the Call schema is a small change** — six numbers, no PII, no text — and then every latency question you have becomes a normal aggregation over `calls`, forever, with no log access and no export. It also gives you the one thing raw log sampling cannot: full population coverage rather than five lines.

I would pair this with the config-summary endpoint as one small PR. **Again — happy to write it; the ship decision is Mukund's.**

**Interim proxy you can compute today:** first agent turn minus `startedAt`. Two known biases to correct for — `startedAt` is set at WebSocket open, and the greeting is deliberately held for the full 7000ms `GREETING_PROTECT_MS` window, so subtract ~7s to approximate model latency; and it is blind to calls where the agent never spoke.

### 3c. Seven-day counts of log strings

**Three of your four are already in the database — you do not need logs for them.** [live, from code]

| You asked for | Query against `angad` | Notes |
|---|---|---|
| `"classifier offline"` | `db.calls.countDocuments({summary:/classifier offline/i, createdAt:{$gte:<7d>}})` | The heuristic fallback writes this literal string into `summary` (`classifier.ts:174`). It is a reliable marker for "the classifier failed and all slots are null" — see round 1, J11. |
| `"night defer"` | `db.scheduledcalls.countDocuments({kind:'night_defer', createdAt:{$gte:<7d>}})` | Same decisive test as §1b. |
| `"webhook" + "failed"` | `db.calls.countDocuments({'webhookDeliveries.status':'failed', createdAt:{$gte:<7d>}})` | Every delivery attempt is recorded per call. Also aggregate `webhookDeliveries.lastStatusCode` to separate 4xx (permanent, no retry) from 5xx. **This is the single most under-used field in the schema.** |
| `"whatsapp" + "401"` | **not in the database** | WhatsApp sends are fire-and-forget and log-only (`media-stream.ts:1718-1728`). Best available proxy: `db.calls.countDocuments({outcome:'PRE_SITE', siteOfficeSentAt:{$exists:false}})` — every PRE_SITE call attempts a site-office location send, so a missing `siteOfficeSentAt` implies the send failed. Not exact, but directionally sound and PII-free. |

### 3d. Is error alerting live? When did the last alert arrive?

**UNKNOWN.** `alerts.ts:52` short-circuits to a no-op unless `TEAMS_WEBHOOK_URL` or `ALERT_EMAIL_WEBHOOK_URL` is set, and I cannot read either. No database trace.

**Route it the human way:** whoever owns the Teams channel or the alert mailbox can answer "have we ever received one, and when was the last" in about ten seconds, and that answer is more trustworthy than the config anyway — a webhook can be set and still be broken. The config-summary endpoint would give you the set/not-set half.

---

# 4. The relay and upstream systems

**Everything in this section is UNKNOWN to me, and I want to be precise about why rather than give you soft answers.** I grepped the entire agent and dashboard source for `growth-relay`, `Intelligent_CRM`, `MASTERLEADID`, `master_lead_id`, `Inncircles`, `reactivation`, `posthook_payloads` — **zero hits on all seven.** This repo is the voice agent: it receives a dial instruction over HTTP, places the call, writes a call record, POSTs a result webhook. It has no knowledge of the relay's internals, its cron, its campaign config, its database, or its retention policy.

Per-item:

- **Which repo/service is `growth-relay.asbl.in`, who owns it, can it be audited** — UNKNOWN. Not this repo. The only relay address in our code is the *fallback* default `ASBL_CRM_CALLBACK_URL` (host `asbl-crm-api.vercel.app`), which is overridden per-call. **You can discover the real hosts yourself:** `db.calls.distinct('callbackUrl')`, grouped by month — that gives you the actual endpoints in use and when they changed, which is a decent starting point for finding the owner.
- **Does the relay feed Intelligent_CRM; why only ~12% of dials; live/frozen/deprecated** — UNKNOWN. Nothing in the agent writes it, so the agent cannot be its source.
- **What decides daily dial volume; request counts Aug 4–7; rejection counts** — UNKNOWN and, importantly, **structurally unknowable from our side.** Suppressed requests (DNC, visit-scheduled, invalid phone, unknown slug, rate-limited) return an HTTP response and **write no row at all**. Only the relay holds the true ask count. This is the single biggest blind spot in the whole audit and it is on their side, not ours.
- **Who uses `POST /api/calls/initiate` (the path that skips the night guard); how often; can it be retired** — **[live] I found you a detection method.** That route calls `makeOutboundCall()` directly and **never creates a Call document** (`index.ts:530-571`); the document is only created later by the media-stream if the call is answered — and it is created **without an `externalScheduleId`**. So:
  ```js
  db.calls.countDocuments({ direction:'outbound',
    $or:[{externalScheduleId:''},{externalScheduleId:{$exists:false}},{externalScheduleId:null}] })
  ```
  **Outbound calls with no esid are almost certainly `/api/calls/initiate` dials.** Bucket by date to see usage over time. Note this also means **unanswered dials on that path leave no record whatsoever** — invisible even to us. Whether it can be retired is a product/ops decision for Mukund; technically it is Bearer-authenticated and its only distinct feature is the legacy `engine` parameter for testing.
- **The ~28,000 failed WhatsApp sends** — UNKNOWN which system sent them, and I remain confident it was not this one. Round 1 established that this code cannot mark a 401 as success (`periskope.ts:97`, `send-brochure.ts:110`) and that it sends at most one brochure per call, deduped. Your figure of 58 agent sends ever is consistent with that. **Finding the real sender matters** — if there is a second WhatsApp path in the estate, it changes the customer-contact picture materially.
- **Does the relay pass `source_lead_id` / `zoho_lead_id` today, and since when** — **[live] you can answer this yourself, precisely.** The agent stores whatever it is given at `Call.metadata.source_lead_id` and `Call.metadata.zoho_lead_id` (`routes.ts:307-309`). So:
  ```js
  db.calls.countDocuments({'metadata.source_lead_id':{$exists:true}})
  // and the "since when":
  db.calls.find({'metadata.source_lead_id':{$exists:true}}).sort({createdAt:1}).limit(1)
  ```
  Recent commits (`46d5d6b` "Echo zoho_lead_id on call_completed too", `7caaac6` "CRM contract: echo source_lead_id") show this was being wired up recently. **If coverage is good, this is a first-party join from a call straight to a Zoho lead id with no intermediate collection and no inference — likely better than your current `master_lead_id` bridge.** Check coverage before committing to it.
- **`posthook_payloads` retention / archiving** — UNKNOWN, relay-side collection.
- **Where Zoho workflows fire calls from; does Zoho enforce calling hours** — UNKNOWN. What I can confirm is the delegation, in writing: `limits.ts:5-9` states *"Production relies on the CRM (Zoho) for call-timing rules, so it leaves CALL_WINDOW UNSET."* **So the code explicitly hands compliance to Zoho. Whether Zoho actually enforces it is exactly the question to put to the CRM team, and your ~10.3k out-of-window dials are prima facie evidence that somebody should answer it in writing.**

---

# 5. Provider portals

**UNKNOWN across the board — I hold no portal credentials and have no dashboard access.** Plivo retention and account-level AMD, Telnyx retention / daily cap / per-destination rates, Periskope live sender count, Google billing account and quota, and last month's spend totals all require portal logins held by Mukund or the platform team. **Route to them.**

Three things I can add that reduce what you need from the portals:

1. **Gemini spend does not need the portal.** `Call.tokenUsage` records exact per-call token counts split by modality (`total`, `prompt`, `response`, `audioInput`, `textInput`, `audioOutput`, `textOutput`, `updates`). Multiply by whatever rates finance confirms and you have true per-call cost with no estimation. **This is the right input for the ₹-per-outcome tile** — better than a monthly total, because it attributes cost to individual outcomes.

2. **The cost shape is counter-intuitive and worth stating on the tile.** [docs] The dominant driver is the system prompt billed as text input on *every* call — the live prompt is **117,703 characters** (measured today via `/api/prompt-version`), roughly ₹2.5 fixed per call before anyone speaks. **Cost per call is therefore closer to flat than proportional to duration**, so ₹-per-outcome is driven by dial volume and connect rate, not call length. Short calls looking expensive per-minute is an artefact, not inefficiency.

3. **[docs, unverified] `config.ts:214-216` mentions a $20/day cap configured on the Telnyx Outbound Voice Profile.** If that is still set it is a **throughput ceiling**, and hitting it would silently stop international calls mid-day. Given ~94.5% of your out-of-window dials are Telnyx/NRI, that cap is worth confirming for reasons beyond cost.

**On your international calling-hours question:** the agent's night guard is `+91`/Plivo-only by explicit condition (`routes.ts:258`), so non-`+91` dials were **never gated by design**. Whether 2am IST is acceptable depends entirely on the destination's local time, which nothing in this system evaluates. **There is no international calling-hours policy implemented anywhere in the agent. If one is intended, it does not exist yet** — that is a product decision that needs making, not a bug to find.

---

# 6. Attendance

**[live — code, verified today] No. No system reachable from here records attendance, and the gap is deeper than "the field is empty".**

I searched the agent and dashboard for `visit_attended`, `attended`, `visit_done`, `checkin`, `check_in`, `showed`, `no_show`: **zero hits.** There is also **no inbound endpoint through which attendance could arrive** — no route, no webhook, no field. Even if Inncircles, Compass, or a security register captured check-ins digitally today, **nothing is wired to receive that signal.** It is un-architected, not merely un-populated.

What exists: BOOKED (`Call.outcome='PRE_SITE'`, `Lead.status='visit-scheduled'`, `Lead.siteVisitScheduledAt`). CONFIRMED does not exist as a state — `ScheduledCall.status='done'` only means a reminder call was answered, not that the customer confirmed anything.

**Whether Inncircles / Compass / a sales app / a security register records it: UNKNOWN — those are outside every system I can see.** That question needs to go to whoever runs the site office, and it is worth asking plainly, because if the data already exists on paper the remaining work is small.

**My recommendation, offered once and then it is the owner's call:** this is the highest-value fix in the entire audit, and it is not a big build. One authenticated endpoint on the agent, or one field that a daily ops sheet writes back to `Lead`, makes the whole funnel measurable. Until then, every metric either of us produces — including everything in this document — is measured against **bookings**, a proxy one full step removed from the goal, and a proxy a sufficiently pushy agent can inflate without moving the real number at all. "Did that change help?" is currently unanswerable by construction.

---

# 7. Security actions

### 7a. The Bitbucket token — investigated, and the news is better than expected

**[live — git, verified today]**

- The token is in the **local working-tree file only** (`brain.md`, 2 occurrences).
- `brain.md` is **not tracked by git**: `git ls-files --error-unmatch brain.md` → *"did not match any file(s) known to git"*.
- **It never entered git history on any ref.** `git log --all -S 'ATCTT3xFf' --oneline` → **0 commits**; `git log --all -S 'x-token-auth' --oneline` → **0 commits**.
- History check: `brain.md` *was* committed once (`06b9eb3`) and then removed and gitignored (`76a67c6` *"keep brain.md local-only"*). I inspected the committed version — **it contained no token**. The token was added to the file after it was already gitignored.

**Conclusion: the token was never pushed to any remote — not GitHub, not either Bitbucket remote. There is no public or repo-level exposure.** That materially de-escalates this: it is a local-workstation secret, not a leaked one.

**Not done, and I cannot do it: rotation.** Revoking a Bitbucket access token requires signing into the Bitbucket account that owns it. That is Mukund's account and his action. **Flagging once, as fact rather than alarm:** the token is live and grants repo read access, so rotating it is still worth doing on general hygiene grounds even with no remote exposure. I have not edited `brain.md` — it is Mukund's working document and the token is embedded in a documented deploy command, so removing it without a replacement placeholder would break that runbook. **I can redact it to a placeholder and note where to source the real value, on request.**

### 7b. `/health` and `/api/health` public exposure

**[live — probed both today]** Confirmed publicly reachable with no authentication. Exact payloads:

```
GET /health       → { ok, version, pool:{warm, active} }
GET /api/health   → { status, version, queue_depth, pool:{warm, active},
                      warmup:{size, fulfilled, pending} }
```

**No PII. No secrets. No customer data. No counts of real business volume.** `version` is the static bot API version (`1.0.0`), and every other field is Gemini session-pool internals.

One correction worth carrying: **`queue_depth` is not a dial queue.** It is `max(0, active - warm)` on the Gemini pool (`index.ts:70`). Any external monitor reading it as a backlog of pending calls is reading a Gemini statistic. That is a monitoring-interpretation bug waiting to happen.

**Assessment:** `/health` is genuinely harmless and is load-bearing for uptime monitoring — leave it. `/api/health` additionally exposes pool internals, which is minor reconnaissance value only (it reveals roughly how much concurrency we provision). **Restricting it is defensible but low priority; the decision is Mukund's, and I have not changed anything.**

### 7c. Webhook secret verification and WS auth

- **Is `X-Webhook-Secret` actually verified on the receiving side?** **UNKNOWN — that is the relay's code, not ours.** Our side always sends the header (`webhook-sender.ts`, secret resolved from the Mongo `webhooks` collection first, then `INHOUSE_POSTHOOK_SECRET`, then empty). Whether the receiver checks it can only be answered by the relay team. Worth asking: if they do not, our posthook endpoint is effectively unauthenticated to anyone who learns the URL.
- **`WS_AUTH_ENABLED` in prod: UNKNOWN** (env). Code default is `false` (`config.ts:47`). Same for `VERIFY_WEBHOOKS`, default `false` (`config.ts:35`). **Both would appear in the config-summary endpoint proposed in §1c.** Weak inference in the meantime: if `VERIFY_WEBHOOKS` were enabled and misconfigured, live calls would 403 and break outright — calls are working, so it is either off or correctly configured.

---

# 8. Product questions

Short answers, code facts separated from decisions. **Every "should we" here is Mukund's call, not mine** — I have given the technical position and a recommendation where I have one.

- **Retire the `site_visit_confirmation` slug?** [live] It is dead in the planner but fully dialable: registered in `PROMPT_SLUGS`, passes `isValidSlug()`, accepted by `/api/schedule-call`. Your finding that 20 such calls came from humans clicking the ops dashboard matches the `dash`-prefixed esid convention (`routes.ts:325`). **Technically it is a two-line gate** (reject the slug at the route, or remove it from the registry). **Recommendation: gate it rather than delete it** — deleting the prompt would break any relay still passing that slug, whereas a gate returns a clean error. Who specifically uses the dashboard button: UNKNOWN, ask the ops team.

- **Telugu / Tamil / Kannada DNC patterns?** [live] Confirmed absent — `crm/dnc.ts:12-33` is English + Hindi/Hinglish only, 16 patterns, while the classifier explicitly recognises Telugu, Tamil and Kannada as call languages. **This is a live compliance gap, not just a metrics artefact:** a Telugu speaker asking us to stop is not detected, `Lead.doNotCall` is never set, and dialling continues. Whether it is on anyone's list: UNKNOWN. Whether the relay or Zoho has its own DNC layer on top: UNKNOWN, ask the CRM team — **and that question matters a lot, because if they do not, nobody catches it.** Adding patterns is a small, low-risk change.

- **Was the 4-minute / 11-turn visit gate ever validated?** UNKNOWN — no validation artefact exists in the repo. [live] Both thresholds **are env-tunable** (`VISIT_GATE_MIN_MS`, `VISIT_GATE_MIN_TURNS`, `media-stream.ts:192-193`), so testing a lower value needs no code change. Your concern is correct and worse than it looks: **a rejected booking leaves no trace at all** — the tool call is refused, the bot is nudged to continue, and nothing is written. So the cost of this gate is invisible in the data by construction, and cannot be measured retrospectively. If you want to size it, it needs instrumentation first (log/persist gate rejections), then a staging A/B.

- **Enable `TRANSCRIBE_POSTCALL`?** [live] Technically ready — fully async, fires only after the recording URL exists, wrapped in try/catch, zero live-call impact. It writes `accurateTranscript`, overwrites `fullText`, and stamps `transcriptSource`. **Two things to flag before anyone flips it.** First, it **overwrites `fullText` but not `transcript[]`**, so those two fields will disagree on newly processed calls — plan for that in your queries (round 1, J5). Second, and more important: **it will not retroactively fix your outcomes.** Classification runs at teardown on the weak transcript and is **never re-run** after re-transcription. So enabling it improves future *readable* transcripts but does not by itself improve booking detection — that needs the classifier to run after re-transcription, which is an additional change. **Recommendation: enable it, and separately consider re-running classification on the accurate transcript** — together those two are, in my view, the highest-leverage technical fix available. Cost concern is fair to dismiss: a batch text model over one 8kHz mp3 is small next to the ~35K-token prompt already billed on every call. Deploy decision: Mukund's.

- **`GREETING_PROTECT_MS` — the real story.** [live, from the code comment at `media-stream.ts:512-520`, which is unusually explicit] It is **not** a latency optimisation, and you are right to push back on that framing. The actual reason: **barge-in during the greeting.** A caller saying "hello? hello?" while the bot was greeting would trigger Gemini's interrupt, cancel the greeting, and force a regenerate — producing **5–7 seconds of dead air at the start of the call**. So for the first 7000ms the agent (a) does not forward inbound customer audio to Gemini and (b) does not flush bot audio on interrupt. The comment also explains why it is handled our side rather than in the model: Gemini Live's VAD sensitivity is a fixed setup-time value with only HIGH/LOW, no MEDIUM and no mid-session change.

  **So the trade is real and it is a genuine trade, not a mistake:** without it, some calls open with 5–7s of dead air; with it, the customer is genuinely unheard for the first 7s. Note it is a **compile-time constant in both provider paths** — lowering it requires a code change and a redeploy, not an env tweak. Whether the team is open to lowering it: UNKNOWN, that is Mukund's call — but the honest engineering framing is that this is tunable and testable on staging, and 7000ms looks like a conservative first guess rather than a measured optimum. No blame attaches to it; the dead-air bug it fixed was worse.

---

# Least-confident answers

Ranked by how much I would want them independently checked before you build on them:

1. **§1b `PLIVO_AMD_ENABLED` proxy.** The "voicemail with no transcript" signature is inference, not proof. A very short real voicemail could also produce an empty transcript. Treat a non-zero result as suggestive, not conclusive.
2. **§2b persona classification from transcripts.** Sound in principle, but rests on a <50%-accurate transcript and on the agent always self-introducing. Expect a fuzzy crossover band around Aug 5–7 rather than a clean cut.
3. **§4 `/api/calls/initiate` detection via missing esid.** I verified the route creates no Call document, so the signature should be clean — but inbound calls also lack an esid, which is why I scoped the query to `direction:'outbound'`. If your numbers look odd, that scoping is the first thing to check.
4. **§1b `MAX_CONCURRENT_CALLS` via interval overlap.** Depends on `endedAt` being reliably set. Calls force-closed by `cleanupStaleCalls` get a synthetic `endedAt`, which will distort overlap counts.
5. **§5 the Telnyx $20/day cap.** [docs] From a code comment, not verified against the portal today. It may be stale.
6. **§2c the Aug 7 hypothesis.** I can rule out agent-side causes confidently; "upstream campaign change" is a labelled hypothesis, not a finding.
7. **§3c the WhatsApp-401 proxy** (`PRE_SITE` with no `siteOfficeSentAt`). Directional only — a missing send could also mean no site office is configured for that project (`brochures.ts` returns null and logs a warning).

Everything in **§1a (timing constants), §7a (token never pushed), §7b (health payloads), and §6 (no attendance anywhere)** I regard as verified fact, checked directly today against code, git, or a live endpoint.

---

# Redaction confirmation

I re-scanned this document three times before sending, per the brief.

- **Phone numbers:** none. No customer numbers, no ASBL business numbers, no partial or masked examples. Where the schema stores numbers (`wa_senders.sender`, `Call.phoneNumber`) I named the **field** and instructed counting rather than listing. The only digit strings present are line numbers, port-free timing values in milliseconds, character counts, a Mongo version counter (3047), and commit hashes.
- **Email addresses / `@` signs:** none. Scanned specifically for `@`; zero occurrences.
- **Credentials:** none. Every secret is referenced by environment-variable **name** only, or as present/absent. No keys, tokens, passwords, webhook secrets, or connection strings. The Bitbucket token in §7a is described and search-counted but **never reproduced** — the `git log -S` search strings shown are a 9-character prefix and a generic URL keyword, neither of which is usable.
- **Connection strings / hosts:** `MONGODB_URI` is referred to by name only. The only hostnames present are the two public agent domains (already known to you) and `asbl-crm-api.vercel.app` as the code's fallback callback default.
- **AWS identifiers:** I deliberately omitted the account number this round, referring only to "the ASBL AWS account (ap-south-1)".
- **No customer data was exported, copied, or moved** in preparing this. I read source code, ran read-only `git` commands against the local checkout, and made unauthenticated GET requests to two public health endpoints. **I did not connect to any database and did not read a single customer record.**
