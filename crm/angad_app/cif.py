"""CIF — Conversation Indicator Framework v1.1 scoring engine.

Implements the 38-indicator framework (CIF_v1.1, June 2026) over the CLEAN
transcript corpus (data/retranscripts.parquet). Never touches the stored
transcripts — the developers measure those at <50% accuracy.

Two classes of indicator:
  COMPUTED  — derivable from the transcript deterministically (this file).
  JUDGE     — needs an LLM read of meaning (relevance, resolution, accuracy).
              Emitted as None so the dashboard shows an honest NA.

Run:  python cif.py        →  data/cif_scores.parquet
Signature: CI Team - citeam.claudemax@gmail.com · Built with Claude (Anthropic)
"""
import hashlib
import re
import pandas as pd
from pathlib import Path

DATA = Path(__file__).parent / "data"

# ---------------------------------------------------------------- lexicons
DEAD = re.compile(r"^(ok+(ay)?|yes|yeah|ya|hmm+|hm+|haan|ha|acha|accha|thik hai|"
                  r"theek hai|fine|right|correct|sure)[.! ]*$", re.I)
MONEY = re.compile(r"\b(budget|lakh|lakhs|crore|cr\b|rupees|price|cost|emi|loan|"
                   r"down\s?payment|finance|bank|pre-?approved)\b", re.I)
LOAN = re.compile(r"\b(emi|loan|finance|bank|pre-?approved|down\s?payment)\b", re.I)
TIMELINE = re.compile(r"\b(month|months|year|years|immediately|soon|next week|"
                      r"this year|by \w+|possession|ready to move)\b", re.I)
FAMILY = re.compile(r"\b(wife|husband|spouse|family|father|mother|parents|"
                    r"in-?laws|partner|discuss with)\b", re.I)
PROPERTY = re.compile(r"\b(sell|selling|my (current )?(flat|house|apartment)|"
                      r"own (a )?(flat|house)|rented|rent)\b", re.I)
ENTHUSIASM = re.compile(r"\b(good|nice|great|interesting|interested|perfect|"
                        r"excellent|wow|super|definitely|sounds good|bahut accha)\b", re.I)
FUTURE_OWN = re.compile(r"\b(i will (buy|take|book)|we will (buy|take|book)|"
                        r"planning to (buy|book)|i'?ll take|we'?ll take|"
                        r"want to (buy|book)|thinking of buying)\b", re.I)
SPECIFIC_Q = re.compile(r"\b(floor|facing|east|west|north|south|sq\.?\s?ft|square feet|"
                        r"carpet|parking|balcony|view|tower|block|unit|bhk|"
                        r"amenit|club|gym|possession date)\b", re.I)
OBJECTION = re.compile(r"\b(expensive|too much|costly|not interested|budget is|"
                       r"far|traffic|later|busy|think about|no time|problem)\b", re.I)


def parse_turns(text):
    """[(speaker, text)] — accepts plain and '[mm:ss]'-stamped clean corpora."""
    out = []
    for line in str(text).splitlines():
        m = re.match(r"^(?:\[\d{1,2}:\d{2}(?::\d{2})?\]\s*)?"
                     r"(Agent|Customer|Bot|Lead)\s*:\s*(.*)$", line.strip())
        if m:
            who = "Agent" if m.group(1) in ("Agent", "Bot") else "Customer"
            out.append((who, m.group(2).strip()))
    return out


def score_call(text):
    """All COMPUTED indicators for one call. JUDGE indicators stay None."""
    turns = parse_turns(text)
    lead = [t for s, t in turns if s == "Customer"]
    agent = [t for s, t in turns if s == "Agent"]
    if not lead or not agent:
        return None
    lw = [len(t.split()) for t in lead]
    aw = [len(t.split()) for t in agent]
    joined = " ".join(lead)
    ex = min(len(lead), len(agent))
    total_w = sum(lw) + sum(aw)
    half = len(lw) // 2

    r = {"exchanges": ex, "short_call": ex < 6}

    # ---- Conversation Quality
    r["EBR"] = round(100 * sum(lw) / total_w, 1) if total_w else None      # 40-60%
    r["RDI"] = round(sum(lw) / len(lw), 1)                                  # >15 words
    r["DER"] = round(100 * sum(1 for t in lead if DEAD.match(t)) / len(lead), 1)
    if ex >= 6 and half:                                                    # >1.0
        a = sum(lw[:half]) / half
        b = sum(lw[half:]) / max(1, len(lw) - half)
        r["CM"] = round(b / a, 2) if a else None
        r["EI"] = round(b, 1)                                               # >20 words
    else:
        r["CM"] = r["EI"] = None
    r["IPS"] = round(100 * sum(1 for w in aw if w > 60) / len(aw), 1)       # <20%

    # ---- Bot Performance (computed subset)
    fes_n = fes_ok = 0
    for i, (who, t) in enumerate(turns[:-1]):
        if who == "Agent" and "?" in t:
            nxt = turns[i + 1]
            if nxt[0] == "Customer":
                fes_n += 1
                if len(nxt[1].split()) > 3 and not DEAD.match(nxt[1]):
                    fes_ok += 1
    r["FES"] = round(100 * fes_ok / fes_n, 1) if fes_n else None            # >60%
    stalls, recov = 0, []
    for i, t in enumerate(lead):
        if len(t.split()) <= 3:
            stalls += 1
            for j in range(i + 1, len(lead)):
                if len(lead[j].split()) > 10:
                    recov.append(j - i)
                    break
    r["RSR"] = round(sum(recov) / len(recov), 2) if recov else None         # <=2

    # ---- Lead Quality
    organic = 0
    for i, (who, t) in enumerate(turns):
        if who == "Customer" and "?" in t:
            prev = turns[i - 1] if i else None
            if not (prev and prev[0] == "Agent" and "?" in prev[1]):
                organic += 1
    r["OQR"] = organic                                                      # >3
    dss = 0.0
    if MONEY.search(joined):    dss += 1.5
    if LOAN.search(joined):     dss += 1.5
    if TIMELINE.search(joined): dss += 1.0
    if FAMILY.search(joined):   dss += 1.0
    if PROPERTY.search(joined): dss += 1.0
    r["DSS"] = round(dss, 1)                                                # >3.5 (max 7)
    r["FRS"] = len([p for p in (MONEY, LOAN, PROPERTY) if p.search(joined)])
    r["AUI"] = "Joint" if FAMILY.search(joined) else "Unknown"

    # ---- Engagement
    r["ACR"] = round(100 * sum(1 for w in lw if w > 10) / len(lw), 1)       # >30%
    r["EM"] = len(ENTHUSIASM.findall(joined))                               # >3

    # ---- Interest Level
    r["FIL"] = len(FUTURE_OWN.findall(joined))                              # >2
    r["SCS"] = sum(1 for t in lead if "?" in t and SPECIFIC_Q.search(t))     # >2
    r["TD"] = 2 if re.search(r"\b(next month|this month|\d+\s?(month|week)s?|"
                             r"immediately|by \w+ \d)", joined, re.I) else (
              1 if TIMELINE.search(joined) else 0)                          # 0/1/2

    # ---- Call Quality
    tail = lead[max(0, int(len(lead) * .75)):]
    r["OC"] = round(100 * sum(1 for t in tail if OBJECTION.search(t)) /
                    max(1, len(lead)), 1)                                   # <30%

    # ---- JUDGE-only (honest NA until the LLM pass runs)
    for k in ("CDS", "RRI", "QRR", "OHR", "CCI", "IAF",   # bot performance
              "TTS",                                       # conversation quality
              "SE", "GLF",                                 # lead quality
              "RAR", "OCS", "QR",                          # engagement
              "CMT", "SVO", "PEQ",                         # interest
              "OHS", "BPQ", "IFQ", "CCT", "NAC"):          # call quality
        r[k] = None
    return r


def fms(row):
    """FMS = CDS·.30 + CM·.15 + DSS_n·.15 + ACR·.15 + SVO_n·.15 + NAC_n·.10,
    −15 if IAF, floor 0. Computable only once the judge pass fills CDS/SVO/NAC."""
    if any(row.get(k) is None or pd.isna(row.get(k)) for k in ("CDS", "SVO", "NAC")):
        return None
    cm = min(max((row.get("CM") or 0) / 1.5, 0), 1) * 100
    v = (row["CDS"] * .30 + cm * .15 + min(row["DSS"] / 7.0, 1.0) * 100 * .15
         + (row.get("ACR") or 0) * .15 + (row["SVO"] + 1) / 4 * 100 * .15
         + row["NAC"] / 4 * 100 * .10)
    if row.get("IAF") == 1:
        v -= 15
    return max(0, round(v, 1))


# ============================================================ CIF v2 additions
# Four changes approved by the user 2026-08-08. Each is listed on the UI so a
# reader can see WHY a number moved.
#
# (1) CORPUS. The Zoho Gemini-2.5-Pro transcripts (~90% accurate, written inside
#     Zoho from 2026-08-07) outrank the audio re-transcriptions, which in turn
#     outrank anything the agent stored live (<50% accurate — never used here).
#     We do NOT throw the older corpus away: it is 3,862 calls against 218, and
#     discarding it would trade accuracy for a sample too small to segment.
#     Every score therefore carries its corpus and accuracy tier, and the UI can
#     filter to tier A alone.
# (2) SUBSTANCE GATE. Conversation-quality indicators are meaningless on a call
#     where the customer never really spoke. A call must clear the gate below
#     before EBR/RDI/DER/CM/EI/ACR are emitted; otherwise they are NA with a
#     reason, not a misleading 0.
# (3) PERSONA ERA. The agent names itself in the greeting. Pooling Radhika,
#     Ankit and Anandita averages different agents into one meaningless number.
# (4) NA REASONS. "needs the LLM judge" and "the field is 100% null upstream"
#     are different problems and must not look identical.

CORPUS_TIER = {                       # name -> (tier, stated accuracy, why)
    "zoho_gemini": ("A", "~90%", "Gemini 2.5 Pro transcription written inside Zoho"),
    "audio_retranscript": ("B", "clean", "re-transcribed from the call audio"),
}

# The agent introduces itself; that is the only per-call persona evidence that
# exists (no prompt_version is persisted). Radhika -> Ankit was dated 7 Aug from
# transcripts; Anandita appears in the Zoho corpus and is a THIRD name.
PERSONA_RX = re.compile(r"\b(?:this is|myself|i am|i'?m)\s+([A-Z][a-z]{2,15})\b")
KNOWN_PERSONAS = {"Radhika", "Ankit", "Anandita"}

# Why each judge indicator is NA. Distinguishing these was approved item (4).
NA_REASON = {
    "judge": "needs the LLM judge pass — meaning, not countable from text",
    "null_upstream": "the source field is 100% null in the database",
}
JUDGE_KEYS = ("CDS", "RRI", "QRR", "OHR", "CCI", "IAF", "TTS", "SE", "GLF",
              "RAR", "OCS", "QR", "CMT", "SVO", "PEQ", "OHS", "BPQ",
              "IFQ", "CCT", "NAC")
# These two are not judge problems — the columns exist and are entirely null,
# which is a DATA problem for the dev team, not a scoring backlog for us.
NULL_UPSTREAM = ("sentiment", "closingScore")

# (2) the substance gate
MIN_CUSTOMER_TURNS = 2
MIN_CUSTOMER_WORDS = 8


def persona_of_text(text):
    """Persona named by the agent in this call, or None."""
    for m in PERSONA_RX.finditer(str(text or "")):
        n = m.group(1).title()
        if n in KNOWN_PERSONAS:
            return n
    return None


def substantive(text):
    """Did the customer actually say anything? (approved item 2)"""
    turns = parse_turns(text)
    lead = [t for s, t in turns if s == "Customer"]
    real = [t for t in lead if len(t.split()) > 2 and not DEAD.match(t)]
    return len(real) >= MIN_CUSTOMER_TURNS and sum(len(t.split()) for t in lead) >= MIN_CUSTOMER_WORDS


# Indicators that describe the CONVERSATION and are nonsense without one.
CONVO_KEYS = ("EBR", "RDI", "DER", "CM", "EI", "ACR", "IPS", "FES", "RSR")


def load_corpus():
    """Both corpora, best-accuracy-first, de-duplicated per (lead, date)."""
    frames = []
    zt = DATA / "Zoho_Database__call_transcripts.parquet"
    if zt.exists():
        z = pd.read_parquet(zt)
        if not z.empty and "transcript" in z.columns:
            frames.append(pd.DataFrame({
                "lead_ref": z.get("lead_ref"),
                "call_date": pd.to_datetime(z.get("at"), errors="coerce"),
                "outcome": z.get("outcome"),
                "project": None,
                "duration_s": None,
                "text": z["transcript"],
                "corpus": "zoho_gemini",
                "source_file": z.get("model", "gemini-2.5-pro"),
                "call_idx": range(len(z)),
            }))
    rt = DATA / "retranscripts.parquet"
    if rt.exists():
        r = pd.read_parquet(rt)
        if not r.empty:
            frames.append(pd.DataFrame({
                "lead_ref": r.get("lead_ref"),
                "call_date": pd.to_datetime(r.get("call_date"), errors="coerce"),
                "outcome": r.get("outcome"),
                "project": r.get("project"),
                "duration_s": r.get("duration_s"),
                "text": r["clean_transcript"],
                "corpus": "audio_retranscript",
                "source_file": r.get("source_file"),
                "call_idx": r.get("call_idx"),
            }))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    # tier A wins where the same call exists in both
    # De-duplicate on the CALL, not the lead-day: a lead can be dialled several
    # times in a day and each dial is its own conversation. Keying on
    # (lead_ref, day) alone collapsed 3,862 rows to 212 and silently threw away
    # most of the corpus — caught by comparing against the previous run's count.
    # De-duplicate on the CONVERSATION ITSELF (lead + transcript hash), not on
    # (lead, day). Two traps caught here:
    #   · a lead is dialled several times a day and each dial is its own call —
    #     keying on lead-day collapsed 3,862 rows to 212;
    #   · `call_date` is NULL on 3,853 of 3,862 retranscript rows and `call_idx`
    #     is <NA> on more, and pandas string-concat PROPAGATES NA, so an
    #     NA-containing key made every such row identical and left exactly ONE
    #     survivor. Never build a dedup key by concatenating nullable columns.
    df["_rank"] = df["corpus"].map({"zoho_gemini": 0, "audio_retranscript": 1})
    df["_day"] = df["call_date"].dt.date
    df["_key"] = (df["lead_ref"].fillna("?").astype(str) + "|"
                  + df["text"].fillna("").astype(str)
                    .map(lambda s: hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:16]))
    df = (df.sort_values("_rank")
            .drop_duplicates(subset=["_key"], keep="first")
            .drop(columns=["_rank", "_key"]))
    return df


def main():
    r = load_corpus()
    if r.empty:
        print("CIF: no corpus available — nothing scored.")
        return
    rows = []
    for _, x in r.iterrows():
        text = x.get("text")
        s = score_call(text)
        if not s:
            continue
        # (2) substance gate — blank the conversation indicators rather than
        #     reporting a confident number computed on one grunt.
        s["substantive"] = bool(substantive(text))
        if not s["substantive"]:
            for k in CONVO_KEYS:
                s[k] = None
        # (3) persona era
        p = persona_of_text(text)
        s["persona"] = p or "unknown"
        # the agent named in the call itself, not an era inferred from a fixed date
        s["persona_era"] = p or "agent not named"
        tier, acc, why = CORPUS_TIER.get(x.get("corpus"), ("?", "?", "?"))
        s.update({"lead_ref": x["lead_ref"], "call_date": x.get("call_date"),
                  "outcome": x.get("outcome"), "project": x.get("project"),
                  "duration_s": x.get("duration_s"), "source_file": x.get("source_file"),
                  "corpus": x.get("corpus"), "accuracy_tier": tier,
                  "accuracy": acc})
        # judged columns pass THROUGH from the corpus when a judge pass has
        # written them — this is what lets FMS switch on with no code change.
        for k in ("CDS", "RRI", "QRR", "OHR", "CCI", "IAF", "TTS", "SE", "GLF",
                  "RAR", "OCS", "QR", "CMT", "SVO", "PEQ", "OHS", "BPQ",
                  "IFQ", "CCT", "NAC"):
            v = x.get(k)
            if v is not None and not (isinstance(v, float) and pd.isna(v)):
                s[k] = v
        rows.append(s)
    df = pd.DataFrame(rows)
    df["FMS"] = df.apply(fms, axis=1)
    df.to_parquet(DATA / "cif_scores.parquet", index=False)

    sub = df[df["substantive"]]
    print(f"CIF v2: {len(df):,} calls scored from {df.corpus.nunique()} corpora")
    for cname, g in df.groupby("corpus"):
        t, acc, why = CORPUS_TIER.get(cname, ("?", "?", "?"))
        print(f"  tier {t} {cname:20s} {len(g):5,} calls  ({acc} — {why})")
    print(f"  substantive (customer actually spoke): {len(sub):,} of {len(df):,} "
          f"({100*len(sub)/max(len(df),1):.1f}%) — conversation indicators are "
          f"reported ONLY on these")
    print()
    print("  indicator medians on SUBSTANTIVE calls only:")
    print(f"    EBR in 40-60 band : {((sub.EBR>=40)&(sub.EBR<=60)).mean()*100:5.1f}%  (target 40-60%)")
    print(f"    RDI median        : {sub.RDI.median():5.1f}   (target >15 words)")
    print(f"    DER median        : {sub.DER.median():5.1f}%  (target <15%)")
    print(f"    DSS median        : {sub.DSS.median():5.1f}   (target >3.5 of 7)")
    print(f"    ACR median        : {sub.ACR.median():5.1f}%  (target >30%)")
    print(f"    OQR median        : {sub.OQR.median():5.1f}   (target >3 per call)")
    print()
    print("  by persona era (RDI / ACR / DSS on substantive calls):")
    for era, g in sub.groupby("persona_era"):
        print(f"    {era:32s} n={len(g):5,}  RDI {g.RDI.median():5.1f}  "
              f"ACR {g.ACR.median():5.1f}  DSS {g.DSS.median():4.1f}")
    named = df[df.persona != "unknown"]
    if len(named):
        print(f"  personas named in-call: {named.persona.value_counts().to_dict()}")
    print("  FMS: NA until the LLM judge pass supplies CDS / SVO / NAC")


if __name__ == "__main__":
    main()
