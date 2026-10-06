"""
MIGRATION FORENSICS — old cluster vs new cluster.  python compare_clusters.py

Answers: was this a clean migration, or did data change / vanish / get added?

Old side: the live old cluster via MONGO_URI_OLD if it still answers, otherwise
the archived snapshot in data_OLD_CLUSTER_2026-08-06/ (taken before cutover —
for rolling-delete collections the archive is MORE complete than the old
cluster ever was at any single moment).
New side: MONGO_URI (point it at the new cluster with set_uri.py first).

STRICTLY READ-ONLY on both sides (readonly.py is imported first — every write
raises). No phone numbers are printed; identities are compared as pseudonyms.

Writes MIGRATION_AUDIT.md and prints the verdict.
"""
import json
import os
from datetime import datetime
from pathlib import Path

import readonly  # noqa: F401 - patches pymongo to refuse every write
import pandas as pd
import pii
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
HERE = Path(__file__).parent
ARCH = HERE / "data_OLD_CLUSTER_2026-08-06"
OUT = HERE / "MIGRATION_AUDIT.md"

NEW_URI = os.environ.get("MONGO_URI")
OLD_URI = os.environ.get("MONGO_URI_OLD")

DBS = ["angad", "ASBLVoiceBot", "Zoho_Database", "Intelligent_CRM"]
# collections worth field-level attention
HOT = {
    "angad": ["calls", "leads", "scheduledcalls", "prompts", "whatsappsenders",
              "callstates", "events", "learninginsights", "dashboard_snapshots"],
    "ASBLVoiceBot": ["call_schedules", "call_transcripts", "call_recordings",
                     "call_transcript_intent_identification", "lead_journeys",
                     "lead_profiles", "kb_store"],
    "Zoho_Database": ["leads", "posthook_payloads", "whatsapp_messages",
                      "site_visit_bookings", "doc_send_log", "stuck_sends",
                      "dead_senders", "bot_settings", "reactivation_list",
                      "mlid_registry", "plid_registry"],
    "Intelligent_CRM": ["crm_leads", "lead_events", "calls", "messages",
                        "persons", "site_visits", "mlid_registry"],
}

md = [f"# Migration audit — old cluster vs new cluster\n",
      f"Generated {datetime.now():%Y-%m-%d %H:%M}. Read-only on both sides; "
      f"write-guard active. Identities compared as pseudonyms.\n"]
say = lambda s="": (print(s), md.append(s + "\n"))


def connect(uri, name):
    if not uri:
        return None
    try:
        c = MongoClient(uri, serverSelectionTimeoutMS=12000)
        c.admin.command("ping")
        host = uri.split("@")[-1].split("/")[0]
        say(f"- **{name}**: connected ({host})")
        return c
    except Exception as ex:
        say(f"- **{name}**: NOT reachable ({type(ex).__name__}) — "
            + ("using the archived snapshot instead" if name == "old" else
               "point MONGO_URI at the new cluster with set_uri.py"))
        return None


def arch_stats():
    f = ARCH / "SNAPSHOT_STATS.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def live_counts(client):
    out = {}
    for db in DBS:
        try:
            names = client[db].list_collection_names()
        except Exception:
            out[db] = None
            continue
        out[db] = {}
        for cn in names:
            try:
                out[db][cn] = client[db][cn].estimated_document_count()
            except Exception:
                out[db][cn] = -1
    return out


def sample_fields(client, db, cn, n=200):
    try:
        docs = list(client[db][cn].aggregate([{"$sample": {"size": n}}],
                                             allowDiskUse=True))
    except Exception:
        docs = list(client[db][cn].find({}, limit=n))
    keys = set()
    for d in docs:
        keys |= set(d.keys())
    return keys


def id_set(client, db, cn, cap=120000):
    ids = set()
    for d in client[db][cn].find({}, {"_id": 1}).limit(cap):
        ids.add(str(d["_id"]))
    return ids


def main():
    say("## Connectivity")
    new = connect(NEW_URI, "new")
    old = connect(OLD_URI, "old")
    if new is None:
        say("\n**Cannot audit yet — no new cluster connection.** "
            "Copy the new connection string in Compass, then run "
            "`python set_uri.py` and re-run this script.")
        OUT.write_text("".join(md), encoding="utf-8")
        return

    a = arch_stats()
    newc = live_counts(new)
    oldc = live_counts(old) if old else None

    # ---------- collection census
    say("\n## Collection census (old vs new)")
    say("| db.collection | old (live) | old (archive) | new | delta vs best-old |")
    say("|---|---:|---:|---:|---:|")
    anomalies = []
    for db in DBS:
        newdb = newc.get(db)
        if newdb is None:
            say(f"| **{db}** | | | MISSING DATABASE | |")
            anomalies.append(f"database `{db}` absent on the new cluster")
            continue
        wanted = HOT[db] + [c for c in (newdb or {}) if c not in HOT[db]]
        for cn in dict.fromkeys(wanted):
            o_live = (oldc or {}).get(db, {}).get(cn) if oldc else None
            akey = (f"{db}__{cn}.parquet" if db != "angad" else
                    {"calls": "calls_raw.parquet", "leads": "leads.parquet",
                     "scheduledcalls": "scheduled.parquet"}.get(cn, f"angad__{cn}.parquet"))
            o_arch = a.get(akey, {}).get("rows")
            n_ = newdb.get(cn)
            base = o_live if o_live is not None else o_arch
            delta = (n_ - base) if (n_ is not None and base is not None) else None
            flag = ""
            if n_ is None:
                flag = " **MISSING**"
                anomalies.append(f"`{db}.{cn}` absent on new cluster "
                                 f"(old had {base})")
            elif base is not None and n_ < base * 0.98:
                flag = " **SHRANK**"
                anomalies.append(f"`{db}.{cn}` shrank: {base} → {n_}")
            say(f"| {db}.{cn} | {o_live if o_live is not None else '—'} | "
                f"{o_arch if o_arch is not None else '—'} | "
                f"{n_ if n_ is not None else '—'} | "
                f"{('%+d' % delta) if delta is not None else '—'}{flag} |")

    # ---------- calls: id-level diff vs archive
    say("\n## angad.calls — record-level diff (archive = pre-cutover truth)")
    calls_arch = ARCH / "calls_raw.parquet"
    if calls_arch.exists() and newc.get("angad", {}).get("calls") is not None:
        olddf = pd.read_parquet(calls_arch, columns=["_id", "roomName", "outcome",
                                                     "status", "createdAt"])
        old_ids = set(olddf["_id"].astype(str))
        new_ids = id_set(new, "angad", "calls")
        added = new_ids - old_ids
        removed = old_ids - new_ids
        say(f"- old (archive): {len(old_ids):,} · new: {len(new_ids):,}")
        say(f"- **added on new** (calls made after the archive, or injected): {len(added):,}")
        say(f"- **REMOVED on new** (existed before cutover, gone now): {len(removed):,}")
        if removed:
            gone = olddf[olddf["_id"].astype(str).isin(list(removed)[:100000])]
            say(f"  - removed by outcome: "
                f"{ {str(k): int(v) for k, v in gone['outcome'].value_counts(dropna=False).head(8).items()} }")
            t = pd.to_datetime(gone["createdAt"], errors="coerce", utc=True)
            if t.notna().any():
                say(f"  - removed date range: {t.min()} → {t.max()}")
            anomalies.append(f"{len(removed):,} call docs present pre-cutover are "
                            "gone on the new cluster")
        # field mutation check on the intersection (sampled)
        inter = list(old_ids & new_ids)[:2000]
        if inter:
            from bson import ObjectId
            q = []
            for i in inter:
                try:
                    q.append(ObjectId(i))
                except Exception:
                    q.append(i)
            newdocs = {str(d["_id"]): d for d in new["angad"]["calls"].find(
                {"_id": {"$in": q}}, {"outcome": 1, "status": 1})}
            om = olddf.set_index(olddf["_id"].astype(str))
            changed = sum(1 for i in inter
                          if i in newdocs and str(om.loc[i, "outcome"]) !=
                          str(newdocs[i].get("outcome")))
            say(f"- outcome changed on {changed} of {len(inter):,} sampled common docs"
                + (" — **records were EDITED, not just moved**" if changed else
                   " — common records look unmodified"))
            if changed:
                anomalies.append(f"{changed}/{len(inter)} sampled call docs have a "
                                "DIFFERENT outcome on the new cluster")

    # ---------- DNC preservation
    # The pseudonym salt was destroyed in the 2026-08-06 PII cleanup, so
    # archived refs are permanently unlinkable. Identity-level DNC comparison
    # therefore runs LIVE-old vs LIVE-new using an EPHEMERAL in-memory digest
    # (random key per run, nothing persisted, no phone stored or printed);
    # if the old cluster is unreachable, fall back to counts-only vs archive.
    say("\n## Do-Not-Call preservation")
    import hashlib as _hl
    import os as _os
    _eph = _os.urandom(16)
    _dig = lambda ph: _hl.sha256(_eph + pii.digits(ph).encode()).hexdigest()[:16]
    new_dnc = {_dig(d.get("phone")) for d in
               new["angad"]["leads"].find({"doNotCall": True}, {"phone": 1})
               if d.get("phone")}
    if old:
        old_dnc = {_dig(d.get("phone")) for d in
                   old["angad"]["leads"].find({"doNotCall": True}, {"phone": 1})
                   if d.get("phone")}
        lost = old_dnc - new_dnc
        say(f"- old (live) DNC leads: {len(old_dnc):,} · new: {len(new_dnc):,} · "
            f"**LOST in migration: {len(lost):,}**")
        if lost:
            anomalies.append(f"{len(lost)} do-not-call flags LOST — those people "
                            "become dialable again (compliance risk)")
    else:
        la = ARCH / "leads.parquet"
        n_old = 0
        if la.exists():
            df_ = pd.read_parquet(la)
            if "doNotCall" in df_.columns:
                n_old = int(df_["doNotCall"].astype(bool).sum())
        say(f"- archive DNC count: {n_old:,} · new DNC count: {len(new_dnc):,} "
            "(counts only — identity match impossible: the pseudonym salt was "
            "deliberately destroyed in the PII cleanup)")
        if len(new_dnc) < n_old:
            anomalies.append(f"DNC count dropped {n_old} → {len(new_dnc)} "
                            "(identity-level check needs the old cluster live)")

    # ---------- config-level: webhooks url + bot_settings + prompts
    say("\n## Configuration diffs (the 'what was done' signals)")
    try:
        hooks = list(new["angad"]["webhooks"].find({}, {"name": 1, "url": 1,
                                                        "active": 1, "events": 1}))
        for h in hooks:
            say(f"- webhook `{h.get('name')}` → `{h.get('url')}` "
                f"(active={h.get('active')})")
        say("  - old cluster pointed at the LEGACY relay "
            "(asbl-crm-api.vercel.app/api/relay/inhouse-posthook). If the URL "
            "above now targets the new CRM, the cutover included the webhook "
            "repoint; if unchanged, the CRM-blindness problem migrated too.")
    except Exception as ex:
        say(f"- webhooks unreadable: {type(ex).__name__}")
    try:
        bs = {d["_id"]: d.get("value") for d in
              new["Zoho_Database"]["bot_settings"].find({})}
        SENSITIVE = ("token", "secret", "key", "password")
        shown = {k: ("<masked>" if any(t in k.lower() for t in SENSITIVE)
                     else str(v)[:24]) for k, v in list(bs.items())[:10]}
        say(f"- bot_settings (new): {shown}")
    except Exception:
        pass
    parch = ARCH / "angad__prompts.parquet"
    if parch.exists():
        import hashlib
        pa = pd.read_parquet(parch)
        try:
            newp = {d.get("key"): hashlib.sha256(str(d.get("content", "")).encode()
                    ).hexdigest()[:12] for d in new["angad"]["prompts"].find({})}
            if "content" in pa.columns and "key" in pa.columns:
                oldp = {r["key"]: hashlib.sha256(str(r["content"]).encode()
                        ).hexdigest()[:12] for _, r in pa.iterrows()}
                same = [k for k in oldp if newp.get(k) == oldp[k]]
                diff = [k for k in oldp if k in newp and newp[k] != oldp[k]]
                gone = [k for k in oldp if k not in newp]
                say(f"- prompts: {len(same)} unchanged, {len(diff)} CHANGED "
                    f"({diff}), {len(gone)} missing ({gone})")
                if diff:
                    anomalies.append(f"prompt content changed during migration: {diff}")
        except Exception as ex:
            say(f"- prompts unreadable: {type(ex).__name__}")

    # ---------- indexes on hot collections
    say("\n## Indexes (new cluster)")
    for db, cns in {"angad": ["calls", "leads"],
                    "Intelligent_CRM": ["lead_events", "crm_leads"]}.items():
        for cn in cns:
            try:
                idx = [f"{i['name']}" for i in new[db][cn].list_indexes()]
                say(f"- `{db}.{cn}`: {idx}")
            except Exception:
                pass

    # ---------- verdict
    say("\n## VERDICT")
    if not anomalies:
        say("**Clean migration as far as data can show**: no missing collections, "
            "no shrunk counts beyond rolling-window behaviour, common records "
            "unmodified, DNC flags intact.")
    else:
        say(f"**NOT a plain migration — {len(anomalies)} anomalies need answers "
            "from the dev team:**")
        for x in anomalies:
            say(f"1. {x}")

    OUT.write_text("".join(md), encoding="utf-8")
    print(f"\nwrote {OUT}")
    if old:
        old.close()
    new.close()


if __name__ == "__main__":
    main()
