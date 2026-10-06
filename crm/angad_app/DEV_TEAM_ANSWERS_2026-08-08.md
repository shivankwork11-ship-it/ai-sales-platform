# ANGAD voice agent — answers for the CI Team

**Prepared by:** developer-side Claude, inside the ANGAD agent repo
**Date:** 2026-08-08
**Repo audited:** `bitbucket.org/asbl-dev/angad` (remote `asbl`), local checkout branch `work/stage-ci` (tracking `asbl/stage`, 8 commits ahead)
**Live systems probed:** `https://growth-angad.asbl.in` (prod) and `https://growth-angad.stage.asbl.in` (stage) — HTTP health/version endpoints only

---

## READ THIS FIRST — three things that change how you read everything below

1. **The deployment moved.** ANGAD used to run on Render with a self-hosted MongoDB on an EC2 box. It now runs on **AWS ECS Fargate** in account `737866084260`, region `ap-south-1`:
   - prod = cluster `asbl-prod-vpc-cluster`, service `growth-angad-prod`, domain `growth-angad.asbl.in`
   - stage = cluster `asbl-test-cluster`, service `growth-angad-staging`, domain `growth-angad.stage.asbl.in`
   Source: `deploy-angad-asbl.sh:17-35`, `bitbucket-pipelines.yml` (custom pipelines `deploy-to-prod` / `deploy-to-stage`). Both domains answered my probe, so both are live right now.

2. **I could NOT read production environment variables, the ECS task definition, AWS SSM, CloudWatch, or any provider dashboard.** There is no AWS CLI on this machine (`aws: command not found`) and I hold no growth-prod MongoDB credentials. Roughly a dozen of your questions turn on a single environment variable being `true` or `false`. Everywhere that happens I say so explicitly and give you a **way to answer it from the data you CAN read**. Please do not read "the code supports X" as "X is on".

3. **A large part of what you are asking about is not in this repo.** `growth-relay.asbl.in`, `Intelligent_CRM`, `MASTERLEADID`, `master_lead_id`, `Zoho_Database.reactivation_list`, Inncircles — I grepped the entire agent and dashboard source for every one of those strings and got **zero hits**. This codebase is the *voice agent*: it receives a "dial this number" instruction over HTTP, places the call, runs the conversation, writes a call record, and POSTs a result webhook back. Lead selection, campaign pacing, identity resolution, and site-visit attendance all live in systems I cannot see.

---

# A. Write path and cluster truth

### A1. Does every component write ONLY to growth-prod? List every process holding a Mongo connection.

**[live code, env UNKNOWN]**

Processes in *this* repo that hold a Mongo connection:

| Process | What it is | Which DB it opens | Read/write |
|---|---|---|---|
| **Agent (ECS `growth-angad-prod`)** | Fastify voice server, `agent/src/index.ts` | `MONGODB_URI` + `MONGO_DB_NAME` (code default `angad`) | read + write |
| **Agent — legacy mirror** | `agent/src/db/legacy-mirror.ts` | `LEGACY_DB_NAME` (default `ASBLVoiceBot`) | write only |
| **Agent — in-process schedulers** | site-visit poller, night-defer poller, stale-call cleanup, dashboard-snapshot — all inside the same agent process, gated by `RUN_SCHEDULERS` | same connection as above | read + write |
| **Dashboard (Next.js, `web/`)** | ops UI | its own `MONGODB_URI` + `MONGO_DB_NAME` | read + write (dispatch, block, prompt admin) |

**The single most useful structural fact:** the legacy mirror does **not** open a second connection. It calls `mongoose.connection.useDb(LEGACY_DB)` (`legacy-mirror.ts:38-41`). `useDb` switches database *on the existing connection*. So **`angad` and `ASBLVoiceBot` are necessarily on the same cluster, always** — it is impossible for them to diverge. If `angad` is on growth-prod, `ASBLVoiceBot` is on growth-prod. You can treat that as a guarantee, not an assumption.

`Zoho_Database` and `Intelligent_CRM` are written by something else entirely — nothing in this repo touches them.

**What I cannot confirm:** the actual value of `MONGODB_URI` in the running ECS task. **How you can confirm it from your side:** run `db.serverStatus()` or check `$currentOp`/connection metadata on growth-prod and look for a connection whose `appName`/driver is the Node Mongoose driver, or simply check whether `angad.calls` has documents with `createdAt` in the last hour. If it does, the agent is writing to growth-prod. If the newest doc predates the cutover, it is not.

### A2. Is the old cluster (angad.3z3qbu) or growth-test still written to? Any periodic copy job between clusters?

**[live code] — Nothing in this repo copies data between clusters. There is no sync job, no replication script, no cross-cluster writer.** The agent opens exactly one connection and writes to exactly the DB names in its own env.

`agent/scripts/migrate_old_mongo.ts` exists but is a **manual, one-shot** migration script — it is not wired into any scheduler, cron, or pipeline. Nothing invokes it.

**Whether the old cluster is still receiving writes depends entirely on whether some *other* deployment (an old Render service, a stale ECS task revision, a dev laptop) is still running with the old URI.** I cannot see that. **[UNKNOWN]**

**How to settle it from your side:** on the old cluster, `db.calls.find().sort({createdAt:-1}).limit(1)`. If `createdAt` is after the cutover date, something is still writing and you have a live second writer. That is the definitive test and it takes ten seconds.

### A3. What is `growth-relay.asbl.in`?

**[UNKNOWN — not in this repo]**

Zero references. What I *can* tell you is the shape of the contract from our side:

- The agent's default outbound webhook target is `ASBL_CRM_CALLBACK_URL`, whose **code default** is `https://asbl-crm-api.vercel.app/api/relay/inhouse-posthook` (`config.ts:244-247`). That is the *old* relay address baked into the code.
- **The default is usually not what is used.** Every dial request can carry a per-call `callback_url`, and it wins (`webhook-sender.ts`, `SendOptions.callbackUrl`; stored on `Call.callbackUrl`). So the relay tells us where to call it back, per call.
- We send `POST` with header `X-Webhook-Secret`, 3 attempts, backoff 10s → 60s → 5min, 4xx is permanent (no retry), 5xx/timeout retries.
- **Every delivery attempt is recorded on the call document** in `Call.webhookDeliveries[]`: `event_id`, `event_type`, `status` (pending/delivered/failed), `attempts`, `lastStatusCode`, `lastError`, `sentAt`.

**This is your best available instrument and I do not think you are using it yet.** `Call.callbackUrl` tells you exactly which relay host each call was reported to — you can group by it and see the cutover happen in the data. And `Call.webhookDeliveries[].lastStatusCode` tells you whether the relay accepted it. If bookings are "invisible" downstream, that array distinguishes "we never sent" from "we sent and they 200'd and dropped it".

`agent/scripts/audit-disposition.ts` (in the repo, read-only) already does exactly this three-way fault split. Point it at your read-only URI and it will tell you, per call, whether a missing booking is ours or theirs.

### A4. Why does Intelligent_CRM miss ~88% of dials? Should we ignore it?

**[UNKNOWN — not in this repo]** No code here reads or writes `Intelligent_CRM`. The agent does not know that database exists.

The honest framing: the agent emits one `call_completed` webhook per finished call, to whatever `callback_url` it was given. Whatever populates `Intelligent_CRM` is downstream of that. An 88% miss rate is consistent with *any* of: only some campaigns passing a callback_url that routes there, a consumer that only ingests certain outcomes, or a partially-completed integration. I cannot distinguish those.

**I will not tell you to ignore it** — that is a product call and I do not have the standing to make it. What I will say is that `angad.calls` is the agent's own system of record and is the only store where the agent guarantees a row per dial it placed. If you need dial-level completeness, read that.

### A5. Which collections have TTLs, capped sizes, or cleanup jobs?

**[live code]**

**TTL indexes declared in this codebase: exactly one.**
- `CallState` (`db/models/CallState.ts:41`) — `{ updatedAt: 1 }, expireAfterSeconds: 7200`. Two hours. This is ephemeral per-call scratch state (answering-machine flag, "already greeted" flag, Gemini session-resumption handle, intentional-end flag). **It is not analytics data and its disappearance loses you nothing** — but note the corollary: the AMD/voicemail flag is *not* durable. Only its *effect* (outcome = VOICEMAIL) survives.

**No other collection in this repo has a TTL, a capped size, or a delete job.** I grepped for `expires`, `expireAfterSeconds`, `TTL`, and reviewed every scheduled job. Nothing calls `deleteOne`/`deleteMany`/`drop` on call or lead data.

**Two things that look like deletion but are not, and one that genuinely mutates history:**
- `cleanupStaleCalls()` (`index.ts:713-797`, every 5 min) **updates, never deletes**. But it *rewrites* outcomes — see J.
- `prompt-store` has a 10-second in-memory cache. Not persistence.
- The `Prompt` collection stores **one document per slug**, overwritten in place (`db/models/Prompt.ts` — `key` is unique, no history array). **Prompt history is destroyed on every prompt update.** This is the one place where history genuinely disappears silently, and it is the direct cause of C12 below.

`posthook_payloads` is not in this repo — that is relay-side, and your observation that it self-deletes is news to me.

---

# B. Call lifecycle — one call, birth to database

### B6. Walk one outbound call end to end.

**[live code]**

**Step 1 — Lead selected.** Not done here. Something external (the Zoho relay's cron, a Zoho button, an inbound webform, or a human uploading a CSV in the dashboard) decides who to call.

**Step 2 — Dial request.** `POST /api/schedule-call` with Bearer `ASBL_VOICEBOT_API_KEY` (`crm/routes.ts:71`). Required: `to` (E.164), `external_schedule_id`. Optional: `customer_name`, `slug`, `callback_url`, `metadata`, `source_lead_id`, `external_customer_id`, `engine`.

The route then, in order:
1. validates auth, phone, esid, slug
2. picks the provider: digits starting `91` → **Plivo**, everything else → **Telnyx** (`pickProvider`, `routes.ts:56-59`)
3. **upserts a `Lead` by phone** — creating it if absent (`routes.ts:182-194`)
4. **suppression gate 1: DNC** — if `lead.doNotCall`, returns HTTP 200 with `success:false, error_code:'DNC_SUPPRESSED'` and does not dial
5. **suppression gate 2: visit already booked** — if `lead.status === 'visit-scheduled'` OR `lead.siteVisitScheduledAt` is in the future, suppresses *only* `lead_qualification_and_selling` dials; site-visit and event slugs still go through
6. **suppression gate 3: night defer** — only if `NIGHT_DEFER_ENABLED=true`, only for Plivo/+91, only for non-site-visit slugs. See D19.
7. dials the provider
8. **writes the `Call` document with `status:'initiated'`**
9. mirrors a row into `ASBLVoiceBot.call_schedules`

**Step 3 — the `roomName` join key, and where it breaks.** This is the part you asked me to confirm, and **your join is valid for Telnyx and conditionally valid for Plivo.**

- **Telnyx:** `roomName = call_control_id`, assigned at dial time, stable for the whole call. `Call.roomName === call_sid` always. No breakage.
- **Plivo:** the dial API returns a `requestUuid`, which is *not* the `CallUUID` the rest of Plivo uses. So the Call doc is created with **`roomName = "pending-<requestUuid>"`** (`routes.ts:371-375`). It is only upgraded to the real `CallUUID` when the media WebSocket opens — i.e. **when the customer actually answers**.

**Therefore: every Plivo call that was never answered keeps `roomName = "pending-<uuid>"` forever.** Your `call_sid == roomName` join silently drops them. That is a systematic, outcome-correlated bias: the calls you lose are exactly the unanswered ones, so any connect-rate computed off that join is **inflated**. Match on `externalScheduleId` instead, or strip the `pending-` prefix — the codebase itself always queries with `$or: [{roomName: id}, {externalScheduleId: id}, {roomName: 'pending-'+id}]` (`routes.ts:501-505`, `610-617`). Copy that pattern.

**Step 4 — answer.** Plivo GETs `/api/plivo/answer`, we return XML containing a `<Stream>` to `wss://<host>/ws/plivo?...` with CRM context in the query string. Telnyx is answered via the Call Control API with `stream_url`.

**Step 5 — media stream opens** (`plivo/media-stream.ts`). In order: upgrade `roomName` to the real CallUUID → look up/create the Lead → set `Call.status = 'in-progress'` → **start recording** via a separate Plivo API call → grab a Gemini Live session (warm pool / warm-up bank / cold create) → send the greeting trigger.

**Step 6 — conversation.** Gemini Live does speech-in, reasoning, speech-out and tool calls in one session. Transcript chunks are buffered ~2s and `$push`ed onto `Call.transcript[]` as they arrive — **so a call that crashes mid-way still has partial transcript.**

**Step 7 — hangup + teardown** (`media-stream.ts:1499-1804`). In order: flush transcript buffer → compute duration → resolve outcome (priority chain, see B7) → optionally run the classifier → write `Call` (status/endedAt/duration/fullText/summary/extractedSlots/userSignals/tokenUsage) → write `outcome` in a **separate guarded update** → set `Lead.doNotCall` if DNC → if PRE_SITE, schedule reminders + WhatsApp the site-office location → optionally send brochure → mirror to `ASBLVoiceBot` → fire `call_completed` webhook.

**Step 8 — recording, asynchronously and later.** Plivo posts `Event=Hangup` to the answer URL; we wait 4 seconds and poll Plivo's Recording API, then write `Call.recordingUrl` and fire a `recording_ready` webhook. Telnyx sends `call.recording.saved`.

**Collections written, in order:** `leads` → `calls` (initiated) → `ASBLVoiceBot.call_schedules` → `calls` (in-progress) → `calls.transcript[]` (incremental) → `callstates` (ephemeral) → `calls` (completed) → `ASBLVoiceBot.call_transcripts` + `lead_profiles` + `lead_journeys` + `call_schedules` → `calls.recordingUrl` → `ASBLVoiceBot.call_recordings`.

**Link fields:** `Call.leadId → Lead._id`; `Call.roomName ↔ provider call sid` (with the Plivo caveat above); `Call.externalScheduleId ↔ the caller's own id` (**the most reliable join** — it is set at creation and never mutated); `ASBLVoiceBot.*.call_sid ↔ Call.roomName`; everything in `ASBLVoiceBot` keyed on phone in E.164 **with** a leading `+`.

### B7. What does each status/outcome value mean, and who sets it?

**[live code]**

**`Call.status`** — 4 values (`db/models/Call.ts:110-114`):

| Value | Meaning | Set by | Terminal? |
|---|---|---|---|
| `initiated` | dial request accepted, provider called | `/api/schedule-call` | no |
| `in-progress` | media WebSocket opened = **customer answered** | media-stream on WS start | no |
| `completed` | teardown ran, or provider reported a billable hangup | teardown; provider hangup webhook | **effectively yes** |
| `failed` | never connected, or was force-cleaned | provider hangup webhook; `cleanupStaleCalls` | **effectively yes** |

`status:'in-progress'` is the single most reliable "the human picked up" signal in the schema, because the WebSocket only opens on a real answer.

**`Call.outcome`** — 12 values (`Call.ts:16-29`), despite the code comment calling it "10 canonical values":

| Value | Meaning | Primary setter |
|---|---|---|
| `CONNECTED` | real two-way conversation, no more specific outcome | classifier, or catch-all default |
| `PRE_SITE` | **site visit booked** | bot's `hangup_call` tool, or classifier |
| `VIRTUAL_TOUR` | virtual tour booked | same |
| `AUTO_CALLBACK` | customer asked to be called back | classifier (also: bot's `CALLBACK_REQUESTED` is remapped here) |
| `NOT_INTERESTED` | explicit decline | bot tool / classifier |
| `DNC_REQUESTED` | customer asked us to stop calling | keyword detector only (`crm/dnc.ts`) |
| `DROPPED` | picked up, gave essentially no input | teardown heuristic |
| `VOICEMAIL` | answering machine | AMD webhook, or transcript phrase-match |
| `NO_ANSWER` / `BUSY` / `SWITCHED_OFF` / `NOT_CONNECTED` | carrier-reported failures | provider hangup webhook, or `cleanupStaleCalls` |

**Resolution priority at teardown** (`media-stream.ts:1540-1583`), highest first:
1. DNC keyword detector fired
2. AMD flagged a machine
3. the bot's own `hangup_call` tool outcome
4. "customer never spoke and call < 20s" → `DROPPED`
5. "customer said < 15 characters and call < 25s" → `DROPPED`
6. an unconditional voicemail-phrase regex over the transcript, which **overrides everything except DNC**
7. the Gemini classifier
8. default `CONNECTED`

**Can an outcome change after being set? Yes, in both directions, and this matters.**
- The final teardown write is **guarded**: `updateOne({_id, outcome: {$nin: CARRIER_TERMINAL_OUTCOMES}}, {$set:{outcome}})` (`media-stream.ts:1683-1686`). A carrier-reported failure that landed first wins and is never overwritten.
- The provider hangup webhook only sets an outcome **if `outcome` is currently null** (`index.ts:236-240`, `telnyx/webhook.ts:167-172`).
- **But `cleanupStaleCalls` has no such guard** and overwrites — see J.

So: carrier-failure outcomes are effectively terminal. `CONNECTED`/`PRE_SITE`/etc. are terminal in practice but are written *last*, so a race with a late provider webhook is theoretically possible.

### B8. Where do dial attempts that never became calls get recorded?

**[live code] — They ARE recorded, and this is good news for you, with one important asterisk.**

Every accepted `/api/schedule-call` creates a `Call` document **before** the provider is dialled. So a busy/rejected/invalid-number dial exists as a row with `status:'failed'` and an outcome of `BUSY`/`NOT_CONNECTED`/`NO_ANSWER`/`SWITCHED_OFF`, plus a mirror row in `ASBLVoiceBot.call_schedules`.

**The asterisk — and it is the reason your connect rates are probably wrong:** those rows carry `roomName = "pending-<requestUuid>"`, because `roomName` is only upgraded to the real CallUUID when the call is *answered*. **If you join on `call_sid == roomName` you drop every one of them.** Your denominator is missing precisely the failures.

**Three categories that are genuinely NOT recorded as calls:**
1. **Requests rejected before dial** — DNC-suppressed, visit-scheduled-suppressed, invalid E.164, unknown slug, rate-limited. These return HTTP 200/400/429 and **write nothing**. They are invisible. (`routes.ts` — every early `return` before the `CallModel.create`.)
2. **Night-deferred requests** (if that flag is on) — these write a `ScheduledCall` row with `kind:'night_defer'`, not a `Call` row.
3. Anything the relay decided not to send us at all.

**So: "dials the agent was asked to place" ≠ "rows in `calls`".** The gap is the suppressions. If you want the true request count you need the relay's log, not ours.

### B9. Retry / re-dial policy — file and function.

**[live code] — The agent has NO general retry policy. This surprised me and it should reframe your model of the system.**

`/api/schedule-call` places **exactly one dial per request** and never retries. There is no max-attempts-per-lead, no spacing rule, no backoff, nothing. **Re-dial cadence is owned entirely by the external relay/CRM.** If a lead was called eleven times, the relay asked eleven times.

The only retry logic anywhere in the agent is **scoped to site-visit reminder calls**: `sitevisit/scheduler.ts`, function `reapPass` — `MAX_ATTEMPTS = 2` (initial + one retry), `RETRY_DELAY_MS = 15 min`, retried only when the outcome is in the `UNANSWERED` set (`NOT_CONNECTED`, `NO_ANSWER`, `VOICEMAIL`, `DROPPED`, `BUSY`, `SWITCHED_OFF`, `FAILED`). It applies to nothing else.

**What stops a dial**, all in `crm/routes.ts` inside the `POST /api/schedule-call` handler:
- `lead.doNotCall === true` → hard block, all slugs (line 201)
- `lead.status === 'visit-scheduled'` OR future `lead.siteVisitScheduledAt` → blocks `lead_qualification_and_selling` only (line 232)
- quiet hours, if `NIGHT_DEFER_ENABLED` (line 256)
- `checkDialAllowed()` in `limits.ts` — concurrency and daily caps, **both default to 0 = unlimited**

An answered call does *not* stop future dials. A booking only stops them via the `visit-scheduled` flag.

---

# C. Fields you cannot find

### C10. Hangup cause / disconnect reason — persisted?

**[live code] — NO. It is received, used, logged, and then thrown away.**

Both providers send it and both handlers read it:
- Plivo: `HangupCause`, `HangupCauseName`, `BillDuration`, `Duration` (`index.ts:170-174`)
- Telnyx: `hangup_cause` (`telnyx/webhook.ts:155-160`)

Both map it to a coarse outcome bucket and both write **only the mapped bucket**. The raw cause string is written to stdout and to nothing else. There is no `hangupCause` field on the `Call` schema.

**What that costs you:** you cannot distinguish "customer hung up on the bot at 20 seconds" from "the network dropped the call at 20 seconds" from "the bot ended it". All three land as a completed call. For a system whose goal is attended site visits, "who ended the call and why" is close to the most valuable missing field in the schema.

**This is a small fix** — one schema field, two one-line writes. If you want it, it is worth asking for.

### C11. Answering-machine detection — enabled? Result saved?

**[live code for the mechanism, env UNKNOWN for whether it is on]**

- **Plivo:** implemented, **off by default**. `config.plivo.amdEnabled` ← `PLIVO_AMD_ENABLED` (`config.ts:153`). When on, the dial passes `machineDetection`, `machineDetectionTime` (default 4000ms), and a callback to `/api/plivo/amd`. On a machine we flag the call and hang up.
- **Telnyx:** the webhook handler for `call.machine.detection.ended` exists (`telnyx/webhook.ts:136`) and acts **only** on `result === 'machine'` — deliberately ignoring `not_sure`/`silence` so we never hang up on a real human. Whether AMD is actually *enabled on the Telnyx application* is a portal setting I cannot see.

**Is the result saved? Only transiently, then only as an effect.** It goes to `CallState.machine`, which has a **2-hour TTL**. At teardown it is read and converted into `outcome: 'VOICEMAIL'`, then explicitly cleared. **There is no durable "AMD said machine" field.**

**The consequence you must not miss:** `outcome === 'VOICEMAIL'` has **two** entirely different origins — AMD, and a regex over the transcript text (`classifier.ts:145`, applied unconditionally at `media-stream.ts:1576`). You cannot tell them apart after the fact. If you are measuring AMD effectiveness, you cannot, from this data.

### C12. Prompt version per call — recoverable for historical calls?

**[live code] — NO, and this is a genuine hole.**

- The `Call` schema has **no** prompt-version field. It stores `slug` and `agentPersonaId`, neither of which is a version.
- The `Prompt` collection stores **one document per slug**, overwritten in place. There is no version history array. `version` is a counter and `fileVersion` a string, but both describe *the current content only*.
- So for any historical call, the prompt version is not recorded anywhere in any database.

**What you can partially reconstruct it from:**
1. Git history of `agent/src/prompts/*.ts` and `agent/src/prompt.ts` — each version bump is a commit with a timestamp, and the repo convention is that a prompt body edit MUST bump its version string in the same commit.
2. ECS task-definition revision history + service deployment events (`aws ecs list-task-definitions` / `describe-services`) gives you deploy timestamps. **I cannot run these — no AWS access.**
3. `Prompt.updatedAt` on the live doc gives you the timestamp of the *most recent* push only.

**A live finding while I was checking this.** `GET https://growth-angad.asbl.in/api/prompt-version` right now returns:

```
slug:                 lead_qualification_and_selling
code_file_version:    2026-08-02-radhika-v3.8.1-facts-restore-staging
mongo_file_version:   2026-08-05-ankit-v3.8-male-v6.0.0-four-endings
mongo_updated_at:     2026-08-07T08:47:21.807Z
in_sync:              false
```

Read that carefully. **The prompt the bot is actually using is not the prompt in the deployed code.** Mongo wins at runtime, so live calls are running the Mongo copy — a *male* "Ankit" persona pushed on 2026-08-07 — while the deployed code expects a *female* "Radhika" v3.8.1. This is intentional (there is a commit "Point the production slug at the Ankit prompt"), but it means:

- **Any analysis that assumes calls before and after 2026-08-05 used the same persona is wrong.** The agent's gender, name, and closing style changed.
- Your Aug 7 anomaly (question I33) sits **one day after** this prompt push. I am not claiming causation — but if you are looking for what changed around then, this is a real, dated, live-verified change, and it is the kind of thing that would never show up in a schema-level audit.

### C13. Which field says Plivo vs Telnyx, and what decides routing?

**[live code] — There is no such field on `Call`, and the code works around its absence in at least two places. This is a real schema gap.**

**Routing decision:** `pickProvider()` in `crm/routes.ts:56-59` — strip non-digits from the destination; starts with `91` → Plivo; anything else → Telnyx. That is the entire rule. No fallback, no cost logic, no load balancing.

**How to recover the provider from data, in descending reliability:**
1. **`Call.phoneNumber` starts with `+91` → Plivo, else Telnyx.** This is exactly what the code itself decided, so it is not a proxy — it is the same rule. **Use this.**
2. `Call.roomName` shape — Telnyx call_control_ids and Plivo CallUUIDs look different (`cleanupStaleCalls` at `index.ts:773` uses a Plivo-UUID regex `^[0-9a-f]{8}-[0-9a-f]{4}-` for exactly this purpose).
3. `ASBLVoiceBot.call_transcripts.call_provider` — **the mirror DOES store it explicitly** (`legacy-mirror.ts:231`). If you need it as a field, it is there, in the legacy database, not the primary one.
4. `Call.recordingUrl` host — the code infers provider from the URL (`routes.ts:520`).

Note the schema-gap smell: `mirrorCallFinalized` is called with a **hardcoded** `provider: 'plivo'` from the Plivo media-stream (`media-stream.ts:1736`) and hardcoded `'telnyx'` from the Telnyx one. The value is correct, but it is correct by construction, not by lookup.

### C14. True latency — is there a timestamp of when audio actually started flowing?

**[live code] — Measured in detail. Persisted: none of it.**

The code instruments this thoroughly (`media-stream.ts:493-557`). Every call logs a timing report with: `ws_open`, `stream_start`, `lead_lookup_done`, `call_doc_ready`, `session_grab_start`, `session_ready`, `trigger_dispatched`, **`first_audio_to_plivo`** — that last one is exactly the "first TTS packet" timestamp you want. There is also per-turn reply latency (`media-stream.ts:740-748`) and an end-of-call summary with avg/p50/p90/max and a count of turns over 2.5s (`media-stream.ts:1503-1511`).

**All of it goes to stdout only. Not one millisecond is written to Mongo.** It exists only in container logs.

**Two things follow.** First, your proxy (first-agent-turn minus call start) is the best you can do from the database, and you are right that it is blind to calls where the agent never spoke — but note it is also **inflated by design**: `startedAt` is set when the *WebSocket opens*, and the greeting is deliberately held for up to `GREETING_PROTECT_MS = 7000ms` of barge-in protection. Second, the real numbers exist in CloudWatch logs right now, grep-able for `[timing]`. That is a much better source than anything you can compute. **I have no CloudWatch access; someone with the AWS account does.**

### C15. Recordings — storage, URL scheme, retention, and is every call recorded?

**[live code, retention UNKNOWN]**

**Not every call is recorded. Only answered ones.** Recording starts from the media-stream `start` handler (`media-stream.ts:1190`) — which only fires when the customer picks up. An unanswered dial has no recording, by construction, not by failure.

**Plivo:** recording is started by a **separate API call after connect** (`plivo/start-recording.ts`) — `POST /v1/Account/{id}/Call/{uuid}/Record/`, mp3, `time_limit=3600`. Two documented gotchas live in that file's comments and both are real historical bugs: `record:true` on the create-call API is **silently ignored** by Plivo, and `time_limit` **defaults to 60 seconds** if omitted, which truncated every recording to one minute for a period. If you have old recordings that are all exactly ~60s, that is why.

**Telnyx:** recording is configured at the **Outbound Voice Profile** level in the portal, so no per-call command is issued for outbound; inbound issues `record_start` explicitly. Dual-channel (recent commit "Record Telnyx calls in dual channel").

**URLs:** Plivo URLs live on Plivo's media host and **require HTTP Basic auth with our account credentials** — they are not publicly fetchable. Telnyx `public_recording_urls.mp3` are public. Because of that asymmetry the agent exposes its own proxy: `GET /api/calls/:call_id/recording-audio`, reachable either with the Bearer token or with a **signed, expiring link** that `recording_ready` webhooks hand out (`crm/signed-url.ts`). Provider is inferred from the URL string to decide whether to attach credentials.

**Retention: UNKNOWN.** It is a Plivo/Telnyx account setting, not a code setting. I have no portal access. **Ask whoever holds the provider accounts** — and ask soon, because if there is a retention window, the recordings are the only ground truth for the transcript-accuracy problem in H29.

**One known trap:** an older note in the project's own docs records that Telnyx recording URLs were saved as **S3 presigned URLs with a 10-minute expiry**, making them undownloadable afterwards. Recent commits (`902f0f8`, `a3d00dc`, `df93466`) address exactly this. **Older Telnyx `recordingUrl` values in your data may be permanently dead links.** Test one before you build anything on Telnyx recordings.

---

# D. Scheduling, follow-ups, and the 89% problem

### D16. Confirm the exact condition that causes the planner to schedule nothing.

**[live code] — Confirmed. Your finding is correct, and here is the precise mechanism.**

The file is still `agent/src/sitevisit/planner.ts`. Two functions matter.

**`planSiteVisitCalls(...)`** (`planner.ts:28-61`) — look at the control flow:

```
const imminent = visitAt != null && visitAt - now <= 1 hour;
if (imminent && visitAt)      { ...push 15m reminder... }
else if (visitAt)             { ...push 12h and 1h reminders... }
return out;   //  ← if visitAt is null, NEITHER branch runs. Returns [].
```

**There is no `else`. If `visitAt` is null, the function returns an empty array and nothing is ever scheduled.** No log, no warning, no fallback. `scheduleSiteVisitCallsForCall` then logs `"nothing to schedule (visit past/imminent)"` — **a message that is actively misleading, because the overwhelmingly common cause is neither "past" nor "imminent", it is "unparseable"** (`schedule-on-presite.ts:35-38`). If you were grepping logs for the cause, that line would have sent you the wrong way.

**Where `visitAt` comes from** — `parseVisitDateTime(req?.date, req?.slot)` where `req = call.extractedSlots.site_visit_requested` (`schedule-on-presite.ts:24,31`). It returns null when:
- `date` is missing/empty/null — **`if (!date) return null`** (`planner.ts:77`), or
- `date` does not match `^(\d{4})-(\d{2})-(\d{2})` — i.e. **anything that is not a strict ISO `YYYY-MM-DD` prefix returns null** (`planner.ts:78-79`).

**The `slot` is NOT the problem.** If `slot` is missing or garbled, the parser defaults to **10:00 IST** and carries on (`planner.ts:81-89`). Only the *date* is fatal.

**Where the date is supposed to come from:** the post-call Gemini classifier, which is instructed to emit `site_visit_requested: {date: "YYYY-MM-DD", slot: "HH:MM AM - HH:MM AM"}` (`classifier.ts:113`) — merged with anything the in-call `schedule_site_visit` tool already wrote, tool-written values winning (`mergeExtractedSlots`, `classifier.ts:30-54`). Neither Zoho nor manual entry feeds it.

**So the 89% breaks down into exactly three failure modes, and you can separate them in your data:**
1. **`extractedSlots.site_visit_requested` is null entirely** — the classifier never extracted a booking, or never ran.
2. **`site_visit_requested.date` is free text** — "Saturday", "this weekend", "kal", "next week". The bot heard a real booking; the classifier failed to resolve it to a calendar date. **I would expect this to be the largest bucket**, because it is exactly what customers actually say.
3. `date` is ISO but the derived reminder times were already in the past.

Query 1 and 2 separately — the fix is different for each. #2 is a classifier-prompt problem (it already does relative-date resolution for `preferred_callback_time` using `CURRENT_TIME_IST`, but the `site_visit_requested.date` instruction carries **no equivalent "resolve to the next future occurrence" rule** — that asymmetry in `classifier.ts:133` vs `:113` looks like the single highest-leverage fix in this whole document). #1 is a detection problem.

**And one more gate you must check before any of this matters.** The entire path is wrapped in:

```
if (process.env.SITEVISIT_SCHEDULER_ENABLED !== 'true') return;
```

at `schedule-on-presite.ts:19` **and again** at `scheduler.ts:189`. **If that env var is not `'true'` in production, zero reminders are scheduled for anyone, and the parsing question is entirely moot.** I cannot read the env. **You can settle it in one query:** `db.scheduledcalls.count({kind: 'sitevisit'})` on growth-prod. If it is zero or frozen at an old date, the feature is off and your 89% is really 100% with a different cause.

### D17. What reminder schedule is live? And what fired the 5 zombie confirmation calls after Jul 17?

**[live code + git history]**

**Confirmed, all three, from the current source:**
- 12h before visit — yes (`planner.ts:50-53`)
- 1h before visit — yes (`planner.ts:54-57`)
- single 15m reminder if the visit is within the next hour — yes (`planner.ts:42-47`)
- **the +30m post-booking confirmation call was removed** — yes. Commit `b08fa02`, **2026-07-17 22:17:13 +0530**, "feat(scheduling): remove +30m site-visit confirmation call + add Indian night-defer guard". Your date is exact. The code comment gives the reason: it re-dialled leads immediately after they booked, and retried on busy/dropped, which read as harassment.

Past-dated reminders are dropped individually (`> now` check on each), so a booking made less than 12h before the visit correctly yields only the 1h reminder.

**The 5 zombie `confirmation-` slug calls after Jul 17 — here is your answer.** Removing the confirmation call from the *planner* did **not** remove the `site_visit_confirmation` **slug**. It is still a fully registered, fully valid, dialable prompt: it is in `PROMPT_SLUGS` (`prompts/index.ts`), it passes `isValidSlug()`, and `POST /api/schedule-call` will happily accept and place it. The type `SiteVisitSlug` in `planner.ts:17` still lists it even though the planner can no longer emit it — dead-but-loaded, which is precisely how something like this comes back.

So: **the agent's own planner did not fire those 5 calls. Something external asked for them by name** — the Zoho relay, a Zoho workflow, or a human using the dashboard's dispatch/redial UI.

**Confirm it in one query.** Look at those 5 calls' `externalScheduleId`:
- starts with **`sched-`** → the agent's own site-visit poller (would contradict my reading — tell me if so)
- starts with **`dash`** → dashboard-initiated, i.e. a human
- **anything else** → the external CRM/relay asked for it

That prefix convention is set in `routes.ts:325` and `scheduler.ts:53-56` and it cleanly separates the three origins. It is a good general-purpose tool for you.

### D18. `ASBLVoiceBot.call_schedules` (~45k rows) — what writes it, reads it, what do the statuses mean?

**[live code] — It is a HISTORICAL LOG, not a queue. And its `status` field will mislead you.**

**Written by exactly two functions, both in `db/legacy-mirror.ts`:**
1. `mirrorSchedule()` — fired at **dial time** from `/api/schedule-call` (`routes.ts:379`). Upserts on `external_schedule_id`.
2. `mirrorCallFinalized()` — at teardown, patches the same row with the final outcome (`legacy-mirror.ts:299-311`).

**Read by:** nothing in this repo. No agent code, no dashboard code queries it. It is written purely for external legacy consumers.

**The status trap — please flag this internally.** `mirrorSchedule` hardcodes **`status: 'called'` at INSERT time** (`legacy-mirror.ts:432`), before the phone has even rung. It is then overwritten at teardown to `'no_answer'` if the outcome is `NO_ANSWER`/`BUSY`/`NOT_CONNECTED`, else `'called'`.

Consequences:
- **`status: 'called'` does NOT mean the call was answered.** It means either "a dial was requested" or "a dial completed non-failingly" — indistinguishable.
- If the teardown never runs (crash, redeploy mid-call, mirror error), the row is **permanently stuck at `'called'`** with no outcome. Silent.
- `VOICEMAIL` and `SWITCHED_OFF` are **not** in the `'no_answer'` list — an unreachable phone lands as `'called'`.

**Read `call_outcome` on that collection, never `status`.** Better still, read `angad.calls.outcome`.

Also note: this collection is a **dial log, one row per dial request**, keyed on `external_schedule_id`. ~45k rows is consistent with ~45k dial requests over the system's life. It is not a queue and nothing polls it.

### D19. The dialling time window — where is the gate, what timezone, and are the ~10.3k 2am/5am dials a bug?

**[live code, env UNKNOWN] — This is the urgent one, so I will be precise about what I know and what I don't.**

**There are THREE separate time gates. All three are opt-in and all three default to OFF.** That is the finding.

| Gate | File | Env var | Default | Scope |
|---|---|---|---|---|
| Night-defer (quiet hours) | `night-window.ts` + `routes.ts:256` | `NIGHT_DEFER_ENABLED` | **off** | `/api/schedule-call`, **Plivo/+91 only**, non-site-visit slugs only |
| General calling window | `limits.ts` `checkDialAllowed()` | `CALL_WINDOW` | **off (unset)** | all dial paths |
| Site-visit poller window | `sitevisit/scheduler.ts:45-51` | `SITEVISIT_CALL_WINDOW` | `8-21` | site-visit reminder dials only |

**Answering your three hypotheses directly:**

- **Is the gate checking UTC by mistake? NO.** I checked the arithmetic carefully and it is correct. `night-window.ts:22-24` shifts by exactly `(5*60+30)*60*1000` and then reads `getUTCHours()` on the shifted Date — the standard, server-timezone-independent way to get IST wall-clock. Quiet hours are `h >= 21 || h < 6`, correctly handling midnight wrap. `limits.ts:17-20` and `scheduler.ts:39-43` use the identical pattern. **The timezone logic is right. This is not a timezone bug.**

- **Is it skipped on some paths? YES, on several, and this is the real story:**
  - `POST /api/calls/initiate` — the legacy/dashboard dial path — has **no night check at all**. It calls `checkDialAllowed()` (off by default) and a DNC check, and dials. `index.ts:530-571`.
  - The night guard covers **only Plivo/+91**. `provider === 'plivo'` is an explicit condition (`routes.ts:258`).
  - **Site-visit slugs are explicitly exempt** from the night guard (`!isSiteVisitCall`, `routes.ts:259`) on the reasoning that their own 8–21 poller window covers them — which it does *for poller-initiated dials*, but **not** for a site-visit dial the relay requests directly.
  - Retries: the site-visit `+15min` retry sets `status:'pending'` and `dueAt: now+15min`; the next `dialPass` re-checks `withinCallWindow`, so retries **are** gated. Night-deferred replays go through `night-defer-scheduler.ts` into the morning window. So retries specifically are not the leak.

- **Are they international numbers? Partly plausible — and testable.** The night guard is +91-only by design. Any non-`+91` dial bypasses it entirely regardless of the flag. **Check `Call.phoneNumber` prefix on your 10.3k out-of-window dials.** If they are `+91`, the flag is off (or they came via `/api/calls/initiate`). If they are not `+91`, they were never gated by design, and 2am IST may be a perfectly reasonable local hour for the destination — which would substantially reduce the compliance exposure.

**My assessment, stated as an assessment and not a fact:** the most likely single explanation is that **`NIGHT_DEFER_ENABLED` is not set to `true` in the production ECS task**, so the guard that exists has never run. It was added on 2026-07-17 as opt-in specifically so it could not change prod behaviour on merge — and opt-in features that are never opted into are one of the most common ways a shipped safeguard does nothing.

**Three queries settle this completely, all runnable on your read-only connection:**
1. `db.scheduledcalls.count({kind: 'night_defer'})` — **if this is 0, the night guard has never fired once, and it is off.** This is the decisive test.
2. On the out-of-window calls: does `phoneNumber` start with `+91`?
3. On the out-of-window calls: what is the `externalScheduleId` prefix (`dash` / `sched-` / other)? That tells you which door they came through.

**And the thing I most want to say plainly:** TRAI compliance for call timing is currently **not enforced by the voice agent at all in the default configuration**. `limits.ts` says so in its own comment — *"Production relies on the CRM (Zoho) for call-timing rules, so it leaves CALL_WINDOW UNSET"*. That is an explicit, documented decision to delegate compliance to a system outside this repo. If the relay is not enforcing it either, then **nobody is**, and 10.3k out-of-window dials is exactly what that looks like. Setting `CALL_WINDOW=8-21` on the prod task is a one-variable change that would close every path at once, since every dial path funnels through `checkDialAllowed()`. That is a decision for whoever owns the prod environment, not for me — but the mechanism is already built and tested.

---

# E. Identity

### E20. MASTERLEADID (6-digit, Inncircles) — who assigns it, can one human get two?

**[UNKNOWN — not in this repo]** Zero references to `MASTERLEADID`, `master_lead_id`, or Inncircles anywhere in the agent or dashboard source.

The agent's own identity key is **`Lead.phone`**, E.164 with a leading `+`, with a **unique index** (`db/models/Lead.ts`). One phone = one Lead document, enforced at the database level. `Lead` documents are created lazily on first dial or first inbound call.

### E21. Source Lead ID vs Zoho lead id vs lead_ref/phone — define each keyspace.

**[live code, for the four this repo actually handles]**

| Key | Owner | Where it lives here | Notes |
|---|---|---|---|
| `phone` (E.164, `+`) | **the agent** | `Lead.phone` (unique), `Call.phoneNumber` | the agent's own primary key |
| `external_schedule_id` | **the caller** (relay/dashboard/poller) | `Call.externalScheduleId` (indexed) | **one per dial**, not per lead. Set at creation, never mutated → the most reliable join key in the system |
| `external_customer_id` | the caller | `Call.externalCustomerId` (indexed) | pass-through; the agent never interprets it |
| `source_lead_id` | the caller | folded into `Call.metadata.source_lead_id` (`routes.ts:307-309`) | accepted top-level or in metadata; top-level is folded in so there is one place to read it |
| `zoho_lead_id` | the caller | `Call.metadata` | echoed back on `call_completed` and `recording_ready` (commits `46d5d6b`, `7caaac6`) |

The agent is a **pass-through** for all four external ids: it stores them, echoes them on every webhook, and never resolves, normalises, or dedupes them.

**On your current bridge** via `call_transcript_intent_identification.master_lead_id` and `Zoho_Database.reactivation_list` — I cannot evaluate it, since neither collection exists in this codebase's world. **But there may be a better path you have not tried:** `Call.metadata.source_lead_id` and `Call.metadata.zoho_lead_id` are populated directly by whoever requested the dial, on the agent's own `calls` collection. If the relay is passing them (and recent commits suggest it is), that is a **first-party** join from a call straight to a Zoho lead id, with no intermediate collection and no inference. **Check coverage first** — `db.calls.count({'metadata.source_lead_id': {$exists: true}})` — because it will be zero for calls placed before the relay started sending it.

### E22. How do leads enter from Zoho? What dedupes them? What does one phone across multiple lead rows mean?

**[live code for our side; the Zoho ingest is UNKNOWN]**

**Into the agent, leads enter exactly one way: as a side effect of a dial request.** There is no lead import, no poll, no sync, no CSV ingest into `angad.leads`. `/api/schedule-call` does `LeadModel.findOne({phone})` and creates one if absent (`routes.ts:182-194`). Inbound calls do the same in the media-stream. That is the whole ingest.

**Dedupe:** the unique index on `Lead.phone`. In `angad.leads`, **one phone can never be two rows.**

Therefore: **if you are seeing the same phone as multiple lead rows, you are looking at a Zoho/CRM collection, not at `angad.leads`.** The person×project multiplicity you describe is a CRM-side modelling choice. On the agent side it collapses to one Lead — with a consequence worth stating: `Lead.propertyInterested` holds **one** project and is **overwritten**, so a human interested in two projects loses that distinction in the agent's store. The per-call project survives in `Call.metadata.project`. **Read project from the call, not the lead.**

Also note a small but real data-quality trap in the same block: when creating a Lead the code defaults `propertyInterested` to the literal string **`'ASBL Loft'`** if no project was passed (`routes.ts:188-189`). **Some fraction of "Loft-interested" leads are simply leads whose project was never specified.** Do not read that field as expressed interest.

### E23. Where is the DNC list, what adds to it, which paths check it, and were the 308 post-DNC dials fixed?

**[live code]**

**There is no DNC *list* collection.** It is a boolean on the lead: `Lead.doNotCall` (indexed), plus `Lead.doNotCallReason`. Your count of 210 should equal `db.leads.count({doNotCall: true})`.

**What sets it — one code path, at teardown:**
```js
if (finalOutcome === 'DNC_REQUESTED' || dncTriggered) {
  LeadModel.updateOne({phone: dncPhone}, {$set: {doNotCall: true, doNotCallReason: 'Customer requested DNC on call'}})
}
```
(`media-stream.ts:1693-1701`; mirrored in the Telnyx stream.) `dncTriggered` is set only by the regex detector in `crm/dnc.ts` — 16 patterns across English and Hindi/Hinglish. The dashboard also has a `POST /api/block/[phone]` route.

**What checks it before dialling — both outbound paths:**
- `/api/schedule-call` → `routes.ts:201`, hard block, all slugs, returns `DNC_SUPPRESSED`
- `/api/calls/initiate` → `index.ts:539-550`, same

**Was the 308-dial bug fixed? Yes, and here is the exact mechanism.** The original bug: `DNC_REQUESTED` was recorded **on the Call document only**; `Lead.doNotCall` was never set. Since the pre-dial guard reads the *Lead* flag, not call history, a customer who said "stop calling" was fully re-dialable the next minute. The code comment names a real case — DNC at 2:18pm followed by 7 more calls. The fix set `Lead.doNotCall` at teardown (the block above), and the project log records **55 affected leads backfilled**.

**Two caveats that keep this from being airtight:**
1. **The guard is Lead-flag-based, not history-based.** Any dial placed *before* the flag write lands is not suppressed, and the write happens at teardown — so a re-dial requested during the same call's teardown window can still slip through.
2. **The regex is the only detector.** A customer who declines in Telugu, or phrases it in a way not in those 16 patterns, never trips DNC. `DNC_REQUESTED` measures *the regex*, not *customer intent*.

**To verify the fix held:** for each lead with `doNotCall: true`, find the earliest `DNC_REQUESTED` call, then count calls to that phone after it. Post-fix that should be zero or near-zero; the 308 should all predate the fix. If you find recent ones, tell me — that would be a live compliance issue and I would want to look again.

---

# F. Site visits and attendance

### F24. Where is BOOKED / CONFIRMED / ATTENDED recorded? Is there really no system of record for attendance?

**[live code] — Confirmed. There is no attendance anywhere in this system, and I want to be blunt about how complete that gap is.**

| Stage | Recorded? | Where |
|---|---|---|
| **BOOKED** | yes | `Call.outcome = 'PRE_SITE'`; `Call.extractedSlots.site_visit_requested`; `Lead.status = 'visit-scheduled'`; `Lead.siteVisitScheduledAt`; `ASBLVoiceBot.lead_profiles.site_visit_agreed` / `ready_to_book` |
| **CONFIRMED** | **no** | there is no confirmed state. `ScheduledCall.status` tracks *whether we managed to place a reminder call*, not whether the customer confirmed anything. `status:'done'` means "the reminder call was answered" — it does **not** mean the customer said yes. |
| **ATTENDED** | **no** | nothing. No field, no collection, no event, no webhook, no code path. |

I searched for every plausible spelling — `visit_attended`, `attended`, `visit_done`, `checkin`, `check_in`, `showed`, `no_show`. **Zero hits in the agent and zero in the dashboard.**

The agent has no inbound channel through which attendance *could* arrive. There is no "site visit happened" endpoint. Even if Compass or Inncircles recorded check-ins digitally, **nothing in this system is wired to receive that signal** — I cannot tell you whether they record it, but I can tell you with certainty it never reaches here.

**So your conclusion is right, and stronger than you stated it: attendance is not merely un-populated, it is un-architected.** Your `visit_attended` field being constantly false in the Zoho export is consistent with a field nobody writes.

**This is the finding I would put in front of the owner first.** The stated goal of the entire system is *attended* site visits. The system cannot observe its own goal. Every metric currently produced — including every optimisation the CI Team or I might propose — is measured against **bookings**, a proxy that is one full step removed from the outcome, and a proxy that a sufficiently pushy sales agent can inflate without moving the real number at all. Until attendance is captured somewhere, "did that change help?" is structurally unanswerable, and there is a real risk of optimising hard in the wrong direction.

Closing it does not need a big build: one endpoint on the agent, or one field the ops sheet writes back to `Lead`, would make the whole funnel measurable. The ops sheets you mention already hold the data — it just needs to land in a database.

### F25. What does `Intelligent_CRM.site_visits` contain and what writes it?

**[UNKNOWN — not in this repo]** No code here reads or writes it. **Nothing in the agent can be its writer**, so its source is the relay or a Zoho workflow.

### F26. When a booking is made on-call, what exactly gets written, and which fields are free text?

**[live code] — This directly determines whether fixing the date parser is enough. Short answer: it fixes the reminders, and nothing else.**

Written at teardown on `PRE_SITE`:

**On the `Call` document:**

| Field | Type | Structured or free text? |
|---|---|---|
| `outcome` | enum | **structured** |
| `extractedSlots.site_visit_requested.date` | string | **FREE TEXT.** Schema type is `String` with no format validation. The classifier is *asked* for `YYYY-MM-DD` and often returns something else. **This is the 89% problem.** |
| `extractedSlots.site_visit_requested.slot` | string | **free text** ("11:00 AM - 12:00 PM" requested; parser tolerates many shapes; defaults to 10:00) |
| `extractedSlots.site_visit_date` | string | flat mirror of `.date` — same free text |
| `summary` | string | free text |

**On the `Lead`:** `status = 'visit-scheduled'` (structured enum) and `siteVisitScheduledAt` (**a real `Date`, but set to `visitAt ?? null` — so it is null in exactly the 89% of cases where parsing failed**, `schedule-on-presite.ts:80`).

**Project, attendees, venue:** these do **not** come from the conversation. They are copied from the *inbound dial request's* `metadata` (`schedule-on-presite.ts:46-59`): `project`, `venue`, `party_size` (defaulting to `1`), `visit_notes`, `contact_number`. **If the relay did not send them, they are empty or defaulted.** The bot does not capture attendee count on the call.

**So, to answer the underlying question you were really asking:** fixing the visit-time parsing **is** enough to make reminders fire, and it would also un-null `siteVisitScheduledAt` (which independently improves the re-dial suppression in B9/E23 — right now a vague booking leaves that null and the system falls back to the `status` check). **But it does not give you a structured booking record.** There is still no venue captured from the call, no attendee count, and — per F24 — no attendance. If the goal is attended visits, the parser fix is necessary and nowhere near sufficient.

---

# G. WhatsApp

### G27. The 19-number sender pool, 28,309 sends, all 401. Current state? Does the code treat 401 as success?

**[live code] — Taking the second half first, because it is the load-bearing claim: no, this code does not treat 401 as success. Anywhere. I checked the whole path.**

`whatsapp/periskope.ts:97`:
```js
if (status >= 200 && status < 300 && json?.success !== false) return { ok: true, ... };
...
if (status === 401 || status === 429) break;   // try the next sender, do not retry this one
```
A 401 returns `{ ok: false }`. And `whatsapp/send-brochure.ts:110-123` sets the `Call.whatsappSent` flag **only if at least one document actually returned `ok: true`**:
```js
const sent = results.filter(r => r.ok).map(r => r.label);
if (sent.length > 0 && opts.callDocId) { ...set whatsappSent: true... }
```
**So a 401 leaves `Call.whatsappSent` false and `whatsappDocs` empty.** If your "sent" metric comes from `angad.calls.whatsappSent`, it is honest and it is not inflated by 401s.

**Which means the 28,309 "sends" almost certainly are not the agent's.** Two independent reasons: (a) the flag can't be set by a 401, and (b) **the agent sends at most one brochure per call, deduped** — 28,309 sends would require ~28,000 successful brochure-consenting calls, which is far more than the system's total call volume. **Something else sent those.** Please find which collection holds them; if it is a relay/Zoho collection, this is not our code path and my analysis does not apply to it. I would genuinely like to know the answer, because if there *is* a second WhatsApp sender in the estate, that changes the picture.

**Provider: Periskope**, not WATI, not Meta Cloud API directly. Org-level Bearer key `PERISKOPE_API_KEY`; the sender pool is `PERISKOPE_SENDERS`, a comma-separated list of WhatsApp numbers sent as an `x-phone` header (`config.ts:200-206`). The code's own comment says 401 means **"phone server switched off"** — i.e. a 401 here is not really an auth failure, it is *that particular WhatsApp sender being offline*. On 401 or 429 it moves to the next sender in the pool. There is also a **sticky sender per customer** (`wa_senders` collection) so the same customer always gets messages from the same number.

**A whole pool returning 401 therefore reads as: every sender number was offline/deregistered, or the API key is invalid.** Both are configuration states, not code bugs.

**Are any senders alive today? [UNKNOWN]** — that needs `PERISKOPE_API_KEY` and the Periskope dashboard, neither of which I have. **How to check from data:** `db.calls.count({whatsappSent: true, whatsappSentAt: {$gte: <recent>}})`. Any recent non-zero means at least one sender is alive.

**One design note worth flagging:** WhatsApp send failures are **fire-and-forget** (`void sendBrochurePostCall(...).catch(...)` at `media-stream.ts:1718-1728`). A total pool outage produces log lines and nothing else — no alert, no retry, no queue. That is how 28,309 failures can happen without anyone noticing, whoever's code path it was.

### G28. Are inbound WhatsApp replies captured anywhere?

**[live code] — No. The agent is send-only.** There is no inbound webhook, no message store, no `wa_messages` collection. The only WhatsApp-related persistence is `wa_senders` (the sticky sender map, phone → sender number) and the per-call `whatsappSent` / `whatsappSentAt` / `whatsappProject` / `whatsappDocs` flags.

If a richer inbound store exists, it is on the Periskope side (their platform holds the conversation) or relay-side. Your hot-reply queue may well be the only structured view of inbound that exists — which, given a customer replying to a brochure is about the warmest signal in the whole funnel, is worth saying out loud.

---

# H. Transcripts and the Gemini session

### H29. Confirm the mechanism behind garbled customer turns. Is it Gemini's conversation memory rather than ASR? Is raw ASR saved anywhere?

**[live code] — Close, but let me correct one detail, because it changes what the fix is.**

It is **not** conversation memory. It is a real transcription — but it is the **Gemini Live native-audio model's own side-channel transcription**, not a dedicated ASR:

```js
inputAudioTranscription: {...},   // ← customer turns come from here
outputAudioTranscription: {},     // ← bot turns come from here
```
(`gemini/client.ts:536-542`; consumed at `:701-708` and pushed to `Call.transcript[]`.)

The distinction matters: it *is* derived from the customer's audio, so it is not hallucinated from context. But it is produced by a model optimised for **conversational response**, not transcription fidelity, and it is a by-product of understanding rather than the output of a transcription-tuned system. **The codebase says so, in its own words, twice:**

- `config.ts:126-128`: *"the Gemini Live native-audio side-transcript is weak on 8 kHz Indian multilingual phone audio"*
- `transcribe-recording.ts:4-6`: *"weak on 8 kHz Indian multilingual phone audio (**<50% accurate**)"*

**Your measured median divergence of 0.79 against audio truth is consistent with the developers' own <50% figure. This is a known, documented, quantified limitation — not a surprise, and not something you need to convince anyone of.**

**Is there better transcription? Yes — and it may be sitting there switched off.** `transcribe-recording.ts` re-transcribes the **full recording** after the call using a batch Gemini model with a proper diarization prompt and a hand-built domain vocabulary (every ASBL project, every Hyderabad locality, trade terms — added because "Gachibowli" was coming back as "Ghatkopar", a Mumbai suburb, which then told the sales team the customer was in the wrong city). It writes `Call.accurateTranscript` **and overwrites `Call.fullText`**, and stamps `Call.transcriptSource` with the model name.

**It is gated by `TRANSCRIBE_POSTCALL`, default `false`.** `db.calls.count({accurateTranscript: {$exists: true}})` tells you instantly whether it is on. **If it is off, turning it on is probably the cheapest single accuracy win available to you** — it is fully async, has zero impact on the live call, and the code is already written and shipped.

**Three consequences for your analysis, and please carry all three:**
1. **`Call.transcript[]` (the turn array) is ALWAYS the weak Live transcript.** Batch re-transcription never touches it. Check `transcriptSource` before trusting `fullText`.
2. **`fullText` and `transcript[]` can disagree** on the same document — different sources, and only one gets replaced.
3. **The classifier runs on the WEAK transcript, always.** Classification happens at teardown; re-transcription happens later, when the recording arrives. So **every outcome, every extracted slot, and every site-visit date was decided from a <50%-accurate transcript — including the ones feeding the 89% problem in D16.** These two findings are not independent. Improving transcript quality may move the booking-detection rate on its own.

### H30. Confirm ENGAGEMENT STATE blocks are internal, and list other internal markers in transcript text.

**[live code] — Confirmed, with a caveat you will want.**

`[ENGAGEMENT STATE: elapsed=… · customer_questions=… · visit_gate=HOLD|OPEN]` was a **silent instruction line injected into the model's context** to police when the bot may propose a site visit. It was **never intended to be spoken**.

**But it was spoken, on a live call.** The code comment at `media-stream.ts:598-605` is unusually candid:

> *"There used to be a forced-turn `[ENGAGEMENT STATE ... visit_gate=HOLD]` instruction injected mid-call to police the visit ask. **It leaked its own bracketed text straight into the bot's speech on a live call**, and it cost a full model turn each time. The prompt now carries the gate itself, so the injection is gone rather than merely disabled — a disabled-by-slug version silently switched itself back on the moment the production slug changed."*

**So: if you see ENGAGEMENT STATE text inside a transcript, it is internal — but it may genuinely have been spoken aloud to the customer** on calls from the leak period. It is not dialogue in the sense of "something the customer said", but it is not always purely invisible either. Treat it as a **date-bounded artefact**: the injection has been removed from the current code, so its presence dates a call to before that removal. `prompt.ts:35` and `:115` still *reference* the line (telling the model to trust it), which is now stale — the prompt describes a mechanism the code no longer provides.

**Other internal markers that can appear inside transcript text:**

1. **Leaked tool calls — the big one.** The production model (`gemini-3.1-flash-live-preview`) is a *cascaded* Live model that sometimes **speaks a tool call aloud** instead of emitting a structured function call — literally saying `hangup_call{outcome: PRE_SITE}` or a bare `outcome: CALLBACK_REQUESTED`. There is a dedicated strip-net (`gemini/tool-leak.ts`) with `looksLikeToolLeak` / `parseLeakedHangup` / `stripLeakedToolCalls` (applied at `media-stream.ts:812-824`). **When the whole chunk is tool syntax, nothing is recorded at all** (`if (!clean) return`) — so leaked turns can vanish from the transcript entirely, leaving a gap. Anything resembling `hangup_call`, `save_qualification`, `schedule_site_visit`, `log_objection`, `save_buying_signal`, `calculate_emi`, `send_whatsapp_details`, or a bare `outcome: <ENUM>` is a tool artefact, not speech.
2. **`[inaudible]`** — emitted by the batch transcriber for genuinely unclear audio (`transcribe-recording.ts:52`). Only in `accurateTranscript`/`fullText`, never in `transcript[]`.
3. **`[no conversation]`** — the batch transcriber's sentinel for silence/ringing/IVR. It is filtered out before writing (`:132-141`), so it should not persist — but if you see it, that call had no human conversation.
4. **`Bot:` / `Customer:` prefixes** — `fullText` is a flattened rendering, not raw speech. The legacy mirror uses a **different** convention: the bot's *name* ("Radhika", from `LEGACY_BOT_NAME`) and the customer's *phone number* as speaker labels, with UTC 12-hour timestamps appended (`legacy-mirror.ts:203-212`). **Same conversation, two incompatible formats in two databases.**
5. **`DNC_GOODBYE_INSTRUCTION`** — an English instruction string injected when DNC fires (`crm/dnc.ts:60-65`). Injected as instruction, not speech, but the same leak class applies.
6. **Reconnect context injection** — on a mid-call reconnect the last ~6 turns are re-injected as `"Bot: …\nCustomer: …"` (`media-stream.ts:662-679`). If *that* ever leaks, you would see duplicated earlier dialogue.

### H31. Which Gemini model/config is live, where configured, when did it last change?

**[live code for defaults; the running env is UNKNOWN]**

Code defaults, all in `agent/src/config.ts`, all env-overridable:

| Setting | Env var | Code default |
|---|---|---|
| Live model (voice) | `GEMINI_LIVE_MODEL` | `gemini-3.1-flash-live-preview` (`:64`) |
| Text-mode Live model | `GEMINI_LIVE_TEXT_MODEL` | `gemini-3.1-flash-live-preview` (`:88`) |
| Post-call classifier | `GEMINI_CLASSIFIER_MODEL` | `gemini-2.5-flash` (`:93`) |
| Batch transcriber | `GEMINI_TRANSCRIBE_MODEL` | `gemini-3.5-flash` (`:132`) |
| Voice | `GEMINI_VOICE` | **`Achird`** (`:98`) |
| Temperature | `GEMINI_TEMPERATURE` | **`0.7`** (`:115`) |
| Language code | `GEMINI_LANGUAGE_CODE` | `en-IN` (`:114`) |
| End-of-turn VAD silence | `GEMINI_VAD_SILENCE_MS` | `700` (`:103`) |
| Output gain | `GEMINI_OUTPUT_GAIN` | `0.65` (`:121`) |

Session setup is at `gemini/client.ts:445-563`: `responseModalities: [AUDIO]`, `contextWindowCompression: { slidingWindow: {} }`, session resumption enabled.

**Three things worth flagging.**

**(a) `temperature: 0.7` on a sales conversation is high.** That is real run-to-run variability in what the bot says — relevant if you are trying to attribute outcome differences to prompt changes. Note the *classifier* correctly uses `temperature: 0` (`classifier.ts:220`), so slot extraction is at least deterministic given a transcript.

**(b) The model choice is deliberate and has a known cost.** The comment at `config.ts:60-64` states plainly that `gemini-3.1-flash-live-preview` is a **cascaded** Live model chosen by the owner for quality/latency, that it **can verbalize tool calls**, and that the leak is handled by the strip-net rather than by switching models. So H30's tool-leak artefacts are an accepted trade-off, not an unknown bug. Earlier project notes record the opposite choice (native-audio, silent tools) — **the code default has since been changed to match the deployed env, deliberately, to remove drift.** Treat any older documentation saying "native audio" as superseded.

**(c) `Achird` is a male voice.** For most of this system's life the agent was "Radhika", a female persona — and a male default voice was previously logged as a bug. It is now consistent with the live prompt being the **male "Ankit"** persona (see C12). Fine today; but it means **voice gender changed at some point in your data**, and if the env sets `GEMINI_VOICE` explicitly it may have changed more than once. `db.calls.distinct('slug')` plus call dates is the closest handle you have.

**When did it last change? [UNKNOWN]** — that is ECS task-definition revision history, which needs AWS access. What I *can* date precisely is the **prompt** change: Mongo `updatedAt = 2026-08-07T08:47:21.807Z`, and the pushed version string is dated `2026-08-05`.

---

# I. Cost, throughput, operations

### I32. Rate card.

**[docs/memory — flagging clearly, these are NOT from live billing]**

Nothing in the agent source hardcodes a rate card. `web/lib/utils/cost.ts` is the single source of pricing for the dashboard; the agent only records raw token counts. From project documentation, **treat as approximate and verify against actual invoices**:

- Gemini Live (`gemini-3.1-flash-live-preview`), per 1M tokens: audio in **$3**, audio out **$12**, text in **$0.75**, text out **$4.50**
- Plivo: **~$0.0075/min**
- Telnyx: **UNKNOWN** — rates are per-destination on the Outbound Voice Profile. `config.ts:214-216` mentions a **$20/day cap** configured on that profile, which is worth knowing for a different reason: **it is a throughput ceiling, and hitting it would silently stop international calls.**
- USD→INR via `USD_INR_RATE` (documented as ~96)
- Documented blended: **~₹4/min, ~₹2.9/call**

**The single most important cost fact, and it is counter-intuitive:** the dominant driver is **not** talk time. It is the **~35K-token system prompt, billed as text input on every single call** — roughly **₹2.5 fixed per call, before anyone says a word**. The live prompt is currently **117,703 characters** (from `/api/prompt-version`).

**Two consequences for your ₹-per-outcome tile:**
1. **Cost per call is closer to flat than proportional to duration.** A 20-second no-answer and a 4-minute conversation cost far more similarly than intuition suggests. Short calls look expensive *per minute* purely because of this fixed cost — do not read that as inefficiency.
2. **Therefore ₹-per-outcome is driven almost entirely by dial volume and connect rate, not by call length.** Reducing wasted dials beats shortening calls.

**You do not need my rate card.** `Call.tokenUsage` records exact per-call token counts split by modality (`total`, `prompt`, `response`, `audioInput`, `textInput`, `audioOutput`, `textOutput`, `updates`). Multiply by whatever rates finance confirms and you have real per-call cost — no estimation. **That is the number to put on the tile.**

### I33. Aug 7: 75 dials vs 697–1,444/day, 0 bookings. Throttle, smaller batch, paused campaign, or a cutover bug?

**[live code + one live observation] — I can rule things out precisely, and I found one dated coincidence.**

**What controls daily dial volume: nothing in the agent, by default.** Both caps in `limits.ts` (`MAX_CONCURRENT_CALLS`, `MAX_CALLS_PER_DAY`) **default to 0 = unlimited**, and `CALL_WINDOW` is unset in prod by design. The agent is purely reactive — it dials when asked. **Daily volume is set entirely by the external relay's cron and campaign configuration.**

**Ruled out (or nearly):**
- **A silent agent-side throttle** — would have to mean `MAX_CALLS_PER_DAY` was explicitly set, which would be visible: it returns HTTP 429 with `error_code` and **writes no Call doc**. A rate-limited day produces *missing* rows, not failed ones.
- **A crash or outage** — both prod and stage answered my health probe cleanly, warm pool at 2, no queue depth. The process also has `unhandledRejection`/`uncaughtException` guards specifically so one bad call cannot kill the server (`index.ts:893-898`).
- **The night guard suddenly engaging** — it would leave a trail: `ScheduledCall` rows with `kind:'night_defer'`. Zero such rows = not this.

**What I would look at, in order:**

1. **The prompt swap.** `mongo_updated_at = 2026-08-07T08:47:21.807Z` — a new prompt was pushed to production **on Aug 7**, and the code/Mongo versions are `in_sync: false`. I am not asserting causation: a prompt push does not reduce *dial volume*, so it cannot explain 75 dials. But "0 bookings" and "a new sales prompt went live that morning" on the same day is a coincidence worth a look, and it would not appear in any purely schema-level audit. With only 75 dials, 0 bookings may also be pure small-sample noise — at a ~2-3% booking rate, 0 out of 75 is entirely unremarkable.
2. **Whether those 75 rows are dials or *rejections*.** Check `Call.status` and `outcome` on them. All `initiated`/`failed` with no `in-progress` would point at a dial-side failure; a normal outcome spread points at "the relay only asked for 75".
3. **Suppression growth.** Requests refused by the DNC or visit-scheduled gates **write no row at all** (see B8). If suppressions spiked, the relay's request count could be normal while `calls` collapses. **This is genuinely invisible from the `calls` collection** — you would need the relay's logs.
4. **`externalScheduleId` prefixes on those 75.** If the usual bulk prefix is absent, a campaign was paused upstream — which is my leading hypothesis, because it is the only explanation that naturally produces *both* a ~95% volume drop *and* zero bookings.

**My honest read: this looks like an upstream campaign/cron change, not an agent bug.** But I cannot prove it from inside this repo, and the relay's logs would settle it in minutes.

### I34. Monitoring and alerting. Where are the logs, how far back, who has access?

**[live code for the mechanism, everything else UNKNOWN]**

**What exists in code — `agent/src/alerts.ts`:** `installErrorAlerting()` **wraps `console.error`**, so *every* error log becomes an alert. Delivery to Microsoft Teams (`TEAMS_WEBHOOK_URL`, via Power Automate Workflows — the legacy Office365 connector is deprecated) and/or email (`ALERT_EMAIL_WEBHOOK_URL`). Deduped per error signature (`ALERT_THROTTLE_MS`, default 15 min) with a global cap (`ALERT_MAX_PER_HOUR`, default 30).

**It is a complete no-op until those webhook URLs are configured** (`alerts.ts:52`: `if (!TEAMS && !EMAIL_WEBHOOK) return;`). **Whether they are set in prod: UNKNOWN.**

**Health endpoints (both live right now, both unauthenticated):**
- `GET /health` → `{ok, version, pool}`
- `GET /api/health` → `{status, version, queue_depth, pool, warmup}`
- `GET /api/prompt-version` → the version drift check

**What is NOT monitored, and I want to name these because they are the failure modes that actually bite:**
- **Webhook delivery failures** — recorded per call in `Call.webhookDeliveries[].status`, but **nothing aggregates or alerts on them**. A relay that starts 4xx-ing every posthook produces a silent, permanent data gap. (4xx is treated as permanent — no retry.)
- **WhatsApp send failures** — fire-and-forget, log-only. Exactly how 28,309 failed sends go unnoticed.
- **Legacy mirror failures** — every mirror function catches and swallows (`legacy-mirror.ts:314`, `:385`, `:445`). `ASBLVoiceBot` can silently drift out of sync with `angad` and nothing says so.
- **Classifier degradation** — after 2 failed attempts it falls back to a heuristic that stamps `"classifier offline"` into the summary and returns **all-null slots**. Logged; not alerted. See J.

**Logs:** the agent logs to stdout via Fastify's pino logger, so on ECS Fargate they go to **CloudWatch Logs**. Retention, log-group name, and access are all AWS-account settings. **[UNKNOWN — no AWS access.]** Whoever administers account `737866084260` can tell you in one command. **They are worth chasing: the per-call `[timing]` reports (C14) and every classifier/mirror/webhook failure exist only there.** Dashboard logs would be on Vercel/Netlify; provider-side call logs in the Plivo and Telnyx consoles.

---

# J. Landmines

Ordered roughly by how badly each would mislead an outside analyst.

**J1. `roomName` is `"pending-<uuid>"` for every unanswered Plivo call.** `crm/routes.ts:371`.
*Naive reading:* `call_sid == roomName` is a valid join. *Correct reading:* it silently drops unanswered Plivo calls — the failures — so every connect rate computed that way is **inflated**. Match on `externalScheduleId`, or `$or` with the `pending-` form.

**J2. `cleanupStaleCalls` fabricates `NO_ANSWER` outcomes.** `index.ts:737-760`.
Any call sitting in `status:'initiated'` for >10 min with no duration is set to `failed` + **`outcome:'NO_ANSWER'`**, and the code comment admits it: *"real outcome unknowable post-hoc but NO_ANSWER is the most common cause in practice."*
*Naive reading:* `NO_ANSWER` means the carrier reported no answer. *Correct reading:* an unknown fraction are **guesses**, indistinguishable from real ones. Every deploy and every restart mid-dial manufactures more. This also runs **every 5 minutes**, and it has **no `CARRIER_TERMINAL_OUTCOMES` guard** — unlike the teardown path. Your `NO_ANSWER` bucket is contaminated and there is no flag to filter it.

**J3. `ASBLVoiceBot.call_schedules.status` is `'called'` before the phone rings.** `legacy-mirror.ts:432`.
*Naive reading:* `'called'` means the call happened. *Correct reading:* it is set at insert. If teardown never runs, it stays `'called'` forever with no outcome. Also `VOICEMAIL`/`SWITCHED_OFF` map to `'called'`, not `'no_answer'`. **Use `call_outcome`.**

**J4. Every outcome and every extracted slot was decided from a <50%-accurate transcript.** `config.ts:126`, `transcribe-recording.ts:5`, classification at `media-stream.ts:1600-1606`.
*Naive reading:* transcript quality affects readability. *Correct reading:* it affects **the data**. The classifier runs at teardown on the weak Live transcript; batch re-transcription (if enabled at all) happens later and **never re-runs classification**. This is upstream of the 89% booking-reminder problem.

**J5. `fullText` and `transcript[]` can be from different sources on the same document.** `transcribe-recording.ts:143-152`.
Batch re-transcription **overwrites `fullText`** but never touches `transcript[]`. *Correct reading:* check `transcriptSource` before comparing them or you will "discover" a divergence that is just two different transcribers.

**J6. `outcome: 'VOICEMAIL'` has two unrelated origins and they are indistinguishable.** AMD (`CallState.machine`, 2h TTL) vs a regex over transcript text (`classifier.ts:145` applied at `media-stream.ts:1576`). *Correct reading:* you cannot measure AMD effectiveness from this data.

**J7. Three time gates, all opt-in, all default OFF, and `limits.ts` says compliance is delegated to the CRM.** `night-window.ts:58`, `limits.ts:26-36`, `scheduler.ts:189`.
*Naive reading:* a night guard exists, therefore night calls are prevented. *Correct reading:* it exists and may never have run. `ScheduledCall.count({kind:'night_defer'}) == 0` proves it never fired. Timezone math is **correct** — this is a configuration gap, not a bug.

**J8. The planner's "nothing to schedule" log message names the wrong cause.** `schedule-on-presite.ts:36` says *"(visit past/imminent)"* whenever `planned.length === 0` — but the dominant real cause is an **unparseable date**. Anyone debugging from logs is sent in the wrong direction.

**J9. `Lead.propertyInterested` defaults to the literal `'ASBL Loft'`.** `routes.ts:188-189`. *Naive reading:* this lead is interested in Loft. *Correct reading:* the project may simply never have been specified. Also single-valued and overwritten — read project from `Call.metadata.project`.

**J10. Silent-catch blocks that swallow failures.** Every legacy-mirror export (`:314`, `:385`, `:445`); DNC lead-flag write (`media-stream.ts:1699`); `Lead.status` update on booking (`schedule-on-presite.ts:81` — a bare `.catch(() => {})`); continuity/AMD state writes; `getRecentHistory` (`:676`); the whole `scheduleSiteVisitCallsForCall` body (`:88`). *Correct reading:* `ASBLVoiceBot` **can be silently incomplete relative to `angad`**. Never treat the mirror as authoritative. Also: `unhandledRejection`/`uncaughtException` are caught and the process **deliberately stays alive** (`index.ts:893-898`) — correct for a call server, but it means crashes leave no crash.

**J11. The classifier's failure mode writes plausible-looking nulls.** `classifier.ts:151-190`. After 2 failed attempts it returns **all-null slots**, `is_qualified: false`, `language_detected: 'hinglish'`, and a summary containing `"classifier offline"`. *Naive reading:* this lead had no budget/timeline/intent. *Correct reading:* the classifier failed. **Filter on `summary` containing "classifier offline"** — that string is your marker.

**J12. `language_detected: 'hinglish'` is also the hardcoded default when the classifier is skipped.** `media-stream.ts:1669-1673`, `:1785-1789`, and again in the fallback. The classifier is skipped for tool-ended, short (<10s), and DNC calls. *Correct reading:* the hinglish bucket is inflated with unknowns.

**J13. `DNC_REQUESTED` measures a 16-pattern regex, not customer intent.** `crm/dnc.ts:12-33`. English + Hindi/Hinglish only — **no Telugu, Tamil, or Kannada patterns**, despite the classifier explicitly recognising those languages. A Telugu-speaking customer asking us to stop is not detected, the lead is never flagged, and dialling continues. **This is a live compliance exposure, not just a metrics artefact.**

**J14. The bot cannot self-report DNC, and the visit gate can reject real bookings.** `media-stream.ts:871-913`. `hangup_call(DNC_REQUESTED)` is **rejected** unless the regex already fired. And `hangup_call(PRE_SITE)` is **rejected** unless the visit gate is open — gate opens only after **4 minutes elapsed** (`VISIT_GATE_MIN_MS=240000`) **or 11 customer turns** (`VISIT_GATE_MIN_TURNS=11`) or the customer raised visiting themselves. *Correct reading:* **a genuine booking on a fast, efficient sub-4-minute call with few turns is actively blocked and never recorded as `PRE_SITE`.** Given the stated goal is booked-and-attended visits, this deserves scrutiny — it is a deliberate anti-pushiness guardrail, but its cost is invisible in the data because a rejected booking leaves no trace.

**J15. Hardcoded numbers you should know about.** `HANGUP_FLOOR_MS = 8000` (hangups blocked in the first 8s if the customer said <10 chars); `DEAD_AIR_TIMEOUT_MS = 45000`; `CUSTOMER_SILENCE_MS = 10000` (10s of customer silence **disconnects the call**); `GREETING_PROTECT_MS = 7000` (inbound audio is **not forwarded to Gemini** during the greeting — a customer who speaks in the first 7s is **not heard at all**); `parseVisitDateTime` defaults a missing slot to **10:00 IST**; bulk dispatch `MAX_ROWS = 500`; Plivo recording `time_limit` **3600s** (was defaulting to 60s — old truncated recordings).

**J16. Timezone assumptions.** IST is hardcoded as `+05:30` in at least four places (`night-window.ts:13`, `limits.ts:19`, `scheduler.ts:41`, `media-stream.ts:1605`). All **correct** for India — but the system now dials internationally via Telnyx, and **every window is evaluated in IST regardless of destination.** Separately, the legacy mirror writes `full_text` timestamps in **UTC 12-hour** format (`legacy-mirror.ts:62-74`) while everything else is ISO — comparing timestamps across the two databases without normalising will be off by 5h30m.

**J17. Test data is mixed into production collections.** `Call.source` defaults to `'zoho'` and is derived from an **esid string prefix**: `dash*` → `'goodcalls'`, `sched-*` → also `'goodcalls'`, everything else → `'zoho'` (`routes.ts:325`). Manual test dials with an arbitrary esid are therefore labelled **`'zoho'`** and are indistinguishable from real CRM traffic. The `'test'` source value exists in the schema and in the mirror's mapping but **is never assigned by any code path**. Look for test numbers by phone, not by `source`.

**J18. Fields that exist but are never populated.** `Call.sentiment`, `Call.objectionsHandled`, `Call.closingScore`, `Call.handoffTriggered` (schema default `false`, never set true), `Lead.email`/`income`/`timeline`/`leadScore`/`notes`/`source`, `Call.source === 'test'`. **`closingScore` and `sentiment` are the dangerous two** — an analyst will reasonably assume a quality score exists. It does not.

**J19. Race between two writers on `outcome`, and the guard is asymmetric.** The provider hangup webhook writes only if `outcome` is null; teardown writes only if the outcome is not a carrier-terminal value; **`cleanupStaleCalls` has neither guard.** All three run concurrently. The comment at `media-stream.ts:1678-1682` names this as the "F6 asymmetric-guard bug", now partly fixed. Rare, but real.

**J20. Prompt history is destroyed on every update.** `db/models/Prompt.ts` — one document per slug, `key` unique, no history. Combined with no prompt version on the `Call` document (C12), **the prompt used for any historical call is unrecoverable from any database.**

**J21. Suppressed dial requests write nothing at all.** DNC-suppressed, visit-scheduled-suppressed, invalid-phone, unknown-slug, and rate-limited requests return an HTTP response and create no record. *Correct reading:* "dials requested" ≠ "rows in `calls`", and the gap is invisible from our side. Only the relay knows.

**J22. `ScheduledCall.status:'done'` means the reminder call was answered — not that the visit was confirmed.** `scheduler.ts:156-161`. `done` is set whenever the outcome is not in the `UNANSWERED` set. Do not read it as confirmation.

**J23. The `site_visit_confirmation` slug is dead in the planner but still fully dialable.** `prompts/index.ts` registers it; `isValidSlug` accepts it; `/api/schedule-call` will place it. This is the mechanism behind your 5 zombie calls (D17), and it will happen again unless the slug is retired or gated.

**J24. `queue_depth` in `/api/health` is not a queue.** `index.ts:70`: `Math.max(0, active - warm)` on the Gemini session pool. It has nothing to do with pending dials. An external monitor reading it as backlog is reading a Gemini pool statistic.

### J36. If you could tell an outside analyst only three things.

**1. This repo is the *voice agent*, not the *system*.** It dials when told, talks, writes a call record, and posts a webhook. Lead selection, campaign pacing, re-dial cadence, identity resolution, and site-visit attendance all live upstream or downstream, in the Zoho relay and CRM. **The agent has no retry policy and no volume control whatsoever.** If a lead was dialled eleven times, something else asked eleven times. Nearly every "why is the agent doing X?" question about *volume, timing, or targeting* is really a question about the relay.

**2. Almost every safety feature you will find in the code is opt-in and defaults to OFF.** The night guard, the calling window, the site-visit scheduler, answering-machine detection, accurate post-call transcription, webhook signature verification, WebSocket auth, concurrency caps, daily caps, error alerting — **every one** defaults to disabled or unconfigured. This is a deliberate, consistent engineering convention (ship dark, enable per-environment) and it is defensible. But it means **"the code does X" tells you nothing about whether X happens in production.** Never infer behaviour from a feature's existence. Where I could give you a data-side test for whether a flag is on, I did — use those.

**3. The system cannot measure the thing it exists to achieve.** The goal is *attended* site visits. Attendance is recorded nowhere and cannot be received. Bookings are captured — but the visit date is free text that fails to parse ~89% of the time, and the classifier that extracts it runs on a transcript the developers themselves measure at **under 50% accuracy**. So the funnel degrades at every step: attendance is absent, booking *details* are mostly unparseable, and the booking *detection* rests on a weak transcript. **Any metric you build on top of this is measuring a proxy of a proxy** — and the highest-leverage fixes are not analytics fixes, they are (in order) capturing attendance at all, enabling accurate transcription, and teaching the classifier to resolve "Saturday" into a date the way it already resolves callback times.

---

# SELF-AUDIT

### Sourcing of every answer

**[live] — read from current source in the working tree, or probed against a live endpoint:**
A1 (structure), A2, A5, B6, B7, B8, B9, C10, C11 (mechanism), C12 (incl. live `/api/prompt-version` probe), C13, C14, C15 (mechanism), D16, D17 (incl. `git log` on commit `b08fa02`), D18, D19 (mechanism + timezone verification), E21, E22, E23, F24, F25, F26, G27 (code path), G28, H29, H30, H31 (defaults), I33 (partial), I34 (mechanism), all of J.

**[docs/memory] — clearly flagged, not verified against live billing or live config:**
I32 (the entire rate card — treat as approximate). The historical narrative in C15 about 10-minute Telnyx presigned URLs, and in E23 about 55 backfilled leads, come from project notes, not from code I can see today.

**[UNKNOWN] — I could not answer and said so:**
A1 (the actual `MONGODB_URI` value in prod), A2 (whether a second writer still exists), A3 (`growth-relay.asbl.in`), A4 (`Intelligent_CRM`), C11 (whether AMD is on), C12 (deploy log for model changes), C15 (recording retention), D19 (whether `NIGHT_DEFER_ENABLED` is set), E20 (MASTERLEADID), F25, G27 (whether senders are alive today; the origin of the 28,309 sends), H31 (running env values; when the model last changed), I32 (Telnyx rates), I34 (log retention, log access, whether alerting is configured).

### Answers I am less than fully confident in

- **I33 (Aug 7).** I can rule out agent-side causes with confidence, and I found a dated prompt push on the same day. I cannot identify the actual cause. My "upstream campaign change" reading is a hypothesis, labelled as one.
- **G27.** I am confident this repo never treats 401 as success and that `whatsappSent` cannot be set by a 401. I am *inferring* that the 28,309 sends are not ours, from volume and from the flag logic. If a second WhatsApp path exists that I do not know about, that inference fails.
- **D19.** The mechanism and the timezone correctness are solid. "The flag is probably off" is an assessment, not a fact — the `night_defer` row count settles it.
- **A1/A2.** Structurally certain that `angad` and `ASBLVoiceBot` share a cluster (that follows from `useDb`). Not certain *which* cluster.
- **C11/H31.** Code defaults are certain; running values are not.
- **I32.** Documentation figures. The *token counts* on `Call.tokenUsage` are exact and are the number I would actually trust.
- **J14.** I am confident the visit gate rejects early `PRE_SITE`. I am *not* able to quantify how often a genuine booking is lost that way, because a rejected booking leaves no record — which is itself the finding.

### Secrets and PII scan

Re-scanned the full document. It contains **no** connection strings, API keys, tokens, passwords, customer names, or customer phone numbers. Credentials are referenced by **environment-variable name only** (`MONGODB_URI`, `PLIVO_AUTH_TOKEN`, `PERISKOPE_API_KEY`, `ASBL_VOICEBOT_API_KEY`, …).

Identifiers I *did* include, deliberately, because they are infrastructure and not secrets: the AWS account id `737866084260`, ECS cluster/service names, the ECR repository path, and the public hostnames `growth-angad.asbl.in` / `growth-angad.stage.asbl.in`. **Say the word and I will strip them.** Two ASBL business phone numbers appear in the source (Plivo/Telnyx caller-ID numbers) — I have not reproduced them.

One thing I should raise once and then leave alone: while reading, I found a live Bitbucket access token and other credentials committed in a local project notes file, and both health endpoints (`/health`, `/api/health`) are unauthenticated on the public internet. Neither is my call to act on, and I have not acted on either — flagging so it is on the record.

### What I did not have access to

- **AWS entirely** — no CLI installed. No ECS task definitions, no environment variables, no SSM parameters, no CloudWatch logs, no ECR, no deploy history. **This is the biggest blind spot by a wide margin**, and it is the reason ~14 answers are `[UNKNOWN]`. Anyone with a role in account `737866084260` could close most of them in about ten minutes with `aws ecs describe-task-definition`.
- **The production MongoDB** — no growth-prod credentials. Everything database-side is read from schema and code, never from data. I did not verify a single row.
- **Provider dashboards** — Plivo, Telnyx, Periskope, Google AI Studio. So: AMD settings, recording retention, actual rates, sender-pool health, Gemini quota.
- **The relay and CRM repos** — `growth-relay.asbl.in`, `asbl-crm-api`, Zoho workflows, `Intelligent_CRM`, `Zoho_Database`. This is where lead selection, dial cadence, and identity resolution actually live.
- **The Inncircles/Compass systems** — relevant to attendance (F24).
- **The `web/` dashboard in depth** — I read its structure and dispatch routes but did not audit it line by line. It is a secondary writer to the same database and is a small residual blind spot.

---

# WORK LOG — surprises, disagreements, dead ends

**2026-08-08 — The repo moved and the documentation did not follow.**
Project notes describe Render + a self-hosted EC2 MongoDB. The live deployment is ECS Fargate in a different AWS account with a different Bitbucket remote (`asbl-dev/angad`), and the notes were last updated 2026-07-20 — nearly three weeks stale. *Lesson: on this project, orient from `deploy-*.sh` and `bitbucket-pipelines.yml`, not from the notes.*

**2026-08-08 — Prod is running a prompt its own code does not contain.**
`/api/prompt-version` returns `in_sync: false`: code expects `2026-08-02-radhika-v3.8.1`, Mongo serves `2026-08-05-ankit-v3.8-male`. Mongo wins at runtime. Intentional, but it means the **agent's persona and gender changed on ~Aug 5–7**, one to two days before the Aug 7 anomaly. *Lesson: `in_sync:false` is not always a bug — sometimes it is the deployment strategy. Always read the live endpoint; never infer the prompt from the code.*

**2026-08-08 — Every question about the relay, Intelligent_CRM, and identity died at the same wall.**
Grepped for `growth-relay`, `Intelligent_CRM`, `MASTERLEADID`, `master_lead_id`, `Inncircles`, `reactivation` across the whole source tree. **Zero hits, all six.** Abandoned A3, A4, E20, F25 as unanswerable from here. *Lesson: state the boundary explicitly and early rather than speculating — the CI Team asked for exactly this and a guess here would have been worse than a blank.*

**2026-08-08 — Code and reality disagree: the log message for the 89% problem names the wrong cause.**
`schedule-on-presite.ts:36` logs `"nothing to schedule (visit past/imminent)"` for **all** empty-plan cases, but the dominant cause is an unparseable date, which is neither. Anyone debugging the 89% from logs would have been sent the wrong way. Filed as **J8**.

**2026-08-08 — I expected to find that 401 was being counted as success. It is not.**
`periskope.ts:97` and `send-brochure.ts:110` are both strict. This *contradicted* the premise of question G27, so I traced the whole path twice before writing it up. The 28,309 sends have to come from somewhere else. *Lesson: check the premise before explaining the mechanism behind it.*

**2026-08-08 — The night-guard timezone was the obvious suspect and it is innocent.**
I fully expected a UTC/IST bug behind 10.3k 2am dials. The arithmetic in `night-window.ts` is correct in all three places it appears. The real answer is that the guard is **opt-in and probably off**, plus `/api/calls/initiate` bypasses it entirely, plus it is +91-only. *Lesson on this codebase: when a safeguard appears not to work, check whether it is switched on before checking whether it is correct. That pattern held for AMD, transcription, alerting, and webhook verification too — it is the single most reliable heuristic here.*

**2026-08-08 — The most consequential finding was not asked about.**
No question asked "can the system see whether anyone attended?" I went looking because F24 implied it, and found the answer is a flat no — no field, no endpoint, no path by which attendance could ever arrive. Given the stated goal, this outranks everything else in the document. *Lesson: answer the question behind the questions.*

**2026-08-08 — Two findings turned out to be the same finding.**
The 89% booking-reminder failure (D16) and the transcript-accuracy problem (H29) look like separate defects in separate subsystems. They are not: the classifier that extracts the visit date runs **at teardown, on the weak Live transcript**, before any batch re-transcription. Fixing transcription may move the booking numbers on its own. *Lesson: trace the data lineage before treating two symptoms as independent.*

**Dead ends worth recording:** no AWS CLI (blocked ~14 answers); no Mongo client and no growth-prod credentials (verified nothing against data); git history for the old repo is present under `origin/*` and was useful for dating commit `b08fa02` to 2026-07-17 — that trick is worth remembering, since the new `asbl` remote's history starts at a squashed "Initial commit".
