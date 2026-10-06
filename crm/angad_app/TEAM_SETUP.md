# ANGAD dashboard — run it on YOUR laptop in 10 minutes
*Anyone with a read-only Mongo URI can run this. Nothing ever writes to any
database — a guard blocks all 19 write operations before pymongo can fire.
Signature: CI Team — citeam.claudemax@gmail.com · Built with Claude (Anthropic).*

## One-time setup
1. Install Python 3.11+ and run: `pip install pandas pyarrow pymongo`
2. Copy this whole `angad_dashboard` folder to your machine.
3. Get the READ-ONLY connection string (user `grow_team_read`) from the CI team
   — never a write user.
4. Run `python set_uri.py` and paste the URI when asked (it stays in your local
   `.env`, never leaves your machine).
5. Verify: `python test_connection.py` → should print CONNECTED and list the
   4 databases (angad, ASBLVoiceBot, Zoho_Database, Intelligent_CRM).

## Daily use
```
python sync.py --full        # pull fresh data (10–20 min; always --full)
python audit_pii.py          # must say CLEAN
python dashboard.py          # prints your personal local URL — open it
```
Or click **Fetch fresh data from Mongo** inside the dashboard.

## What you're looking at
- **Today** — the cockpit: dials, bookings, digest, Call Room (audio + transcript).
- **Funnel** — TOFU / MOFU / BOFU from BOTH truth systems (Zoho + Intelligent_CRM),
  disagreements shown, per-project daily stage table.
- **Insights** — what each role (CEO / CTO / Growth / CI / DS / builder) should
  read first, with click-through evidence.
- **Click any lead id anywhere** → the lead opens in a modal with Call History,
  WhatsApp, Web History, Lead History (every transaction), Stage History, Scoring.
- **Globe** — where the calls go (RI vs NRI), drag to spin.
- **✎ Feedback** (bottom right) — your note lands in RELEASES/feedback.jsonl,
  tagged with the tab you were on. The CI team reads them all.

## Rules (non-negotiable)
- The dashboard is 127.0.0.1-only with a per-run token. Do not port-forward it.
- Phone numbers are one-way pseudonymised (LEAD-xxxxxxxx); no reverse map exists.
- If `audit_pii.py` ever reports a finding: stop, tell the CI team.
