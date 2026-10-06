# Release 07 — RAMA: full requirements, then the plan
*2026-08-08. Written BEFORE any code, as instructed. Reference: the user's
Inncircles screenshots (Lead Logs, Filter modal, Date picker, dense table,
lead modal, Daily Leads Reports + its click-through pop-up) and the CIF v1.1
spec. Signature: CI Team — citeam.claudemax@gmail.com.*

---

## PART 0 — What I got wrong (stated plainly, so the plan corrects it)

| # | What I did | What was asked |
|---|---|---|
| 1 | Never opened the CIF dashboard folder | "look into the existing dashboard for CIF" — **FINDING: that folder is empty.** It holds `CIF_v1.1_Corrected 1.docx` and a leftover `.vite/deps_temp_*` cache. There is no code in it. The spec doc IS there and is complete (6 parameters, 38 indicators, FMS). **I need the actual CIF dashboard repo path, or I build from the spec.** |
| 2 | Top tab bar | **Left collapsible sidebar**, CRM-style, like Inncircles |
| 3 | CIF = one small tab | **CIF is a big section with its own sub-tabs** (one per parameter + FMS) |
| 4 | Invented `MLID`, `Band`, `SourceLeadID` columns | **Use Inncircles' own ids**: MLID (= the 6-digit Inncircles master id), Source Lead ID, Lead ID. No new id vocabulary. |
| 5 | Collapsed the date picker out of sight | **Date range always visible in the toolbar** with presets (Today / Yesterday / Last 7 / Last 30 / This Month / Last Month / YTD / Custom) exactly like the screenshot |
| 6 | Globe as a canvas toy | Ugly and low-value → **cut it** (or reduce to a country table). Space goes to the work surfaces. |
| 7 | Patched an already-large page.html | **Rewrite clean and lightweight**; no dead code |
| 8 | Drill only on headline KPIs | **Every number, everywhere**, resolves to its constituents, recursively |

---

## PART 1 — REQUIREMENTS (the complete list, nothing dropped)

### R1. Shell / layout (Inncircles pattern)
- R1.1 **Left sidebar**, collapsible to icons, sections grouped; active item highlighted.
- R1.2 **Top bar**: page title · search-by-id (MLID / Lead ID / Source Lead ID / phone-pseudonym) · row count on the right.
- R1.3 **Toolbar row**: entity selector · filter button (opens the Filter modal) · **date-range picker with presets** · export · unit toggle · refresh. All always visible.
- R1.4 **Filter modal** (like their Filter dialog): stage · transitioned stage · status · source · project · outcome · era · NRI/RI · qualified · broker · WhatsApp · transcript quality · attempts · duration · hour · weekday · language · prompt. Apply / Clear.
- R1.5 Active filters shown as removable chips under the toolbar.
- R1.6 Sizing discipline: one card grid, fixed row heights, no stretched charts, nothing wider than the content column.

### R2. Sections (the left nav)
1. **Overview** — the daily cockpit: what happened today, what's broken, what it cost.
2. **Daily Report** — the Growth-Reports table: Date · Week · Project × [New Lead · Lead Initiated · Pre Site Visit · Post Site Visit · Sales Closure · Not Interested · Flat Blocked · Total]. **Every cell clickable** → pop-up with Total Count, date, searchable list, Download (their exact interaction).
3. **Leads (Transactions DB)** — every lead as a row with all Inncircles ids and its live state; search; click → lead modal.
4. **Funnel** — TOFU / MOFU / BOFU with the disagreement flag; click a band → its leads.
5. **CIF** — big section, own sub-tabs: **FMS overview · Bot Performance (8) · Conversation Quality (6) · Lead Quality (6) · Engagement (6) · Interest Level (6) · Call Quality (6) · Conversations (turn browser)**. Every indicator card states: definition, target, current value, n, and NA if it needs the LLM judge.
6. **Calls** — call-level table + call metrics + cost.
7. **WhatsApp · Site visits · Intent · Integrity · Audit · Insights · Review panel** — retained from the old dashboard (nothing lost).

### R3. Drill contract (recursive)
- R3.1 Any number → pop-up of its **constituents** (the rows that make it up), with count parity shown.
- R3.2 Any constituent row → the **next level down** (lead → its calls → one call → its turns/events).
- R3.3 Terminal level = a raw record: one call with timestamp, outcome, recording, transcript turns; or one event with actor+time.
- R3.4 Pop-ups carry: Total Count · the filter state · search · Download (their pattern).

### R4. Lead modal (Inncircles layout)
Header strip: MLID · Project · Stage · Source · Born date · Status.
Tabs: **Overview · Call History · Conversation (turns) · WhatsApp History · Web History · Lead History (transactions) · Stage History · Scoring**.
Prev/next arrows to walk the current list. NA cards where data genuinely absent.

### R5. Identity (no invented ids)
Columns everywhere, in this order: **MLID · Lead ID · Source Lead ID · Project**.
MLID = the Inncircles 6-digit master id (what we currently compute as
MASTERLEADID). The 4-digit registry id is internal plumbing — hidden by default.

### R6. Data truth (retained)
Truth layer joins; Zoho-vs-CRM stage disagreement surfaced; taxonomy-break
warnings; contamination badges; honest NA; every metric explains itself.

### R7. Code quality
One clean page.html (sectioned, commented, no dead code), one dashboard.py
with a clear builder-per-section structure, no duplicated logic, lightweight
payloads (no 48k id-cells shipped to the browser).

---

## PART 2 — THE PLAN (order of execution, one cycle each, verified between)

**RAMA-1 Shell.** New clean page.html: sidebar + topbar + toolbar with the
always-visible date presets + filter modal + chips. Port existing sections in
untouched so nothing is lost. Verify: every old section still renders.

**RAMA-2 Identity.** Backend: MLID column = Inncircles master id; internal
registry id demoted; Band moves from a column to a lead-modal fact.
Verify: tables show MLID/Lead ID/Source Lead ID only.

**RAMA-3 Daily Report.** The Growth-Reports table with per-cell click →
pop-up (Total Count + search + Download). Verify: cell count == pop-up rows.

**RAMA-4 CIF section.** 6 parameter sub-tabs + FMS overview + Conversations
browser, all 38 indicators listed with definition/target/value/NA.
Verify: every indicator from the spec is present.

**RAMA-5 Recursive drill.** Constituent pop-ups on every number in every
section; each row drills further; terminal = raw record.

**RAMA-6 Cleanup.** Remove the globe; delete dead code; size every card;
lighten payloads. Verify: page weight and load time measured before/after.

---

## PART 3 — EXECUTION STATUS (updated 2026-08-08, verified in browser)

| Step | Status | Evidence |
|---|---|---|
| RAMA-1 Shell | **DONE** | Left sidebar (18 items + "Deep dives" group, collapsible to icons); top bar with id-search; toolbar with **always-visible date range + 7 presets** (Today/Yesterday/Last 7/Last 30/This month/ANGAD era/All time); filters moved into a modal (23 fields); page title follows the section |
| Date filter | **FIXED** | Root cause: it worked, but I had hidden it inside a collapsed panel. Now permanent in the toolbar. Verified: preset click → window 2026-08-02→08-08, data reloads |
| RAMA-2 Identity | **DONE** | Columns are now **MLID · Lead ID · Source Lead ID** — verified live: `995518 · 3414-LEGACY · 1272682`. The invented `MASTERLEADID`/`Band`/4-digit-MLID columns are gone |
| RAMA-4 CIF | **DONE** | Big section, 8 sub-tabs: Overview + the 6 CIF parameters + Per-call scores. All **38 indicators** registered with weight/target/value; **18 computed** from the clean corpus, 20 marked NA pending the LLM judge. `cif.py` rewritten to compute FES, RSR, OQR, DSS, FRS, AUI, ACR, EI, EM, FIL, SCS, TD, OC, IPS on top of CM/EBR/RDI/DER |
| Globe | **REMOVED** | Replaced by a Geography section of real tables (RI vs NRI counts, countries, landing pages, project sites). Dead canvas code deleted |
| RAMA-3 Daily Report | **NEXT** | The Growth-Reports table with per-cell click → pop-up (Total Count + search + Download) |
| RAMA-5 Recursive drill | **PARTIAL** | Headline KPIs drill with count parity; band/CIF/lead chains work; needs extending to every remaining number |
| RAMA-6 Cleanup | **PARTIAL** | Globe code removed; full dead-code sweep and payload slimming still to do |

**First CIF findings on the current clean corpus (669 calls):** dialogue balance
in the healthy 40–60% band on only **2.5%** of calls · median response depth
**2.8 words** (target >15) · median Decision Signal Score **0.0 of 7** (target
>3.5) · median organic questions **0** (target >3). The bot is talking; the
customer is not.

---

**Blocking question for you:** where is the built CIF dashboard? The folder
you pointed me at has only the spec. If it lives elsewhere, give me the path
and I will match its design exactly; otherwise RAMA-4 is built from the
CIF v1.1 spec, which is complete.
