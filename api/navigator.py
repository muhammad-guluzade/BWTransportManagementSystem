"""The navigator's part of the API (step 2): connections from one place to
another (GET /api/v1/journeys), and sights, addresses and streets in the stop
search (`places=1`). Documented in docs/API.md.

Kept apart so that the navigator can be switched off (BW_NAVIGATOR=off in
.env: the site is then the departures app alone, and these answer "not found"
as if they had never existed) or removed: delete this file, services/
journeys.py, the navigator's page files, and the lines marked "navigator"."""
from datetime import datetime, timezone

from flask import abort, current_app, jsonify, request

from api import limits, v1
from services import journeys, stops


def on() -> bool:
    return bool(current_app.config.get("NAVIGATOR"))


def plan_journeys():
    if not on():
        abort(404)
    return jsonify(journeys.plan(request.args.get("from", ""), request.args.get("to", ""),
                                 start=v1.chosen_time(request.args.get("when", "")),
                                 arrive=v1.flag("arrive"), dticket=v1.flag("dticket")))


def journeys_live():
    """The live state of a followed connection's rides: ?ride={board_stop}/{departure_id}
    (repeated, at most 8), as the journeys answer names them."""
    if not on():
        abort(404)
    rides = []
    for value in request.args.getlist("ride"):
        stop, _, departure = value.strip().partition("/")
        if not stop or not departure:
            return v1.error(400, "invalid_parameter", "ride must be {board_stop}/{departure_id}")
        rides.append((stop, departure))
    if not rides:
        return v1.error(400, "invalid_parameter", "ride is missing: {board_stop}/{departure_id}, one per ride")
    if len(rides) > journeys.LIVE_RIDES_MAX:
        return v1.error(400, "invalid_parameter", f"at most {journeys.LIVE_RIDES_MAX} rides")
    return jsonify({"rides": journeys.live_rides(rides),
                    "checked": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})


def place_search(*args):
    """The stop search with places, or None when the navigator is off (then
    the search is the stops' alone, `places` unknown)."""
    return stops.route_search(*args) if on() else None


def invalid_place(exc):
    return v1.error(400, "invalid_parameter", str(exc))


def register(bp):
    """Hooks the navigator into the API blueprint (before it is registered)."""
    bp.add_url_rule("/journeys", "plan_journeys", plan_journeys)
    bp.add_url_rule("/journeys/live", "journeys_live", journeys_live)
    # both ask the timetable service: the visitor's timetable allowance (api/limits.py)
    limits.KIND_OF_ENDPOINT.update({"api_v1.plan_journeys": "timetable", "api_v1.journeys_live": "timetable"})
    bp.register_error_handler(journeys.InvalidPlace, invalid_place)
    v1.place_search = place_search
    v1.endpoint_list.append(("journeys", "/api/v1/journeys?from={stop_id|lat,lon}&to={stop_id|lat,lon}&when={time}&arrive={0|1}&dticket={0|1}",
                             on))
    v1.endpoint_list.append(("journeys_live", "/api/v1/journeys/live?ride={board_stop}/{departure_id}&ride=...", on))
