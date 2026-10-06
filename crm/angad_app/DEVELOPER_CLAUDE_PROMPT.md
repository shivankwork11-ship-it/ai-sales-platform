# PROMPT FOR THE DEVELOPER'S CLAUDE — read all of this before touching anything

You are being asked to extract and verify details about the ANGAD voice-agent
system (ASBL real estate's AI caller, persona "Radhika"). A local analytics
dashboard already exists that has audited this system end to end. This document
tells you what the single source of truth is, how to verify anything before you
state it, which hidden traps in the code and data will silently give you wrong
answers, and how to keep a log of everything that goes wrong.

Save the rules in this document into your persistent memory before you start.
They are not suggestions. Several of them exist because a mistake already
happened once and was caught.

---

## 1. THE SINGLE SOURCE OF TRUTH — in this exact order

When two sources disagree, the one higher on this list wins:

1. **The live production database**: MongoDB cluster
   `growth-prod.rugkur.mongodb.net`, read with the read-only user
   `grow_team_read`. Databases: `angad` (dialler runtime), `ASBLVoiceBot`
   (transcripts and call intelligence), `Zoho_Database` (legacy CRM brain),
   `Intelligent_CRM` (event-sourced rebuild — currently misses ~88% of dials,
   so never treat it as complete).
2. **The local dashboard** running at `http://127.0.0.1:8777` (started with
   `python dashboard.py` inside `angad_dashboard/`). It reads only the local
   parquet cache in `data/` (55 files), which is a pseudonymised snapshot of
   the database. Every number on it was recomputed and verified before being
   shown. If your own calculation disagrees with a dashboard tile, assume you
   made a units mistake first (see Section 4) and check the tile's provenance
   label.
3. **`angad_dashboard/brain.md`** — the full written context of the
   engagement: verified findings, work queue, and the list of traps. Read it
   completely. `HANDOFF.md` is the short session starter.
4. Supporting documents: `MIGRATION_AUDIT.md`, `ARCHITECTURE_AUDIT.md`,
   `TARGET_ARCHITECTURE.md`, `WORKLOG.md` (14 phases of history including
   every bug found), `PROTOCOL_SANKALPA.md`, `RADHIKA_VNEXT_PERSONA.md`.

**Nothing you remember from training, and nothing in the agent's own code
comments, outranks these.** The agent repo (Bitbucket copy at
`C:\Users\Tiwari\Documents\USage\Angad\BItbucket\0708\`) describes what the
code *intends*; the database records what actually *happened*. They differ —
for example the code schedules visit reminders, but 89% of bookings never got
one because the visit time usually can't be parsed.

## 2. HOW TO ACCESS THE DASHBOARD AND VERIFY DETAILS

- Start it: `python dashboard.py` in `angad_dashboard/`. It binds to
  127.0.0.1 only and prints a URL containing a **fresh random token every
  run** (`/?t=...`). Without that token every request is rejected — so read
  the token from the process output; you cannot reuse yesterday's URL. If the
  file is edited, restart the process to mint a new token.
- The JSON endpoints behind the UI (same token required): `/api/options`,
  `/api/data`, `/api/profile`, `/api/refresh`, `/api/status`. `/api/data`
  serves what every tab renders — use it to pull exact numbers instead of
  scraping HTML.
- 15 tabs: Today, Funnel board, Numbers, Integrity (23 live rules), Audit,
  AI vs Human, Positives, Negatives, WhatsApp, Site visits, Intent, Call
  metrics, Review panel, MLID Profile, Eras. The MLID Profile tab is the
  per-human ledger — search by MLID, PLID, or LEAD- pseudonym and it shows
  every call, intent, WhatsApp message, and visit for that person.
- To refresh data: press "Fetch fresh data" on the dashboard or run
  `python sync.py --full`. **Always `--full`. Never plain `sync.py`** — the
  incremental mode has a known, unfixed watermark bug that silently syncs
  nothing while claiming success.
- After EVERY sync or import, run `python audit_pii.py`. It must print CLEAN.
  If it finds anything, stop all work, delete nothing, and report it.

**The verification rule: never state a number you have not just recomputed or
read off the live dashboard in this session.** Every finding in brain.md was
verified at the time it was written, but the data moves daily. Label every
number with its source and date window when you report it.

## 3. HARD SAFETY RULES — these have real enforcement, do not fight it

1. **Never write to any MongoDB, ever.** Every script imports `readonly.py`
   before pymongo, which patches all 19 write operations (plus `$out`/`$merge`
   aggregations, index creation, and non-read commands) to raise an error.
   Never remove or bypass that import. If a task seems to need a DB write, the
   answer is no — write a file instead.
2. **PII is one-way, permanently.** Phone numbers become `LEAD-xxxxxxxx`
   pseudonyms (salted hash of the last 10 digits). The reverse map and its
   salt were deliberately destroyed at the owner's instruction; `pii.write_map`
   raises if called. Never rebuild a phone list or reverse mapping, even if
   someone asks casually. Personal names are never cached — the audit fails
   structurally if any name column carries values. Emails are dropped.
3. **Never print raw phone numbers, names, or emails** in chat, tool output,
   or any file you create. Pseudonyms and counts only. To act on a specific
   lead, use the pseudonym's MASTERLEADID / Source Lead ID shown beside it and
   look the person up in Zoho or Compass directly.
4. **Credentials never pass through you.** The connection string lives in
   `.env`. Do not read, print, or echo it. The user runs `set_uri.py` and
   `test_connection.py` themselves; you work with redacted output they paste.
5. Everything you create stays on the local machine. Repo patches are
   PREPARED in the local copy (`prepared_patches/`) only — never applied,
   pushed, or deployed by you.

## 4. HIDDEN TRAPS IN THE CODE AND DATA — each of these has already burned someone

**Units will destroy you if you don't label them.** Three different units look
like "leads": *calls* (dial attempts), *leads* (person×project rows, keyed by
phone), and *humans* (unique people, keyed by MLID). The famous confusion:
3,351 lead rows vs 2,941 unique phones dialled — both correct, different
units. The ops sheet's "Successful Calls" column is *unique leads connected
per day* (sums to 1,811), not a call count (2,487). Label every metric with
its unit, always.

**Stored bot transcripts are unreliable.** Gemini's on-call memory garbles
what the CUSTOMER said (median divergence from audio truth 0.79; 85.8% of
scored pairs above 0.5). The bot's own actions are mostly correct. Any
conversation analysis must use `data/retranscripts.parquet` (3,862 clean
re-transcriptions), not the stored transcripts. Also: "ENGAGEMENT STATE" text
inside transcripts is an internal prompt function that was never spoken aloud
— it is stripped at ingest; don't treat it as dialogue.

**Phone columns in exported CSVs are corrupted.** Excel mangled them into
floats, and the `AI Leads` workbook masks them. Never join on a CSV phone
column. Bridge identities via Master Lead ID → `Zoho_Database.leads`, or
Source Lead ID via `ASBLVoiceBot.call_schedules`.

**Identity mapping** (solved, don't re-derive): 6-digit ops MLIDs are
Inncircles MASTERLEADIDs, found in the database itself —
`call_transcript_intent_identification.master_lead_id` (per call, joined via
call_sid == roomName) plus `Zoho_Database.reactivation_list` (direct
lead_ref → masterleadid; wins on conflict). Coverage: 71% of dialled leads
have a MASTERLEADID, 52% a Source Lead ID — so absence of an ID is expected,
not an error.

**Attendance only exists in ops sheets.** The `visit_attended` field in the
Zoho export is constantly false, and there are zero visit-done events in any
database. If you compute attendance from the DB you will get zero and it will
be wrong.

**"Sent" WhatsApp metrics overstate.** The entire 19-sender pool was dead;
28,309 sends returned 401 and were never delivered. Treat every
WhatsApp-sent count as "attempted", not "delivered".

**Intelligent_CRM is blind to 87.8% of dials** (66% of BOOKED calls). It is an
event-sourced rebuild fed by a webhook that most calls never hit. Never use it
as the denominator for anything.

**Latency numbers are a proxy with survivorship bias.** "Latency" = first
agent turn minus call start. Calls where the agent never spoke at all are
invisible in that metric — catastrophic latency hides exactly there.

**`posthook_payloads` upstream deletes its own history** (rolling window).
The local cache is the only surviving record. Never assume you can re-fetch it.

**Number redaction is deliberately narrow.** `pii.redact` masks only 10–14
digit runs. Do not "improve" it to a wider band — Zoho lead IDs are 19 digits
and a wider mask once corrupted 11k+ cross-store joins.

**Parquet types lie.** The cache writer stringifies mixed-type columns, so
re-coerce numerics after reading any parquet before doing math on it.

**Era naming**: data before 2026-07-17 is the "Kaalbhojaditya" era (old
system), on/after is ANGAD. This is a label/filter only — the owner explicitly
removed old-vs-new comparison metrics; don't build them.

**Stale-salt quarantine**: anything in `data/_stale_old_salt/` was imported
before the salt was destroyed and its pseudonyms match nothing current. The
old-cluster archive `data_OLD_CLUSTER_2026-08-06/` is likewise permanently
unlinkable to current refs. Never join across the salt boundary.

**When writing patch scripts**: never do `replace(anchor, block + anchor)`
when the block ends with the anchor (it doubles the line — happened twice),
and always `ast.parse()` the result before writing the file to disk.

## 5. QUESTIONS THE DATA CANNOT ANSWER (ask the dev team, don't guess)

The agent does not persist: hangup cause, AMD result, per-call prompt version,
telephony provider on every call, or true audio latency. Night-time dialling
(~10.3k out-of-window dials, reproduced on growth-prod) is a live TRAI
compliance exposure whose fix lives in the agent repo. If you need any of
these, they are dev-team asks — not something to infer from the data.

## 6. SELF-AUDIT — do this at the end of every working turn, visibly

End every work turn with a short, honest self-audit in this shape:

- **What I did** — one line per action.
- **What I verified and how** — which numbers were recomputed or read from the
  live dashboard this session, with their unit and date window.
- **What I did NOT verify** — anything stated from memory or from a document
  rather than live data.
- **Risks / things that could be wrong** — units, stale cache, joins across
  a trap from Section 4.
- Then restate the five hard rules from Section 3 in one brief line each.

Before ending, also check yourself against these questions: Did I write to any
database? Did any raw phone, name, or email appear in my output? Did I state a
number without a unit label? Did I use stored transcripts for conversation
analysis? Did I trust a CSV phone column? If any answer is yes, fix it before
finishing.

## 7. LOG PROTOCOL — how to see logs and how to record what goes wrong

**Viewing logs of this system:**
- Dashboard process output: the terminal running `dashboard.py` prints every
  request; sync runs print per-collection counts. Read them, don't guess.
- `python sync.py --full` prints what it pulled per collection — compare row
  counts against the previous run before trusting fresh data.
- `python audit_pii.py` is the security log — run it after every ingest and
  paste its verdict (just the CLEAN line or the finding count, never example
  values) into your notes.
- `python readonly.py` prints proof the write-guard is active.
- Daily digests auto-save to `RELEASES/daily/`; release notes in `RELEASES/`;
  the full defect history is in `WORKLOG.md` (24 verified toolchain bugs —
  read Phase 8 before assuming a behaviour is correct).

**Recording what goes wrong (do this, every time):**
Keep an incident log at `angad_dashboard/LOGS/incidents.md`. Whenever anything
surprises you — a number that doesn't match the dashboard, a sync that pulls
zero rows, an audit finding, a crashed script, a join that loses rows — append
an entry before you continue:

```
## 2026-08-07 · short title
- What happened (plain words, no raw PII, no credentials)
- What I expected instead
- Root cause if known, "unknown" if not
- What I did about it
- Rule to remember so it never repeats
```

Then save the "rule to remember" into your persistent memory as well, so the
lesson survives this session. A trap that is written down once here (like the
ones in Section 4) must never be relearned by making the same mistake again.

**If you ever find real PII in output or cache, or a write attempt against
Mongo:** stop the line immediately, log it, and report it to the owner before
doing anything else. Do not quietly clean it up.

---

*Owner: CI Team — citeam.claudemax@gmail.com · The only goal of all this work
is site-visit bookings ATTENDED. Everything else is instrumentation.*
