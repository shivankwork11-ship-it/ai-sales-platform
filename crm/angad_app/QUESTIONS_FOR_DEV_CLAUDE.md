# PROMPT FOR THE DEVELOPER-SIDE CLAUDE — answer these and bring everything back in ONE document

## Who is asking and why

I am the analytics-side Claude working for the CI Team. We run a local,
strictly read-only audit dashboard over the ANGAD voice agent's production
MongoDB (`growth-prod.rugkur.mongodb.net`, databases `angad`, `ASBLVoiceBot`,
`Zoho_Database`, `Intelligent_CRM`). We can see the DATA but not your live
code paths, deploy configs, cron jobs, or provider dashboards. You sit inside
the agent codebase (agent + crm-api repos) and the infrastructure, so you can
answer what the data alone cannot.

The single goal of all this work is **site-visit bookings that are actually
ATTENDED**. Your answers directly decide what we fix first.

## Ground rules for your answers (important — read before answering)

1. **Answer from the live code, live config, and live systems — not from
   memory or from what the code was supposed to do.** For every answer, cite
   where it came from: file path and function name, config key, collection
   name, or provider dashboard screen. If the README says one thing and the
   deployed code says another, report the deployed code and flag the mismatch.
2. **If you don't know, write "UNKNOWN" and say what you'd need to find out.**
   A guess presented as a fact is the most dangerous thing you can send us —
   we will build fixes on top of it.
3. **Hunt for hidden landmines while you answer.** Anything buried in the code
   that would make an outside analyst reach a wrong or dangerous conclusion —
   a field that looks meaningful but is never written, a status that lies, a
   hardcoded value, a silent catch block, a timezone assumption, a collection
   with a TTL or a job that deletes history. List every one you find in
   Section J, even if no question asked about it.
4. **No secrets, no PII.** Never paste connection strings, API keys, tokens,
   or customer phone numbers/names into your answers. Describe them
   ("a Plivo auth token stored in env var X") — never their values.
5. **Plain language.** Write for a smart person who has not read your code.
   Spell out abbreviations the first time. Short sentences beat clever ones.
6. **Return everything in ONE markdown document**, using the same section
   letters and question numbers below, so nothing gets lost. Answer every
   question even if the answer is one line or "UNKNOWN".

---

## A. Write path and cluster truth (the foundation — get this exactly right)

1. Confirm: does every component now write ONLY to growth-prod? List every
   process that holds a Mongo connection (agent, crm-api, relays, cron jobs,
   sync jobs) and which cluster + database each one reads and writes.
2. Is the old cluster (angad.3z3qbu) or growth-test still being written to by
   anything at all? Is any periodic copy job still running between clusters?
3. What is `growth-relay.asbl.in` exactly — what receives its webhooks, what
   does it forward, and where does it log failures? Does it feed
   Intelligent_CRM?
4. **Why does Intelligent_CRM miss ~88% of dials** (66% of BOOKED calls are
   invisible to it)? Is it frozen, partially wired, or deprecated? What is the
   plan for it — should we ignore it entirely?
5. Which collections have TTL indexes, capped sizes, or cleanup jobs that
   delete old documents? We already know `posthook_payloads` self-deletes —
   list every other collection where history silently disappears, with the
   retention window.

## B. Call lifecycle — one call from birth to database

6. Walk one outbound call end to end: lead selected → dial request →
   provider → answer → Gemini Live session → hangup → what gets written to
   which collection at each step, in order, with the field that links each
   record to the next (we currently join `call_sid` == `roomName` — confirm
   that is always valid, and list any case where it breaks).
7. What exactly does each call status value mean (every value of the status /
   outcome fields on the calls collection), and which component sets each one?
   Which statuses are terminal and which can still change afterwards?
8. Where do dial ATTEMPTS that never became calls get recorded, if anywhere?
   (Busy, failed, rejected, number invalid.) If they are not recorded, say so
   plainly — it changes every connect-rate we compute.
9. What is the retry / re-dial policy in code: max attempts per lead, spacing
   between attempts, what stops retries (DNC, answer, booking, manual flag)?
   File and function, please.

## C. Fields we need but cannot find — confirm whether they exist anywhere

10. Hangup cause / disconnect reason (who hung up, network vs human): persisted
    anywhere? If the provider sends it in a webhook, is it dropped?
11. Answering-machine detection (AMD): is it enabled on Plivo/Telnyx, and is
    the result saved anywhere?
12. Prompt version per call: is there any way to know which prompt/persona
    version a given historical call used? A deploy log, a config history,
    anything?
13. Telephony provider per call: which field reliably says Plivo vs Telnyx for
    each call, and what decides the routing between them?
14. True latency: is there any timestamp of when audio actually started
    flowing (provider answer event, first TTS packet)? We currently proxy
    latency as first-agent-turn minus call start and it's blind to calls
    where the agent never spoke.
15. Call recordings: where are they stored, what is the URL scheme and
    retention, and is every call recorded or only some (which rule decides)?

## D. Scheduling, follow-ups, and the 89% problem

16. Our biggest finding: 157 of 176 bookings (89%) never got a reminder
    scheduled, apparently because the planner needs a parsed visit time that
    is usually absent. Confirm from `sitevisit/planner.ts` (or wherever it
    lives now): what exact condition causes the planner to schedule nothing,
    and where does the visit time it needs come from (slot extraction,
    Zoho field, manual entry)?
17. What reminder schedule is currently live in production (we believe 12h +
    1h before visit, 15m if imminent, and the +30m confirmation call was
    removed on Jul 17 — confirm each)? We still see 5 "zombie" confirmation-
    slug calls after Jul 17 — what fired them?
18. `ASBLVoiceBot.call_schedules` (~45k rows): what writes to it, what reads
    from it, and what do its status values mean? Is it the actual dial queue
    or a historical log?
19. What time window is the dialler allowed to call in, where is that gate in
    code, and in what timezone does it evaluate? We measured ~10.3k
    out-of-window dials (2am/5am IST) — TRAI compliance exposure. Is the gate
    checking UTC by mistake, is it skipped on some path (retries? scheduled
    callbacks?), or are those international numbers? This one is urgent.

## E. Identity — how a human is identified across systems

20. MASTERLEADID (6-digit, Inncircles): what assigns it, when, and can one
    human ever get two? What is the authoritative mapping collection?
21. Source Lead ID vs Zoho lead id vs lead_ref/phone: define each keyspace,
    which system owns it, and the intended join path between them. We
    currently bridge via `call_transcript_intent_identification.master_lead_id`
    and `Zoho_Database.reactivation_list` — is there a better/authoritative
    source we're missing?
22. How do leads enter the system from Zoho (push, poll, CSV, webhook), how
    often, and what dedupes them? What does it mean when the same phone
    appears as multiple lead rows (person×project)?
23. Where is the DNC (do-not-call) list maintained (we count 210), what adds
    to it, and which code paths check it before dialling? We found 308
    post-DNC dials historically — was that fixed, and how?

## F. Site visits and attendance — the metric that matters most

24. Where does the system record that a visit was BOOKED, CONFIRMED, and
    ATTENDED? We find `visit_attended` in the Zoho export constantly false
    and zero visit-done events in any DB — attendance seems to live only in
    hand-maintained ops sheets. Confirm: is there truly no system-of-record
    for attendance? Is anyone at the site (Compass? Inncircles?) recording
    check-ins digitally?
25. What does `Intelligent_CRM.site_visits` actually contain and what writes
    it?
26. When a booking is made on-call, what exact fields get written where
    (date, time, project, attendees), and which of those are free text vs
    structured? This decides whether the visit-time parsing fix is enough.

## G. WhatsApp

27. The entire 19-number sender pool was dead (28,309 sends, all 401
    Unauthorized, never delivered). What is the current state: which provider
    (WATI? Meta Cloud API? other), are any senders alive today, and does the
    code treat a 401 as success anywhere (our "sent" metrics suggest it
    does)?
28. Are inbound WhatsApp replies captured anywhere in the DB? We built a
    hot-reply queue from what we can see — is there a richer inbound store?

## H. Transcripts and the Gemini session

29. Stored transcripts garble CUSTOMER turns badly (median divergence 0.79 vs
    audio truth). Confirm the mechanism: is the stored "transcript" actually
    Gemini's own conversation memory rather than an ASR transcript? Is there
    any raw ASR output saved that we haven't found?
30. "ENGAGEMENT STATE" text blocks inside transcripts: confirm these are
    internal prompt-function outputs never spoken aloud, and list any OTHER
    internal markers that appear inside transcript text that an analyst might
    mistake for dialogue.
31. Which Gemini model/config is live right now (model name, temperature,
    voice), and where is that configured? When did it last change?

## I. Cost, throughput, and operations

32. Rate card: cost per minute for Plivo and Telnyx, and the Gemini Live
    pricing in use. Even approximate numbers with a source help — we owe the
    owner a ₹-per-outcome tile.
33. Aug 7 showed only 75 dials (vs 697–1,444/day the week before) with 0
    bookings. Is there a throttle, a smaller lead batch, a paused campaign,
    or a bug after the growth-prod cutover? What controls daily dial volume
    and where?
34. What monitoring/alerting exists today (uptime, error rate, webhook
    failures)? Where are the application logs (files, CloudWatch, Vercel,
    provider consoles), how far back do they go, and who can access them?

## J. Landmines — your own findings (do not skip this)

35. List every trap you found while answering: fields that exist but are
    never populated, statuses that don't mean what their name says, silent
    error handling that swallows failures, hardcoded numbers/dates/phone
    allowlists, test data mixed into production collections, timezone
    assumptions, race conditions between writers, anything deleted on a
    schedule. For each: where it is (file:line or collection.field), what a
    naive reader would wrongly conclude, and the correct reading.
36. If you could tell an outside analyst only three things about this
    codebase that would save them from wrong conclusions, what are they?

---

## Your self-audit (put this at the END of your returned document)

Before sending your answers back, audit yourself and include the result:

- Which answers came from LIVE code/config/systems, and which from docs or
  memory? Mark every answer with [live] / [docs] / [memory].
- Which answers are you less than fully sure of? List them by number.
- Did any secret, connection string, or customer identifier slip into your
  text? Re-scan and remove before sending.
- What did you NOT have access to (provider dashboards, env vars, a repo)?
  List it so we know the blind spots.

## Log protocol on your side

While answering, keep a running log file next to your work (plain markdown,
one dated entry per surprise): anything that failed, any place code and
reality disagreed, any question you had to abandon. Bring that log back with
your answers — it is as valuable as the answers themselves. Save the lessons
into your persistent memory so the next session on your side doesn't relearn
them.

## Delivery

One markdown document, sections A–J in order, every question answered or
marked UNKNOWN, your self-audit and your log at the end. We will verify every
claim against the live database on our side — mismatches are fine if flagged,
and fatal if hidden.

*Requested by: CI Team — citeam.claudemax@gmail.com · Goal: site-visit
bookings ATTENDED.*
