# Fixability matrix — every confirmed finding: real? fixable? how? who?
*2026-08-08. Verdicts: each finding was verified (our data, dev's live code, or both)
before appearing here. Effort: S = one line/env var · M = a day · L = a project.
Signature: CI Team — citeam.claudemax@gmail.com · Built with Claude (Anthropic).*

| # | Finding | Real? | Fixable? | How | Owner | Effort |
|---|---|---|---|---|---|---|
| 1 | Attendance un-architected (system can't see its goal) | ✔ dev-confirmed | ✔ | ONE field written back from ops sheet to Lead (or one webhook endpoint); dashboard already consumes ops truth | Dev + ops | **S–M** |
| 2 | 89% bookings unreminded (visit date free text, no fallback) | ✔ both sides | ✔ | prepared_patches/01+02 (parser + null-fallback) AND classifier relative-date rule (dev: one prompt instruction, mirrors callback handling) | Dev | **S** |
| 3 | Outcomes/slots decided on <50%-accurate transcript | ✔ dev's own figure | ✔ | `TRANSCRIBE_POSTCALL=true` (already coded, async, no live-call risk); later: re-run classifier on accurate transcript | Dev | **S** |
| 4 | Persona swapped to male Ankit in prod, silently | ✔ live endpoint | ✔ | Decision needed: A/B or rollback; then stamp prompt version per call (1 schema field) so this is never invisible again | Owner + dev | **S** |
| 5 | pending- roomName join bias (52.7% of calls) | ✔ our count | ✔ ours: done | We join on externalScheduleId now; dev fix = upgrade roomName on hangup webhook too | Both | **S** |
| 6 | NO_ANSWER fabricated by cleanupStaleCalls | ✔ code | ✔ | Add `outcomeSource:'cleanup'` flag on that write (1 line); we already caveat the bucket | Dev | **S** |
| 7 | +91 night dials (450) + /api/calls/initiate bypass | ✔ our count | ✔ | Set `CALL_WINDOW=8-21` on prod task (one env var — closes every path) | Dev/infra | **S** |
| 8 | DNC has no Telugu/Tamil/Kannada patterns | ✔ code | ✔ | Extend regex list; interim: classifier already detects language — flag DNC-ish Telugu calls for review | Dev | **S–M** |
| 9 | Visit gate blocks sub-4-min genuine bookings, invisibly | ✔ code | ✔ | Log rejected PRE_SITE attempts (1 line) → measure the loss before changing the threshold | Dev | **S** |
| 10 | WhatsApp: 19-sender pool dead, 28k sends relay-side | ✔ both | ✔ | Periskope portal: reconnect/replace senders; add pool-health alert; find the relay's sender | Ops + relay | **M** |
| 11 | Zombie confirmation slug (human-dialable) | ✔ our esid scan | ✔ | Retire slug or gate it in isValidSlug; tell the ops-dashboard user | Dev | **S** |
| 12 | Hangup cause / AMD result / latency / prompt-version not persisted | ✔ code | ✔ | 4 schema fields + one-line writes each (dev's own suggestion) | Dev | **S–M** |
| 13 | 1,061 leads in different funnel bands (Zoho vs Intelligent_CRM) | ✔ our truth layer | ◐ | Surfaced (not averaged) on our Funnel tab; root fix needs the relay/CRM owner to reconcile stage writers | Relay owner | **L** |
| 14 | Intelligent_CRM missing 88% of dials | ✔ our recon | ? | Cause unknown until relay answers §4 of the questionnaire | Relay owner | ? |
| 15 | Aug-7 volume collapse | ✔ | ? | Agent ruled out (dev); relay request counts for Aug 4–7 will settle it | Relay owner | ? |
| 16 | Bitbucket token committed; public health endpoints | ✔ dev-flagged | ✔ | Rotate now (questionnaire §7); decide on endpoint auth | Dev/infra | **S** |
| 17 | Recordings: old Telnyx URLs may be dead; retention unknown | ✔ dev | ◐ | Ask retention (questionnaire §5); download-and-archive clean corpus recordings before the window closes | Us + dev | **M** |
| 18 | Bot monologues (CIF: 2.5% of calls in the 40–60% dialogue band; median customer turn 2.8 words) | ✔ our CIF pass | ✔ | Prompt work: check-questions every 1–2 facts (human-corpus pattern §1.12); measure with CIF after each prompt change | Owner + dev | **M** |

**Bottom line: 13 of 18 are small fixes (S), and 6 of those are literally one env var or one line.**
The three unknowns (14, 15, part of 17) all wait on the relay/portal answers already requested.

## Addendum sent to the dev side (prompt diff request)
Bring back BOTH prompt texts so we can diff what actually changed on the phone:
1. The ORIGINAL production prompt (Radhika v3.8.1 — the last version live before Aug 5).
2. The CURRENT production prompt (ankit-v3.8-male-v6.0.0 from Mongo, the one live now).
Plain text, redact any embedded secrets/numbers. We will diff: persona/gender,
closing ritual, visit-gate wording, language rules, fact blocks — and map each
diff to the CIF metrics and booking rate so the swap's effect is measured, not argued.
