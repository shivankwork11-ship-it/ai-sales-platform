"""Test harness for the ASBL dashboard.

The app talks to MongoDB at import time (dashboard.py creates a MongoClient and
requires MONGO_URI). We patch pymongo.MongoClient -> mongomock BEFORE importing
dashboard, so every collection lives in an in-memory fake DB — deterministic,
no network, safe to run anywhere. Tests seed source collections and exercise the
real routes via Flask's test client.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")

import mongomock          # noqa: E402
import pymongo            # noqa: E402
pymongo.MongoClient = mongomock.MongoClient  # patch before dashboard import

import pytest             # noqa: E402


@pytest.fixture()
def dash():
    """Return the imported dashboard module with a clean in-memory DB per test."""
    import dashboard
    for name in ("analytics_db", "asbl_loft", "Zoho_Database", "ASBLVoiceBot"):
        dashboard.client.drop_database(name)
    dashboard.app.secret_key = "test"
    dashboard.app.config["TESTING"] = True
    return dashboard
