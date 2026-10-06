# ANGAD dashboard — share with your team
*One page. Anyone can be running it in about 10 minutes.
Last updated 2026-08-08 · Signature: CI Team — citeam.claudemax@gmail.com*

---

## Send them three things
1. This whole `angad_dashboard` folder (zip it, or share the drive path).
2. The **read-only** MongoDB string — user `grow_team_read`. **Never a write user.**
3. This page.

## They run, once (10 minutes)
```bash
pip install pandas pyarrow pymongo python-dotenv
```
```bash
python set_uri.py
```
The string stays in a local `.env` on their machine and never leaves it.
```bash
python test_connection.py
```
Should print CONNECTED and list four databases.

## Then, every day
```bash
python sync.py
```
```bash
python audit_pii.py
```
```bash
python dashboard.py
```
The last one prints a personal link. **Run `audit_pii.py` after every sync — if
it is not CLEAN, do not use the cache.** To automate all three:
```bash
powershell -ExecutionPolicy Bypass -File .\schedule_sync.ps1 -Register -EveryMinutes 60
```

---

## READ THIS BEFORE YOU QUOTE A NUMBER

**1. Click the ⓘ on any card.** Every card states what it counts, its source
collection, and whether its contract is **FULL** (verified against a query) or
**PARTIAL** (sourced but not yet verified). **Only quote FULL cards outside the
team.** A PARTIAL card is directional.

**2. Start on `Still pending`.** It counts everything the system has left undone,
live. It is the fastest honest way into this dashboard.

**3. Check `Database health` before quoting anything stage-related.** Right now
**21 source collections are stale**, and all of `Intelligent_CRM` is ~3 days
behind. The funnel's CRM-stage side reads from it, so those numbers lag — and
part of the "Zoho vs CRM disagreement" is that lag, not disagreement.

**4. "Connected" understates contact by about 3x.** The underlying `reached`
field is False on every DROPPED and VOICEMAIL row. **A human picks up on ~21% of
dials; the Connected card shows ~7%.** Both are on the dashboard; know which one
you are saying out loud.

**5. Attendance is only 4.2% observed.** The goal — visits *attended* — has no
field anywhere upstream. The `Attendance` tab is a local ledger, and 158 of 165
bookings have no verdict at all. Every improvement claim is provisional until
that rises.

**6. NA means something specific.** Each NA states which problem it is: a judge
pass not yet run, an upstream column that is 100% null, or too few qualifying
calls. It never means zero.

---

## What each section answers

| Section | The question |
|---|---|
| **Overview** | Are we winning today, what is broken, what did it cost |
| **Daily Report** | Date × project × stage, every cell opens its leads |
| **Day by day** | Bot performance over time — pickup, turns, talk share, NRI vs RI, overlayable |
| **By project** | Leads in, fresh vs follow-up called, turns, WhatsApp, bookings, visits done |
| **Attendance** | The goal's sensor — and how much of it we cannot see |
| **Leads board** | Every lead with the Inncircles filter set |
| **Lead lookup** | Any human by MLID / Lead ID / Source Lead ID |
| **Funnel** | TOFU / MOFU / BOFU, per project |
| **Still pending** | Everything left undone, counted live |
| **Insights · Integrity · Audit** | What is failing, and the evidence |
| **Database health** | Which collections are fresh, which fields are dead |
| **CIF scores** | Conversation quality, 30 indicators |
| **QA & to-do** | QA queue by CIF category · document ledger · working list |
| **Builder metrics** | Per-turn analysis and the bot-version diff |
| **Call metrics · WhatsApp · Site visits** | Channel detail |

---

## House rules

- **Never type a customer name** into a to-do or attendance note. Phone numbers
  and emails are stripped automatically; a name written freely **cannot** be
  detected reliably. Use the MLID or Lead ID — every row links to the lead.
- **Recordings are real customer audio.** The Call Room plays them. Treat access
  to this dashboard as access to recordings, and share it accordingly.
- The dashboard is **read-only by construction** — `readonly.py` makes every
  Mongo write raise. Nobody can damage production from here.
- It binds to **127.0.0.1 only** with a per-run token. It is not reachable from
  the network. Each person runs their own copy.
- **No phone numbers, emails or credentials** are ever written to the local
  cache. Leads appear as `LEAD-xxxxxxxx` pseudonyms alongside their real
  business ids (MLID · Lead ID · Source Lead ID).

---

## The three things to raise with the dev team

1. **The site-visit reminder cron has never fired.** `pre-site-visit-reminders`:
   **990 runs, 35,291 records scanned, 0 reminders fired, 0 errors reported.** It
   is green on every dashboard they own. The fix is its fire condition.
2. **`Intelligent_CRM` has stopped being written** — lead_events, calls,
   crm_leads, persons, mlid_registry all ~3 days stale.
3. **Attendance needs a field.** Until visits-attended is written somewhere
   upstream, the business cannot observe its own goal.

*Two more worth their time: 60% of calls die between the agent's first line and
the customer's second turn; and Zoho `stage` is not a maintained field — the only
jobs that write it ran three times, unscheduled, in July.*

---

*Built with Claude (Anthropic) for CI Team. Everything stays on the local machine.*
