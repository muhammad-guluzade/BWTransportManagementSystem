"""
Flask app for looking up stops in Baden-Württemberg and showing live
departures grouped by transport mode, then by platform within each mode.

This file only holds the routes:
  efa/client.py  -- HTTP calls to the EFA API (with caching)
  efa/parse.py   -- raw EFA JSON -> clean values (modes, platforms, ...)
  services/      -- stop search and the departure board
"""

from flask import Flask, jsonify, render_template, request

from efa.client import EfaError
from services import departures, stops

app = Flask(__name__)


@app.errorhandler(EfaError)
def efa_unavailable(error):
    app.logger.warning("EFA request failed: %s", error)
    return jsonify({"error": "The timetable service is not responding. Please try again."}), 502


@app.route("/api/search_stops")
def search_stops():
    return jsonify(stops.search(request.args.get("q", "")))


@app.errorhandler(departures.UnknownStop)
def unknown_stop(error):
    return jsonify({"error": "This stop could not be found."}), 404


@app.route("/api/departures")
def get_departures():
    stop_id = request.args.get("stop_id", "").strip()
    if not stop_id:
        return jsonify({"error": "missing stop_id"}), 400
    return jsonify(departures.board(stop_id, request.args.get("tab", "")))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/map")
def map_page():
    return render_template("map.html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
