"""
ANGAD Analytics - SANKALPA signature: CI Team - citeam.claudemax@gmail.com

Ingest the clean re-transcription corpus (truth source for conversations).
    python import_retranscripts.py

Sources (user-supplied, Downloads):
  - transcript_comparison.csv        per-call: bot-memory transcript + clean
                                     re-transcription from audio (both sides)
  - site_visit_retranscripts 3.csv   per-lead wide: Call 1..N clean transcripts
  - new_prompt_verification_v2 1.csv per-lead wide: Call 1..N (new prompt era)

PII: phones -> lead_ref (same salt as the cache, so everything joins); names
dropped; phone/email shapes redacted from transcript text. Output is LOCAL
parquet only. audit_pii covers the result like everything else.

Also computes the bot-vs-audio DIVERGENCE score where both versions exist:
similarity of the customer-side text (difflib ratio, 0..1; lower = the stored
transcript strays further from what was actually said).
"""
import difflib
import re
from pathlib import Path

import pandas as pd
import pii

DATA = Path(__file__).parent / "data"
DL = Path("C:/Users/Tiwari/Downloads")

ENG = re.compile(r"\[ENGAGEMENT STATE[^\]]*\]\.?")


def clean_text(t):
    if not isinstance(t, str):
        return None
    return pii.redact(ENG.sub(" ", t)).strip()[:8000]


def customer_side(t):
    """Pull only the customer's lines for divergence comparison."""
    if not isinstance(t, str):
        return ""
    lines = [l.split(":", 1)[1].strip() for l in t.splitlines()
             if re.match(r"^\s*(\[\d\d:\d\d\]\s*)?Customer\s*:", l)]
    return " ".join(lines)


def zoho_bridge():
    """Excel mangled the wide files' phone column (float precision zeroed the
    last digits) - bridge identity via Source Lead ID / PLID through the
    synced Zoho leads table instead."""
    try:
        zl = pd.read_parquet(DATA / "Zoho_Database__leads.parquet")
        clean = lambda s_: s_.astype(str).str.replace(r"\.0$", "", regex=True)
        by_src = dict(zip(clean(zl["source_lead_id"].dropna()),
                          zl.loc[zl["source_lead_id"].notna(), "lead_ref"]))
        by_plid = dict(zip(clean(zl["_id"].dropna()),
                           zl.loc[zl["_id"].notna(), "lead_ref"]))
        return by_src, by_plid
    except Exception:
        return {}, {}


def run():
    frames = []
    by_src, by_plid = zoho_bridge()

    # ---- per-call comparison file (both versions -> divergence)
    f = DL / "transcript_comparison.csv"
    if f.exists():
        df = pd.read_csv(f, encoding="utf-8-sig")
        rows = []
        for _, r in df.iterrows():
            bot = str(r.get("Bot's Own Transcript (from memory)") or "")
            clean = str(r.get("Our Re-transcription (from audio)") or "")
            cb, cc = customer_side(bot.replace("Customer:", "\nCustomer:")), \
                customer_side(clean)
            div = round(1 - difflib.SequenceMatcher(
                None, cb.lower(), cc.lower()).ratio(), 3) if cb and cc else None
            rows.append({
                "lead_ref": pii.ref(r.get("Phone")),
                "call_date": pd.to_datetime(r.get("Call Date"), errors="coerce"),
                "outcome": r.get("Outcome"),
                "duration_s": pd.to_numeric(r.get("Duration (s)"), errors="coerce"),
                "bot_transcript": clean_text(bot),
                "clean_transcript": clean_text(clean),
                "customer_divergence": div,
                "source_file": "transcript_comparison",
                "call_idx": None,
            })
        frames.append(pd.DataFrame(rows))
        print(f"  transcript_comparison: {len(rows)} calls "
              f"(divergence computed on {sum(1 for x in rows if x['customer_divergence'] is not None)})")

    # ---- wide per-lead files (clean transcripts only)
    for name, tag in [("site_visit_retranscripts 3.csv", "site_visit_retranscripts"),
                      ("new_prompt_verification_v2 1.csv", "new_prompt_verification")]:
        f = DL / name
        if not f.exists():
            print(f"  {name}: not found, skipped")
            continue
        df = pd.read_csv(f, encoding="utf-8-sig")
        callcols = [c for c in df.columns if re.fullmatch(r"Call \d+", str(c))]
        rows = []
        for _, r in df.iterrows():
            sid = str(r.get("Source Lead ID") or "").replace(".0", "")
            lid = str(r.get("Lead ID") or "").replace(".0", "")
            ref = by_src.get(sid) or by_plid.get(lid) or pii.ref(r.get("Phone"))
            for i, cc in enumerate(callcols, 1):
                t = r.get(cc)
                if isinstance(t, str) and t.strip():
                    rows.append({
                        "lead_ref": ref, "call_date": pd.NaT, "outcome": None,
                        "duration_s": None, "bot_transcript": None,
                        "clean_transcript": clean_text(t),
                        "customer_divergence": None,
                        "source_file": tag, "call_idx": i,
                        "project": r.get("Project"),
                        "source_lead_id": str(r.get("Source Lead ID") or "")[:40],
                    })
        frames.append(pd.DataFrame(rows))
        print(f"  {tag}: {len(rows)} call transcripts from {len(df)} leads")

    if not frames:
        print("nothing ingested")
        return

    out = pd.concat(frames, ignore_index=True)
    from sync import to_parquet_safe
    # MERGE, never overwrite: a new batch must not destroy corpora imported
    # earlier. Rows are replaced only for the source files present in THIS run;
    # everything else is preserved, and the previous file is kept as a backup.
    p = DATA / "retranscripts.parquet"
    if p.exists():
        try:
            prev = pd.read_parquet(p)
            tags = set(out["source_file"].dropna())
            keep = prev[~prev["source_file"].isin(tags)] if "source_file" in prev.columns else prev
            if len(keep):
                print(f"  preserving {len(keep):,} rows from earlier batches: "
                      f"{sorted(set(keep['source_file'].dropna()))}")
                out = pd.concat([keep, out], ignore_index=True)
            stamp = pd.Timestamp.now().strftime("%Y%m%d-%H%M%S")
            p.replace(p.with_name(f"retranscripts.bak-{stamp}.parquet"))
        except Exception as e:
            print(f"  (merge guard skipped: {type(e).__name__}: {str(e)[:60]})")
    to_parquet_safe(out, p)
    n_div = out["customer_divergence"].notna().sum()
    if n_div:
        med = out["customer_divergence"].median()
        print(f"\n  stored {len(out):,} rows -> retranscripts.parquet")
        print(f"  divergence available on {n_div} calls, median {med:.2f} "
              "(0 = stored transcript matches audio; 1 = totally different)")
    else:
        print(f"\n  stored {len(out):,} rows -> retranscripts.parquet")


if __name__ == "__main__":
    run()
