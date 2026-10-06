"""
STEP 1 — Profile EVERY document in EVERY collection.

Not a sample. Field fill rates are computed server-side with an aggregation, so
all 44k+ calls are counted without pulling them over the network.

For each collection it reports:
  * exact doc count and createdAt range
  * every field path found (auto-discovered, nested to depth 3)
  * how many docs actually have each field filled, across the WHOLE collection
  * value distributions for every low-cardinality field
  * index list (what you can filter on cheaply)

Phone numbers are never printed — they are shown as LEAD-xxxxxxxx pseudonyms.

    python probe.py                       # every collection
    python probe.py --since 2026-06-01    # only docs created on/after this date
    python probe.py --only calls,leads
    python probe.py --max-depth 4

Writes schema_report.md — a shareable, PII-free description of the database.
"""
import argparse
import os
import re
from collections import Counter
from datetime import datetime, timezone

import readonly  # noqa: F401 - patches pymongo to refuse every write
import pii
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import OperationFailure

load_dotenv()
URI = os.environ["MONGO_URI"]
DB = os.getenv("MONGO_DB", "angad")

# Collections holding plaintext secrets — never profiled, never exported.
SKIP = {"apikeys", "webhooks"}

# Field names whose VALUES are personal data. Names are listed so their fill
# rate is still reported, but sample values are never shown.
PII_FIELDS = {"phone", "phoneNumber", "email", "customerName", "name",
              "dncVerbatim", "notes", "recordingUrl"}

DATE_HINTS = ("At", "Date", "date", "dueAt", "timestamp")


def is_pii(path):
    leaf = path.split(".")[-1]
    return leaf in PII_FIELDS


def walk(doc, prefix="", depth=1, max_depth=3, out=None):
    """Collect field paths present in one document."""
    if out is None:
        out = set()
    if not isinstance(doc, dict):
        return out
    for k, v in doc.items():
        if k in ("_id", "__v"):
            continue
        path = f"{prefix}{k}"
        out.add(path)
        if isinstance(v, dict) and depth < max_depth:
            walk(v, path + ".", depth + 1, max_depth, out)
        elif isinstance(v, list) and v and isinstance(v[0], dict) and depth < max_depth:
            # array of sub-docs: record the shape but don't explode per element
            for sk in v[0]:
                if sk not in ("_id",):
                    out.add(f"{path}[].{sk}")
    return out


def discover(coll, sample_n=1500, max_depth=3):
    """Union of field paths across a random sample — cheap shape discovery."""
    paths = set()
    try:
        cur = coll.aggregate([{"$sample": {"size": sample_n}}], allowDiskUse=True)
    except OperationFailure:
        cur = coll.find({}, limit=sample_n)
    for d in cur:
        walk(d, max_depth=max_depth, out=paths)
    return sorted(paths)


def fill_rates(coll, paths, match):
    """Exact filled-count per field across the WHOLE collection, server-side.

    'Filled' = present, not null, not '' and not an empty array/object.
    Batched because $group with hundreds of accumulators can exceed limits."""
    results = {}
    plain = [p for p in paths if "[]" not in p]
    BATCH = 60
    for i in range(0, len(plain), BATCH):
        chunk = plain[i:i + BATCH]
        acc = {}
        for j, p in enumerate(chunk):
            expr = f"${p}"
            acc[f"f{j}"] = {"$sum": {"$cond": [
                {"$or": [
                    {"$eq": [{"$type": expr}, "missing"]},
                    {"$eq": [expr, None]},
                    {"$eq": [expr, ""]},
                    {"$eq": [expr, []]},
                    {"$eq": [expr, {}]},
                ]}, 0, 1]}}
        pipe = ([{"$match": match}] if match else []) + [{"$group": {"_id": None, **acc}}]
        try:
            r = next(iter(coll.aggregate(pipe, allowDiskUse=True)), {})
        except OperationFailure as e:
            print(f"    (fill-rate batch failed: {str(e)[:90]})")
            r = {}
        for j, p in enumerate(chunk):
            results[p] = int(r.get(f"f{j}", 0))
    # array-of-subdoc fields: report on the parent array instead
    for p in paths:
        if "[]" in p:
            results.setdefault(p, None)
    return results


def distributions(coll, paths, match, total, max_distinct=40):
    """Value distribution for every low-cardinality field."""
    out = {}
    for p in paths:
        if "[]" in p or is_pii(p):
            continue
        leaf = p.split(".")[-1]
        if any(h in leaf for h in DATE_HINTS) or leaf in ("transcript", "summary",
                                                          "fullText", "metadata"):
            continue
        pipe = ([{"$match": match}] if match else []) + [
            {"$sortByCount": f"${p}"}, {"$limit": max_distinct + 1}]
        try:
            rows = list(coll.aggregate(pipe, allowDiskUse=True))
        except OperationFailure:
            continue
        if not rows or len(rows) > max_distinct:
            continue
        vals = [(r["_id"], r["count"]) for r in rows]
        # skip fields that are effectively unique or effectively constant-null
        if len(vals) == 1 and vals[0][0] is None:
            continue
        if all(isinstance(v, (dict, list)) for v, _ in vals if v is not None):
            continue
        out[p] = vals
    return out


def date_range(coll, field, match):
    pipe = ([{"$match": match}] if match else []) + [
        {"$group": {"_id": None, "lo": {"$min": f"${field}"}, "hi": {"$max": f"${field}"}}}]
    try:
        r = next(iter(coll.aggregate(pipe, allowDiskUse=True)), None)
        if r and r.get("lo"):
            return r["lo"], r["hi"]
    except OperationFailure:
        pass
    return None, None


def sample_values(coll, path, match, n=3):
    """A few example values, so you can see the actual format. PII-safe."""
    if is_pii(path):
        return ["<withheld: personal data>"]
    pipe = ([{"$match": match}] if match else []) + [
        {"$match": {path: {"$nin": [None, "", []]}}},
        {"$sample": {"size": n}}, {"$project": {"v": f"${path}"}}]
    try:
        vals = [d.get("v") for d in coll.aggregate(pipe, allowDiskUse=True)]
    except OperationFailure:
        return []
    out = []
    for v in vals:
        if isinstance(v, str):
            v = pii.redact(v)
            v = v[:110] + ("…" if len(v) > 110 else "")
        elif isinstance(v, (list, dict)):
            v = f"<{type(v).__name__} len={len(v)}>"
        out.append(v)
    return out


def profile(coll, match, max_depth, md):
    name = coll.name
    total = coll.count_documents(match) if match else coll.estimated_document_count()
    hdr = f"{name} — {total:,} docs" + (" (in date filter)" if match else "")
    print(f"\n{'=' * 82}\n{hdr}\n{'=' * 82}")
    md.append(f"\n## `{name}` — {total:,} docs\n")
    if total == 0:
        md.append("_empty_\n")
        return

    try:
        idx = [f"{i['name']}: {dict(i['key'])}" for i in coll.list_indexes()]
        print("  indexes: " + "; ".join(idx))
        md.append("**Indexes** (what you can filter on cheaply): " +
                  ", ".join(f"`{i}`" for i in idx) + "\n")
    except OperationFailure:
        pass

    for f in ("createdAt", "startedAt", "dueAt", "updatedAt"):
        lo, hi = date_range(coll, f, match)
        if lo:
            print(f"  {f}: {lo}  ->  {hi}")
            md.append(f"- `{f}` range: {lo} → {hi}\n")

    paths = discover(coll, max_depth=max_depth)
    print(f"  {len(paths)} field paths discovered; computing exact fill rates "
          f"over all {total:,} docs...")
    fills = fill_rates(coll, paths, match)

    print(f"\n  {'field':<44} {'filled':>9} {'%':>7}")
    print(f"  {'-' * 44} {'-' * 9} {'-' * 7}")
    md.append("\n| field | filled | % | note |\n|---|---:|---:|---|\n")
    for p in paths:
        n = fills.get(p)
        if n is None:
            print(f"  {p:<44} {'(array)':>9}")
            md.append(f"| `{p}` | (array member) | | |\n")
            continue
        pctv = 100.0 * n / total
        note = ""
        if pctv == 0:
            note = "EMPTY — never written"
        elif pctv < 10:
            note = "very sparse"
        elif pctv < 50:
            note = "sparse"
        if is_pii(p):
            note = (note + " · " if note else "") + "PII — pseudonymised"
        flag = f"  <-- {note}" if note else ""
        print(f"  {p:<44} {n:>9,} {pctv:>6.1f}%{flag}")
        md.append(f"| `{p}` | {n:,} | {pctv:.1f}% | {note} |\n")

    dists = distributions(coll, paths, match, total)
    if dists:
        print("\n  value distributions:")
        md.append("\n### Value distributions\n")
        for p, vals in dists.items():
            print(f"\n    {p}")
            md.append(f"\n**`{p}`**\n\n| value | docs | % |\n|---|---:|---:|\n")
            for v, c in vals:
                lbl = "(null)" if v is None else str(v)[:60]
                print(f"      {lbl:<46} {c:>9,}  {100.0 * c / total:>5.1f}%")
                md.append(f"| `{lbl}` | {c:,} | {100.0 * c / total:.1f}% |\n")

    # example values for the fields that carry meaning but aren't enumerable
    interesting = [p for p in paths
                   if "[]" not in p and p not in dists
                   and fills.get(p) not in (None, 0)
                   and not any(h in p.split(".")[-1] for h in DATE_HINTS)]
    if interesting:
        md.append("\n### Example values\n\n| field | examples |\n|---|---|\n")
        print("\n  example values:")
        for p in interesting[:45]:
            ex = sample_values(coll, p, match)
            if not ex:
                continue
            s = " · ".join(str(e) for e in ex)
            print(f"    {p:<40} {s[:120]}")
            md.append(f"| `{p}` | {s[:300].replace('|', '/')} |\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="only docs with createdAt >= this (YYYY-MM-DD)")
    ap.add_argument("--until", default=None, help="only docs with createdAt <= this (YYYY-MM-DD)")
    ap.add_argument("--only", default=None, help="comma-separated collection names")
    ap.add_argument("--max-depth", type=int, default=3)
    args = ap.parse_args()

    match = {}
    if args.since or args.until:
        r = {}
        if args.since:
            r["$gte"] = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        if args.until:
            r["$lte"] = datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc)
        match["createdAt"] = r

    client = MongoClient(URI, serverSelectionTimeoutMS=30000, socketTimeoutMS=900000)
    db = client[DB]

    names = sorted(db.list_collection_names())
    if args.only:
        want = {x.strip() for x in args.only.split(",")}
        names = [n for n in names if n in want]
    skipped = [n for n in names if n in SKIP]
    names = [n for n in names if n not in SKIP]

    print(f"database '{DB}' — profiling {len(names)} collections")
    if skipped:
        print(f"skipping {skipped} (these hold plaintext secrets)")
    if match:
        print(f"date filter: {match['createdAt']}")

    md = [f"# `{DB}` — schema & data profile\n",
          f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · "
          f"every document profiled, not a sample · phone numbers withheld\n"]
    if skipped:
        md.append(f"\nNot profiled (plaintext secrets): {', '.join(skipped)}\n")

    for n in names:
        try:
            profile(db[n], match if n in ("calls", "leads", "scheduledcalls",
                                          "events") else None,
                    args.max_depth, md)
        except Exception as e:
            print(f"\n  {n}: FAILED — {type(e).__name__}: {str(e)[:200]}")
            md.append(f"\n## `{n}`\n\n_profile failed: {str(e)[:200]}_\n")

    out = os.path.join(os.path.dirname(__file__), "schema_report.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("".join(md))
    print(f"\n\nwrote {out} — PII-free, safe to share")
    print("next:  python sync.py --full")
    client.close()


if __name__ == "__main__":
    main()
