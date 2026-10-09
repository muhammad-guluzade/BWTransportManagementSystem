"""
Turn raw EFA JSON into clean, uniform values.

Everything that depends on EFA's field names or regional quirks lives here,
so a change on EFA's side only needs fixing in this file. The rules were
checked against real responses with `python debug_efa.py --survey`.
"""
import base64
import binascii
import re
from datetime import datetime, timezone

# EFA product class -> our mode group. Only classes listed here are trusted;
# anything else becomes "Other" so it can't be mislabelled silently.
MODE_GROUPS = {
    0: "Regional train",
    13: "Regional train",
    1: "S-Bahn",
    2: "U-Bahn / Tram",
    3: "U-Bahn / Tram",
    4: "U-Bahn / Tram",
    5: "Bus",
    6: "Bus",
    7: "Bus",
    17: "Bus",  # rail replacement
    19: "Bus",  # community bus
    10: "On-demand",
    8: "Cable car",
    9: "Ferry",
    14: "Long-distance train",
    15: "Long-distance train",
    16: "Long-distance train",
}
RAIL_CLASSES = {0, 1, 13, 14, 15, 16}
# attributes EFA sets on services with their own fare system
SPECIAL_FARE_ATTRS = {"LONG_DISTANCE_TRAINS", "SUPPLEMENT", "DIFFERENT_FARES", "HIGHSPEEDTRAIN"}
# classes where local tickets normally apply; others get no D-Ticket label
DTICKET_CLASSES = {0, 1, 2, 3, 4, 5, 6, 7, 13, 17, 19}

# Tabs on a station page: (id, name, EFA product classes). A tab loads only
# its own classes, so e.g. buses can't crowd the trains out of the list.
TABS = [
    ("trains", "Trains", (0, 13, 14, 15, 16)),
    ("sbahn", "S-Bahn", (1,)),
    ("tram", "U-Bahn / Tram", (2, 3, 4)),
    ("bus", "Bus", (5, 6, 7, 17, 19)),
    ("ferry", "Ferry", (9,)),
    ("cablecar", "Cable car", (8,)),
]
OTHER_TAB = ("other", "Other")
# Not scheduled public transport, so left out of the app entirely:
# 10 = on-demand taxis / call buses, 12 = flights
EXCLUDED_CLASSES = {10, 12}

UNKNOWN_PLATFORM = "Unknown platform"
PLATFORM_PREFIX = re.compile(r"^(gleis|bstg\.?|bussteig|bahnsteig|steig|pos\.?|platform)\s*", re.IGNORECASE)
# a bare word like "Flix" or "Ein" is not a platform name
PLATFORM_JUNK = re.compile(r"^[^\W\d_]{3,}$")


def slug(name: str) -> str:
    """A stable id for a display name: 'U-Bahn / Tram' -> 'u_bahn_tram'.
    Clients (e.g. a mobile app) key translations and icons on it."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def lat_lon(coord) -> tuple:
    """(lat, lon) from a rapidJSON `coord` pair, or (None, None)."""
    try:
        lat, lon = float(coord[0]), float(coord[1])
    except (TypeError, ValueError, IndexError):
        return None, None
    return lat, lon


def as_list(value) -> list:
    """EFA returns a bare dict instead of a list when there is one item."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# --- stop search -----------------------------------------------------------

def extract_points(stopfinder_response: dict) -> list:
    """The matches of a stop search, always as a list."""
    points = stopfinder_response.get("stopFinder", {}).get("points")
    # single match: {"point": {...}}; multiple matches: [{...}, {...}]
    if isinstance(points, dict):
        points = points.get("point", points)
    return as_list(points)


def parse_stop(point: dict) -> dict | None:
    """One stop-search match as {id, name, lat, lon, quality, classes}, or
    None if it isn't a stop (the search also returns addresses and POIs)."""
    if point.get("anyType") != "stop":
        return None
    ref = point.get("ref") or {}
    stop_id = ref.get("gid") or point.get("stateless") or ref.get("id")
    if not stop_id:
        return None
    try:
        quality = int(point.get("quality", 0))
    except (TypeError, ValueError):
        quality = 0
    # "modes" is a comma-separated list of the product classes served at the stop
    classes = [int(m) for m in (point.get("modes") or "").split(",") if m.strip().isdigit()]
    # the stop search gives "lon,lat" as one string
    lon, lat = lat_lon(str(ref.get("coords") or "").split(","))
    return {
        "id": stop_id,
        # the full name keeps the district, e.g. "Schwenningen a.N., Bahnhof"
        # where mainLoc would only say "Villingen-Schwenningen"
        "name": point.get("name") or point.get("object", ""),
        "lat": lat,
        "lon": lon,
        "quality": quality,
        "classes": classes,
    }


def in_baden_wuerttemberg(stop_id: str) -> bool:
    """Global stop ids start with the country and state code; 08 is BW."""
    return stop_id.startswith("de:08")


# --- station ---------------------------------------------------------------

def tabs_for(classes) -> list:
    """The tabs a station with these product classes could have, as
    [{id, name, classes}]. Classes we don't know end up in an 'Other' tab."""
    classes = set(classes or ()) - EXCLUDED_CLASSES
    tabs = []
    for tab_id, name, tab_classes in TABS:
        if classes & set(tab_classes):
            # ask for the whole family: a station listing only class 16 (ICE)
            # may still have class 15 (IC) departures
            tabs.append({"id": tab_id, "name": name, "classes": list(tab_classes)})
            classes -= set(tab_classes)
    if classes:
        tabs.append({"id": OTHER_TAB[0], "name": OTHER_TAB[1], "classes": sorted(classes)})
    return tabs


def tab_names(classes) -> str:
    """'Trains, Bus' -- what stops at a station, for telling apart two
    search results with the same name."""
    return ", ".join(tab["name"] for tab in tabs_for(classes))


def parse_station(station_response: dict) -> dict | None:
    """A station as {id, name, lat, lon, classes, nearby: [{id, name, lat,
    lon, classes}]}, or None if EFA doesn't know the stop id."""
    locations = station_response.get("locations") or []
    if not locations or locations[0].get("type") != "stop":
        return None
    location = locations[0]
    lat, lon = lat_lon(location.get("coord"))
    station = {"id": location.get("id"), "name": location.get("name") or "", "lat": lat, "lon": lon,
               "classes": [], "nearby": []}
    # assignedStops lists the station itself plus the stations EFA links to it
    for assigned in location.get("assignedStops") or []:
        if assigned.get("id") == station["id"]:
            station["classes"] = assigned.get("productClasses") or []
        elif assigned.get("id") and assigned.get("name"):
            lat, lon = lat_lon(assigned.get("coord"))
            station["nearby"].append({
                "id": assigned["id"], "name": assigned["name"], "lat": lat, "lon": lon,
                "classes": assigned.get("productClasses") or [],
            })
    return station


# --- departures: transport -------------------------------------------------

def is_excluded(event: dict) -> bool:
    """Flights and on-demand services are not shown at all."""
    product = (event.get("transportation") or {}).get("product") or {}
    return product.get("class") in EXCLUDED_CLASSES


def transport_attrs(transportation: dict) -> set:
    return set((transportation.get("properties") or {}).get("attributes") or [])


def mode_group(transportation: dict) -> str:
    cls = (transportation.get("product") or {}).get("class")
    group = MODE_GROUPS.get(cls, "Other")
    if "LONG_DISTANCE_TRAINS" in transport_attrs(transportation):
        if group == "Regional train":
            return "Long-distance train"
        if group == "Bus":
            return "Long-distance bus"
    return group


def dticket_valid(transportation: dict) -> bool | None:
    """True = Deutschlandticket is valid, False = not valid, None = unsure."""
    cls = (transportation.get("product") or {}).get("class")
    if cls in (14, 15, 16) or transport_attrs(transportation) & SPECIAL_FARE_ATTRS:
        return False
    if cls in DTICKET_CLASSES:
        return True
    return None


def line_name(transportation: dict) -> str:
    """Rider-facing line label, e.g. 'U7', 'RE5', 'ICE 1291'."""
    if transportation.get("disassembledName"):
        return transportation["disassembledName"]
    # long-distance trains have no line name, only a type and train number
    props = transportation.get("properties") or {}
    if props.get("trainType"):
        return f"{props['trainType']} {props.get('trainNumber', '')}".strip()
    return transportation.get("number") or transportation.get("name") or "?"


# --- departures: places ----------------------------------------------------

def platform_code(location: dict) -> str | None:
    """The bare platform, without the regional prefix: 'Gleis 3', 'Bstg. A',
    'Pos. 2', '101' -> '3', 'A', '2', '101'. None if EFA gives no platform."""
    props = location.get("properties") or {}
    raw = (props.get("platformName") or props.get("platform") or "").strip()
    code = PLATFORM_PREFIX.sub("", raw).strip()
    if not code or PLATFORM_JUNK.match(code):
        return None
    return code


def platform_label(location: dict) -> str:
    """'Platform 3', 'Platform A', ... or 'Unknown platform'."""
    code = platform_code(location)
    return f"Platform {code}" if code else UNKNOWN_PLATFORM


def locality(location: dict) -> str:
    """City/town of a platform or stop location (platform -> stop -> locality)."""
    node = location
    while node:
        if node.get("type") == "locality":
            return node.get("name") or ""
        node = node.get("parent")
    return ""


def short_name(name: str, city: str) -> str:
    """Drop the city from a stop name when it is the city we are already in:
    'Heidelberg, Seegarten' / 'Karlsruhe Ebertstraße' -> 'Seegarten' / 'Ebertstraße'."""
    # also try the first word, e.g. locality "Freiburg im Breisgau" vs "Freiburg, ..."
    for prefix in filter(None, (city, city.split(" ")[0] if city else "")):
        for sep in (", ", " "):
            if name.startswith(prefix + sep) and len(name) > len(prefix + sep):
                return name[len(prefix + sep):]
    return name


# Points of a trip that are not places to get on or off: the routing points of
# long-distance buses ("Heidelberg, Post Waypoint") and the railway's border
# points ("Schöna (Gr)").
NOT_A_STOP = re.compile(r"(Waypoint|\(Gr\))$")


def station_of(location: dict) -> dict:
    """The station a location of a trip stands for. Most are platforms, with
    the station as their parent; some are stations themselves, and their
    parent is the town."""
    parent = location.get("parent") or {}
    if not parent or location.get("type") == "stop" or parent.get("type") == "locality":
        return location
    return parent


def onward_name(onward_location: dict) -> str:
    return station_of(onward_location).get("name") or onward_location.get("name") or ""


def stop_score(onward_location: dict) -> int:
    """How 'major' an onward stop looks, from data EFA already gives us.

    Weak for ordinary tram/bus stops (they all score about the same), so it
    is only used for stops the stop database doesn't have: see
    `estimated_importance`."""
    classes = set(onward_location.get("productClasses") or [])
    name = onward_name(onward_location).lower()
    score = len(classes)
    if classes & RAIL_CLASSES:
        score += 3
    if re.search(r"hauptbahnhof|hauptbf|\bhbf\b", name):
        score += 3
    elif re.search(r"bahnhof|\bbf\b|\bzob\b", name):
        score += 1
    return score


def estimated_importance(onward_location: dict) -> float:
    """The stop database's importance, estimated for a stop it doesn't have
    (anything outside Baden-Württemberg), so that Frankfurt or Augsburg can
    be compared with the stations it does have.

    The curve is the typical database importance of stops with the same
    `stop_score`, fitted on 2,697 stops known to both (October 2026):
    score 5 -> 6, 10 -> 20, 13 (Frankfurt Hbf) -> 32."""
    return 1.2 + 0.185 * stop_score(onward_location) ** 2


# Trains and long-distance buses stop every few kilometres, so their two most
# important stops are named as they are. City lines stop every few hundred
# metres: there the second stop named must not be the first one's neighbour.
FAR_APART_MODES = {"Regional train", "Long-distance train", "Long-distance bus"}
# The railway's timetable lists border crossings ("Kehl Grenze") like stops.
# Buses really do stop at places called that, trains don't.
RAIL_MODES = {"Regional train", "Long-distance train", "S-Bahn"}
BORDER_POINT = re.compile(r"\bGrenze$")
LONG_DISTANCE_MODES = {"Long-distance train", "Long-distance bus"}


def passed_not_served(location: dict, mode: str) -> bool:
    """Long-distance timetables also list points the vehicle only passes: a
    border crossing, a tram stop on a coach's way through town. Those come
    without a platform and with no time between arriving and leaving; real
    stops have one or the other. (Local lines are different: their stops
    often have neither, so the rule is not for them.)"""
    if mode not in LONG_DISTANCE_MODES or location.get("type") == "platform":
        return False
    arrival = location.get("arrivalTimePlanned")
    return bool(arrival) and arrival == location.get("departureTimePlanned")


def via_stops(event: dict, importance: dict | None = None, horizon: int = 8, count: int = 2) -> list:
    """Major stops one departure calls at next, e.g. U6 to Gerlingen from
    Pragfriedhof -> ['Pragsattel', 'Feuerbach Bf'].

    `importance` is the stop database's {station id: importance}; a stop it
    doesn't have gets an estimate. Without it every stop gets the estimate.

    Looks at the next `horizon` stops before the signed destination and
    returns the most important ones in travel order: on city lines the most
    important stop and the most important one that isn't right next to it,
    on trains simply the `count` most important. If they all look equally
    important, takes every second stop instead. Empty list = the destination
    is the next stop, so there is nothing to add."""
    importance = importance or {}
    here = event.get("location") or {}
    here_name = station_of(here).get("name")
    city = locality(here)
    transportation = event.get("transportation") or {}
    mode = mode_group(transportation)
    signed = (transportation.get("destination") or {}).get("name") or ""
    destination = short_name(signed, city)

    stops = []  # (name, importance), de-duplicated, in travel order
    reached = False
    for o in event.get("onwardLocations") or []:
        name = onward_name(o)
        if not name or name == here_name or NOT_A_STOP.search(name) or any(name == n for n, _ in stops):
            continue
        if passed_not_served(o, mode) or (mode in RAIL_MODES and BORDER_POINT.search(name)):
            continue
        if signed and (name == signed or short_name(name, city) == destination):
            # vehicles often carry on past what their sign says (to the depot,
            # round a ring): nothing after it is on the way there
            reached = True
            break
        known = importance.get(station_of(o).get("id"))
        stops.append((name, estimated_importance(o) if known is None else known))
    if not reached:
        # the sign names the last stop in other words ("Böfingen" for
        # "Ostpreußenweg"): the last stop is the destination
        stops = stops[:-1]
    stops = stops[:horizon]
    if not stops:
        return []

    def best(candidates):
        return max(candidates, key=lambda i: (stops[i][1], -i))  # the earlier one of equals

    if len(stops) > 1 and len({score for _, score in stops}) == 1:
        picked = stops[1::2][:count]
    elif count == 2 and len(stops) > 2 and mode not in FAR_APART_MODES:
        first = best(range(len(stops)))
        apart = [i for i in range(len(stops)) if abs(i - first) > 1] or [i for i in range(len(stops)) if i != first]
        picked = [stops[i] for i in sorted((first, best(apart)))]
    else:
        top = sorted(range(len(stops)), key=lambda i: (-stops[i][1], i))[:count]
        picked = [stops[i] for i in sorted(top)]
    return [short_name(name, city) for name, _ in picked]


# --- departures: one row ---------------------------------------------------

def parse_time(value: str | None) -> datetime | None:
    """EFA timestamps are UTC, e.g. '2026-09-30T23:15:00Z'."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def iso(moment: datetime | None) -> str | None:
    return moment.isoformat().replace("+00:00", "Z") if moment else None


def whole_minutes(delta) -> int:
    """Full minutes of a time difference, rounded towards zero: 40 seconds
    late is not yet "+1 min" (the clock would show the same minute twice)."""
    return int(delta.total_seconds() / 60)


def departure_id(event: dict) -> str:
    """An id for one departure of a board, to ask for its stops later.

    It is made of what identifies the trip at this platform (planned time,
    platform, line, EFA's trip number), so it stays the same from one refresh
    to the next, and it can be decoded again to look the trip up in EFA."""
    transportation = event.get("transportation") or {}
    parts = (
        event.get("departureTimePlanned") or "",
        (event.get("location") or {}).get("id") or "",
        transportation.get("id") or "",
        str((transportation.get("properties") or {}).get("tripCode", "")),
    )
    return base64.urlsafe_b64encode("|".join(parts).encode("utf-8")).decode("ascii").rstrip("=")


def decode_departure_id(token: str) -> dict | None:
    """{planned, platform_id} out of a departure id, or None if it isn't one."""
    try:
        text = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    parts = text.split("|")
    planned = parse_time(parts[0]) if len(parts) == 4 else None
    if not planned or not parts[1]:
        return None
    return {"planned": planned, "platform_id": parts[1]}


def trip_stop(location: dict, city: str, arriving: bool) -> dict:
    """One stop of a trip. `arriving` picks the time shown: when the vehicle
    gets there (stops still ahead) or when it left (stops behind)."""
    station = station_of(location)
    first, second = ("arrival", "departure") if arriving else ("departure", "arrival")
    planned = parse_time(location.get(f"{first}TimePlanned") or location.get(f"{second}TimePlanned"))
    estimated = parse_time(location.get(f"{first}TimeEstimated") or location.get(f"{second}TimeEstimated"))
    lat, lon = lat_lon(location.get("coord"))
    code = platform_code(location)
    stop_id = station.get("id") or ""
    return {
        "id": stop_id,
        "name": short_name(station.get("name") or location.get("name") or "", city),
        "lat": lat,
        "lon": lon,
        # can this stop be opened in the departures endpoint (it is in Baden-Württemberg)?
        "has_board": in_baden_wuerttemberg(stop_id),
        "platform": {"code": code, "name": f"Platform {code}" if code else UNKNOWN_PLATFORM},
        "time": iso(estimated or planned),
        "planned": iso(planned),
        "delay": whole_minutes(estimated - planned) if estimated and planned else None,
        "realtime": estimated is not None,
        "cancelled": location.get("isCancelled") is True,
    }


def trip_stops(locations, city: str, arriving: bool, skip: str = "", mode: str = "") -> list:
    """The stops of a trip before or after this station, in travel order,
    without repeats, without this station itself and without points the
    vehicle only passes."""
    stops = []
    for location in locations if isinstance(locations, list) else []:
        stop = trip_stop(location, city, arriving)
        if not stop["name"] or NOT_A_STOP.search(stop["name"]) or stop["id"] == skip or passed_not_served(location, mode):
            continue
        if stops and stops[-1]["id"] == stop["id"]:
            continue
        stops.append(stop)
    return stops


def parse_trip(event: dict) -> dict:
    """One departure with all the stops of its trip:
    {id, line, destination, here, previous: [...], onward: [...]}.
    `previous` are the stops the vehicle has come from, `onward` the ones
    still ahead, both in travel order; `here` is this station."""
    location = event.get("location") or {}
    transportation = event.get("transportation") or {}
    city = locality(location)
    mode = mode_group(transportation)
    here = trip_stop({**location,
                      "departureTimePlanned": event.get("departureTimePlanned"),
                      "departureTimeEstimated": event.get("departureTimeEstimated"),
                      "isCancelled": event.get("isCancelled")}, city, arriving=False)
    return {
        "id": departure_id(event),
        "line": line_name(transportation),
        "destination": short_name((transportation.get("destination") or {}).get("name") or "?", city),
        "here": here,
        "previous": trip_stops(event.get("previousLocations"), city, arriving=False, skip=here["id"], mode=mode),
        "onward": trip_stops(event.get("onwardLocations"), city, arriving=True, skip=here["id"], mode=mode),
    }


def parse_departure(event: dict, now: datetime | None = None, importance: dict | None = None) -> dict:
    """One stop event as a flat dict the rest of the app works with.
    `importance` ranks the via stops, see `via_stops`."""
    now = now or datetime.now(timezone.utc)
    transportation = event.get("transportation") or {}
    location = event.get("location") or {}
    city = locality(location)

    planned = parse_time(event.get("departureTimePlanned"))
    # prefer the live time if EFA sent one, else the scheduled one
    estimated = parse_time(event.get("departureTimeEstimated"))
    actual = estimated or planned
    destination = short_name((transportation.get("destination") or {}).get("name") or "?", city)

    return {
        "id": departure_id(event),
        "mode": mode_group(transportation),
        "platform": platform_label(location),
        "platform_code": platform_code(location),
        # sub-stop the platform belongs to, e.g. "Hauptbahnhof (tief)"
        "platform_area": short_name(location.get("name") or "", city),
        "line": line_name(transportation),
        "destination": destination,
        # on ring lines the sign on the vehicle names a stop on the way, not
        # the last stop; don't list it a second time as a via stop
        "via": [stop for stop in via_stops(event, importance) if stop != destination],
        "dticket": dticket_valid(transportation) is True,
        "time": iso(actual),
        "planned": iso(planned),
        "minutes": max(0, round((actual - now).total_seconds() / 60)) if actual else None,
        "delay": whole_minutes(estimated - planned) if estimated and planned else None,
        "realtime": estimated is not None,
        "cancelled": event.get("isCancelled") is True,
    }
