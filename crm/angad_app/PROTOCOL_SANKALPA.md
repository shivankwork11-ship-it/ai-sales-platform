# SANKALPA — the operating protocol for ANGAD Analytics
*(Sankalpa: the declared vow. This file is the standing rule-book; show it to
anyone to explain how this project runs. In force from 2026-08-07.)*

**Author / signature: CI Team — citeam.claudemax@gmail.com · Built with Claude (Anthropic)**
Every source file carries this signature block in its header.

## The one goal
Make the calling agent **as golden as a human**: talk naturally, book site
visits, get them ATTENDED, and follow up like a disciplined salesperson.
Every metric, screen and audit exists only to serve that goal. Anything that
does not move connect → conversation → booking → attendance is decoration.

## The loop (loop engineering — every piece of work follows it)
1. **PLAN** — written, scoped, with the goal-link stated.
2. **PANEL AUDIT** — the plan is challenged in the six roles (CEO, CTO, PM,
   Growth, Data Science, Customer Intelligence). Objections are written down.
   No development before the panel verdict.
3. **DEVELOP** — cleanest-form code, standard practices, signature embedded.
4. **AUDIT** — front-end audit + back-end audit + PII audit (`audit_pii.py`)
   + read-only guard check (`readonly.py`). All four must pass.
5. **TEST THE DATA** — is it clean? Can a *story* be told from it? Can an
   *actionable insight* be derived without explanation? If not, back to 3.
6. **RECORD** — release log written (see Versioning), conclusions included.
7. **SELF-AUDIT** — every working turn ends with a self-audit: what was done,
   what was verified, what broke, what rule was at risk. Produced visibly.
Then the loop repeats on the next increment.

## Versioning — the Dashavatara ladder
Releases are named in the order of the ten avatars; each release gets
`RELEASES/NN-<name>.md` holding: scope, what shipped, verdicts, conclusions,
self-audit. The ladder:

| # | Name | Meaning chosen for | Status |
|---|---|---|---|
| 01 | **Matsya** | the fish that saved the Vedas from the flood — we archived the old cluster before it could be lost | ✅ SHIPPED (everything to 2026-08-07) |
| 02 | **Kurma** | the turtle that bears the churning — the daily cockpit that bears daily ops | IN PLANNING |
| 03 | Varaha | lifting the earth — data-quality & attendance write-back era | queued |
| 04 | Narasimha | the guardian at the threshold — compliance & integrity hardening | queued |
| 05 | Vamana | three steps that cover everything — full-funnel attribution | queued |
| 06–10 | Parashurama · Rama · Krishna · Buddha · Kalki | reserved | — |

## Non-negotiables (imprinted; hold at every turn)
- **NEVER write to any MongoDB.** `import readonly` before pymongo, always.
- **PII one-way forever**: pseudonyms only, no reverse map, no names/emails in
  any cache, phone/email shapes redacted from text, `audit_pii.py` after every
  import — a finding is a stop-the-line incident.
- **All regulations** (TRAI calling windows, DNC) are surfaced, never hidden.
- **Units named on every metric** (calls vs leads vs humans) — mixed units are
  how audits die.
- **Honesty over polish**: if data cannot answer a question, the screen says so.

## 10x / horizontal scaling protocol (executable at any moment)
Triggers: cache > 1M call rows, payload build > 5s, or multi-user need.
1. **Partition** the cache by month: `data/calls/year=YYYY/month=MM/*.parquet`
   (writers append-per-partition; readers glob). One flag in sync.py.
2. **Query engine swap**: pandas → DuckDB over the same parquet (SQL views,
   zero copy). The builders keep their signatures; only the load layer changes.
3. **Stateless read tier**: dashboard.py is GET-only and shares nothing between
   requests except the cache → run N processes on ports 8777-878x behind any
   reverse proxy; cache on shared disk. No code change required.
4. **Sync sharding**: one worker per collection (the OTHER_DBS map is already
   the shard list); watermark per collection.
5. **Multi-project tenancy**: `.env` becomes per-tenant
   (`MONGO_URI`,`TENANT=asbl-loft`), cache under `data/<tenant>/` — the same
   codebase serves every future project/business.
Each step is independent; execute in order only as triggers fire.

## Standing logs
- `WORKLOG.md` — everything done, running log.
- `RELEASES/` — versioned conclusions (the mythology ladder).
- `MIGRATION_AUDIT.md` — the cluster-migration forensics.
- Daily audit digests (from Kurma onward): `RELEASES/daily/YYYY-MM-DD.md`.
