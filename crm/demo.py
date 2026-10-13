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
# Never reach the real calling relay from the demo: "Trigger Call" stays dormant when
# these are empty, and the app's .env loader won't override a value that's already set.
os.environ["RELAY_CALL_URL"] = ""
os.environ["RELAY_API_TOKEN"] = ""

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

# Bulk Calls tab: upload the dummy campaign sheet through the real upload route, as an admin would.
import io  # noqa: E402
import openpyxl  # noqa: E402

wb = openpyxl.Workbook()
wb.active.append(list(demo_data.BULK_SHEET[0]))
for row in demo_data.BULK_SHEET:
    wb.active.append(list(row.values()))
sheet = io.BytesIO()
wb.save(sheet)
sheet.seek(0)
tc = dashboard.app.test_client()
with tc.session_transaction() as s:
    s.update(user_email="demo@example.com", role="admin", csrf_token="demo")
up = tc.post("/api/bulk-calls/upload", data={"file": (sheet, "campaign.xlsx")},
             headers={"X-CSRF-Token": "demo"}, content_type="multipart/form-data").get_json()
print(f"[demo] bulk campaign uploaded: {up}")

# Main Dashboard tab (ANGAD): build its cache from the SAME dummy data with its real
# sync.py, then serve it on 127.0.0.1:8777, where the CRM's /main-dashboard proxy looks.
import importlib.util  # noqa: E402
import threading  # noqa: E402
import types  # noqa: E402

ANGAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "angad_app")
sys.path.insert(0, ANGAD_DIR)
# sync.py also imports real export files from a Downloads folder; never in the demo.
for _name in ("import_zoho", "import_cohorts", "import_retranscripts"):
    sys.modules[_name] = types.SimpleNamespace(run=lambda: None)
import sync as angad_sync  # noqa: E402
import cif as angad_cif  # noqa: E402


_zspec = importlib.util.spec_from_file_location("angad_import_zoho", os.path.join(ANGAD_DIR, "import_zoho.py"))
angad_import_zoho = importlib.util.module_from_spec(_zspec)
_zspec.loader.exec_module(angad_import_zoho)


def angad_rebuild():
    """sync.py --full, the daily Zoho export import, then cif.py, against the dummy data."""
    import csv
    argv, sys.argv = sys.argv, ["sync.py", "--full"]
    try:
        angad_sync.main()
        export = os.path.join(ANGAD_DIR, "data", "demo_zoho_export.csv")  # data/ is git-ignored
        with open(export, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(demo_data.ZOHO_EXPORT[0]))
            w.writeheader()
            w.writerows(demo_data.ZOHO_EXPORT)
        angad_import_zoho.run(export)
        angad_cif.main()
    finally:
        sys.argv = argv


angad_rebuild()
_spec = importlib.util.spec_from_file_location("angad_dashboard", os.path.join(ANGAD_DIR, "dashboard.py"))
angad = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(angad)
os.environ["ANGAD_TOKEN"] = angad.TOKEN  # the CRM proxy forwards this per-run token


def _angad_demo_refresh(args):
    """ANGAD's "Fetch fresh data" button: rebuild from the dummy data, not a real database."""
    def work():
        angad._sync_state.update(running=True, log="rebuilding from demo data…")
        try:
            angad_rebuild()
            angad._sync_state["log"] = "rebuilt from demo data"
        except Exception as e:
            angad._sync_state["log"] = f"{type(e).__name__}: {e}"
        finally:
            for fn in (angad.load, angad.truth, angad.id_maps, angad.master_maps):
                fn.cache_clear()
            angad._sync_state.update(running=False, finished_at=angad.datetime.now().strftime("%H:%M:%S"))
    threading.Thread(target=work, daemon=True).start()


angad.run_sync = _angad_demo_refresh
_angad_srv = angad.ThreadingHTTPServer((angad.HOST, angad.PORT), angad.Handler)
threading.Thread(target=_angad_srv.serve_forever, daemon=True).start()
print(f"[demo] Main Dashboard (ANGAD) serving on {angad.HOST}:{angad.PORT}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[demo] open http://localhost:{port}  (login: demo@example.com / demo1234)")
    dashboard.app.run(host="127.0.0.1", port=port)
