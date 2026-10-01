"""
The JSON API, version 1. Documented in docs/API.md.

The web page in templates/ is one client of this API; a mobile app would
be another. Responses carry display text (`name`) together with the stable
values behind it (`id`, `code`), so a client can build its own labels.

Once clients exist, v1 responses must only grow (new fields are fine);
anything that renames or removes a field belongs in a new version.
"""
from flask import Blueprint, current_app, jsonify, request

from efa.client import EfaError
from services import departures, stops

bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")


def error(status: int, code: str, message: str):
    """Every error has the same shape: a `code` for programs, a `message`
    for people."""
    return jsonify({"error": {"code": code, "message": message}}), status


@bp.errorhandler(EfaError)
def efa_unavailable(exc):
    current_app.logger.warning("EFA request failed: %s", exc)
    return error(502, "timetable_unavailable", "The timetable service is not responding. Please try again.")


@bp.errorhandler(departures.UnknownStop)
def unknown_stop(exc):
    return error(404, "unknown_stop", "This stop could not be found.")


@bp.route("/")
def index():
    return jsonify({
        "name": "BW Departures API",
        "version": 1,
        "endpoints": {
            "search_stops": "/api/v1/stops/search?q={text}",
            "departures": "/api/v1/stops/{stop_id}/departures?tab={tab_id}",
        },
    })


@bp.route("/stops/search")
def search_stops():
    return jsonify({"stops": stops.search(request.args.get("q", ""))})


@bp.route("/stops/<stop_id>/departures")
def stop_departures(stop_id):
    return jsonify(departures.board(stop_id.strip(), request.args.get("tab", "")))
