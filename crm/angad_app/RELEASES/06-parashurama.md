# Release 06 — PARASHURAMA (release log, 2026-08-08)
*CIF merged, layout redesigned, drill pop-boxes everywhere. Plan:
06-parashurama-PLAN.md. Every item browser-verified before logging.
Signature: CI Team — citeam.claudemax@gmail.com · Built with Claude (Anthropic).*

## Shipped (one cycle, no errors — py_compile + node --check green on every edit)
- **P0 Operating frameworks** written to standing memory (runs every turn):
  imprints, SANKALPA, design contract, verification rules.
- **P1 Layout redesign**: story-order nav [Overview · Funnel · Leads · CIF ·
  Insights · Review · Globe | Deep dives: …]; Today → **Overview** with
  systemic-failure strip, funnel-band strip, cost strip — "are we winning,
  what's broken, what does it cost" on one screen.
- **P2 Truth marks everywhere**: add_ids() now stamps **Band** (+⚠ on
  Zoho-vs-CRM disagreement) on every lead row in every table, including drills.
- **P3 CIF merged**: per-lead CIF chips in the lead modal Overview (verified on
  1002-LOFT), CIF tab, CIF finding (#18 bot monologue) in matrix + insights.
- **P4 Tomorrow-proof pipeline**: refresh chain = sync → CIF rescore
  automatically; new clean transcriptions need zero deploys — import, refresh,
  scores update. (Corpus formats handled: plain + [mm:ss]-stamped.)
- **P5 Cost explanation**: lineage panel now explains the flat-cost structure
  (fixed 35k-token prompt vs variable); finance rate-card + invoices already
  requested in questionnaire §5.
- **Drill pop-boxes (user spec, added mid-cycle)**: clicking ANY headline
  number opens the Inncircles-style box: definition + the EXACT constituent
  rows under current filters + **aggregate-vs-rows parity displayed on the
  box** ("card shows 11 · drill returns 11 — MATCH ✓"). A mismatch renders
  red as a build-breaking bug with a feedback prompt (contract rule 2, live).
  Chain depth 4: metric → constituents → lead modal → raw events.

## Verified in browser this cycle
Nav divider renders · Overview strips (3 fails, 5 bands, cost line) · drill
box parity MATCH on Booked (11=11) with Band column present · modal CIF chip ·
/api/drill correct under browser filter state.

## Audits
py/js syntax green per edit · PII audit CLEAN · read-only guard untouched ·
data-story test: the user's Inncircles screenshots are now the dashboard's
own behaviour.

## Cycle 2 additions (same release, 2026-08-08 late)
- **LEARNINGS.md** — 20 consolidated learnings from every answer set (system,
  selling, ourselves), each with the action it changed.
- **DEV_IMPROVEMENT_PLAN.md** — 5 habits (each from a real incident), a
  one-day fix list (12 items, mostly env vars/one-liners), and 4 working
  agreements with the builder team.
- **PROTOCOLS_INDEX.md** — every plan and protocol in plain words, with
  one-go execution steps: imprints, SANKALPA, design contract, verification
  rules, daily refresh, the clean-transcription drop runbook, SV rescue,
  dev handoff, the release ladder, and which document to read when.
- **Collapsible filter hero** — filters collapsed by default into a slim bar
  showing active chips + a count badge; opens on click (progressive
  disclosure); chips removable from the collapsed bar; Reset all inline.
- **Insights single panel** — four-stat summary (failures / warnings /
  observations / all) that filters the list on click, plus a "Do this first"
  line naming the top systemic failure.
- **Calling turns embedded (CIF in our layer)** — new **Conversation** tab in
  the lead modal: `/api/turns` renders the clean transcript exchange by
  exchange as a chat, with timestamps and per-turn CIF marks (dead reply ·
  brief · engaged · elaborated · asked a question · MONOLOGUE) and the call's
  dialogue balance. Verified live: 44 turns, 23 agent / 21 customer.
  The LLM judge's Progressed/Resolved/Stalled/Regressed labels attach to these
  same rows when the judge pass runs.
- **Bug found and fixed while verifying**: `build_profile` uppercased the
  whole pseudonym (`LEAD-2216680f` → `…680F`), so every direct pseudonym
  lookup silently returned an empty profile. Prefix-only uppercase now; the
  turns lookup is case-insensitive as a second guard.

## Open
Drill coverage beyond headline KPIs (visits/CIF/WA cards fall back to lineage
panel with an honest flag) · LLM judge pass for CDS→FMS when tomorrow's clean
transcriptions land · questionnaire answers pending (env vars, relay, prompt
diff, attendance, invoices).
