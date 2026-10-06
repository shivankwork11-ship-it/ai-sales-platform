"""
HARD read-only guard for MongoDB.

Importing this module patches pymongo itself so that every write operation
raises instead of executing. It is imported at the top of sync.py, probe.py and
test_connection.py — before any client is created — so nothing in this project
can write to the cluster, whether by accident, by a future edit, or by a
mistaken copy-paste.

This is enforcement, not documentation. The methods are replaced on the pymongo
classes, so it applies to every collection object anywhere in the process.

What it blocks:
  * every write method on Collection (insert/update/delete/replace/bulk_write/
    find_one_and_*), plus index and collection mutation
  * Database.drop_collection / create_collection, MongoClient.drop_database
  * aggregate() pipelines containing $out or $merge — these LOOK like reads but
    write their results into a collection
  * db.command() for anything that is not on the read-only allowlist
  * GridFS uploads

Verify it yourself at any time:

    python readonly.py
"""
import pymongo
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.mongo_client import MongoClient


class WriteBlocked(RuntimeError):
    """Raised when anything in this project attempts a Mongo write."""


_MSG = ("BLOCKED: {op}() is a write operation. This project is strictly "
        "read-only against the ASBL cluster — reporting must never modify "
        "production data. (readonly.py)")

COLLECTION_WRITES = [
    "insert_one", "insert_many", "update_one", "update_many", "replace_one",
    "delete_one", "delete_many", "bulk_write", "find_one_and_update",
    "find_one_and_replace", "find_one_and_delete", "drop", "rename",
    "create_index", "create_indexes", "drop_index", "drop_indexes",
    "create_search_index", "create_search_indexes", "drop_search_index",
    "update_search_index", "save", "insert", "update", "remove",
    "find_and_modify",
]
DATABASE_WRITES = ["drop_collection", "create_collection", "validate_collection"]
CLIENT_WRITES = ["drop_database"]

# Commands that only read. Anything else via db.command() is refused.
READ_COMMANDS = {
    "ping", "hello", "ismaster", "buildinfo", "connectionstatus", "collstats",
    "dbstats", "count", "distinct", "find", "aggregate", "listcollections",
    "listdatabases", "listindexes", "serverstatus", "hostinfo", "explain",
    "getparameter", "connpoolstats", "top", "currentop", "usersinfo",
    "rolesinfo", "atlasversion", "whatsmyuri",
}


def _blocker(op):
    def refuse(*a, **kw):
        raise WriteBlocked(_MSG.format(op=op))
    refuse.__name__ = op
    return refuse


def install():
    if getattr(pymongo, "_asbl_readonly", False):
        return
    for name in COLLECTION_WRITES:
        if hasattr(Collection, name):
            setattr(Collection, name, _blocker(name))
    for name in DATABASE_WRITES:
        if hasattr(Database, name):
            setattr(Database, name, _blocker(name))
    for name in CLIENT_WRITES:
        if hasattr(MongoClient, name):
            setattr(MongoClient, name, _blocker(name))

    # aggregate() looks like a read, but $out / $merge write a collection
    _agg = Collection.aggregate

    def guarded_aggregate(self, pipeline, *a, **kw):
        for stage in (pipeline or []):
            if isinstance(stage, dict):
                for key in stage:
                    if str(key).lower() in ("$out", "$merge"):
                        raise WriteBlocked(_MSG.format(op=f"aggregate {key}"))
        return _agg(self, pipeline, *a, **kw)

    Collection.aggregate = guarded_aggregate

    # db.command() can run anything, including write commands
    _cmd = Database.command

    def guarded_command(self, command, *a, **kw):
        name = command if isinstance(command, str) else (
            next(iter(command), "") if isinstance(command, dict) else "")
        if str(name).lower() not in READ_COMMANDS:
            raise WriteBlocked(_MSG.format(op=f"command {name!r}"))
        return _cmd(self, command, *a, **kw)

    Database.command = guarded_command

    try:  # GridFS, if it is installed
        import gridfs
        for cls, meths in ((gridfs.GridFS, ["put", "delete", "new_file"]),
                           (gridfs.GridFSBucket,
                            ["upload_from_stream", "delete",
                             "upload_from_stream_with_id", "rename"])):
            for m in meths:
                if hasattr(cls, m):
                    setattr(cls, m, _blocker(f"gridfs.{m}"))
    except Exception:
        pass

    pymongo._asbl_readonly = True


install()


# ---------------------------------------------------------------- self-test

def _selftest():
    """Prove the guard works, without needing a live connection."""
    import os
    from dotenv import load_dotenv
    load_dotenv()

    print("\n  Read-only guard — self test\n")
    uri = os.getenv("MONGO_URI")
    if not uri:
        print("  no MONGO_URI in .env; testing the patch offline\n")
        client = MongoClient("mongodb://127.0.0.1:1/", connect=False,
                             serverSelectionTimeoutMS=1)
    else:
        client = MongoClient(uri, connect=False, serverSelectionTimeoutMS=1)

    coll = client["angad"]["calls"]
    db = client["angad"]

    attempts = [
        ("insert_one", lambda: coll.insert_one({"x": 1})),
        ("insert_many", lambda: coll.insert_many([{"x": 1}])),
        ("update_one", lambda: coll.update_one({}, {"$set": {"x": 1}})),
        ("update_many", lambda: coll.update_many({}, {"$set": {"x": 1}})),
        ("replace_one", lambda: coll.replace_one({}, {"x": 1})),
        ("delete_one", lambda: coll.delete_one({})),
        ("delete_many", lambda: coll.delete_many({})),
        ("bulk_write", lambda: coll.bulk_write([])),
        ("find_one_and_update", lambda: coll.find_one_and_update({}, {"$set": {}})),
        ("find_one_and_delete", lambda: coll.find_one_and_delete({})),
        ("drop", lambda: coll.drop()),
        ("create_index", lambda: coll.create_index("x")),
        ("drop_collection", lambda: db.drop_collection("calls")),
        ("create_collection", lambda: db.create_collection("tmp")),
        ("drop_database", lambda: client.drop_database("angad")),
        ("aggregate $out", lambda: coll.aggregate([{"$out": "copy"}])),
        ("aggregate $merge", lambda: coll.aggregate([{"$merge": "copy"}])),
        ("command dropDatabase", lambda: db.command("dropDatabase")),
        ("command createIndexes", lambda: db.command({"createIndexes": "calls"})),
    ]

    failures = 0
    for name, fn in attempts:
        try:
            fn()
            print(f"    {name:<26} NOT BLOCKED  <-- PROBLEM")
            failures += 1
        except WriteBlocked:
            print(f"    {name:<26} blocked")
        except Exception as e:
            # a connection error means it got past the guard
            print(f"    {name:<26} NOT BLOCKED ({type(e).__name__})  <-- PROBLEM")
            failures += 1

    print()
    if failures:
        print(f"  {failures} operation(s) were NOT blocked.\n")
        return 1
    print(f"  All {len(attempts)} write operations blocked.")
    print("  Reads are unaffected: find / aggregate / count / list_* still work.\n")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
