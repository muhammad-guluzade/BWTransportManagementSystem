"""
The JSON API, version 1. Documented in docs/API.md.

The web page in templates/ is one client of this API; a mobile app would
be another. Responses carry display text (`name`) together with the stable
values behind it (`id`, `code`), so a client can build its own labels.

Once clients exist, v1 responses must only grow (new fields are fine);
anything that renames or removes a field belongs in a new version.
"""
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request

from api import limits
from efa.client import EfaError
from efa.localtime import from_local
from gtfs.store import StopsNotImported
from services import departures, journeys, stops

bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")
# how often one visitor may ask (api/limits.py); switched off with the app
# setting RATE_LIMIT = False (the tests that ask a lot on purpose)
limiter = limits.Limiter()


def error(status: int, code: str, message: str):
    """Every error has the same shape: a `code` for programs, a `message`
    for people."""
    return jsonify({"error": {"code": code, "message": message}}), status


@bp.before_request
def take_turns():
    kind = limits.KIND_OF_ENDPOINT.get(request.endpoint)
    if not kind or not current_app.config.get("RATE_LIMIT", True):
        return None
    wait = limiter.wait(request.remote_addr or "", kind)
    if not wait:
        return None
    response, status = error(429, "too_many_requests",
                             "Too many requests from your connection. Please wait a moment.")
    response.headers["Retry-After"] = limits.retry_after(wait)
    return response, status


@bp.errorhandler(EfaError)
def efa_unavailable(exc):
    current_app.logger.warning("EFA request failed: %s", exc)
    return error(502, "timetable_unavailable", "The timetable service is not responding. Please try again.")


@bp.errorhandler(departures.UnknownStop)
def unknown_stop(exc):
    return error(404, "unknown_stop", "This stop could not be found.")


@bp.errorhandler(departures.UnknownDeparture)
def unknown_departure(exc):
    return error(404, "unknown_departure", "This departure is no longer available.")


@bp.errorhandler(departures.UnknownPlatform)
def unknown_platform(exc):
    return error(404, "unknown_platform", "This platform is not one of this stop's.")


@bp.errorhandler(stops.InvalidArea)
def invalid_area(exc):
    return error(400, "invalid_parameter", str(exc))


class InvalidTime(ValueError):
    pass


@bp.errorhandler(InvalidTime)
def invalid_time(exc):
    return error(400, "invalid_parameter", str(exc))


def chosen_time(text: str) -> datetime | None:
    """The `when` of a departures request as a UTC moment, to the minute;
    None for now. "2026-10-25T08:00" is German local time (what people read
    on the station's clock); with "Z" or an offset it is that moment."""
    text = text.strip()
    if not text or text.lower() == "now":
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        raise InvalidTime("when must be a date and time like 2026-10-25T08:00 (German time), or now") from None
    if moment.tzinfo is None:
        moment = from_local(moment)
    if not 2000 <= moment.year <= 2100:
        raise InvalidTime("when is out of range")
    return moment.astimezone(timezone.utc).replace(second=0, microsecond=0)


@bp.errorhandler(journeys.InvalidPlace)
def invalid_place(exc):
    return error(400, "invalid_parameter", str(exc))


@bp.errorhandler(StopsNotImported)
def stops_not_imported(exc):
    return error(503, "stops_not_imported",
                 "The stop database is missing or was built by an older version. Run: python import_stops.py")


@bp.route("/")
def index():
    return jsonify({
        "name": "BW Departures API",
        "version": 1,
        "endpoints": {
            "search_stops": "/api/v1/stops/search?q={text}&near={lat},{lon}&radius={km}&limit={n}&places={0|1}",
            "stops_in_area": "/api/v1/stops?bbox={west},{south},{east},{north}&limit={n}&spread={0|1}&platforms={0|1}",
            "departures": "/api/v1/stops/{stop_id}/departures?tab={tab_id}",
            "platform_departures": "/api/v1/stops/{stop_id}/departures?platform={platform_id}",
            "departure_stops": "/api/v1/stops/{stop_id}/departures/{departure_id}/stops",
            "journeys": "/api/v1/journeys?from={stop_id|lat,lon}&to={stop_id|lat,lon}&when={time}&arrive={0|1}&dticket={0|1}",
        },
        # null until the stop database has been built (python import_stops.py)
        "stop_data": stops.data_info(),
    })


@bp.route("/stops")
def stops_in_area():
    return jsonify(stops.in_area(request.args.get("bbox", ""), request.args.get("limit", ""),
                                 request.args.get("spread", ""), request.args.get("platforms", "")))


@bp.route("/stops/search")
def search_stops():
    args = (request.args.get("q", ""), request.args.get("near", ""), request.args.get("radius", ""), request.args.get("limit", ""))
    if flag("places"):
        # the navigator: sights, addresses and streets too, ranked with the stops
        return jsonify(stops.route_search(*args))
    return jsonify({"stops": stops.search(*args)})


@bp.route("/stops/<stop_id>/departures")
def stop_departures(stop_id):
    return jsonify(departures.board(stop_id.strip(), request.args.get("tab", ""), request.args.get("platform", "").strip(),
                                    trains_together=request.args.get("trains", "").strip().lower() == "together",
                                    start=chosen_time(request.args.get("when", ""))))


@bp.route("/stops/<stop_id>/departures/<departure_id>/stops")
def departure_stops(stop_id, departure_id):
    return jsonify(departures.trip(stop_id.strip(), departure_id.strip()))


def flag(name: str) -> bool:
    return request.args.get(name, "").strip().lower() in ("1", "true", "yes", "on")


@bp.route("/journeys")
def plan_journeys():
    return jsonify(journeys.plan(request.args.get("from", ""), request.args.get("to", ""),
                                 start=chosen_time(request.args.get("when", "")), arrive=flag("arrive"), dticket=flag("dticket")))

