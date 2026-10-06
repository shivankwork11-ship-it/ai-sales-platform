# LEARNINGS — consolidated from every answer set (living document)
*Updated 2026-08-08. Sources: dev-side answers (DEV_TEAM_ANSWERS_2026-08-08.md),
our cache verifications, the release-notes digest, the CIF first pass, and the
human-call corpus mining. One line each, with the action it changed.
Signature: CI Team — citeam.claudemax@gmail.com · Built with Claude (Anthropic).*

## About the system (change how we read data)
1. The agent is a dialler, not a strategist — volume/timing/targeting questions belong to the relay. → We stopped blaming the agent for the Aug-7 collapse; asked the relay.
2. Features ship dark: every safeguard is opt-in and may be off. → We verify runtime state from data (defer rows, accurateTranscript columns), never from code existence.
3. Join on externalScheduleId, never roomName (52.7% pending- bias). → All new joins use esid; esid prefix decodes origin (dash*=human, sched-=poller, prd-=relay).
4. Outcomes are layered guesses: cleanup fabricates NO_ANSWER, VOICEMAIL has two origins, hinglish is a default. → Buckets carry caveats on their face (DQ badges).
5. The transcript the classifier reads is <50% accurate — outcome quality is capped by transcript quality. → CIF runs only on clean transcripts; judge pass waits for the clean corpus.
6. Attendance is un-architected — the goal has no sensor. → #1 dev ask; ops sheets remain truth; every "improvement" claim is provisional until attendance lands digitally.
7. The persona can change under our feet (Ankit, Aug 7, Mongo-over-code). → Split analyses at prompt boundaries; asked for per-call prompt-version field.
8. Cost is flat per call (35k-token fixed prompt) — dial volume drives spend. → Cost lever = wasted dials (AMD, attempt tail, DROPPED), not call quality.
9. IST night ≠ customer night: 94.5% of out-of-window dials are NRI. → Compliance lens = +91 only (450 real) + the initiate-bypass door.
10. Zombie behaviours come from live-but-orphaned artifacts (dialable dead slug, stale Zoho workflows). → Retire artifacts, don't just remove their callers.

## About humans selling (change what the bot should do)
11. Winners pivot to the visit EARLY with logistics (24% WIN vs 7% STALL) — the hard visit gate has a measurable cost. → J14 experiment proposed.
12. Close = day THEN time THEN attendees ("Saturday or Sunday?" → "first half or second half?"). → Fixes the planner's parse problem at the source.
13. Promises must be kept verbatim (callback slots, day-before confirms, refix-on-cancel). → prepared_patches/03.
14. Talk less: our CIF pass shows 2.5% of calls in the healthy dialogue band; median customer turn 2.8 words. → Prompt work: check-question every 1–2 facts.

## About ourselves (change how we work — now in standing memory)
15. Verify by doing; recompute before presenting; check premises before mechanisms.
16. If a safeguard looks broken, check if it's ON before checking if it's correct.
17. Trace lineage before treating two symptoms as separate (89% ↔ transcript accuracy).
18. Chat-only knowledge dies at handoff — every finding goes to a file same-turn.
19. A release without its log is a breach; docs drift is a defect like any other.
20. Honest NA beats invented numbers; parity checks belong ON the UI, not in QA docs.
