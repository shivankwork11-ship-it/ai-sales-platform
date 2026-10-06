# Release 04 — NARASIMHA (PLAN, approved 2026-08-07)
*The guardian at the threshold* — the agent codebase itself. Repo:
`C:/Users/Tiwari/Documents/USage/Angad/BItbucket/0708` (agent + crm-api).
Signature: CI Team — citeam.claudemax@gmail.com

## Evidence base (verified this turn)
1. **Engagement-state — DESIGNED, not a bug (user-corrected)**: it is a
   prompt-side internal function; never spoken on calls; it enters Gemini's
   memory and therefore the stored transcription. 37 transcripts carried it —
   now STRIPPED at analytics ingest (sync + cache cleaned in place) so
   transcript metrics stay honest. No agent change required for this item.
2. **ASR hallucination root cause**: gemini/client.ts deliberately leaves
   inputAudioTranscription.languageCodes empty — setting it kills the session
   on the PAYG Live API (their comment: verified live; Vertex-only feature).
   Result: customer turns transcribed as Italian/Spanish/etc. Mitigation is
   prompt-side only. The re-transcription CSVs (user-supplied) prove audio is
   recoverable: clean re-transcripts exist for the same calls.
3. **Stored-transcript unreliability (user-corrected framing)**: the bot
   mostly RESPONDS correctly on-call; it is the STORED memory transcription
   that garbles customer turns ("own use" → "on you"). Consequence: analytics
   built on stored transcripts understate customer quality — the clean
   re-transcription corpus is the truth source for conversation analytics.
   Residual behaviour misses exist (e.g. "first week of August" → booked
   "Saturday morning") and the divergence score will separate the two.
4. **Follow-up logic today** (sitevisit/planner.ts): PRE_SITE → reminders 12h
   + 1h before visit (single 15m if imminent); +30m confirmation call removed
   2026-07-17 (harassment). Night-defer + scheduler in place but leaking
   (our Integrity data: out-of-window dials daily).

## Scope Narasimha-1 (analysis + prepared patches in the local repo copy)
A. **Transcript truth pipeline**: treat bot-memory transcripts as
   non-authoritative; ingest the re-transcription CSVs (PII-scrubbed) into the
   cache as `retranscripts` and surface bot-vs-audio divergence per call in
   the Call Room; quantify misheard-answer rate on the 3,540-row corpus.
B. **Prompt verification harness**: `new_prompt_verification_v2` +
   repo prompt (prompt.ts / ankit-male.ts): check the live prompt version
   against observed behaviours (visit-gate discipline, leak absence,
   wrong-language recovery rule §3) — a repeatable checklist, not a one-off.
C. **Follow-up additions** (from the funnel board + human patterns), proposed
   as concrete planner.ts extensions:
   - callback promises honoured: bot says "tomorrow morning" → a real
     ScheduledCall (today it evaporates);
   - no-show recovery: visit time passed + no attendance → next-day rebook;
   - booked-then-cold rescue (our 9-lead queue) at +48h;
   - WA hot-reply → call within 15 min during window;
   - fresh-lead speed: born → first dial ≤30 min (connect 53% vs 31% proof);
   - visit-morning WA: location pin + expected duration.
D. **Persona & personality frameworks** (from human corpus + clean AI calls):
   mine the 2-lakh human transcriptions (Sheet1, 185MB xlsx already in the
   project folder) for: opening lines that survive 10s, objection pivots,
   Hinglish register, closing sequences; distil into a persona spec
   (Radhika v-next) with DO/DON'T exemplars. PII-scrubbed at ingest.

## Panel minutes
- CTO: repo patches PREPARED in the local copy, clearly marked, never pushed
  by us — deployment stays human. Leak metric added to Integrity. ACCEPTED.
- Product: misheard-answer rate is the new booking-quality metric; false
  bookings explain part of the 24% show rate. ACCEPTED.
- DS: bot-vs-audio divergence needs a per-call score, not anecdotes. ACCEPTED.
- CI: retranscript ingest must pass audit_pii (files carry names+phones). ACCEPTED.
- CEO: everything maps to bookings ATTENDED (false bookings are the leak
  between booked and attended). APPROVED.
