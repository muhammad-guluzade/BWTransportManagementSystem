"""The navigator's part of the API (step 2): connections from one place to
another (GET /api/v1/journeys), and sights, addresses and streets in the stop
search (`places=1`). Documented in docs/API.md.

Kept apart so that the navigator can be switched off (BW_NAVIGATOR=off in
.env: the site is then the departures app alone, and these answer "not found"
as if they had never existed) or removed: delete this file, services/
journeys.py, the navigator's page files, and the lines marked "navigator"."""
from flask import abort, current_app, jsonify, request

from api import v1
from services import journeys, stops


def on() -> bool:
    return bool(current_app.config.get("NAVIGATOR"))


def plan_journeys():
    if not on():
        abort(404)
    return jsonify(journeys.plan(request.args.get("from", ""), request.args.get("to", ""),
                                 start=v1.chosen_time(request.args.get("when", "")),
                                 arrive=v1.flag("arrive"), dticket=v1.flag("dticket")))


def place_search(*args):
    """The stop search with places, or None when the navigator is off (then
    the search is the stops' alone, `places` unknown)."""
    return stops.route_search(*args) if on() else None


def invalid_place(exc):
    return v1.error(400, "invalid_parameter", str(exc))


def register(bp):
    """Hooks the navigator into the API blueprint (before it is registered)."""
    bp.add_url_rule("/journeys", "plan_journeys", plan_journeys)
    bp.register_error_handler(journeys.InvalidPlace, invalid_place)
    v1.place_search = place_search
    v1.endpoint_list.append(("journeys", "/api/v1/journeys?from={stop_id|lat,lon}&to={stop_id|lat,lon}&when={time}&arrive={0|1}&dticket={0|1}",
                             on))
