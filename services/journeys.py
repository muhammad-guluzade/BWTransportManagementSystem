"""The navigator: connections from one place to another, from EFA's journey
planner, with the departure boards' rules for line names, D-Ticket, brands
and notices (efa/parse.py), the changes checked, and start and destination
in Baden-Württemberg.

A place is a stop id ("de:08111:115") or a point "lat,lon" (a tap on the
map, or the user's location)."""

import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from efa import client, parse
from gtfs import store
from services import departures

log = logging.getLogger(__name__)

# the D-Ticket switch: only the classes whose rides the boards mark valid
DTICKET_CLASSES = sorted(parse.DTICKET_CLASSES)
# more connections are asked for with the switch on: some still fall out
JOURNEYS, JOURNEYS_DTICKET = 6, 8
# a point counts as in Baden-Württemberg when a stop of the state is this near
NEAR_A_STOP_DEGREES = 0.03          # about 3 km
POINT = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
# without a stop database: the state's rough outline
BW_BOX = (47.50, 7.45, 49.85, 10.55)

EMPTY_TOO_CLOSE = "too_close"        # start and destination are the same place
EMPTY_NONE = "none"                  # the planner found nothing (night, no service)
EMPTY_NO_TIMETABLE = "no_timetable"  # outside the timetable period


class InvalidPlace(ValueError):
    """A start or destination that can't be used; the message says why."""


def place(text: str, which: str) -> tuple:
    """(what EFA is asked with, {kind, id, lat, lon}) for "from" / "to"."""
    text = (text or "").strip()
    if not text:
        raise InvalidPlace(f"{which} is missing: a stop id, or a point as lat,lon")
    found = POINT.match(text)
    if found:
        lat, lon = float(found.group(1)), float(found.group(2))
        if not in_baden_wuerttemberg(lat, lon):
            raise InvalidPlace(f"{which} is outside Baden-Württemberg")
        return client.place_param(lat, lon), {"kind": "place", "id": None, "lat": lat, "lon": lon}
    if not parse.is_station_id(text):
        raise InvalidPlace(f"{which} must be a stop id of Baden-Württemberg (from the search) or a point lat,lon")
    known = departures.stop_importance()
    if known and text not in known:
        raise InvalidPlace(f"{which}: no such stop")
    return text, {"kind": "stop", "id": text, "lat": None, "lon": None}


def in_baden_wuerttemberg(lat: float, lon: float) -> bool:
    """A point is in the state when one of its stops is near (the outline of
    the state is not a box: Strasbourg and Basel are in its box). Near the
    border that is not enough (Neu-Ulm): plan() then asks the town's key."""
    try:
        d = NEAR_A_STOP_DEGREES
        stops, total = store.in_area(lon - d, lat - d, lon + d, lat + d, 1)
        return bool(stops or total)
    except (store.StopsNotImported, sqlite3.Error, ValueError):
        south, west, north, east = BW_BOX
        return south <= lat <= north and west <= lon <= east


def town_in_bw(data: dict, end: str) -> bool | None:
    """Is the town the planner found at a journey's "origin" / "destination"
    in Baden-Württemberg (by its municipality key)? None: no answer says."""
    for raw in data.get("journeys") or []:
        legs = raw.get("legs") if isinstance(raw, dict) else None
        leg = (legs[0] if end == "origin" else legs[-1]) if isinstance(legs, list) and legs else None
        location = leg.get(end) if isinstance(leg, dict) else None
        parent = location.get("parent") if isinstance(location, dict) else None
        if isinstance(parent, dict):
            return parse.municipality_in_bw(parent.get("id"))
    return None


def ride_events(raw: dict) -> list:
    return [parse.leg_as_event(leg) for leg in raw.get("legs") or [] if isinstance(leg, dict) and not parse.is_walk(leg)]


def plan(origin: str, destination: str, start: datetime | None = None, arrive: bool = False, dticket: bool = False) -> dict:
    """{from, to, when, arrive, dticket, journeys: [...], empty}. `start` is
    a UTC moment (None: now): the departure, or with `arrive` the latest
    arrival. `dticket`: only connections valid with the Deutschlandticket."""
    asked_from, start_place = place(origin, "from")
    asked_to, end_place = place(destination, "to")
    data = client.get_journeys(asked_from, asked_to, start, arrive, DTICKET_CLASSES if dticket else (),
                               JOURNEYS_DTICKET if dticket else JOURNEYS)
    # a point near the state's border can still be across it (Neu-Ulm): the
    # town the planner found there says which state
    for which, asked, end in (("from", start_place, "origin"), ("to", end_place, "destination")):
        if asked["kind"] == "place" and town_in_bw(data, end) is False:
            raise InvalidPlace(f"{which} is outside Baden-Württemberg")
    importance = departures.stop_importance()
    journeys, seen = [], set()
    for raw in data.get("journeys") or []:
        if not isinstance(raw, dict):
            continue
        try:
            if any(parse.is_excluded(event) for event in ride_events(raw)):
                continue   # flights and on-demand services are not offered
            journey = parse.parse_journey(raw, importance)
        except Exception:  # noqa: BLE001 -- one unreadable connection must not take the others down
            log.warning("Skipping a journey that could not be read", exc_info=True)
            continue
        if journey is None or journey["id"] in seen or not journey["legs"]:
            continue
        if dticket and not journey["dticket"]:
            continue
        seen.add(journey["id"])
        for leg in journey["legs"]:
            if leg["kind"] == "ride" and leg["mode"] in departures.TRAIN_MODES:
                leg["mode"] = departures.TRAINS     # as on the boards: one kind "Trains"
        journeys.append(journey)
    if arrive and start:
        # the planner also offers some that arrive later than asked
        latest = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        in_time = [j for j in journeys if (j["arrival"]["planned"] or "") <= latest]
        journeys = in_time or journeys
    elif not arrive:
        # ... and some that have already left (a minute's grace: the clock of
        # the phone and of the timetable can differ a little)
        earliest = ((start or datetime.now(timezone.utc)) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        journeys = [j for j in journeys if (j["departure"]["time"] or "") >= earliest]
    journeys.sort(key=lambda j: (j["departure"]["time"] or "", j["arrival"]["time"] or ""))
    for journey in journeys:
        for leg in journey["legs"]:
            if leg["kind"] == "ride":
                leg["mode"] = {"id": parse.slug(leg["mode"]), "name": leg["mode"]}

    empty = None
    if not journeys:
        empty = parse.journey_error(data) or (EMPTY_NO_TIMETABLE if departures.outside_timetable(start) else EMPTY_NONE)
    if journeys:
        first, last = journeys[0]["legs"][0]["from"], journeys[0]["legs"][-1]["to"]
        start_place.update({"name": first["name"], "lat": start_place["lat"] or first["lat"], "lon": start_place["lon"] or first["lon"]})
        end_place.update({"name": last["name"], "lat": end_place["lat"] or last["lat"], "lon": end_place["lon"] or last["lon"]})
    else:
        start_place["name"] = end_place["name"] = None
    return {
        "from": start_place,
        "to": end_place,
        "when": departures.when_text(start),
        "arrive": bool(arrive and start),
        "dticket": bool(dticket),
        "journeys": journeys,
        "empty": empty,
    }
