"""Run the QA dashboard on dummy results from all five agents, with no database.

    python demo.py        then open http://localhost:5050

Data lives in memory (see ../demo_data.py) and resets on every start.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import demo_data  # noqa: E402

demo_data.install()  # must run before dashboard is imported

import dashboard  # noqa: E402

if __name__ == "__main__":
    print("[demo] open http://localhost:5050")
    dashboard.app.run(host="127.0.0.1", port=5050)
