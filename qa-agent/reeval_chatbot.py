"""
Re-evaluate ALL chatbot QA docs with the updated prompt + transcript verification.
Deletes all existing docs, then re-runs the chatbot QA agent on every conversation.
"""

import os
import sys
from datetime import datetime, timezone

import pymongo
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

sys.path.insert(0, os.path.dirname(__file__))
from agents.chatbot_qa import score_conversation, load_knowledge_base, load_agent_prompt, run_on_new_conversation

MONGO_URI_PROD    = os.getenv("MONGO_URI_PROD")
MONGO_URI_RESULTS = os.getenv("MONGO_URI_RESULTS")


def run():
    results_client = pymongo.MongoClient(MONGO_URI_RESULTS)
    prod_client    = pymongo.MongoClient(MONGO_URI_PROD)

    qa_col   = results_client["qa_results"]["chatbot_qa"]
    conv_col = prod_client["asbl_loft"]["conversations"]

    # Step 1 — get ALL conversation_ids
    existing  = list(qa_col.find({}, {"conversation_id": 1}))
    conv_ids  = [d["conversation_id"] for d in existing if d.get("conversation_id")]
    print(f"Found {len(conv_ids)} total docs to re-evaluate")

    if not conv_ids:
        print("Nothing to do.")
        return

    # Step 2 — delete all existing docs
    result = qa_col.delete_many({"conversation_id": {"$in": conv_ids}})
    print(f"Deleted {result.deleted_count} existing docs")

    # Step 3 — fetch full conversations from prod
    convs = list(conv_col.find({"conversationId": {"$in": conv_ids}}))
    print(f"Fetched {len(convs)} conversations from prod")

    if not convs:
        print("No conversations found in prod — nothing to evaluate.")
        return

    # Step 4 — re-evaluate
    kb           = load_knowledge_base()
    agent_prompt = load_agent_prompt()

    print(f"Starting re-evaluation of {len(convs)} conversations...\n")
    prod_mongo_client = pymongo.MongoClient(MONGO_URI_PROD)
    for i, conv in enumerate(convs, 1):
        cid = conv.get("conversationId", "unknown")
        print(f"[{i}/{len(convs)}] {cid}")
        try:
            run_on_new_conversation(conv, kb, agent_prompt, prod_mongo_client)
        except Exception as e:
            print(f"  ERROR: {e}")

    # Step 5 — summary
    pass_count = qa_col.count_documents({"status": "PASS"})
    fail_count = qa_col.count_documents({"status": "FAIL"})
    skip_count = qa_col.count_documents({"status": "SKIPPED"})

    print(f"\n{'='*50}")
    print(f"Re-evaluation complete.")
    print(f"  PASS    : {pass_count}")
    print(f"  FAIL    : {fail_count}")
    print(f"  SKIPPED : {skip_count}")


if __name__ == "__main__":
    run()
