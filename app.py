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

import os
import threading

from flask import Flask, jsonify, render_template, request

from api import v1
from gtfs import store
from services import stop_index

# A year: page files are asked for with their version in the address (see
# below), so a changed file has a new address and no browser keeps the old one
STATIC_MAX_AGE = 365 * 24 * 3600


class App(Flask):
    def get_send_file_max_age(self, filename):
        # versioned page files never change under their address; without a
        # version the browser asks every time whether the file has changed
        if request.args.get("v"):
            return STATIC_MAX_AGE
        return super().get_send_file_max_age(filename)


app = App(__name__)
app.json.sort_keys = False  # keep fields in the order the API documents them
# a changed page template is used at once, also by a server that keeps running
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.register_blueprint(v1.bp)


@app.url_defaults
def static_version(endpoint, values):
    """Every page file's address carries the file's version (the time it was
    last changed): /static/js/map.js?v=1760700000. A page and its files then
    always belong together: a browser that has an older map.js asks for the
    new one, never mixing old and new files."""
    if endpoint == "static" and "filename" in values:
        try:
            values["v"] = int(os.stat(os.path.join(app.static_folder, values["filename"])).st_mtime)
        except OSError:
            pass  # a missing file: the address stays as it is (and gives 404)


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
@app.route("/stop/<path:stop_id>")
def index(stop_id=None):
    """The one page. A stop's own address (/stop/de:08111:6115) is the same
    page; it opens that stop itself (static/js/address.js)."""
    threading.Thread(target=prepare_search, daemon=True).start()
    response = app.make_response(render_template("index.html"))
    # the page is asked for anew each time, so its files' versions are current
    response.headers["Cache-Control"] = "no-cache"
    return response


if __name__ == "__main__":
    import sys
    if "--phone" in sys.argv:
        # reachable from other devices on the home network (a phone on the same
        # Wi-Fi): http://<this computer's address>:5000. Without the debugger,
        # whose error pages can run code and must never be reachable from outside.
        # It restarts itself when the code changes (as the debug server does),
        # so it never serves an old page with new files.
        app.run(host="0.0.0.0", port=5000, use_reloader=True)
    else:
        app.run(debug=True, port=5000)
