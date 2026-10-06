"""
Re-evaluate ALL voice QA docs (PASS, FAIL, SKIPPED) with the updated prompt.
Deletes all existing docs, then re-runs the voice QA agent on every call.
"""

import os
import sys
from datetime import datetime, timezone

import pymongo
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

sys.path.insert(0, os.path.dirname(__file__))
from agents.voice_qa import score_call, load_knowledge_base, load_agent_prompt, run_on_new_call

MONGO_URI_PROD    = os.getenv("MONGO_URI_PROD")
MONGO_URI_RESULTS = os.getenv("MONGO_URI_RESULTS")


def run():
    results_client = pymongo.MongoClient(MONGO_URI_RESULTS)
    prod_client    = pymongo.MongoClient(MONGO_URI_PROD)

    qa_col   = results_client["qa_results"]["voice_qa"]
    call_col = prod_client["ASBLVoiceBot"]["call_transcripts"]

    # Step 1 — get ALL call_sids (PASS, FAIL, and SKIPPED)
    existing = list(qa_col.find({}, {"call_sid": 1}))
    call_sids = [d["call_sid"] for d in existing if d.get("call_sid")]
    print(f"Found {len(call_sids)} total docs to re-evaluate (PASS + FAIL + SKIPPED)")

    if not call_sids:
        print("Nothing to do.")
        return

    # Step 2 — delete them so the evaluator treats them as new
    result = qa_col.delete_many({"call_sid": {"$in": call_sids}})
    print(f"Deleted {result.deleted_count} existing docs")

    # Step 3 — fetch full call docs from prod
    calls = list(call_col.find({"call_sid": {"$in": call_sids}}))
    print(f"Fetched {len(calls)} calls from prod")

    if not calls:
        print("No calls found in prod — nothing to evaluate.")
        return

    # Step 4 — re-evaluate with updated prompt
    kb           = load_knowledge_base()
    agent_prompt = load_agent_prompt()

    print(f"Starting re-evaluation of {len(calls)} calls...\n")
    for i, call in enumerate(calls, 1):
        sid = call.get("call_sid", "unknown")
        print(f"[{i}/{len(calls)}] {sid}")
        try:
            run_on_new_call(call, kb, agent_prompt)
        except Exception as e:
            print(f"  ERROR: {e}")

    # Step 5 — summary
    pass_count = qa_col.count_documents({"status": "PASS"})
    fail_count = qa_col.count_documents({"status": "FAIL"})
    skip_count = qa_col.count_documents({"status": "SKIPPED"})
    guardrail  = qa_col.count_documents({"issues.type": "GUARDRAIL_VIOLATION"})

    print(f"\n{'='*50}")
    print(f"Re-evaluation complete.")
    print(f"  PASS             : {pass_count}")
    print(f"  FAIL             : {fail_count}")
    print(f"  SKIPPED (empty transcript only): {skip_count}")
    print(f"  GUARDRAIL_VIOLATION remaining: {guardrail}")


if __name__ == "__main__":
    run()
