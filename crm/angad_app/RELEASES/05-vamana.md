# Release 05 — VAMANA (release log, 2026-08-08)
*Recovery + full-funnel truth + the design contract. Plan: 05-vamana-PLAN.md.
Every item below was verified in the live browser before being logged.
Signature: CI Team — citeam.claudemax@gmail.com · Built with Claude (Anthropic).*

## V1 — Truth layer
`truth()` in dashboard.py: canonical person/project/stage joins (no arbitrary
row-picking on the 18% multi-project humans), Zoho-vs-Intelligent_CRM band
disagreement surfaced (1,061 leads), outcome-taxonomy boundary rules
(29-Jun/7-Jul/16-Jul), `parse_mixed_date` (fixes the 426-dropped-leads
COHORT-BORNDATE-COERCE bug), audience map (Telnyx→NRI / Plivo→RI).

## V2 — Lead modal (Inncircles layout)
Click any id anywhere (48k cells) → modal with Overview · Call History ·
WhatsApp · Web History · Lead History (lead_events transactions) · Stage
History · Visits & Reminders, plus the colour scoring model
(green/purple/orange) from the July asks. Honest NA cards where data is absent.

## V3 — TOFU/MOFU/BOFU Funnel tab
Bands from both truth systems (verified: TOFU 1,593 · MOFU 1,213 · BOFU 13),
disagreement rows listed, per-project daily stage table in the ops
Growth-Reports layout.

## V4 — UX layer
One click contract (lead→modal, bar→filter, KPI→drill) · borderless depth ·
KPI hierarchy · scope chips · fixed-window badges · chart width caps ·
unit toggle made real (daily+funnel now human-aware; verified 5,715→3,143).

## V5 — Insights + instruments
20 role-lens insights (red = systemic failure only) · unified glossary ·
NRI/RI filter + split calling-time chart · interactive globe (canvas,
drag/zoom, NA cards for absent geo) · feedback widget → RELEASES/feedback.jsonl
· TEAM_SETUP.md · ₹-per-outcome cost engine (exact tokens × documented rates:
~₹1.7/call · ₹639/booked · ₹4,995/attended · 63.9% of spend never reaches a
human) with by-outcome spend chart.

## Design contract (user spec 2026-08-08) — status per rule
1. Every number a link — **KPIs done** (8/8 drillable); chart bars filter;
   id cells open the modal. Remaining: minor counts inside prose notes.
2. Aggregate = drill-through query — bands/tables share the truth layer;
   formal count-equality assertions NOT yet automated (queued).
3. metric→cohort→lead→event — **done** (KPI → tab → lead modal → Lead
   History event rows with timestamps).
4. URL-reproducible views — **done** (filter state in location.hash,
   restored on load).
5. "Explain this number" — **done** for all 8 headline KPIs (lineage
   registry: definition, numerator/denominator, window, scope, source,
   exclusions). Coverage of every minor number: rolling.
6. Inline data-quality badges — **done** on contaminated KPIs (transcript-
   quality flags, implausible durations); more metrics rolling.
7. Global filters as removable chips — pre-existing, now URL-encoded.
8. Comparison as a toggle — **done** (△ vs previous equal-length period on
   headline KPIs; date-window comparison, labelled).
9. Saved investigations — **done** (📌 Save view; pin/reopen/right-click-delete).
10. Command palette — **done** (Ctrl+K: lead ids, tabs, scopes).
11. Raw query console — **NOT built** (deliberate: a query console on a
    PII-pseudonymised local dashboard needs its own safety review; queued).
12. Exports carry filter state — **done** (filename + header row with window,
    filters, row counts, timestamp).

## Docs spine repaired (V6)
RELEASES/04-narasimha.md retro-logged · schema_report.md regenerated against
growth-prod · exec_report.html regenerated · WORKLOG.md brought current ·
DEV_TEAM_ANSWERS_2026-08-08.md archived in-project with our verification.

## Data story test (step 5) — run against the user's own complaints
Filters apply everywhere or say they don't; clicking is one contract; the
transaction ledger is visible per lead; TOFU/MOFU/BOFU exists; disagreements
are shown not averaged; every headline number explains itself. Remaining
gaps are stated above, not hidden.
