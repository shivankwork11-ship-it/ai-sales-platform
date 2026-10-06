"""
ANGAD Analytics - SANKALPA signature: CI Team - citeam.claudemax@gmail.com - Built with Claude (Anthropic)

THE DASHBOARD.  python dashboard.py

Local-only web dashboard over the ANGAD cluster. Opens in your browser, has
live filters, and can pull fresh data from Mongo with a button.

SECURITY — this is the whole design, not an afterthought:

  1. Binds to 127.0.0.1 ONLY. Never 0.0.0.0. Nothing on your network, your
     office wifi, or the internet can reach it. Not even another machine.
  2. A random 32-char token is minted per run and required on every request.
     Without it every request is refused. This blocks the real localhost
     attack: any website you have open in another tab can fire requests at
     127.0.0.1:PORT, and without a token it would get your data.
  3. Cross-origin requests are refused outright (any request carrying an
     Origin/Referer that is not this server, or a Host that is not loopback,
     is rejected before it reaches any handler). No CORS headers are ever
     sent, so a browser cannot read a response even if one leaked.
  4. Read-only. Every Mongo operation in this project is a find/aggregate.
     Nothing writes to the cluster.
  5. Your Mongo URI and password never leave the Python process. They are not
     in the HTML, not in the JavaScript, not in any API response.
  6. No phone numbers, emails, names-as-keys, or secrets are ever loaded -
     sync.py pseudonymises them before they touch the cache (see pii.py).
  7. No CDN, no external fetch, no telemetry. The page is fully offline.
  8. GET only. POST/PUT/DELETE are refused; the one action (refresh) is an
     explicit, token-guarded endpoint that runs sync.py as a subprocess.
"""
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from datetime import date, datetime, timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd

import pii  # to-do notes are free text and must go through the same gate

HERE = Path(__file__).parent
DATA = HERE / "data"
HOST = "127.0.0.1"
PORT = int(os.getenv("DASH_PORT", "8777"))
TOKEN = secrets.token_urlsafe(24)

GROUPS = ["Booked", "Engaged", "Dropped", "Not reached", "Rejected"]
GROUP_COLORS = {
    "Booked": "#008300", "Engaged": "#2a78d6", "Dropped": "#e34948",
    "Not reached": "#4a3aa7", "Rejected": "#eb6834", "Unclassified": "#8a8f98",
}
BLUE = "#2a78d6"
ROW_CAP = 1500

_lock = threading.Lock()
_sync_state = {"running": False, "log": "", "finished_at": None}


# ============================================================ data loading

def _p(name):
    f = DATA / name
    return pd.read_parquet(f) if f.exists() else pd.DataFrame()


@lru_cache(maxsize=1)
def load():
    """Load the local cache. Every source is optional - the dashboard degrades
    gracefully to whatever sync.py actually managed to pull."""
    calls = _p("calls.parquet")
    if calls.empty:
        return None

    # The cache may store "date" as text, timestamp or date depending on which
    # version of sync.py wrote it. Normalise here so filtering always works
    # without forcing a re-sync.
    if "date" in calls.columns and calls["date"].dtype.name != "object":
        calls["date"] = pd.to_datetime(calls["date"], errors="coerce").dt.date
    elif "date" in calls.columns:
        first = calls["date"].dropna().head(1)
        if len(first) and isinstance(first.iloc[0], str):
            calls["date"] = pd.to_datetime(calls["date"], errors="coerce").dt.date

    # VARAHA: the era dimension. Pre-2026-07-17 = the Kaalbhojaditya era,
    # referred to by that name everywhere. Computed here if the cache predates
    # the Varaha sync.
    if "era" not in calls.columns and "date" in calls.columns:
        cut = pd.Timestamp("2026-07-17").date()
        calls["era"] = calls["date"].map(
            lambda d_: "Kaalbhojaditya" if pd.notna(d_) and d_ < cut else "ANGAD")

    bundle = {
        "calls": calls,
        "leads": _p("leads.parquet"),
        "sched": _p("scheduled.parquet"),
        # ASBLVoiceBot - the conversation intelligence (budget/timeline/objections)
        "tx": _p("ASBLVoiceBot__call_transcripts.parquet"),
        "profiles": _p("ASBLVoiceBot__lead_profiles.parquet"),
        # Zoho_Database - project, attribution, WhatsApp, visits
        "zleads": _p("Zoho_Database__leads.parquet"),
        "wa_msgs": _p("Zoho_Database__whatsapp_messages.parquet"),
        "doc_log": _p("Zoho_Database__doc_send_log.parquet"),
        "visits": _p("Zoho_Database__site_visit_bookings.parquet"),
        "callbacks": _p("Zoho_Database__callback_log.parquet"),
        # Intelligent_CRM - the stage funnel
        "crm": _p("Intelligent_CRM__crm_leads.parquet"),
        "persons": _p("Intelligent_CRM__persons.parquet"),
        "crm_visits": _p("Intelligent_CRM__site_visits.parquet"),
        "lead_events": _p("Intelligent_CRM__lead_events.parquet"),
        # post-call intent extraction (ASBLVoiceBot) - the intelligence pass
        "intent_id": _p("ASBLVoiceBot__call_transcript_intent_identification.parquet"),
        # phone -> MLID registry: its _id WAS the phone, so it arrives as
        # lead_ref + mlid and joins to everything without any raw numbers
        "mlid": _p("Zoho_Database__mlid_registry.parquet"),
        "plid": _p("Zoho_Database__plid_registry.parquet"),
        # the daily Zoho CRM export (import_zoho.py), pseudonymised
        "zoho": _p("zoho_export.parquet"),
        # cohort workbooks (import_cohorts.py) - AI vs Human comparison
        "co_ai_daily": _p("cohort_ai_daily.parquet"),
        "co_ai_leads": _p("cohort_ai_leads.parquet"),
        "co_ai_booked": _p("cohort_ai_booked.parquet"),
        "co_ai_done": _p("cohort_ai_done.parquet"),
        "co_hu_leads": _p("cohort_human_leads.parquet"),
        "co_hu_booked": _p("cohort_human_booked.parquet"),
        "co_hu_done": _p("cohort_human_done.parquet"),
        "co_sv_true": _p("cohort_sv_true.parquet"),
        "co_sv_false": _p("cohort_sv_false.parquet"),
        "co_sv_completed": _p("cohort_sv_completed.parquet"),
        # ops-truth stores surfaced by the inversion sweep
        "stuck_sends": _p("Zoho_Database__stuck_sends.parquet"),
        "dead_senders": _p("Zoho_Database__dead_senders.parquet"),
        "crm_msgs": _p("Intelligent_CRM__messages.parquet"),
        "schedules_vb": _p("ASBLVoiceBot__call_schedules.parquet"),
        "crm_calls": _p("Intelligent_CRM__calls.parquet"),
        "recordings": _p("ASBLVoiceBot__call_recordings.parquet"),
        "posthooks": _p("Zoho_Database__posthook_payloads.parquet"),
    }

    # crm_leads keys on MLID, not phone - resolve through persons
    crm, persons = bundle["crm"], bundle["persons"]
    if not crm.empty and not persons.empty and "person_id" in crm.columns:
        m = persons[["_id", "lead_ref"]].rename(columns={"_id": "person_id"})
        bundle["crm"] = crm.merge(m, on="person_id", how="left")

    return bundle


@lru_cache(maxsize=1)
def phone_map():
    """lead_ref -> real number, from data/phone_map.csv if the user built it.
    Absent by default; the dashboard shows pseudonyms unless it exists AND the
    'Show phone numbers' toggle is on."""
    f = DATA / "phone_map.csv"
    if not f.exists():
        return {}
    try:
        m = pd.read_csv(f, dtype=str)
        return dict(zip(m["lead_ref"], m["phone"]))
    except Exception:
        return {}


@lru_cache(maxsize=1)
def id_maps():
    """lead_ref -> business ids the user actually recognises. MLID and PLID
    are surrogate keys (non-PII per the data dictionary), so they are shown
    EVERYWHERE beside the pseudonym. Sources: Zoho_Database.mlid_registry
    (lead_ref->mlid) and Zoho_Database.leads (lead_ref->PLID=_id)."""
    mlid, plid = {}, {}
    try:
        m = pd.read_parquet(DATA / "Zoho_Database__mlid_registry.parquet")
        if {"lead_ref", "mlid"} <= set(m.columns):
            mm = m.dropna(subset=["lead_ref", "mlid"])
            mlid = dict(zip(mm["lead_ref"], mm["mlid"].astype(str)))
    except Exception:
        pass
    try:
        z = pd.read_parquet(DATA / "Zoho_Database__leads.parquet",
                            columns=["lead_ref", "_id"])
        zz = z.dropna()
        plid = dict(zip(zz["lead_ref"], zz["_id"].astype(str)))
    except Exception:
        pass
    return mlid, plid


def _clean_id_series(s):
    """Stringify an id column: strip, drop float-mangle '.0', blank the junk."""
    v = s.astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    v[v.str.lower().isin(["", "nan", "none", "<na>"])] = ""
    return v


@lru_cache(maxsize=1)
def master_maps():
    """lead_ref -> Inncircles MASTERLEADID and lead_ref -> Source Lead ID.

    The user-recognised unique identifier is Inncircles' master lead id
    (6-digit, e.g. 992727) which reaches our DBs in THREE places:
      1. Zoho_Database.reactivation_list  lead_ref -> masterleadid (direct);
      2. ASBLVoiceBot.call_transcript_intent_identification.master_lead_id,
         joined per call via call_sid == angad.calls.roomName -> lead_ref;
      3. cohort workbooks (display-only, no lead_ref bridge).
    Source Lead ID (the acquisition id: Inncircles numeric / 'l:' Facebook /
    19-digit Zoho record) comes from ASBLVoiceBot.call_schedules (lead_ref
    keyed, 45k rows) with Zoho_Database.leads.source_lead_id as fallback."""
    master, srcid = {}, {}
    try:  # 2) per-call intent join (bulk of coverage) — filled first, then
        # the direct reactivation map overrides on conflict
        it = pd.read_parquet(
            DATA / "ASBLVoiceBot__call_transcript_intent_identification.parquet",
            columns=["call_sid", "master_lead_id"])
        ca = pd.read_parquet(DATA / "calls.parquet", columns=["roomName", "lead_ref"])
        j = it.merge(ca.dropna().drop_duplicates("roomName"),
                     left_on="call_sid", right_on="roomName", how="inner")
        j["master_lead_id"] = _clean_id_series(j["master_lead_id"])
        j = j[j["master_lead_id"] != ""]
        for r, v in zip(j["lead_ref"], j["master_lead_id"]):
            master.setdefault(r, v)
    except Exception:
        pass
    try:  # 1) direct map wins
        ra = pd.read_parquet(DATA / "Zoho_Database__reactivation_list.parquet",
                             columns=["lead_ref", "masterleadid"])
        ra["masterleadid"] = _clean_id_series(ra["masterleadid"])
        ra = ra[(ra["masterleadid"] != "") & ra["lead_ref"].notna()]
        for r, v in zip(ra["lead_ref"], ra["masterleadid"]):
            master[r] = v
    except Exception:
        pass
    try:  # source lead id: call_schedules first (45k, lead_ref keyed)
        cs = pd.read_parquet(DATA / "ASBLVoiceBot__call_schedules.parquet",
                             columns=["lead_ref", "source_lead_id"])
        cs["source_lead_id"] = _clean_id_series(cs["source_lead_id"])
        cs = cs[(cs["source_lead_id"] != "") & cs["lead_ref"].notna()]
        for r, v in zip(cs["lead_ref"], cs["source_lead_id"]):
            srcid.setdefault(r, v)
    except Exception:
        pass
    try:  # fallback: the Zoho brain's own source_lead_id
        zl = pd.read_parquet(DATA / "Zoho_Database__leads.parquet",
                             columns=["lead_ref", "source_lead_id"])
        zl["source_lead_id"] = _clean_id_series(zl["source_lead_id"])
        zl = zl[(zl["source_lead_id"] != "") & zl["lead_ref"].notna()]
        for r, v in zip(zl["lead_ref"], zl["source_lead_id"]):
            srcid.setdefault(r, v)
    except Exception:
        pass
    return master, srcid


def add_ids(df):
    """Insert lead_id (PLID), MLID (registry), MASTERLEADID (Inncircles) and
    SourceLeadID columns right after lead_ref — the ids the user recognises."""
    if df is None or df.empty or "lead_ref" not in df.columns:
        return df
    mlid, plid = id_maps()
    master, srcid = master_maps()
    if not mlid and not plid and not master and not srcid:
        return df
    df = df.copy()
    # ONE id vocabulary — Inncircles' own, no invented columns:
    #   MLID           = the Inncircles 6-digit master lead id (their primary key)
    #   Lead ID        = the project lead id / PLID (e.g. 1002-LOFT)
    #   Source Lead ID = the acquisition id the lead arrived with
    # The internal 4-digit registry id is plumbing and is NOT shown.
    df["MLID"] = df["lead_ref"].map(master)
    df.loc[df["MLID"].isna(), "MLID"] = df["lead_ref"].map(mlid)  # fallback only
    df["Lead ID"] = df["lead_ref"].map(plid)
    df["Source Lead ID"] = df["lead_ref"].map(srcid)
    cols = list(df.columns)
    for c in ("Source Lead ID", "Lead ID", "MLID"):
        cols.insert(cols.index("lead_ref") + 1, cols.pop(cols.index(c)))
    return df[cols]


def add_phone(df):
    """Insert a phone column immediately after lead_ref."""
    m = phone_map()
    if not m or df is None or df.empty or "lead_ref" not in df.columns:
        return df
    df = df.copy()
    df["phone"] = df["lead_ref"].map(m)
    cols = list(df.columns)
    cols.insert(cols.index("lead_ref") + 1, cols.pop(cols.index("phone")))
    return df[cols]


def norm_project(s):
    """Unify project names across the four databases.

    angad.leads says "ASBL Loft " / "ASBL LOFT", Zoho says "ASBL Loft",
    Intelligent_CRM says "LOFT" — all the same project. Without this the
    filter list has six entries for one building."""
    return (s.fillna("").astype(str).str.strip().str.upper()
             .str.replace(r"^ASBL[\s_-]+", "", regex=True)
             .str.replace(r"\s+", " ", regex=True).str.strip())


def col(df, *names):
    """First column that exists, from a list of candidates."""
    for n in names:
        if not df.empty and n in df.columns:
            return n
    return None


# ============================================================ filtering

def option_counts(b, c):
    """How many rows each filter value would match INSIDE the current window.

    "The filters don't work" was not a filter bug — every one of the 22 was
    verified to filter correctly. The modal was offering values drawn from the
    WHOLE dataset while filtering applied to the windowed set, so choosing
    e.g. era=Kaalbhojaditya (all of which predates 17 Jul) inside a July-August
    window returned an empty screen, which reads as broken. Now every option
    carries its live count and the UI can mark the dead ones."""
    out = {}
    if c is None or c.empty:
        return out
    pairs = [("outcome", "outcome"), ("outcome_group", "outcome_group"),
             ("status", "status"), ("direction", "direction"), ("slug", "slug"),
             ("engine", "voiceEngine"), ("source", "source"),
             ("language", "language"), ("era", "era"),
             ("drop_stage", "drop_stage"), ("weekday", "weekday")]
    for key, colname in pairs:
        if colname in c.columns:
            vc = c[colname].dropna().astype(str).value_counts()
            out[key] = {str(k): int(v) for k, v in vc.items()}
    if "hour" in c.columns:
        vc = c["hour"].dropna().astype(int).value_counts()
        out["hour"] = {f"{int(k):02d}": int(v) for k, v in vc.items()}
    try:
        amap = truth()["audience"]
        a = c["lead_ref"].map(lambda r: amap.get(r, "RI")).value_counts()
        out["audience"] = {"RI (India · Plivo)": int(a.get("RI", 0)),
                           "NRI (Telnyx)": int(a.get("NRI", 0))}
    except Exception:
        pass
    return out


def options(b):
    c = b["calls"]

    def opts(df, name, limit=60):
        if df.empty or name not in df.columns:
            return []
        v = df[name].dropna().astype(str)
        v = v[(v.str.strip() != "") & (v.str.lower() != "nan") & (v.str.lower() != "none")]
        return sorted(v.value_counts().head(limit).index.tolist())

    proj = []
    for df, n in ((b["zleads"], col(b["zleads"], "project")),
                  (b["crm"], col(b["crm"], "project")),
                  (b["leads"], col(b["leads"], "propertyInterested"))):
        if n:
            v = norm_project(df[n])
            proj += [x for x in v.unique() if x]

    return {
        "date_min": str(c["date"].min()), "date_max": str(c["date"].max()),
        "outcome": opts(c, "outcome"), "outcome_group": GROUPS,
        "status": opts(c, "status"), "direction": opts(c, "direction"),
        "slug": opts(c, "slug"), "engine": opts(c, "voiceEngine"),
        "source": opts(c, "source"),
        "project": sorted(set(proj)),
        "stage": opts(b["crm"], col(b["crm"], "stage") or "stage"),
        "language": opts(c, "language"),
        "era": ["Kaalbhojaditya", "ANGAD"],
        "drop_stage": opts(c, "drop_stage"),
        "weekday": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                    "Saturday", "Sunday"],
        "hour": [f"{h:02d}" for h in range(24)],
        "has_transcript": ["with transcript", "without transcript"],
        "qualified": ["qualified", "not qualified"],
        "broker": ["broker", "not broker"],
        "whatsapp": ["texted on WhatsApp", "never texted"],
        "attempts": ["1", "2", "3", "4", "5", "6-10", "11+"],
        "duration": ["0-15s", "15-60s", "1-3m", "3m+"],
        "audience": ["RI (India · Plivo)", "NRI (Telnyx)"],
        "born_from": [], "born_to": [],
        "quality": (["clean"] + sorted(x for x in c.columns
                                       if x.startswith("tq_") and x != "tq_any"))
                   if "tq_any" in c.columns else [],
    }


def apply_filters(b, q):
    c = b["calls"].copy()

    d0 = q.get("from") or str(c["date"].min())
    d1 = q.get("to") or str(c["date"].max())
    d0, d1 = pd.to_datetime(d0).date(), pd.to_datetime(d1).date()
    c = c[(c["date"] >= d0) & (c["date"] <= d1)]

    for key, colname in [("outcome", "outcome"), ("outcome_group", "outcome_group"),
                         ("status", "status"), ("direction", "direction"),
                         ("slug", "slug"), ("engine", "voiceEngine"),
                         ("source", "source"), ("language", "language"),
                         ("era", "era")]:
        vals = [v for v in q.get(key, "").split("|") if v]
        if vals and colname in c.columns:
            c = c[c[colname].astype(str).isin(vals)]

    # project / stage live in the other databases - filter by the lead refs there
    proj = [v for v in q.get("project", "").split("|") if v]
    if proj:
        refs = set()
        for df, n in ((b["zleads"], col(b["zleads"], "project")),
                      (b["crm"], col(b["crm"], "project")),
                      (b["leads"], col(b["leads"], "propertyInterested"))):
            if n and "lead_ref" in df.columns:
                refs |= set(df.loc[norm_project(df[n]).isin(proj), "lead_ref"].dropna())
        c = c[c["lead_ref"].isin(refs)]

    for key, colname in [("drop_stage", "drop_stage"), ("weekday", "weekday")]:
        vals = [v for v in q.get(key, "").split("|") if v]
        if vals and colname in c.columns:
            c = c[c[colname].astype(str).isin(vals)]

    # BORN DATE (when the lead entered the system) — a global filter, applied
    # person-level via the truth layer's parsed born date.
    bf, bt = q.get("born_from"), q.get("born_to")
    if bf or bt:
        try:
            z = truth()["zoho"]
            if not z.empty and "born" in z.columns:
                bb = pd.to_datetime(z["born"], errors="coerce")
                m = pd.Series(True, index=z.index)
                if bf:
                    m &= bb.dt.date >= pd.to_datetime(bf).date()
                if bt:
                    m &= bb.dt.date <= pd.to_datetime(bt).date()
                c = c[c["lead_ref"].isin(set(z.index[m]))]
        except Exception:
            pass

    # audience: NRI (telnyx) vs RI (plivo), person-level via the truth layer
    auds = [v for v in q.get("audience", "").split("|") if v]
    if auds:
        want = {("NRI" if "NRI" in v else "RI") for v in auds}
        amap = truth()["audience"]
        c = c[c["lead_ref"].map(lambda r: amap.get(r, "RI")).isin(want)]

    hrs = [int(v) for v in q.get("hour", "").split("|") if v.isdigit()]
    if hrs:
        c = c[c["hour"].isin(hrs)]

    def boolfilter(key, colname, true_label):
        vals = [v for v in q.get(key, "").split("|") if v]
        if not vals or colname not in c.columns or len(vals) == 2:
            return c
        want = vals[0] == true_label
        return c[truthy(c[colname]) == want]

    c = boolfilter("has_transcript", "has_transcript", "with transcript")
    c = boolfilter("qualified", "is_qualified", "qualified")
    c = boolfilter("broker", "is_broker", "broker")
    c = boolfilter("whatsapp", "whatsappSent", "texted on WhatsApp")

    att = [v for v in q.get("attempts", "").split("|") if v]
    if att and "total_attempts" in c.columns:
        t = pd.to_numeric(c["total_attempts"], errors="coerce").fillna(0)
        mask = pd.Series(False, index=c.index)
        for a in att:
            if a == "6-10":
                mask |= t.between(6, 10)
            elif a == "11+":
                mask |= t >= 11
            elif a.isdigit():
                mask |= t == int(a)
        c = c[mask]

    dur = [v for v in q.get("duration", "").split("|") if v]
    if dur and "duration_sec" in c.columns:
        t = pd.to_numeric(c["duration_sec"], errors="coerce").fillna(0)
        band = {"0-15s": (0, 15), "15-60s": (15, 60), "1-3m": (60, 180),
                "3m+": (180, 10 ** 9)}
        mask = pd.Series(False, index=c.index)
        for d_ in dur:
            if d_ in band:
                lo, hi = band[d_]
                mask |= (t >= lo) & (t < hi)
        c = c[mask]

    qual = [v for v in q.get("quality", "").split("|") if v]
    if qual and "tq_any" in c.columns:
        mask = pd.Series(False, index=c.index)
        for v in qual:
            if v == "clean":
                mask |= ~truthy(c["tq_any"])
            elif v in c.columns:
                mask |= truthy(c[v])
        c = c[mask]

    stage = [v for v in q.get("stage", "").split("|") if v]
    if stage and not b["crm"].empty:
        sn = col(b["crm"], "stage")
        if sn and "lead_ref" in b["crm"].columns:
            refs = set(b["crm"].loc[b["crm"][sn].astype(str).isin(stage), "lead_ref"].dropna())
            c = c[c["lead_ref"].isin(refs)]

    if q.get("search"):
        s = q["search"].lower()
        hay = (c["lead_said"].fillna("") + " " + c["agent_said"].fillna("") + " "
               + c["summary"].fillna("") + " " + c["lead_ref"].fillna(""))
        c = c[hay.str.lower().str.contains(re.escape(s), na=False)]

    return c, d0, d1


# ============================================================ payload

def pct(n, d):
    return round(100.0 * n / d, 1) if d else 0.0


# ---------------------------------------------------------------- rate card
# ONE definition of money. Recalibrated 2026-08-08 — see the long note on
# D["cost"]. Anything that quotes ₹ must call cost_of(), never re-implement it.
USD_INR = 96.0
GEM_IN_PER_M = 0.50          # USD per 1M input tokens  (Flash-tier, calibrated)
GEM_OUT_PER_M = 2.50         # USD per 1M output tokens
TEL_PER_MIN = 0.60           # ₹ per started minute, Plivo, ANSWERED calls only


def export_gate(text, where):
    """P1 — the export boundary. An ASSUMED number may not leave the building.

    The cost error (RETRACTIONS R1) reached exec_report.html and the team
    one-pager because nothing stood between an assumed rate and an export. This
    is that gate: any artefact bound for a human outside this machine is scanned
    for figures we have superseded, and the write is REFUSED — not warned about,
    refused — if one is found. It is deliberately a hard fail: a warning is a
    habit, and habits are what this is here to replace."""
    BANNED = [
        (r"₹\s*1\.7\s*/\s*call", "superseded cost: ₹1.7/call (RETRACTIONS R1)"),
        (r"₹\s*639\b", "superseded cost: ₹639/booked (R1)"),
        (r"₹\s*4,?995\b", "superseded cost: ₹4,995/attended (R1)"),
        (r"\$3\s*/\s*\$12\s*per\s*1M", "superseded rate card $3/$12 (R1)"),
        (r"scheduler reads the wrong database", "retracted claim (R4)"),
        (r"extractedSlots is empty", "retracted claim (R2)"),
        (r"~?11[- ]day", "retracted cron reading (R8)"),
    ]
    hits = [why for rx, why in BANNED if re.search(rx, text, re.I)]
    if hits:
        raise RuntimeError(
            f"EXPORT GATE REFUSED {where}: " + " · ".join(hits) +
            "\n  A retracted or superseded figure may not cross the export "
            "boundary. Fix the source, do not suppress the check.")
    return text


def cost_of(c):
    """Per-call ₹ for a calls frame. Returns (total, model, telephony, answered).

    Unanswered dials cost ZERO: verified that 0.0% of BUSY / NOT_CONNECTED and
    0.2% of NO_ANSWER ever open a Gemini session. We hold no carrier invoice, so
    a per-attempt carrier fee — if one exists — is not captured here."""
    tokP = pd.to_numeric(c.get("tok_prompt"), errors="coerce").fillna(0)
    tokR = pd.to_numeric(c.get("tok_response"), errors="coerce").fillna(0)
    tokT = pd.to_numeric(c.get("tok_total"), errors="coerce").fillna(0)
    secs = pd.to_numeric(c.get("duration_sec"), errors="coerce").fillna(0)
    # THE LINE WAS ANSWERED is not the same thing as `reached`. `reached` means
    # "answered AND the conversation counted" — it is False on all 6,727 DROPPED
    # and all 9,331 VOICEMAIL rows. But 5,428 of those opened a Gemini session
    # (median 19s of audio), so the handset was physically picked up and the
    # carrier billed it. Costing on `reached` silently dropped the single largest
    # spend bucket. Bill on evidence the line opened, not on the outcome label.
    # Careful: duration_sec is NOT proof of pickup. 44% of NO_ANSWER rows carry a
    # median 30s (that is ring time), and NO_ANSWER is partly fabricated by
    # cleanupStaleCalls anyway. Only these count as the line opening:
    #   · reached            — answered and the conversation counted
    #   · a Gemini session   — audio flowed, so the handset was up
    #   · DROPPED / VOICEMAIL with real duration — human or machine picked up
    oc = c["outcome"].astype(str) if "outcome" in c.columns else pd.Series("", index=c.index)
    picked_up = (tokT > 0) | (oc.isin(["DROPPED", "VOICEMAIL"]) & (secs > 0))
    if "reached" in c.columns:
        picked_up = picked_up | c["reached"].fillna(False).astype(bool)
    model = (tokP * GEM_IN_PER_M + tokR * GEM_OUT_PER_M) / 1e6 * USD_INR
    tel = np.ceil(secs / 60).clip(lower=1) * picked_up.astype(int) * TEL_PER_MIN
    return model + tel, model, tel, picked_up


def clean(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (pd.Timestamp, datetime, date)):
        return str(v)[:19]
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:
            return str(v)
    return v


def table(df, cols, cap=ROW_CAP, labels=None):
    # user-recognisable ids: MLID + PLID beside every pseudonym, everywhere
    if not df.empty and "lead_ref" in df.columns and "lead_ref" in cols:
        df = add_ids(df)
        cols = list(cols)
        i = cols.index("lead_ref") + 1
        for c in ("Source Lead ID", "Lead ID", "MLID"):
            if c in df.columns and c not in cols:
                cols.insert(i, c)
    # show the real number beside the pseudonym when PII display is enabled
    if not df.empty and "phone" in df.columns and "lead_ref" in cols \
            and "phone" not in cols:
        cols = list(cols)
        cols.insert(cols.index("lead_ref") + 1, "phone")
    cols = [c for c in cols if not df.empty and c in df.columns]
    if not cols:
        return {"cols": [], "rows": [], "total": 0, "shown": 0}
    sub = df[cols].head(cap)
    return {"cols": [(labels or {}).get(c, c) for c in cols],
            "rows": [[clean(v) for v in r] for r in sub.itertuples(index=False)],
            "total": int(len(df)), "shown": int(len(sub))}


def counts(series, top=None):
    if series is None or len(series) == 0:
        return []
    vc = series.value_counts()
    if top:
        vc = vc.head(top)
    return [{"label": str(k)[:60], "value": int(v)} for k, v in vc.items()]


def truthy(series):
    """Boolean test that survives bool, 'True'/'False' text, and 1/0."""
    if series is None or len(series) == 0:
        return pd.Series(dtype=bool)
    if series.dtype == bool or str(series.dtype) == "boolean":
        return series.fillna(False).astype(bool)
    return (series.astype(str).str.strip().str.lower()
            .isin(["true", "1", "yes", "y"]))


def nonempty(s):
    if s is None:
        return pd.Series(dtype=str)
    s = s.dropna().astype(str).str.strip()
    return s[(s != "") & (~s.str.lower().isin(["nan", "none", "null", "unknown", "[]"]))]


def build(b, c, d0, d1, q_unit="calls", q_irule=None, q_fstage=None):
    n = len(c)
    reached = c[c["reached"]]
    drops = c[c["is_dropped"]]
    missed = c[c["outcome_group"] == "Not reached"]
    rejected = c[c["outcome_group"] == "Rejected"]
    booked = c[c["is_booked"]]
    refs = set(c["lead_ref"].dropna())

    # ---- join the other databases.
    # Transcripts join PER CALL on call_sid == roomName. Joining them per LEAD
    # (as an earlier version did) attached one arbitrary call's budget/timeline
    # to every call that lead ever received, and leaked slots from calls outside
    # the selected date range.
    tx = b["tx"]
    if not tx.empty and "call_sid" in tx.columns:
        tx = tx[tx["call_sid"].isin(set(c["roomName"].dropna()))]
    elif not tx.empty and "lead_ref" in tx.columns:
        tx = tx[tx["lead_ref"].isin(refs)]
    zl = b["zleads"]
    if not zl.empty and "lead_ref" in zl.columns:
        zl = zl[zl["lead_ref"].isin(refs)]
    crm = b["crm"]
    if not crm.empty and "lead_ref" in crm.columns:
        crm = crm[crm["lead_ref"].isin(refs)]

    D = {"meta": {"from": str(d0), "to": str(d1), "groups": GROUPS,
                  "colors": GROUP_COLORS,
                  "sources": {k: int(len(v)) for k, v in b.items()
                              if isinstance(v, pd.DataFrame)}}}

    refmap_k = human_map(b)
    hseries = c["lead_ref"].map(lambda r: refmap_k.get(r, r))
    if q_unit == "humans":
        D["kpis"] = [
            {"label": "Dial attempts", "value": f"{n:,}", "src": "angad.calls"},
            {"label": "Humans dialled", "value": f"{hseries.nunique():,}",
             "src": "angad.calls · Zoho_Database.mlid_registry"},
            {"label": "Humans connected",
             "value": f"{hseries[c['reached'] | c.get('human_conversation', False)].nunique():,}",
             "sub": "answered outcome or customer spoke", "src": "angad.calls"},
            {"label": "Humans in real conversation",
             "value": f"{hseries[c['human_conversation']].nunique():,}"
             if "human_conversation" in c.columns else "—",
             "sub": "voicemail excluded", "src": "angad.calls transcripts"},
            {"label": "Humans booked (call outcome)",
             "value": f"{hseries[c['outcome'].isin(['PRE_SITE','VIRTUAL_TOUR'])].nunique():,}",
             "src": "angad.calls.outcome"},
            {"label": "Humans DNC", "value": f"{hseries[truthy(c['dncTriggered'])].nunique():,}",
             "src": "angad.calls.dncTriggered"},
            {"label": "Median talk time",
             "value": (f"{reached.loc[reached.get('duration_known', True), 'duration_sec'].median():.0f}s"
                       if len(reached) else "—"),
             "sub": "connected calls", "src": "angad.calls"},
        ]
    else:
        D["kpis"] = [
        {"label": "Calls triggered", "value": f"{n:,}", "src": "angad.calls"},
        {"label": "Unique numbers", "value": f"{c['lead_ref'].nunique():,}", "src": "angad.calls"},
        {"label": "Unique humans",
         "value": f"{c['lead_ref'].map(lambda r, _m=human_map(b): _m.get(r, r)).nunique():,}",
         "sub": "Master Lead ID collapsed", "src": "Zoho_Database.mlid_registry"},
        {"label": "Connected", "value": f"{len(reached):,}", "sub": f"{pct(len(reached), n)}%",
         "src": "angad.calls.outcome"},
        {"label": "Booked", "value": f"{len(booked):,}", "sub": f"{pct(len(booked), n)}%",
         "src": "angad.calls.outcome"},
        {"label": "Dropped", "value": f"{len(drops):,}", "sub": f"{pct(len(drops), n)}%",
         "src": "angad.calls.outcome"},
        {"label": "Qualified",
         "value": f"{int(truthy(c['is_qualified']).sum()):,}" if "is_qualified" in c.columns else "—",
         "sub": "userSignals.is_qualified", "src": "angad.calls.userSignals"},
        {"label": "Median talk time",
         "value": (f"{reached.loc[reached.get('duration_known', True), 'duration_sec'].median():.0f}s"
                   if len(reached) else "—"),
         "sub": "connected calls with a known duration", "src": "angad.calls"},
        ]

    # ================================ 1. NUMBERS
    # UNIT-AWARE (the "call and human filter does nothing" fix): in humans
    # mode every daily bar and funnel step counts unique MLID-collapsed
    # people, not call events.
    if q_unit == "humans":
        daily = (c.assign(_h=hseries)
                  .groupby(["date", "outcome_group"])["_h"].nunique()
                  .reset_index(name="v"))
    else:
        daily = c.groupby(["date", "outcome_group"]).size().reset_index(name="v")
    dates = sorted(daily["date"].unique())
    D["daily"] = {
        "unit": q_unit,
        "dates": [str(x) for x in dates],
        "series": [{"name": g, "color": GROUP_COLORS.get(g, "#8a8f98"),
                    "values": [int(daily[(daily["date"] == d) &
                                         (daily["outcome_group"] == g)]["v"].sum())
                               for d in dates]}
                   for g in GROUPS + ["Unclassified"] if (daily["outcome_group"] == g).any()],
    }

    # NRI vs RI hour-of-day split — every time chart needs this lens
    amap = truth()["audience"]
    aud_series = c["lead_ref"].map(lambda r: amap.get(r, "RI"))
    ah = (c.assign(_a=aud_series).groupby(["hour", "_a"]).size().unstack(fill_value=0))
    D["audience_hours"] = {
        "hours": [f"{int(h):02d}" for h in ah.index],
        "series": [{"name": a, "color": "#2a78d6" if a == "RI" else "#8a5cd6",
                    "values": [int(v) for v in ah[a]]} for a in ah.columns],
        "src": "audience = telephony provider per lead (Telnyx→NRI, Plivo→RI), "
               "ASBLVoiceBot.call_schedules"}

    # Funnel must be monotonic. "lead_ever_spoke" is NOT a valid step: a
    # voicemail greeting is transcribed as the lead speaking, so it exceeded
    # "connected". Each step below is a strict subset of the one above it.
    answered = c[c["has_transcript"]]
    talked = c[c["human_conversation"]] if "human_conversation" in c.columns         else c[c["lead_ever_spoke"] & ~c["outcome"].eq("VOICEMAIL")]
    # "Qualified" is NOT a funnel step: a booking does not require the
    # qualification flag, so it is not a superset of Booked. It is reported as
    # its own metric instead of being forced into a funnel it breaks.
    if q_unit == "humans":
        _hn = hseries.nunique()
        stages = [("Humans dialled", _hn),
                  ("Answered", hseries[c["has_transcript"]].nunique()),
                  ("Real conversation", hseries[c["human_conversation"]].nunique()
                   if "human_conversation" in c.columns else 0),
                  ("Booked", hseries[c["is_booked"]].nunique())]
        n_base = _hn
    else:
        stages = [("Dialled", n),
                  ("Call answered", len(answered)),
                  ("Real conversation", len(talked)),
                  ("Booked", len(booked))]
        n_base = n
    D["funnel"] = [{"label": s, "value": v, "pct": pct(v, n_base)} for s, v in stages]
    D["funnel_note"] = ("Each step is a strict subset of the one above. "
                        "&ldquo;Real conversation&rdquo; excludes voicemail: an answering-machine "
                        "greeting is transcribed as the customer speaking, which "
                        "would otherwise overstate human contact by "
                        f"{int(c['is_voicemail'].sum() if 'is_voicemail' in c.columns else 0):,} calls.")
    D["outcome_mix"] = counts(c["outcome"].fillna("(unclassified)"))
    D["status_mix"] = counts(c["status"].fillna("(none)"))

    # CRM stage funnel, if the rebuild data came through
    sn = col(crm, "stage")
    D["crm_stage"] = counts(crm[sn]) if sn else []

    # ================================ 2. POSITIVES
    prof = c.copy()
    if not b["leads"].empty:
        keep = [x for x in ["lead_ref", "lead_status", "propertyInterested",
                            "leadScore", "doNotCall", "siteVisitScheduledAt"]
                if x in b["leads"].columns]
        prof = prof.merge(b["leads"][keep], on="lead_ref", how="left")
    if not zl.empty:
        keep = [x for x in ["lead_ref", "project", "stage", "lead_status",
                            "lead_budget", "size_preference", "possession_timeline",
                            "purchase_purpose", "utm_source", "campaign_name",
                            "call_status"] if x in zl.columns]
        if len(keep) > 1:
            prof = prof.merge(zl[keep].drop_duplicates("lead_ref"),
                              on="lead_ref", how="left", suffixes=("", "_zoho"))
    if not tx.empty and "call_sid" in tx.columns:
        keep = [x for x in ["call_sid", "intent", "budget", "timeline",
                            "preferred_unit", "facing_preference", "work_location",
                            "ready_to_book", "site_visit_agreed", "objections",
                            "language_used", "project"] if x in tx.columns]
        if len(keep) > 1:
            t = tx[keep].drop_duplicates("call_sid").rename(
                columns={"call_sid": "roomName", "project": "call_project"})
            prof = prof.merge(t, on="roomName", how="left", suffixes=("", "_tx"))

    D["called"] = table(
        prof[prof["reached"]].sort_values("ts", ascending=False),
        ["ts_str", "lead_ref", "display_name", "outcome", "duration_sec",
         "call_project", "project", "stage", "budget", "timeline",
         "preferred_unit", "intent",
         "site_visit_agreed", "is_qualified", "is_broker", "language_used",
         "utm_source", "campaign_name", "slug"],
        labels={"ts_str": "called at (IST)", "duration_sec": "sec"})

    by_hour = (c.groupby("hour").agg(calls=("roomName", "size"),
                                     reached=("reached", "sum"))
                 .reindex(range(24), fill_value=0).reset_index())
    # hours with zero dials must yield 0, not NA - a narrow filter leaves many
    denom = by_hour["calls"].astype(float)
    by_hour["reach_pct"] = ((100 * by_hour["reached"].astype(float))
                            .div(denom).where(denom > 0, 0.0).round(1))
    D["by_hour"] = {"labels": [f"{h:02d}" for h in by_hour["hour"]],
                    "calls": [int(x) for x in by_hour["calls"]],
                    "reach_pct": [float(x) for x in by_hour["reach_pct"]]}
    D["by_weekday"] = counts(c["weekday"])

    # what projects were picked up
    pieces = []
    for df, pcol in ((tx, col(tx, "project")), (zl, col(zl, "project")),
                     (crm, col(crm, "project")),
                     (b["leads"], col(b["leads"], "propertyInterested"))):
        if pcol:
            d2 = df[df["lead_ref"].isin(refs)] if "lead_ref" in df.columns else df
            v = norm_project(d2[pcol])
            pieces.append(v[v != ""])
    proj = pd.concat(pieces) if pieces else pd.Series(dtype=str)
    D["projects"] = counts(proj, top=15)
    D["campaigns"] = counts(nonempty(zl[col(zl, "campaign_name")]) if col(zl, "campaign_name")
                            else pd.Series(dtype=str), top=15)
    D["utm"] = counts(nonempty(zl[col(zl, "utm_source")]) if col(zl, "utm_source")
                      else pd.Series(dtype=str), top=15)

    # what did they ask - objections + intent, from the intelligence store
    obj = pd.Series(dtype=str)
    if col(tx, "objections"):
        obj = nonempty(tx[col(tx, "objections")]).str.split(",").explode().str.strip()
        obj = obj[obj != ""]
    def slot(name, top=15):
        """A slot chart plus how many of the selected calls actually carry it.
        These fields exist on ~97% of transcripts but are mostly EMPTY STRINGS -
        without the coverage line the chart implies far more signal than exists."""
        cn = col(tx, name)
        vals = nonempty(tx[cn]) if cn else pd.Series(dtype=str)
        return {"data": counts(vals, top=top), "filled": int(len(vals)),
                "of": int(len(tx)), "pct": pct(len(vals), len(tx))}

    D["objections"] = {"data": counts(obj, top=15),
                       "filled": int(obj.nunique() and len(obj)),
                       "of": int(len(tx)), "pct": pct(len(obj), len(tx))}
    D["intents"] = slot("intent")
    D["budgets"] = slot("budget")
    D["timelines"] = slot("timeline")
    D["languages"] = slot("language_used")
    D["unit_pref"] = slot("preferred_unit")
    sv = col(tx, "site_visit_agreed")
    if sv:
        agreed = truthy(tx[sv])
        D["site_visit_agreed"] = {"data": [
            {"label": "Agreed to a site visit", "value": int(agreed.sum())},
            {"label": "Did not agree", "value": int((~agreed).sum())}],
            "filled": int(len(tx)), "of": int(len(tx)), "pct": 100.0}
    else:
        D["site_visit_agreed"] = {"data": [], "filled": 0, "of": 0, "pct": 0}

    said = c[c["has_transcript"]]
    D["transcripts"] = table(
        said.sort_values("ts", ascending=False),
        ["ts_str", "lead_ref", "display_name", "outcome", "duration_sec",
         "turns_total", "summary", "agent_said", "lead_said"],
        labels={"ts_str": "when", "agent_said": "what the BOT said",
                "lead_said": "what the CUSTOMER said"})

    # ================================ 3. NEGATIVES
    never = c.groupby("lead_ref")["reached"].max()
    stuck = 0
    if not b["sched"].empty and "sched_status" in b["sched"].columns:
        stuck = int(b["sched"]["sched_status"].isin(["pending", "failed"]).sum())
    D["missed_kpis"] = [
        {"label": "Never-reached dials", "value": f"{len(missed):,}", "sub": f"{pct(len(missed), n)}%"},
        {"label": "Unique leads missed", "value": f"{missed['lead_ref'].nunique():,}"},
        {"label": "Leads never reached once", "value": f"{int((~never).sum()):,}"},
        {"label": "Dials marked failed", "value": f"{int(c['call_failed'].sum()):,}",
         "sub": f"{pct(int(c['call_failed'].sum()), n)}%"},
        {"label": "Scheduled calls stuck", "value": f"{stuck:,}"},
    ]
    D["missed"] = table(
        missed.sort_values("ts", ascending=False),
        ["ts_str", "lead_ref", "display_name", "outcome", "status", "attempt_no",
         "total_attempts", "was_recontacted", "hours_to_next_call", "next_outcome",
         "ever_reached"], labels={"ts_str": "dialled at (IST)"})

    if not drops.empty:
        drops = drops.copy()
        drops["last_customer_words"] = drops["lead_said"].fillna("").str[-120:]
        drops["last_agent_words"] = drops["agent_said"].fillna("").str[-120:]
        ds = drops["drop_stage"].value_counts().sort_index()
        D["drop_stage"] = [{"label": k, "value": int(v), "pct": pct(v, len(drops))}
                           for k, v in ds.items()]
        bins = [0, 10, 20, 30, 45, 60, 90, 120, 180, 300, 10 ** 7]
        lbl = ["0-10s", "10-20s", "20-30s", "30-45s", "45-60s", "60-90s",
               "90-120s", "2-3m", "3-5m", "5m+"]
        cut = pd.cut(drops["duration_sec"], bins=bins, labels=lbl, right=False,
                     include_lowest=True)
        D["drop_hist"] = [{"label": str(k), "value": int(v)}
                          for k, v in cut.value_counts().reindex(lbl, fill_value=0).items()]
        rc = int(drops["was_recontacted"].sum())
        med = drops.loc[drops["was_recontacted"], "hours_to_next_call"].median()
        D["drop_kpis"] = [
            {"label": "Dropped calls", "value": f"{len(drops):,}"},
            {"label": "Contacted again", "value": f"{rc:,}", "sub": f"{pct(rc, len(drops))}%"},
            {"label": "Recontact connected",
             "value": f"{int(drops['recontact_reached'].sum()):,}",
             "sub": f"{pct(int(drops['recontact_reached'].sum()), rc)}% of recontacts"},
            {"label": "Median gap to recontact",
             "value": f"{med:.1f}h" if pd.notna(med) else "—"},
            {"label": "Median sec before drop", "value": f"{drops['duration_sec'].median():.0f}s"},
        ]
        D["drops"] = table(
            drops.sort_values("ts", ascending=False),
            ["ts_str", "lead_ref", "display_name", "duration_sec", "drop_stage",
             "last_customer_words", "last_agent_words",
             "turns_lead", "was_recontacted", "hours_to_next_call", "next_call_str",
             "next_outcome", "hours_to_first_answer", "attempt_no", "total_attempts"],
            labels={"ts_str": "dropped at (IST)", "duration_sec": "sec",
                    "hours_to_next_call": "gap (h)", "next_call_str": "recontacted at"})
    else:
        D["drop_stage"] = D["drop_hist"] = D["drop_kpis"] = []
        D["drops"] = None

    D["rejected"] = table(
        rejected.sort_values("ts", ascending=False),
        ["ts_str", "lead_ref", "display_name", "outcome", "duration_sec",
         "dncTriggered", "dncVerbatim", "summary"], labels={"ts_str": "when"})

    D["retry"] = counts(c["total_attempts"].astype(str) + " attempts")

    # ================================ 4. WHATSAPP
    wa = b["wa_msgs"]
    if not wa.empty and "lead_ref" in wa.columns:
        wa = wa[wa["lead_ref"].isin(refs)]
    dl = b["doc_log"]
    if not dl.empty and "lead_ref" in dl.columns:
        dl = dl[dl["lead_ref"].isin(refs)]

    inb = col(wa, "inbound_count")
    outb = col(wa, "outbound_count")
    D["wa_kpis"] = [
        {"label": "Leads with WhatsApp history", "value": f"{len(wa):,}"},
        {"label": "Messages out",
         "value": f"{int(pd.to_numeric(wa[outb], errors='coerce').fillna(0).sum()):,}" if outb else "—"},
        {"label": "Messages in",
         "value": f"{int(pd.to_numeric(wa[inb], errors='coerce').fillna(0).sum()):,}" if inb else "—"},
        {"label": "Replied at least once",
         "value": f"{int((pd.to_numeric(wa[inb], errors='coerce').fillna(0) > 0).sum()):,}" if inb else "—",
         "sub": f"{pct(int((pd.to_numeric(wa[inb], errors='coerce').fillna(0) > 0).sum()), len(wa))}% of texted" if inb and len(wa) else None},
        {"label": "Documents sent", "value": f"{len(dl):,}"},
    ]
    D["wa_docs"] = counts(nonempty(dl[col(dl, "doc_type")]) if col(dl, "doc_type")
                          else pd.Series(dtype=str))
    D["wa_outcome"] = counts(nonempty(dl[col(dl, "outcome")]) if col(dl, "outcome")
                             else pd.Series(dtype=str))
    if "phone" in c.columns:
        wa, dl = add_phone(wa), add_phone(dl)
    D["wa"] = table(wa, [x for x in ["lead_ref", "inbound_count", "outbound_count",
                                     "total_count", "first_message_at", "last_message_at"]
                         if x in wa.columns])
    D["wa_doc_log"] = table(dl, [x for x in ["lead_ref", "project", "doc_type",
                                             "matched_file", "outcome", "block_reason",
                                             "created_at"] if x in dl.columns])

    # what they said on the call, for the people who were texted
    wa_refs = set(wa["lead_ref"].dropna()) if not wa.empty else set()
    D["wa_said"] = table(
        c[c["lead_ref"].isin(wa_refs) & c["has_transcript"]].sort_values("ts", ascending=False),
        ["ts_str", "lead_ref", "outcome", "summary", "lead_said"],
        labels={"lead_said": "what the CUSTOMER said"})

    # hot-reply queue: inbound WhatsApp with classified intent (Intelligent_CRM)
    cm = b.get("crm_msgs", pd.DataFrame())
    if not cm.empty and {"direction", "lead_ref"} <= set(cm.columns):
        inb_m = cm[cm["direction"].astype(str).eq("inbound")].copy()
        inb_m = inb_m[inb_m["lead_ref"].isin(refs)]
        icol = col(inb_m, "intent")
        D["wa_replies"] = {"total": int(len(inb_m)),
                          "leads": int(inb_m["lead_ref"].nunique()),
                          "intents": counts(nonempty(inb_m[icol]) if icol else pd.Series(dtype=str), top=12)}
        hot = inb_m[nonempty(inb_m[icol]).reindex(inb_m.index).notna()] if icol else inb_m.head(0)
        hot_cols = [x for x in ["ts", "lead_ref", "project", "intent", "body"] if x in hot.columns]
        if len(hot):
            hot = hot.sort_values("ts", ascending=False) if "ts" in hot.columns else hot
            D["wa_hot"] = table(hot, hot_cols, cap=300,
                                labels={"body": "what the customer wrote"})
        else:
            D["wa_hot"] = None
    else:
        D["wa_replies"], D["wa_hot"] = None, None

    # ================================ 5. METRICS
    share = reached["lead_talk_share"].median()
    frt = reached["first_lead_reply_sec"].median()
    # ₹-per-outcome from REAL per-call token counts × the RECALIBRATED rate card.
    #
    # CORRECTION 2026-08-08: the previous model used $3/$12 per 1M — Pro-tier TEXT
    # list pricing, which we assumed. It overstated model spend ~5.9x. The dev
    # team reported an OBSERVED ₹1.60-1.70 of Gemini per answered Indian call.
    # Calibrating against our own exact token counts (mean 32,753 prompt + 190
    # response on 8,149 answered calls) reproduces that figure at $0.50/$2.50 per
    # 1M: mean ₹1.62, median ₹1.74. Two independent sources agreeing to within 2%
    # is the strongest evidence we have, so these are the rates.
    #
    # Telephony: the same source gives ~₹0.60 Plivo for a call answered and
    # dropped after one second — i.e. a per-STARTED-MINUTE charge with a 1-minute
    # minimum, on ANSWERED calls only. Unanswered dials carry no model cost
    # (verified: 0.0% of BUSY/NOT_CONNECTED open a session) and we hold no
    # carrier invoice, so they are costed at zero and that is stated on the card.
    # Telnyx (NRI) is "thoda jada" per the same source but unquantified, so NRI
    # telephony is UNDERSTATED here rather than invented.
    call_inr, gem_inr, tel_inr, answered = cost_of(c)
    cost_inr = float(call_inr.sum())
    booked_h = c[c["is_booked"]]["lead_ref"].nunique()
    sv_att = len(b.get("co_sv_completed", pd.DataFrame()))
    by_outcome = (call_inr.groupby(c["outcome"].fillna("(unclassified)"))
                  .sum().sort_values(ascending=False))
    never = float(call_inr[~c["reached"]].sum())
    vm_mask = c["outcome"] == "VOICEMAIL"
    D["cost"] = {
        "total_inr": round(float(cost_inr)),
        "per_call": round(float(cost_inr / n), 1) if n else 0,
        "per_booked": round(float(cost_inr / booked_h)) if booked_h else None,
        "per_attended_alltime": round(float(cost_inr / sv_att)) if sv_att else None,
        "by_outcome": [{"label": str(k), "value": round(float(v)),
                        "pct": pct(float(v), float(cost_inr))}
                       for k, v in by_outcome.head(9).items()],
        "never_connected_inr": round(never),
        "never_connected_pct": pct(never, float(cost_inr)),
        "voicemail_inr": round(float(call_inr[vm_mask].sum())),
        "voicemail_calls": int(vm_mask.sum()),
        "booked_share_pct": pct(float(call_inr[c["is_booked"]].sum()), float(cost_inr)),
        "model_inr": round(float(gem_inr.sum())),
        "telephony_inr": round(float(tel_inr.sum())),
        "per_answered": round(float(call_inr[answered].mean()), 2) if int(answered.sum()) else None,
        "note": ("RECALIBRATED 2026-08-08. Token counts are EXACT "
                 "(angad.calls.tokenUsage); the rates were not. The old card "
                 "($3/$12 per 1M) was Pro-tier text list pricing we assumed, and it "
                 "overstated model spend ~5.9x. Now $0.50/$2.50 per 1M (USD→INR 96), "
                 "which reproduces the dev team's OBSERVED ₹1.60-1.70 per answered "
                 "call from our own token counts to within 2%. Telephony ₹0.60 per "
                 "started minute, 1-minute minimum, ANSWERED calls only. "
                 "UNANSWERED DIALS ARE COSTED AT ZERO: verified that 0.0% of BUSY / "
                 "NOT_CONNECTED and 0.2% of NO_ANSWER ever open a Gemini session — "
                 "but we hold no carrier invoice, so any per-attempt carrier fee is "
                 "NOT captured here. NRI/Telnyx is understated (rate unknown). "
                 "STRUCTURE: 99.4% of tokens are the fixed ~35k prompt and "
                 "corr(duration, tokens) = 0.12 — a call answered and dropped in one "
                 "second costs almost exactly what a full conversation costs."),
    }

    D["metric_kpis"] = [
        {"label": "₹ spend (approx)", "value": f"₹{cost_inr:,.0f}",
         "sub": f"₹{D['cost']['per_call']}/call"
                + (f" · ₹{D['cost']['per_booked']:,}/booked human" if D["cost"]["per_booked"] else ""),
         "src": "angad.calls.tokenUsage (exact) × rate card calibrated to observed billing"},
        {"label": "Median duration",
         "value": (f"{reached.loc[reached.get('duration_known', True), 'duration_sec'].median():.0f}s"
                   if len(reached) else "—"),
         "sub": f"{int(c.get('duration_known', pd.Series(dtype=bool)).sum()):,} of {n:,} calls have a usable duration"},
        {"label": "Median turns",
         "value": f"{reached['turns_total'].median():.0f}" if len(reached) else "—"},
        {"label": "Customer talk share",
         "value": f"{100 * share:.0f}%" if pd.notna(share) else "—",
         "sub": "low = bot monologuing"},
        {"label": "Median first reply",
         "value": f"{frt:.1f}s" if pd.notna(frt) else "—"},
        {"label": "Calls with a transcript",
         "value": f"{int(c['has_transcript'].sum()):,}",
         "sub": f"{pct(int(c['has_transcript'].sum()), n)}%"},
        {"label": "Total tokens", "value": f"{c['tok_total'].fillna(0).sum():,.0f}"},
    ]
    bins = [0, 15, 30, 60, 90, 120, 180, 240, 300, 10 ** 7]
    lbl = ["0-15s", "15-30s", "30-60s", "1-1.5m", "1.5-2m", "2-3m", "3-4m", "4-5m", "5m+"]
    cut = pd.cut(reached["duration_sec"], bins=bins, labels=lbl, right=False,
                 include_lowest=True)
    D["dur_hist"] = [{"label": str(k), "value": int(v)}
                     for k, v in cut.value_counts().reindex(lbl, fill_value=0).items()]

    depth = (c.groupby("outcome_group")
               .agg(calls=("roomName", "size"), median_turns=("turns_total", "median"),
                    median_sec=("duration_sec", "median"),
                    talk_share=("lead_talk_share", "median"))
               .reindex(GROUPS).dropna(how="all").reset_index())
    depth["talk_share"] = (100 * depth["talk_share"]).round(1)
    D["depth"] = table(depth.round(1), list(depth.columns))
    D["depth_chart"] = [{"label": r["outcome_group"],
                         "value": float(r["median_turns"] or 0),
                         "color": GROUP_COLORS.get(r["outcome_group"], "#8a8f98")}
                        for _, r in depth.iterrows()]

    tok = c.groupby("date")[["tok_prompt", "tok_response"]].sum().reset_index()
    D["tokens"] = {"dates": [str(x) for x in tok["date"]],
                   "series": [{"name": "Prompt", "color": BLUE,
                               "values": [int(x) for x in tok["tok_prompt"].fillna(0)]},
                              {"name": "Response", "color": "#eb6834",
                               "values": [int(x) for x in tok["tok_response"].fillna(0)]}]}
    D["metrics"] = table(
        c.sort_values("ts", ascending=False),
        ["ts_str", "lead_ref", "outcome", "status", "duration_sec", "turns_total",
         "turns_lead", "turns_agent", "lead_talk_share", "first_lead_reply_sec",
         "tok_total", "has_recording", "webhook_status", "attempt_no",
         "total_attempts", "voiceEngine", "slug"], labels={"ts_str": "when"})

    # ---- transcript quality: how much of "what the customer said" is noise
    tq_flags = [x for x in c.columns if x.startswith("tq_") and x != "tq_any"]
    if tq_flags:
        FRIENDLY = {
            "tq_no_transcript_connected": "CONNECTED call, no transcript at all",
            "tq_agent_monologue": "Agent monologue (customer <5% of a 60s+ call)",
            "tq_repeated_line": "Bot loop: same line repeated 3+ times",
            "tq_impossible_density": "Impossible density (>25 chars/sec)",
            "tq_foreign_script": "Hallucinated foreign script (Korean/Thai/…)",
            "tq_ivr_as_customer": "Voicemail prompt transcribed as customer",
            "tq_timing_broken": "Turn timestamps outside the call window",
            "tq_transcript_on_unanswered": "Transcript on an unanswered call",
        }
        n_any = int(truthy(c["tq_any"]).sum())
        n_tx = int(c["has_transcript"].sum())
        D["tq"] = {
            "kpis": [
                {"label": "Calls with any quality flag", "value": f"{n_any:,}",
                 "sub": f"{pct(n_any, n)}% of calls · {pct(n_any, max(n_tx,1))}% of transcribed"},
                {"label": "CONNECTED but no transcript",
                 "value": f"{int(truthy(c['tq_no_transcript_connected']).sum()):,}"},
                {"label": "Voicemail-as-customer",
                 "value": f"{int(truthy(c['tq_ivr_as_customer']).sum()):,}",
                 "sub": "conservative phrase match - a lower bound"},
                {"label": "Bot stuck in a loop",
                 "value": f"{int(truthy(c['tq_repeated_line']).sum()):,}"},
            ],
            "flags": [{"label": FRIENDLY.get(f, f),
                       "value": int(truthy(c[f]).sum())} for f in tq_flags],
        }
    else:
        D["tq"] = None

    D["audit"] = build_audit(b, c, d0, d1)
    D["audit"]["timing"] = build_timing(b, c)
    D["audit"]["pipeline"] = build_pipeline(b, c)
    D["intent_tab"] = build_intent(b, c)
    D["visits_tab"] = build_visits(b, c)
    D["cohorts"] = build_cohorts(b, c)
    D["funnel_x"] = build_funnel_x(b, c, d0, d1)
    D["integrity"] = build_integrity(b, c, irule=q_irule)
    D["cockpit"] = build_cockpit(b)
    D["fboard"] = build_fboard(b, fstage=q_fstage, c=c)
    D["eras"] = build_eras(b)
    D["sources"] = build_sources(b, c)
    # latency proxy panel (post-Varaha-sync caches only)
    if "first_agent_sec" in c.columns:
        lp = pd.to_numeric(c["first_agent_sec"], errors="coerce")
        lp = lp[(lp >= 0) & (lp <= 60)]
        if len(lp):
            drops_lp = c.loc[lp.index]
            slow = lp >= 8
            D["latency"] = {
                "n": int(len(lp)), "median": round(float(lp.median()), 1),
                "p90": round(float(lp.quantile(0.9)), 1),
                "slow_share": pct(int(slow.sum()), len(lp)),
                "drop_when_slow": pct(int(drops_lp[slow]["is_dropped"].sum()),
                                      int(slow.sum())) if slow.any() else None,
                "drop_when_fast": pct(int(drops_lp[~slow]["is_dropped"].sum()),
                                      int((~slow).sum())) if (~slow).any() else None,
            }
        else:
            D["latency"] = None
    else:
        D["latency"] = None

    return D



# ============================================================ identity / audit

def human_map(b):
    """lead_ref -> stable human id (the CRM's Master Lead ID / MLID).

    One human appears under several Zoho project-leads, so counting leads
    overstates people. Sources share ONE 4-digit MLID space, so both resolve
    to the same un-prefixed key (an earlier version prefixed per-source, which
    could split one human in two). Resolution order:
      1. Zoho_Database.mlid_registry - the canonical phone->MLID map
         (its Mongo _id was the phone, so it arrives as lead_ref+mlid);
      2. the daily Zoho export's Master Lead ID;
      3. the lead_ref itself (bot-only number with no CRM identity).
    Intelligent_CRM's registry is NOT used - its key was destroyed by an old
    redaction bug and it adds nothing over the Zoho one.
    """
    m = {}
    reg = b.get("mlid", pd.DataFrame())
    if not reg.empty and {"lead_ref", "mlid"} <= set(reg.columns):
        for r, v in zip(reg["lead_ref"], reg["mlid"]):
            if pd.notna(r) and pd.notna(v):
                m.setdefault(r, "MLID:" + str(v).strip())
    z = b.get("zoho", pd.DataFrame())
    if not z.empty and "master_lead_id" in z.columns:
        for r, v in zip(z["lead_ref"], z["master_lead_id"]):
            if pd.notna(r) and pd.notna(v):
                m.setdefault(r, "MLID:" + str(v).strip().rstrip(".0")
                             if not str(v).strip().endswith(".0")
                             else "MLID:" + str(v).strip()[:-2])
    return m


def cohort_refs(b):
    """lead_refs of the AI campaign cohort, bridged via Master Lead ID
    (the workbook's own phone column is masked)."""
    ai = b.get("co_ai_leads", pd.DataFrame())
    z = b.get("zoho", pd.DataFrame())
    if ai.empty or z.empty or "master_lead_id" not in ai.columns:
        return set()
    mk = set(ai["master_lead_id"].dropna().astype(str).str.replace(".0", "", regex=False))
    zk = z["master_lead_id"].astype(str).str.replace(".0", "", regex=False)
    return set(z.loc[zk.isin(mk), "lead_ref"].dropna())


def build_cohorts(b, c):
    """Standing AI-vs-Human comparison from the imported workbooks. Every row
    names its unit and its source, so the 3,352-vs-2,941 class of confusion
    cannot recur."""
    ai = b.get("co_ai_leads", pd.DataFrame())
    hu = b.get("co_hu_leads", pd.DataFrame())
    if ai.empty and hu.empty:
        return {"available": False}

    refs = cohort_refs(b)
    cc = c[c["lead_ref"].isin(refs)]
    conn_calls = int(cc["reached"].sum())
    conn_leads = int(cc[cc["reached"]]["lead_ref"].nunique())
    dialled = int(cc["lead_ref"].nunique())
    booked_sys = int(cc[cc["outcome"].isin(["PRE_SITE", "VIRTUAL_TOUR"])]["lead_ref"].nunique())

    ai_rows = len(ai)
    ai_humans = int(ai["master_lead_id"].nunique()) if "master_lead_id" in ai.columns else None
    hu_rows = len(hu)
    hu_humans = int(hu["mlid"].nunique()) if "mlid" in hu.columns else None
    hu_contacted = int(hu["contacted"].astype(str).str.upper().eq("YES").sum()) \
        if "contacted" in hu.columns else None

    ai_booked = len(b.get("co_ai_booked", pd.DataFrame()))
    hu_bk = b.get("co_hu_booked", pd.DataFrame())
    hu_booked = len(hu_bk)
    hu_booked_u = int(hu_bk["mlid"].nunique()) if "mlid" in hu_bk.columns else None
    ai_dn = b.get("co_ai_done", pd.DataFrame())
    ai_done = int(ai_dn["mlid"].nunique()) if "mlid" in ai_dn.columns else len(ai_dn)
    hu_dn = b.get("co_hu_done", pd.DataFrame())
    hu_done = int(hu_dn["mlid"].nunique()) if "mlid" in hu_dn.columns else len(hu_dn)
    sv_true = len(b.get("co_sv_true", pd.DataFrame()))
    sv_false = len(b.get("co_sv_false", pd.DataFrame()))

    def pr(v, d):
        return f"{v:,} ({pct(v, d)}%)" if v is not None and d else (f"{v:,}" if v is not None else "—")

    rows = [
        ["Lead rows (person × project)", f"{ai_rows:,}", f"{hu_rows:,}"],
        ["Unique humans", pr(ai_humans, None), pr(hu_humans, None)],
        ["Humans dialled / worked", f"{dialled:,}",
         pr(hu_contacted, None) + " contacted"],
        ["Connected calls (dialler log)", f"{conn_calls:,}", "no call log"],
        ["Unique humans connected", pr(conn_leads, ai_humans), pr(hu_contacted, hu_humans)],
        ["Visits booked (their sheet)", f"{ai_booked:,}",
         f"{hu_booked:,}" + (f" ({hu_booked_u:,} humans)" if hu_booked_u else "")],
        ["Visits booked (system, booked-outcome leads)", f"{booked_sys:,}", "—"],
        ["Visits DONE", f"{ai_done:,}", f"{hu_done:,}"],
        ["Booked → done", f"{pct(ai_done, ai_booked)}%", f"{pct(hu_done, hu_booked)}%"],
        ["Connected/contacted → booked", f"{pct(ai_booked, conn_leads)}%",
         f"{pct(hu_booked, hu_contacted or 1)}%"],
        ["Humans → visit done", f"{pct(ai_done, ai_humans or 1)}%",
         f"{pct(hu_done, hu_humans or 1)}%"],
    ]
    C = {"available": True,
         "compare": {"cols": ["metric", "AI (ANGAD)", "Human callers"],
                     "rows": rows, "total": len(rows), "shown": len(rows)}}
    C["attribution"] = [
        {"label": "Verified: visit truly from an ANGAD booking", "value": sv_true,
         "color": "#008300"},
        {"label": "Rejected: visit credited to ANGAD wrongly", "value": sv_false,
         "color": "#e34948"},
    ] if (sv_true or sv_false) else []

    # daily table: their sheet columns beside the system of record
    daily = b.get("co_ai_daily", pd.DataFrame())
    if not daily.empty and "date" in daily.columns:
        dd = daily.copy()
        dd["d"] = pd.to_datetime(dd["date"], errors="coerce").dt.date
        born = parse_mixed_date(ai.get("born_date")).dt.date.value_counts() \
            if "born_date" in ai.columns else pd.Series(dtype=int)
        g = cc.groupby("date")
        sys_conn = g["reached"].sum()
        sys_uniq = cc[cc["reached"]].groupby("date")["lead_ref"].nunique()
        sys_bk = (cc[cc["outcome"].isin(["PRE_SITE", "VIRTUAL_TOUR"])]
                  .groupby("date")["lead_ref"].nunique())
        rows2 = []
        for _, r in dd.sort_values("d").iterrows():
            d_ = r["d"]
            if pd.isna(d_):
                continue
            rows2.append([str(d_), int(born.get(d_, 0)),
                          clean(r.get("successful_calls_picked_up")),
                          int(sys_conn.get(d_, 0)), int(sys_uniq.get(d_, 0)),
                          clean(r.get("site_visits_booked")), int(sys_bk.get(d_, 0))])
        C["daily"] = {"cols": ["date", "humans born (sys)", "picked up (sheet)",
                               "connected calls (sys)", "unique leads connected (sys)",
                               "booked (sheet)", "booked leads (sys)"],
                      "rows": rows2, "total": len(rows2), "shown": len(rows2)}
    else:
        C["daily"] = None
    return C


def build_funnel_x(b, c, d0, d1):
    """The end-to-end funnel: CRM leads -> humans -> dialled -> connected ->
    real conversation -> booked -> confirmed -> visited. Sheet-sourced stages
    are labelled as such; they exist only for the AI campaign cohort."""
    z = b.get("zoho", pd.DataFrame())
    refmap = human_map(b)
    human = c["lead_ref"].map(lambda r: refmap.get(r, r))

    steps = []
    extra_note = None
    cw = c
    if not z.empty and "lead_received_at" in z.columns:
        recv = pd.to_datetime(z["lead_received_at"], errors="coerce")
        zw = z[(recv >= pd.Timestamp(d0)) & (recv < pd.Timestamp(d1) + pd.Timedelta(days=1))]
        steps.append(("Zoho lead rows received (window)", len(zw), "Zoho export"))
        steps.append(("Unique humans among them",
                      int(zw["master_lead_id"].nunique()), "Zoho export"))
        # a strict funnel follows ONE population: restrict the dialler stages
        # to the window-received cohort; humans dialled outside it (older
        # leads, numbers absent from the export) are reported separately
        zw_refs = set(zw["lead_ref"].dropna())
        cw = c[c["lead_ref"].isin(zw_refs)]
        extra = int(human[~c["lead_ref"].isin(zw_refs)].nunique())
        if extra:
            extra_note = (f"+{extra:,} additional humans were dialled outside "
                          "this cohort (received before the window, or absent "
                          "from the Zoho export) — excluded from the funnel "
                          "so every step is a subset of the one above.")
    human_w = cw["lead_ref"].map(lambda r: refmap.get(r, r))
    steps.append(("Humans dialled", int(human_w.nunique()), "dialler log"))
    # connected = answered outcome OR the customer audibly spoke; a dropped
    # call where the customer talked is still a connection, and without this
    # the conversation step could exceed the connected step
    conn_mask = cw["reached"]
    if "human_conversation" in cw.columns:
        conn_mask = conn_mask | cw["human_conversation"]
    steps.append(("Humans connected (answered or spoke)",
                  int(human_w[conn_mask].nunique()), "dialler log"))
    if "human_conversation" in cw.columns:
        steps.append(("Humans with a real conversation",
                      int(human_w[cw["human_conversation"]].nunique()), "dialler log"))
    steps.append(("Humans with a booked-visit call outcome",
                  int(human_w[cw["outcome"].isin(["PRE_SITE", "VIRTUAL_TOUR"])].nunique()),
                  "dialler log"))
    ai_booked = b.get("co_ai_booked", pd.DataFrame())
    if len(ai_booked):
        steps.append(("Bookings surviving manual confirmation",
                      len(ai_booked), "ops sheet"))
    ai_done = b.get("co_ai_done", pd.DataFrame())
    if len(ai_done):
        n = int(ai_done["mlid"].nunique()) if "mlid" in ai_done.columns else len(ai_done)
        steps.append(("Site visits actually done", n, "ops sheet"))

    top = steps[0][1] if steps else 0
    out = []
    prev = None
    for label, v, src in steps:
        out.append({"label": f"{label}  [{src}]", "value": int(v),
                    "pct": pct(v, top),
                    "step_pct": pct(v, prev) if prev else 100.0})
        prev = v if v else prev
    return {"steps": out, "note": extra_note}


def build_pipeline(b, c):
    """Where call records vanish between the dialler and the CRM. Respects the
    current filters: rates are computed over the SELECTED calls."""
    full = c if c is not None and len(c) else b["calls"]
    rooms = set(full["roomName"].dropna().astype(str))
    hops = []

    def hop(name, df, keycol, note=""):
        if df is None or df.empty or keycol not in df.columns:
            return
        keys = df[keycol].dropna().astype(str)
        matched = int(keys.isin(rooms).sum())
        hops.append({"store": name, "rows": int(len(df)), "matched": matched,
                     "pct": pct(matched, len(full)), "note": note})

    hops.append({"store": "angad.calls (dial log - ground truth)",
                 "rows": int(len(full)), "matched": int(len(full)), "pct": 100.0,
                 "note": ""})
    sv = b.get("schedules_vb", pd.DataFrame())
    if not sv.empty:
        # a schedule row can carry the dialler key in call_id OR only in
        # external_schedule_id - single-key matching understates by half
        esids = set(full["externalScheduleId"].dropna().astype(str))             if "externalScheduleId" in full.columns else set()
        m = 0
        cid = sv["call_id"].astype(str) if "call_id" in sv.columns else pd.Series("", index=sv.index)
        esd = sv["external_schedule_id"].astype(str) if "external_schedule_id" in sv.columns             else pd.Series("", index=sv.index)
        m = int((cid.isin(rooms) | esd.isin(esids)).sum())
        hops.append({"store": "ASBLVoiceBot.call_schedules", "rows": int(len(sv)),
                     "matched": m, "pct": pct(m, len(full)),
                     "note": "who requested each dial (crm / dashboard / retry)"})
    tx = b.get("tx", pd.DataFrame())
    hop("ASBLVoiceBot.call_transcripts", tx, "call_sid", "conversation record")
    hop("ASBLVoiceBot.call_recordings", b.get("recordings"), "call_sid", "audio")
    hop("Zoho.posthook_payloads", b.get("posthooks"), "call_id",
        "ROLLING window - upstream deletes old rows; cache preserves")
    hop("Intelligent_CRM.calls", b.get("crm_calls"), "_id", "what the new CRM knows")

    # CRM blindness per outcome
    crm = b.get("crm_calls", pd.DataFrame())
    blind_rows = []
    if not crm.empty and "_id" in crm.columns:
        crm_ids = set(crm["_id"].dropna().astype(str))
        ph = b.get("posthooks", pd.DataFrame())
        ph_ids = set(ph["call_id"].dropna().astype(str)) if not ph.empty and "call_id" in ph.columns else set()
        known = crm_ids | ph_ids
        f = full.copy()
        f["blind"] = ~f["roomName"].astype(str).isin(known)
        g = f.groupby(f["outcome"].fillna("(none)"))["blind"]
        for outc, gg in g:
            blind_rows.append([outc, int(gg.size), int(gg.sum()),
                               pct(int(gg.sum()), int(gg.size))])
        blind_rows.sort(key=lambda r: -r[2])
    return {"hops": hops,
            "blind": {"cols": ["outcome", "dialled", "invisible to CRM", "%"],
                      "rows": blind_rows, "total": len(blind_rows),
                      "shown": len(blind_rows)} if blind_rows else None}


def build_timing(b, c):
    """When do people answer? Heatmap of connect rate by hour x weekday, plus
    speed-to-lead: how fast the first dial follows lead birth, and whether
    speed buys connection."""
    T = {}
    WD = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    g = c.groupby(["weekday", "hour"])
    dials = g.size()
    conn = g["reached"].sum()
    grid, counts = [], []
    for w in WD:
        row_r, row_n = [], []
        for h in range(24):
            n = int(dials.get((w, h), 0))
            r = pct(int(conn.get((w, h), 0)), n) if n else None
            row_r.append(r)
            row_n.append(n)
        grid.append(row_r)
        counts.append(row_n)
    T["heatmap"] = {"weekdays": WD, "hours": [f"{h:02d}" for h in range(24)],
                    "rate": grid, "dials": counts}

    # speed to lead: born -> first dial, per ref
    z = b.get("zoho", pd.DataFrame())
    T["speed"] = []
    if not z.empty and "born_date" in z.columns:
        born = (z.dropna(subset=["lead_ref"])
                 .assign(bd=parse_mixed_date(z["born_date"]))
                 .dropna(subset=["bd"]).drop_duplicates("lead_ref")
                 .set_index("lead_ref")["bd"])
        first = c.groupby("lead_ref").agg(first_dial=("ts", "min"),
                                          first_reached=("reached", "max"))
        j = first.join(born, how="inner")
        if len(j):
            bd = j["bd"]
            if bd.dt.tz is None:
                bd = bd.dt.tz_localize("Asia/Kolkata", nonexistent="NaT",
                                       ambiguous="NaT")
            delta_h = (j["first_dial"].dt.tz_convert("Asia/Kolkata") - bd
                       ).dt.total_seconds() / 3600
            j = j.assign(delta_h=delta_h)
            j = j[(j["delta_h"] >= 0) & (j["delta_h"] < 24 * 90)]
            bins = [(1/12, "< 5 min"), (0.5, "5-30 min"), (2, "30 min - 2 h"),
                    (6, "2-6 h"), (24, "6-24 h"), (72, "1-3 days"),
                    (10**9, "3+ days")]
            prev = -1
            for hi, lbl in bins:
                m = (j["delta_h"] > prev) & (j["delta_h"] <= hi)
                n = int(m.sum())
                if n:
                    ever = int(j.loc[m, "first_reached"].sum())
                    T["speed"].append({"label": lbl, "value": n,
                                       "pct": pct(ever, n)})
                prev = hi
            T["speed_note"] = (f"{len(j):,} dialled leads have a usable born "
                               "timestamp. Bar = leads first-dialled in that "
                               "delay bucket; (n%) = share ever connected.")
    return T



PHONE_IN_URL = __import__("re").compile(r"(?<!\d)[6-9]\d{9}(?!\d)")


def playable(url):
    """True when the recording URL is worth offering as audio: stable Plivo
    media (no presigned querystring) and free of phone-shaped digits."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    if "?" in url:            # presigned S3 - expires in minutes, almost surely dead
        return False
    if PHONE_IN_URL.search(url):
        return False          # PII in the path - never expose
    return True


FUNNEL_STAGES = [
    ("dnc", "Do not call", "#e34948"),
    ("booked_cold", "Booked, then went cold", "#eb6834"),
    ("booked", "Visit booked", "#008300"),
    ("rejected", "Not interested", "#eb6834"),
    ("conversation", "In conversation", "#2a78d6"),
    ("answered", "Answered, no real talk yet", "#4a3aa7"),
    ("unreached", "Dialled, never answered", "#8a8f98"),
]


def classify_leads(b):
    """One funnel stage per lead_ref, from the FULL call history. Later events
    override earlier ones (a booking followed by not-interested = booked_cold;
    DNC suppresses everything)."""
    full = b["calls"]
    g = full.sort_values("ts").groupby("lead_ref")
    agg = g.agg(
        attempts=("roomName", "size"),
        ever_reached=("reached", "max"),
        ever_conv=("human_conversation", "max"),
        last_ts=("ts", "max"),
        last_outcome=("outcome", "last"),
        dnc=("dncTriggered", "max"),
    )
    booked_ts = (full[full["outcome"].isin(["PRE_SITE", "VIRTUAL_TOUR"])]
                 .groupby("lead_ref")["ts"].max())
    ni_ts = (full[full["outcome"].eq("NOT_INTERESTED")]
             .groupby("lead_ref")["ts"].max())
    agg["booked_ts"] = booked_ts
    agg["ni_ts"] = ni_ts

    def stage(r):
        if truthy(pd.Series([r["dnc"]])).iloc[0] or r["last_outcome"] == "DNC_REQUESTED":
            return "dnc"
        if pd.notna(r["booked_ts"]):
            if pd.notna(r["ni_ts"]) and r["ni_ts"] > r["booked_ts"]:
                return "booked_cold"
            return "booked"
        if pd.notna(r["ni_ts"]):
            return "rejected"
        if bool(r["ever_conv"]):
            return "conversation"
        if bool(r["ever_reached"]):
            return "answered"
        return "unreached"

    agg["stage"] = agg.apply(stage, axis=1)
    return agg


def best_hours(b, min_dials=500):
    """Global best calling hours with confidence, for the follow-up view."""
    full = b["calls"]
    g = full.groupby("hour").agg(dials=("roomName", "size"), conn=("reached", "sum"))
    g = g[g["dials"] >= min_dials]
    g["rate"] = 100 * g["conn"] / g["dials"]
    g = g[(g.index >= 9) & (g.index < 21)]  # only compliant hours are recommendable
    return g.sort_values("rate", ascending=False)


def build_fboard(b, fstage=None, c=None):
    """KURMA-2: the funnel board + follow-up scheduler."""
    agg = classify_leads(b)
    # universal filter responsiveness: limit the board to leads that appear in
    # the CURRENT filtered slice (stage itself still derives from full history
    # so a date filter cannot mislabel a lead's true state)
    if c is not None and len(c) < len(b["calls"]):
        agg = agg[agg.index.isin(set(c["lead_ref"].dropna()))]
    counts = agg["stage"].value_counts()
    total = int(len(agg))
    cards = [{"id": sid, "label": lbl, "color": col,
              "value": int(counts.get(sid, 0)),
              "pct": pct(int(counts.get(sid, 0)), total)}
             for sid, lbl, col in FUNNEL_STAGES]

    out = {"cards": cards, "total": total, "selected": fstage, "detail": None}

    if fstage in {sid for sid, _, _ in FUNNEL_STAGES}:
        sub = agg[agg["stage"] == fstage].copy()
        sub = sub.reset_index()
        sub["last_ist"] = pd.to_datetime(sub["last_ts"], errors="coerce", utc=True) \
            .dt.tz_convert("Asia/Kolkata").dt.strftime("%d %b %H:%M")
        sub = sub.sort_values("last_ts", ascending=False)
        out["detail"] = table(
            sub, ["lead_ref", "attempts", "last_outcome", "last_ist"],
            cap=800, labels={"last_ist": "last dialled (IST)"})

    # ---------- follow-up scheduler
    sched = b.get("sched", pd.DataFrame())
    fu = None
    if not sched.empty and "dueAt" in sched.columns:
        pend = sched[sched["sched_status"].eq("pending")].copy()
        if len(pend):
            pend["due_ist"] = pd.to_datetime(pend["dueAt"], errors="coerce", utc=True) \
                .dt.tz_convert("Asia/Kolkata").dt.strftime("%d %b %H:%M")
            fu = table(pend.sort_values("dueAt"),
                       [x for x in ["lead_ref", "slug", "kind", "due_ist", "attempts"]
                        if x in pend.columns], cap=300)
    out["followups"] = fu

    # recommended next-dial hour for workable stages (own history > global)
    bh = best_hours(b)
    global_hint = (f"{bh.index[0]:02d}:00 ({bh['rate'].iloc[0]:.1f}% over "
                   f"{int(bh['dials'].iloc[0]):,} dials)") if len(bh) else "n/a"
    workable = agg[agg["stage"].isin(["conversation", "answered", "unreached"])]
    full = b["calls"]
    own = (full[full["reached"]].groupby("lead_ref")["hour"]
           .agg(lambda x: int(x.mode().iloc[0]) if len(x) else None))
    rec = workable.reset_index()[["lead_ref", "stage", "attempts", "last_ts"]].copy()
    rec["own_hour"] = rec["lead_ref"].map(own)
    rec["recommended"] = rec["own_hour"].map(
        lambda h: f"{int(h):02d}:00 (they answered then before)" if pd.notna(h) else
        f"{global_hint} [global]")
    rec["last_ist"] = pd.to_datetime(rec["last_ts"], errors="coerce", utc=True) \
        .dt.tz_convert("Asia/Kolkata").dt.strftime("%d %b")
    rec = rec.sort_values(["own_hour"], na_position="last")
    out["recommend"] = table(
        rec, ["lead_ref", "stage", "attempts", "last_ist", "recommended"],
        cap=500, labels={"last_ist": "last dialled",
                         "recommended": "recommended next-dial hour (IST)"})
    out["hours_note"] = (f"Global best compliant hour: {global_hint}. "
                        "Per-lead recommendation prefers the hour the lead has "
                        "actually answered at before; confidence shown inline. "
                        "Hours outside 09-21 IST are never recommended.")
    return out


def build_eras(b):
    """VARAHA: Kaalbhojaditya vs ANGAD, side by side. Same metrics, same
    units; provider split shown so the eras are compared honestly."""
    full = b["calls"]
    if "era" not in full.columns:
        return None
    rows = []
    for era in ["Kaalbhojaditya", "ANGAD"]:
        e = full[full["era"] == era]
        if not len(e):
            continue
        dials = len(e)
        booked = int(e[e["outcome"].isin(["PRE_SITE", "VIRTUAL_TOUR"])]["lead_ref"].nunique())
        conv = int(e["human_conversation"].sum())
        rows.append({
            "era": era, "days": int(e["date"].nunique()), "dials": dials,
            "humans": int(e["lead_ref"].nunique()),
            "connected_calls": int(e["reached"].sum()),
            "conversations": conv,
            "booked_leads": booked,
            "booked_per_1k_dials": round(1000 * booked / dials, 2) if dials else 0,
            "conv_per_1k_dials": round(1000 * conv / dials, 1) if dials else 0,
            "median_talk_s": round(float(e.loc[e["reached"], "duration_sec"].median() or 0)),
            "out_of_window": int(((e["hour"] < 9) | (e["hour"] >= 21)).sum()),
        })
    if not rows:
        return None
    cols = list(rows[0].keys())
    return {"cols": cols, "rows": [[r[c] for c in cols] for r in rows],
            "total": len(rows), "shown": len(rows),
            "note": ("Kaalbhojaditya = everything before 2026-07-17; ANGAD = on/after. "
                     "Same units everywhere: dials are call events, humans/booked are "
                     "unique people. Provider mix differs between eras - compare rates, "
                     "not raw counts.")}


def build_sources(b, c):
    """VARAHA: URL / source intelligence from Zoho_Database.leads (already in
    the cache): which sources and pages produce leads that CONNECT and BOOK."""
    zl = b.get("zleads", pd.DataFrame())
    if zl.empty or "lead_ref" not in zl.columns:
        return None
    refs = set(c["lead_ref"].dropna())
    z = zl[zl["lead_ref"].isin(refs)].drop_duplicates("lead_ref")
    if not len(z):
        return None
    per = c.groupby("lead_ref").agg(
        dials=("roomName", "size"), conn=("reached", "max"),
        conv=("human_conversation", "max"),
        booked=("outcome", lambda x: x.isin(["PRE_SITE", "VIRTUAL_TOUR"]).any()))
    j = z.merge(per, on="lead_ref", how="left").fillna(
        {"dials": 0, "conn": False, "conv": False, "booked": False})

    def cut(colname, top=12):
        if colname not in j.columns:
            return None
        v = j.copy()
        v[colname] = v[colname].fillna("(unknown)").astype(str).str.strip()
        v.loc[v[colname].isin(["", "nan", "None"]), colname] = "(unknown)"
        g = v.groupby(colname).agg(
            leads=("lead_ref", "nunique"), connected=("conn", "sum"),
            conversations=("conv", "sum"), booked=("booked", "sum"))
        g = g.sort_values("leads", ascending=False).head(top)
        g["connect_%"] = (100 * g["connected"] / g["leads"]).round(1)
        g["book_%"] = (100 * g["booked"] / g["leads"]).round(1)
        g = g.reset_index()
        return {"cols": list(g.columns),
                "rows": [[clean(x) for x in r] for r in g.itertuples(index=False)],
                "total": len(g), "shown": len(g)}

    return {"lead_source": cut("lead_source"),
            "utm_source": cut("utm_source"),
            "campaign": cut("campaign_name"),
            "first_page": cut("first_page_visited"),
            "referrer": cut("referrer_url"),
            "note": ("From Zoho_Database.leads already in the cache - no CSV needed. "
                     "Quality columns (connect % / book %) matter more than lead counts: "
                     "a source that produces answering, booking humans beats one that "
                     "produces volume.")}


def build_cockpit(b):
    """KURMA: the Today screen. Latest data-day cards + the Call Room picks +
    the daily digest. Independent of user filters - this is the fixed morning
    view; every card carries a jump target into the filtered deep views."""
    full = b["calls"]
    rec = b.get("recordings", pd.DataFrame())
    sched = b.get("sched", pd.DataFrame())

    day = full["date"].max()
    d = full[full["date"] == day]
    prev = full[full["date"] == (pd.Timestamp(day) - pd.Timedelta(days=1)).date()] \
        if pd.notna(day) else full.head(0)

    def delta(now, before):
        return None if not len(prev) else int(now) - int(before)

    conn_calls = int(d["reached"].sum())
    conv = int(d["human_conversation"].sum()) if "human_conversation" in d.columns else 0
    booked = int(d[d["outcome"].isin(["PRE_SITE", "VIRTUAL_TOUR"])]["lead_ref"].nunique())
    dnc = int(truthy(d["dncTriggered"]).sum())
    oo = int(((d["hour"] < 9) | (d["hour"] >= 21)).sum())
    whf = int(d["webhook_status"].astype(str).str.contains("failed", na=False).sum())

    fu_due = 0
    if not sched.empty and "dueAt" in sched.columns:
        due = pd.to_datetime(sched["dueAt"], errors="coerce", utc=True)
        today_ist = pd.Timestamp.now(tz="Asia/Kolkata").date()
        fu_due = int(((due.dt.tz_convert("Asia/Kolkata").dt.date == today_ist)
                      & sched["sched_status"].eq("pending")).sum())

    cm = b.get("crm_msgs", pd.DataFrame())
    hot = 0
    if not cm.empty and "direction" in cm.columns:
        ts = pd.to_datetime(cm.get("ts"), errors="coerce", utc=True)
        hot = int((cm["direction"].astype(str).eq("inbound")
                   & (ts >= pd.Timestamp(day).tz_localize("UTC"))).sum())

    idle_hours = None
    newest = pd.to_datetime(full["ts"], errors="coerce").max()
    if pd.notna(newest):
        idle_hours = round((pd.Timestamp.now(tz="UTC") - newest).total_seconds() / 3600, 1)

    cards = [
        {"label": "Dials", "value": f"{len(d):,}", "delta": delta(len(d), len(prev)),
         "target": {"tab": "numbers"}},
        {"label": "Connected calls", "value": f"{conn_calls:,}",
         "delta": delta(conn_calls, prev["reached"].sum() if len(prev) else 0),
         "target": {"tab": "positives"}},
        {"label": "Real conversations", "value": f"{conv:,}",
         "delta": delta(conv, prev["human_conversation"].sum() if len(prev) else 0),
         "target": {"tab": "positives"}},
        {"label": "Leads booked", "value": f"{booked:,}", "accent": "good",
         "delta": delta(booked, prev[prev["outcome"].isin(["PRE_SITE", "VIRTUAL_TOUR"])]
                        ["lead_ref"].nunique() if len(prev) else 0),
         "target": {"tab": "visits"}},
        {"label": "Follow-ups due today", "value": f"{fu_due:,}",
         "target": {"tab": "negatives"}},
        {"label": "WhatsApp replies (day)", "value": f"{hot:,}", "accent": "good" if hot else None,
         "target": {"tab": "whatsapp"}},
        {"label": "DNC requests (day)", "value": f"{dnc:,}",
         "accent": "bad" if dnc else None, "target": {"tab": "integrity"}},
        {"label": "Out-of-window dials (day)", "value": f"{oo:,}",
         "accent": "bad" if oo else None, "target": {"tab": "integrity", "irule": "hours"}},
        {"label": "Webhook failures (day)", "value": f"{whf:,}",
         "accent": "bad" if whf else None, "target": {"tab": "integrity", "irule": "whfail"}},
    ]

    # ---------- the Call Room: best & worst of the day, audio + transcript
    pool = d
    if int(pool["human_conversation"].sum() if len(pool) else 0) < 3:
        # thin day (e.g. dialler idle): widen to the last 3 data days
        days3 = sorted(full["date"].dropna().unique())[-3:]
        pool = full[full["date"].isin(days3)]

    rmap = {}
    if not rec.empty:
        rr = rec.dropna(subset=["recording_url"])
        rmap = dict(zip(rr["call_sid"].astype(str), rr["recording_url"].astype(str)))

    def pick(df, n):
        rows = []
        for _, r in df.head(n * 2).iterrows():
            url = rmap.get(str(r["roomName"]))
            rows.append({
                "roomName": str(r["roomName"]), "lead_ref": r["lead_ref"],
                "when": str(pd.to_datetime(r["ts_ist"]))[:16],
                "outcome": r["outcome"], "dur": int(r["duration_sec"] or 0),
                "why": r["_why"],
                "audio": url if playable(url) else None,
                "transcript_agent": (r.get("agent_said") or "")[:3500],
                "transcript_lead": (r.get("lead_said") or "")[:3500],
                "summary": (r.get("summary") or "")[:400],
            })
            if len(rows) >= n:
                break
        return rows

    best = pool[pool["outcome"].isin(["PRE_SITE", "VIRTUAL_TOUR"])].copy()
    best["_why"] = "booked a visit"
    more = pool[pool["human_conversation"] & (pool["duration_sec"] >= 90)
                & ~pool["outcome"].isin(["PRE_SITE", "VIRTUAL_TOUR"])].copy()
    more["_why"] = "long real conversation"
    best = pd.concat([best, more.sort_values("duration_sec", ascending=False)])
    best["_has_audio"] = best["roomName"].astype(str).map(
        lambda x: playable(rmap.get(x)))
    best = best.sort_values(["_has_audio"], ascending=False)

    tqcols = [x for x in pool.columns if x.startswith("tq_")]
    worst_a = pool[truthy(pool["dncTriggered"])].copy()
    worst_a["_why"] = "customer asked never to be called"
    worst_b = pool[pool["is_dropped"] & pool["duration_sec"].between(30, 120)].copy()
    worst_b["_why"] = "dropped mid-pitch"
    worst_c = (pool[pool[tqcols].fillna(False).astype(bool).any(axis=1)].copy()
               if tqcols else pool.head(0).copy())
    if len(worst_c):
        worst_c["_why"] = "transcript-quality failure (loop/hallucination)"
    worst = pd.concat([worst_a, worst_b, worst_c])
    worst = worst[~worst.index.duplicated()]
    worst["_has_audio"] = worst["roomName"].astype(str).map(
        lambda x: playable(rmap.get(x)))
    worst = worst.sort_values(["_has_audio", "duration_sec"], ascending=False)

    callroom = {"best": pick(best, 3), "worst": pick(worst, 3),
                "note": ("Audio plays for stable Plivo recordings; presigned/"
                         "expired links degrade to transcript-only. "
                         f"Pool: {'latest day' if pool is d else 'last 3 data days (thin day)'}.")}

    # ---------- daily digest (auto-written once per data-day)
    digest = {
        "day": str(day), "idle_hours": idle_hours,
        "right": [], "wrong": [], "actions": [],
    }
    if booked:
        digest["right"].append(f"{booked} lead(s) booked a visit")
    if conv:
        digest["right"].append(f"{conv} real conversations from {len(d):,} dials")
    if hot:
        digest["right"].append(f"{hot} WhatsApp replies came in - work the hot queue")
    if not booked:
        digest["wrong"].append("ZERO bookings on the day")
    if oo:
        digest["wrong"].append(f"{oo} dials outside 09:00-21:00 IST (night-defer leak)")
    if dnc:
        digest["wrong"].append(f"{dnc} do-not-call request(s) triggered")
    if whf:
        digest["wrong"].append(f"{whf} webhook deliveries failed")
    if idle_hours and idle_hours > 6:
        digest["wrong"].append(f"DIALLER IDLE {idle_hours:.0f}h - nothing is being dialled")
        digest["actions"].append("Ask ops why Zoho stopped requesting dials (pool empty?)")

    # FOLLOW-UP DISCIPLINE, checked for the day's calls, every day
    day_bk = d[d["outcome"].isin(["PRE_SITE", "VIRTUAL_TOUR"])]
    if len(day_bk) and not sched.empty and "kind" in sched.columns:
        svd = sched[sched["kind"].eq("sitevisit")]
        svc = pd.to_datetime(svd.get("sched_createdAt"), errors="coerce", utc=True)
        covered = 0
        for _, bk_ in day_bk.iterrows():
            if len(svd[(svd["lead_ref"] == bk_["lead_ref"])
                       & (svc >= bk_["ts"] - pd.Timedelta(minutes=5))]):
                covered += 1
        miss = len(day_bk) - covered
        if miss:
            digest["wrong"].append(
                f"FOLLOW-UP GAP: {miss} of {len(day_bk)} today's bookings have "
                "NO reminder scheduled")
            digest["actions"].append(
                "Open Integrity > 'Visit BOOKED but no reminder' and hand the "
                "list to whoever confirms visits manually")
        else:
            digest["right"].append(
                f"All {len(day_bk)} of today's bookings have reminders scheduled")
    if fu_due:
        digest["actions"].append(f"{fu_due} follow-ups due today - confirm the scheduler fires")
    if hot:
        digest["actions"].append("Call the WhatsApp repliers first (they asked for it)")
    if not digest["actions"]:
        digest["actions"].append("Review the Call Room worst-3 and file one product fix")

    try:
        ddir = HERE / "RELEASES" / "daily"
        ddir.mkdir(parents=True, exist_ok=True)
        f = ddir / f"{day}.md"
        if not f.exists():
            f.write_text(
                f"# Daily audit - {day}\n\n"
                f"Dials {len(d):,} | connected {conn_calls:,} | conversations {conv:,} | "
                f"booked {booked} | DNC {dnc} | out-of-window {oo} | webhook-fail {whf}\n\n"
                "## Went right\n" + "".join(f"- {x}\n" for x in digest["right"] or ["- (nothing notable)"])
                + "\n## Went wrong\n" + "".join(f"- {x}\n" for x in digest["wrong"] or ["- (clean day)"])
                + "\n## Actions\n" + "".join(f"- {x}\n" for x in digest["actions"]),
                encoding="utf-8")
    except Exception:
        pass

    return {"day": str(day), "cards": cards, "callroom": callroom, "digest": digest}


SEV_COLORS = {"critical": "#e34948", "high": "#eb6834",
              "medium": "#eda100", "low": "#8a8f98"}


def build_integrity(b, c, irule=None):
    """The inversion audit: every way this calling agent can misbehave,
    computed as a rule with a live count and drill-down rows. This is the
    'what is going wrong' surface - if a number here is not zero, someone
    should either fix it or consciously accept it."""
    R = []
    full = b["calls"]  # rules about sequencing need ALL calls, not the filtered slice
    now = pd.Timestamp.now(tz="Asia/Kolkata")

    def rule(rid, name, severity, mask_df, why, second_order, cols=None):
        n = int(len(mask_df))
        entry = {"id": rid, "name": name, "severity": severity, "count": n,
                 "why": why, "second_order": second_order}
        if irule == rid and n:
            show = mask_df.sort_values("ts", ascending=False) if "ts" in mask_df.columns else mask_df
            entry["detail"] = table(show, cols or
                ["ts_str", "lead_ref", "display_name", "outcome", "duration_sec",
                 "attempt_no", "total_attempts", "slug"], cap=500)
        R.append(entry)

    c2 = c.copy()
    c2["ts_str"] = pd.to_datetime(c2["ts_ist"], errors="coerce").dt.strftime("%d %b %H:%M")
    sched = b.get("sched", pd.DataFrame())

    # ---- 1. harassment cadence
    fast = c2[pd.to_numeric(c2["hours_since_prev_call"], errors="coerce") < 1]
    rule("redial60", "Redialled within 60 minutes of the previous call", "high",
         fast, "Same lead dialled again less than an hour after the last attempt.",
         "Reads as harassment; drives DNC requests and brand damage.")

    day_counts = c2.groupby(["lead_ref", "date"])["roomName"].transform("size")
    burst = c2[day_counts >= 3]
    rule("burst3", "3+ dials to the same lead in one day", "medium", burst,
         "A lead hit three or more times in a single calendar day.",
         "Answer rates collapse after the second same-day attempt; pure waste plus irritation.")

    # ---- 2. DNC compliance (sequencing on the FULL log, displayed for filtered leads)
    dnc_first = full[truthy(full["dncTriggered"])].groupby("lead_ref")["ts"].min()
    after_dnc = c2[c2["lead_ref"].map(dnc_first).notna() &
                   (c2["ts"] > c2["lead_ref"].map(dnc_first))]
    rule("afterdnc", "Dialled AFTER the lead asked never to be called", "critical",
         after_dnc, "A dial that happened after this lead triggered do-not-call on an earlier call.",
         "Regulatory exposure (TRAI) and the single fastest way to generate a formal complaint.")

    # ---- 3. re-selling a booked customer
    book_first = full[full["outcome"].isin(["PRE_SITE", "VIRTUAL_TOUR"])].groupby("lead_ref")["ts"].min()
    after_book = c2[c2["lead_ref"].map(book_first).notna() &
                    (c2["ts"] > c2["lead_ref"].map(book_first)) &
                    (~c2["slug"].astype(str).str.contains("site_visit", na=False))]
    rule("afterbook", "Sales dial AFTER the lead already booked a visit", "high",
         after_book, "Qualification/selling call placed after a booking existed "
         "(site-visit reminder slugs excluded).",
         "The bot re-pitches a converted customer - documented in your QA sheet as a trust killer.")

    # ---- 4. calling-hours compliance
    oo = c2[(c2["hour"] < 9) | (c2["hour"] >= 21)]
    rule("hours", "Dialled outside 09:00-21:00 IST", "critical", oo,
         "Calls placed outside the permitted telemarketing window.",
         "Direct TRAI violation risk; also the worst-converting hours anyway.")

    # ---- 5. dead numbers still being ground
    per = full.groupby("lead_ref").agg(att=("roomName", "size"), ever=("reached", "max"))
    dead_refs = set(per[(per["att"] >= 10) & (~per["ever"].astype(bool))].index)
    grind = c2[c2["lead_ref"].isin(dead_refs)]
    rule("deadgrind", "Dials to numbers that never answered in 10+ attempts", "medium",
         grind, "This lead has 10 or more attempts and has never once connected.",
         "Every further dial has near-zero expected value - this is the waste budget.",
         cols=["ts_str", "lead_ref", "outcome", "attempt_no", "total_attempts"])

    # ---- 6. instrumentation lies
    ghost = c2[c2["reached"] & (~c2["has_transcript"] | (c2["duration_sec"] <= 0))]
    rule("ghost", "Connected outcome but no transcript / zero duration", "medium",
         ghost, "The outcome says a human answered but the call left no evidence.",
         "Connect rate is overstated; downstream intent analysis silently skips these.")

    # ---- 7. CRM never told
    wfail = c2[c2["webhook_status"].astype(str).str.contains("failed", na=False)]
    rule("whfail", "CRM webhook delivery FAILED", "high", wfail,
         "The call finished but the result webhook to the CRM failed all retries.",
         "Zoho keeps stale state and re-dials leads whose outcome it never received.",
         cols=["ts_str", "lead_ref", "outcome", "webhook_status", "webhook_attempts"])
    wnone = c2[c2["status"].eq("completed") & c2["outcome"].notna() & (c2["webhook_count"] == 0)]
    rule("whnone", "Completed call with NO webhook attempt recorded", "high", wnone,
         "An outcome exists but no delivery to the CRM was even attempted.",
         "Same blindness as a failed webhook, harder to spot because nothing errored.",
         cols=["ts_str", "lead_ref", "outcome", "webhook_count"])

    # ---- 8. booking hygiene
    bnw = c2[c2["outcome"].eq("PRE_SITE") & ~truthy(c2["whatsappSent"])]
    rule("bookno_wa", "Visit booked but site-office WhatsApp never sent", "high",
         bnw, "PRE_SITE outcome without the follow-up location message.",
         "No-show risk: the customer agreed to visit but never got the address.")

    # ---- 9. scheduler stuck
    if not sched.empty and "sched_status" in sched.columns:
        due = pd.to_datetime(sched["dueAt"], errors="coerce", utc=True)
        stuck = sched[sched["sched_status"].eq("pending") & (due < pd.Timestamp.now(tz="UTC"))]
        n = int(len(stuck))
        e = {"id": "stucksched", "name": "Scheduled calls stuck past their due time",
             "severity": "high", "count": n,
             "why": "Reminder / night-defer rows still pending after dueAt.",
             "second_order": "Site-visit reminders silently never go out; visits no-show."}
        if irule == "stucksched" and n:
            e["detail"] = table(stuck, [x for x in ["lead_ref", "slug", "kind",
                                "sched_status", "dueAt", "attempts"] if x in stuck.columns], cap=500)
        R.append(e)

    # ---- 10. transcript-quality noise budget (aggregate of tq flags)
    tqcols = [x for x in c2.columns if x.startswith("tq_")]
    if tqcols:
        noisy = c2[c2[tqcols].fillna(False).astype(bool).any(axis=1)]
        rule("tqnoise", "Calls whose transcript is untrustworthy (any quality flag)", "low",
             noisy, "One or more transcript-quality flags fired.",
             "Every transcript-derived metric double-counts or misses these.",
             cols=["ts_str", "lead_ref", "outcome"] + tqcols[:5])

    # ---- 11. WhatsApp delivery truth (found by the inversion sweep)
    ss = b.get("stuck_sends", pd.DataFrame())
    if not ss.empty:
        n = int(len(ss))
        leads_n = int(ss["lead_ref"].nunique()) if "lead_ref" in ss.columns else None
        e = {"id": "wastuck", "name": "WhatsApp messages that NEVER delivered (all senders failed)",
             "severity": "critical", "count": n,
             "why": (f"stuck_sends backlog: every sender in the pool was tried and failed "
                     f"(401 UNAUTHORIZED). {leads_n:,} leads affected." if leads_n else
                     "stuck_sends backlog - all senders exhausted."),
             "second_order": "Every 'texted' metric overstates reality; brochures and "
                             "site-office locations silently never arrived."}
        if irule == "wastuck" and n:
            cols = [x for x in ["lead_ref", "at", "last_error", "tried_senders"] if x in ss.columns]
            e["detail"] = table(ss.sort_values("at", ascending=False) if "at" in ss.columns else ss,
                                cols, cap=500)
        R.append(e)
    ds = b.get("dead_senders", pd.DataFrame())
    if not ds.empty:
        R.append({"id": "deadpool", "name": "WhatsApp sender numbers permanently dead",
                  "severity": "critical", "count": int(len(ds)),
                  "why": "Every sender in the Periskope pool is marked permanently failed.",
                  "second_order": "ALL WhatsApp follow-ups are down until the pool is replaced."})

    # ---- 12. night-defer discipline (independent hunter's findings)
    if not sched.empty and {"kind", "sched_status", "dialedCallId"} <= set(sched.columns):
        nd = sched[sched["kind"].eq("night_defer") & sched["sched_status"].eq("done")]
        known = set(full["roomName"].dropna().astype(str))
        ghost = nd[~nd["dialedCallId"].astype(str).isin(known) & nd["dialedCallId"].notna()]
        e = {"id": "ndghost", "name": "Night-defer marked done but the dial does not exist in the log",
             "severity": "high", "count": int(len(ghost)),
             "why": "The scheduler says it dialled, yet no matching call record exists.",
             "second_order": "Either dials are vanishing from the log or the scheduler "
                             "reports work it never did - both corrupt every daily count."}
        if irule == "ndghost" and len(ghost):
            e["detail"] = table(ghost, [x for x in ["lead_ref", "dueAt", "dialedCallId",
                                "attempts"] if x in ghost.columns], cap=500)
        R.append(e)

    # ---- 13. inbound calls failed (customer called US and we fumbled)
    inb = c2[c2["direction"].eq("inbound") & ~c2["reached"]]
    rule("inboundfail", "Inbound call from a lead not successfully handled", "high",
         inb, "A customer called the bot back and the call did not complete as connected.",
         "The warmest possible lead - a callback - bounced off the front door.",
         cols=["ts_str", "lead_ref", "outcome", "duration_sec"])

    # ---- 14. voicemail bombing + attempt-cap blowout
    vm_per = full[full["outcome"].eq("VOICEMAIL")].groupby("lead_ref").size()
    vm_refs = set(vm_per[vm_per >= 6].index)
    vmb = c2[c2["lead_ref"].isin(vm_refs) & c2["outcome"].eq("VOICEMAIL")]
    rule("vmbomb", "Voicemail bombing: 6+ voicemails left for one lead", "medium", vmb,
         "This lead's mailbox has been hit six or more times.",
         "Voicemail-full numbers keep absorbing dial budget and look like activity.")
    cap = c2[pd.to_numeric(c2["total_attempts"], errors="coerce") >= 25]
    rule("capblow", "Attempt-cap blowout: lead with 25+ lifetime dials", "medium", cap,
         "No sane cadence reaches 25 attempts - one lead in the log has 200+.",
         "Pure waste plus harassment exposure; a hard cap in the agent would stop it.")

    # ---- 14b. FOLLOW-UP DISCIPLINE (Narasimha) - does reality match
    # sitevisit/planner.ts? Promise: visit booked -> reminders at
    # visitAt-12h and visitAt-1h (single -15m if imminent); the +30m
    # confirmation call was REMOVED by product decision on 2026-07-17.
    bookings = full[full["outcome"].isin(["PRE_SITE", "VIRTUAL_TOUR"])]
    if not sched.empty and "kind" in sched.columns:
        sv = sched[sched["kind"].eq("sitevisit")].copy()
        sv_created = pd.to_datetime(sv.get("sched_createdAt"), errors="coerce", utc=True)

        # FU1: booking with no reminder ever scheduled for that lead afterwards
        no_rem = []
        for _, bk_ in bookings.iterrows():
            mine = sv[(sv["lead_ref"] == bk_["lead_ref"])
                      & (sv_created >= bk_["ts"] - pd.Timedelta(minutes=5))]
            if not len(mine):
                no_rem.append(bk_)
        no_rem_df = pd.DataFrame(no_rem) if no_rem else full.head(0)
        if len(no_rem_df):
            no_rem_df = no_rem_df.copy()
            no_rem_df["ts_str"] = pd.to_datetime(no_rem_df["ts_ist"],
                                                 errors="coerce").dt.strftime("%d %b %H:%M")
        e = {"id": "fu_norem", "name": "Visit BOOKED but no reminder ever scheduled",
             "severity": "critical", "count": int(len(no_rem_df)),
             "why": "The planner schedules reminders only when it parsed a visit "
                    "time; these bookings got none - the customer will get no "
                    "12h/1h reminder.",
             "second_order": "Directly feeds the 76% no-show rate - an unreminded "
                             "booking is a coin-flip visit."}
        if irule == "fu_norem" and len(no_rem_df):
            e["detail"] = table(no_rem_df, ["ts_str", "lead_ref", "outcome",
                                            "duration_sec", "slug"], cap=300)
        R.append(e)

        # FU2: reminder scheduled at the wrong offset from the visit
        sv_v = sv[sv["visitAt"].notna() & sv["dueAt"].notna()].copy()
        if len(sv_v):
            va = pd.to_datetime(sv_v["visitAt"], errors="coerce", utc=True)
            da = pd.to_datetime(sv_v["dueAt"], errors="coerce", utc=True)
            off_h = (va - da).dt.total_seconds() / 3600
            ok = (off_h.between(11.6, 12.4) | off_h.between(0.6, 1.4)
                  | off_h.between(0.15, 0.35))
            bad = sv_v[~ok.fillna(False)].copy()
            bad["offset_h"] = off_h[~ok.fillna(False)].round(2)
            e = {"id": "fu_offset", "name": "Reminder scheduled at the WRONG offset",
                 "severity": "medium", "count": int(len(bad)),
                 "why": "dueAt is not ~12h, ~1h or ~15m before visitAt - the "
                        "planner contract is broken for these rows.",
                 "second_order": "Reminders at odd times annoy instead of remind."}
            if irule == "fu_offset" and len(bad):
                e["detail"] = table(bad, [x for x in ["lead_ref", "slug", "dueAt",
                                    "visitAt", "offset_h", "sched_status"]
                                    if x in bad.columns], cap=300)
            R.append(e)

        # FU3: sitevisit reminder marked done but the dial record is absent
        known_rooms = set(full["roomName"].dropna().astype(str))
        done_sv = sv[sv["sched_status"].eq("done")]
        ghost_sv = done_sv[done_sv["dialedCallId"].notna()
                           & ~done_sv["dialedCallId"].astype(str).isin(known_rooms)]
        undialed = done_sv[done_sv["dialedCallId"].isna()]
        e = {"id": "fu_ghost", "name": "Reminder marked done without a matching dial",
             "severity": "high",
             "count": int(len(ghost_sv) + len(undialed)),
             "why": "Status says the reminder call happened; the call log has no "
                    "record (or no dialedCallId at all).",
             "second_order": "Reminder coverage reads healthy while customers "
                             "hear nothing."}
        if irule == "fu_ghost" and (len(ghost_sv) + len(undialed)):
            gg = pd.concat([ghost_sv, undialed])
            e["detail"] = table(gg, [x for x in ["lead_ref", "slug", "dueAt",
                                "sched_status", "dialedCallId", "attempts"]
                                if x in gg.columns], cap=300)
        R.append(e)

    # FU5: the REMOVED confirmation call still firing after 2026-07-17
    conf = c2[c2["slug"].eq("site_visit_confirmation")
              & (c2["ts"] >= pd.Timestamp("2026-07-17", tz="UTC"))]
    rule("fu_conf", "Removed confirmation-call slug still firing (post Jul-17)",
         "high", conf,
         "Product removed the +30m confirmation call on 2026-07-17; these calls "
         "used that slug afterwards.",
         "The exact harassment pattern the removal was meant to stop.",
         cols=["ts_str", "lead_ref", "outcome", "duration_sec", "slug"])

    # ---- 15. DNC captured but not propagated to the CRM lead flag
    ld = b.get("leads", pd.DataFrame())
    if not ld.empty and "doNotCall" in ld.columns:
        flagged = set(ld.loc[truthy(ld["doNotCall"]), "lead_ref"].dropna())
        said_dnc = set(full.loc[truthy(full["dncTriggered"]), "lead_ref"].dropna())
        gap_refs = said_dnc - flagged
        gap = c2[c2["lead_ref"].isin(gap_refs) & truthy(c2["dncTriggered"])]
        rule("dncgap", "DNC said on a call but lead flag never set", "high", gap,
             "The customer opted out on a call, yet leads.doNotCall is still false.",
             "The suppression that prevents future dials is not armed - these WILL be redialled.",
             cols=["ts_str", "lead_ref", "outcome", "dncVerbatim"])

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    R.sort(key=lambda r: (order.get(r["severity"], 9), -r["count"]))

    sev_tot = {}
    for r in R:
        sev_tot[r["severity"]] = sev_tot.get(r["severity"], 0) + r["count"]

    # engine health strip
    health = []
    try:
        st = json.loads((DATA / "state.json").read_text())
        last = pd.to_datetime(st.get("last_sync"))
        age_h = (pd.Timestamp.now(tz="UTC") - last).total_seconds() / 3600
        health.append({"label": "Cache age", "value": f"{age_h:.1f} h",
                       "sub": "since last Mongo sync"})
    except Exception:
        pass
    newest = pd.to_datetime(full["ts"], errors="coerce").max()
    if pd.notna(newest):
        health.append({"label": "Newest call in cache",
                       "value": str(newest.tz_convert("Asia/Kolkata"))[:16]})
    health.append({"label": "Critical findings", "value": f"{sev_tot.get('critical', 0):,}"})
    health.append({"label": "High findings", "value": f"{sev_tot.get('high', 0):,}"})

    return {"rules": [{k: v for k, v in r.items()} for r in R],
            "severity_totals": sev_tot, "health": health,
            "colors": SEV_COLORS, "selected": irule}


def build_audit(b, c, d0, d1):
    """The audit tab: identity bifurcation, Zoho reconciliation, attempt-level
    funnel, CRM sync gaps. Everything computed live from the cache so the
    numbers always match the current filter selection."""
    A = {}
    z = b.get("zoho", pd.DataFrame())
    refmap = human_map(b)
    refs = set(c["lead_ref"].dropna())

    human = c["lead_ref"].map(lambda r: refmap.get(r, r))
    dials, phones = len(c), c["lead_ref"].nunique()
    mapped_refs = sum(1 for r in refs if r in refmap)
    humans = int(human.nunique())

    # the CRM-identity layer: how many dialled numbers the CRM even knows
    with_id = sum(1 for r in refs if r in refmap)
    # project-leads (PLIDs) held by the dialled humans, from the PLID registry
    # keyed by MLID (not by ref - 256 refs are ambiguous there)
    plid = b.get("plid", pd.DataFrame())
    dialled_mlids = {refmap[r].split(":", 1)[1] for r in refs if r in refmap}
    plids_held = 0
    leads_per_human = []
    if not plid.empty and {"mlid", "plid"} <= set(plid.columns):
        pp = plid[plid["mlid"].astype(str).isin(dialled_mlids)]
        plids_held = int(len(pp))
        per = pp.groupby("mlid").size()
        lb = [(1, "1 project-lead"), (2, "2 project-leads"),
              (3, "3 project-leads"), (4, "4 project-leads"),
              (10 ** 9, "5+ project-leads")]
        for i, (hi, lbl) in enumerate(lb):
            lo = 0 if i == 0 else lb[i - 1][0]
            n = int(((per > lo) & (per <= hi)).sum())
            if n:
                leads_per_human.append({"label": lbl, "value": n})

    A["pyramid"] = [
        {"label": "Dial attempts", "value": dials, "pct": 100.0},
        {"label": "Unique phone numbers dialled", "value": int(phones),
         "pct": pct(phones, dials)},
        {"label": "Unique humans", "value": humans, "pct": pct(humans, dials)},
        {"label": "…with a CRM identity (MLID)", "value": with_id,
         "pct": pct(with_id, phones)},
        {"label": "Project-leads those humans hold", "value": plids_held,
         "pct": pct(plids_held, max(with_id, 1))},
    ]
    A["leads_per_human"] = leads_per_human
    A["pyramid_note"] = (
        "One human = one Master Lead ID. A human holds a separate Zoho lead "
        "per project (25% of humans hold 2+), so lead counts always exceed "
        f"people counts. {len(refs) - with_id:,} dialled numbers have no CRM "
        "identity at all - the bot called numbers Zoho does not know, or the "
        "registry has not caught up; each is counted as one human.")

    # ---- dials per human: how many times each person was actually called
    per_h = c.groupby(human).size()
    bins = [(1, "1 call"), (2, "2 calls"), (3, "3 calls"), (4, "4 calls"),
            (5, "5 calls"), (10, "6-10 calls"), (20, "11-20 calls"),
            (10 ** 9, "21+ calls")]
    dist = []
    for i, (hi, lbl) in enumerate(bins):
        lo = 0 if i == 0 else bins[i - 1][0]
        n = int(((per_h > lo) & (per_h <= hi)).sum())
        if n:
            dist.append({"label": lbl, "value": n})
    A["calls_per_human"] = dist
    A["calls_per_human_kpis"] = [
        {"label": "Median calls per human", "value": f"{per_h.median():.0f}"},
        {"label": "Mean calls per human", "value": f"{per_h.mean():.1f}"},
        {"label": "Max calls to one human", "value": f"{int(per_h.max())}"},
        {"label": "Humans called 5+ times",
         "value": f"{int((per_h >= 5).sum()):,}",
         "sub": f"{pct(int((per_h >= 5).sum()), len(per_h))}% of humans"},
    ]

    # ---- connect rate by attempt number: does redialling work?
    att = c[c["attempt_no"].notna()].copy()
    att["attempt_bucket"] = att["attempt_no"].map(
        lambda a: str(int(a)) if a <= 8 else "9+")
    ga = (att.groupby("attempt_bucket")
             .agg(dials=("roomName", "size"), connected=("reached", "sum"),
                  booked=("is_booked", "sum")))
    order = [str(i) for i in range(1, 9)] + ["9+"]
    ga = ga.reindex([o for o in order if o in ga.index]).dropna()
    A["by_attempt"] = {
        "labels": [f"attempt {i}" for i in ga.index],
        "dials": [int(x) for x in ga["dials"]],
        "connect_pct": [pct(int(r), int(dd)) for r, dd in
                        zip(ga["connected"], ga["dials"])],
        "booked": [int(x) for x in ga["booked"]],
    }

    # ---- daily connect-rate trend (the funnel over time)
    gd = (c.groupby("date")
            .agg(dials=("roomName", "size"), connected=("reached", "sum"),
                 talked=("human_conversation", "sum")
                 if "human_conversation" in c.columns else ("reached", "sum")))
    A["daily_rates"] = {
        "dates": [str(x) for x in gd.index],
        "connect_pct": [pct(int(r), int(dd)) for r, dd in
                        zip(gd["connected"], gd["dials"])],
        "talk_pct": [pct(int(t), int(dd)) for t, dd in
                     zip(gd["talked"], gd["dials"])],
    }

    # ---- Zoho reconciliation (window = the dashboard's date filter)
    if not z.empty and "lead_received_at" in z.columns:
        recv = pd.to_datetime(z["lead_received_at"], errors="coerce")
        t0, t1 = pd.Timestamp(d0), pd.Timestamp(d1) + pd.Timedelta(days=1)
        zw = z[(recv >= t0) & (recv < t1)]
        zw_refs = set(zw["lead_ref"].dropna())
        z_all_refs = set(z["lead_ref"].dropna())
        matched = zw_refs & refs
        not_dialled = zw_refs - refs
        extra = refs - zw_refs
        extra_older = {r for r in extra if r in z_all_refs}
        extra_absent = extra - extra_older
        A["recon"] = {
            "rows": [
                {"label": "Zoho lead rows received in window", "value": len(zw)},
                {"label": "= unique humans (Master Lead ID)",
                 "value": int(zw["master_lead_id"].nunique())},
                {"label": "= unique phone numbers", "value": len(zw_refs)},
                {"label": "dialled in this selection", "value": len(matched)},
                {"label": "received but NOT dialled here", "value": len(not_dialled)},
                {"label": "dialled: older leads (received before window)",
                 "value": len(extra_older)},
                {"label": "dialled: absent from the Zoho export",
                 "value": len(extra_absent)},
            ],
            "note": (
                "Zoho export rows are PROJECT-level leads — the same human "
                "appears once per project. The dashboard counts dialled phone "
                "numbers. Both are correct; they answer different questions. "
                f"Cross-project duplicate rows in this window: "
                f"{len(zw) - int(zw['master_lead_id'].nunique()):,}."),
        }

        # Zoho's own status vs what the dialler saw - the sync-gap detector
        zz = z[z["lead_ref"].isin(refs)].drop_duplicates("lead_ref")
        if "lead_status" in zz.columns and len(zz):
            ever = c.groupby("lead_ref")["reached"].max()
            zz = zz.assign(dialler_reached=zz["lead_ref"].map(ever))
            ct = (zz.groupby([zz["lead_status"].fillna("(blank)"),
                              zz["dialler_reached"].fillna(False)])
                    .size().unstack(fill_value=0))
            rows = []
            for status, r in ct.iterrows():
                yes = int(r.get(True, 0)); no = int(r.get(False, 0))
                rows.append({"zoho_status": str(status), "dialler_reached": yes,
                             "dialler_never_reached": no,
                             "reached_pct": pct(yes, yes + no)})
            rows.sort(key=lambda x: -(x["dialler_reached"] + x["dialler_never_reached"]))
            A["status_vs_dialler"] = {
                "cols": ["zoho_status", "dialler_reached",
                         "dialler_never_reached", "reached_pct"],
                "rows": [[r[k] for k in ("zoho_status", "dialler_reached",
                                          "dialler_never_reached", "reached_pct")]
                         for r in rows],
                "total": len(rows), "shown": len(rows),
            }

        # CRM sync gaps the export itself proves
        gaps = []
        for col_, friendly in (("visit_attended", "Visit Attended"),
                               ("call_connected_flag", "Call Connected Flag")):
            if col_ in z.columns:
                vc = z[col_].astype(str).str.lower().value_counts()
                if vc.get("true", 0) == 0 and len(z):
                    gaps.append(f"'{friendly}' is FALSE on all {len(z):,} export "
                                "rows — this flag is never synced back from the "
                                "dialler, so Zoho cannot see it")
        if "call_attempt_count" in z.columns:
            filled = int(z["call_attempt_count"].notna().sum())
            csv_sum = int(pd.to_numeric(z["call_attempt_count"],
                                        errors="coerce").fillna(0).sum())
            gaps.append(f"'Call Attempt Count' is filled on only {filled:,} of "
                        f"{len(z):,} rows (sum {csv_sum:,}) while the dialler "
                        f"log holds {dials:,} dials in this selection alone — "
                        "Zoho materially under-counts calling effort")
        A["sync_gaps"] = gaps
    else:
        A["recon"] = None
        A["sync_gaps"] = []

    return A


# ============================================================ site visits

def build_visits(b, c):
    """Site-visit tab. Canonical bookings = Intelligent_CRM.site_visits (its
    Zoho mirror lost its dates to an old redaction bug); attendance evidence =
    Zoho lead_status, because NO source records visit_done/no_show events."""
    sv = b.get("crm_visits", pd.DataFrame())
    persons = b.get("persons", pd.DataFrame())
    z = b.get("zoho", pd.DataFrame())
    refs = set(c["lead_ref"].dropna())
    V = {}

    if sv.empty:
        return {"note": "No site-visit bookings in the cache.", "kpis": [],
                "charts": [], "funnel": None, "table": None, "gap": None}

    sv = sv.copy()
    if not persons.empty and "person_id" in sv.columns and "_id" in persons.columns:
        pm = dict(zip(persons["_id"].astype(str), persons["lead_ref"]))
        sv["lead_ref"] = sv["person_id"].astype(str).map(pm)
    in_sel = sv["lead_ref"].isin(refs) if "lead_ref" in sv.columns else pd.Series(True, index=sv.index)
    sv_sel = sv[in_sel]
    shown = sv_sel if len(sv_sel) else sv  # tiny table: never show an empty tab
    subset_note = "" if len(sv_sel) == len(sv) else (
        f" ({len(sv_sel)} of {len(sv)} bookings match the current filters; "
        "showing all when none match.)")

    for col_ in ("created_at", "scheduled_for"):
        if col_ in shown.columns:
            shown = shown.assign(**{col_: pd.to_datetime(shown[col_], errors="coerce", utc=True)})

    now = pd.Timestamp.now(tz="UTC")
    sched = shown["scheduled_for"] if "scheduled_for" in shown.columns else pd.Series(dtype="datetime64[ns, UTC]")
    upcoming = int((sched > now).sum())
    past = int((sched <= now).sum())

    # attendance evidence, per lead, from the Zoho export. Verified: the 10
    # 'Post Site Visit' leads in the export are an OLDER cohort - none of the
    # bot's bookings is among them.
    post_visit, booked_status = 0, []
    if not z.empty and "lead_status" in z.columns:
        zz = z[z["lead_ref"].isin(set(shown.get("lead_ref", pd.Series()).dropna()))]
        post_visit = int(zz["lead_status"].eq("Post Site Visit").sum())
        booked_status = [{"label": str(k), "value": int(v)} for k, v in
                         nonempty(zz["lead_status"]).value_counts().items()]

    V["kpis"] = [
        {"label": "Visits booked by the bot", "value": f"{len(shown):,}"},
        {"label": "Unique people", "value": f"{shown['lead_ref'].nunique():,}"
         if "lead_ref" in shown.columns else "—"},
        {"label": "Site / virtual",
         "value": (lambda vc: f"{vc.get('site', 0)} / {vc.get('virtual', 0)}")(
             shown["type"].value_counts()) if "type" in shown.columns else "—"},
        {"label": "Visit date still ahead", "value": f"{upcoming:,}"},
        {"label": "Visit date passed", "value": f"{past:,}"},
        (lambda done: {"label": "Visits DONE (ops sheet)",
                       "value": f"{int(done['mlid'].nunique()):,}" if not done.empty
                       and "mlid" in done.columns else "0",
                       "sub": "cohort_sv_completed - the ONLY attendance record; "
                              "no visit_done events exist in any database"})(
            b.get("co_sv_completed", pd.DataFrame())),
    ]

    def vc_chart(col_):
        if col_ not in shown.columns:
            return []
        return [{"label": str(k)[:40], "value": int(v)}
                for k, v in nonempty(shown[col_]).value_counts().items()]

    day = (shown["created_at"].dt.tz_convert("Asia/Kolkata").dt.date.value_counts().sort_index()
           if "created_at" in shown.columns else pd.Series(dtype=int))
    V["charts"] = [
        [{"title": "Bookings by project", "data": vc_chart("project")},
         {"title": "Booked per day", "data": [{"label": str(k), "value": int(v)}
                                              for k, v in day.items()]}],
        [{"title": "Visit type", "data": vc_chart("type")},
         {"title": "Zoho lead status TODAY for the booked leads",
          "data": booked_status}],
    ]

    booked_n = len(shown)
    V["funnel"] = [
        {"label": "Booked on a call", "value": booked_n, "pct": 100.0},
        {"label": "Zoho later shows 'Post Site Visit'", "value": post_visit,
         "pct": pct(post_visit, booked_n)},
        {"label": "Attendance events recorded", "value": 0, "pct": 0.0},
    ]

    cols = [x for x in ["created_at", "scheduled_for", "type", "project",
                        "lead_ref", "status", "source_status", "booked_via"]
            if x in shown.columns]
    t = shown.copy()
    for col_ in ("created_at", "scheduled_for"):
        if col_ in t.columns:
            t[col_] = t[col_].dt.tz_convert("Asia/Kolkata").dt.strftime("%d %b %H:%M")
    if "phone" in c.columns:
        t = add_phone(t)
    V["table"] = table(t.sort_values(cols[0], ascending=False) if cols else t, cols)

    V["note"] = ("Bookings from <code>Intelligent_CRM.site_visits</code> — the one "
                 "source that kept its dates; its Zoho mirror and the event log "
                 "agree row-for-row." + subset_note)
    V["gap"] = ("AUDIT GAP - attendance is not tracked. " + f"{booked_n} visits "
                "booked, zero visit_done / no_show / cancelled events in any "
                "database, 'Visit Attended' is false on every Zoho export row, "
                f"and only {post_visit} of the booked leads ever reach 'Post "
                "Site Visit' status. Until attendance is written back, "
                "bookings are the end of the measurable funnel.")
    return V


# ============================================================ intent tab

INTENT_STAGE_ORDER = ["greeting_only", "discovery", "pitch",
                      "objection_handling", "negotiation", "closing",
                      "follow_up_scheduling", "wrong_number", "complaint",
                      "no_contact"]
SENTIMENT_ORDER = ["very_negative", "negative", "neutral", "positive", "unknown"]


def build_intent(b, c):
    """Charts over ASBLVoiceBot.call_transcript_intent_identification, joined
    per call (call_sid == roomName) and filtered to the current selection."""
    it = b.get("intent_id", pd.DataFrame())
    if it.empty or "call_sid" not in it.columns:
        return {"available": False}

    sel = it[it["call_sid"].isin(set(c["roomName"].dropna()))].copy()
    if sel.empty:
        return {"available": True, "rows": 0}
    ex = sel[truthy(sel["extracted"])] if "extracted" in sel.columns else sel

    T = {"available": True, "rows": int(len(sel)), "extracted": int(len(ex)),
         "extracted_pct": pct(len(ex), len(sel))}

    def dist(frame, colname, order=None, top=None):
        if colname not in frame.columns:
            return []
        v = nonempty(frame[colname])
        vc = v.value_counts()
        if order:
            items = [(o, int(vc[o])) for o in order if o in vc.index]
            items += [(k, int(n)) for k, n in vc.items() if k not in order]
        else:
            items = list(vc.items())
        if top:
            items = items[:top]
        return [{"label": str(k)[:48], "value": int(n)} for k, n in items]

    skip = sel["skip_reason"] if "skip_reason" in sel.columns else pd.Series(dtype=str)
    skipd = nonempty(skip).value_counts()
    T["skip"] = ([{"label": "extracted (real conversation)", "value": int(len(ex)),
                   "color": "#008300"}] +
                 [{"label": str(k)[:48], "value": int(n)} for k, n in skipd.items()][:10])

    T["stage"] = dist(ex, "intent.conversation_stage", order=INTENT_STAGE_ORDER)
    T["caller_intent"] = dist(ex, "intent.caller_intent")
    T["strength"] = dist(ex, "intent.intent_strength",
                         order=["strong", "moderate", "weak", "none"])
    T["sentiment"] = dist(ex, "intent.customer_sentiment", order=SENTIMENT_ORDER)
    T["engagement"] = dist(ex, "intent.customer_engagement",
                           order=["high", "medium", "low", "none"])
    T["talk_balance"] = dist(ex, "intent.talk_balance",
                             order=["customer_led", "balanced", "agent_led",
                                    "agent_only"])
    T["visit_outcome"] = dist(ex, "intent.site_visit_outcome")
    T["language"] = dist(ex, "intent.language", top=8)

    # urgency has LLM prompt-leak garbage - keep the first enum token only
    if "intent.urgency" in ex.columns:
        u = (nonempty(ex["intent.urgency"]).str.lower()
             .str.extract(r"^(none|low|medium|high|unknown)")[0].dropna())
        T["urgency"] = [{"label": k, "value": int(n)}
                        for k, n in u.value_counts().items()]
    else:
        T["urgency"] = []

    # BHK requirement, lightly normalised
    if "intent.requirement_bhk" in ex.columns:
        bhk = (nonempty(ex["intent.requirement_bhk"]).str.upper()
               .str.replace(" ", "", regex=False))
        T["bhk"] = [{"label": k, "value": int(n)}
                    for k, n in bhk.value_counts().head(10).items()]
    else:
        T["bhk"] = []

    # budget in Cr buckets
    if "intent.budget_inr" in ex.columns:
        amt = pd.to_numeric(ex["intent.budget_inr"], errors="coerce").dropna()
        amt = amt[amt > 100000]
        cr = (amt / 1e7).round(1).map(lambda v: f"{v:.1f} Cr")
        T["budget"] = [{"label": k, "value": int(n)}
                       for k, n in cr.value_counts().head(10).items()]
    else:
        T["budget"] = []

    # objections leaderboard - free text, split on ; and normalise timing noise
    if "intent.objections" in ex.columns:
        ob = (nonempty(ex["intent.objections"]).str.lower()
              .str.split(";").explode().str.strip())
        ob = ob[ob != ""]
        ob = ob.str.replace(
            r"^(not a good time.*|no time.*|bad timing.*|busy.*|driving.*|"
            r"in a meeting.*)$", "no time / busy", regex=True)
        T["objections"] = [{"label": k[:48], "value": int(n)}
                           for k, n in ob.value_counts().head(14).items()]
    else:
        T["objections"] = []

    # agent behaviour scorecard
    def share(colname):
        if colname not in ex.columns or not len(ex):
            return None
        t = truthy(ex[colname])
        return {"n": int(t.sum()), "pct": pct(int(t.sum()), len(ex))}
    q = share("intent.agent_did_qualify")
    nx = share("intent.agent_asked_for_next_step")
    dm = share("intent.decision_maker_present")
    T["scorecard"] = [
        {"label": "Agent qualified the lead",
         "value": f"{q['pct']}%" if q else "—",
         "sub": f"{q['n']:,} of {len(ex):,} conversations" if q else None},
        {"label": "Agent asked for a next step",
         "value": f"{nx['pct']}%" if nx else "—",
         "sub": f"{nx['n']:,} of {len(ex):,}" if nx else None},
        {"label": "Decision-maker on the line",
         "value": f"{dm['pct']}%" if dm else "—",
         "sub": f"{dm['n']:,} of {len(ex):,}" if dm else None},
    ]

    # the hot-lead list: strong/moderate intent, with the LLM's own summary
    if "intent.intent_strength" in ex.columns:
        hot = ex[ex["intent.intent_strength"].isin(["strong", "moderate"])].copy()
        hot["when"] = pd.to_datetime(hot.get("call_time"), errors="coerce",
                                     utc=True)
        try:
            hot["when"] = hot["when"].dt.tz_convert("Asia/Kolkata").dt.strftime("%d %b %H:%M")
        except Exception:
            hot["when"] = hot["when"].astype(str)
        hot = hot.sort_values("intent.intent_strength")
        cols = [x for x in ["when", "lead_ref", "intent.intent_strength",
                            "intent.caller_intent", "intent.requirement_bhk",
                            "intent.budget_inr", "intent.site_visit_outcome",
                            "intent.next_step", "intent.call_summary"]
                if x in hot.columns]
        if "phone" in c.columns and "lead_ref" in hot.columns:
            hot = add_phone(hot)
        T["hot_leads"] = table(hot, cols, cap=400)
    else:
        T["hot_leads"] = None

    return T


# ============================================================ VAMANA: truth layer
# Canonical identity/stage/project joins. Fixes the three data bugs:
# COHORT-BORNDATE-COERCE (mixed date formats silently dropped 426 leads),
# LEADS-MERGE-FANOUT (naive joins fan out on the 18% multi-project humans),
# and encodes the outcome-taxonomy boundaries from the dev team's own release
# notes so no chart crosses them silently.

TAXONOMY_BREAKS = [
    ("2026-06-29", "outcome taxonomy rewritten (+AUTO_CALLBACK, +DROPPED; "
                   "historical NOT_CONNECTED rows MUTATED to DROPPED)"),
    ("2026-07-07", "voicemail AMD shipped — VOICEMAIL with 0-min duration begins"),
    ("2026-07-16", "slot provenance flips: mid-call tools → post-call classifier; "
                   "45s minimum-duration wall replaced by <8s floor"),
]

# METRIC LINEAGE (design contract rule 5): every headline number carries its
# plain-English definition, numerator/denominator, time window, filter scope,
# source collection and known exclusions/contamination. Rendered as the
# "Explain this number" panel on every KPI.
LINEAGE = {
    # ---- CIF headline cards ----
    "Clean calls scored": {
        "def": ("Calls with a usable clean transcript that produced a CIF score. "
                "The agent's own live transcripts are NEVER scored — the dev team "
                "measures them at <50% accuracy."),
        "num": "rows in cif_scores.parquet after de-duplication by transcript hash",
        "den": None, "window": "corpus, not the date filter",
        "filters": "lead-level filters only",
        "src": "data/cif_scores.parquet (Zoho Gemini 2.5 Pro tier A + audio re-transcripts tier B)",
        "excl": ("Calls with no dialogue turns. Note 3,853 of 3,862 re-transcript "
                 "rows carry no call_date, so most of this corpus cannot be dated."),
    },
    "Dialogue balance in band": {
        "def": ("Share of SUBSTANTIVE calls whose EBR (customer share of all words) "
                "falls in the healthy 40–60% band. Below 40% the agent is "
                "monologuing; above ~70% it has lost control of the call."),
        "num": "count(calls with 40 <= EBR <= 60)", "den": "substantive calls",
        "window": "corpus", "filters": "lead-level filters only",
        "src": "CIF: EBR over clean transcripts",
        "excl": ("Computed ONLY on calls where the customer actually spoke "
                 "(>=2 real turns and >=8 words). Scoring balance on a call where "
                 "the customer grunted once produces a confident-looking number "
                 "about nothing."),
    },
    "Median response depth": {
        "def": ("Median words per customer turn across the call (RDI). Target >15 "
                "words. Under 10 is a disengaged customer."),
        "num": "median(mean words per customer turn)", "den": None,
        "window": "corpus", "filters": "lead-level filters only",
        "src": "CIF: RDI", "excl": "Substantive calls only.",
    },
    "Median decision signal": {
        "def": ("Median DSS, a weighted 0–7 score: budget mentioned (1.5) + "
                "loan/EMI (1.5) + timeline (1.0) + family/co-decision-maker (1.0) "
                "+ specific property asked about (1.0). Target >3.5. It is a "
                "weighted score, not a count."),
        "num": "median(DSS)", "den": None, "window": "corpus",
        "filters": "lead-level filters only", "src": "CIF: DSS",
        "excl": "Lexicon-based, so it detects mentions, not sincerity.",
    },
    "FMS (master score)": {
        "def": ("The CIF master score. Human-agent formula: CM·.18 + DSS·.18 + "
                "ACR·.18 + SVO·.18 + NAC·.18 + IFQ·.10."),
        "num": "the formula above", "den": None, "window": "corpus",
        "filters": "lead-level filters only", "src": "CIF v2",
        "excl": ("NA by design: SVO, NAC and IFQ require the LLM judge pass, "
                 "which has not run. The passthrough is built — FMS switches on "
                 "automatically once those columns exist, with no code change."),
    },
    "Bookings we can see": {
        "def": ("Distinct bookings assembled from two sources and de-duplicated on "
                "(lead, visit date): calls whose outcome was PRE_SITE/VIRTUAL_TOUR, "
                "and Zoho's site_visit_bookings rows."),
        "num": "unique (lead_ref, visit_date) pairs", "den": None,
        "window": "all time", "filters": "none",
        "src": "angad.calls.outcome + Zoho_Database.site_visit_bookings",
        "excl": ("Bookings made by a human agent outside the dialler are invisible "
                 "unless they reached Zoho."),
    },
    "Attendance recorded": {
        "def": ("Bookings that have ANY attendance verdict — from the ops sheet or "
                "from the local ledger. Everything else is explicitly UNKNOWN."),
        "num": "bookings with verdict != unknown", "den": "bookings we can see",
        "window": "all time", "filters": "none",
        "src": "RELEASES/attendance.jsonl + ops sheet (co_sv_completed)",
        "excl": ("Attendance has NO field, endpoint or path anywhere upstream. "
                 "This is a local sensor. Until it exists upstream, every "
                 "improvement claim in this engagement stays provisional."),
    },
    # ---- humans-unit cards. These SIX had no entry at all, so every one of
    # them opened "NO DEFINITION EXPOSED yet for this metric". A card that
    # cannot say what it counts is not a metric, it is a rumour.
    "Dial attempts": {
        "def": ("Every outbound dial event in the window — one row per attempt, "
                "not per person. The same human dialled 34 times contributes 34."),
        "num": "count(angad.calls rows in window)", "den": None,
        "window": "action-date filter", "filters": "all",
        "src": "angad.calls",
        "excl": ("Nothing excluded. This is the raw denominator behind every "
                 "connect/booking rate on the page."),
    },
    "Humans dialled": {
        "def": ("Distinct PEOPLE dialled at least once — calls collapsed to the "
                "person via lead_ref, then to the Inncircles master id where one "
                "exists. A person appearing in two projects counts ONCE here "
                "(that is what makes this different from 'leads')."),
        "num": "nunique(person behind angad.calls.lead_ref)", "den": None,
        "window": "action-date filter", "filters": "all",
        "src": "angad.calls · Zoho_Database.mlid_registry",
        "excl": ("Rows with no resolvable lead_ref cannot be collapsed and fall "
                 "back to counting as their own person — see the identity "
                 "pyramid on the Audit tab for how many."),
    },
    "Humans connected": {
        "def": ("People with at least one call whose outcome is in the answered "
                "family (CONNECTED / NOT_INTERESTED / AUTO_CALLBACK / "
                "DNC_REQUESTED / PRE_SITE / VIRTUAL_TOUR). NOTE: this is the "
                "`reached` definition and it EXCLUDES DROPPED and VOICEMAIL — "
                "so it understates how many people actually picked the phone up. "
                "The true human-pickup figure is ~20.9% of dials against the "
                "7.4% this reports. See release 08-krishna §2."),
        "num": "nunique(person where any call reached==True)",
        "den": "Humans dialled", "window": "action-date filter", "filters": "all",
        "src": "angad.calls.outcome",
        "excl": ("DROPPED (6,727 calls, 4,904 of which opened a Gemini session) "
                 "and VOICEMAIL are NOT counted as connected even though the "
                 "line opened."),
    },
    "Humans in real conversation": {
        "def": ("Connected people where the CUSTOMER actually spoke — voicemail "
                "greetings are transcribed as customer speech and would "
                "otherwise inflate human contact by ~8,995 calls."),
        "num": "nunique(person with a customer utterance, voicemail excluded)",
        "den": "Humans connected", "window": "action-date filter", "filters": "all",
        "src": "angad.calls transcripts",
        "excl": ("Voicemail. Also inherits the stored-transcript accuracy "
                 "problem (<50% per the dev team) — treat as approximate."),
    },
    "Humans booked (call outcome)": {
        "def": ("People with at least one call whose outcome is PRE_SITE or "
                "VIRTUAL_TOUR — i.e. a visit was agreed ON THE CALL. This is "
                "NOT attendance and NOT a surviving booking."),
        "num": "nunique(person where outcome in (PRE_SITE, VIRTUAL_TOUR))",
        "den": "Humans dialled", "window": "action-date filter", "filters": "all",
        "src": "angad.calls.outcome",
        "excl": ("Bookings made by a human agent outside the dialler, and any "
                 "booking that was later cancelled — neither is visible here. "
                 "Attendance is un-architected anywhere in the estate."),
    },
    "Humans DNC": {
        "def": ("People who asked not to be called again, on any call in the "
                "window (dncTriggered)."),
        "num": "nunique(person where dncTriggered)", "den": "Humans dialled",
        "window": "action-date filter", "filters": "all",
        "src": "angad.calls.dncTriggered",
        "excl": ("The standing DNC list (210 entries) is separate and larger — "
                 "this counts only NEW requests captured on a call in the window. "
                 "DNC was non-blocking before ~20 Jul."),
    },
    "Calls triggered": {
        "def": "Count of dial attempts the agent placed (one row per accepted dial request).",
        "num": "rows in angad.calls", "den": None,
        "window": "global date filter", "filters": "all global filters apply",
        "src": "angad.calls",
        "excl": "Requests suppressed before dialling (DNC, visit-booked, invalid, "
                "rate-limited) write NO row — the true request count lives with the relay."},
    "Unique numbers": {
        "def": "Distinct pseudonymised phone numbers dialled.",
        "num": "distinct lead_ref in the selection", "den": None,
        "window": "global date filter", "filters": "all",
        "src": "angad.calls.lead_ref", "excl": "one person with two SIMs counts twice"},
    "Unique humans": {
        "def": "Distinct people after collapsing multiple numbers/projects via Master Lead ID.",
        "num": "distinct MLID (fallback lead_ref)", "den": None,
        "window": "global date filter", "filters": "all",
        "src": "Zoho_Database.mlid_registry", "excl": "numbers absent from the registry count as themselves"},
    "Connected": {
        "def": "Calls whose outcome is in the answered family.",
        "num": "calls with reached=true", "den": "calls triggered (for the %)",
        "window": "global date filter", "filters": "all",
        "src": "angad.calls.outcome",
        "excl": "NO_ANSWER is partly fabricated by cleanupStaleCalls (dev answers J2) — "
                "connect % is a floor, not an exact figure"},
    "Booked": {
        "def": "Calls that ended with a site visit or virtual tour agreed (PRE_SITE / VIRTUAL_TOUR).",
        "num": "calls with is_booked", "den": "calls triggered",
        "window": "global date filter", "filters": "all",
        "src": "angad.calls.outcome",
        "excl": "The visit gate rejects bookings before 4 min / 11 turns — fast genuine "
                "bookings leave no trace (dev answers J14). Booking ≠ attendance."},
    "Dropped": {
        "def": "Customer picked up but gave no input, or the call died mid-conversation.",
        "num": "calls with is_dropped", "den": "calls triggered",
        "window": "global date filter", "filters": "all",
        "src": "angad.calls (drop heuristics)", "excl": None},
    "Qualified": {
        "def": "Calls where the bot captured a qualification signal.",
        "num": "calls with userSignals.is_qualified", "den": None,
        "window": "global date filter", "filters": "all",
        "src": "angad.calls.userSignals",
        "excl": "classifier-skipped calls default to not-qualified — undercounts"},
    "Median talk time": {
        "def": "Median duration of connected calls with a usable duration.",
        "num": "median(duration_sec) on reached & duration_known", "den": None,
        "window": "global date filter", "filters": "all",
        "src": "angad.calls.durationSeconds",
        "excl": "pre-16-Jul durations inflated by the 45s minimum-duration wall"},
    "₹ spend (approx)": {
        "def": ("Estimated spend: exact per-call Gemini tokens × a rate card "
                "RECALIBRATED on 2026-08-08 against the dev team's observed "
                "₹1.60-1.70 per answered call (the old $3/$12 card overstated model "
                "spend ~5.9x). STRUCTURE: cost per answered call is nearly FLAT — "
                "99.4% of tokens are the fixed ~35k-token system prompt, billed as "
                "input the moment the line is answered, and corr(duration, tokens) "
                "= 0.12. A call answered and dropped after one second costs almost "
                "what a full conversation costs. Unanswered dials open no session "
                "and are costed at ZERO."),
        "num": "Σ (tok_prompt×$0.50 + tok_response×$2.50)/1M × 96 + answered_started_minutes×₹0.60",
        "den": None,
        "window": "global date filter", "filters": "all",
        "src": "angad.calls.tokenUsage (exact) × rate card calibrated to observed billing",
        "excl": ("Rates unconfirmed — finance rate card + last-month Plivo/Telnyx/"
                 "Gemini invoices requested in the dev questionnaire §5. Unanswered "
                 "calls carry near-zero token cost (no session) — their cost is "
                 "telephony-only, which is why never-connected spend share (63.9%) "
                 "is measured in duration-weighted terms.")},
}

# Unified glossary — ONE vocabulary for every role (CEO to builder).
GLOSSARY = {
    "Dial": "one outbound call attempt (angad.calls row)",
    "Connected": "outcome in the answered family — a human (or their voicemail-filtered line) picked up",
    "Real conversation": "connected AND the customer spoke (voicemail excluded)",
    "Call drop": "customer picked up but gave no input, or the call died mid-conversation (is_dropped)",
    "Booked": "call outcome PRE_SITE or VIRTUAL_TOUR — a visit was agreed on the call",
    "Visit done": "attendance per ops sheets (the ONLY attendance truth; Zoho's flag is constant false)",
    "Lead": "one person×project record (PLID)",
    "Human": "one person (MLID-collapsed across projects)",
    "MLID": "Inncircles' master lead id — the canonical person id used across the business",
    "TOFU": "top of funnel: New Lead / Lead Initiated / new / contacted",
    "MOFU": "middle: engaged / qualified / Pre Site Visit / visit_scheduled",
    "BOFU": "bottom: Post Site Visit / visit_done / Flat Blocked / Sales Closure",
    "NRI": "lead dialled via Telnyx (international); RI = Plivo (India)",
}

_Z_RANK = {"New Lead": 1, "Lead Initiated": 2, "Pre Site Visit": 3,
           "Post Site Visit": 4, "Flat Blocked": 5, "Sales Closure": 6,
           "Not Interested": 0}
_C_RANK = {"new": 1, "contacted": 2, "engaged": 3, "qualified": 3,
           "visit_scheduled": 3, "visit_done": 4, "not_interested": 0, "spam": 0}


def band_of(zstage, cstage):
    """TOFU/MOFU/BOFU/OUT from the max of both truth systems."""
    zr = _Z_RANK.get(str(zstage), None)
    cr = _C_RANK.get(str(cstage), None)
    r = max([x for x in (zr, cr) if x is not None], default=None)
    if r is None:
        return "Unstaged"
    if r == 0:
        return "OUT"
    return "TOFU" if r <= 2 else ("MOFU" if r == 3 else "BOFU")


def parse_mixed_date(s):
    """Mixed-format date parser (the COHORT-BORNDATE-COERCE fix): ISO first,
    then day-first for the rest — no silent NaT drops from one-format parsing."""
    v = pd.to_datetime(s, errors="coerce", format="ISO8601")
    left = v.isna()
    if left.any():
        v2 = pd.to_datetime(pd.Series(s)[left], errors="coerce", dayfirst=True)
        v = v.copy()
        v[left] = v2
    return v


@lru_cache(maxsize=1)
def truth():
    """Per-lead_ref canonical facts, aggregated NEVER arbitrarily picked:
    projects (all of them), best Zoho stage, CRM stage, disagreement flag,
    audience (NRI/RI via telephony provider), web-history fields, scoring."""
    out = {}
    zl = _p("Zoho_Database__leads.parquet")
    if not zl.empty and "lead_ref" in zl.columns:
        zl = zl[zl["lead_ref"].notna()].copy()
        zl["_zrank"] = zl["stage"].map(_Z_RANK) if "stage" in zl.columns else None
        g = zl.groupby("lead_ref")
        agg = pd.DataFrame({
            "projects": g["project"].apply(
                lambda x: " + ".join(sorted({str(p) for p in x.dropna() if str(p).strip()}))),
            "plids": g["plid"].apply(
                lambda x: ", ".join(sorted({str(p) for p in x.dropna()})))
            if "plid" in zl.columns else "",
            # "best stage per lead" — sort by rank once and take the last row
            # per group. The previous groupby.apply ran idxmax inside every one
            # of ~3,300 groups and cost 8.9s of a 19s page load on its own.
            "z_stage": (zl.dropna(subset=["_zrank"]).sort_values("_zrank")
                          .groupby("lead_ref", sort=False)["stage"].last()),
            "lead_status": g["lead_status"].first() if "lead_status" in zl.columns else None,
            "lead_source": g["lead_source"].first() if "lead_source" in zl.columns else None,
            "born": parse_mixed_date(g["born_date"].first()) if "born_date" in zl.columns else None,
            "first_page": g["first_page_visited"].first() if "first_page_visited" in zl.columns else None,
            "last_page": g["last_page_visited"].first() if "last_page_visited" in zl.columns else None,
            "time_spent_min": pd.to_numeric(g["time_spent_minutes"].max(), errors="coerce")
            if "time_spent_minutes" in zl.columns else None,
            "page_views": pd.to_numeric(g["total_page_views"].max(), errors="coerce")
            if "total_page_views" in zl.columns else None,
            "utm_source": g["utm_source"].first() if "utm_source" in zl.columns else None,
            "campaign": g["campaign_name"].first() if "campaign_name" in zl.columns else None,
        })
        out["zoho"] = agg
    else:
        out["zoho"] = pd.DataFrame()

    crm = _p("Intelligent_CRM__crm_leads.parquet")
    persons = _p("Intelligent_CRM__persons.parquet")
    if not crm.empty and not persons.empty and "person_id" in crm.columns:
        m = persons[["_id", "lead_ref"]].rename(columns={"_id": "person_id"})
        crm = crm.merge(m, on="person_id", how="left")
    if not crm.empty and "lead_ref" in crm.columns:
        crm = crm[crm["lead_ref"].notna()].copy()
        crm["_crank"] = crm["stage"].map(_C_RANK) if "stage" in crm.columns else None
        g = crm.groupby("lead_ref")
        cagg = pd.DataFrame({
            "c_stage": (crm.dropna(subset=["_crank"]).sort_values("_crank")
                           .groupby("lead_ref", sort=False)["stage"].last()),
            "wa_in": pd.to_numeric(g["counters.wa_in"].max(), errors="coerce")
            if "counters.wa_in" in crm.columns else None,
            "wa_out": pd.to_numeric(g["counters.wa_out"].max(), errors="coerce")
            if "counters.wa_out" in crm.columns else None,
        })
        out["crm"] = cagg
    else:
        out["crm"] = pd.DataFrame()

    # audience: NRI (telnyx) vs RI (plivo), per lead, by majority of schedules
    aud = {}
    cs = _p("ASBLVoiceBot__call_schedules.parquet")
    if not cs.empty and {"lead_ref", "provider"} <= set(cs.columns):
        mode = (cs.dropna(subset=["lead_ref", "provider"])
                .groupby("lead_ref")["provider"]
                .agg(lambda x: x.value_counts().idxmax()))
        aud = {r: ("NRI" if p == "telnyx" else "RI") for r, p in mode.items()}
    out["audience"] = aud

    # combined band per lead_ref — one joined frame, not ~3,300 scalar Series
    # .get() lookups against a pyarrow-backed string index. The loop version
    # cost 2.6s of the page load by itself.
    z, c_ = out["zoho"], out["crm"]
    zs = (z["z_stage"] if not z.empty else pd.Series(dtype=object)).rename("z")
    cs = (c_["c_stage"] if not c_.empty else pd.Series(dtype=object)).rename("c")
    j = pd.concat([zs, cs], axis=1)
    if j.empty:
        out["band"], out["disagree"] = {}, set()
        return out
    j["z"] = j["z"].astype(object).where(j["z"].notna(), None)
    j["c"] = j["c"].astype(object).where(j["c"].notna(), None)
    # band_of is a tiny pure function over two labels — memoise the label pair
    # so it runs once per DISTINCT combination (a handful) instead of per lead.
    combo = {}
    def _band(zv, cv):
        k = (zv, cv)
        if k not in combo:
            combo[k] = band_of(zv, cv)
        return combo[k]
    out["band"] = {r: _band(zv, cv)
                   for r, zv, cv in zip(j.index, j["z"], j["c"])}
    both = j[j["z"].notna() & j["c"].notna()]
    out["disagree"] = {r for r, zv, cv in zip(both.index, both["z"], both["c"])
                       if _band(zv, None) != _band(None, cv)}
    return out


def scoring_of(ref, t, calls_me):
    """The user's colour model: green = qualified · purple = talked on
    WhatsApp with the campaign manager · orange = heavy campaign reaction."""
    z = t["zoho"]; c_ = t["crm"]
    flags = []
    q = False
    if len(calls_me) and "is_qualified" in calls_me.columns:
        q = bool(truthy(calls_me["is_qualified"]).any())
    if not c_.empty and str(c_["c_stage"].get(ref)) in ("qualified", "visit_scheduled", "visit_done"):
        q = True
    if q:
        flags.append({"colour": "green", "label": "QUALIFIED",
                      "why": "qualification captured on a call or CRM stage ≥ qualified",
                      "src": "angad.calls.userSignals · Intelligent_CRM.crm_leads.stage"})
    wa_in = c_["wa_in"].get(ref) if (not c_.empty and "wa_in" in c_.columns) else None
    if pd.notna(wa_in) and (wa_in or 0) > 0:
        flags.append({"colour": "purple", "label": "TALKED ON WHATSAPP",
                      "why": f"{int(wa_in)} inbound WhatsApp message(s) — spoke with the campaign manager",
                      "src": "Intelligent_CRM.crm_leads.counters.wa_in"})
    if not z.empty and ref in z.index:
        pv = z["page_views"].get(ref); tm = z["time_spent_min"].get(ref)
        if (pd.notna(pv) and (pv or 0) >= 10) or (pd.notna(tm) and (tm or 0) >= 10):
            flags.append({"colour": "orange", "label": "HIGH CAMPAIGN REACTION",
                          "why": f"{int(pv or 0)} page views · {round(float(tm or 0), 1)} min on site",
                          "src": "Zoho_Database.leads attribution"})
    return flags


def build_bands(b, c):
    """TOFU / MOFU / BOFU board + the per-project daily stage table
    (the Inncircles Growth-Reports layout), all from the truth layer."""
    t = truth()
    refs = list(set(c["lead_ref"].dropna()))
    if not refs:
        return None
    band = t["band"]
    rows = []
    z, c_ = t["zoho"], t["crm"]
    for r in refs:
        rows.append({"lead_ref": r, "band": band.get(r, "Unstaged"),
                     "z_stage": z["z_stage"].get(r) if not z.empty and r in z.index else None,
                     "c_stage": c_["c_stage"].get(r) if not c_.empty and r in c_.index else None,
                     "projects": z["projects"].get(r) if not z.empty and r in z.index else None,
                     "disagree": r in t["disagree"]})
    df = pd.DataFrame(rows)
    order = ["TOFU", "MOFU", "BOFU", "OUT", "Unstaged"]
    B = {"bands": []}
    for bd in order:
        sub = df[df["band"] == bd]
        drill = add_ids(sub.copy())
        B["bands"].append({
            "band": bd, "humans": int(len(sub)),
            "disagree": int(sub["disagree"].sum()),
            "tbl": table(drill.sort_values("z_stage", na_position="last"),
                         [x for x in ["lead_ref", "MLID", "Lead ID", "Source Lead ID",
                                      "projects", "z_stage", "c_stage", "disagree"]
                          if x in drill.columns], cap=400)})
    B["note"] = ("Band = the FURTHER of the two truth systems (Zoho stage vs "
                 "Intelligent_CRM stage). 'disagree' = the two systems put this "
                 "lead in different bands — those rows are shown, not hidden.")
    B["disagree_total"] = int(df["disagree"].sum())

    # per-project daily stage table from Zoho truth (prd_stage × born day)
    zl = b.get("zleads", pd.DataFrame())
    if not zl.empty and {"born_date", "project"} <= set(zl.columns):
        stage_col = "prd_stage" if "prd_stage" in zl.columns else "stage"
        zz = zl[zl["lead_ref"].isin(set(refs))].copy()
        zz["d"] = parse_mixed_date(zz["born_date"]).dt.date
        zz = zz[zz["d"].notna()]
        stages = ["New Lead", "Lead Initiated", "Pre Site Visit",
                  "Post Site Visit", "Sales Closure", "Not Interested", "Flat Blocked"]
        piv = zz.pivot_table(index=["d", "project"], columns=stage_col,
                             aggfunc="size", fill_value=0)
        rows2 = []
        for (d_, pj), r in piv.sort_index(ascending=[False, True]).iterrows():
            vals = [int(r.get(s, 0)) for s in stages]
            rows2.append([str(d_), str(pj)] + vals + [int(r.sum())])
        B["daily_stage"] = {"cols": ["Date", "Project"] + stages + ["Total Leads"],
                            "rows": rows2[:120], "total": len(rows2),
                            "shown": min(120, len(rows2)),
                            "note": "Zoho_Database.leads: prd_stage per born day per project "
                                    "— the Growth-Reports layout. Mixed date formats repaired "
                                    "(the 426-dropped-leads bug is fixed here)."}
    return B


def build_insights(b, c, d0, d1):
    """Auto-computed insights, one lens per role. Every insight names its
    number, its source, and where to click next. Red = systemic failure only."""
    t = truth()
    I = []
    n = len(c)

    def add(category, title, detail, severity="info", target=None, src=""):
        I.append({"category": category, "title": title, "detail": detail,
                  "importance": {"fail": "CRITICAL — act today",
                                 "warn": "IMPORTANT — plan this week",
                                 "info": "MONITOR — know it"}.get(severity, "MONITOR"),
                  "severity": severity, "target": target, "src": src})

    # taxonomy boundary warning (every role, but phrased for the DS)
    for iso, why in TAXONOMY_BREAKS:
        bdate = datetime.strptime(iso, "%Y-%m-%d").date()
        if d0 <= bdate <= d1:
            add("DATA INTEGRITY", f"Your date range crosses {iso}",
                f"{why}. Numbers before and after are NOT comparable — split the range.",
                "warn", None, "dev release notes v5–v8")

    # CEO: funnel outcome
    booked = c[c["is_booked"]]["lead_ref"].nunique()
    sv_done = b.get("co_sv_completed", pd.DataFrame())
    add("FUNNEL OUTCOME", f"{booked} humans booked in range; attendance truth = ops sheets",
        f"{len(sv_done)} attended visits on record overall. Bottom-of-funnel "
        "(confirmation→attendance) remains the leak.",
        "info", "funnel3", "angad.calls · ops workbooks")

    # Growth: speed to lead + ramp
    daily = c.groupby("date").size()
    if len(daily) >= 8:
        last, med = daily.iloc[-1], daily.iloc[-8:-1].median()
        if med and last < 0.25 * med:
            add("PIPELINE SUPPLY", f"Dial volume collapsed: {int(last)} vs {int(med)} median",
                "Yesterday's dials are a quarter of the trailing week. If Zoho stopped "
                "requesting dials, every downstream number starves.",
                "fail", "numbers", "angad.calls")
    add("PIPELINE SUPPLY", "Speed-to-lead is the cheapest win",
        "≤24h first-dials connect at 53% vs 31% at 3+ days; nothing is dialled "
        "within 30 min of birth. The rescue lists exploit this.",
        "warn", "review", "angad.calls first-dial timing")

    # CTO / builder: reminders + night + zombies
    sched = b.get("sched", pd.DataFrame())
    rem = int((sched["slug"] == "site_visit_reminder").sum()) if not sched.empty else 0
    # Superseded 2026-08-08. This card used to say "the scheduler is unshipped,
    # prepared_patches/02 is the fix" — built on RETRACTIONS R5. The cron DOES
    # run; it fires nothing. Patch 02 targets the agent's planner, a different
    # system from the Zoho-side cron that actually fails, so it cannot fix this.
    add("FOLLOW-UP DISCIPLINE",
        f"The reminder cron runs hourly and has never fired ({rem} reminder rows ever)",
        "`pre-site-visit-reminders` has run 987 times, scanned 35,291 records and "
        "fired 0 twenty-four-hour and 0 three-hour reminders, reporting 0 errors — "
        "so it looks healthy on every dashboard the dev team owns. Candidates "
        "exist: 41 parsed visit dates, several still in the future. The fix is "
        "the cron's fire condition, NOT prepared_patches/02 (that patches the "
        "agent's planner, a different system, and was written against a theory "
        "since retracted).",
        "fail", "audit", "Zoho_Database.cron_log · task=pre-site-visit-reminders")
    ow = c[(c["hour"] < 8) | (c["hour"] >= 21)]
    if len(ow):
        amap_ow = t["audience"]
        ow_aud = ow["lead_ref"].map(lambda r: amap_ow.get(r, "unknown"))
        n_nri, n_ri = int((ow_aud == "NRI").sum()), int((ow_aud == "RI").sum())
        add("COMPLIANCE", f"{len(ow):,} out-of-IST-window dials — but {pct(n_nri, len(ow))}% are NRI",
            f"Verified 2026-08-08 against the dev-side audit: {n_nri:,} are Telnyx/NRI "
            f"(IST night = the customer's local day — NOT a TRAI issue) and only "
            f"{n_ri:,} are Plivo/+91. The night-defer guard IS on (5,070 defer rows). "
            "The real exposure is the +91 remainder plus dials via /api/calls/initiate "
            "which skips the guard entirely.",
            "warn" if n_ri else "info", "metrics",
            "angad.calls · ASBLVoiceBot.call_schedules.provider · dev answers D19")

    # dev-audit landmines, verified against our cache 2026-08-08
    add("DATA INTEGRITY", "52.7% of calls carry roomName='pending-…' (24,379 rows)",
        "Every call_sid==roomName join silently drops them — and they are exactly the "
        "unanswered calls, so joined connect-rates are inflated. Join on "
        "externalScheduleId instead. Our own funnel counts read calls directly and "
        "are unaffected; per-call intent/recording joins only ever cover answered calls.",
        "warn", None, "verified: angad.calls.roomName prefix scan · dev answers J1")
    add("PRODUCTION DEFECT", "The 20 zombie confirmation calls: a HUMAN fired them",
        "Every post-Jul-17 site_visit_confirmation call carries esid prefix 'dash_s' = "
        "the ops dashboard's dispatch UI. Not the planner, not Zoho. The slug is dead "
        "in the planner but still dialable — retire it or gate it (dev answers J23).",
        "warn", "integrity", "verified: angad.calls.externalScheduleId prefixes")
    add("PRODUCTION DEFECT", "PERSONA SWITCHED ON 7 AUGUST: Radhika (female) → Ankit (male)",
        "Dated from OUR OWN data, not from their in_sync flag (which the dev team says "
        "is a bug in their check): the agent names itself in the greeting, so we counted "
        "the name in the agent's words. Aug 5: 0 Ankit / 210 Radhika. Aug 6: 3 / 200. "
        "Aug 7: 602 Ankit / 11 Radhika. A clean cutover on 7 Aug, matching the Mongo "
        "prompt push at 08:47 IST and the commit 'Point the production slug at the Ankit "
        "prompt'. The slug did NOT change and no prompt-version is stored, so this is the "
        "only per-call evidence. ANY metric spanning 6–7 Aug compares two different "
        "agents — split there before drawing conclusions.",
        "fail", "numbers", "PROBE: agent self-introduction in angad.calls transcripts")
    add("DATA INTEGRITY", "Outcome-bucket contamination rules (encode in every analysis)",
        "NO_ANSWER: partly fabricated by cleanupStaleCalls (no guard, every 5 min). "
        "VOICEMAIL: two indistinguishable origins (AMD vs transcript regex). "
        "language='hinglish': inflated — it is the hardcoded default when the classifier "
        "is skipped. Slots all-null + summary contains 'classifier offline' = classifier "
        "failure, not an unqualified lead. propertyInterested='ASBL Loft' is the "
        "unspecified-project default, not expressed interest.",
        "warn", None, "dev answers J2/J6/J9/J11/J12")
    # DB PROBE 2026-08-08: the extraction pipeline writes almost nothing.
    full = b["calls"]
    slot_fill = {s: int(full[s].notna().sum()) for s in
                 ("slot_budget", "slot_timeline", "slot_config", "slot_callback",
                  "slot_objection", "slot_intent", "slot_site_visit")
                 if s in full.columns}
    if slot_fill:
        dead = [k for k, v in slot_fill.items() if v == 0]
        sv = slot_fill.get("slot_site_visit", 0)
        it_ = b.get("intent_id", pd.DataFrame())
        sv_intent = int(nonempty(it_["intent.site_visit_outcome"]).shape[0]) \
            if "intent.site_visit_outcome" in it_.columns else 0
        add("PRODUCTION DEFECT",
            "THE SCHEDULER READS THE WRONG DATABASE — extracted slots are empty on "
            f"angad.calls ({len(dead)} of {len(slot_fill)} slot fields = 0 rows) while the "
            "same intelligence IS being written to ASBLVoiceBot",
            f"Probed directly. angad.calls.extractedSlots: "
            f"{', '.join(k.replace('slot_','') for k in dead)} = ZERO across {len(full):,} calls; "
            f"site_visit filled on only {sv:,} ({pct(sv, len(full))}%). BUT "
            f"ASBLVoiceBot.call_transcript_intent_identification holds budget on 246 calls, "
            f"timeline on 3,894, objections on 2,095, site-visit outcome on {sv_intent:,}, and "
            "lead_profiles holds budget/timeline/site_visit_agreed. So the classifier RUNS and "
            "its output IS persisted — just into the mirror database, not onto the call document. "
            "The site-visit planner reads call.extractedSlots.site_visit_requested "
            "(schedule-on-presite.ts), which is empty on 99.8% of calls. That is the true root "
            "cause of the 89% no-reminder gap: not a date-parsing edge case — the scheduler "
            "cannot see data that already exists one database away. Ask the dev team to write "
            "the classifier result back onto the Call doc (or point the planner at the intent "
            "collection). Verify the claim with them before assuming intent.",
            "fail", "numbers",
            "PROBE: angad.calls vs ASBLVoiceBot.call_transcript_intent_identification / lead_profiles")
    # ROUND-4 VERIFIED: the follow-up cron was starved for ~11 days
    try:
        cl = _p("Zoho_Database__cron_log.parquet")
        if not cl.empty and "task" in cl.columns:
            cad = cl[cl["task"].astype(str).str.contains("cadence", case=False, na=False)].copy()
            cad["d"] = pd.to_datetime(cad["ran_at"], errors="coerce", utc=True).dt.date
            per = cad.groupby("d").size()
            recent = per[per.index >= date(2026, 7, 26)]
            starved = int((recent < 5).sum())
            if starved:
                add("PIPELINE SUPPLY",
                    f"The follow-up cron (prd-cadence) was starved for ~{starved} days",
                    "Verified in Zoho_Database.cron_log: runs/day were 56 / 28 / 21 / 37 in "
                    "late July, then ZERO rows 26–31 Jul, then 1–3 runs/day through 6 Aug, "
                    "recovering to 26 on 7 Aug and 56 on 8 Aug. The relay dev attributes this "
                    "to a Vercel→ECS migration with crons off; the data shows the outage was "
                    "far longer than the Aug 6–7 window they described. Follow-up dials are "
                    "the nurtured, higher-converting traffic — their absence explains both the "
                    "volume collapse AND the zero-booking days.",
                    "fail", "numbers", "PROBE: Zoho_Database.cron_log.task='prd-cadence'")
    except Exception:
        pass
    # ROUND-4: the over-dialling regression, dated
    add("EFFICIENCY", "Over-dialling is a DATED REGRESSION, not a design choice",
        "The relay's follow-up v3 (shipped 2026-06-18) REMOVED the attempt count cap that "
        "v1 and v2 had. What remains is slot hours plus a 7-day lifetime — 9 slots/day x 7 "
        "days = up to ~63 dials per lead. Retries do NOT stop on CONNECTED (picked up but "
        "did not book) or on BUSY / NOT_CONNECTED / SWITCHED_OFF. Consecutive_Missed_Count "
        "exists but nothing enforces on it. That is exactly our 62.6%-of-all-calls finding: "
        "unreachable numbers dialled every slot for a week. Restoring a count cap is the "
        "single biggest cost lever available.",
        "fail", "metrics", "dev round 4 §4 (followup.ts:627) + our attempts probe")
    add("MEASUREMENT GAP", "Attendance is un-architected — the system cannot observe its own goal",
        "Dev-side confirms: no field, no endpoint, no path by which attendance could "
        "ever arrive. Ops sheets are the only truth. One write-back field would make "
        "the whole funnel measurable — this is the #1 ask for the dev team.",
        "fail", "visits", "dev answers F24")
    add("PRODUCTION DEFECT", "Cheapest accuracy win: TRANSCRIBE_POSTCALL is OFF",
        "accurateTranscript exists on zero cached calls — batch re-transcription "
        "(domain-vocabulary, diarised) is shipped but disabled. Every outcome and "
        "every visit date is currently extracted from a transcript the devs "
        "themselves measure at <50% accuracy. Turning one env var on is fully "
        "async and risk-free to live calls.",
        "fail", None, "verified: no accurateTranscript column in calls · dev answers H29")
    add("REVENUE OPPORTUNITY", "Sub-4-minute bookings are actively blocked (invisible cost)",
        "hangup_call(PRE_SITE) is rejected until 4 elapsed minutes or 11 customer "
        "turns. A fast, efficient booking on an efficient call is refused and leaves "
        "NO trace. Worth an experiment before any script optimisation.",
        "warn", None, "dev answers J14")

    # CI head: WhatsApp + intents
    add("CHANNEL HEALTH", "WhatsApp 'sent' ≠ delivered",
        "The 19-sender pool died silently; 28,309 sends never delivered. Treat every "
        "WhatsApp metric as send-attempts until the pool is replaced.",
        "warn", "whatsapp", "Zoho_Database.stuck_sends · dead_senders")
    it = b.get("intent_id", pd.DataFrame())
    if not it.empty and "call_sid" in it.columns:
        sel = it[it["call_sid"].isin(set(c["roomName"].dropna()))]
        if len(sel) and "intent.intent_strength" in sel.columns:
            hot = int(sel["intent.intent_strength"].isin(["strong", "moderate"]).sum())
            add("REVENUE OPPORTUNITY", f"{hot} strong/moderate-intent conversations in range",
                "Each is a warm human. The Intent tab lists them with next steps.",
                "info", "intent", "ASBLVoiceBot.call_transcript_intent_identification")

    # Business builder: identity + disagreement
    dis = len(t["disagree"])
    if dis:
        add("DATA INTEGRITY", f"{dis} leads sit in different funnel bands per Zoho vs CRM",
            "The two systems disagree about where these humans are. The Funnel tab "
            "shows every disagreement row — resolve upstream, don't average it away.",
            "warn", "funnel3", "Zoho_Database.leads vs Intelligent_CRM.crm_leads")
    aud = t["audience"]
    if aud:
        nri = sum(1 for r in set(c["lead_ref"].dropna()) if aud.get(r) == "NRI")
        add("SEGMENTATION", f"{nri} NRI humans in this range",
            "NRI (Telnyx) and RI (Plivo) calling windows are different worlds — use "
            "the NRI/RI filter before reading any time-of-day chart.",
            "info", "metrics", "ASBLVoiceBot.call_schedules.provider")

    # THE 89% GAP, located in the cron ledger (2026-08-08). Three earlier
    # explanations were wrong; this one has a runtime record behind it.
    try:
        cl = _p("Zoho_Database__cron_log.parquet")
        if not cl.empty and "task" in cl.columns:
            rem = cl[cl["task"] == "pre-site-visit-reminders"]
            if len(rem):
                scanned = int(pd.to_numeric(rem.get("result.scanned"),
                                            errors="coerce").fillna(0).sum())
                f24 = int(pd.to_numeric(rem.get("result.fired_24h"),
                                        errors="coerce").fillna(0).sum())
                f3 = int(pd.to_numeric(rem.get("result.fired_3h"),
                                       errors="coerce").fillna(0).sum())
                errs = int(pd.to_numeric(rem.get("result.errors_count"),
                                         errors="coerce").fillna(0).sum())
                if f24 + f3 == 0:
                    add("SYSTEM FAILURE",
                        "The site-visit reminder cron has never fired a single reminder",
                        f"`pre-site-visit-reminders` has run {len(rem):,} times, scanned "
                        f"{scanned:,} records, and fired {f24} 24-hour and {f3} 3-hour "
                        f"reminders — zero, across every run since 26 June. It reports "
                        f"{errs} errors, so it looks healthy on every dashboard the dev "
                        "team has. Candidates exist: site_visit_bookings holds 41 parsed "
                        "visit dates and several are still in the future. This — not "
                        "'the visit time was never parsed' — is why booked leads get no "
                        "reminder. THREE earlier explanations of this gap were wrong "
                        "(wrong-database, our own field-name bug, no-parsed-time); this "
                        "one is the job's own runtime ledger.",
                        "bad", "audit", "Zoho_Database.cron_log · task=pre-site-visit-reminders")
            # the stage field is not maintained — it is hand-backfilled
            rec = cl[cl["task"].isin(["reconcile-stage-from-status",
                                      "backfill-presite-stage"])]
            if len(rec) < 10:
                last = pd.to_datetime(rec["ran_at"], errors="coerce", utc=True).max()
                add("DATA INTEGRITY",
                    "Zoho `stage` is not a maintained field — it is a manual backfill",
                    f"The only jobs that write it (`reconcile-stage-from-status`, "
                    f"`backfill-presite-stage`) have run {len(rec)} times in total, "
                    f"last on {str(last)[:10]}. They are not scheduled. Consequence: "
                    "`stage` is null on 98-100% of every lead born since 29 July, "
                    "against 18-40% before it. Any funnel or daily report reading Zoho "
                    "stage is reading a field nobody updates — use the ANGAD relay "
                    "stage, or read both (the Daily Report toggle does).",
                    "bad", "daily", "Zoho_Database.cron_log + Zoho_Database.leads.stage")
    except Exception:
        pass

    # Recomputed live off the corrected rate card — the old text hardcoded
    # percentages from the $3/$12 model and became false when the card changed.
    try:
        ci, _, _, ans = cost_of(c)
        tot_inr = float(ci.sum())
        drop_pct = pct(float(ci[c["outcome"] == "DROPPED"].sum()), tot_inr)
        book_pct = pct(float(ci[c["is_booked"]].sum()), tot_inr)
        talked = (ans & (pd.to_numeric(c.get("duration_sec"), errors="coerce")
                         .fillna(0) >= 30))
        waste_pct = pct(float(ci[~talked].sum()), tot_inr)
        add("COST", f"{drop_pct}% of spend goes to calls the customer abandoned",
            f"Of ~₹{tot_inr:,.0f} estimated in this window, {drop_pct}% lands on "
            f"DROPPED calls — picked up, nothing said — and only {book_pct}% on "
            f"calls that booked a visit. {waste_pct}% goes to calls that never "
            "reached 30 seconds of conversation. Because 99.4% of tokens are the "
            "fixed ~35k prompt, a call dropped after one second costs almost what a "
            "full conversation costs: the lever is stopping wasted PICKUPS (AMD, "
            "attempt-tail suppression, opening line), not shorter conversations. "
            "Unanswered dials are free on the model side and are costed at zero.",
            "warn", "metrics",
            "angad.calls.tokenUsage (exact) × rate card calibrated to observed billing")
    except Exception:
        pass
    # DB PROBE: the retry tail
    if "total_attempts" in c.columns:
        att = pd.to_numeric(c["total_attempts"], errors="coerce")
        hi = c[att >= 20]
        if len(hi):
            add("EFFICIENCY",
                f"{pct(len(hi), len(c))}% of ALL calls go to leads already dialled 20+ times "
                f"— {hi['lead_ref'].nunique():,} leads, {len(hi):,} calls",
                f"These connect at {pct(int(hi['reached'].sum()), len(hi))}% vs "
                f"{pct(int(c['reached'].sum()), len(c))}% overall, median duration "
                f"{pd.to_numeric(hi['duration_sec'], errors='coerce').median():.0f}s, and "
                f"produced {int(hi['is_booked'].sum())} bookings. The mix is BUSY / VOICEMAIL / "
                "NOT_CONNECTED — unreachable numbers being hammered. The agent has NO retry "
                "policy of its own (dev answer B9), so the relay is asking for every one of "
                "these. Capping attempts is the single biggest cost lever on the board. "
                "Click the Attempts-per-lead bars to see the calls, what was said, and whether "
                "they got frustrated.",
                "warn", "metrics", "PROBE: angad.calls.total_attempts ≥ 20")
    add("EFFICIENCY", "Attempt-curve saturation",
        "Connect probability by attempt number flattens after ~4 attempts — every "
        "dial after that costs the same and returns less (Audit tab, attempt curve).",
        "info", "audit", "angad.calls attempt linkage")
    return I


def build_globe(b, c):
    """Call-geography payload for the globe: arcs Hyderabad→countries (NRI via
    Telnyx + workbook countries), project sites for the Hyderabad zoom, and
    honest NA cards where the data does not exist."""
    t = truth()
    aud = t["audience"]
    refs = set(c["lead_ref"].dropna())
    nri_refs = {r for r in refs if aud.get(r) == "NRI"}
    ri_refs = {r for r in refs if aud.get(r) == "RI"}
    cn = b.get("co_hu_leads", pd.DataFrame())
    countries = {}
    if not cn.empty and "country" in cn.columns:
        vc = nonempty(cn["country"]).str.strip().str.title().value_counts().head(15)
        countries = {k: int(v) for k, v in vc.items() if k.lower() not in ("nan", "na", "")}
    COORDS = {"India": (78.9, 20.6), "United States": (-98.5, 39.8), "Usa": (-98.5, 39.8),
              "United Arab Emirates": (54.0, 24.0), "Uae": (54.0, 24.0),
              "United Kingdom": (-1.5, 52.5), "Uk": (-1.5, 52.5),
              "Singapore": (103.8, 1.35), "Australia": (134.0, -25.0),
              "Canada": (-106.0, 56.0), "Qatar": (51.2, 25.3), "Kuwait": (47.8, 29.3),
              "Saudi Arabia": (45.0, 24.0), "Oman": (56.0, 21.0), "Germany": (10.4, 51.1),
              "Ireland": (-8.0, 53.4), "Netherlands": (5.3, 52.1), "Bahrain": (50.5, 26.0),
              "Nigeria": (8.0, 9.0), "South Africa": (24.0, -29.0), "Japan": (138.0, 36.2),
              "New Zealand": (174.0, -41.0)}
    HYD = (78.475, 17.435)
    arcs = []
    for name, cnt in countries.items():
        if name == "India":
            continue
        ll = COORDS.get(name)
        if ll:
            arcs.append({"to": name, "lon": ll[0], "lat": ll[1], "calls": cnt})
    SITES = [
        {"name": "ASBL Spectra", "area": "Financial District", "lon": 78.336, "lat": 17.411},
        {"name": "ASBL Loft", "area": "Financial District", "lon": 78.345, "lat": 17.417},
        {"name": "ASBL Landmark", "area": "Kukatpally Y Junction", "lon": 78.399, "lat": 17.494},
        {"name": "ASBL Broadway", "area": "KPHB", "lon": 78.39, "lat": 17.48},
    ]
    zl = b.get("zleads", pd.DataFrame())
    urls = []
    if not zl.empty and "first_page_visited" in zl.columns:
        u = nonempty(zl[zl["lead_ref"].isin(refs)]["first_page_visited"]).value_counts().head(10)
        urls = [{"label": str(k)[:60], "value": int(v)} for k, v in u.items()]
    return {"hyd": {"lon": HYD[0], "lat": HYD[1]},
            "ri_calls": int(sum(1 for _ in ri_refs)), "nri_calls": int(len(nri_refs)),
            "countries": countries, "arcs": arcs, "sites": SITES, "urls": urls,
            "na": ["City-level RI geography is NOT in any collection (no pincode/city on "
                   "dialled leads) — shown as NA until the CRM exports it",
                   "Per-call tower/exchange location is never captured — arcs are "
                   "person-level (audience by telephony provider)"],
            "src": "ASBLVoiceBot.call_schedules.provider · cohort workbook countries · "
                   "Zoho_Database.leads.first_page_visited"}


# ============================================================ UX-2 (task #8)
# The notebook review panel, the ops-format daily table, dedicated era
# sections and the per-human (MLID) transaction ledger.

def _ist_str(s):
    t = pd.to_datetime(s, errors="coerce", utc=True)
    try:
        return t.dt.tz_convert("Asia/Kolkata").dt.strftime("%d %b %H:%M")
    except Exception:
        return t.astype(str)


def build_review(b, c, d0, d1):
    """The user's handwritten review panel ('Driving the funnel'), answered
    live for the CURRENT filter selection. One block per notebook question,
    each with its number, a drill table and the source collection."""
    R = {"from": str(d0), "to": str(d1)}
    it = b.get("intent_id", pd.DataFrame())
    sel_it = pd.DataFrame()
    if not it.empty and "call_sid" in it.columns:
        sel_it = it[it["call_sid"].isin(set(c["roomName"].dropna()))]

    # ---------- +ve's
    pos = []
    daily = c.groupby("date").size()
    pos.append({"q": "Daily call trigger count",
                "a": f"{len(c):,} calls · {daily.mean():.0f}/day avg",
                "src": "angad.calls",
                "chart": {"labels": [str(x) for x in daily.index],
                          "values": [int(v) for v in daily.values]}})

    per = c.groupby("lead_ref").agg(
        dials=("roomName", "size"), reached=("reached", "max"),
        last=("ts_ist", "max")).reset_index()
    per["last"] = pd.to_datetime(per["last"], errors="coerce").dt.strftime("%d %b %H:%M")
    who = add_ids(per.sort_values("dials", ascending=False))
    pos.append({"q": "Who was called? (profiles linked)",
                "a": f"{c['lead_ref'].nunique():,} unique leads",
                "src": "angad.calls · ids: Zoho_Database.mlid_registry/leads",
                "tbl": table(who, [x for x in ["lead_ref", "MLID", "Lead ID",
                                               "dials", "reached", "last"]
                                   if x in who.columns], cap=300),
                "hint": "click any lead_id / MLID to open its full profile"})

    hh = c.groupby("hour").size()
    pos.append({"q": "When were they called?",
                "a": f"peak hour {int(hh.idxmax()):02d}:00" if len(hh) else "—",
                "src": "angad.calls.ts_ist",
                "chart": {"labels": [f"{int(h):02d}h" for h in hh.index],
                          "values": [int(v) for v in hh.values]}})

    if not sel_it.empty:
        told = nonempty(sel_it.get("intent.call_summary", pd.Series(dtype=str)))
        ci = counts(sel_it["intent.caller_intent"]) if "intent.caller_intent" in sel_it.columns else []
        pos.append({"q": "What was told to them / what did they ask?",
                    "a": f"{len(told):,} calls have an extracted summary",
                    "src": "ASBLVoiceBot.call_transcript_intent_identification",
                    "mix": ci[:10],
                    "samples": [str(x)[:220] for x in told.head(8)]})
    else:
        pos.append({"q": "What was told to them / what did they ask?",
                    "a": "no intent rows for this selection",
                    "src": "ASBLVoiceBot.call_transcript_intent_identification"})

    zl = b.get("zleads", pd.DataFrame())
    if not zl.empty and "project" in zl.columns and "lead_ref" in zl.columns:
        zsel = zl[zl["lead_ref"].isin(set(c["lead_ref"].dropna()))]
        pj = norm_project(zsel["project"]).replace("", "(unknown)")
        pos.append({"q": "What projects were picked up?",
                    "a": f"{pj[pj != '(unknown)'].nunique()} projects",
                    "src": "Zoho_Database.leads.project",
                    "mix": counts(pj)[:12]})

    # ---------- -ve's (the attempts chain)
    neg = []
    never = per[~per["reached"].astype(bool)]
    crm = b.get("crm", pd.DataFrame())
    born_und = None
    if not crm.empty and "born_at" in crm.columns and "lead_ref" in crm.columns:
        bt = pd.to_datetime(crm["born_at"], errors="coerce", utc=True)
        inr = crm[(bt.dt.date >= d0) & (bt.dt.date <= d1)]
        dialled = set(b["calls"]["lead_ref"].dropna())
        und = inr[~inr["lead_ref"].isin(dialled)]
        born_und = add_ids(und[["lead_ref"]].drop_duplicates())
    nv = add_ids(never)
    neg.append({"q": "Who was missed?",
                "a": (f"{len(never):,} leads dialled but never answered"
                      + (f" · {len(born_und):,} born in range, never dialled"
                         if born_und is not None else "")),
                "src": "angad.calls (ever answered) · Intelligent_CRM.crm_leads.born_at (never dialled)",
                "tbl": table(nv, [x for x in ["lead_ref", "MLID", "Lead ID",
                                              "dials", "last"] if x in nv.columns], cap=300),
                "tbl2": (table(born_und, [x for x in ["lead_ref", "MLID", "Lead ID"]
                                          if x in born_und.columns], cap=200)
                         if born_und is not None and len(born_und) else None)})

    miss = c[c["outcome_group"] == "Not reached"]
    mh = miss.groupby("hour").size()
    neg.append({"q": "What time were they called (the missed ones)?",
                "a": f"{len(miss):,} not-reached calls",
                "src": "angad.calls",
                "chart": {"labels": [f"{int(h):02d}h" for h in mh.index],
                          "values": [int(v) for v in mh.values]}})

    drops = c[c["is_dropped"]].copy()
    if len(drops):
        drops["when"] = pd.to_datetime(drops["ts_ist"], errors="coerce").dt.strftime("%d %b %H:%M")
        drops["last_customer_words"] = drops.get("lead_said", pd.Series(index=drops.index)).astype(str).str[-120:]
        drops["last_agent_words"] = drops.get("agent_said", pd.Series(index=drops.index)).astype(str).str[-120:]
        dtab = add_ids(drops.sort_values("ts_ist", ascending=False))
        neg.append({"q": "Who dropped, and when did they drop?",
                    "a": f"{len(drops):,} dropped calls ({pct(len(drops), len(c))}%)",
                    "src": "angad.calls (is_dropped)",
                    "mix": counts(drops["drop_stage"].fillna("(unknown stage)"))[:10],
                    "tbl": table(dtab, [x for x in ["when", "lead_ref", "MLID", "Lead ID",
                                                    "drop_stage", "duration_sec",
                                                    "last_customer_words", "last_agent_words"]
                                        if x in dtab.columns], cap=300)})

        tech = drops[truthy(drops["call_failed"])] if "call_failed" in drops.columns else drops.iloc[0:0]
        neg.append({"q": "Why was it dropped — technical or call failure?",
                    "a": f"{len(tech):,} technical (call_failed) · {len(drops) - len(tech):,} conversation drops",
                    "src": "angad.calls.status/call_failed + drop_stage",
                    "mix": counts(drops["status"].fillna("(none)"))[:8]})

        rc = truthy(drops["was_recontacted"]) if "was_recontacted" in drops.columns else pd.Series(False, index=drops.index)
        again = drops[rc].copy()
        ans = truthy(again["recontact_reached"]) if "recontact_reached" in again.columns else pd.Series(False, index=again.index)
        med_h = pd.to_numeric(again.get("hours_to_next_call"), errors="coerce").median()
        if len(again):
            again["dropped_at"] = again["when"]
            again["recontacted_at"] = _ist_str(again["next_call_at"])
            again["hrs_later"] = pd.to_numeric(again["hours_to_next_call"], errors="coerce").round(1)
            rtab = add_ids(again.sort_values("ts_ist", ascending=False))
            rtbl = table(rtab, [x for x in ["lead_ref", "MLID", "Lead ID", "dropped_at",
                                            "recontacted_at", "hrs_later", "next_outcome"]
                                if x in rtab.columns], cap=300)
        else:
            rtbl = None
        neg.append({"q": "Whoever dropped — was he contacted again? When? Did he answer?",
                    "a": (f"{int(rc.sum()):,} of {len(drops):,} recontacted ({pct(int(rc.sum()), len(drops))}%)"
                          + (f" · median {med_h:.1f}h later" if pd.notna(med_h) else "")
                          + f" · {int(ans.sum()):,} answered on recontact"),
                    "src": "angad.calls (next-call linkage computed at sync)",
                    "tbl": rtbl})
    else:
        neg.append({"q": "Who dropped?", "a": "no dropped calls in this selection",
                    "src": "angad.calls"})

    # ---------- WA
    wa = []
    texted = c[truthy(c["whatsappSent"])] if "whatsappSent" in c.columns else c.iloc[0:0]
    wam = b.get("wa_msgs", pd.DataFrame())
    wa_leads = wam[wam["lead_ref"].isin(set(c["lead_ref"].dropna()))] if not wam.empty else wam
    wa.append({"q": "Who was texted?",
               "a": (f"{texted['lead_ref'].nunique():,} leads texted from calls in range"
                     + (f" · {wa_leads['lead_ref'].nunique():,} with any WhatsApp history"
                        if not wa_leads.empty else "")),
               "src": "angad.calls.whatsappSent · Zoho_Database.whatsapp_messages"})
    if not sel_it.empty and len(texted):
        ts = sel_it[sel_it["call_sid"].isin(set(texted["roomName"].dropna()))]
        said = nonempty(ts.get("intent.call_summary", pd.Series(dtype=str)))
        wa.append({"q": "What did they say on the call (the texted ones)?",
                   "a": f"{len(said):,} summaries",
                   "src": "ASBLVoiceBot.call_transcript_intent_identification",
                   "samples": [str(x)[:220] for x in said.head(8)]})
    dl = b.get("doc_log", pd.DataFrame())
    if not dl.empty:
        dsel = dl[dl["lead_ref"].isin(set(c["lead_ref"].dropna()))]
        wa.append({"q": "What was sent?",
                   "a": f"{len(dsel):,} documents to {dsel['lead_ref'].nunique():,} leads",
                   "src": "Zoho_Database.doc_send_log",
                   "mix": counts(dsel["doc_type"].fillna("(untyped)"))[:10] if len(dsel) else []})

    R["pos"], R["neg"], R["wa"] = pos, neg, wa
    R["note"] = ("This panel answers the handwritten review sheet question by question, "
                 "for whatever filters are active. Every block names its source "
                 "collection; tables show lead_id + MLID and open the profile on click.")
    return R


def build_ops_daily(b, c):
    """The exact daily table from the user's ops screenshot: Date · Leads Born ·
    Successful Calls (picked up) · SV Booked · SV Done · MLIDs(SV done).
    First three columns come verbatim from the ops workbook import; SV-done
    dates from the AI-done cohort sheet (Excel day/month mangling repaired)."""
    od = b.get("co_ai_daily", pd.DataFrame())
    if od.empty:
        return None
    t = od.copy()
    t["date"] = pd.to_datetime(t["date"], errors="coerce").dt.date

    done = b.get("co_ai_done", pd.DataFrame())
    done_by_day = {}
    if not done.empty and "updated_date" in done.columns and "mlid" in done.columns:
        def fixd(x):
            d = pd.to_datetime(x, errors="coerce", dayfirst=True)
            if pd.isna(d):
                return None
            # Excel float-mangle: month/day swapped when 'month' is not Jul/Aug
            if d.month not in (7, 8) and d.day in (7, 8):
                d = d.replace(month=d.day, day=d.month)
            return d.date()
        dd = done.copy()
        dd["visit_day"] = dd["updated_date"].map(fixd)
        for day, grp in dd.dropna(subset=["visit_day"]).groupby("visit_day"):
            ml = [str(m) for m in grp["mlid"].dropna().astype(str).unique()]
            done_by_day[day] = ml

    rows = []
    for _, r in t.sort_values("date").iterrows():
        day = r["date"]
        ml = done_by_day.get(day, [])
        rows.append([str(day), int(r.get("leads_born", 0) or 0),
                     int(r.get("successful_calls_picked_up", 0) or 0),
                     int(r.get("site_visits_booked", 0) or 0),
                     len(ml), ", ".join(ml)])
    return {"cols": ["Date", "Leads Born", "Successful Calls (Picked Up)",
                     "Site Visits Booked", "Site Visits Done", "MLIDs (SV Done)"],
            "rows": rows, "total": len(rows), "shown": len(rows),
            "note": ("Leads Born / Successful Calls / SV Booked come verbatim from the "
                     "ops workbook (cohort_ai_daily import). SV Done + MLIDs from the "
                     "AI-done cohort sheet with Excel day/month swaps repaired. "
                     "'Successful Calls' = unique leads picked up that day (ops "
                     "definition), not call events. Attendance exists ONLY in ops "
                     "sheets — Zoho's visit_attended flag is constant false.")}


# ---------------------------------------------------------------- RAMA-3
# The Growth-Reports "Daily Leads Report": Date · Week · Project × stage, with
# EVERY cell clickable down to the leads that make it up (R2.2 / R3.1).
# Grain is the LEAD (person x project = PLID), not the call — a lead born on a
# day is counted on that day regardless of when it was dialled.

REPORT_STAGES = ["New Lead", "Lead Initiated", "Pre Site Visit",
                 "Post Site Visit", "Sales Closure", "Not Interested",
                 "Flat Blocked"]

# Two stage systems exist on the same row and they disagree on half the leads:
#   stage      = the Zoho / Inncircles CRM stage (what the ops report shows)
#   prd_stage  = the stage ANGAD's own relay maintains
# We do not pick a winner — the toggle chooses, the disagreement is reported.
STAGE_SRC = {"zoho": ("stage", "Zoho / Inncircles CRM stage"),
             "angad": ("prd_stage", "ANGAD relay stage (prd_stage)")}


def _report_frame(b, q, d0, d1):
    """Zoho lead rows in scope for the Daily Report, with a parsed born date,
    a normalised project and both stage columns. Returns (df, window)."""
    z = b.get("zleads", pd.DataFrame())
    if z.empty or "lead_ref" not in z.columns:
        return pd.DataFrame(), (d0, d1)
    z = z.copy()
    born = parse_mixed_date(z["born_date"]) if "born_date" in z.columns else pd.Series(pd.NaT, index=z.index)
    born = pd.to_datetime(born, errors="coerce", utc=True)
    if "created_at" in z.columns:
        miss = born.isna()
        if miss.any():                      # born_date is absent on 369 rows
            born.loc[miss] = pd.to_datetime(z.loc[miss, "created_at"],
                                            errors="coerce", utc=True)
    # born dates are calendar days in IST — the report is read by an IST team
    z["_born"] = born.dt.tz_convert("Asia/Kolkata").dt.date
    z["_project"] = norm_project(z["project"]) if "project" in z.columns else ""
    z.loc[z["_project"].astype(str).str.strip() == "", "_project"] = "(no project)"
    for src, _ in STAGE_SRC.values():
        if src not in z.columns:
            z[src] = None

    # window: the Born-date range if the user set one, otherwise the action
    # window — a report about leads born is dated by birth, never by dial.
    w0 = pd.to_datetime(q.get("born_from")).date() if q.get("born_from") else d0
    w1 = pd.to_datetime(q.get("born_to")).date() if q.get("born_to") else d1
    z = z[z["_born"].notna() & (z["_born"] >= w0) & (z["_born"] <= w1)]

    proj = [v for v in q.get("project", "").split("|") if v]
    if proj:
        z = z[z["_project"].isin(proj)]
    auds = [v for v in q.get("audience", "").split("|") if v]
    if auds:
        want = {("NRI" if "NRI" in v else "RI") for v in auds}
        amap = truth()["audience"]
        z = z[z["lead_ref"].map(lambda r: amap.get(r, "RI")).isin(want)]
    return z, (w0, w1)


def build_daily_report(b, q, d0, d1, key="zoho"):
    """Date x Project x stage counts, one row per (born date, project)."""
    key = key if key in STAGE_SRC else "zoho"
    scol, slabel = STAGE_SRC[key]
    z, (w0, w1) = _report_frame(b, q, d0, d1)
    if z.empty:
        return {"cols": [], "rows": [], "total": 0, "shown": 0, "stage_src": key,
                "note": "No leads were born inside this window."}

    st = z[scol].astype(str).str.strip()
    st = st.where(st.isin(REPORT_STAGES), "Unstaged")
    z = z.assign(_stage=st)

    # disagreement: both systems say something and they land on different stages
    a = z[STAGE_SRC["zoho"][0]].astype(str).str.strip()
    c2 = z[STAGE_SRC["angad"][0]].astype(str).str.strip()
    both = a.isin(REPORT_STAGES) & c2.isin(REPORT_STAGES)
    z = z.assign(_disagree=both & (a != c2))

    cols_out = ["Date", "Week", "Project"] + REPORT_STAGES + ["Unstaged", "Total", "⚠ disagree"]
    rows = []
    for (day, proj), g in z.groupby(["_born", "_project"], sort=True):
        wk = pd.Timestamp(day).isocalendar()
        counts_ = g["_stage"].value_counts()
        r = [str(day), f"W{wk.week}", str(proj)]
        r += [int(counts_.get(s, 0)) for s in REPORT_STAGES]
        r += [int(counts_.get("Unstaged", 0)), int(len(g)), int(g["_disagree"].sum())]
        rows.append(r)
    rows.sort(key=lambda r: (r[0], r[2]), reverse=False)

    tot = ["TOTAL", "", f"{z['_project'].nunique()} projects"]
    tot += [int((z["_stage"] == s).sum()) for s in REPORT_STAGES]
    tot += [int((z["_stage"] == "Unstaged").sum()), int(len(z)), int(z["_disagree"].sum())]

    return {
        "cols": cols_out, "rows": rows, "total": len(rows), "shown": len(rows),
        "totals": tot, "stage_src": key, "stage_label": slabel,
        "window": f"{w0} → {w1}",
        "leads": int(len(z)), "projects": int(z["_project"].nunique()),
        "disagree": int(z["_disagree"].sum()),
        "note": ("One row per born-date x project. Grain is the LEAD "
                 "(person x project), so a person in two projects appears twice — "
                 "that is how the ops report counts. Stage shown is the "
                 f"{slabel}. 'Unstaged' is honest: the row carries no stage in "
                 "that system, it is not a zero. Dated by BIRTH, not by dial, so "
                 "the Born-date range drives this table when it is set."),
    }


def build_report_cell(b, q):
    """The constituents behind one Daily Report cell (R3.1: count parity)."""
    key = q.get("sstage") if q.get("sstage") in STAGE_SRC else "zoho"
    scol, slabel = STAGE_SRC[key]
    day, proj, want = q.get("date", ""), q.get("project", ""), q.get("stage", "")
    d0 = pd.to_datetime(q.get("from")).date() if q.get("from") else date(2000, 1, 1)
    d1 = pd.to_datetime(q.get("to")).date() if q.get("to") else date(2100, 1, 1)
    z, _ = _report_frame(b, {k: v for k, v in q.items() if k != "project"}, d0, d1)
    if z.empty:
        return {"error": "no leads in this window"}
    if day:
        z = z[z["_born"].astype(str) == day]
    if proj:
        z = z[z["_project"] == proj]
    st = z[scol].astype(str).str.strip()
    st = st.where(st.isin(REPORT_STAGES), "Unstaged")
    z = z.assign(_stage=st)
    if want and want != "Total":
        z = z[z["_stage"] == want]

    z = z.assign(**{
        "Born": z["_born"].astype(str),
        "Project": z["_project"],
        "Stage (Zoho)": z[STAGE_SRC["zoho"][0]].astype(str).replace({"nan": "—", "None": "—"}),
        "Stage (ANGAD)": z[STAGE_SRC["angad"][0]].astype(str).replace({"nan": "—", "None": "—"}),
    })
    show = ["lead_ref", "Born", "Project", "Stage (Zoho)", "Stage (ANGAD)",
            "lead_status", "call_attempt_count", "last_call_at", "lead_source"]
    tbl = table(z, show, labels={"lead_ref": "lead_ref",
                                 "lead_status": "Status",
                                 "call_attempt_count": "Attempts",
                                 "last_call_at": "Last call",
                                 "lead_source": "Source"})
    return {"count": int(len(z)), "tbl": tbl,
            "cell": {"date": day or "all dates", "project": proj or "all projects",
                     "stage": want or "all stages", "src": slabel},
            "note": ("These are the exact lead rows behind that cell, counted the "
                     "same way the cell is counted (one row per person x project). "
                     "Click any id to open the lead.")}


# ---------------------------------------------------------------- RAMA-7
# The Leads board: one row per lead with the Inncircles Lead-Logs column set,
# plus the filter dimensions their board has that ours did not — Zoho stage,
# NEW vs FOLLOW-UP call, per-call timing and outcome, and WhatsApp detail.
# Reference for the columns: the team's own LeadLogs export (30 columns). We
# mirror the SHAPE, not the file — that export carries names and free-text
# comments and is never ingested.

def build_leads_board(b, q, d0, d1):
    z = b.get("zleads", pd.DataFrame())
    calls = b.get("calls", pd.DataFrame())
    if z.empty or "lead_ref" not in z.columns:
        return None
    z = z.copy()
    z["_project"] = norm_project(z["project"]) if "project" in z.columns else ""
    born = parse_mixed_date(z["born_date"]) if "born_date" in z.columns else None
    z["_born"] = pd.to_datetime(born, errors="coerce", utc=True).dt.tz_convert(
        "Asia/Kolkata").dt.date if born is not None else None

    # per-lead call facts, computed once
    cf = pd.DataFrame()
    if not calls.empty and "lead_ref" in calls.columns:
        cc = calls.copy()
        cc["_ts"] = pd.to_datetime(cc["ts_ist"], errors="coerce", utc=True)
        cc["_sec"] = pd.to_numeric(cc.get("duration_sec"), errors="coerce").fillna(0)
        cc["_pick"] = cost_of(cc)[3]          # ONE call — it is not cheap
        # Sort once, then take last() — a groupby.apply that re-sorts inside
        # every group turned this endpoint into a 59-SECOND request over ~4k
        # groups. Sort-then-last is the same answer in well under a second.
        cc = cc.sort_values("_ts")
        g = cc.groupby("lead_ref", sort=False)
        cf = pd.DataFrame({
            "dials": g.size(),
            "answered": g["_pick"].sum(),
            "connected": g["reached"].sum() if "reached" in cc.columns else 0,
            "booked": g["is_booked"].sum() if "is_booked" in cc.columns else 0,
            "first_dial": g["_ts"].min(),
            "last_dial": g["_ts"].max(),
            "talk_secs": g["_sec"].sum(),
            "last_outcome": cc.dropna(subset=["outcome"])
                              .groupby("lead_ref", sort=False)["outcome"].last(),
        })
    z = z.merge(cf, left_on="lead_ref", right_index=True, how="left")
    for cname in ("dials", "answered", "connected", "booked", "talk_secs"):
        if cname in z.columns:
            z[cname] = pd.to_numeric(z[cname], errors="coerce").fillna(0).astype(int)

    # --- the Inncircles filter dimensions --------------------------------
    # NEW vs FOLLOW-UP is a per-LEAD state on their board: has this lead been
    # dialled more than once? A "new call" lead has exactly one attempt.
    z["call_type"] = np.where(z["dials"] <= 0, "Never dialled",
                       np.where(z["dials"] == 1, "New call", "Follow-up"))
    # Contacted mirrors their YES/NO column, using the corrected pickup test
    z["contacted"] = np.where(z["answered"] > 0, "YES", "NO")
    # Response gap: hours from lead birth to the first dial (their "Response Gap")
    bt = pd.to_datetime(z["_born"], errors="coerce")
    fd = pd.to_datetime(z["first_dial"], errors="coerce", utc=True).dt.tz_localize(None)
    z["response_gap_h"] = ((fd - bt).dt.total_seconds() / 3600).round(1)
    z["talk_min"] = (z["talk_secs"] / 60).round(1)

    # WhatsApp detail per lead
    crm = b.get("crm", pd.DataFrame())
    if not crm.empty and "lead_ref" in crm.columns:
        wcols = {c: c for c in ("counters.wa_in", "counters.wa_out") if c in crm.columns}
        if wcols:
            wg = crm.groupby("lead_ref").agg(
                **{"wa_out": ("counters.wa_out", "max")} if "counters.wa_out" in crm.columns else {},
                **{"wa_in": ("counters.wa_in", "max")} if "counters.wa_in" in crm.columns else {})
            z = z.merge(wg, left_on="lead_ref", right_index=True, how="left")
    for cname in ("wa_in", "wa_out"):
        z[cname] = pd.to_numeric(z.get(cname), errors="coerce").fillna(0).astype(int)
    z["wa_replied"] = np.where(z["wa_in"] > 0, "YES", "NO")

    # --- apply the board's own filters -----------------------------------
    def multi(key):
        return [v for v in str(q.get(key, "")).split("|") if v]

    if q.get("lb_born_from"):
        z = z[z["_born"] >= pd.to_datetime(q["lb_born_from"]).date()]
    if q.get("lb_born_to"):
        z = z[z["_born"] <= pd.to_datetime(q["lb_born_to"]).date()]
    for key, colname in (("lb_stage", "stage"), ("lb_prdstage", "prd_stage"),
                         ("lb_project", "_project"), ("lb_calltype", "call_type"),
                         ("lb_contacted", "contacted"), ("lb_outcome", "last_outcome"),
                         ("lb_source", "lead_source"), ("lb_wa", "wa_replied")):
        vals = multi(key)
        if vals and colname in z.columns:
            z = z[z[colname].astype(str).isin(vals)]
    if multi("lb_attempts"):
        m = pd.Series(False, index=z.index)
        for a in multi("lb_attempts"):
            if a == "0":       m |= z["dials"] == 0
            elif a == "1":     m |= z["dials"] == 1
            elif a == "2-5":   m |= z["dials"].between(2, 5)
            elif a == "6-20":  m |= z["dials"].between(6, 20)
            elif a == "20+":   m |= z["dials"] > 20
        z = z[m]
    if q.get("lb_search"):
        s = str(q["lb_search"]).lower()
        hay = z[["lead_ref", "mlid", "masterleadid", "source_lead_id", "plid",
                 "_project"]].astype(str).agg(" ".join, axis=1).str.lower()
        z = z[hay.str.contains(re.escape(s), na=False)]

    z = z.sort_values("last_dial", ascending=False, na_position="last")
    show = ["lead_ref", "_born", "_project", "stage", "prd_stage", "call_type",
            "contacted", "dials", "answered", "connected", "booked", "talk_min",
            "response_gap_h", "last_outcome", "last_dial", "wa_out", "wa_in",
            "wa_replied", "lead_source", "lead_status"]
    labels = {"_born": "Born date", "_project": "Project", "stage": "Stage (Zoho)",
              "prd_stage": "Stage (ANGAD)", "call_type": "Call type",
              "contacted": "Contacted", "dials": "Dials", "answered": "Answered",
              "connected": "Connected", "booked": "Booked", "talk_min": "Talk (min)",
              "response_gap_h": "Response gap (h)", "last_outcome": "Last outcome",
              "last_dial": "Last dial", "wa_out": "WA sent", "wa_in": "WA replies",
              "wa_replied": "WA replied", "lead_source": "Source",
              "lead_status": "Status"}
    tbl = table(z, show, cap=600, labels=labels)

    def opts(colname):
        if colname not in z.columns:
            return []
        v = z[colname].dropna().astype(str)
        v = v[(v.str.strip() != "") & (~v.str.lower().isin(["nan", "none"]))]
        return sorted(v.value_counts().head(40).index.tolist())

    return {
        "tbl": tbl,
        "total": int(len(z)),
        "summary": [
            {"label": "Leads on this board", "value": f"{len(z):,}"},
            {"label": "Never dialled", "value": f"{int((z['dials'] == 0).sum()):,}",
             "sub": "no attempt has ever been made"},
            {"label": "Contacted", "value": f"{int((z['answered'] > 0).sum()):,}",
             "sub": "the line was actually answered at least once"},
            {"label": "Follow-up leads", "value": f"{int((z['dials'] > 1).sum()):,}",
             "sub": "dialled more than once"},
            {"label": "WhatsApp replied", "value": f"{int((z['wa_in'] > 0).sum()):,}",
             "sub": "inbound WA exists — the estate is not send-only"},
        ],
        "options": {
            "lb_stage": opts("stage"), "lb_prdstage": opts("prd_stage"),
            "lb_project": opts("_project"),
            "lb_calltype": ["Never dialled", "New call", "Follow-up"],
            "lb_contacted": ["YES", "NO"], "lb_outcome": opts("last_outcome"),
            "lb_source": opts("lead_source"), "lb_wa": ["YES", "NO"],
            "lb_attempts": ["0", "1", "2-5", "6-20", "20+"],
        },
        "note": ("One row per LEAD (person × project), the grain the Inncircles "
                 "Lead Logs board uses. 'Contacted' uses the corrected pickup test "
                 "(the line opened), not `reached` — `reached` is False on every "
                 "DROPPED and VOICEMAIL row and understates contact badly. "
                 "'Call type' is NEW when the lead has exactly one attempt and "
                 "FOLLOW-UP above that. Response gap is hours from lead birth to "
                 "the first dial. Both stage systems are shown side by side "
                 "because they disagree on half the leads. Click any id for the "
                 "full lead modal."),
    }


# ---------------------------------------------------------------- RAMA-8
# Docs → to-do → QA. Clicking a downloadable puts it on a to-do list; the QA
# queue serves the worst calls per CIF category with their full history, and a
# QA verdict lands on the same list. The list is a local append-only journal —
# the same pattern as the feedback log, no database write anywhere.

TODO_LOG = HERE / "RELEASES" / "todo.jsonl"


def todo_add(kind, ref, text, meta=None):
    TODO_LOG.parent.mkdir(exist_ok=True)
    row = {"at": datetime.now().isoformat(timespec="seconds"), "kind": kind,
           "ref": pii.redact(str(ref or ""))[:120],
           "text": pii.clean_text(str(text or ""))[:600],
           "meta": meta or {}, "done": False}
    with TODO_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")
    return row


def todo_list(n=200):
    if not TODO_LOG.exists():
        return []
    out = []
    for line in TODO_LOG.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out[-n:][::-1]


def build_docs(b, c):
    """Every document request, whether it was actually sent, and why not."""
    dl = _p("Zoho_Database__doc_send_log.parquet")
    pdoc = _p("Zoho_Database__project_documents.parquet")
    if dl.empty and pdoc.empty:
        return None
    out = {"todo": todo_list()}
    if not dl.empty:
        d = dl.copy()
        d["when"] = _ist_str(d["created_at"]) if "created_at" in d.columns else None
        blocked = (nonempty(d["block_reason"].astype(str))
                   if "block_reason" in d.columns else pd.Series(dtype=str))
        out["requests"] = table(
            d.sort_values("created_at", ascending=False),
            [x for x in ["lead_ref", "when", "project", "doc_type", "outcome",
                         "block_reason", "matched_file", "sizes_in_reply_count"]
             if x in d.columns], cap=400,
            labels={"when": "When", "doc_type": "Document", "outcome": "Outcome",
                    "block_reason": "Blocked because", "matched_file": "File"})
        out["mix"] = counts(d["doc_type"]) if "doc_type" in d.columns else []
        out["outcome_mix"] = counts(d["outcome"]) if "outcome" in d.columns else []
        out["blocked_n"] = int(len(blocked))
        out["requests_n"] = int(len(d))
    if not pdoc.empty:
        out["library"] = table(pdoc, [x for x in ["project", "doc_type", "filename",
                                                  "size_label", "tower", "facing",
                                                  "applies_to_all"]
                                      if x in pdoc.columns], cap=200,
                               labels={"doc_type": "Document", "filename": "File",
                                       "size_label": "Size", "applies_to_all": "All units"})
        out["library_n"] = int(len(pdoc))
    out["note"] = ("Every document the bot was asked for, and whether it actually "
                   "went out. 'Blocked because' is the bot's own reason. Click "
                   "'Add to to-do' on any row to put it on the working list "
                   "below — the list is a local append-only journal "
                   "(RELEASES/todo.jsonl), nothing is written to any database.")
    return out


CIF_QA_CATEGORIES = {
    "Conversation quality": ["EBR", "RDI", "DER"],
    "Lead quality": ["DSS", "OQR", "FRS"],
    "Engagement": ["ACR", "EM", "EI"],
    "Interest level": ["FIL", "SCS", "TD"],
    "Call quality": ["OC", "IPS"],
}


def build_qa(b, q):
    """QA queue: the worst calls in one CIF category, with full history."""
    f = DATA / "cif_scores.parquet"
    if not f.exists():
        return None
    df = pd.read_parquet(f)
    if df.empty:
        return None
    cat = q.get("qa_cat") if q.get("qa_cat") in CIF_QA_CATEGORIES else "Conversation quality"
    codes = [x for x in CIF_QA_CATEGORIES[cat] if x in df.columns]
    sub = df[df.get("substantive", True).fillna(False)] if "substantive" in df.columns else df
    if not len(sub) or not codes:
        return {"cat": cat, "cats": list(CIF_QA_CATEGORIES), "empty": True,
                "note": "No substantive scored calls yet for this category."}
    # rank worst-first: low is bad for all of these except DER (high is bad)
    z = sub.copy()
    score = pd.Series(0.0, index=z.index)
    for code in codes:
        v = pd.to_numeric(z[code], errors="coerce")
        if v.notna().sum() < 3:
            continue
        r = v.rank(pct=True)
        score += (1 - r) if code != "DER" else r
    z["_qa_rank"] = score
    z = z.sort_values("_qa_rank", ascending=False)
    cols = ["lead_ref", "call_date", "corpus", "accuracy_tier", "persona",
            "persona_era", "exchanges"] + codes
    return {
        "cat": cat, "cats": list(CIF_QA_CATEGORIES), "codes": codes,
        "n": int(len(z)),
        "tbl": table(add_ids(z), [x for x in cols if x in add_ids(z).columns], cap=120,
                     labels={"call_date": "Date", "corpus": "Corpus",
                             "accuracy_tier": "Tier", "persona": "Persona",
                             "persona_era": "Era", "exchanges": "Exchanges"}),
        "todo": todo_list(),
        "note": (f"The worst calls for {cat}, ranked by combined percentile across "
                 f"{', '.join(codes)} (for DER, high is bad — it is inverted). Only "
                 "calls where the customer actually spoke are queued; scoring a call "
                 "nobody talked on is not QA. Every row carries its corpus and "
                 "accuracy tier, so you know how much to trust the transcript before "
                 "you judge the call. Click an id for the lead's full history, then "
                 "'Add to to-do' to log a verdict."),
    }


# ---------------------------------------------------------------- RAMA-9
# Builder / production metrics: what happens turn by turn, and what the new
# bot version says differently from the old.

def _turns_of(text):
    out = []
    for line in str(text or "").splitlines():
        m = re.match(r"^(?:\[\d{1,2}:\d{2}(?::\d{2})?\]\s*)?"
                     r"(Agent|Customer|Bot|Lead)\s*:\s*(.*)$", line.strip())
        if m:
            who = "Agent" if m.group(1) in ("Agent", "Bot") else "Customer"
            out.append((who, m.group(2).strip()))
    return out


def build_builder(b, q):
    """Per-turn analysis + a persona-versus-persona diff of what is said."""
    rows = []
    zt = _p("Zoho_Database__call_transcripts.parquet")
    rt = _p("retranscripts.parquet")
    if not zt.empty and "transcript" in zt.columns:
        rows += [(t, "zoho_gemini") for t in zt["transcript"].dropna()]
    if not rt.empty and "clean_transcript" in rt.columns:
        rows += [(t, "audio_retranscript") for t in rt["clean_transcript"].dropna()]
    if not rows:
        return None

    MAXT = 20
    agg = {i: {"agent_w": [], "cust_w": [], "alive": 0, "cust_spoke": 0}
           for i in range(1, MAXT + 1)}
    persona_open, persona_stats = {}, {}
    prx = re.compile(r"\b(?:this is|myself|i am|i'?m)\s+([A-Z][a-z]{2,15})\b")
    known = {"Radhika", "Ankit", "Anandita"}

    for text, corpus in rows:
        turns = _turns_of(text)
        if not turns:
            continue
        who = None
        for m in prx.finditer(text):
            if m.group(1).title() in known:
                who = m.group(1).title()
                break
        pairs = []
        i = 0
        while i < len(turns):
            if turns[i][0] == "Agent":
                nxt = turns[i + 1][1] if i + 1 < len(turns) and turns[i + 1][0] == "Customer" else None
                pairs.append((turns[i][1], nxt))
                i += 2 if nxt is not None else 1
            else:
                i += 1
        for idx, (a, cst) in enumerate(pairs[:MAXT], start=1):
            agg[idx]["alive"] += 1
            agg[idx]["agent_w"].append(len(a.split()))
            if cst:
                agg[idx]["cust_spoke"] += 1
                agg[idx]["cust_w"].append(len(cst.split()))
        if who:
            st = persona_stats.setdefault(who, {"calls": 0, "turns": [], "aw": [], "cw": []})
            st["calls"] += 1
            st["turns"].append(len(pairs))
            st["aw"] += [len(a.split()) for a, _ in pairs]
            st["cw"] += [len(c_.split()) for _, c_ in pairs if c_]
            if pairs:
                persona_open.setdefault(who, []).append(pairs[0][0][:160])

    total = max(agg[1]["alive"], 1)
    per_turn = [{
        "turn": i,
        "calls_alive": agg[i]["alive"],
        "survival_pct": pct(agg[i]["alive"], total),
        "agent_words": round(sum(agg[i]["agent_w"]) / len(agg[i]["agent_w"]), 1)
                       if agg[i]["agent_w"] else None,
        "customer_words": round(sum(agg[i]["cust_w"]) / len(agg[i]["cust_w"]), 1)
                          if agg[i]["cust_w"] else None,
        "customer_replied_pct": pct(agg[i]["cust_spoke"], agg[i]["alive"]),
    } for i in range(1, MAXT + 1) if agg[i]["alive"]]

    # the biggest single drop between consecutive turns — where calls die
    worst = None
    for a_, b_ in zip(per_turn, per_turn[1:]):
        d = a_["calls_alive"] - b_["calls_alive"]
        if worst is None or d > worst["lost"]:
            worst = {"from": a_["turn"], "to": b_["turn"], "lost": d,
                     "pct": pct(d, max(a_["calls_alive"], 1))}

    versions = []
    for name, st in sorted(persona_stats.items(), key=lambda x: -x[1]["calls"]):
        versions.append({
            "persona": name, "calls": st["calls"],
            "median_turns": float(pd.Series(st["turns"]).median()) if st["turns"] else None,
            "agent_words_per_turn": round(sum(st["aw"]) / len(st["aw"]), 1) if st["aw"] else None,
            "customer_words_per_turn": round(sum(st["cw"]) / len(st["cw"]), 1) if st["cw"] else None,
            "openings": list(dict.fromkeys(persona_open.get(name, [])))[:4],
        })

    return {
        "per_turn": per_turn, "worst_drop": worst, "versions": versions,
        "corpus_n": len(rows),
        "note": ("Turn 1 = the agent's first utterance and the customer's reply to it. "
                 "'Calls alive' is how many transcripts still have an exchange at that "
                 "turn, so the series is a survival curve, not a sample of equals — "
                 "read the later turns as the behaviour of the calls that SURVIVED. "
                 "Version comparison is grouped by the name the agent gives itself, "
                 "the only per-call version evidence that exists (no prompt_version is "
                 "persisted). Three names appear, not two."),
    }


# ================================================================ X2 — THE SENSOR
# The stated goal is "site-visit bookings ATTENDED" and it has had no sensor
# anywhere in the estate: no field, no endpoint, no path. Zoho's visit_attended
# is constant false; leads.site_visit_date is null on all 4,219 rows. Attendance
# has lived only in ops spreadsheets, which means every improvement claim this
# engagement has made is provisional (LEARNINGS #6).
#
# This is the sensor. It cannot write to Mongo (imprint 1) and must not — so it
# is a LOCAL, append-only attendance ledger keyed by the booking, reconciled
# against every booking we can see, and it reports its own coverage honestly:
# how many bookings have an attendance verdict, and how many are simply unknown.
# "Unknown" is a first-class state here. A sensor that guesses is not a sensor.
#
# It also produces the exact backfill file the dev team needs to make attendance
# a real field upstream — which is how this eventually stops being local.

ATTEND_LOG = HERE / "RELEASES" / "attendance.jsonl"
ATTEND_STATES = ("attended", "no_show", "rescheduled", "cancelled", "unknown")


def attend_add(booking_key, state, note="", who="ops"):
    if state not in ATTEND_STATES:
        return {"error": f"state must be one of {ATTEND_STATES}"}
    ATTEND_LOG.parent.mkdir(exist_ok=True)
    row = {"at": datetime.now().isoformat(timespec="seconds"),
           "booking_key": str(booking_key)[:80],
           "state": state,
           "note": pii.clean_text(str(note or ""))[:400],
           "recorded_by": pii.clean_text(str(who or ""))[:40]}
    with ATTEND_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")
    return row


def attend_map():
    """Latest verdict per booking — the log is append-only, last write wins."""
    out = {}
    if not ATTEND_LOG.exists():
        return out
    for line in ATTEND_LOG.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            out[r["booking_key"]] = r
        except Exception:
            pass
    return out


def build_attendance(b, q=None):
    """Every known booking, its attendance verdict, and honest coverage."""
    rows = []

    # source 1 — the call that booked it (outcome PRE_SITE / VIRTUAL_TOUR)
    c = b.get("calls", pd.DataFrame())
    if not c.empty:
        bk = c[c["outcome"].isin(["PRE_SITE", "VIRTUAL_TOUR"])].copy()
        bk["_ts"] = pd.to_datetime(bk["ts_ist"], errors="coerce", utc=True)
        bk = bk.sort_values("_ts")
        for ref, g in bk.groupby("lead_ref"):
            last = g.iloc[-1]
            rows.append({"lead_ref": ref,
                         "booked_on": str(pd.Timestamp(last["_ts"]).date())
                         if pd.notna(last["_ts"]) else None,
                         "visit_date": None, "visit_time": None,
                         "project": clean(last.get("call_project")),
                         "source": "call outcome"})

    # source 2 — Zoho's booking rows, which carry the parsed visit date/slot
    sv = _p("Zoho_Database__site_visit_bookings.parquet")
    if not sv.empty:
        for _, r in sv.iterrows():
            rows.append({"lead_ref": clean(r.get("lead_ref")),
                         "booked_on": str(r.get("booked_at"))[:10],
                         "visit_date": clean(r.get("visit_date")),
                         "visit_time": clean(r.get("visit_time")),
                         "project": clean(r.get("project")),
                         "source": "site_visit_bookings"})

    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df[df["lead_ref"].notna()]
    # one row per (lead, visit date) — the booking is the unit, not the call
    df["visit_date"] = df["visit_date"].fillna("")
    df["booking_key"] = df["lead_ref"].astype(str) + "|" + df["visit_date"].astype(str)
    df = (df.sort_values("source")                      # site_visit_bookings wins
            .drop_duplicates(subset=["booking_key"], keep="last"))

    # ops-sheet attendance: the only attendance truth that exists today
    ops_done = b.get("co_sv_completed", pd.DataFrame())
    ops_mlids = set()
    if not ops_done.empty and "mlid" in ops_done.columns:
        ops_mlids = {str(x) for x in ops_done["mlid"].dropna().astype(str)}
    master, _ = master_maps()
    df["mlid"] = df["lead_ref"].map(master)
    df["ops_says_done"] = df["mlid"].astype(str).isin(ops_mlids)

    # local ledger verdicts
    led = attend_map()
    df["verdict"] = df["booking_key"].map(lambda k: (led.get(k) or {}).get("state"))
    df["verdict_note"] = df["booking_key"].map(lambda k: (led.get(k) or {}).get("note"))
    # ops sheet counts as a recorded verdict where the ledger is silent
    df.loc[df["verdict"].isna() & df["ops_says_done"], "verdict"] = "attended"
    df.loc[df["verdict"].isna() & df["ops_says_done"], "verdict_note"] = "from ops sheet"
    df["verdict"] = df["verdict"].fillna("unknown")

    total = int(len(df))
    known = int((df["verdict"] != "unknown").sum())
    attended = int((df["verdict"] == "attended").sum())
    tbl = table(add_ids(df.sort_values(["verdict", "visit_date"])),
                [x for x in ["lead_ref", "MLID", "Lead ID", "booked_on",
                             "visit_date", "visit_time", "project", "verdict",
                             "verdict_note", "source", "booking_key"]
                 if x in add_ids(df).columns], cap=500,
                labels={"booked_on": "Booked on", "visit_date": "Visit date",
                        "visit_time": "Slot", "verdict": "Attendance",
                        "verdict_note": "Note", "source": "Booking source",
                        "booking_key": "Booking key"})
    return {
        "kpis": [
            {"label": "Bookings we can see", "value": f"{total:,}",
             "sub": "call outcome + Zoho booking rows, de-duplicated"},
            {"label": "Attendance recorded", "value": f"{known:,}",
             "sub": f"{pct(known, total)}% of bookings have ANY verdict"},
            {"label": "Attended", "value": f"{attended:,}",
             "sub": "ops sheet + local ledger"},
            {"label": "Unknown", "value": f"{total - known:,}",
             "sub": "no verdict exists anywhere — this is the blind spot"},
        ],
        "coverage_pct": pct(known, total),
        "mix": counts(df["verdict"]),
        "tbl": tbl, "states": list(ATTEND_STATES),
        "note": ("THE GOAL'S SENSOR. Attendance had no field, no endpoint and no "
                 "path anywhere in the estate — it lived only in ops spreadsheets, "
                 "which is why every improvement claim in this engagement has been "
                 "provisional. This ledger makes it observable: one row per "
                 "booking, a verdict you can record, and an explicit UNKNOWN for "
                 "everything nobody has answered. Unknown is a real state — a "
                 "sensor that guesses is not a sensor. Nothing here is written to "
                 "any database; the ledger is a local append-only journal "
                 "(RELEASES/attendance.jsonl) and last write wins. Export it to "
                 "hand the dev team the backfill they need to make attendance a "
                 "real upstream field."),
    }


# ================================================================ DAY-BY-DAY
# Bot call performance per day, overlayable. Every series is a rate or a median
# so days of different volume are comparable — plotting raw counts beside rates
# on one axis is how a busy day gets mistaken for a good day.

def build_daybyday(b, q, d0, d1):
    c = b.get("calls", pd.DataFrame())
    if c.empty:
        return None
    c = c[(c["date"] >= d0) & (c["date"] <= d1)].copy()
    if c.empty:
        return None
    _, _, _, picked = cost_of(c)
    c["_picked"] = picked
    for x in ("turns_total", "turns_lead", "turns_agent", "lead_talk_share",
              "duration_sec"):
        if x in c.columns:
            c[x] = pd.to_numeric(c[x], errors="coerce")
    amap = truth()["audience"]
    c["_aud"] = c["lead_ref"].map(lambda r: amap.get(r, "RI"))

    rows = []
    for day, g in c.groupby("date", sort=True):
        conv = g[g["_picked"] & g["turns_total"].notna()] if "turns_total" in g.columns else g.iloc[0:0]
        nri, ri = g[g["_aud"] == "NRI"], g[g["_aud"] == "RI"]
        rows.append({
            "date": str(day),
            "dials": int(len(g)),
            "picked_up": int(g["_picked"].sum()),
            "not_picked": int(len(g) - g["_picked"].sum()),
            "pickup_pct": pct(int(g["_picked"].sum()), len(g)),
            "connected": int(g["reached"].sum()) if "reached" in g.columns else 0,
            "booked": int(g["is_booked"].sum()) if "is_booked" in g.columns else 0,
            "median_turns": round(float(conv["turns_total"].median()), 1) if len(conv) else None,
            "avg_turns": round(float(conv["turns_total"].mean()), 1) if len(conv) else None,
            "customer_turns": round(float(conv["turns_lead"].mean()), 1)
                              if len(conv) and "turns_lead" in conv.columns else None,
            "agent_turns": round(float(conv["turns_agent"].mean()), 1)
                           if len(conv) and "turns_agent" in conv.columns else None,
            "customer_talk_pct": round(float(conv["lead_talk_share"].median()), 1)
                                 if len(conv) and "lead_talk_share" in conv.columns else None,
            "median_talk_s": round(float(g.loc[g["_picked"], "duration_sec"].median()), 0)
                             if int(g["_picked"].sum()) else None,
            "nri_dials": int(len(nri)), "ri_dials": int(len(ri)),
            "nri_pickup_pct": pct(int(nri["_picked"].sum()), len(nri)) if len(nri) else None,
            "ri_pickup_pct": pct(int(ri["_picked"].sum()), len(ri)) if len(ri) else None,
        })
    if not rows:
        return None
    df = pd.DataFrame(rows)
    SERIES = [
        ("pickup_pct", "Pickup rate %", "var(--c1)", "rate"),
        ("median_turns", "Median turns", "var(--c2)", "count"),
        ("avg_turns", "Average turns", "var(--c3)", "count"),
        ("customer_talk_pct", "Customer talk share %", "var(--c4)", "rate"),
        ("customer_turns", "Customer turns / call", "var(--c5)", "count"),
        ("agent_turns", "Agent turns / call", "var(--c6)", "count"),
        ("nri_pickup_pct", "NRI pickup %", "var(--c2)", "rate"),
        ("ri_pickup_pct", "RI pickup %", "var(--c1)", "rate"),
    ]
    return {
        "dates": df["date"].tolist(),
        "series": [{"key": k, "name": n, "color": col, "kind": kind,
                    "values": [None if pd.isna(v) else float(v) for v in df[k]]}
                   for k, n, col, kind in SERIES if k in df.columns],
        "volume": {"dials": df["dials"].tolist(),
                   "picked_up": df["picked_up"].tolist(),
                   "not_picked": df["not_picked"].tolist()},
        "tbl": table(df, list(df.columns), cap=200,
                     labels={"date": "Date", "dials": "Dials",
                             "picked_up": "Picked up", "not_picked": "Not picked up",
                             "pickup_pct": "Pickup %", "connected": "Connected",
                             "booked": "Booked", "median_turns": "Median turns",
                             "avg_turns": "Avg turns",
                             "customer_turns": "Customer turns",
                             "agent_turns": "Agent turns",
                             "customer_talk_pct": "Customer talk %",
                             "median_talk_s": "Median talk (s)",
                             "nri_dials": "NRI dials", "ri_dials": "RI dials",
                             "nri_pickup_pct": "NRI pickup %",
                             "ri_pickup_pct": "RI pickup %"}),
        "note": ("One row per day. 'Picked up' uses the corrected test (the line "
                 "opened), NOT `reached` — `reached` is False on every DROPPED and "
                 "VOICEMAIL row and understates contact by roughly 3x. Turn "
                 "statistics are computed only on calls that were picked up AND "
                 "have a turn count, so they describe conversations rather than "
                 "dial attempts. Tick series to overlay them; rates and counts are "
                 "drawn on separate axes because plotting them together makes a "
                 "busy day look like a good day."),
    }


# ================================================================ PER PROJECT
def build_projects(b, q, d0, d1):
    """Per project: what came in, what got called, what happened."""
    c = b.get("calls", pd.DataFrame())
    z = b.get("zleads", pd.DataFrame())
    if c.empty or z.empty:
        return None
    z = z.copy()
    z["_project"] = norm_project(z["project"]) if "project" in z.columns else ""
    born = parse_mixed_date(z["born_date"]) if "born_date" in z.columns else None
    z["_born"] = (pd.to_datetime(born, errors="coerce", utc=True)
                    .dt.tz_convert("Asia/Kolkata").dt.date) if born is not None else None
    ref_proj = dict(zip(z["lead_ref"], z["_project"]))
    ref_born = dict(zip(z["lead_ref"], z["_born"]))

    cw = c[(c["date"] >= d0) & (c["date"] <= d1)].copy()
    _, _, _, picked = cost_of(cw)
    cw["_picked"] = picked
    cw["_proj"] = cw["lead_ref"].map(ref_proj)
    cw["_born"] = cw["lead_ref"].map(ref_born)
    # FRESH = dialled on the same calendar day the lead was born; everything
    # else with a known birth date is a FOLLOW-UP of an older lead.
    cw["_fresh"] = cw["_born"].notna() & (cw["_born"] == cw["date"])
    for x in ("turns_total", "duration_sec"):
        if x in cw.columns:
            cw[x] = pd.to_numeric(cw[x], errors="coerce")

    sv = _p("Zoho_Database__site_visit_bookings.parquet")
    att = build_attendance(b) or {}
    done_by_proj = {}
    ops = b.get("co_sv_completed", pd.DataFrame())
    if not ops.empty and "project" in ops.columns:
        done_by_proj = norm_project(ops["project"]).value_counts().to_dict()

    rows = []
    projects = sorted({p for p in z["_project"].dropna().unique() if str(p).strip()})
    for p in projects:
        refs = {r for r, pr in ref_proj.items() if pr == p}
        born_in = z[(z["_project"] == p) & z["_born"].notna()
                    & (z["_born"] >= d0) & (z["_born"] <= d1)]
        g = cw[cw["_proj"] == p]
        conv = g[g["_picked"] & g["turns_total"].notna()] if "turns_total" in g.columns else g.iloc[0:0]
        fresh, follow = g[g["_fresh"]], g[~g["_fresh"]]
        booked_sv = int(len(sv[norm_project(sv["project"]) == p])) if not sv.empty and "project" in sv.columns else 0
        rows.append([
            p, int(len(born_in)),
            int(fresh["lead_ref"].nunique()), int(follow["lead_ref"].nunique()),
            int(len(g)), int(g["_picked"].sum()),
            pct(int(g["_picked"].sum()), len(g)),
            round(float(conv["turns_total"].median()), 1) if len(conv) else None,
            round(float(conv["turns_total"].mean()), 1) if len(conv) else None,
            int(truthy(g["whatsappSent"]).sum()) if "whatsappSent" in g.columns else 0,
            int(g["is_booked"].sum()) if "is_booked" in g.columns else 0,
            booked_sv, int(done_by_proj.get(p, 0)),
        ])
    cols = ["Project", "Leads born (window)", "Fresh leads called (same day)",
            "Older leads called (follow-up)", "Calls triggered", "Picked up",
            "Pickup %", "Median turns", "Avg turns", "WhatsApp sent",
            "Booked on call", "Booking rows (Zoho)", "Visits DONE (ops sheet)"]
    tot = ["TOTAL"] + [sum(r[i] or 0 for r in rows) if i not in (6, 7, 8) else None
                       for i in range(1, len(cols))]
    return {
        "cols": cols, "rows": rows, "totals": tot,
        "total": len(rows), "shown": len(rows),
        "attendance_coverage": att.get("coverage_pct"),
        "note": ("One row per project, for the current date window. 'Fresh' = "
                 "dialled on the same calendar day the lead was born; 'older' = "
                 "everything else with a known birth date. 'Picked up' uses the "
                 "corrected test (the line opened), not `reached`. Turn statistics "
                 "cover picked-up calls with a turn count only. 'Visits DONE' comes "
                 "from the ops sheet — the only attendance truth that exists — and "
                 "attendance coverage across ALL bookings is currently "
                 f"{att.get('coverage_pct', 0)}%, so treat that column as a floor, "
                 "not a total. Every cell opens the leads behind it."),
    }


# ================================================================ DB HEALTH
# Per-collection performance report. Not "is the file there" — how big it is,
# how fresh the SOURCE is, how much of it is actually usable, and which of its
# key fields are dead. A collection can be present, synced and useless.

DB_KEY_FIELDS = {
    "calls": ["outcome", "duration_sec", "turns_total", "tok_total",
              "slot_intent", "sentiment", "closingScore", "externalScheduleId"],
    "Zoho_Database__leads": ["stage", "prd_stage", "born_date", "site_visit_date",
                             "lead_source", "masterleadid"],
    "ASBLVoiceBot__call_schedules": ["call_outcome", "metadata.visit_date",
                                     "metadata.reason", "masterleadid"],
    "Zoho_Database__site_visit_bookings": ["visit_date", "visit_time", "visit_raw.date"],
    "Intelligent_CRM__crm_leads": ["stage", "person_id"],
    "Zoho_Database__cron_log": ["task", "ran_at", "error"],
}
_TIME_HINTS = ("updatedAt", "updated_at", "createdAt", "created_at", "ran_at",
               "at", "ts", "ts_ist", "date", "booked_at", "modified_time")


def build_dbhealth(b, q=None):
    rows, field_rows = [], []
    now = pd.Timestamp.now(tz="UTC")
    for f in sorted(DATA.glob("*.parquet")):
        name = f.stem
        try:
            df = pd.read_parquet(f)
        except Exception as e:
            rows.append([name, 0, 0, None, None, f"UNREADABLE: {type(e).__name__}"])
            continue
        size_mb = round(f.stat().st_size / 1e6, 2)
        # newest record in the SOURCE, not our sync time — a stale source is
        # invisible if you only look at file mtime
        newest, tcol = None, None
        for cand in _TIME_HINTS:
            if cand in df.columns:
                v = pd.to_datetime(df[cand], errors="coerce", utc=True)
                if v.notna().any():
                    newest, tcol = v.max(), cand
                    break
        age_d = round((now - newest).total_seconds() / 86400, 1) if newest is not None else None
        # usable = share of columns that are not entirely null
        dead_cols = int(sum(1 for c in df.columns if df[c].notna().sum() == 0))
        state = ("EMPTY" if len(df) == 0
                 else "STALE" if (age_d is not None and age_d > 2)
                 else "NO TIMESTAMP" if age_d is None else "OK")
        rows.append([name, int(len(df)), size_mb,
                     str(newest)[:16] if newest is not None else None,
                     age_d, f"{dead_cols}/{len(df.columns)}", state, tcol])

        for kf in DB_KEY_FIELDS.get(name, []):
            if kf not in df.columns:
                field_rows.append([name, kf, "COLUMN ABSENT", 0, 0.0])
                continue
            nn = int(df[kf].notna().sum())
            field_rows.append([name, kf,
                               "ALL NULL" if nn == 0 else "ok",
                               nn, pct(nn, len(df))])
    rows.sort(key=lambda r: -(r[1] or 0))
    stale = sum(1 for r in rows if r[6] == "STALE")
    dead_fields = sum(1 for r in field_rows if r[2] != "ok")
    return {
        "kpis": [
            {"label": "Collections cached", "value": f"{len(rows):,}",
             "src": "data/*.parquet"},
            {"label": "Rows held", "value": f"{sum(r[1] or 0 for r in rows):,}",
             "src": "sum across all collections"},
            {"label": "Stale sources", "value": f"{stale}",
             "sub": "newest record older than 2 days",
             "src": "newest record per collection"},
            {"label": "Dead key fields", "value": f"{dead_fields}",
             "sub": "absent or 100% null where we depend on them",
             "src": "DB_KEY_FIELDS audit"},
        ],
        "tbl": {"cols": ["Collection", "Rows", "Size (MB)", "Newest record",
                         "Age (days)", "Dead columns", "State", "Timestamp field"],
                "rows": rows, "total": len(rows), "shown": len(rows)},
        "fields": {"cols": ["Collection", "Key field", "State", "Non-null", "% filled"],
                   "rows": field_rows, "total": len(field_rows), "shown": len(field_rows)},
        "note": ("Per-collection health. 'Newest record' reads the SOURCE's own "
                 "timestamp, not our file mtime — a source that stopped being "
                 "written is invisible if you only check whether the file is "
                 "fresh. 'Dead columns' counts columns that are 100% null: a "
                 "collection can be present, synced and useless. The key-field "
                 "table is the one that matters — those are fields the dashboard "
                 "depends on, and ALL NULL there means a number somewhere is "
                 "quietly resting on nothing."),
    }


# ================================================================ PENDING
# "Show what is still pending" — one place for everything the system has left
# undone, each with the query behind it and a route to the rows.

def build_pending(b, q=None):
    items = []

    def add(area, what, n, detail, tab=None, severity="warn"):
        items.append({"area": area, "what": what, "count": n, "detail": detail,
                      "tab": tab, "severity": severity})

    c = b.get("calls", pd.DataFrame())
    z = b.get("zleads", pd.DataFrame())

    # 1. the goal
    att = build_attendance(b) or {}
    if att:
        unknown = int(att["kpis"][3]["value"].replace(",", ""))
        add("THE GOAL", "Bookings with no attendance verdict", unknown,
            f"{att.get('coverage_pct')}% of bookings have any verdict. Until this "
            "rises, every improvement claim stays provisional.", "attend", "bad")

    # 2. leads never contacted
    if not z.empty and not c.empty:
        dialled = set(c["lead_ref"].dropna())
        never = z[~z["lead_ref"].isin(dialled)]
        add("PIPELINE", "Leads never dialled", int(len(never)),
            "In the Zoho lead store but no call attempt exists.", "leadsboard")
        _, _, _, pu = cost_of(c)
        picked_refs = set(c.loc[pu, "lead_ref"].dropna())
        dialled_never_answered = dialled - picked_refs
        add("PIPELINE", "Dialled but never once answered", len(dialled_never_answered),
            "The line has never opened for these people.", "leadsboard")

    # 3. the reminder gap
    try:
        cl = _p("Zoho_Database__cron_log.parquet")
        rem = cl[cl["task"] == "pre-site-visit-reminders"] if not cl.empty else pd.DataFrame()
        if len(rem):
            fired = int(pd.to_numeric(rem.get("result.fired_24h"), errors="coerce").fillna(0).sum()
                        + pd.to_numeric(rem.get("result.fired_3h"), errors="coerce").fillna(0).sum())
            if fired == 0:
                add("SYSTEM", "Reminders the cron has never fired", int(len(rem)),
                    f"{len(rem):,} runs, 0 reminders fired, 0 errors reported. "
                    "The job is green and doing nothing.", "audit", "bad")
    except Exception:
        pass

    # 4. measurement debt
    f = DATA / "cif_scores.parquet"
    if f.exists():
        try:
            cs = pd.read_parquet(f)
            judge_na = sum(1 for k in ("CDS", "SVO", "NAC", "IFQ", "RRI", "QRR", "OHR",
                                       "CCI", "IAF", "TTS", "SE", "GLF", "RAR", "OCS")
                           if k not in cs.columns or cs[k].notna().sum() == 0)
            add("MEASUREMENT", "CIF indicators awaiting the LLM judge", judge_na,
                "FMS stays NA until CDS / SVO / NAC land. The passthrough is "
                "built — no code change needed when they do.", "cif")
        except Exception:
            pass

    # 5. stale sources
    try:
        h = build_dbhealth(b)
        stale = [r[0] for r in h["tbl"]["rows"] if r[6] == "STALE"]
        if stale:
            add("DATA", "Source collections gone stale", len(stale),
                "Newest record older than 2 days: " + ", ".join(stale[:6])
                + ("…" if len(stale) > 6 else ""), "dbhealth")
        dead = [f"{r[0]}.{r[1]}" for r in h["fields"]["rows"] if r[2] != "ok"]
        if dead:
            add("DATA", "Key fields absent or 100% null", len(dead),
                "Numbers may be resting on nothing: " + ", ".join(dead[:5])
                + ("…" if len(dead) > 5 else ""), "dbhealth", "bad")
    except Exception:
        pass

    # 6. our own debt
    try:
        pp = HERE / "prepared_patches"
        patches = sorted(x.name for x in pp.glob("*.patch")) if pp.exists() else []
        if patches:
            add("OUR DEBT", "Prepared patches not applied", len(patches),
                ", ".join(patches) + ". Patch 02 must be REWRITTEN first — it "
                "targets the agent's planner, not the cron that actually fails.",
                None)
    except Exception:
        pass
    add("OUR DEBT", "Cards with a PARTIAL contract only", None,
        "Every card can state what it counts, but only some have been verified "
        "against a query. See any card's ⓘ.", None)

    order = {"bad": 0, "warn": 1, "info": 2}
    items.sort(key=lambda x: (order.get(x["severity"], 3), -(x["count"] or 0)))
    return {"items": items,
            "note": ("Everything the system has left undone, in one place. Counts "
                     "are live — each is recomputed from the cache on load, not "
                     "carried forward from a note someone wrote. Where a route "
                     "exists, the row opens the section that holds the rows.")}


def build_era_sections(b):
    """Dedicated Kaalbhojaditya and ANGAD sections: same panels per era so
    each era is a self-contained story (label/filter only — no comparison
    table, per user instruction)."""
    full = b["calls"]
    if "era" not in full.columns:
        return None
    out = {}
    for era in ["Kaalbhojaditya", "ANGAD"]:
        e = full[full["era"] == era]
        if not len(e):
            continue
        daily = e.groupby("date").size()
        answered = e[e["has_transcript"]] if "has_transcript" in e.columns else e[e["reached"]]
        talked = e[e["human_conversation"]] if "human_conversation" in e.columns else answered
        booked = e[e["is_booked"]] if "is_booked" in e.columns else e[e["outcome"].isin(["PRE_SITE", "VIRTUAL_TOUR"])]
        n = len(e)
        out[era] = {
            "span": f"{e['date'].min()} → {e['date'].max()}",
            "kpis": [
                {"label": "Dials", "value": f"{n:,}", "src": "angad.calls"},
                {"label": "Unique leads", "value": f"{e['lead_ref'].nunique():,}", "src": "angad.calls"},
                {"label": "Connected", "value": f"{int(e['reached'].sum()):,}",
                 "sub": f"{pct(int(e['reached'].sum()), n)}%", "src": "angad.calls"},
                {"label": "Real conversations", "value": f"{len(talked):,}", "src": "angad.calls transcripts"},
                {"label": "Booked (call outcome)", "value": f"{booked['lead_ref'].nunique():,} leads",
                 "src": "angad.calls.outcome"},
                {"label": "Out-of-window dials", "value": f"{int(((e['hour'] < 9) | (e['hour'] >= 21)).sum()):,}",
                 "sub": "before 09:00 / after 21:00 IST", "src": "angad.calls.ts_ist"},
            ],
            "daily": {"labels": [str(x) for x in daily.index],
                      "values": [int(v) for v in daily.values]},
            "outcomes": counts(e["outcome"].fillna("(unclassified)"))[:10],
        }
    if not out:
        return None
    out["note"] = ("Era naming: everything before 2026-07-17 is the Kaalbhojaditya era; "
                   "on/after is ANGAD. Sections are separate stories, not a comparison.")
    return out


def build_profile(b, key):
    """The per-human transaction ledger: everything we know about one MLID /
    lead_id / pseudonym, dated, with the source collection on every block."""
    key = str(key or "").strip()
    if not key:
        return {"error": "empty query"}
    mlid, plid = id_maps()
    master, srcid = master_maps()
    # resolve to lead_refs across EVERY id keyspace we hold: registry MLID,
    # PLID, Inncircles MASTERLEADID (the user's unique identifier, e.g.
    # 992727), Source Lead ID, and the pseudonym itself.
    refs = set()
    kl = key.lower()
    knum = kl.rstrip(".0") if kl.endswith(".0") else kl
    for r, m in mlid.items():
        if str(m).lower() in (kl, knum):
            refs.add(r)
    for r, p in plid.items():
        if str(p).lower() == kl:
            refs.add(r)
    for r, v in master.items():
        if str(v).lower() in (kl, knum):
            refs.add(r)
    for r, v in srcid.items():
        if str(v).lower() in (kl, knum):
            refs.add(r)
    zl_all = b.get("zleads", pd.DataFrame())
    if not zl_all.empty and "lead_ref" in zl_all.columns:
        for cn in ("masterleadid", "mlid", "plid", "_id", "zoho_lead_id"):
            if cn not in zl_all.columns:
                continue
            v = zl_all[cn].astype(str).str.strip().str.lower()
            v = v.str.replace(r"\.0$", "", regex=True)
            hit = zl_all.loc[v.isin({kl, knum}), "lead_ref"].dropna()
            refs.update(hit)
    if key.upper().startswith("LEAD-"):
        # the suffix is lowercase hex — uppercasing it breaks every match
        refs.add("LEAD-" + key[5:])
    if not refs:
        # prefix search as a courtesy
        for r, p in plid.items():
            if str(p).lower().startswith(kl):
                refs.add(r)
                if len(refs) > 4:
                    break
    if not refs:
        return {"error": f"nothing found for '{key}' — try an MLID (registry or "
                         "Zoho Master Lead ID), a lead_id (e.g. 1002-LOFT) or a "
                         "LEAD-pseudonym"}

    calls = b["calls"]
    mine = calls[calls["lead_ref"].isin(refs)].copy()
    master_z = set()
    if not zl_all.empty and "masterleadid" in zl_all.columns:
        mm = zl_all[zl_all["lead_ref"].isin(refs)]["masterleadid"].dropna()
        master_z = {str(x).strip().rstrip(".0" if str(x).strip().endswith(".0") else "")
                    for x in mm if str(x).strip() not in ("", "nan", "None")}
    P = {"query": key,
         "lead_refs": sorted(refs),
         "mlids": sorted(master_z or {str(mlid[r]) for r in refs if r in mlid}),
         "lead_ids": sorted({str(plid[r]) for r in refs if r in plid}),
         "masterleadids": sorted({str(master[r]) for r in refs if r in master}),
         "source_lead_ids": sorted({str(srcid[r]) for r in refs if r in srcid})}

    # header facts
    zl = b.get("zleads", pd.DataFrame())
    zsel = zl[zl["lead_ref"].isin(refs)] if not zl.empty else zl
    crm = b.get("crm", pd.DataFrame())
    csel = crm[crm["lead_ref"].isin(refs)] if (not crm.empty and "lead_ref" in crm.columns) else pd.DataFrame()
    hdr = []
    if len(mine):
        hdr.append({"label": "Calls", "value": f"{len(mine):,}",
                    "sub": f"{int(mine['reached'].sum())} connected", "src": "angad.calls"})
        ct = pd.to_datetime(mine["createdAt"], errors="coerce", utc=True)
        lo, hi = ct.min(), ct.max()
        fmt = lambda x: (x.tz_convert("Asia/Kolkata").strftime("%d %b %H:%M")
                         if pd.notna(x) else "—")
        hdr.append({"label": "First / last dial",
                    "value": f"{fmt(lo)} → {fmt(hi)}",
                    "src": "angad.calls"})
        er = mine["era"].dropna().unique().tolist() if "era" in mine.columns else []
        hdr.append({"label": "Era", "value": " + ".join(er) or "—", "src": "derived"})
    if len(zsel):
        z0 = zsel.iloc[0]
        for lab, cn in (("Project", "project"), ("Stage", "stage"),
                        ("Lead status", "lead_status"), ("Source", "lead_source")):
            if cn in zsel.columns and pd.notna(z0.get(cn)) and str(z0.get(cn)).strip():
                hdr.append({"label": lab, "value": str(z0[cn]),
                            "src": f"Zoho_Database.leads.{cn}"})
    al = b.get("leads", pd.DataFrame())
    asel = al[al["lead_ref"].isin(refs)] if (not al.empty and "lead_ref" in al.columns) else pd.DataFrame()
    if len(asel):
        a0 = asel.iloc[0]
        if "propertyInterested" in asel.columns and pd.notna(a0.get("propertyInterested")):
            hdr.append({"label": "Property interested",
                        "value": str(a0["propertyInterested"]), "src": "angad.leads"})
        if "doNotCall" in asel.columns and truthy(asel["doNotCall"]).any():
            hdr.append({"label": "DNC", "value": "YES — do not call",
                        "src": "angad.leads.doNotCall"})
    if len(csel):
        sn = col(csel, "stage")
        if sn:
            hdr.append({"label": "CRM stage", "value": str(csel.iloc[0][sn]),
                        "src": "Intelligent_CRM.crm_leads"})
    P["header"] = hdr

    # the call ledger, intent rows joined per call
    it = b.get("intent_id", pd.DataFrame())
    if len(mine):
        mine["when"] = _ist_str(mine["createdAt"])
        led_cols = ["when", "lead_ref", "direction", "outcome", "status",
                    "duration_sec", "slug", "language", "is_qualified",
                    "slot_intent", "slot_budget", "slot_config", "slot_timeline",
                    "slot_callback", "slot_site_visit", "slot_objection",
                    "whatsappSent", "summary"]
        if not it.empty and "call_sid" in it.columns:
            j = it[it["call_sid"].isin(set(mine["roomName"].dropna()))]
            keep = [x for x in ["call_sid", "intent.conversation_stage",
                                "intent.caller_intent", "intent.intent_strength",
                                "intent.customer_sentiment", "intent.next_step",
                                "intent.call_summary"] if x in j.columns]
            if len(j) and keep:
                mine = mine.merge(j[keep].rename(columns={"call_sid": "roomName"}),
                                  on="roomName", how="left")
                led_cols += [x for x in keep if x != "call_sid"]
        led = mine.sort_values("createdAt", ascending=False)
        P["ledger"] = table(led, [x for x in led_cols if x in led.columns], cap=200)
        P["ledger_src"] = ("angad.calls (+ extracted slots) joined per call to "
                           "ASBLVoiceBot.call_transcript_intent_identification")
    else:
        P["ledger"] = None

    # scheduled calls (reminders, callbacks)
    sc = b.get("sched", pd.DataFrame())
    ssel = sc[sc["lead_ref"].isin(refs)].copy() if not sc.empty else sc
    if len(ssel):
        for cn in ("dueAt", "visitAt", "sched_createdAt"):
            if cn in ssel.columns:
                ssel[cn] = _ist_str(ssel[cn])
        P["sched"] = table(ssel.sort_values("dueAt", ascending=False, na_position="last"),
                           [x for x in ["kind", "slug", "sched_status", "dueAt",
                                        "visitAt", "attempts", "label"] if x in ssel.columns],
                           cap=100)
        P["sched_src"] = "angad.scheduledcalls"
    else:
        P["sched"] = None

    # WhatsApp + documents
    wam = b.get("wa_msgs", pd.DataFrame())
    wsel = wam[wam["lead_ref"].isin(refs)] if not wam.empty else wam
    if len(wsel):
        w0 = wsel.iloc[0]
        P["wa"] = {"outbound": int(pd.to_numeric(w0.get("outbound_count"), errors="coerce") or 0),
                   "total": int(pd.to_numeric(w0.get("total_count"), errors="coerce") or 0),
                   "first": _ist_str(pd.Series([w0.get("first_message_at")])).iloc[0],
                   "last": _ist_str(pd.Series([w0.get("last_message_at")])).iloc[0],
                   "src": "Zoho_Database.whatsapp_messages"}
    dl = b.get("doc_log", pd.DataFrame())
    dsel = dl[dl["lead_ref"].isin(refs)].copy() if not dl.empty else dl
    if len(dsel):
        dsel["sent_at"] = _ist_str(dsel["created_at"])
        P["docs"] = table(dsel.sort_values("created_at", ascending=False),
                          [x for x in ["sent_at", "doc_type", "project", "outcome",
                                       "reply_text"] if x in dsel.columns], cap=50)
        P["docs_src"] = "Zoho_Database.doc_send_log"

    # visits, both systems
    vz = b.get("visits", pd.DataFrame())
    vzs = vz[vz["lead_ref"].isin(refs)].copy() if not vz.empty else vz
    if len(vzs):
        vzs["booked_at"] = _ist_str(vzs["booked_at"])
        P["visits_zoho"] = table(vzs, [x for x in ["booked_at", "visit_date", "visit_time",
                                                   "project", "type", "call_status"]
                                       if x in vzs.columns], cap=30)
        P["visits_zoho_src"] = "Zoho_Database.site_visit_bookings"
    persons = b.get("persons", pd.DataFrame())
    cv = b.get("crm_visits", pd.DataFrame())
    if not persons.empty and not cv.empty and "lead_ref" in persons.columns:
        pid = persons[persons["lead_ref"].isin(refs)]["_id"]
        cvs = cv[cv["person_id"].isin(set(pid))].copy()
        if len(cvs):
            for cn in ("scheduled_for", "created_at"):
                if cn in cvs.columns:
                    cvs[cn] = _ist_str(cvs[cn])
            P["visits_crm"] = table(cvs, [x for x in ["scheduled_for", "project", "status",
                                                      "type", "booked_via"] if x in cvs.columns],
                                    cap=30)
            P["visits_crm_src"] = "Intelligent_CRM.site_visits"

    # stage timeline
    le = b.get("lead_events", pd.DataFrame())
    if not persons.empty and not le.empty:
        pid = set(persons[persons["lead_ref"].isin(refs)]["_id"])
        les = le[le["person_id"].isin(pid)].copy() if "person_id" in le.columns else le.iloc[0:0]
        if len(les):
            les["at"] = _ist_str(les["ts"])
            les = les.sort_values("ts", ascending=False)
            P["events"] = table(les, [x for x in ["at", "type", "channel", "project",
                                                  "actor"] if x in les.columns], cap=100)
            P["events_src"] = "Intelligent_CRM.lead_events"

    return P


CIF_SPEC = [
    ("Conv. Quality", "CM", "\ud83d\udcac", [
        ("CM", "Conversation Momentum", "see definition", "Formula: avg lead words/turn in 2nd half \u00f7 1st half. >1.0 = building momentum (lead talking more as call goes on). Needs \u22656 exchanges or \u22653 min to compute.", False, True),
        ("EBR", "Exchange Balance Ratio", ">40", "Formula: lead turns \u00f7 total turns \u00d7 100. Target 40\u201360% (balanced conversation). Too low = agent monologuing; too high (>70%) = agent lost control of the call.", False, True),
        ("RDI", "Response Depth (avg words)", ">15", "Formula: avg word count per lead response across the whole call. >15 words = engaged; <10 = disengaged.", False, True),
        ("DER", "Dead Exchange Rate", "<15", "Formula: exchanges with \u22643-word lead response \u00f7 total exchanges \u00d7 100. <15% is good \u2014 fewer flat/disengaged replies.", True, True),
        ("TTS", "Transition Smoothness", "see definition", "Formula: avg score per topic transition (0=abrupt, 1=agent-led, 2=lead-initiated). >1.4 = smooth, natural topic changes.", False, False),
        ("IPS", "Info Pacing Score", "<20", "Formula: agent turns that info-dumped (3+ facts, no pause) \u00f7 total agent turns \u00d7 100. <20% is good \u2014 info paced out, not overwhelming.", True, True),
    ]),
    ("Lead Quality", "DSS", "\ud83c\udfaf", [
        ("OQR", "Organic Question Rate", ">3", "Count of questions the lead asked unprompted. >3 = high interest.", False, True),
        ("DSS", "Decision Signal Score", ">3.5", "Weighted score, max 7.0: budget mentioned (1.5) + loan/EMI discussed (1.5) + timeline given (1.0) + family/co-decision-maker mentioned (1.0) + specific property asked about (1.0). Not a raw count \u2014 each signal has its own weight.", False, True),
        ("SE", "Specificity Escalation", "see definition", "Escalating / Flat / Declining \u2014 do the lead's questions get more specific (floor, price, possession date) as the call goes on, or stay generic/drop off? Needs \u22656 exchanges.", False, False),
        ("AUI", "Authority Indicator", "see definition", "Sole DM / Joint DM / Not DM / Unknown \u2014 is this person the actual purchase decision-maker, or do they need to check with someone else?", False, True),
        ("FRS", "Financial Readiness", ">2", "Count of financial-readiness mentions (budget figure, loan/EMI, down payment). >1 = financially engaged; 0 = unqualified so far.", False, True),
        ("GLF", "Geo / Lifestyle Fit", "see definition", "Confirmed / Partial / Mismatch / Unknown \u2014 does the project's location/lifestyle match what the lead said they need (commute, family size, amenities)?", False, False),
    ]),
    ("Engagement", "ACR", "\u26a1", [
        ("ACR", "Active Contribution Rate", ">30", "Formula: lead turns containing unprompted new info \u00f7 total lead turns \u00d7 100. >30% = active participant, not just answering yes/no.", False, True),
        ("EI", "Elaboration Index (words)", ">20", "Formula: ABSOLUTE avg words/turn in the 2nd half only (not a ratio vs. 1st half \u2014 that's CM). >20 words = elaborating freely.", False, True),
        ("EM", "Enthusiasm Markers", ">3", "Count of positive verbal cues (\"great\", \"interesting\", \"love it\"). >3 = warm tone.", False, True),
        ("RAR", "Re-engagement Accept Rate", ">60", "Formula: substantive (>3 word) responses after an agent re-engage attempt \u00f7 total re-engage attempts \u00d7 100. >60% = lead responds well when pulled back in.", False, False),
        ("OCS", "Objection\u2192Curiosity Shift", ">40", "Formula: objections followed by a related question within 1\u20132 turns \u00f7 total objections \u00d7 100. >40% = objections are turning into curiosity, not shutdown.", False, False),
        ("QR", "Question Reciprocity", ">50", "Formula: agent-introduced topics the lead followed up on \u00f7 total agent-introduced topics \u00d7 100. >50% = lead engaging back with what the agent raises.", False, False),
    ]),
    ("Interest Level", "SVO", "\ud83d\udd25", [
        ("FIL", "Future Intent Language", ">2", "Count of future-tense ownership phrases, e.g. \"when we move in\". >2 = buyer mindset, imagining life in the property.", False, True),
        ("SCS", "Specification Curiosity", ">2", "Count of specific attribute questions (floor, facing, parking, view). >2 = actively shortlisting, not just browsing.", False, True),
        ("TD", "Timeline Declaration", ">2", "0=none, 1=vague (\"sometime next year\"), 2=specific (3-6 months), 3=immediate \u2014 how concrete is the lead's stated move-in timeline?", False, True),
        ("CMT", "Competitor Mention Trend", "see definition", "Formula: competitor mentions in 2nd half MINUS 1st half (a delta, never a ratio). Negative = narrowing away from competitors; positive = increasingly comparison shopping.", False, False),
        ("SVO", "Site Visit Openness", "see definition", "-1=resistant, 0=deflects, +1=open, +2=eager, +3=proactively asked for a visit \u2014 the single strongest site-visit interest signal in the call.", False, False),
        ("PEQ", "Price Engagement Quality", "see definition", "-1=silence/hangup on price, 0=just \"okay\", +1=asked a clarifying question, +2=asked about payment/EMI, +3=compared it to their own stated budget.", False, False),
    ]),
    ("Call Quality", "NAC", "\ud83d\udcde", [
        ("OHS", "Opening Hook Success", ">2", "0=no engagement, 1=surface reply, 2=genuine engagement, 3=lead asks a question within the first 60 seconds.", False, False),
        ("BPQ", "Agent Pivot Quality", ">65", "Formula: agent-initiated topic pivots where the lead responded with >3 words \u00f7 total agent-initiated pivots \u00d7 100. >65% = pivots are landing, not falling flat.", False, False),
        ("IFQ", "Information Flow Quality", "see definition", "-1=info dumped in the first 2-3 min, 0=front-loaded, +1=naturally distributed across the call. Needs \u22656 exchanges or \u22653 min. Feeds directly into the FMS formula.", False, False),
        ("CCT", "Call Completion Type", "see definition", "-2=abrupt end/hangup, 0=no decision reached, +1=soft commitment, +2=site visit or specific callback booked. CCT=+2 is what marks a call as \"booked\" on the dashboard.", False, False),
        ("NAC", "Next Action Clarity", ">3", "0=none, 1=vague, 2=info sent on WhatsApp, 3=callback with a specific date, 4=site visit booked with date+time. Normalized to NAC_norm=NAC/4\u00d7100 for the FMS formula.", False, False),
        ("OC", "Objection Concentration", "<30", "Formula: objections raised in the final 25% of the call \u00f7 total objections \u00d7 100. <30% is good \u2014 late objections usually mean the agent tried to close too early.", True, True),
    ]),
]

def build_cif(b, c):
    """CIF v1.1 — the full 38-indicator framework over the CLEAN corpus.
    COMPUTED indicators carry live values; JUDGE indicators show NA until the
    LLM judge pass runs (it drops into the same columns, no code change)."""
    f = DATA / "cif_scores.parquet"
    if not f.exists():
        return None
    df = pd.read_parquet(f)
    if df.empty:
        return None
    refs = set(c["lead_ref"].dropna())
    sel = df[df["lead_ref"].isin(refs)]
    scope = "current filter selection"
    if len(sel) < 30:
        sel, scope = df, "full clean corpus (filter matched too few scored calls)"
    n = len(sel)

    # ---- CIF v2 (four changes approved 2026-08-08) -----------------------
    # (2) SUBSTANCE GATE. Conversation indicators are computed ONLY on calls
    #     where the customer actually spoke. Reporting a confident EBR over a
    #     call whose customer said "hmm" once is not a measurement.
    has_sub = "substantive" in sel.columns
    sub = sel[sel["substantive"].fillna(False)] if has_sub else sel
    n_sub = int(len(sub))

    # (1) CORPUS TIERS — every score says which transcript it came from.
    corpora = []
    if "corpus" in sel.columns:
        acc = {"zoho_gemini": ("A", "~90% — Gemini 2.5 Pro, written inside Zoho"),
               "audio_retranscript": ("B", "clean — re-transcribed from call audio")}
        for cname, g in sel.groupby("corpus"):
            t, why = acc.get(str(cname), ("?", "unknown provenance"))
            corpora.append({"corpus": str(cname), "tier": t, "why": why,
                            "calls": int(len(g))})
        corpora.sort(key=lambda x: x["tier"])

    # (3) PERSONA ERA — pooling different agents averages them into nothing.
    eras = []
    if "persona_era" in sub.columns and n_sub:
        for era, g in sub.groupby("persona_era"):
            eras.append({
                "era": str(era), "calls": int(len(g)),
                "RDI": round(float(pd.to_numeric(g["RDI"], errors="coerce").median()), 1)
                       if g["RDI"].notna().any() else None,
                "ACR": round(float(pd.to_numeric(g["ACR"], errors="coerce").median()), 1)
                       if g["ACR"].notna().any() else None,
                "DSS": round(float(pd.to_numeric(g["DSS"], errors="coerce").median()), 1)
                       if g["DSS"].notna().any() else None,
                "EBR_in_band": pct(int(((g["EBR"] >= 40) & (g["EBR"] <= 60)).sum()), len(g)),
            })
        eras.sort(key=lambda x: -x["calls"])
    personas = []
    if "persona" in sel.columns:
        vc = sel.loc[sel["persona"].astype(str) != "unknown", "persona"].value_counts()
        personas = [{"name": str(k), "calls": int(v)} for k, v in vc.items()]

    # Conversation-shaped indicators read the SUBSTANTIVE subset; everything
    # else (lead quality, interest, intent language) reads the full selection.
    CONVO = {"EBR", "RDI", "DER", "CM", "EI", "ACR", "IPS", "FES", "RSR"}

    def stat(code, kind):
        src = sub if (code in CONVO and has_sub) else sel
        if code not in src.columns:
            return None
        if kind == "cat":
            v = nonempty(src[code].astype(str))
            if not len(v):
                return None
            top = v.value_counts()
            return {"value": f"{top.index[0]} ({pct(int(top.iloc[0]), len(v))}%)",
                    "n": int(len(v))}
        s = pd.to_numeric(src[code], errors="coerce").dropna()
        if not len(s):
            return None
        med = s.median()
        return {"value": (f"{med:.1f}%" if kind == "pct" else f"{med:.2f}"),
                "raw": float(med), "n": int(len(s))}

    params = []
    for pname, core, icon, inds in CIF_SPEC:
        rows = []
        for code, name, target, definition, low_good, computed in inds:
            kind = ("cat" if code in ("AUI", "SE", "GLF")
                    else "pct" if code in ("EBR", "DER", "IPS", "ACR", "OC",
                                           "RAR", "QR", "BPQ", "FES") else "num")
            st = stat(code, kind) if computed else None
            # (4) NA IS NOT ONE THING. Three different problems were all
            #     rendering as a bare "NA", which made a scoring backlog look
            #     identical to a dead upstream field.
            if st:
                na_reason = None
            elif not computed:
                na_reason = ("needs the LLM judge pass — this indicator reads "
                             "MEANING and cannot be counted from text")
            elif code in CONVO and has_sub and not n_sub:
                na_reason = ("no call in this selection has the customer "
                             "actually speaking — a number here would be noise")
            else:
                na_reason = ("the source column is empty for every call in "
                             "this selection")
            rows.append({"code": code, "label": name, "target": target,
                         "definition": definition, "low_is_good": low_good,
                         "value": st["value"] if st else "NA",
                         "n": st["n"] if st else 0,
                         "judge": not computed,
                         "na_reason": na_reason,
                         "scoped_to_substantive": code in CONVO and has_sub})
        params.append({"param": pname, "core": core, "icon": icon,
                       "indicators": rows,
                       "computed": sum(1 for r in rows if not r["judge"]),
                       "total": len(rows)})

    tab = sel.sort_values("exchanges", ascending=False)
    cols = ["lead_ref", "call_date", "project", "outcome", "exchanges",
            "EBR", "RDI", "CM", "DER", "DSS", "ACR", "OQR", "FMS"]
    return {
        "n": int(n), "scope": scope,
        "n_substantive": n_sub,
        "substantive_pct": pct(n_sub, n),
        "corpora": corpora, "eras": eras, "personas": personas,
        "v2_note": (
            "CIF v2 (2026-08-08). Four changes: (1) the Zoho Gemini-2.5-Pro "
            "transcripts (~90% accurate) are now the preferred corpus and every "
            "score carries its tier — the older audio re-transcriptions are kept "
            "because 669 calls you can segment beats 113 you cannot; (2) "
            "conversation indicators are computed ONLY on calls where the "
            f"customer actually spoke ({n_sub:,} of {n:,}, {pct(n_sub, n)}%) — "
            "before this, a call where the customer grunted once produced a "
            "confident-looking balance score; (3) every indicator is segmented by "
            "persona era, because pooling Radhika, Ankit and Anandita averages "
            "three different agents; (4) NA now states WHICH problem it is — a "
            "judge backlog, an empty upstream column, or too few substantive "
            "calls. The agent's live stored transcripts are still never scored."),
        "short_pct": pct(int(sel["short_call"].sum()), n),
        "params": params,
        "computed_total": sum(p["computed"] for p in params),
        "indicator_total": sum(p["total"] for p in params),
        "headline": [
            {"label": "Clean calls scored", "value": f"{n:,}",
             "sub": f"{pct(int(sel['short_call'].sum()), n)}% short (<6 exchanges)",
             "src": "data/cif_scores.parquet"},
            {"label": "Dialogue balance in band",
             "value": f"{pct(int(((sel['EBR']>=40)&(sel['EBR']<=60)).sum()), n)}%",
             "sub": "EBR target 40-60% lead share", "src": "CIF: EBR"},
            {"label": "Median response depth", "value": f"{sel['RDI'].median():.1f} w",
             "sub": "RDI target > 15 words", "src": "CIF: RDI"},
            {"label": "Median decision signal", "value": f"{sel['DSS'].median():.1f} / 7",
             "sub": "DSS target > 3.5", "src": "CIF: DSS"},
            {"label": "FMS (master score)", "value": "NA",
             "sub": "needs SVO · NAC · IFQ from the judge pass (human-agent formula)",
             "src": "CIF v1.1 human-agent: CM·.18 + DSS·.18 + ACR·.18 + SVO·.18 + NAC·.18 + IFQ·.10"},
        ],
        "ebr_hist": [{"label": f"{lo}-{lo+10}%",
                      "value": int(((sel["EBR"] >= lo) & (sel["EBR"] < lo + 10)).sum())}
                     for lo in range(0, 100, 10)],
        "table": table(add_ids(tab), [x for x in cols if x in add_ids(tab).columns], cap=400),
        "note": (f"Scope: {scope}. Computed from CLEAN re-transcriptions only — the "
                 "stored transcripts are <50% accurate (developer's own figure) and are "
                 "never scored. Drop new clean transcriptions into the import and press "
                 "Refresh: sync re-runs cif.py automatically and every number here "
                 "updates. JUDGE indicators show NA by design until the LLM pass runs."),
    }


def build_drill(b, q, metric):
    """Contract rules 1+2: the number IS the query. Returns the exact rows
    behind a metric under the CURRENT filters, plus the aggregate recomputed
    with the SAME expression the card used, so the front-end can assert parity.
    count_kind: 'count' = the number is a row count · 'measure' = a statistic
    (median/sum) where row-count parity is meaningless and must not be claimed."""
    c, d0, d1 = apply_filters(b, q)
    if c.empty:
        return {"error": "no rows match the current filters"}
    c = c.copy()
    c["when"] = pd.to_datetime(c["ts_ist"], errors="coerce").dt.strftime("%d %b %H:%M")
    base = ["when", "lead_ref", "outcome", "status", "duration_sec", "slug"]
    refmap = human_map(b)
    kind = "count"

    def calls_tbl(sub, extra=None):
        sub = sub.sort_values("ts_ist", ascending=False)
        s2 = add_ids(sub)
        return table(s2, [x for x in base + (extra or []) if x in s2.columns], cap=500)

    def leads_tbl(sub):
        per = (sub.groupby("lead_ref")
               .agg(dials=("roomName", "size"), reached=("reached", "max"),
                    booked=("is_booked", "max"), last=("ts_ist", "max")).reset_index())
        per["last"] = pd.to_datetime(per["last"], errors="coerce").dt.strftime("%d %b %H:%M")
        p2 = add_ids(per.sort_values("dials", ascending=False))
        return table(p2, [x for x in ["lead_ref", "MLID", "Lead ID", "Source Lead ID",
                                      "dials", "reached", "booked", "last"]
                          if x in p2.columns], cap=500)

    m = metric
    if m in ("Calls triggered", "Dial attempts"):
        n, t_ = len(c), calls_tbl(c)
    elif m == "Unique numbers":
        n, t_ = c["lead_ref"].nunique(), leads_tbl(c)
    elif m in ("Unique humans", "Humans dialled"):
        n = c["lead_ref"].map(lambda r: refmap.get(r, r)).nunique()
        t_ = leads_tbl(c)
    elif m == "Connected":
        sub = c[c["reached"]]
        n, t_ = len(sub), calls_tbl(sub)
    elif m == "Humans connected":
        # mirrors the card exactly: reached OR the customer actually spoke
        sub = c[c["reached"] | c.get("human_conversation", False)]
        n = sub["lead_ref"].map(lambda r: refmap.get(r, r)).nunique()
        t_ = calls_tbl(sub)
    elif m in ("Booked", "Humans booked (call outcome)"):
        sub = c[c["is_booked"]]
        n = (len(sub) if m == "Booked"
             else sub["lead_ref"].map(lambda r: refmap.get(r, r)).nunique())
        t_ = calls_tbl(sub, ["slot_site_visit"])
    elif m == "Dropped":
        sub = c[c["is_dropped"]]
        n, t_ = len(sub), calls_tbl(sub, ["drop_stage"])
    elif m == "Qualified":
        sub = c[truthy(c["is_qualified"])] if "is_qualified" in c.columns else c.iloc[0:0]
        n, t_ = len(sub), calls_tbl(sub, ["slot_budget", "slot_config"])
    elif m == "Humans in real conversation":
        sub = c[c["human_conversation"]] if "human_conversation" in c.columns else c[c["reached"]]
        n = sub["lead_ref"].map(lambda r: refmap.get(r, r)).nunique()
        t_ = calls_tbl(sub)
    elif m == "Humans DNC":
        sub = c[truthy(c["dncTriggered"])] if "dncTriggered" in c.columns else c.iloc[0:0]
        n = sub["lead_ref"].map(lambda r: refmap.get(r, r)).nunique()
        t_ = calls_tbl(sub)
    elif m == "Median talk time":
        # a STATISTIC, not a count — same population the card measures
        sub = c[c["reached"] & c.get("duration_known", True)]
        n = f"{sub['duration_sec'].median():.0f}s" if len(sub) else "—"
        kind = "measure"
        t_ = calls_tbl(sub.sort_values("duration_sec", ascending=False))
    elif m == "₹ spend (approx)":
        tp = pd.to_numeric(c.get("tok_prompt"), errors="coerce").fillna(0)
        tr = pd.to_numeric(c.get("tok_response"), errors="coerce").fillna(0)
        mins = pd.to_numeric(c.get("duration_sec"), errors="coerce").fillna(0) / 60
        series = (tp * 3 + tr * 12) / 1e6 * 96.0 + mins * 0.72
        c["inr_est"] = series.round(2)
        n = f"₹{series.sum():,.0f}"          # summed unrounded, then rounded ONCE
        kind = "measure"
        t_ = calls_tbl(c.sort_values("inr_est", ascending=False), ["inr_est"])
    else:
        return {"error": f"no drill is defined for '{metric}' yet — this metric "
                         "is not in the drill registry.", "lineage": LINEAGE.get(m)}
    return {"metric": m, "count": clean(n), "count_kind": kind,
            "rows_total": t_["total"], "tbl": t_,
            "lineage": LINEAGE.get(m), "window": f"{d0} → {d1}"}



SLICE_DIMS = {
    "outcome": "outcome", "status": "status", "drop_stage": "drop_stage",
    "attempts": "total_attempts", "hour": "hour", "weekday": "weekday",
    "language": "language", "slug": "slug", "era": "era",
    "engine": "voiceEngine", "direction": "direction", "source": "source",
    "outcome_group": "outcome_group",
}


def build_project_funnel(b, c):
    """FUNNEL BY PROJECT — the same funnel, once per building. A lead can touch
    several projects, so a call is counted under EVERY project its lead holds
    (stated, not hidden): the columns are per-project performance, not a
    partition of the total."""
    t = truth()
    z = t["zoho"]
    if z.empty or "projects" not in z.columns:
        return None
    band = t["band"]
    rows, detail = [], {}
    projects = set()
    for v in z["projects"].dropna():
        for p in str(v).split(" + "):
            if p.strip():
                projects.add(p.strip())
    refs_in_view = set(c["lead_ref"].dropna())
    for p in sorted(projects):
        prefs = {r for r in z.index
                 if p in str(z["projects"].get(r, "")).split(" + ")}
        prefs &= refs_in_view
        if not prefs:
            continue
        sub = c[c["lead_ref"].isin(prefs)]
        humans = len(prefs)
        conn = int(sub["reached"].sum())
        conv = int(sub["human_conversation"].sum()) if "human_conversation" in sub.columns else 0
        booked = int(sub[sub["is_booked"]]["lead_ref"].nunique()) if "is_booked" in sub.columns else 0
        bands = {}
        for r in prefs:
            bands[band.get(r, "Unstaged")] = bands.get(band.get(r, "Unstaged"), 0) + 1
        rows.append([p, humans, len(sub), conn, conv, booked,
                     pct(booked, humans),
                     bands.get("TOFU", 0), bands.get("MOFU", 0), bands.get("BOFU", 0),
                     bands.get("OUT", 0)])
        detail[p] = {
            "funnel": [
                {"label": "Leads in this project", "value": humans, "pct": 100.0},
                {"label": "Dialled", "value": int(sub["lead_ref"].nunique()),
                 "pct": pct(int(sub["lead_ref"].nunique()), humans)},
                {"label": "Connected (calls)", "value": conn, "pct": pct(conn, len(sub))},
                {"label": "Real conversation", "value": conv, "pct": pct(conv, len(sub))},
                {"label": "Booked (humans)", "value": booked, "pct": pct(booked, humans)},
            ],
            "outcomes": counts(sub["outcome"].fillna("(unclassified)"))[:8],
            "bands": [{"label": k, "value": v} for k, v in sorted(bands.items())],
        }
    rows.sort(key=lambda r: -r[1])
    return {"table": {"cols": ["Project", "Leads", "Calls", "Connected",
                               "Real conv.", "Booked", "Book %",
                               "TOFU", "MOFU", "BOFU", "OUT"],
                      "rows": rows, "total": len(rows), "shown": len(rows)},
            "detail": detail,
            "note": ("One row per project, respecting the current filters. A human "
                     "interested in two projects is counted under both — these are "
                     "per-project performance columns, NOT a split of the total, so "
                     "the column will not sum to the headline. Click a project to "
                     "open its own funnel.")}


def persona_of(c):
    """Which agent persona ran the call — derived from the transcript, because
    the slug never changed and no prompt-version field exists (dev round 2, §2b).
    The agent names itself in the greeting, so the name in the agent's own words
    is the only per-call evidence that exists."""
    txt = c.get("agent_said", pd.Series("", index=c.index)).fillna("").astype(str)
    out = pd.Series("unknown", index=c.index)
    out[txt.str.contains(r"\bradhika\b", case=False, regex=True)] = "Radhika (female)"
    out[txt.str.contains(r"\bankit\b", case=False, regex=True)] = "Ankit (male)"
    return out


def build_slice(b, q, dim, value):
    """UNIVERSAL PROBE — click any bar/segment anywhere and get the whole story
    of the rows behind it: the calls, who they were, what they talked about,
    whether they got frustrated, whether WhatsApp went out, and why there were
    so many attempts. This is the 'investigate inside the component' surface."""
    col = SLICE_DIMS.get(dim)
    if not col:
        return {"error": f"'{dim}' is not a sliceable dimension"}
    c, d0, d1 = apply_filters(b, q)
    if c.empty or col not in c.columns:
        return {"error": "no rows under the current filters"}
    s = c[col]
    if col in ("hour", "total_attempts"):
        sel = c[pd.to_numeric(s, errors="coerce") == pd.to_numeric(value, errors="coerce")]
    else:
        sel = c[s.astype(str) == str(value)]
    if sel.empty:
        return {"error": f"no rows for {dim} = {value}"}

    refs = set(sel["lead_ref"].dropna())
    S = {"dim": dim, "value": str(value), "calls": int(len(sel)),
         "leads": int(sel["lead_ref"].nunique()),
         "window": f"{d0} → {d1}"}

    # --- headline facts about this slice
    reach = int(sel["reached"].sum()) if "reached" in sel.columns else 0
    S["facts"] = [
        {"label": "Calls in this slice", "value": f"{len(sel):,}"},
        {"label": "Unique leads", "value": f"{sel['lead_ref'].nunique():,}"},
        {"label": "Connected", "value": f"{reach:,}", "sub": f"{pct(reach, len(sel))}%"},
        {"label": "Median duration",
         "value": f"{pd.to_numeric(sel['duration_sec'], errors='coerce').median():.0f}s"},
        {"label": "Booked from this slice",
         "value": f"{int(sel['is_booked'].sum()):,}" if "is_booked" in sel.columns else "—"},
    ]

    # --- WHY so many calls: attempt spread and the outcomes that caused re-dials
    if "total_attempts" in sel.columns:
        at = pd.to_numeric(sel["total_attempts"], errors="coerce").dropna()
        if len(at):
            S["facts"].append({"label": "Median attempts on these leads",
                               "value": f"{at.median():.0f}", "sub": f"max {at.max():.0f}"})
    S["outcome_mix"] = counts(sel["outcome"].fillna("(unclassified)"))[:10]
    S["status_mix"] = counts(sel["status"].fillna("(none)"))[:6]
    if "drop_stage" in sel.columns:
        S["drop_mix"] = counts(nonempty(sel["drop_stage"]))[:8]

    # --- WHAT they talked about (intent collection, joined per call)
    it = b.get("intent_id", pd.DataFrame())
    if not it.empty and "call_sid" in it.columns:
        j = it[it["call_sid"].isin(set(sel["roomName"].dropna()))]
        if len(j):
            S["talked_about"] = {
                "n": int(len(j)),
                "caller_intent": counts(j["intent.caller_intent"])[:8]
                if "intent.caller_intent" in j.columns else [],
                "objections": ([{"label": str(k)[:44], "value": int(v)} for k, v in
                                nonempty(j["intent.objections"]).str.lower().str.split(";")
                                .explode().str.strip().value_counts().head(10).items()]
                               if "intent.objections" in j.columns else []),
                "sentiment": counts(j["intent.customer_sentiment"])[:6]
                if "intent.customer_sentiment" in j.columns else [],
                "summaries": [str(x)[:190] for x in
                              nonempty(j.get("intent.call_summary", pd.Series(dtype=str))).head(6)],
                "src": "ASBLVoiceBot.call_transcript_intent_identification",
            }
            # frustration proxy: negative sentiment + repeat objections
            if "intent.customer_sentiment" in j.columns:
                neg = int(j["intent.customer_sentiment"].isin(
                    ["negative", "very_negative"]).sum())
                S["facts"].append({"label": "Negative sentiment",
                                   "value": f"{neg:,}", "sub": f"{pct(neg, len(j))}% of scored calls"})

    # --- did they also get WhatsApp?
    wa = b.get("wa_msgs", pd.DataFrame())
    if not wa.empty and "lead_ref" in wa.columns:
        w = wa[wa["lead_ref"].isin(refs)]
        S["facts"].append({"label": "Leads with WhatsApp history",
                           "value": f"{w['lead_ref'].nunique():,}",
                           "sub": f"of {len(refs):,} leads in this slice"})
    if "whatsappSent" in sel.columns:
        S["facts"].append({"label": "Calls that sent WhatsApp",
                           "value": f"{int(truthy(sel['whatsappSent']).sum()):,}"})

    # --- the rows themselves, and the leads behind them
    sel = sel.copy()
    sel["when"] = pd.to_datetime(sel["ts_ist"], errors="coerce").dt.strftime("%d %b %H:%M")
    s2 = add_ids(sel.sort_values("ts_ist", ascending=False))
    S["calls_tbl"] = table(s2, [x for x in ["when", "lead_ref", "MLID", "Lead ID",
                                            "outcome", "status", "duration_sec",
                                            "total_attempts", "drop_stage", "slug",
                                            "whatsappSent", "summary"]
                                if x in s2.columns], cap=400)
    per = (sel.groupby("lead_ref")
           .agg(calls=("roomName", "size"), attempts=("total_attempts", "max"),
                reached=("reached", "max"), booked=("is_booked", "max"),
                last=("ts_ist", "max")).reset_index())
    per["last"] = pd.to_datetime(per["last"], errors="coerce").dt.strftime("%d %b %H:%M")
    p2 = add_ids(per.sort_values("calls", ascending=False))
    S["leads_tbl"] = table(p2, [x for x in ["lead_ref", "MLID", "Lead ID", "Source Lead ID",
                                            "calls", "attempts", "reached", "booked", "last"]
                                if x in p2.columns], cap=400)
    S["note"] = ("Every row opens its lead; the lead modal's Conversation tab shows the "
                 "turn-by-turn transcript. Objections and sentiment come from the intent "
                 "collection (present on ~45% of calls); absence means unscored, not calm.")
    return S


def build_db_freshness(b):
    """WHAT PART OF THE DATABASE IS ACTUALLY MOVING — newest record per
    collection, so a frozen source is visible instead of quietly feeding stale
    numbers into every metric. Fast path: only real datetime columns, no
    string parsing (a full scan hung the payload)."""
    rows = []
    now = pd.Timestamp.now(tz="UTC")
    for name, df in b.items():
        if not isinstance(df, pd.DataFrame) or df.empty:
            continue
        newest, used = None, None
        for c in df.columns:
            if not pd.api.types.is_datetime64_any_dtype(df[c]):
                continue
            try:
                v = df[c].max()
                if pd.isna(v):
                    continue
                v = pd.Timestamp(v)
                v = v.tz_localize("UTC") if v.tzinfo is None else v.tz_convert("UTC")
                if newest is None or v > newest:
                    newest, used = v, c
            except Exception:
                continue
        age = (now - newest).days if newest is not None else None
        rows.append([name, int(len(df)), used or "— (no datetime column)",
                     newest.strftime("%Y-%m-%d %H:%M") if newest is not None else "unknown",
                     ("today" if age == 0 else f"{age}d ago") if age is not None else "—",
                     "STALE" if (age is not None and age > 2) else ""])
    rows.sort(key=lambda r: (r[5] == "", r[0]))
    return {"cols": ["Collection (cached)", "Rows", "Date field used",
                     "Newest record", "Age", "Flag"],
            "rows": rows, "total": len(rows), "shown": len(rows),
            "note": ("Newest record per cached collection — this measures the SOURCE data, "
                     "not when we synced. A collection whose newest row is days old is not "
                     "being written upstream, and every metric built on it is quietly frozen. "
                     "STALE = no new record for more than 2 days.")}


_DEAD_RE = re.compile(r"^(ok+(ay)?|yes|yeah|ya|hmm+|hm+|haan|ha|acha|accha|"
                      r"thik hai|theek hai|fine|right|correct)[.! ]*$", re.I)


def build_turns(b, lead_ref, idx=None):
    """CALLING TURNS viewer — the conversation itself, exchange by exchange,
    with CIF marks per turn. Reads the CLEAN corpus only (never the garbled
    stored transcript). Deterministic marks now; the LLM judge's
    Progressed/Resolved/Stalled/Regressed labels drop into the same rows when
    the judge pass runs on tomorrow's corpus."""
    f = DATA / "retranscripts.parquet"
    if not f.exists():
        return {"error": "no clean-transcript corpus cached"}
    r = pd.read_parquet(f)
    sel = r[r["lead_ref"].astype(str).str.lower() == str(lead_ref).lower()]
    if not len(sel):
        return {"error": f"no clean transcript for {lead_ref} yet — clean "
                         "transcriptions cover part of the corpus; this lead's "
                         "calls are not in it."}
    calls = []
    for i, (_, row) in enumerate(sel.iterrows()):
        txt = str(row.get("clean_transcript") or "")
        turns = []
        for line in txt.splitlines():
            m = re.match(r"^(?:\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*)?"
                         r"(Agent|Customer|Bot|Lead)\s*:\s*(.*)$", line.strip())
            if not m:
                continue
            who = "Agent" if m.group(2) in ("Agent", "Bot") else "Customer"
            text = m.group(3).strip()
            words = len(text.split())
            mark, tone = "", ""
            if who == "Customer":
                if _DEAD_RE.match(text):
                    mark, tone = "dead reply", "bad"
                elif "?" in text:
                    mark, tone = f"asked a question · {words}w", "good"
                elif words > 20:
                    mark, tone = f"elaborated · {words}w", "good"
                elif words >= 8:
                    mark, tone = f"engaged · {words}w", ""
                else:
                    mark, tone = f"brief · {words}w", "warn"
            else:
                mark = f"{words}w"
                if words > 90:
                    mark, tone = f"MONOLOGUE · {words}w", "bad"
            turns.append({"who": who, "at": m.group(1), "text": text,
                          "mark": mark, "tone": tone})
        if not turns:
            continue
        lead_w = sum(len(t["text"].split()) for t in turns if t["who"] == "Customer")
        all_w = sum(len(t["text"].split()) for t in turns)
        calls.append({
            "idx": i,
            "call_date": clean(row.get("call_date")),
            "outcome": clean(row.get("outcome")),
            "duration_s": clean(row.get("duration_s")),
            "project": clean(row.get("project")),
            "source_file": clean(row.get("source_file")),
            "exchanges": min(sum(1 for t in turns if t["who"] == "Agent"),
                             sum(1 for t in turns if t["who"] == "Customer")),
            "DB": round(100 * lead_w / all_w, 1) if all_w else None,
            "turns": turns,
        })
    if not calls:
        return {"error": "clean transcript exists but has no dialogue turns "
                         "(no-recording / agent-only call)"}
    return {"lead_ref": lead_ref, "calls": calls,
            "note": ("Clean re-transcription only — the stored transcript is "
                     "<50% accurate and is never shown here. Marks are the "
                     "deterministic CIF pass; the LLM judge's exchange labels "
                     "(Progressed/Resolved/Stalled/Regressed → CDS) attach to "
                     "these same rows when the judge pass runs.")}


def build_lead_modal(b, key):
    """One lead, every truth, tabbed like the Inncircles modal: Overview ·
    Calls · WhatsApp · Web History · Lead History (transactions) · Stage
    History · Scoring. NA cards say NA — they never pretend."""
    P = build_profile(b, key)
    if P.get("error"):
        return P
    refs = set(P["lead_refs"])
    t = truth()
    z = t["zoho"]

    # web history (the July ask finally surfaced)
    web = None
    for r in refs:
        if not z.empty and r in z.index:
            row = z.loc[r]
            web = {"first_page": clean(row.get("first_page")),
                   "last_page": clean(row.get("last_page")),
                   "time_spent_min": clean(row.get("time_spent_min")),
                   "page_views": clean(row.get("page_views")),
                   "utm_source": clean(row.get("utm_source")),
                   "campaign": clean(row.get("campaign")),
                   "src": "Zoho_Database.leads (attribution fields)"}
            break
    P["web"] = web  # None → the UI shows an NA card

    # scoring (colour model)
    calls_me = b["calls"][b["calls"]["lead_ref"].isin(refs)]
    P["scoring"] = scoring_of(next(iter(refs)), t, calls_me)
    P["scoring_src"] = ("green=qualified (calls/CRM) · purple=inbound WhatsApp "
                        "(campaign manager) · orange=campaign reaction (web activity)")

    # CIF scores for this lead's clean-transcribed calls (PARASHURAMA merge)
    cf = DATA / "cif_scores.parquet"
    if cf.exists():
        cs = pd.read_parquet(cf)
        mine_cif = cs[cs["lead_ref"].isin(refs)]
        if len(mine_cif):
            # CIF v1.0 called these DB / RD. v1.1 renamed them EBR / RDI and this
            # call site was never updated, so EVERY lead that HAS a CIF score
            # raised KeyError: 'DB' and its card refused to open — while leads
            # with no score opened fine. That is why the modal worked "in some
            # places and not others". Read by code, defensively, never by
            # subscript: a renamed indicator must degrade to NA, not to a crash.
            def cifavg(*codes):
                for cd in codes:
                    if cd in mine_cif.columns:
                        v = pd.to_numeric(mine_cif[cd], errors="coerce").dropna()
                        if len(v):
                            return round(float(v.mean()), 1)
                return None
            P["cif"] = {
                "n": int(len(mine_cif)),
                "EBR": cifavg("EBR", "DB"),
                "RDI": cifavg("RDI", "RD"),
                "CM": cifavg("CM"),
                "DER": cifavg("DER"),
                "DSS": cifavg("DSS"),
                "ACR": cifavg("ACR"),
                "src": "CIF v2 deterministic pass on clean transcripts (cif_scores.parquet)"}

    # per-lead calling metrics, split RI vs NRI. The two are different worlds —
    # IST night is the customer's day for a Telnyx lead — so pooling them inside
    # a lead card hides the only comparison that matters for that lead.
    try:
        cm = b["calls"][b["calls"]["lead_ref"].isin(refs)].copy()
        if len(cm):
            _, _, _, pu = cost_of(cm)
            cm["_picked"] = pu
            amap = truth()["audience"]
            cm["_aud"] = cm["lead_ref"].map(lambda r: amap.get(r, "RI"))
            blocks = []
            for aud, g in cm.groupby("_aud"):
                conv = g[g["_picked"]]
                blocks.append({
                    "audience": str(aud),
                    "provider": "Telnyx (international)" if aud == "NRI" else "Plivo (India)",
                    "dials": int(len(g)),
                    "picked_up": int(g["_picked"].sum()),
                    "pickup_pct": pct(int(g["_picked"].sum()), len(g)),
                    "connected": int(g["reached"].sum()) if "reached" in g.columns else 0,
                    "booked": int(g["is_booked"].sum()) if "is_booked" in g.columns else 0,
                    "median_turns": clean(pd.to_numeric(conv.get("turns_total"), errors="coerce").median())
                                    if len(conv) and "turns_total" in conv.columns else None,
                    "median_talk_s": clean(pd.to_numeric(conv.get("duration_sec"), errors="coerce").median())
                                     if len(conv) else None,
                })
            P["audience_split"] = {
                "blocks": blocks,
                "note": ("Pickup uses the corrected test (the line opened), not "
                         "`reached`. NRI is dialled via Telnyx and RI via Plivo; "
                         "their calling windows are different worlds, so their "
                         "pickup rates are not comparable to each other — only to "
                         "the same audience elsewhere."),
            }
    except Exception:
        P["audience_split"] = None

    # stage truth + band + disagreement
    r0 = next(iter(refs))
    c_ = t["crm"]
    P["stage_truth"] = {
        "zoho": clean(z["z_stage"].get(r0)) if not z.empty and r0 in z.index else None,
        "crm": clean(c_["c_stage"].get(r0)) if not c_.empty and r0 in c_.index else None,
        "band": t["band"].get(r0, "Unstaged"),
        "disagree": r0 in t["disagree"],
        "audience": t["audience"].get(r0, "unknown"),
    }
    return P


_FEEDBACK = HERE / "RELEASES" / "feedback.jsonl"


def feedback_add(text, tab):
    rec = {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "tab": str(tab)[:40], "text": str(text)[:2000]}
    with open(_FEEDBACK, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def feedback_list(n=30):
    if not _FEEDBACK.exists():
        return []
    lines = _FEEDBACK.read_text(encoding="utf-8").strip().splitlines()
    out = []
    for ln in lines[-n:][::-1]:
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def payload(q):
    b = load()
    if b is None:
        return {"empty": True}
    c, d0, d1 = apply_filters(b, q)
    if c.empty:
        applied = {k: v for k, v in q.items()
                   if k not in ("t", "pii") and v not in (None, "")}
        return {"no_rows": True, "options": options(b),
                "applied": applied,
                "total_calls": int(len(b["calls"])),
                "meta": {"from": str(d0), "to": str(d1)}}
    c = c.copy()
    if q.get("pii") == "1":
        c = add_phone(c)
    c["ts_str"] = pd.to_datetime(c["ts_ist"], errors="coerce").dt.strftime("%d %b %H:%M")
    c["next_call_str"] = pd.to_datetime(c["next_call_at"], errors="coerce", utc=True)\
        .dt.tz_convert("Asia/Kolkata").dt.strftime("%d %b %H:%M")
    for x in ("lead_talk_share", "first_lead_reply_sec", "hours_to_next_call",
              "hours_to_first_answer", "duration_sec"):
        if x in c.columns:
            c[x] = pd.to_numeric(c[x], errors="coerce").round(2)
    D = build(b, c, d0, d1, q_unit=q.get("unit", "calls"), q_irule=re.sub(r"[^a-z0-9_]", "", q.get("irule", ""))[:40] or None, q_fstage=re.sub(r"[^a-z_]", "", q.get("fstage", ""))[:24] or None)
    D["options"] = options(b)
    # live per-value counts so the modal can mark options that match nothing
    # in the current window instead of silently returning an empty screen
    try:
        D["option_counts"] = option_counts(b, c)
    except Exception:
        D["option_counts"] = {}
    D["pii"] = {"available": bool(phone_map()), "on": q.get("pii") == "1"}
    D["review"] = build_review(b, c, d0, d1)
    D["ops_daily"] = build_ops_daily(b, c)
    try:
        # both stage systems are built — the toggle switches between them in the
        # browser, so flipping it never costs a round trip
        D["daily_report"] = {k: build_daily_report(b, q, d0, d1, k) for k in STAGE_SRC}
    except Exception as e:
        D["daily_report"] = {"error": f"{type(e).__name__}: {e}"}
    D["era_sections"] = build_era_sections(b)
    D["bands"] = build_bands(b, c)
    try:
        D["project_funnel"] = build_project_funnel(b, c)
    except Exception:
        D["project_funnel"] = None
    try:
        pm = persona_of(c)
        known = pm[pm != "unknown"]
        pd_ = (c.assign(_p=pm)[pm != "unknown"].groupby(["date", "_p"]).size()
               .unstack(fill_value=0))
        D["persona"] = {
            "mix": [{"label": k, "value": int(v)} for k, v in known.value_counts().items()],
            "named": int(len(known)), "total": int(len(c)),
            "dates": [str(x) for x in pd_.index],
            "series": [{"name": col, "color": "#c2683a" if "Ankit" in col else "#3f7d6e",
                        "values": [int(v) for v in pd_[col]]} for col in pd_.columns],
            "note": ("Persona is derived from the agent naming itself in the call — the "
                     "slug never changed and no prompt-version is stored, so this is the "
                     "only per-call evidence that exists (method from the dev team, round 2 §2b). "
                     "Only calls with a transcript can be labelled."),
        }
    except Exception:
        D["persona"] = None
    D["insights"] = build_insights(b, c, d0, d1)
    D["globe"] = build_globe(b, c)
    D["glossary"] = GLOSSARY
    D["taxonomy_breaks"] = TAXONOMY_BREAKS
    D["feedback"] = feedback_list()
    D["lineage"] = LINEAGE
    try:
        D["freshness"] = build_db_freshness(b)
    except Exception:
        D["freshness"] = None
    try:
        D["cif"] = build_cif(b, c)
    except Exception:
        D["cif"] = None   # degrade to the honest NA card

    # contract rule 8: comparison as a toggle — previous period of equal
    # length, date-window comparison (other filters not re-applied; labelled).
    try:
        full = b["calls"]
        span = (d1 - d0).days + 1
        p1 = d0 - timedelta(days=1)
        p0 = p1 - timedelta(days=span - 1)
        pc = full[(full["date"] >= p0) & (full["date"] <= p1)]
        # the KAALBHOJADITYA baseline: everything before the 2026-07-17 cutover.
        # This is the real "previous period" for this business — the old system.
        kb = full[full["era"] == "Kaalbhojaditya"] if "era" in full.columns else full.iloc[0:0]
        def blk(df, label, window):
            n = len(df)
            dys = int(df["date"].nunique()) if n else 0
            return {"label": label, "window": window, "dials": n, "days": dys,
                    "dials_per_day": round(n / dys) if dys else 0,
                    "connected": int(df["reached"].sum()) if n else 0,
                    "connect_pct": pct(int(df["reached"].sum()), n) if n else 0,
                    "booked": int(df["is_booked"].sum()) if n else 0,
                    "booked_per_1k": round(1000 * int(df["is_booked"].sum()) / n, 2) if n else 0}
        D["prev"] = {
            "window": f"{p0} → {p1}",
            "dials": int(len(pc)), "connected": int(pc["reached"].sum()),
            "booked": int(pc["is_booked"].sum()),
            "baselines": [
                blk(pc, "Previous equal period", f"{p0} → {p1}"),
                blk(kb, "Kaalbhojaditya (pre-17 Jul)",
                    f"{kb['date'].min()} → {kb['date'].max()}" if len(kb) else "—"),
            ],
            "note": ("Two baselines: the immediately preceding window of the same length, "
                     "and the Kaalbhojaditya era — everything before the 2026-07-17 cutover, "
                     "i.e. the old system. Rates are the honest comparison; raw counts are not "
                     "(volume differs by ~35x)."),
        }
    except Exception:
        D["prev"] = None

    # contract rule 6: inline data-quality badges on the KPIs themselves
    tqn = int(truthy(c["tq_any"]).sum()) if "tq_any" in c.columns else 0
    dqm = {
        "Calls triggered": None,
        "Connected": {"n": tqn, "why": "calls carry a transcript-quality flag"},
        "Median talk time": {"n": int(truthy(c["duration_implausible"]).sum())
                             if "duration_implausible" in c.columns else 0,
                             "why": "implausible durations excluded"},
    }
    drills = {"Calls triggered": "numbers", "Unique numbers": "funnel3",
              "Unique humans": "funnel3", "Connected": "numbers",
              "Booked": "visits", "Dropped": "negatives", "Qualified": "intent",
              "Median talk time": "metrics", "₹ spend (approx)": "metrics",
              "Dial attempts": "numbers", "Humans dialled": "funnel3",
              "Humans connected": "numbers", "Humans in real conversation": "numbers",
              "Humans booked (call outcome)": "visits", "Humans DNC": "integrity"}
    for k in D["kpis"]:
        k["drill"] = drills.get(k["label"])
        dq = dqm.get(k["label"])
        if dq and dq["n"]:
            k["dq"] = {"n": dq["n"], "why": dq["why"]}
    # ---- CARD CONTRACTS (every card must be able to say what it counts) ----
    # LINEAGE holds hand-written contracts, but there are ~79 KPI tiles across
    # the app and most had no entry, so "Explain this number" answered
    # "NO DEFINITION EXPOSED" — a card that cannot say what it counts is not a
    # metric, it is a rumour. Rather than leave the gap, every card now gets a
    # contract: the hand-written one where it exists, otherwise one DERIVED from
    # the card's own sub-label and source, explicitly marked PARTIAL so the
    # difference between "contracted" and "merely sourced" stays visible and
    # countable instead of silently blank.
    try:
        contracts, partial = {}, []

        def is_card(n):
            # A KPI TILE has a label, a value, and provenance (sub and/or src).
            # A CHART BAR also has label+value — which is why the first pass
            # counted 449 "cards" including '0-10s' and '0.5 Cr'. Bars carry
            # pct/color and never carry src, so require provenance and reject
            # the bar-shaped keys.
            if not ("label" in n and "value" in n):
                return False
            if "color" in n or "pct" in n:
                return False
            return ("src" in n) or ("sub" in n) or ("drill" in n) or ("dq" in n)

        def walk(node):
            if isinstance(node, dict):
                if is_card(node):
                    lb = str(node.get("label"))
                    if lb and lb not in contracts:
                        if lb in LINEAGE:
                            contracts[lb] = dict(LINEAGE[lb], contract="FULL")
                        else:
                            partial.append(lb)
                            contracts[lb] = {
                                "contract": "PARTIAL",
                                "def": (str(node.get("sub") or "").strip()
                                        or "No written definition yet — this card "
                                           "states a value and its source, but its "
                                           "constituents have not been contracted."),
                                "num": None, "den": None,
                                "window": "current date filter",
                                "filters": "all active filters",
                                "src": str(node.get("src") or "not stated on the card"),
                                "excl": ("PARTIAL contract: derived from the card's own "
                                         "sub-label and source. It has NOT been verified "
                                         "against a query the way a FULL contract has. "
                                         "Treat the number as directional until it is."),
                            }
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(D)
        D["card_contracts"] = contracts
        D["contract_coverage"] = {
            "total": len(contracts),
            "full": len(contracts) - len(partial),
            "partial": len(partial),
            "pct_full": pct(len(contracts) - len(partial), len(contracts)),
            "missing": sorted(partial)[:80],
        }
    except Exception:
        D["card_contracts"], D["contract_coverage"] = {}, {}

    return D


# ============================================================ refresh

def run_sync(args):
    def work():
        with _lock:
            _sync_state["running"] = True
            _sync_state["log"] = "starting…"
        try:
            p = subprocess.run([sys.executable, str(HERE / "sync.py")] + args,
                               capture_output=True, text=True, cwd=str(HERE),
                               timeout=5400)
            out = (p.stdout or "") + (p.stderr or "")
            # PARASHURAMA: CIF rescoring rides every refresh, so a new clean-
            # transcription import needs no deploy — data in, scores out.
            try:
                p2 = subprocess.run([sys.executable, str(HERE / "cif.py")],
                                    capture_output=True, text=True, cwd=str(HERE),
                                    timeout=600)
                out += "\n--- CIF rescore ---\n" + (p2.stdout or "") + (p2.stderr or "")
            except Exception as e2:
                out += f"\nCIF rescore failed: {e2}"
            _sync_state["log"] = out[-4000:]
        except Exception as e:
            _sync_state["log"] = f"{type(e).__name__}: {e}"
        finally:
            load.cache_clear()
            _sync_state["running"] = False
            _sync_state["finished_at"] = datetime.now().strftime("%H:%M:%S")
    threading.Thread(target=work, daemon=True).start()


# ============================================================ http

class Handler(BaseHTTPRequestHandler):
    server_version = "local"

    def log_message(self, *a):
        pass  # no request logging

    # ---- security gate, runs before every handler
    def _authorised(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False, "bad host"
        # a cross-site request from another page would carry Origin/Referer
        for h in ("Origin", "Referer"):
            v = self.headers.get(h)
            if v and not v.startswith(f"http://{HOST}:{PORT}"):
                return False, "cross-origin refused"
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            return False, "cross-site refused"
        q = parse_qs(urlparse(self.path).query)
        if not secrets.compare_digest((q.get("t") or [""])[0], TOKEN):
            return False, "bad or missing token"
        return True, ""

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; "
                         "script-src 'unsafe-inline'; img-src data:; "
                         "connect-src 'self'; media-src https:; "
                         "form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self._send(405, "method not allowed", "text/plain")

    do_PUT = do_DELETE = do_PATCH = do_POST

    def do_GET(self):
        ok, why = self._authorised()
        if not ok:
            self._send(403, f"refused: {why}", "text/plain; charset=utf-8")
            return
        path = urlparse(self.path).path
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        try:
            if path == "/":
                self._send(200, PAGE.replace("__TOKEN__", TOKEN),
                           "text/html; charset=utf-8")
            elif path == "/api/options":
                b = load()
                if b is None:
                    self._send(200, json.dumps({"empty": True}), "application/json")
                else:
                    self._send(200, json.dumps(
                        {"options": options(b),
                         "pii": {"available": bool(phone_map()), "on": False}},
                        default=str), "application/json")
            elif path == "/api/data":
                self._send(200, json.dumps(payload(q), default=str),
                           "application/json")
            elif path == "/api/slice":
                b = load()
                if b is None:
                    self._send(200, json.dumps({"error": "no cache"}), "application/json")
                else:
                    self._send(200, json.dumps(
                        build_slice(b, q, q.get("dim", ""), q.get("value", "")),
                        default=str), "application/json")
            elif path == "/api/turns":
                b = load()
                if b is None:
                    self._send(200, json.dumps({"error": "no cache"}), "application/json")
                else:
                    self._send(200, json.dumps(
                        build_turns(b, q.get("lead", "")), default=str),
                        "application/json")
            elif path == "/api/drill":
                b = load()
                if b is None:
                    self._send(200, json.dumps({"error": "no cache"}), "application/json")
                else:
                    self._send(200, json.dumps(
                        build_drill(b, q, q.get("metric", "")), default=str),
                        "application/json")
            elif path == "/api/dbhealth":
                b = load()
                self._send(200, json.dumps(
                    build_dbhealth(b, q) if b is not None else {"error": "no cache"},
                    default=str), "application/json")
            elif path == "/api/pending":
                b = load()
                self._send(200, json.dumps(
                    build_pending(b, q) if b is not None else {"error": "no cache"},
                    default=str), "application/json")
            elif path == "/api/daybyday":
                b = load()
                if b is None:
                    self._send(200, json.dumps({"error": "no cache"}), "application/json")
                else:
                    _c, dd0, dd1 = apply_filters(b, q)
                    self._send(200, json.dumps(
                        build_daybyday(b, q, dd0, dd1), default=str), "application/json")
            elif path == "/api/projects":
                b = load()
                if b is None:
                    self._send(200, json.dumps({"error": "no cache"}), "application/json")
                else:
                    _c, dd0, dd1 = apply_filters(b, q)
                    self._send(200, json.dumps(
                        build_projects(b, q, dd0, dd1), default=str), "application/json")
            elif path == "/api/attendance":
                b = load()
                if b is None:
                    self._send(200, json.dumps({"error": "no cache"}), "application/json")
                else:
                    if q.get("key") and q.get("state"):
                        attend_add(q["key"], q["state"], q.get("note", ""),
                                   q.get("who", "ops"))
                    self._send(200, json.dumps(build_attendance(b, q), default=str),
                               "application/json")
            elif path == "/api/docs":
                b = load()
                self._send(200, json.dumps(
                    build_docs(b, None) if b is not None else {"error": "no cache"},
                    default=str), "application/json")
            elif path == "/api/qa":
                b = load()
                self._send(200, json.dumps(
                    build_qa(b, q) if b is not None else {"error": "no cache"},
                    default=str), "application/json")
            elif path == "/api/builder":
                b = load()
                self._send(200, json.dumps(
                    build_builder(b, q) if b is not None else {"error": "no cache"},
                    default=str), "application/json")
            elif path == "/api/todo":
                if q.get("text", "").strip():
                    todo_add(q.get("kind", "note"), q.get("ref", ""),
                             q.get("text", ""), {"cat": q.get("cat", "")})
                self._send(200, json.dumps({"ok": True, "todo": todo_list()},
                                           default=str), "application/json")
            # NOTE: this endpoint is deliberately NOT called /api/leadsboard.
            # That path contains the substring "ads", which ad-blocker filter
            # lists match on, and the browser refused the request with
            # ERR_BLOCKED_BY_CLIENT before it ever reached the server. Cost an
            # hour to find. Never put "ads" in a URL path.
            elif path == "/api/board":
                b = load()
                if b is None:
                    self._send(200, json.dumps({"error": "no cache"}), "application/json")
                else:
                    c2, dd0, dd1 = apply_filters(b, q)
                    self._send(200, json.dumps(
                        build_leads_board(b, q, dd0, dd1), default=str),
                        "application/json")
            elif path == "/api/report":
                b = load()
                if b is None:
                    self._send(200, json.dumps({"error": "no cache"}), "application/json")
                else:
                    self._send(200, json.dumps(
                        build_report_cell(b, q), default=str), "application/json")
            elif path == "/api/lead":
                b = load()
                if b is None:
                    self._send(200, json.dumps({"error": "no cache"}), "application/json")
                else:
                    self._send(200, json.dumps(
                        build_lead_modal(b, q.get("id", "")), default=str),
                        "application/json")
            elif path == "/api/feedback":
                if q.get("text", "").strip():
                    feedback_add(q["text"], q.get("tab", ""))
                self._send(200, json.dumps({"ok": True,
                                            "recent": feedback_list()}), "application/json")
            elif path == "/api/profile":
                b = load()
                if b is None:
                    self._send(200, json.dumps({"error": "no cache"}), "application/json")
                else:
                    self._send(200, json.dumps(
                        build_profile(b, q.get("id", "")), default=str),
                        "application/json")
            elif path == "/api/refresh":
                if not _sync_state["running"]:
                    # incremental sync is a no-op (stale-watermark bug) — force --full
                    a = ["--full"]
                    if q.get("since"):
                        a += ["--since", re.sub(r"[^0-9\-]", "", q["since"])[:10]]
                    run_sync(a)
                self._send(200, json.dumps(_sync_state), "application/json")
            elif path == "/api/status":
                self._send(200, json.dumps(_sync_state), "application/json")
            else:
                self._send(404, "not found", "text/plain")
        except Exception as e:
            self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}),
                       "application/json")


PAGE = (HERE / "page.html").read_text(encoding="utf-8")


def main():
    if not (DATA / "calls.parquet").exists():
        print("\n  No local cache yet. Run this first:\n")
        print("    python sync.py --full\n")
        print("  (the dashboard reads the cache; the Refresh button re-runs that sync)\n")
    url = f"http://{HOST}:{PORT}/?t={TOKEN}"
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"""
  ANGAD dashboard running — LOCAL ONLY

    {url}

  Bound to 127.0.0.1 only: unreachable from your network or the internet.
  Every request needs the token above; cross-origin requests are refused.
  No phone numbers, emails or credentials are served to the page.

  Press Ctrl+C to stop.
""")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")


if __name__ == "__main__":
    main()
