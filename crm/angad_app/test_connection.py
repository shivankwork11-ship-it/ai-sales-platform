"""
STEP 0 — Can Python reach the Mongo that Compass is already talking to?

Run this before anything else. It reports exactly which layer is failing and
what to do about it, instead of a raw pymongo stack trace.

    python test_connection.py
"""
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent


def fail(msg, *fixes):
    print(f"\n  FAILED: {msg}")
    for f in fixes:
        print(f"    -> {f}")
    sys.exit(1)


# ---------------------------------------------------------------- .env

env_path = HERE / ".env"
if not env_path.exists():
    fail(f"no .env file at {env_path}",
         "copy .env.example to .env",
         "paste your connection string into MONGO_URI=",
         "in Compass: click the pencil/edit icon on your saved connection - the "
         "URI is in the box at the top. Copy it, then replace <password> with "
         "the real password (Compass masks it).")

try:
    from dotenv import load_dotenv
except ImportError:
    fail("python-dotenv not installed",
         "pip install pymongo pandas pyarrow python-dotenv")

load_dotenv(env_path)
URI = os.getenv("MONGO_URI", "").strip()
DB = os.getenv("MONGO_DB", "angad").strip()

if not URI:
    fail("MONGO_URI is empty in .env", "paste the connection string from Compass")
if URI.startswith("mongodb://USER:PASSWORD@"):
    fail("MONGO_URI is still the example placeholder",
         "replace USER:PASSWORD@host with your real values")
if "<" in URI or ">" in URI:
    fail("MONGO_URI still contains a <placeholder>",
         "Compass masks the password as <password> - substitute the real one",
         "if the password has @ : / ? # % in it, URL-encode those characters "
         "(@ becomes %40, : becomes %3A, / becomes %2F, # becomes %23)")

redacted = re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:****@", URI)
print(f"  .env      : {env_path}")
print(f"  URI       : {redacted}")
print(f"  database  : {DB}")

# ---------------------------------------------------------------- connect

import readonly  # noqa: F401 - patches pymongo to refuse every write

try:
    from pymongo import MongoClient
    from pymongo.errors import (OperationFailure, ServerSelectionTimeoutError,
                                ConfigurationError)
except ImportError:
    fail("pymongo not installed", "pip install pymongo pandas pyarrow python-dotenv")

print("\n  connecting (15s timeout)...")

try:
    client = MongoClient(URI, serverSelectionTimeoutMS=15000)
    info = client.admin.command("ping")
    build = client.server_info()
except ConfigurationError as e:
    fail(f"malformed URI - {e}",
         "SRV URIs (mongodb+srv://) need: pip install \"pymongo[srv]\"",
         "check for stray spaces or a missing scheme")
except ServerSelectionTimeoutError as e:
    fail("could not reach the server (network layer)",
         "Compass may be connecting through an SSH TUNNEL - check Advanced > "
         "Proxy/SSH in your saved connection. If so, open the same tunnel first: "
         "ssh -L 27017:localhost:27017 user@host   then point MONGO_URI at "
         "mongodb://localhost:27017/",
         "or Compass has a TLS/CA certificate configured - copy those file paths "
         "into the URI as &tls=true&tlsCAFile=C:/path/to/ca.pem",
         "or your IP is not allowlisted on the Mongo box / VPN is off",
         f"raw error: {str(e)[:300]}")
except OperationFailure as e:
    if "auth" in str(e).lower():
        fail("authentication failed",
             "wrong username/password, or the wrong authSource",
             "add ?authSource=admin to the URI (users are usually created in admin)",
             "URL-encode special characters in the password",
             f"raw error: {str(e)[:200]}")
    fail(f"server rejected the command: {str(e)[:300]}")
except Exception as e:
    fail(f"{type(e).__name__}: {str(e)[:400]}")

print(f"  CONNECTED - MongoDB {build.get('version')}")

# ---------------------------------------------------------------- inspect

try:
    dbs = client.list_database_names()
    print(f"  databases visible: {dbs}")
    if DB not in dbs:
        print(f"\n  WARNING: '{DB}' is not in that list.")
        print("    -> either the name is different (set MONGO_DB in .env), or")
        print("    -> your user lacks listDatabases and it still exists - "
              "the collection check below is the real test")
except OperationFailure:
    print("  (user cannot list databases - that's fine, checking collections)")

db = client[DB]
try:
    colls = sorted(db.list_collection_names())
except OperationFailure as e:
    fail(f"cannot list collections in '{DB}': {str(e)[:200]}",
         "the user has no read permission on this database",
         "ask for a role with read on 'angad'")

print(f"\n  collections in '{DB}': {colls}")

expected = ["calls", "leads", "scheduledcalls"]
missing = [c for c in expected if c not in colls]
if missing:
    print(f"\n  WARNING: expected collections missing: {missing}")
    print("    -> is this the right database? the reports need calls + leads")

print()
for c in expected:
    if c in colls:
        try:
            n = db[c].estimated_document_count()
            print(f"  {c:<16} ~{n:>9,} docs")
        except OperationFailure as e:
            print(f"  {c:<16} cannot count: {str(e)[:80]}")

# newest call, so you know the data is live and what the date range looks like
if "calls" in colls:
    try:
        newest = db.calls.find_one({}, {"createdAt": 1, "outcome": 1},
                                   sort=[("createdAt", -1)])
        oldest = db.calls.find_one({"createdAt": {"$ne": None}}, {"createdAt": 1},
                                   sort=[("createdAt", 1)])
        if newest:
            print(f"\n  newest call: {newest.get('createdAt')}  ({newest.get('outcome')})")
        if oldest:
            print(f"  oldest call: {oldest.get('createdAt')}")
    except OperationFailure as e:
        print(f"  could not read calls: {str(e)[:120]}")

# is this user able to write? you want the answer to be NO.
try:
    db.command("collstats", "calls")
    can_write = db.client[DB].command({"connectionStatus": 1})
    roles = can_write.get("authInfo", {}).get("authenticatedUserRoles", [])
    if roles:
        print(f"\n  your roles: {[r.get('role') for r in roles]}")
        if any(r.get("role") in ("readWrite", "dbOwner", "root", "dbAdmin")
               for r in roles):
            print("    NOTE: this user can WRITE to production. Reporting should "
                  "use a read-only user - ask for one if you can.")
except Exception:
    pass

client.close()
print("\n  All good. Next:\n    python probe.py\n    python sync.py --full --days 180")
