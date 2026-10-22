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

import gzip
import os
import threading
import time

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

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


def page_version() -> str:
    """The version of the page and its files: the newest change time among
    them. Every API answer names it (X-App-Version), and an open page that
    was built with another one knows that the app has been updated
    (static/js/update.js). Looked up at most once a second."""
    now = time.monotonic()
    if now - _version[0] > 1:
        newest = 0
        for folder in (app.static_folder, os.path.join(app.root_path, app.template_folder)):
            for root, _, files in os.walk(folder):
                for name in files:
                    try:
                        newest = max(newest, os.stat(os.path.join(root, name)).st_mtime)
                    except OSError:
                        pass  # removed meanwhile
        _version[:] = [now, str(int(newest))]
    return _version[1]


_version = [float("-inf"), ""]

# answers worth compressing: text, and long enough to gain from it
COMPRESSED_TYPES = {"application/json", "text/html", "text/plain", "text/css", "application/javascript", "text/javascript"}
COMPRESS_FROM_BYTES = 1024


@app.after_request
def finish(response):
    """Every answer: a few safety headers; the API's: the page version;
    text answers gzip-compressed when the browser takes them so (a board's
    35 KB of JSON become about 3 KB on the way to a phone)."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    if request.path.startswith("/api/"):
        response.headers["X-App-Version"] = page_version()
    if (response.direct_passthrough                       # a file sent as it is (the page's files)
            or "gzip" not in request.headers.get("Accept-Encoding", "").lower()
            or "Content-Encoding" in response.headers
            or response.mimetype not in COMPRESSED_TYPES):
        return response
    body = response.get_data()
    if len(body) < COMPRESS_FROM_BYTES:
        return response
    response.set_data(gzip.compress(body, compresslevel=6))
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(response.get_data()))
    response.vary.add("Accept-Encoding")
    return response


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
    response = app.make_response(render_template("index.html", version=page_version()))
    # the page is asked for anew each time, so its files' versions are current
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/favicon.ico")
def favicon():
    """Browsers and other programs ask for the tab icon here, whatever the
    page says."""
    return send_from_directory(os.path.join(app.static_folder, "icons"), "favicon.ico", mimetype="image/x-icon")


@app.route("/robots.txt")
def robots():
    """Search engines may list the pages, but must not call the API: each of
    its answers can cost requests to the timetable service."""
    return Response("User-agent: *\nAllow: /\nDisallow: /api/\n", mimetype="text/plain")


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
