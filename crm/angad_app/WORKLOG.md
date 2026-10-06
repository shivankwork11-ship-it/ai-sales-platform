# ANGAD analytics — work log

Everything done in this engagement, in order, with current state and open items.
(Last updated 2026-08-06. All database access read-only throughout — enforced by
`readonly.py`, 19/19 write operations blocked, verified against the live cluster.)

## Phase 1 — Foundation
- Connected to the Atlas cluster (`angad.3z3qbu.mongodb.net`) via `.env`; built
  `test_connection.py` (layer-by-layer diagnostics) and `set_uri.py`
  (Compass-clipboard → .env).
- `probe.py`: full-collection profiler — exact fill rates for every field across
  all documents, server-side; writes `schema_report.md`.
- `sync.py`: Mongo → local parquet cache across **all four databases**
  (angad, ASBLVoiceBot, Zoho_Database, Intelligent_CRM), ~190k documents.
  The dashboard NEVER queries Mongo interactively.

## Phase 2 — Privacy & safety rails
- `pii.py`: every phone number pseudonymised to `LEAD-xxxxxxxx` (salted hash,
  last-10 normalisation) BEFORE touching disk; emails/names-as-keys dropped;
  phone/email shapes redacted from free text. Redaction limited to the 10–14
  digit band so 19-digit Zoho ids are never touched.
- `readonly.py`: patches pymongo so every write raises `WriteBlocked`.
  Self-test: `python readonly.py`.
- `audit_pii.py`: post-sync scanner. Caught two real incidents (phones/emails
  typed into ops-sheet name/company fields) — both fixed by scrubbing every
  text column of hand-maintained imports. Cache clean at last run.
- Optional `build_phone_map.py` + in-dashboard "Show phone numbers" toggle
  (separate local file, gitignored; badge turns orange when on).

## Phase 3 — The dashboard (`python dashboard.py`)
Local-only server (127.0.0.1, per-run token, CSRF-refused, GET-only, CSP).
Ten tabs: Numbers · Integrity · Audit · AI vs Human · Positives · Negatives ·
WhatsApp · Site visits · Intent · Call metrics. 21 multi-select filters with
chips; unit toggle (call events ↔ unique humans); date-range; transcript search.

## Phase 4 — Data correctness work (bugs found & fixed along the way)
- Per-call transcript join fixed (was per-lead → wrong slots on every call).
- Duration sanity (8-day "calls" from failed-dial timestamps → capped/flagged).
- Monotonic funnel (voicemail no longer counts as "customer spoke").
- Boolean corruption in parquet writer fixed (`is_qualified` etc.).
- Project-name normalisation across four stores; date-dtype normalisation.
- Zoho reconciliation solved exactly: 3,351 window lead-rows → 2,430 humans;
  dashboard 2,941 = unique phones dialled — different units, both right.

## Phase 5 — Cohorts, funnel, timing
- `import_zoho.py` (daily CSV) + `import_cohorts.py` (Humans & AI workbook +
  SV sheets) auto-run on every sync.
- End-to-end funnel: 3,351 rows → 2,430 humans → 2,397 dialled → ~1,270
  connected → ~1,190 real conversation → 90 booked → 66 confirmed → **16 visited**.
- AI vs Human standing tab (booked→done 24% vs 32%...), daily sheet-vs-system.
- Timing: hour×weekday connect heatmap; speed-to-lead (≤24h: 53% vs 3d+: 31%).

## Phase 6 — Integrity engine (inversion audit)
19 live rules with drill-down. Verified headline findings:
- **87.8% of dials invisible to Intelligent_CRM** (66% of booked calls blind) —
  root cause chain for 825 booked-lead redials and 308 post-DNC dials; fix is
  the webhook cutover (repoint `angad.webhooks.url`).
- **Night-defer gate leaks**: ~10.3k dials outside 09–21 IST (tz math verified).
- **WhatsApp sender pool dead** (19/19), 28,309 sends never delivered.
- Provider split: Plivo 8.0% positive vs Telnyx 2.3%.
- Pipeline-loss table + CRM-blindness-by-outcome on the Audit tab.

## Phase 7 — Executive deliverables
- `exec_report.py` → `exec_report.html`: every whiteboard question, one chart +
  one "So what" each; six-role summary strip; regenerable daily.

## Phase 8 — Adversarial review of THIS toolchain (workflow, 31 agents)
- 27 bugs claimed by two hunters; 25 adversarially verified → **24 confirmed
  real, 1 refuted, 2 unverified** (agent budget hit).
- **Per explicit instruction: NONE APPLIED.** Register preserved at
  `scratchpad/confirmed_fixes` + workflow journal `wf_01ef22ff-c95`. Highest
  impact, for whenever fixing is wanted:
  1. `SYNC-STALE-WATERMARK` (high) — incremental sync filters on `createdAt`,
     so calls updated after fetch (outcome lands at hangup) freeze stale in the
     cache; 495 affected docs measured. Workaround until fixed: use
     `python sync.py --full`.
  2. `COHORT-BORNDATE-COERCE` (high) — mixed-format born_date silently drops
     426 leads from cohort daily counts (needs `format="mixed"`).
  3. `LEADS-MERGE-FANOUT` (high) — missing dedup can fan out joined rows.
  4. B1–B4 (high, UI) — racing loads, PII badge desync, search-chip removal,
     endless busy-poll if server dies.
  (Architecture & scale review agents failed on session limits — docs pending.)

## Phase 9 — MIGRATION EVENT (current)
- User reports the Mongo has been **replaced with a new cluster**.
- **Old cluster archived first**: `data_OLD_CLUSTER_2026-08-06/` — 56 parquets,
  45 MB, + `SNAPSHOT_STATS.json` (counts, date ranges, outcome distributions,
  id-set hashes). For rolling-delete collections this archive is more complete
  than the old cluster ever was at one moment.
- Old URI preserved as `MONGO_URI_OLD` in `.env` — **still answers** as of now
  (angad.calls = 45,897, +1,213 since archive → old cluster still receiving
  traffic; worth asking the dev team which cluster the agent writes to TODAY).
- `compare_clusters.py` built: three-way forensic diff (archive / old-live /
  new-live): collection census, record-level added/removed on calls, outcome
  mutation check on common docs, **DNC-flag preservation**, webhook-URL cutover
  check, prompt content hashes, bot_settings, indexes → `MIGRATION_AUDIT.md`
  with an explicit clean-migration-or-not verdict.

## To run the migration audit (2 commands, after copying the NEW connection string in Compass)
```bash
python set_uri.py
```
```bash
python compare_clusters.py
```
Then full re-baseline against the new cluster:
```bash
python sync.py --full && python audit_pii.py && python dashboard.py
```

## Standing asks (outside this tool)
- Rotate the leaked `asbl-growth-write` credential; issue a read-only user.
- Persist per call: latency, carrier hangup cause, AMD result, prompt version.
- Webhook cutover to the new CRM; replace WhatsApp sender pool; fix night-defer.
- Attendance write-back (visit done / no-show) in Zoho.

## Phase 10 — Cutover watch + SANKALPA protocol (2026-08-07)
- Status check both clusters: webhook now points at growth-relay on BOTH sides
  (cutover of outcomes DONE); production calls still land on OLD (newest 13:28
  Aug 6); replication stale since ~11:50 Aug 6; **dialler idle 17+ hours**
  (frozen=false, paused=false — cause upstream in Zoho/campaign).
- Dashboard re-baselined on NEW cluster with read-only credential; PII audit
  CLEAN; old-salt files quarantined (data/_stale_old_salt/) — joins verified
  salt-consistent.
- SANKALPA protocol adopted (PROTOCOL_SANKALPA.md): loop engineering, panel
  gate before dev, Dashavatara release ladder, 10x horizontal-scaling runbook,
  per-turn self-audit, signature embedding.
- Release 01 MATSYA logged (RELEASES/01-matsya.md). Release 02 KURMA planned +
  panel-approved (RELEASES/02-kurma-PLAN.md): Today cockpit, Call Room
  (listen+transcript one place), Daily Audit Digest; Kurma-2: funnel board +
  follow-up view.
- ACTIVE ITEMS: build Kurma-1; re-import Zoho CSV + cohort workbooks under new
  salt (waiting on files in Downloads); dev-team answers on final sync of the
  123-call tail + write cutover timing; architecture + target docs (pending).

## Phase 11 — growth-prod cutover complete + architecture docs (2026-08-07)
- Third cluster growth-prod.rugkur verified: CLEAN migration, tail swept,
  zero loss/edits, DNC 210 intact, dialler LIVE on it (41 calls by 13:11).
- Full re-baseline; PII CLEAN; guard verified on third cluster.
- Kurma-2 shipped: funnel board (7 stages, 4,777 humans classified,
  booked-then-cold rescue queue = 9) + follow-up scheduler with recommended
  compliant dial hours.
- ARCHITECTURE_AUDIT.md written (as-is: 20 evidence-backed findings, scale
  ranking, owner-mapped fixes) + TARGET_ARCHITECTURE.md (to-be: call ledger,
  one outcome enum, MLID spine, compliance-at-edge, retention/archive,
  7-step no-big-bang migration order, replication guide).
- Night-defer leak REPRODUCED on growth-prod's first morning (9/41 dials) —
  fresh evidence attached to the dev-team ask.

## Phase 12 — VARAHA begins (2026-08-07)
- Release 03 plan + panel approval (RELEASES/03-varaha-PLAN.md). Constraint:
  no developer changes available - workarounds only, PII-compliant.
- ERA DIMENSION shipped: pre-2026-07-17 = Kaalbhojaditya era, named and
  filterable; era comparison on Numbers. First read: Kaalbhojaditya 7.9
  booked/1k dials (high-touch, 56 dials/day) vs ANGAD 2.31/1k at 12x volume;
  night-window leak is ANGAD-era only (124 vs 10,277).
- URL/SOURCE INTELLIGENCE shipped from Zoho_Database.leads already in cache
  (no CSV needed): connect%/book% by lead source, UTM, campaign, first page,
  referrer. 99-acres: 504 leads at 46.8% connect.
- LATENCY PROXY: first-agent-turn-ts minus call-start, computed at sync
  (workaround for the unpersisted metric); guarded UI note on Call metrics
  with slow-vs-fast drop-rate comparison. Lands with the running re-sync.
- METRICS HISTORY: per-day per-era headline metrics persisted to
  data/metrics_history.parquet on every sync ("keep all metrics stored").
- Agent codebase verified at Bitbucket/0708 (agent + crm-api, 131 TS files) -
  queued as Release 04 NARASIMHA: read-only quality audit of media-stream,
  gemini client, night-defer, transcription config; transcription quality
  (11% noise measured) confirmed as an agent-side fix.
- 2-lakh human-call transcription corpus announced by user - framework mining
  queued; NEED: file location.
- Process bug hit twice (doubled patch anchor + write-before-parse) - pattern
  memorised; parse-before-write now the rule.

## Phase 13 — NARASIMHA opens: agent codebase + clean-transcript corpus (2026-08-07)
- Repo read (agent + crm-api): follow-up logic = 12h/1h pre-visit reminders
  (confirmation call removed Jul 17); visit gate moved from injected turn to
  code (visitGateOpen); ASR languageCodes deliberately empty (Live API kills
  session; Vertex-only) - root cause of wrong-language stored transcripts.
- USER CORRECTIONS integrated: engagement-state is a DESIGNED prompt function
  (memory-only, never spoken) -> now stripped at analytics ingest (37 rows
  cleaned in cache + future syncs); stored transcripts are Gemini on-call
  memory - bot mostly responds correctly, the RECORD is what's wrong; clean
  re-transcription CSVs are the truth corpus.
- Era comparison table REMOVED per instruction (Kaalbhojaditya stays as
  name/filter only).
- New corpus registered: transcript_comparison.csv, site_visit_retranscripts
  (3,540 rows), new_prompt_verification_v2 (28-call matrices), human corpus
  xlsx (Sheet1, 185MB, in project root all along).
- Narasimha-1 next: retranscript ingest (PII-scrubbed) + divergence score +
  prompt verification harness + follow-up additions (callback promises,
  no-show recovery, booked-then-cold rescue, hot-reply callback, speed-to-lead)
  + persona/personality mining from the human corpus.

## Phase 14 — Follow-up discipline engine (2026-08-07, Narasimha dev begins)
- 4 new Integrity rules verify sitevisit/planner.ts's promise daily, for all
  calls: (FU1) booking without reminder - **157 of 176 bookings (89%) never
  got a reminder scheduled** - the likeliest driver of the 76% no-show rate;
  (FU2) wrong reminder offset - 3; (FU3) done-without-dial ghosts - 0;
  (FU5) removed confirmation slug still fired 5x after Jul 17.
- Daily digest now carries a per-day follow-up coverage line (today's
  bookings vs reminders scheduled) with a direct action pointer.
- Integrity engine now 23 rules. PII CLEAN after changes.

## Phase 15 — VAMANA + design contract + CIF (2026-08-07 night → 08-08)
Recovery audit (all chats + docs + dev release notes) → RELEASES/05-vamana-PLAN.md.
Shipped and browser-verified: truth layer (1,061 Zoho-vs-CRM band disagreements
surfaced; 3 data bugs fixed incl parse_mixed_date/426-leads), Inncircles-style
lead modal on every id (7 tabs incl Web History + colour Scoring), TOFU/MOFU/
BOFU Funnel tab + per-project daily stage table, one-click contract, insights
(20, role lenses), NRI/RI splits, globe, feedback widget, unit-toggle fix,
refresh-button fix (--full forced), identity solve (MASTERLEADID 71% via
intent-rows + reactivation_list), rescue lists RESCUE_SV/ (+F daily >1min
recordings), cost engine (tokens × documented rates: ₹639/booked, ₹4,995/
attended; 63.9% of spend never reaches a human), design contract (lineage
"explain this number" on all KPIs, URL-state, saved views, Ctrl+K palette,
contract-stamped exports, prev-period toggle), CIF v1.1 deterministic engine
(cif.py → 669 clean calls; dialogue-balance in-band 2.5% = bot monologue) +
CIF tab. Dev-side answers received, verified, archived
(DEV_TEAM_ANSWERS_2026-08-08.md); corrections returned (night guard IS on;
94.5% of night dials NRI; zombies = dashboard humans). FIXABILITY_MATRIX.md:
13/18 findings are small fixes. Docs spine repaired: 04-narasimha.md retro,
05-vamana.md, schema_report.md + exec_report.html regenerated, TEAM_SETUP.md.
Questionnaire for prod env vars/relay/attendance sent via user.
