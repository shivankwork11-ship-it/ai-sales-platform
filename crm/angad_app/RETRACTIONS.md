# RETRACTIONS
*The graveyard. Everything here was once stated as fact by us and is now known to
be wrong. It is kept because the failure teaches better than the rule — and it is
kept HERE so that it is deleted everywhere else. A live document that carries both
a wrong number and its correction propagates the wrong number: stated earlier, in
more places, and more confidently than the fix.*

*Governance: the claim gate (P1–P5) and execution gate (X1–X5). Retraction
hygiene: a retraction DELETES from the live doc and lands here.*

---

## R1 — Cost model overstated 5.9x
**Claimed:** ~₹1.7/call · ₹639/booked human · ₹4,995/attended visit, sourced as
"the dev team's documented rates ($3/$12 per 1M in/out)".
**Wrong because:** the $3/$12 card was **ours**, assumed from Pro-tier text list
pricing, and then mislabelled in a code comment as theirs.
**Class:** provenance (P1).
**Truth:** $0.50/$2.50 per 1M + ₹0.60/started minute (answered only) →
**₹0.5/dial · ₹1.33/answered · ₹246/booked · ₹1,504/attended.** Calibrated
against the dev team's observed ₹1.60–1.70/answered call; reproduces it to
within 2%.
**Caught by:** the dev team stating an observed figure that disagreed with ours.
**Gate that would have caught it:** P1 — an ASSUMED number reached
`exec_report.py` and `SHARE_WITH_TEAM.md`.

## R2 — "extractedSlots is empty across 48,306 calls"
**Claimed:** budget / timeline / config / callback / objection are 0 non-null;
therefore the scheduler reads the wrong database.
**Wrong because:** `sync.py` read `extractedSlots.budget`, `.timeline`,
`.config`, `.callback`, `.objection`. The real names are `budget_bucket`,
`timeline_bucket`, `configuration_interest`, `preferred_callback_time`,
`objection_raised`. The two we spelled correctly (`intent`, `site_visit`) were
the only two ever non-zero — the fingerprint of our own error.
**Class:** naming (P2).
**Truth:** post-fix, non-null on 264 / 173 / 150 / 177 / 1,287 calls.
**Cost:** the dev team spent a full round theorising about an outage we invented.
**Gate:** P2 — a field-name assertion in `sync.py` kills this class permanently.

## R3 — "`reached` means the line was answered"
**Claimed:** connect rate 7.4%; unanswered calls carry no telephony cost.
**Wrong because:** `reached` is False on **every** DROPPED and **every**
VOICEMAIL row — yet 5,428 of those opened a Gemini session with a median 19s of
audio. The handset was up and the carrier billed it.
**Class:** semantics (P3).
**Truth:** line opened **39.7%** · a human picked up **20.9%** · conversation
counted 7.4%. We are not failing to reach people; two-thirds of the humans who
answer hang up inside 19 seconds.
**Gate:** P3 — `FIELD_CONTRACTS.md`.

## R4 / R5 — The 89% reminder gap, two dead theories
**R4 claimed:** "the scheduler reads the wrong database" (built on R2).
**R5 claimed:** "the planner has no parsed visit time."
**R5 wrong because:** the visit date IS parsed on **63.2%** of bookings; 53 leads
(42.4%) have it and still get no follow-up; 10 get a follow-up **without** one, so
it is not even a hard requirement.
**Class:** causal (P4) — twice. A plausible mechanism was accepted as the actual
mechanism, with no kill-query written.
**Truth:** `pre-site-visit-reminders` has run **987 times**, scanned **35,291
records**, and fired **0** 24-hour and **0** 3-hour reminders, reporting **0
errors**. It runs, scans, matches nothing, and reports success.
**Gate:** P4 — the kill-query ("if the cron shows non-zero fires, I am wrong")
would have ended this at theory one.

## R6 — "No follow-ups → no dials"
**Claimed:** the cron outage explains the volume collapse.
**Wrong because:** dial volume did not collapse. prd- dials Aug 2–8: 756, 703,
672, 671, 964, 720, 772.
**Class:** causal (P4) — the chain was asserted before the dial counts were pulled.
**Open question it leaves:** what created 720 prd- dials during a cadence outage?

## R7 — "`in_sync:false` is meaningful"
**Claimed:** the prompt was out of sync with deployed code.
**Wrong because:** the dev team later said it is a bug in their own check.
**Class:** provenance (P1) — a dev-supplied flag was taken as verified.
**Truth:** persona is dated from our own transcripts instead (Aug 5: 0/210 ·
Aug 6: 3/200 · Aug 7: 602/11).

## R8 — Cron gap: "~11 days" vs "~35 hours"
**Two live readings of one quantity survived in different files for three turns**
(brain.md said ~11 days; REVIEW3_START.md and CI_REPLY_ROUND4.md said ~35h).
**Both are wrong.**
**Class:** reconciliation (P5).
**Truth, from `cron_log.ran_at`, largest-gap analysis on 6,011 `prd-cadence` runs:**
- **One continuous outage of 6 days 10h49m** — last run 2026-07-25 ~19:46 IST,
  next run **2026-08-01 06:35 IST**.
- Then **six days of degraded once-daily running** (1–6 Aug: 2, 3, 2, 2, 2, 1
  runs/day against 21–37/day before), with ~23h45m between runs.
- Then a **second gap of 1 day 10h53m**, ending 2026-08-07 17:28 — this is the
  dev team's "35 hours". It is real, but it is the *second* gap, not the main one.
- Recovery: 26 runs on 7 Aug, 74 on 8 Aug.

"~11 days" came from reading 26–31 Jul zero-rows plus the degraded period as one
continuous outage. "~35h" came from looking only at the most recent gap.

## R9 — "Every section overflows horizontally by N px"
**Claimed:** a measured UI audit across 20 sections.
**Wrong because:** measured in a browser pane that was not compositing —
`window.innerWidth` was **0**, so every layout number was garbage.
**Class:** provenance (P1) — MEASURED was claimed for numbers that were not
measurable.
**Standing note:** no page in this engagement has been visually verified by the
assistant. Anything about appearance is the user's observation, not ours.

---

## Retraction classes, tallied
| Class | Gate | Count |
|---|---|---|
| provenance | P1 | R1, R7, R9 |
| naming | P2 | R2 |
| semantics | P3 | R3 |
| causal | P4 | R4, R5, R6 |
| reconciliation | P5 | R8 |

Zero of these were reasoning errors. All nine were good logic applied to an
unverified input, at high confidence, corrected only after reaching a deliverable.

*CI Team — citeam.claudemax@gmail.com*
