# ANGAD audit — round 4: relay answers folded in

**From:** developer-side Claude
**Date:** 2026-08-08
**New input:** code-verified answers from the relay/Zoho developer (repo `asbl-dev/angad-crm-api`), covering all 23 questions.

The relay side answered everything and answered it well — with file names, commit dates, and explicit "verify this" markers where they weren't certain. **Several of their answers correct mine.** I am leading with those, because the CI Team said mismatches are fine if flagged and fatal if hidden, and two of my earlier statements need withdrawing.

---

# 1. Corrections to my earlier answers — read before re-using rounds 1–3

### 1.1 ❌ I was wrong: `NIGHT_DEFER_ENABLED` **is ON** in production

**What I said** (round 1 D19, repeated in round 1 J7 and round 2 §1b): *"the most likely single explanation is that `NIGHT_DEFER_ENABLED` is not set to `true` in the production task… it exists and may never have run."*

**What is true:** the relay dev live-tested it — a night-time trigger returned `NIGHT_DEFERRED` and the dial was rescheduled into the morning window. **The flag is on and the guard is working.** My assessment was labelled as an assessment, but it was still wrong and it was the load-bearing guess in that section.

**What survives:** the *mechanism* analysis was right, and it is exactly what the relay confirms — the guard is **+91/Plivo-only by explicit condition** (`routes.ts:258`), and `/api/calls/initiate` bypasses it entirely. So the ~10.3k out-of-window dials are not a switched-off safeguard; they are **international calls the guard was never written to cover**, which matches your own finding that 94.5% of them are Telnyx/NRI.

**Still worth running** the query I gave you (`db.scheduledcalls.countDocuments({kind:'night_defer'})`) — not to discover whether it is on, but to confirm it independently and to date when it started. If that count is zero, then the dev's test was on staging and we have a genuine prod/stage divergence.

### 1.2 ❌ I was wrong: calling-hours compliance **is** enforced — for follow-ups, and on the relay

**What I said** (round 1 D19): *"TRAI compliance for call timing is currently not enforced by the voice agent at all in the default configuration… If the relay is not enforcing it either, then nobody is."*

The conditional was sound but the premise failed. **The relay computes customer-local time from a `COUNTRY_TZ` map and schedules all follow-up slots in the customer's own 9am–8pm local window.** So there is real timezone-aware gating, it just lives in a system I could not see.

**The corrected picture — two narrow gaps, not a blanket absence:**

| Path | Gated? | By what |
|---|---|---|
| Follow-up calls, any country | ✅ yes | relay `COUNTRY_TZ` → customer-local 9am–8pm slots |
| First call (T=0), **+91** | ✅ yes | agent's `NIGHT_DEFERRED` (21:00–06:00 IST → 08:00–11:30 morning window) |
| First call (T=0), **international** | ❌ **no** | agent's night-defer is +91-only; no relay-side T=0 gate |
| Any country **missing from `COUNTRY_TZ`** | ❌ **no** | falls back to `Asia/Kolkata` → IST slots applied to a non-IST customer |

Both gaps are narrow and both are fixable. The dev's own recommendation: add the missing NRI country codes to `COUNTRY_TZ`, and add an international night-defer (either side, **but only one side** — double-gating would silently halve reachable hours).

### 1.3 ⚠️ Partially wrong: the Aug 7 volume drop

**What I said** (round 2 §2c): leading hypothesis "an upstream campaign change". Direction right — it was upstream, not the agent — but the mechanism is different and more mundane: **the relay was itself migrating Vercel → ECS, Vercel crons were switched off, and the ECS scheduler only went up today (Aug 8). In between, `prd-cadence` did not run**, so follow-up dials stopped and only T=0/manual calls got through. 75 dials is what a day with no follow-up cron looks like.

**Verification is on the relay side, not ours:** `Zoho_Database.cron_log`, look for a gap in `prd-cadence` entries across Aug 6–7. **You have this collection in your read-only sync — you can confirm it yourselves without asking anyone.**

Note this also cleanly explains the 0 bookings: with follow-up dials absent, the remaining 75 were mostly T=0 first calls, which convert far worse than nurtured follow-ups.

---

# 2. Now closed

| # | Question | Answer |
|---|---|---|
| Q1–3 | Retry cadence | `api/cron/followup.ts` (`prd-cadence`, every 15 min) + `api/_utils/call_scheduling.ts` + `api/relay/inhouse-posthook.ts`. **See §4 — root cause found.** |
| Q4–6 | Calling hours | Two-layer, two narrow gaps. **See §1.2.** |
| Q7 | Collections | **Sync list partly wrong — see §6.** |
| Q8 | Write cadence | Not real-time. `recording_ready` → queue → `transcribe-calls` scheduler every **~2 min**. Posthook writes stage/`Next_Call_At` immediately (provisional); transcription overwrites ~2 min later **only when the flag is on**. |
| Q9–11 | Transcription system of record | **Still the bot's disposition.** Their flag `transcript_drives_cadence` is **OFF**. **See §5.** |
| Q12–13 | Volume control / Aug 7 | `prd-cadence` cron + `first_call_batch_size` (15/tick when `calls_delayed_batched` on) + `calls_paused` global kill. No separate campaign config. Aug 7 = cron gap. |
| Q15–16 | Identity | **`zoho_lead_id` always sent. Big unlock — see §3.** `source_lead_id` only from ~Aug 8, so useless historically. |
| Q17 | growth-relay | `asbl-crm-api`, Bitbucket `asbl-dev/angad-crm-api`, ECS service, growth team owns. Auditable. |
| Q18 | Intelligent_CRM | New event-sourced CRM, **not yet live/driving** — hence ~12%. **Use `Zoho_Database.leads`, not Intelligent_CRM.** Answers round 1 A4, which I had to leave open. |
| Q19 | posthook_payloads | **7-day TTL** (`expireAfterSeconds: 604800`). Extendable or archivable. |
| Q20 | 28k WhatsApp 401s | **Relay → Periskope**, 10-sender round-robin pool. 401 = sender phone dead/switched off (`dead_senders`, 1h TTL). Pool partially alive. **Confirms my round-2 inference that it was not the agent.** |
| Q21 | `X-Webhook-Secret` | **Not verified today — the posthook endpoint is effectively open.** Enforcement is built (`posthook_secret`, `posthook_enforce_secret`), default OFF, needs secret alignment then switch-on. |
| Q22 | Relay DNC | Manual block only (`call_block_list`, `call_blocked`). **No speech-based detection on either side.** |
| Q23 | `/api/calls/initiate` | Relay does not use it (moved to `/api/schedule-call` 2026-06-08). **Safe to close from the relay's side** — check for other callers first with the query in round 2 §4. |

---

# 3. The biggest analytical unlock in this round — identity, retroactively

**`external_schedule_id` is `prd-{zoho_lead_id}` for every relay-originated dial.**

This is much better than anything we had discussed. It means **the Zoho lead id is embedded in a field the agent has stored on every single call, for the entire history** — no dependency on `source_lead_id` (which only started ~Aug 8), no bridge through `master_lead_id`, no inference.

```js
db.calls.aggregate([
  { $match: { externalScheduleId: /^prd-/ } },
  { $project: { zohoLeadId: { $replaceOne: { input: '$externalScheduleId', find: 'prd-', replacement: '' } },
                phoneNumber: 1, outcome: 1, createdAt: 1 } }
])
```

Cross-validate against `metadata.zoho_lead_id` on recent calls (where both exist) before trusting it wholesale.

**This also completes the esid origin taxonomy**, which is now a clean four-way split for attributing any call:

| esid prefix | Origin |
|---|---|
| `prd-` | **relay follow-up cadence** (the `prd-cadence` cron) — and carries the Zoho lead id |
| `dash*` | ops dashboard — a human clicked |
| `sched-` | the agent's own site-visit reminder poller |
| anything else | other/manual/legacy |

**⚠️ One thing to check before relying on it as a per-dial key.** In round 1 (E21) I told you `externalScheduleId` is "one per dial, not per lead" — that holds for `dash*` and `sched-` esids, but **if `prd-{zoho_lead_id}` has no per-dial suffix, then every dial to the same lead reuses the same esid.** Two consequences:

- it is a **lead** key, not a **dial** key, for relay traffic — correct my round-1 statement accordingly;
- `ASBLVoiceBot.call_schedules` **upserts on `external_schedule_id`** (`legacy-mirror.ts:418`), so repeated esids would collapse all dials to a lead into a **single row** — meaning that collection would undercount dials badly.

Your 45k `call_schedules` rows against 48,306 calls suggests esids are *mostly* unique, so there is probably a suffix the dev's shorthand omitted. **Settle it in one query:**

```js
db.calls.aggregate([
  { $group: { _id: '$externalScheduleId', n: { $sum: 1 } } },
  { $match: { n: { $gt: 1 } } },
  { $count: 'reusedEsids' }
])
```

---

# 4. Root cause of the over-dialing — found, dated, and fixable

Your 62.6% finding now has a precise cause, and it is a **regression, not a design choice**.

`followup.ts:627` comment, quoted by the dev:
> *"Removed (2026-06-18): v1 SS-call tree (3 × 4h), v2 retry tree (10-min + 3h aggressive + 3-day exhaustion). v3 is just slot hours + 7-day lifetime cap."*

| Version | Attempt cap |
|---|---|
| v1 | 3 SS calls × 4h — **count cap** |
| v2 | aggressive tree + **3-day exhaustion** — count + time cap |
| **v3 (2026-06-18 → today)** | **count cap removed.** Only slot hours + 7-day lifetime (`LEAD_CALL_LIFETIME_MS`) |

**The arithmetic:** 9 slots/day × 7 days = **~63 dials possible per lead**. `Consecutive_Missed_Count` is now a dead field — it exists but nothing enforces on it. (Add that to the "fields that exist but are never used" list, alongside our own `sentiment`/`closingScore`.)

**And the stop conditions explain the outcome mix exactly.** Retries stop on: booking (Pre Site / Virtual Tour), "Not Interested", the 7-day cap, `bot_enabled=false`, Spam, manual clear, block-list. **They do NOT stop on:** plain `CONNECTED` (picked up but didn't book → rescheduled), or `BUSY` / `NOT_CONNECTED` / `SWITCHED_OFF` (retried every slot). In v3 the only difference between a lead who answers and one who never does is **which slot hours are used** — not how many attempts they get.

So: an unreachable number is dialled every slot for seven days. That is precisely your BUSY/VOICEMAIL/NOT_CONNECTED-heavy 30,255 calls at 3.9% connect.

**The fix is one count cap restored in `call_scheduling.ts`, on the relay side.** The dev has already identified it and agrees. This is the single largest saving on the board and it does not need anything from our side.

**One analysis I'd suggest you run:** max calls per lead. If any lead exceeds ~63, the 7-day lifetime has a leak (probably reset by new lead activity), and the cap fix alone won't fully close it.

```js
db.calls.aggregate([
  { $group: { _id: '$phoneNumber', n: { $sum: 1 } } },
  { $sort: { n: -1 } }, { $limit: 20 }
])
```

---

# 5. Transcription — the answer resolves C1, and reveals a gap nobody owned

**C1 is settled: keep `TRANSCRIBE_POSTCALL` OFF on the agent.** The relay's Gemini pipeline is the intended replacement, and running both over the same audio is duplicated spend for one answer. Agreed and closed.

**But note carefully what the answers add up to, because it is worse than either side assumed alone:**

- The relay pipeline **started Aug 8 — today**.
- Its flag `transcript_drives_cadence` is **OFF**. It stores, it does not drive.
- It has an **open hallucination bug** — it fabricated transcripts on short/dropped recordings. Fix in progress.
- **No backfill.**
- Meanwhile the agent's own accurate re-transcription is off, and its live transcript is <50% accurate on this audio.

**So as of today, no accurate transcription is driving anything, anywhere, and none of the historical data has been corrected.** Combined with the `extractedSlots` outage from round 3 §B1 (all-null from 16 Jul to 7 Aug), the practical position is:

> **Structured call intelligence before ~7–8 August 2026 is largely absent or unreliable, on both sides.** Treat that as a hard floor for historical analysis rather than something to work around.

That is not a criticism of either team — both changes were improvements in flight. But it needs saying plainly, because a chart drawn across that boundary will show a "trend" that is really a change in instrumentation.

**Coordination point, worth writing down somewhere shared:** the agent's flag must stay OFF *while* the relay's stays OFF, and the switch must be one-way — relay verified → relay ON → agent stays OFF permanently. If both end up on, you pay twice; if the coordination is forgotten, you get zero.

---

# 6. Your read-only sync list needs correcting

**Wrong — the relay does not write these:** `crm_call_logs`, `crm_notes`, `audit_logs`. They are not in the relay's collection constants. They likely belong to `Intelligent_CRM` or another event-sourced system. **Do not treat them as relay output.**

**Correct — relay-written, all in `Zoho_Database`:**
`call_transcripts` · `cron_log` · `call_throttle` · `transcription_queue` · `leads` · `bot_settings`

**⚠️ The `call_transcripts` collision is confirmed real and it is the one most likely to burn you:**

| Database | Written by | Shape |
|---|---|---|
| `ASBLVoiceBot.call_transcripts` | **the voice agent's legacy mirror** (`legacy-mirror.ts:240`) | keyed `call_sid`; speaker labels are the bot's *name* and the customer's *phone*; timestamps UTC 12-hour |
| `Zoho_Database.call_transcripts` | **the relay's Gemini pipeline** | different schema, ~2-min lag, currently store-only |

**Qualify by database in every query and in your sync config.** These are two unrelated collections that happen to share a name, with different keys, different schemas and different writers.

Two more useful ones the dev surfaced that you should add if not already synced: **`cron_log`** (confirms the Aug 7 gap yourselves) and **`bot_settings`** (holds `calls_paused`, `first_call_batch_size`, `calls_delayed_batched`, `transcript_drives_cadence`, `posthook_enforce_secret` — **this is effectively the relay's live feature-flag table, and it is readable**, which is the relay-side equivalent of the config endpoint I proposed for the agent).

---

# 7. Gaps that belong to neither side — now visible for the first time

These only became apparent once both halves were on the table. Each needs a decision about **who** owns it, because right now nobody does.

**7.1 — The true "ask" count is unknowable by anyone.** I reported (round 1 B8) that suppressed dial requests write no row on the agent side. The dev confirms **rejected/suppressed dials are console-only on the relay side too** — no stored rows. So "how many dials were requested vs refused" cannot be answered from stored data anywhere. **This is a gap to build, not to find.** The dev has offered to add reject-logging; the agent could equally log its three suppression gates. **One side should do it, and it should be the relay** (it sees the requests we never hear about at all).

**7.2 — Speech-based DNC is missing on both sides, in three languages.** The agent has an English + Hindi/Hinglish regex (16 patterns, `crm/dnc.ts`). The relay has manual block-lists only. **Neither detects a Telugu, Tamil or Kannada opt-out** — so a customer asking us to stop in those languages is never flagged, and the relay keeps requesting dials for the full 7 days. This is a live compliance exposure, confirmed from both sides, and it is genuinely nobody's today. The dev's point is a good one: the new Gemini transcription could catch it in any language once it is reliable — but that is a future win and the gap is open now. **Adding regex patterns to `crm/dnc.ts` is a small, immediate mitigation.**

**7.3 — Does a bot-detected DNC actually stop the relay?** The agent fires a `dnc_requested` webhook (`media-stream.ts:692`) and sets its own `Lead.doNotCall`, which hard-blocks future dials at `/api/schedule-call`. But the relay's stop-conditions list does **not** mention the DNC webhook — only manual block-lists. **If the relay does not consume `dnc_requested` into `call_blocked`, then a DNC'd lead keeps being requested every slot for 7 days and the agent refuses each one.** No customer harm (the agent's block holds, and I verified that gate is real), but it is wasted cycles and it means the relay's own view of that lead is wrong. **Needs a one-line answer from the dev — added to §10.**

**7.4 — Two independent WhatsApp senders, same customers, same provider.** Both systems send via Periskope from **different pools** — the relay uses a 10-sender round-robin, the agent uses `PERISKOPE_SENDERS` with **sticky-sender-per-customer** logic (`wa_senders`) specifically so a customer always sees one consistent thread. **That guarantee only holds within the agent's sends.** A customer can receive relay messages from one number and agent messages from another. Worth a product decision on whether the pools should be unified.

---

# 8. New landmine found this round

**J25 — `sched-` prefixed calls are labelled `source: 'zoho'`, not `'goodcalls'`. The code comment says the opposite.**

- `crm/routes.ts:325`: `const source = esid.toLowerCase().startsWith('dash') ? 'goodcalls' : 'zoho';`
- `sitevisit/scheduler.ts:54`: `// \`sched\` prefix (not \`dash\`) → source classified as goodcalls in routes.ts.`

`'sched-…'.startsWith('dash')` is **false**, so the source is `'zoho'`. The comment asserts `goodcalls`.

*Naive reading:* `source: 'zoho'` means the CRM asked for this call. *Correct reading:* it also includes **the agent's own site-visit reminder calls**, which are self-generated. Anyone attributing dial volume or cost by `source` will over-attribute to the relay.

**Use the esid prefix taxonomy in §3 instead of the `source` field** — it is derived from the same string but is actually correct.

---

# 9. Updated action board

**Relay side** (dev already agrees on the first two):
1. 🔴 **Restore a count cap (~8) in `call_scheduling.ts`** — biggest single saving; the v3 regression.
2. 🔴 **Add missing NRI country codes to `COUNTRY_TZ`** + an **international night-defer** (one side only).
3. 🟡 Align `posthook_secret` and switch `posthook_enforce_secret` ON — the endpoint is open today.
4. 🟡 Verify the Gemini transcription fix, then flip `transcript_drives_cadence` — and tell us, so the agent's stays off.
5. 🟢 Add reject/suppression logging (§7.1).
6. 🟢 Extend `posthook_payloads` TTL beyond 7 days, or archive.

**Agent side** — all scoped, none built, all awaiting Mukund:
7. 🔴 **`shouldClassify` fix** (round 3 §B1) — outcome resolution and slot extraction must not share a gate.
8. 🔴 **Classifier prompt: site-visit date resolution rule** (round 3 §C2) — one instruction, prompt-only.
9. 🟡 **F1–F4 persistence PR** — `prompt_version`, `outcomeSource`, `hangupCause`, visit-gate rejections. ~12 lines, purely additive, zero read-path risk.
10. 🟡 **Add Telugu/Tamil/Kannada DNC patterns** to `crm/dnc.ts` (§7.2).
11. 🟢 Retire or gate `/api/calls/initiate` — relay-clear, pending a check for other callers.
12. 🟢 Fix the misleading comment at `sitevisit/scheduler.ts:54` (§8).
13. ⏸️ Keep `TRANSCRIBE_POSTCALL` **OFF** — resolved, no action.

**Product — Mukund:**
14. 🔴 **Attendance capture** (round 3 §A1). Unchanged and still the only thing that makes any of the above falsifiable. ~30 lines for the minimum version.

---

# 10. Short follow-ups for the relay dev

Only four, all one-liners:

1. **Does `external_schedule_id` = `prd-{zoho_lead_id}` have a per-dial suffix, or is it literally the same string on every dial to that lead?** (§3 — decides whether it is a dial key or a lead key, and whether `call_schedules` is undercounting.)
2. **Does the relay consume the agent's `dnc_requested` webhook into `call_blocked`?** (§7.3 — if not, DNC'd leads are re-requested for 7 days.)
3. **Can `cron_log` confirm the Aug 6–7 `prd-cadence` gap?** They suggested it; we can now check it ourselves, but their confirmation would close it.
4. **Does the 7-day `LEAD_CALL_LIFETIME_MS` ever reset** (new lead activity, re-import, stage change)? If it does, a count cap alone won't fully stop the over-dialing.

---

# Confidence

**High — verified in our code today:** the `sched-`/`source` mislabel (§8), the esid taxonomy mechanics, the `call_transcripts` collision, `dnc_requested` being fired, and everything carried forward from round 3 §B1.

**High — from the relay dev, code-cited with commit dates:** the v3 cap removal, stop conditions, cron/batch controls, collection ownership, TTLs, the Periskope pool, and the webhook-secret state. They marked their own uncertainty explicitly, which I have preserved rather than smoothed over.

**Flagged as their hypothesis, not fact:** the Aug 7 cron-gap explanation. It is strong and it is checkable from `cron_log`, which you already sync — **check it before treating it as settled.**

**Open:** the four items in §10.

**Withdrawn by me this round:** my `NIGHT_DEFER_ENABLED`-is-off assessment, and my "nobody enforces calling hours" conclusion. Both wrong; see §1.

---

# Redaction confirmation

Re-scanned before sending. **No phone numbers** — no customer numbers, no sender-pool numbers, no examples. Digit strings present are line numbers, dates, commit dates, TTL seconds, and the counts both teams supplied. **No `@` signs, no email addresses.** **No credentials** — `posthook_secret`, `PERISKOPE_SENDERS` and the rest are named only, never valued. **No connection strings.** Repo and collection names are included as identifiers; no hostnames beyond `growth-relay.asbl.in`, which the CI Team named first. **No customer data was read, exported, or moved:** this round used source code, local read-only `git`, and the relay dev's written answers. I did not connect to any database.
