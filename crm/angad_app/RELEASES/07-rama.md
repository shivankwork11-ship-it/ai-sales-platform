# Release 07 — RAMA (release log, 2026-08-08)
*Plan: 07-RAMA-REQUIREMENTS-AND-PLAN.md. Everything below was verified in the
running dashboard or against the cache before being written down.
Signature: CI Team — citeam.claudemax@gmail.com · Built with Claude (Anthropic).*

## Shipped

**Shell / layout**
- Left collapsible sidebar (18 sections + "Deep dives" group), CRM pattern.
- Top bar: page title · search by MLID / Lead ID / Source Lead ID · row context.
- Toolbar with the **date range permanently visible** + 7 presets
  (Today · Yesterday · Last 7 · Last 30 · This month · ANGAD era · All time).
  Root cause of "the date filter doesn't work": it fired correctly but had been
  hidden inside a collapsed panel.
- Advanced filters moved into an Inncircles-style modal (23 fields), chips on
  the toolbar, **NRI / RI audience selector** beside the unit toggle.
- **Warm white theme** (paper, not screen) with a 6-colour categorical palette;
  the OS dark preference no longer overrides it. Feedback widget removed —
  it served no purpose.
- Equal-height KPI tiles; charts capped at 16 rows so one chart cannot eat a screen.

**Identity** — one vocabulary, no invented ids: **MLID · Lead ID · Source Lead ID**
(verified live: `995518 · 3414-LEGACY · 1272682`).

**CIF — rebuilt from the REAL dashboard**, which arrived mid-release
(`cif-scoring-dashboard/`, React + an Express→Gemini proxy):
- The **human-agent variant**: 5 parameters / **30 indicators** (Bot Performance
  is excluded for human agents — CDS and IAF stay as standalone informational metrics).
- All 30 indicators ported with their **verbatim original definitions**,
  targets and good/warn thresholds — no paraphrasing.
- FMS uses their formula: `CM·.18 + DSS·.18 + ACR·.18 + SVO·.18 + NAC·.18 + IFQ·.10`,
  with their normalisations. **16 of 30 indicators compute today**; the rest show
  NA until the judge pass.
- Section has its own sub-tabs: Overview + the 5 parameters + Per-call scores.

**Universal probe (the big one)** — `/api/slice`: click any chart bar anywhere and
get the whole story behind those rows, in four tabs: *Why these rows* (outcome /
status / drop mix) · *What they talked about* (caller intent, objections,
sentiment as a frustration signal, real call summaries) · *The leads* ·
*Every call*. Verified: BUSY → 2,758 calls across 905 leads.

**Source freshness** (Audit tab) — newest record per cached collection, with a
STALE flag past 2 days. Measures the SOURCE, not our sync time. 4 collections
currently stale.

**Incremental sync corrected** — the documented watermark bug is fixed. It
watermarked on `createdAt`, but a call is created at dial and **mutated** at
hangup, so every row that changed after first fetch froze forever. Now it
watermarks on `updatedAt` (falling back to `createdAt`), with a **90-minute
overlap window** for in-flight writes and clock skew. `--full` stops being
mandatory; refreshes get cheap and stay correct.

**New Zoho collections wired into sync** — the dev created a Gemini-2.5-Pro
transcription pipeline *inside Zoho* the night of 2026-08-07:
`call_transcripts`, `crm_call_logs`, `crm_notes`, `cron_log`, `call_throttle`,
`audit_logs`. Their own words: *"calling agent galat transcript bhejta tha isliye
followups misfire hote the"* — which independently confirms our transcript finding.

**Clean-transcript pipeline hardened** — the importer now runs inside sync (it
never did, so tomorrow's batch would never have arrived) and **merges instead of
overwriting**, keeping a timestamped backup.

## Audit fixes (35-agent adversarial review, 27 confirmed gaps)
- **Headline KPI strip was completely inert** — `innerHTML` copying destroyed
  every click handler. This was the real cause of "clicking does nothing".
- **`build_drill` had been deleted by my own CIF splice** — called but undefined.
  Restored with parity fixes.
- Card-vs-drill formula mismatches fixed (Humans connected union mask; ₹ summed
  unrounded then rounded once; Median talk time declared a *measure*, so a
  statistic no longer reports a false MISMATCH).
- Cards no longer advertise drills that do not exist.
- CIF failures can no longer take down the whole payload.
- Leads tab no longer blanks permanently after a zero-match filter.

## DB probe findings (see DB_PROBE_FINDINGS.md)
- **The scheduler reads the wrong database.** `angad.calls.extractedSlots` is
  empty (budget/timeline/config/callback/objection = 0 across 48,306 calls;
  site_visit on 109) while the same intelligence exists in `ASBLVoiceBot`
  (timeline 3,894 · objections 2,095 · budget 246). First reading was wrong
  ("classifier writes nothing") and was corrected before reporting.
- **62.6% of all calls** go to leads already dialled 20+ times: 805 leads,
  30,255 calls, 3.9% connect vs 7.5% overall, 42 bookings, ~₹35,776.
- `sentiment` and `closingScore` are 100% null — no quality score exists.
- A Zoho export sat unimported because the watched folder does not exist.

## Polish pass (after your screenshot)
- **Typography**: Inter-first stack with antialiasing, tabular numerals and
  legibility rendering — the old `ui-sans-serif` fallback was rendering muddy.
- **Tile alignment fixed properly**: the ⓘ was consuming a label line and
  breaking the 2-line clamp. Tiles are now an explicit CSS grid
  (28px label / 32px value / rest), so **every value in a row shares one
  baseline** — verified: row 1 five tiles all at y=313, row 2 three tiles all
  at y=447.
- Reclaimed the wasted third toolbar row; chips read "Compare to previous
  period" and "Save this view" instead of stray glyphs.

## Round 2 + project funnel additions
- **Funnel by project** — one row per building with its own funnel on click
  (LEGACY books at 6.0% vs LOFT 2.6% on the same engine). A lead touching two
  projects counts under both; stated on the panel, not hidden.
- **Persona dated from our own data** using the dev team's method: the agent
  names itself in the greeting. Aug 5: 0 Ankit / 210 Radhika · Aug 6: 3 / 200 ·
  **Aug 7: 602 / 11**. Clean cutover on 7 Aug. The insight no longer cites
  `in_sync`, which the dev team says is a bug in their own check.
- **Comparison baselines** now include **Kaalbhojaditya (pre-17 Jul)** as the
  real "previous period": 56 dials/day, **23.0% connect, 12.41 booked per 1k**
  — rates the current era has not matched at 35x the volume.
- Round-2 answers archived as `DEV_TEAM_ANSWERS_ROUND2.md`; open asks written to
  `DEV_ASKS_ROUND3.md` (AWS/log requests formally withdrawn — their refusal was
  correct). Team handover simplified to one page: `SHARE_WITH_TEAM.md`.

## Open
RAMA-7 (Leads board full Inncircles filter set + per-call history detail) ·
RAMA-8 (to-do logs, downloadable capture, QA dashboard by CIF category) ·
RAMA-9 (builder/production metrics, per-turn graphs, new-vs-old prompt diff) ·
CIF judge pass for the remaining 14 indicators.
