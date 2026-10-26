"""
Turn raw EFA JSON into clean, uniform values.

Everything that depends on EFA's field names or regional quirks lives here,
so a change on EFA's side only needs fixing in this file. The rules were
checked against real responses with `python debug_efa.py --survey`.
"""
import base64
import binascii
import hashlib
import html
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


# what the search finds besides stops, as the navigator names the kinds
PLACE_KINDS = {"poi": "place", "singlehouse": "address", "address": "address", "street": "street"}


def municipality_in_bw(code) -> bool | None:
    """Is a municipality (EFA's "omc" 8421000, or a locality "placeID:8421000:-1")
    in Baden-Württemberg? German municipality keys start with the state, 08
    for Baden-Württemberg (Ulm 08421000; Neu-Ulm across the Danube 09775135).
    None when unknown."""
    found = re.search(r"\d{6,8}", str(code or ""))
    if not found:
        return None
    return found.group(0).zfill(8).startswith("08")


def parse_place(point: dict) -> dict | None:
    """A search match that is not a stop - a sight or building ("poi"), an
    address ("singlehouse"), a street - as {kind, name, lat, lon}; None for
    anything else or without a position. For the navigator: a place to start
    from or go to."""
    kind = PLACE_KINDS.get(point.get("anyType"))
    if not kind:
        return None
    lon, lat = lat_lon(str((point.get("ref") or {}).get("coords") or "").split(","))
    name = (point.get("name") or point.get("object") or "").strip()
    if lat is None or not name or municipality_in_bw((point.get("ref") or {}).get("omc")) is False:
        return None
    return {"kind": kind, "name": name, "lat": lat, "lon": lon}


def in_baden_wuerttemberg(stop_id: str) -> bool:
    """Global stop ids start with the country and state code; 08 is BW."""
    return stop_id.startswith("de:08")


# The shape of a station's global id in Baden-Württemberg: "de:08111:6115";
# 62 of the 29,373 stations have letters in the last part
# ("de:08118:nvbw_bus_11198"). Anything else is no station, and is answered
# without asking the timetable service.
STATION_ID = re.compile(r"de:08\d{3}:[A-Za-z0-9_-]{1,40}")
# what may follow a station's id in the id of one of its platforms: ":1:2",
# and sometimes free text (":0:Hp. 2", ":0:>Berw", ":2:RiN/O")
PLATFORM_PART_LENGTH = 40


def is_station_id(text: str) -> bool:
    return bool(STATION_ID.fullmatch(text or ""))


def is_platform_id_of(platform_id: str, station_id: str) -> bool:
    """Could this be the id of one of the station's platforms? Its own id
    (a simple stop is its only platform), or its id and more parts: short and
    printable (a slash is fine, "RiN/O" is a direction), no ".." or "\\"."""
    if platform_id == station_id:
        return True
    if not platform_id.startswith(station_id + ":"):
        return False
    rest = platform_id[len(station_id) + 1:]
    return 0 < len(rest) <= PLATFORM_PART_LENGTH and rest.isprintable() and ".." not in rest and "\\" not in rest


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


def tab_of(transportation: dict) -> str:
    """The id of the tab a service is listed under: 'bus', 'trains', ..."""
    cls = (transportation.get("product") or {}).get("class")
    return next((tab_id for tab_id, _, classes in TABS if cls in classes), OTHER_TAB[0])


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


# Train types under the name riders know: DB's code for the TGV is "RHT"
# (EFA: "RHT 9570 TGV"; DB Navigator and the station's boards: "TGV 9570")
TRAIN_TYPE_SHOWN = {"RHT": "TGV"}


def line_name(transportation: dict) -> str:
    """Rider-facing line label, e.g. 'U7', 'RE5', 'ICE 1291'."""
    if transportation.get("disassembledName"):
        return transportation["disassembledName"]
    props = transportation.get("properties") or {}
    number = str(transportation.get("number") or "").strip()
    train_number = str(props.get("trainNumber") or "").strip()
    # Services taken over from the Swiss timetable come without the usual line
    # label but with the line in `number` ("S14", "908"), next to a train
    # number that riders never see ("S 23811")
    if number and train_number and train_number not in number:
        return number
    # long-distance trains have no line name, only a type and train number
    if props.get("trainType"):
        kind = TRAIN_TYPE_SHOWN.get(props["trainType"], props["trainType"])
        return f"{kind} {train_number}".strip()
    return number or transportation.get("name") or "?"


def is_replacement(transportation: dict) -> bool:
    """A bus running in place of a train ("Schienenersatzverkehr", SEV). EFA
    lists some of them as trains, at the bus station they leave from."""
    product = (transportation.get("product") or {}).get("name") or ""
    props = transportation.get("properties") or {}
    return ("ersatz" in product.lower() or props.get("trainType") == "BSV"
            or bool(re.match(r"SEV\b", line_name(transportation), re.IGNORECASE)))


def brand(transportation: dict) -> str | None:
    """'ice' for an ICE, 'flix' for FlixTrain and Flixbus, otherwise None:
    for clients that colour these apart. FlixTrain comes from two
    timetables: as "FlixTrain (Sondertarif)" by operator "FlixTrain-de", and
    as train type FLX by operator "FlixTrain"; Flixbus as "Flixbus
    (Sondertarif)" by "FlixBus-de"."""
    product = (transportation.get("product") or {}).get("name") or ""
    operator = (transportation.get("operator") or {}).get("name") or ""
    train_type = (transportation.get("properties") or {}).get("trainType") or ""
    if "flix" in product.lower() or "flix" in operator.lower() or train_type == "FLX":
        return "flix"
    if train_type == "ICE" or product == "ICE":
        return "ice"
    return None


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


def last_station(event: dict) -> str | None:
    """The id of the station where the trip ends; None when no stop follows."""
    onward = event.get("onwardLocations") or []
    return station_of(onward[-1]).get("id") if onward else None


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


def not_on_the_way(onward_location: dict, name: str, mode: str) -> bool:
    """Entries of a trip's stop list where nobody gets on or off."""
    return bool(NOT_A_STOP.search(name) or passed_not_served(onward_location, mode)
                or (mode in RAIL_MODES and BORDER_POINT.search(name)))


def next_stop(event: dict) -> str | None:
    """The first stop after this station: which way the vehicle leaves.
    None at the end of a trip."""
    here = event.get("location") or {}
    here_name = station_of(here).get("name")
    mode = mode_group(event.get("transportation") or {})
    for o in event.get("onwardLocations") or []:
        name = onward_name(o)
        if name and name != here_name and not not_on_the_way(o, name, mode):
            return short_name(name, locality(here))
    return None


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
        if not name or name == here_name or not_on_the_way(o, name, mode) or any(name == n for n, _ in stops):
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
    """{planned, platform_id, line_id, trip} out of a departure id, or None
    if it isn't one."""
    try:
        text = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    parts = text.split("|")
    planned = parse_time(parts[0]) if len(parts) == 4 else None
    if not planned or not parts[1]:
        return None
    return {"planned": planned, "platform_id": parts[1], "line_id": parts[2], "trip": parts[3]}


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
    properties = location.get("properties") or {}
    signposted = (properties.get("platformName") or "").strip()
    platform_lat, platform_lon = lat_lon(location.get("coord"))

    return {
        "id": departure_id(event),
        "mode": mode_group(transportation),
        "platform": platform_label(location),
        "platform_code": platform_code(location),
        # sub-stop the platform belongs to, e.g. "Hauptbahnhof (tief)"
        "platform_area": short_name(location.get("name") or "", city),
        # The platform as a place. EFA's own id of it, and whether the label is
        # the name on the signs ("Gleis 7 Süd") or only EFA's internal number:
        # a signposted name is the same platform whatever the id says, an
        # internal number means nothing without the id.
        "platform_id": location.get("id"),
        "platform_lat": platform_lat,
        "platform_lon": platform_lon,
        "platform_named": bool(signposted),
        # moved to another platform today; the id still is the planned one's
        "platform_moved": bool(signposted) and (properties.get("plannedPlatformName") or signposted).strip() != signposted,
        "next_stop": next_stop(event),
        # where the trip ends (its last station's id; None: no stop after this
        # one) and the destination as EFA names it: to tell two timetables'
        # copies of one train apart (`dedupe`), and a train ending here
        "last_station": last_station(event),
        "destination_full": (transportation.get("destination") or {}).get("name") or "",
        "line": line_name(transportation),
        "destination": destination,
        # on ring lines the sign on the vehicle names a stop on the way, not
        # the last stop; don't list it a second time as a via stop
        "via": [stop for stop in via_stops(event, importance) if stop != destination],
        "dticket": dticket_valid(transportation) is True,
        "replacement": is_replacement(transportation),
        "brand": brand(transportation),
        "time": iso(actual),
        "planned": iso(planned),
        "minutes": max(0, round((actual - now).total_seconds() / 60)) if actual else None,
        "delay": whole_minutes(estimated - planned) if estimated and planned else None,
        "realtime": estimated is not None,
        "cancelled": event.get("isCancelled") is True,
    }


# --- journeys (the journey planner, XML_TRIP_REQUEST2) ----------------------

# a walk (EFA's product classes, both named "footpath"): 100 to or from an
# address or a point on the map, 99 between two stations (Arnulf-Klett-Platz
# to the Hauptbahnhof)
WALK_CLASSES = {99, 100}
# The least time a change needs when the planner gives no walk for it (it
# only says "Fussweg" without minutes): to another platform, and staying on
# the same one. A walk leg brings its own minutes.
CHANGE_OTHER_PLATFORM_MINUTES = 2
CHANGE_SAME_PLATFORM_MINUTES = 1


def is_walk(leg: dict) -> bool:
    product = (leg.get("transportation") or {}).get("product") or {}
    return product.get("class") in WALK_CLASSES or product.get("name") == "footpath"


def leg_as_event(leg: dict) -> dict:
    """A ride of a journey in the shape of a departure-board event, so that
    the board's rules apply to it unchanged: line names, mode, brand,
    D-Ticket, and the departure's id (the same id as on the board of the stop
    it leaves from, so the live checks can look the ride up there)."""
    origin = leg.get("origin") or {}
    return {
        "location": origin,
        "departureTimePlanned": origin.get("departureTimePlanned"),
        "departureTimeEstimated": origin.get("departureTimeEstimated"),
        "transportation": leg.get("transportation") or {},
        "onwardLocations": (leg.get("stopSequence") or [])[1:],
        "isCancelled": origin.get("isCancelled"),
    }


def journey_place(location: dict, city: str, arriving: bool) -> dict:
    """A start, an end or a change of a journey: a stop (with its platform),
    or, for a point on the map, the address the planner found there."""
    place = trip_stop(location, city, arriving)
    kind = "stop" if location.get("type") in ("stop", "platform") else "place"
    if kind == "place":
        place["id"] = ""
        place["has_board"] = False
        place["name"] = location.get("name") or "?"
    place["kind"] = kind
    return place


def plain_text(markup: str) -> str:
    """An operator's notice (HTML) as plain text: line breaks and list items
    kept, everything else dropped. Nothing of it is ever shown as markup."""
    text = re.sub(r"(?i)<\s*(br|/p|/div|/li|/h\d)\b[^>]*>", "\n", markup or "")
    text = re.sub(r"(?i)<\s*li\b[^>]*>", "\n• ", text)
    text = html.unescape(re.sub(r"<[^>]*>", "", text)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def journey_notices(leg: dict) -> list:
    """The operators' notices on a ride: [{title, text, priority}]."""
    notices, seen = [], set()
    for info in leg.get("infos") or []:
        for link in info.get("infoLinks") or []:
            title = (link.get("urlText") or link.get("subtitle") or "").strip()
            text = plain_text(link.get("content") or link.get("htmlText") or "")
            if not (title or text) or (title, text) in seen:
                continue
            seen.add((title, text))
            notices.append({"title": title, "text": text if text != title else "", "priority": info.get("priority") or "normal"})
    return notices


# A leg's way on the map is drawn with the points that matter: a bend of
# less than this (degrees, about 10 m) is not seen even at street level. EFA
# sends every few metres (Freiburg - Konstanz: 41,542 points, 1 MB).
PATH_TOLERANCE = 0.0001


def path_of(leg: dict) -> list:
    """The leg's way on the map: [[lat, lon], ...], simplified."""
    points = []
    for coord in leg.get("coords") or []:
        lat, lon = lat_lon(coord)
        if lat is not None and (not points or points[-1] != [lat, lon]):
            points.append([lat, lon])
    return simplify(points, PATH_TOLERANCE)


def simplify(points: list, tolerance: float) -> list:
    """Douglas-Peucker: the fewest points that keep the line within
    `tolerance` of the original."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        (ay, ax), (by, bx) = points[first], points[last]
        dy, dx = by - ay, bx - ax
        length = (dx * dx + dy * dy) ** 0.5
        far, index = 0.0, None
        for i in range(first + 1, last):
            py, px = points[i]
            distance = abs(dy * (px - ax) - dx * (py - ay)) / length if length else ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            if distance > far:
                far, index = distance, i
        if index is not None and far > tolerance:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    return [point for point, kept in zip(points, keep) if kept]


def change_minutes_needed(walk: dict | None, arriving: dict, leaving: dict) -> int:
    """The least time a change takes: the walk's own minutes, else a little
    more for another platform than for the same one."""
    if walk is not None:
        return walk["minutes"]
    same = arriving["id"] == leaving["id"] and arriving["platform"]["code"] == leaving["platform"]["code"]
    return CHANGE_SAME_PLATFORM_MINUTES if same else CHANGE_OTHER_PLATFORM_MINUTES


def journey_line(line: str, transportation: dict) -> str:
    """A ride's name in a connection. The French railways' copy of a TGV comes
    without a number ("Zug", operator SNCF, a long-distance train); on the boards
    DB's numbered copy stands for it (dedupe), in a connection there is no twin,
    so it is called what it is."""
    attributes = (transportation.get("properties") or {}).get("attributes") or []
    if line == "Zug" and "SNCF" in str((transportation.get("operator") or {}).get("name") or "") and "LONG_DISTANCE_TRAINS" in attributes:
        return "TGV"
    return line


def parse_journey(journey: dict, importance: dict | None = None) -> dict | None:
    """One connection: {id, departure, arrival, minutes, changes, dticket,
    legs: [...], transfers: [...]}. None if it can't be read."""
    raw_legs = [leg for leg in journey.get("legs") or [] if isinstance(leg, dict)]
    if not raw_legs:
        return None
    city = locality(raw_legs[0].get("origin") or {})
    legs = []
    for leg in raw_legs:
        start = journey_place(leg.get("origin") or {}, city, arriving=False)
        end = journey_place(leg.get("destination") or {}, city, arriving=True)
        if is_walk(leg):
            seconds = leg.get("duration") or 0
            legs.append({"kind": "walk", "minutes": max(1, round(seconds / 60)) if seconds else 0,
                         "metres": leg.get("distance"), "from": start, "to": end, "path": path_of(leg)})
            continue
        event = leg_as_event(leg)
        ride = parse_departure(event, importance=importance)
        stops = trip_stops((leg.get("stopSequence") or [])[1:-1], city, arriving=True, skip=start["id"], mode=ride["mode"])
        legs.append({
            "kind": "ride",
            "line": journey_line(ride["line"], leg.get("transportation") or {}),
            "mode": ride["mode"],
            "brand": ride["brand"],
            "destination": ride["destination"],
            "dticket": ride["dticket"],
            "replacement": ride["replacement"],
            # the same ride on the departure board of the stop it leaves from
            # (`board_stop`): GET /stops/{board_stop}/departures/{departure_id}/stops
            "departure_id": ride["id"],
            "board_stop": station_of(leg.get("origin") or {}).get("id") if start["has_board"] else None,
            "from": start,
            "to": end,
            "stops": stops,
            "path": path_of(leg),
            "notices": journey_notices(leg),
        })
    rides = [leg for leg in legs if leg["kind"] == "ride"]
    transfers = []
    for i, leg in enumerate(legs):
        if leg["kind"] != "ride":
            continue
        later = [j for j in range(i + 1, len(legs)) if legs[j]["kind"] == "ride"]
        if not later:
            break
        nxt = legs[later[0]]
        walks = [legs[j] for j in range(i + 1, later[0]) if legs[j]["kind"] == "walk"]
        walk = {"minutes": sum(w["minutes"] for w in walks)} if walks else None
        arrival, leaving = parse_time(leg["to"]["time"]), parse_time(nxt["from"]["time"])
        needed = change_minutes_needed(walk, leg["to"], nxt["from"])
        spare = whole_minutes(leaving - arrival) - needed if arrival and leaving else None
        transfers.append({"at": leg["to"]["name"], "to": nxt["from"]["name"], "walk_minutes": walk["minutes"] if walk else None,
                          "needed_minutes": needed, "spare_minutes": spare, "possible": spare is None or spare >= 0})
    first, last = legs[0]["from"], legs[-1]["to"]
    departure = parse_time(first["time"])
    arrival = parse_time(last["time"])
    key = "|".join(leg.get("departure_id") or f"walk:{leg['minutes']}" for leg in legs)
    return {
        "id": hashlib.sha1(key.encode()).hexdigest()[:16],
        "departure": {"time": first["time"], "planned": first["planned"], "delay": first["delay"]},
        "arrival": {"time": last["time"], "planned": last["planned"], "delay": last["delay"]},
        "minutes": whole_minutes(arrival - departure) if arrival and departure else None,
        "changes": max(0, len(rides) - 1),
        "dticket": bool(rides) and all(r["dticket"] for r in rides),
        "legs": legs,
        "transfers": transfers,
    }


def journey_error(data: dict) -> str | None:
    """The planner's reason for finding nothing: "too_close" (start and
    destination are the same place), else None."""
    for message in data.get("systemMessages") or []:
        if isinstance(message, dict) and message.get("type") == "error" and str(message.get("code")) == "-4007":
            return "too_close"
    return None

