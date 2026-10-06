# DEV IMPROVEMENT PLAN — for the ANGAD builder team
*From the CI Team, 2026-08-08. Grounded in their own answers doc + our data
verification. Tone: every finding here was verified, none is blame — the same
audit found their code careful in many places (strict 401 handling, correct
timezone math, guarded outcome writes). This is the path from "careful code"
to "trustworthy system". Signature: CI Team — citeam.claudemax@gmail.com.*

## Part 1 — Five habits to change (the principles, each from a real incident)

**1. A feature that ships dark is not shipped.**
Incident: night guard, AMD, accurate transcription, alerting — all coded, all
defaulting OFF, several never enabled. Practice: every flag gets (a) a line in
a FLAGS.md with its prod value, (b) exposure on a /status endpoint like your
own /api/prompt-version (that endpoint is the house pattern — copy it), and
(c) a data-side test stated in the PR ("if on, collection X gains rows").

**2. The absence of an action needs a record, same as the action.**
Incident: null visitAt → empty plan → silence; suppressed dials → no row;
rejected PRE_SITE (visit gate) → no trace; WhatsApp failures → fire-and-forget.
Practice: every early-return on a business path writes a reason row or at
minimum a counter. "We didn't do X because Y" must be queryable.

**3. Inferred values carry provenance.**
Incident: cleanupStaleCalls writes fabricated NO_ANSWER indistinguishable from
carrier truth; mirror writes status:'called' at insert. Practice: any guessed
write carries a source field (outcomeSource:'cleanup'|'carrier'|'classifier').
One enum column ends an entire class of analyst error.

**4. Fields that downstream systems schedule against are contracts.**
Incident: site_visit_requested.date is free text; the whole reminder system
starves on it (89%). Practice: validate at write; reject-and-log on contract
violation; resolve relative dates at the classifier (it already does this for
callbacks — the asymmetry IS the bug).

**5. Instrument the goal before optimising the proxy.**
Incident: attendance (the company goal) has no field, no endpoint, no path.
Practice: before any conversion work, land the attendance write-back. One
field. Everything else is unmeasurable until then.

## Part 2 — The one-day fix list (all verified, all small)
| Fix | Size |
|---|---|
| Set CALL_WINDOW=8-21 on prod task (closes every night path incl. /api/calls/initiate) | env var |
| Set TRANSCRIBE_POSTCALL=true (already coded, async, no live-call risk) | env var |
| Classifier prompt: give site_visit_requested.date the same "resolve to next future occurrence vs CURRENT_TIME_IST" rule callbacks already have | prompt line |
| Persist hangup cause + AMD result + prompt version + provider on Call | 4 fields |
| outcomeSource field on cleanupStaleCalls writes | 1 field |
| Log rejected PRE_SITE attempts from the visit gate (measure J14's cost) | 1 line |
| Retire/gate the site_visit_confirmation slug (dashboard humans still fire it) | small |
| Fix the misleading "(visit past/imminent)" log message | 1 line |
| Upgrade pending- roomName on hangup webhook too (kills the 52.7% join trap at source) | small |
| DNC regex: add Telugu/Tamil/Kannada patterns (live compliance gap) | small |
| Rotate the committed Bitbucket token; decide on public /health auth | ops |
| Apply prepared_patches/01-03 (visit-time parser, null-visit fallback, promise-keeping planners) — sandbox-verified by us | review+apply |

## Part 3 — Working agreements with the CI side
1. Env truth: after any prod env change, ping us — our dashboards encode flag
   states as facts.
2. Prompt pushes are data events: bump a per-call version field; never swap
   personas silently (Ankit, Aug 7).
3. Weekly: we return the FIXABILITY_MATRIX delta + CIF trend; you return
   deploy/flag changes. 15 minutes.
4. Attendance write-back design: one session with ops + Inncircles owner —
   we bring the join keys, you bring the endpoint.
