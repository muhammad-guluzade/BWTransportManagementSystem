"""
Flask app for looking up stops in Baden-Württemberg and showing live
departures grouped by transport mode, then by platform within each mode.

This file only wires things together:
  api/v1.py      -- the JSON API (see docs/API.md)
  efa/client.py  -- HTTP calls to the EFA API (with caching)
  efa/parse.py   -- raw EFA JSON -> clean values (modes, platforms, ...)
  services/      -- stop search and the departure board
  templates/     -- the web pages, which use the JSON API like any client
"""

import threading

from flask import Flask, jsonify, render_template, request

from api import v1
from gtfs import store
from services import stop_index

app = Flask(__name__)
app.json.sort_keys = False  # keep fields in the order the API documents them
app.register_blueprint(v1.bp)


@app.errorhandler(404)
@app.errorhandler(405)
def not_found(exc):
    # API clients get JSON errors; browsers get the normal error page
    if request.path.startswith("/api/"):
        code = "not_found" if exc.code == 404 else "method_not_allowed"
        return jsonify({"error": {"code": code, "message": exc.description}}), exc.code
    return exc


def prepare_search():
    """Build the search index while the page loads, so that the first search
    doesn't have to wait for it (it takes a few seconds)."""
    try:
        stop_index.current()
    except (store.StopsNotImported, OSError):
        pass  # no stop database: the search asks the timetable service only


@app.route("/")
def index():
    threading.Thread(target=prepare_search, daemon=True).start()
    return render_template("index.html")


if __name__ == "__main__":
    import sys
    if "--phone" in sys.argv:
        # reachable from other devices on the home network (a phone on the same
        # Wi-Fi): http://<this computer's address>:5000. Without the debugger,
        # whose error pages can run code and must never be reachable from outside.
        app.run(host="0.0.0.0", port=5000)
    else:
        app.run(debug=True, port=5000)
