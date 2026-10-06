# Release 02 — KURMA (PLAN)
*The turtle that bears the churning.* The daily cockpit: one screen, minimal
clicks, everything auditable from cards that open depth on click.
Signature: CI Team — citeam.claudemax@gmail.com

## Goal-link
Every card exists to increase booked visits ATTENDED. Nothing else qualifies.

## Scope (Kurma-1)
1. **TODAY screen** — new default tab. Cards: Calls · Connected · Real
   conversations · Booked · Visits due today · Follow-ups due · Integrity
   criticals · WhatsApp hot replies. Click any card → the pre-filtered deep
   view (existing tabs reused; zero new click-paths to learn).
2. **CALL ROOM** — listen + read in ONE place. Auto-picked daily best calls
   (booked / strong intent) and worst calls (tq-flagged, mid-pitch drops,
   DNC) with audio player (recording joined per call) beside the transcript.
   One click from the Today screen. Expired/auth-gated audio degrades to
   transcript-first with a clear label.
3. **DAILY AUDIT DIGEST** — auto-generated: what went RIGHT (bookings,
   confirmations, good calls) / what went WRONG (new integrity violations vs
   yesterday, webhook failures, night leaks, idle dialler). Stored dated in
   RELEASES/daily/, rendered on the Today screen.
4. **FUNNEL BOARD** — every lead classified by stage (kanban counts,
   click-through lists), source × time × topic cuts.
5. **FOLLOW-UP VIEW** — scheduledcalls due + recommended next-dial hour per
   lead from the connect-rate-by-hour model + attempt curve.
6. **LIVE LOG** — event feed reconstructed from data (call landed, booking,
   DNC, webhook fail), newest first.

## Panel audit (held 2026-08-07)
- **CTO**: PASS with conditions — (a) audio URLs may be expired (Telnyx
  presigned) or auth-gated (Plivo): the player MUST handle 403/404 gracefully,
  transcript-first; (b) no new server surface: cockpit = same GET API, same
  token; (c) scaling protocol steps must not be blocked by cockpit code —
  builders stay stateless. ACCEPTED into scope.
- **PM**: cut scope to ship — Kurma-1 = Today + Call Room + Digest;
  Funnel board + Follow-up view = Kurma-2. ACCEPTED (two increments).
- **CEO**: the digest must LEAD with bookings & attendance, not calls. ACCEPTED.
- **Growth**: source/URL intelligence requires the Zoho export re-imported
  under the new salt — BLOCKED until the daily CSV lands in Downloads.
  Flagged as external dependency.
- **Data Science**: recommended-hour model must show its confidence (dial
  counts), never a bare recommendation. ACCEPTED.
- **Customer Intelligence**: hot-reply queue gets a card on Today. ACCEPTED.
- **Verdict: APPROVED — development begins (Kurma-1).**

## Definition of done (the loop, steps 4-6)
Front-end audit + back-end audit + PII audit + guard check all pass; a
non-technical reader can state tomorrow's three actions from the Today screen
alone; daily digest file appears for the current data-day; release log
written as RELEASES/02-kurma.md.
