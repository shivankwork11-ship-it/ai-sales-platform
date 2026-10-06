# Release 06 — PARASHURAMA (CIF merge + layout redesign) — PLAN
*2026-08-08. One cycle, executed immediately after this plan (SANKALPA step 1).
Deadline context: clean transcriptions arrive tomorrow evening — everything must
work with today's corpus and absorb tomorrow's drop with zero code changes.
Signature: CI Team — citeam.claudemax@gmail.com · Built with Claude (Anthropic).*

## Panel (step 2, six roles, one line each)
CEO: the home screen must answer "are we winning?" in 5 seconds — outcome first.
CTO: no new moving parts; the transcript drop must be a data event, not a deploy.
PM: story order — Overview → Funnel → Leads → CIF → everything else; nothing hidden.
DS: truth marks (band + disagreement) must ride on EVERY lead row, not one tab.
Growth: cost and CIF must sit beside bookings, not in separate silos.
CI: the modal is the workbench — CIF scores must appear per call inside it.

## Scope (one cycle)
**P1 — Layout redesign.** Nav regrouped into a story: primary [Overview ·
Funnel · Leads · CIF · Insights · Review · Globe] + "Deep dives" group
[Numbers · Integrity · Audit · AI vs Human · Positives · Negatives · WhatsApp
· Site visits · Intent · Call metrics · Eras]. Today tab becomes **Overview**:
cockpit + top systemic-failure insights + funnel-band strip + cost strip —
one screen answers "are we winning, what's broken, what does it cost".

**P2 — Truth marks everywhere.** add_ids() gains Band (+⚠ on disagreement)
from the truth layer, so every lead row on every tab carries its funnel truth.

**P3 — CIF merged, not tabbed.** CIF chips in the lead modal (per-lead scores
in Call History context), CIF strip on Overview, CIF columns already in tables
via its tab; per-lead CIF summary API.

**P4 — Tomorrow-proof transcript pipeline.** Refresh button chain becomes
sync → audit gate → cif.py, so the moment tomorrow's clean transcriptions are
imported, CIF rescoring is automatic. Import path documented on the CIF tab.

**P5 — Cost explanation.** Cost lineage panel extended (fixed-vs-variable
split explained); the finance rate-card + invoice ask is already in the sent
questionnaire §5 — tracked on the chase list.

## No-error discipline
py_compile + node --check after every file edit · browser verification of each
P before the next · audit_pii before release log · release log 06-parashurama.md
closes the cycle.
