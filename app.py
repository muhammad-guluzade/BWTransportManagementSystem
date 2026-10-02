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

from flask import Flask, jsonify, render_template, request

from api import v1

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


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
