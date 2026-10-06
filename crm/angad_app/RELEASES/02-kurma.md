# Release 02 — KURMA (Kurma-1 SHIPPED 2026-08-07)
Signature: CI Team — citeam.claudemax@gmail.com · Built with Claude (Anthropic)

## Shipped
- **Today cockpit** — new default tab: 9 live cards (dials, connected,
  conversations, booked, follow-ups due, WhatsApp replies, DNC, out-of-window,
  webhook failures) with vs-previous-day deltas; every card is a single click
  into the pre-filtered deep view (integrity cards open the exact rule's
  offending rows directly).
- **Call Room** — best-3 and worst-3 calls of the latest data day, audio player
  + agent/customer transcripts side by side in one place; playable-URL policy
  (stable Plivo only, presigned/expired → transcript-first; 1 URL excluded for
  phone digits in path). CSP relaxed by exactly one directive (media-src https:).
- **Daily audit digest** — right/wrong/do-this-now, rendered on Today and
  auto-saved to RELEASES/daily/<day>.md (first file: 2026-08-06.md).
- Idle-dialler banner: cockpit leads with the ops-critical fact when calls stop.

## The four audits (loop step 4)
- Front-end: default tab correct; 9 cards; 3-column digest; 6 players; 12
  transcript panes; card-click lands on Integrity WITH drill-down open; zero
  console errors; no horizontal overflow. PASS
- Back-end: payload builds on live cache; thin-day pool widening works;
  deltas computed; daily file idempotent. PASS
- PII: audit CLEAN (56→51-file cache, zero names/phones/emails). PASS
- Guard: all 19 Mongo write ops blocked. PASS

## Data-story test (loop step 5)
The Today screen, read cold, states its own actions: "dialler idle 19h — ask
ops"; "63 out-of-window dials — open the rule"; "3 booked — listen to what
worked in the Call Room". PASS.

## Deferred to Kurma-2
Funnel classification board · follow-up scheduler view with recommended dial
hours · source/URL intelligence (blocked on Zoho CSV re-import under new salt).

## Kurma-2 (SHIPPED same day, 2026-08-07)
- **Funnel board** — every dialled human classified from FULL history into 7
  stages (DNC / booked-then-cold / booked / not-interested / in-conversation /
  answered-only / never-answered); stage cards click through to the lead lists.
  First classification: 4,777 humans — 58.9% never answered, 1,084 in
  conversation, 112 booked, 9 booked-then-went-cold (the rescue queue).
- **Follow-up scheduler view** — pending scheduled calls (20) + a recommended
  next-dial hour per workable lead (own answered-hour history preferred,
  global best compliant hour with confidence otherwise; 09-21 IST only, per
  the DS-panel condition).
- Audits: front-end (7 cards, drill-through verified with real click, no
  overflow), back-end (classification totals reconcile to unique humans), PII
  CLEAN, guard 19/19. Data-story test: the board names its own work queues
  (rescue the 9, work the 1,084) unprompted. PASS.
