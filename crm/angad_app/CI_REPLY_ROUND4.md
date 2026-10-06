# CI Team → dev side: round 4 reply

**Date:** 2026-08-08 · verified against our read-only cache (49,116 calls, 65 collections)

---

## 1. WE WERE WRONG, AND IT COST YOU A ROUND. The `extractedSlots` finding was our bug.

**Retract B1 entirely as a data finding.** Our "budget / timeline / config / objection /
callback = 0 non-null across 48,306 calls" was an artefact of our own sync code
reading field names that do not exist.

Our `sync.py` was mapping:

| our column | what we read | what the field is actually called |
|---|---|---|
| `slot_budget` | `extractedSlots.budget` | **`budget_bucket`** |
| `slot_timeline` | `extractedSlots.timeline` | **`timeline_bucket`** |
| `slot_config` | `extractedSlots.config` | **`configuration_interest`** |
| `slot_callback` | `extractedSlots.callback` | **`preferred_callback_time`** |
| `slot_objection` | `extractedSlots.objection` | **`objection_raised`** |
| `slot_intent` | `extractedSlots.intent` | correct |
| `slot_site_visit` | `extractedSlots.site_visit_requested` | correct |

**The two we spelled correctly are the only two that were ever non-zero.** That is
the whole pattern, and it is a perfect fingerprint of our error rather than of a
data outage. We have fixed the mapping (real names first, short names as a
fallback for schema drift) and will re-sync.

So: **you built the three-regime theory on bad evidence we supplied.** We are
sorry for the round. Two things survive and we do not want them lost in the
retraction:

1. **Your B1.3 mechanism finding stands on its own.** `shouldClassify` skipping
   slot extraction whenever `preliminaryOutcome` is already set is a real design
   fault regardless of what our columns showed — outcome resolution and slot
   extraction should not share a gate, and the moment `hangup_call` is
   re-enabled the outage becomes real. **We still support that fix**, now on
   code-reading grounds rather than on our numbers.
2. **Your correction of our "it lands in the mirror" inference was right** and
   saved us from pointing the planner at
   `call_transcript_intent_identification` — another team's collection. Thank
   you for pushing back rather than accepting our framing.

**We will re-run your B1.6 regime queries after the re-sync and send you real
numbers.** Until then, treat regimes 1/2/3 as untested, not as confirmed.

For reference, the one place we *can* see these slots today —
`Intelligent_CRM.calls`, 7,175 rows — has `budget_bucket` 94, `timeline_bucket`
53, `configuration_interest` 34, `preferred_callback_time` 76,
`objection_raised` 398, `site_visit_requested.date` 46. Sparse, but not zero.

---

## 2. Aug 7 — the cron gap is REAL and we confirmed it ourselves. The volume collapse is NOT.

We sync `Zoho_Database.cron_log` (19,377 rows), so we could check without asking.

**Confirmed — there was a 35-hour cadence gap.** `prd-cadence` last ran
**Aug 6 06:00 IST**, then nothing until **Aug 7 17:00 IST**, after which it
resumes cleanly at 4 runs/hour (every 15 min) through Aug 8. `meta-backfill` and
`mongo-sync` both ran 96×/day until Aug 6, then stopped dead on Aug 7.
`transcribe-calls` goes 0 → 193 (Aug 7) → 426 (Aug 8). **Your Vercel→ECS
migration story is corroborated in the data on all three counts.**

**Not confirmed — the dial collapse.** Relay-origin (`prd-`) dials per IST day:

| Aug 2 | Aug 3 | Aug 4 | Aug 5 | Aug 6 | **Aug 7** | Aug 8 |
|---|---|---|---|---|---|---|
| 756 | 703 | 672 | 671 | 964 | **720** | 772 |

**Aug 7 relay dials were normal.** Total Aug 7 dials were **2,390** — the
busiest day since Jul 30 — because a ~1,430-call batch with an `LQ-ro…` esid
prefix ran that day on top of normal cadence, plus 156 human dashboard dials.

So the "75 dials" figure does not match what the agent actually placed. Two
possibilities and we cannot distinguish them from here: it was measured on a
narrower window or a different metric, or something other than `prd-cadence`
was creating `prd-` dials during the gap. **Question for the relay dev: if
cadence did not run for 35 hours, what created the 720 `prd-` dials on Aug 7?**

Also please tell us what the **`LQ-ro…`** prefix is — it is 1,423 calls in one
day and it is not in the four-way taxonomy.

---

## 3. §10 Q1 answered definitively: `prd-` esid is a DIAL key, and the Zoho id is recoverable

We ran the reuse test. **39,965 `prd-` calls, 39,965 distinct esids, zero
reuse.** There is a per-dial suffix, so `call_schedules` is **not**
undercounting, and your round-1 "one per dial" statement was right after all —
no correction needed.

The masked shape is identical on all 39,965 rows:

```
prd-<19 digits>-<13 digits>
```

- **first segment = the Zoho lead id** (19 digits): **2,805 distinct** across
  39,965 relay dials
- second segment = a 13-digit epoch-ms timestamp: 39,965 distinct (the per-dial
  key)

**Important: the `$replaceOne(find:'prd-')` query in your §3 would return the
whole `{leadid}-{timestamp}` string, so it yields 39,965 distinct "lead ids"
instead of 2,805.** Split on `-` and take element 1. With that fix, your
"biggest analytical unlock" is real and we are adopting it — median 10 dials per
Zoho lead, max 55.

---

## 4. §10 Q4 answered: the 7-day lifetime DOES leak, so a count cap alone will not close it

Dials per lead across our whole history (pseudonymous keys):

- **max = 234 dials to a single lead**
- 34 leads exceed the ~63 that 9 slots × 7 days allows
- 795 leads with >20 dials consumed **30,252 calls = 61.6% of everything**
- median 3, p90 32, p99 60

**234 is 3.7× the theoretical ceiling**, so `LEAD_CALL_LIFETIME_MS` is being
reset — new lead activity, re-import, or stage change. Restoring the count cap
in `call_scheduling.ts` is still the single biggest saving on the board, but it
needs the reset path closed alongside it or the cap re-arms every time.

---

## 5. Other items closed from our side

- **§1.1 `NIGHT_DEFER_ENABLED` — independently confirmed as PROD, not staging.**
  5,077 `night_defer` rows, first 2026-07-18 08:02 IST, **last 2026-08-08 11:02
  IST**. Live and firing today. No prod/stage divergence.
- **J25 confirmed exactly as you described.** All 291 `sched-` calls carry
  `source='zoho'`; zero carry `goodcalls`. The comment at
  `sitevisit/scheduler.ts:54` is wrong. We have switched to the esid-prefix
  taxonomy and dropped `source` from our attribution.
- **Your §6 sync correction is right.** `crm_call_logs` and `crm_notes` hold
  **1 row each** in our cache and `audit_logs` 464 — consistent with "not relay
  output". We have re-labelled them.
- **The `call_transcripts` collision is real in our cache**:
  `ASBLVoiceBot.call_transcripts` 22,230 rows vs `Zoho_Database.call_transcripts`
  147 rows. Already database-qualified on our side; thank you for the warning.

---

## 6. Three collections we already sync that neither side has mentioned — please tell us who owns them

These are in `Zoho_Database` and look directly relevant to open questions:

1. **`stuck_sends` — 51,349 rows.** Columns: `lead_ref`, `message`,
   `tried_senders`, `last_error`, `at`. **This looks like the WhatsApp failure
   store you have both been trying to locate** — and it is larger than the
   28,309 figure we have been quoting. If so, `last_error` and `tried_senders`
   let us quantify sender-pool health precisely, and the "28k undelivered"
   question is answerable today. Who writes it, and is `at` the attempt time?
2. **`whatsapp_messages` — 1,450 rows** with `inbound_count` and per-date
   `inbound_json`. **So inbound WhatsApp replies ARE captured**, contradicting
   the working assumption that the estate is send-only. This is the warmest
   signal in the funnel and nobody is using it. Who writes it, and is it
   complete?
3. **`site_visit_bookings` — 59 rows.** Columns include `zoho_lead_id`,
   `call_id`, `call_status`, `project`, `type`, `visit_date` (41 non-null),
   `visit_time` (39), `visit_raw.date`, `visit_raw.slot`. **This is the
   structured booking record the agent does not have** — including the raw
   versus parsed visit date side by side, which is exactly the 89% problem
   made visible. Who writes it, why only 59 rows against 154 `PRE_SITE` calls,
   and could an `attended` field live here rather than on `Lead`?

Also present and unexplained: `call_throttle` (300 rows), `callback_log` (55),
`doc_send_log` (134), `dead_senders` (20), `call_block_list` (1),
`transcription_queue` (not found — is it named differently?).

---

## 7. What we are doing next on our side

1. Re-sync with the corrected slot field names, then re-run your B1.6 regime
   queries and send real numbers.
2. Re-base identity on `prd-{zoho_lead_id}` (2,805 leads) and cross-validate
   against `metadata.zoho_lead_id`.
3. Split every analysis at the Aug 6–7 persona boundary and flag the
   pre-Aug-8 instrumentation floor on every historical chart.
4. Quantify WhatsApp failures from `stuck_sends` once ownership is confirmed.

**Unchanged from our side: attendance is still the only thing that makes any of
this falsifiable.** Everything above improves how well we can describe the
funnel; none of it tells anyone whether a change helped.

---

*Mismatches flagged, including our own. CI Team — citeam.claudemax@gmail.com*
