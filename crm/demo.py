"""Run the CRM on 1,000 dummy leads, with no database to set up.

    python demo.py        then open http://localhost:5000
    Login: demo@example.com / demo1234

Data lives in memory (see ../demo_data.py) and resets on every start.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import demo_data  # noqa: E402

client = demo_data.install()  # must run before dashboard is imported
os.environ.setdefault("MONGO_URI", "mongodb://demo")
os.environ.setdefault("MONGO_URI_QA_RESULTS", "mongodb://demo")

from werkzeug.security import generate_password_hash  # noqa: E402

# Demo-only login, stored in the in-memory database.
client["analytics_db"]["dashboard_users"].insert_one(
    {"_id": "demo@example.com", "hash": generate_password_hash("demo1234"),
     "role": "admin", "name": "Demo Admin"})

import dashboard  # noqa: E402

# Build lead profiles from the dummy raw data with the app's real refresh pipeline.
with dashboard.app.app_context():
    result = dashboard.cron_refresh(_force_all_zoho=True).get_json()
print(f"[demo] built {result['refreshed']} lead profiles ({result['errors']} errors)")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[demo] open http://localhost:{port}  (login: demo@example.com / demo1234)")
    dashboard.app.run(host="127.0.0.1", port=port)
