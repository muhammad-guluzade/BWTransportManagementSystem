"""Departure board: one station's upcoming departures for one transport type
(tab), grouped by mode, then by platform within each mode."""
import logging
import math
import re
import sqlite3
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from efa import client, parse
from efa.localtime import to_local
from gtfs import store

log = logging.getLogger(__name__)

# Display order for mode groups; anything else is appended alphabetically.
MODE_ORDER = [
    "U-Bahn / Tram",
    "S-Bahn",
    "Trains",
    "Regional train",
    "Long-distance train",
    "Bus",
    "Long-distance bus",
    "Ferry",
    "Cable car",
]

# On request (`trains_together`) regional and long-distance trains are one
# mode: at a big station an ICE then stands among the regional trains of its
# platform instead of in a list of its own further down.
TRAINS = "Trains"
TRAIN_MODES = {"Regional train", "Long-distance train"}


def put_trains_together(departures: list) -> list:
    for dep in departures:
        if dep["mode"] in TRAIN_MODES:
            dep["mode"] = TRAINS
    return departures


# why a board has no departures
EMPTY_NEARBY = "nearby"          # nothing here, but linked stations have service
EMPTY_NO_SERVICE = "no_service"  # the timetable has nothing for this station
EMPTY_PLATFORM = "platform"      # the one platform asked for has nothing in the next 24 hours
EMPTY_NO_TIMETABLE = "no_timetable"  # the chosen time is outside the timetable period


def outside_timetable(start: datetime | None) -> bool:
    """Is a chosen moment before or after the period of the timetable we
    know (the stop data's, e.g. 12 July to 12 December)? Now never is;
    without a stop database nothing is known, so no."""
    if start is None:
        return False
    try:
        info = store.info()
    except (store.StopsNotImported, sqlite3.Error):
        return False
    day = to_local(start).date().isoformat()
    return bool((info["valid_from"] and day < info["valid_from"]) or (info["valid_until"] and day > info["valid_until"]))


def when_text(start: datetime | None) -> str | None:
    """The chosen moment as the API writes times (UTC); None for now."""
    return start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if start else None

# EFA only answers with departures of the next 24 hours (a departure 23 hours
# away is found, one 24 hours away is not). A stop served on school days only
# looks dead on a Saturday. So an empty board is asked again for later days,
# in steps a little under EFA's window, for about a week.
LOOK_AHEAD_STEP = timedelta(hours=23)
LOOK_AHEAD_STEPS = 7


# The stops of each departure of a board are remembered for a while, so that
# asking for them (a click on a departure) needs no further request to EFA.
# A page refreshes its board every 30 seconds, which renews them.
TRIP_TTL = 600           # seconds
TRIP_MEMORY = 3000       # departures; the oldest are dropped beyond this
_trips = {}              # departure id -> (expires_at, trip)
_trips_lock = threading.Lock()


class UnknownStop(Exception):
    """EFA has no stop with this id."""


class UnknownDeparture(Exception):
    """This departure can't be found (any more)."""


class UnknownPlatform(Exception):
    """The platform asked for is not one of this station's."""


def mode_sort_key(name: str):
    return (MODE_ORDER.index(name), name) if name in MODE_ORDER else (len(MODE_ORDER), name)


def platform_sort_key(name: str):
    """Numbered platforms in numeric order, then lettered ones, unknown last."""
    if name == parse.UNKNOWN_PLATFORM:
        return (2, 0, name)
    number = re.search(r"\d+", name)
    return (0, int(number.group()), name) if number else (1, 0, name)


def stop_importance() -> dict:
    """{station id: importance} from the stop database, for picking the via
    stops. {} if the database isn't there: a board must work without it, the
    via stops then go by what the timetable service says about each stop."""
    try:
        return store.importance()
    except store.StopsNotImported:
        return {}
    except sqlite3.Error:
        log.warning("Could not read the stop database for the via stops", exc_info=True)
        return {}


def parse_events(events) -> list:
    """Stop events as departure dicts. Flights and on-demand services are
    left out, and so is any event that can't be read: one malformed
    departure from EFA must not take the whole board down."""
    departures = []
    importance = stop_importance()
    for event in events if isinstance(events, list) else []:
        try:
            if parse.is_excluded(event):
                continue
            departure = parse.parse_departure(event, importance=importance)
            # without a departure time there is nothing to show
            if departure["time"]:
                departures.append(departure)
                remember_trip(parse.parse_trip(event))
        except Exception:  # noqa: BLE001 -- whatever EFA sent, skip just this row
            log.warning("Skipping a departure that could not be read", exc_info=True)
    return departures


def remember_trip(trip: dict) -> None:
    now = time.monotonic()
    with _trips_lock:
        _trips.pop(trip["id"], None)   # re-insert, so the dict stays ordered by age
        _trips[trip["id"]] = (now + TRIP_TTL, trip)
        while len(_trips) > TRIP_MEMORY:
            del _trips[next(iter(_trips))]


def trip(stop_id: str, departure_id: str) -> dict:
    """The stops of one departure of a station's board:
    {id, line, destination, here, previous: [...], onward: [...]}.

    Normally answered from memory. If the departure isn't remembered (the
    board was loaded long ago, or the server restarted), EFA is asked once
    for the departures around its planned time."""
    with _trips_lock:
        remembered = _trips.get(departure_id)
    # a departure belongs to one station: under another station's address it is unknown
    if remembered and remembered[0] > time.monotonic() and remembered[1]["here"]["id"] == stop_id:
        return remembered[1]

    wanted = parse.decode_departure_id(departure_id)
    # the id says which platform it leaves from; that must be one of this station's
    if not wanted or not parse.is_station_id(stop_id) or not (wanted["platform_id"] + ":").startswith(stop_id + ":"):
        raise UnknownDeparture(departure_id)
    data = client.get_departures(stop_id, (), start=wanted["planned"] - timedelta(minutes=1))
    for event in data.get("stopEvents") or []:
        try:
            if same_ride(parse.departure_id(event), departure_id, wanted) and not parse.is_excluded(event):
                found = parse.parse_trip(event)
                if found["here"]["id"] == stop_id:
                    remember_trip(found)
                    return found
        except Exception:  # noqa: BLE001 -- an unreadable event is not the one we want
            continue
    raise UnknownDeparture(departure_id)


def same_ride(board_id: str, departure_id: str, wanted: dict) -> bool:
    """Is this departure of the board the one asked for? The same id; or the
    same planned time, line and trip at the same station, whatever platform
    either names: the journey planner and the board do not always agree
    (25 October 2026: a journey's RE1 named only Stuttgart Hbf where the board
    had platform 1, a MEX13 platform 3:3 where the board had 3:2 or only the
    station), and a train can move to another platform."""
    if board_id == departure_id:
        return True
    other = parse.decode_departure_id(board_id)
    station = lambda platform_id: ":".join(platform_id.split(":")[:3])
    return bool(other) and other["planned"] == wanted["planned"] and other["line_id"] == wanted["line_id"]         and other["trip"] == wanted["trip"] and bool(wanted["trip"]) and station(other["platform_id"]) == station(wanted["platform_id"])


def dedupe(departures: list) -> list:
    """EFA sometimes sends one trip twice: the timetable entry plus a copy
    created from live data, or two timetable entries. Keep one, preferring
    the copy with live data; between equals always the same one (the
    smallest id), since EFA's order of the copies changes from one request
    to another, and the id must not."""
    kept = {}
    for dep in departures:
        key = (dep["mode"], dep["platform"], dep["platform_area"], dep["line"], dep["destination"], dep["planned"])
        if key not in kept or (not dep["realtime"], dep["id"]) < (not kept[key]["realtime"], kept[key]["id"]):
            kept[key] = dep
    # Near the Swiss border the same train also comes from a second timetable,
    # without a platform (Waldshut: "S27, platform 5" and "S27" again). The
    # copy that knows its platform is the one to show.
    def trip(dep):
        return dep["mode"], dep["line"], dep["destination"], dep["planned"]
    with_platform = {trip(dep) for dep in kept.values() if dep.get("platform_code")}
    kept = [dep for dep in kept.values() if dep.get("platform_code") or trip(dep) not in with_platform]
    # The French timetable has the Paris trains too, without a number (shown
    # as "Zug") and sometimes at another platform (Stuttgart Hbf: 10 instead
    # of 4). A numberless train is that copy when a numbered one leaves at the
    # same time for the same destination: the same name, or (the names are
    # spelt differently: "München Hbf" / "München Hauptbahnhof") the same last
    # station, or towards the same next stop (23 October 2026: DB's TGV 9574
    # "to Strasbourg", the French copy "to Paris Est", both via Karlsruhe).
    # Then only the numbered one stays, DB's (its platform is the right one),
    # with the copy's route if that goes on further: DB Navigator showed that
    # TGV 9574 to Paris Est. (A train whose label simply has no number, like
    # "SAB", has no such twin.)
    numbered = [dep for dep in kept if dep["mode"] in TRAIN_MODES and re.search(r"\d", dep["line"])]

    def twin_of(dep):
        return next((other for other in numbered
                     if other["planned"] == dep["planned"]
                     and (other["destination"] == dep["destination"]
                          or (dep.get("last_station") and other.get("last_station") == dep["last_station"])
                          or (dep.get("next_stop") and other.get("next_stop") == dep["next_stop"]))), None)
    shown = []
    for dep in kept:
        twin = twin_of(dep) if dep["mode"] in TRAIN_MODES and not re.search(r"\d", dep["line"]) else None
        if twin is None:
            shown.append(dep)
        else:
            take_longer_route(twin, dep)
    return shown


def take_longer_route(kept: dict, copy: dict) -> None:
    """DB's row of a train stays; when the other timetable's copy of it goes on
    further along the same stops (DB's ends at Strasbourg, the copy goes on to
    Paris), the row takes the copy's destination, via stops and stop list."""
    if not kept.get("id") or not copy.get("id"):
        return
    with _trips_lock:
        mine, theirs = _trips.get(kept["id"]), _trips.get(copy["id"])
    if not mine or not theirs:
        return
    stops = lambda trip: [stop.get("id") or stop.get("name") for stop in trip["onward"]]
    short, long = stops(mine[1]), stops(theirs[1])
    if len(long) <= len(short) or long[:len(short)] != short:
        return
    kept.update(destination=copy["destination"], via=copy["via"], last_station=copy.get("last_station"),
                destination_full=copy.get("destination_full"))
    remember_trip({**mine[1], "destination": copy["destination"], "onward": theirs[1]["onward"]})


def plain(name: str) -> str:
    """A name for comparing: lower case, words only ("Stuttgart, Hauptbahnhof"
    and "Stuttgart Hauptbahnhof" are the same)."""
    return " ".join(re.findall(r"\w+", name.lower()))


def ends_here(dep: dict, station: dict) -> bool:
    """A train that ends at this station: EFA lists it among the departures
    (Stuttgart Hbf: "RE90 -> Stuttgart Hauptbahnhof"), though it goes nowhere
    from here. Both must hold: no stop after this one, and this station as the
    destination, so that a departure whose stops EFA left out still shows."""
    if dep.get("last_station") is not None:
        return False
    destination, here = plain(dep.get("destination_full") or ""), plain(station.get("name") or "")
    return bool(destination) and (here.startswith(destination) or destination.startswith(here))


def departing(departures: list, station: dict) -> list:
    """The departures of a station: each trip once (`dedupe`), without the
    trains that end here."""
    return [dep for dep in dedupe(departures) if not ends_here(dep, station)]


# two platform positions this close are one place to stand
SAME_SPOT_METRES = 5
# what a departure row carries only for the grouping
PLATFORM_FIELDS = ("mode", "platform", "platform_code", "platform_area", "platform_id", "platform_lat", "platform_lon",
                   "platform_named", "platform_moved", "next_stop", "last_station", "destination_full")


def metres(a: tuple, b: tuple) -> float:
    """Distance between two (lat, lon) positions; exact enough within a station."""
    return 111_000 * math.hypot(a[0] - b[0], (a[1] - b[1]) * math.cos(math.radians(a[0])))


def position(dep: dict) -> tuple | None:
    lat, lon = dep.get("platform_lat"), dep.get("platform_lon")
    return None if lat is None or lon is None else (lat, lon)


def station_level(platform_id: str) -> bool:
    """Is this the id of a whole station ("de:08226:4252") rather than of one
    of its platforms ("de:08226:4252:3:2")?"""
    return platform_id.count(":") < 3


def split_platforms(rows: list) -> list:
    """The departures under one platform label, as one list per physical
    platform.

    A label that is the name on the signs ("Gleis 7 Süd") is one platform,
    whatever ids EFA attaches to it: trains moved to another platform keep
    the planned one's id, and some stations are in the data twice. A label
    that is only EFA's internal number is not: two bus bays of one station
    can both be number 1. Those are told apart by their ids, unless they sit
    on the same spot. A departure that carries only the station's id (no
    platform part, as EFA sometimes sends) says nothing about which platform
    it is, so it joins the nearest one of its label."""
    together = [dep for dep in rows if dep.get("platform_named") or not dep.get("platform_id") or position(dep) is None]
    groups = []  # [{"spots": [(lat, lon)], "rows": [...]}]
    if together:
        groups.append({"spots": [spot for spot in map(position, together) if spot], "rows": together})

    by_id = {}
    for dep in rows:
        if not any(dep is other for other in together):
            by_id.setdefault(dep["platform_id"], []).append(dep)
    # the busiest first, so that a stray id joins the main platform and not the
    # other way round; the station's own id last
    for platform_id, members in sorted(by_id.items(), key=lambda item: (station_level(item[0]), -len(item[1]), item[0])):
        spot = position(members[0])
        home = next((g for g in groups if any(metres(spot, other) <= SAME_SPOT_METRES for other in g["spots"])), None)
        if not home and station_level(platform_id) and groups:
            home = min(groups, key=lambda g: min((metres(spot, other) for other in g["spots"]), default=math.inf))
        if home:
            home["rows"] += members
            home["spots"].append(spot)
        else:
            groups.append({"spots": [spot], "rows": members})
    return [group["rows"] for group in groups]


def platform_place(rows: list) -> tuple:
    """(id, lat, lon) of the platform these departures leave from: the id most
    of them carry, not counting trains that were moved here from elsewhere."""
    ids = Counter(dep["platform_id"] for dep in rows if dep.get("platform_id") and not dep.get("platform_moved"))
    ids = ids or Counter(dep["platform_id"] for dep in rows if dep.get("platform_id"))
    if not ids:
        return None, None, None
    most = max(ids.values())
    platform_id = min(pid for pid, n in ids.items() if n == most)
    spot = next((position(dep) for dep in rows if dep.get("platform_id") == platform_id and position(dep)), None)
    return (platform_id, *(spot or (None, None)))


def towards(rows: list) -> list:
    """Where the departures of a platform go next: the one or two most common
    next stops, each for at least a quarter of the departures."""
    counts = Counter(dep["next_stop"] for dep in rows if dep.get("next_stop"))
    total = sum(counts.values())
    return [name for name, n in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:2] if n * 4 >= total]


def group_departures(departures: list) -> list:
    """[{id, name, flat, platforms: [{code, name, area, id, lat, lon, towards,
    departures: [...]}]}].

    `name` is display text; `id` / `code` are the stable values behind it,
    for clients that build their own labels. Lists rather than dicts so the
    order survives JSON serialisation."""
    # mode -> (platform label, sub-stop) -> departures. The sub-stop is part of
    # the key because one station can have two different "Platform 1"s, e.g.
    # a tram "Gleis 1" and a bus "Pos. 1" in different parts of the station.
    grouped = {}
    for dep in departures:
        key = (dep["platform"], dep["platform_area"])
        grouped.setdefault(dep["mode"], {}).setdefault(key, []).append(dep)

    modes = []
    for mode in sorted(grouped, key=mode_sort_key):
        found = []  # (label, sub-stop, id, lat, lon, towards, rows), one per physical platform
        for (label, area), rows in grouped[mode].items():
            for part in split_platforms(rows):
                found.append((label, area, *platform_place(part), towards(part), part))
        areas_of = {}
        twins = Counter()
        for label, area, *_ in found:
            areas_of.setdefault(label, set()).add(area)
            twins[(label, area)] += 1

        platforms = []
        for label, area, platform_id, lat, lon, heading, rows in sorted(
                found, key=lambda f: (platform_sort_key(f[0]), f[1], f[2] or "")):
            # only spell out what is needed to tell this platform from the others
            show_area = bool(area) and len(areas_of[label]) > 1
            name = f"{label} · {area}" if show_area else label
            if twins[(label, area)] > 1 and heading:
                # "or": stop names have commas of their own ("Sinsheim, Egerlandstraße")
                name += " · towards " + " or ".join(heading)
            # by the time itself: two departures in the same minute keep their seconds' order
            rows = sorted(rows, key=lambda d: (d["time"] is None, d["time"] or ""))
            code = rows[0]["platform_code"]
            for row in rows:
                for field in PLATFORM_FIELDS:
                    row.pop(field, None)
            platforms.append({
                "code": code,
                "name": name,
                "area": area if show_area else None,
                "id": platform_id,
                "lat": lat,
                "lon": lon,
                "towards": heading,
                "departures": rows,
            })

        # fewer than 2 distinct platforms within this mode -> flat list
        modes.append({"id": parse.slug(mode), "name": mode, "flat": len(platforms) < 2, "platforms": platforms})
    return modes


def has_departures(stop_id: str, classes, start: datetime | None = None) -> bool:
    data = client.get_departures(stop_id, classes, limit=1, with_stops=False, ttl=client.TAB_CHECK_TTL, start=start)
    return bool(data.get("stopEvents"))


def available_tabs(stop_id: str, classes, start: datetime | None = None) -> list:
    """The tabs of a station, without the ones that have no departures.

    EFA's list of transport types per station isn't reliable -- it lists
    trains at Stuttgart's Arnulf-Klett-Platz, where none stop -- so each
    candidate tab is checked with one tiny request (cached for an hour)."""
    tabs = parse.tabs_for(classes)
    if not tabs:
        return []
    with ThreadPoolExecutor(max_workers=len(tabs)) as pool:
        served = list(pool.map(lambda tab: has_departures(stop_id, tab["classes"], start), tabs))
    return [tab for tab, ok in zip(tabs, served) if ok]


def load_tab(station: dict, tab_id: str, start: datetime | None = None):
    """(tabs with departures, selected tab, its departures), from `start`
    on (None: now).

    The wanted tab's departures and the checks of the other tabs are
    fetched at the same time, so opening a station costs one round of
    requests instead of two."""
    candidates = parse.tabs_for(station["classes"])
    if not candidates:
        # EFA didn't say what stops here; ask for everything instead
        return [], None, parse_events(client.get_departures(station["id"], start=start).get("stopEvents"))

    wanted = next((t for t in candidates if t["id"] == tab_id), candidates[0])
    others = [t for t in candidates if t is not wanted]
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        fetch = pool.submit(client.get_departures, station["id"], wanted["classes"], start=start)
        checks = [pool.submit(has_departures, station["id"], t["classes"], start) for t in others]
        departures = parse_events(fetch.result().get("stopEvents"))
        served = {t["id"] for t, check in zip(others, checks) if check.result()}
    if departures:
        served.add(wanted["id"])

    tabs = [t for t in candidates if t["id"] in served]
    if not departures and tabs:
        # the wanted tab turned out to be empty: show the first one that isn't
        wanted = tabs[0]
        departures = parse_events(client.get_departures(station["id"], wanted["classes"], start=start).get("stopEvents"))
    return tabs, (wanted if departures else None), departures


def later_departures(stop_id: str, now: datetime | None = None) -> list:
    """The first departures after EFA's 24-hour window, looking about a week
    ahead; [] if there are none."""
    # on the hour, so that the requests of one hour can share cached answers
    now = (now or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    starts = [now + LOOK_AHEAD_STEP * step for step in range(1, LOOK_AHEAD_STEPS + 1)]

    def served(start):
        data = client.get_departures(stop_id, (), limit=1, with_stops=False, ttl=client.TAB_CHECK_TTL, start=start)
        return bool(data.get("stopEvents"))

    with ThreadPoolExecutor(max_workers=len(starts)) as pool:
        hits = list(pool.map(served, starts))
    for start, hit in zip(starts, hits):
        if hit:
            departures = parse_events(client.get_departures(stop_id, (), start=start).get("stopEvents"))
            if departures:  # not only flights or on-demand services
                return departures
    return []


def nearby_with_service(nearby: list, start: datetime | None = None) -> list:
    """Of the linked stations, those that have departures (from `start` on,
    None: now), each with the transport types that really stop there:
    [{id, name, types: [...]}]."""
    if not nearby:
        return []
    with ThreadPoolExecutor(max_workers=len(nearby)) as pool:
        tabs = list(pool.map(lambda n: available_tabs(n["id"], n["classes"], start), nearby))
    return [
        {**public_stop(n), "types": [{"id": t["id"], "name": t["name"]} for t in station_tabs]}
        for n, station_tabs in zip(nearby, tabs)
        if station_tabs
    ]


def public_stop(stop: dict) -> dict:
    return {"id": stop["id"], "name": stop["name"], "lat": stop["lat"], "lon": stop["lon"]}


def known_platform(platform_id: str) -> dict:
    """What the stop database says about a platform ({} if nothing): for a
    platform that has no departures to take it from."""
    try:
        return store.platform(platform_id) or {}
    except (store.StopsNotImported, sqlite3.Error):
        return {}


def platform_board(station: dict, nearby: list, platform_id: str, trains_together: bool = False,
                   start: datetime | None = None) -> dict:
    """The board of ONE platform of a station: the same shape as `board`,
    with `platform` saying which. EFA answers for a platform id with that
    platform's departures alone, so a quiet bay of a big bus station gets its
    own next departures, not its share of the station's next hundred."""
    # a platform's id is its station's id plus the part of the station and a
    # number; some simple stops are their own and only platform
    if platform_id != station["id"] and not platform_id.startswith(station["id"] + ":"):
        raise UnknownPlatform(platform_id)
    events = [event for event in client.get_departures(platform_id, start=start).get("stopEvents") or []
              if isinstance(event, dict) and (event.get("location") or {}).get("id") == platform_id]
    tab = next((parse.tab_of(event.get("transportation") or {}) for event in events if not parse.is_excluded(event)), None)
    departures = departing(parse_events(events), station)
    if trains_together:
        put_trains_together(departures)
    heading = towards(departures)
    modes = group_departures(departures)
    shown = modes[0]["platforms"][0] if modes else known_platform(platform_id)
    code = shown.get("code")
    return {
        "stop": public_stop(station),
        "nearby": [public_stop(n) for n in nearby],
        "tabs": [],
        "tab": tab if modes else None,
        "modes": modes,
        "empty": None if modes else (EMPTY_NO_TIMETABLE if outside_timetable(start) else EMPTY_PLATFORM),
        "next_service": None,
        "platform": {
            "id": platform_id,
            "code": code,
            "name": f"Platform {code}" if code else parse.UNKNOWN_PLATFORM,
            "lat": shown.get("lat"),
            "lon": shown.get("lon"),
            "towards": heading if modes else shown.get("towards", []),
        },
        "when": when_text(start),
    }


def board(stop_id: str, tab_id: str = "", platform_id: str = "", trains_together: bool = False,
          start: datetime | None = None) -> dict:
    """The departure board for one station and tab:
    {stop: {id, name, lat, lon}, nearby: [{id, name, lat, lon}],
     tabs: [{id, name}], tab, modes: [...],
     empty: None | "nearby" | "no_service" | "platform" | "no_timetable",
     next_service: None | "YYYY-MM-DD", platform: None | {...}, when: None | "...Z"}.

    The departures are those from `start` on (a UTC moment; None: now).
    `next_service` is set when nothing departs in the 24 hours from then and
    the departures shown are the next ones after that; it is their (local) date.
    With `platform_id` the board holds that platform's departures only, see
    `platform_board`. With `trains_together` regional and long-distance
    trains are one mode, "Trains"."""
    # what can't be a station (or one of its platforms) is answered at once,
    # without sending it to the timetable service
    if not parse.is_station_id(stop_id):
        raise UnknownStop(stop_id)
    if platform_id and not parse.is_platform_id_of(platform_id, stop_id):
        raise UnknownPlatform(platform_id)
    station = parse.parse_station(client.get_station(stop_id))
    # EFA resolves an id it doesn't know, or any text, to whatever stop
    # matches loosely, anywhere in Europe ("Stuttgart Hbf", even ".."): only
    # the stop that was asked for counts, and only in Baden-Württemberg
    if not station or station["id"] != stop_id or not parse.in_baden_wuerttemberg(stop_id):
        # A stop the stop database knows: either EFA is having trouble (some
        # nights it answers every stop with nothing for a while; on 16 October
        # it answered with other stops), then the page says the service isn't
        # answering and tries again; or EFA really doesn't know this one stop
        # (a few in NVBW's list, e.g. de:08117:10121), which a well-known
        # station tells apart: EFA describes that one normally.
        if stop_id in stop_importance() and not client.answering():
            log.warning("EFA did not describe the known stop %s (%s)", stop_id, station and station["id"])
            raise client.EfaError(f"no description of the known stop {stop_id}")
        raise UnknownStop(stop_id)
    nearby = [n for n in station["nearby"] if parse.in_baden_wuerttemberg(n["id"])]
    if platform_id:
        return platform_board(station, nearby, platform_id, trains_together, start)

    tabs, tab, departures = load_tab(station, tab_id, start)
    departures = departing(departures, station)

    next_service = None
    if not departures and outside_timetable(start):
        # before or after the timetable the timetable service has: nothing
        # to look for, here or next door
        return {"stop": public_stop(station), "nearby": [public_stop(n) for n in nearby], "tabs": [], "tab": None,
                "modes": [], "empty": EMPTY_NO_TIMETABLE, "next_service": None, "platform": None, "when": when_text(start)}
    if not departures:
        departures = departing(later_departures(station["id"], start), station)
        times = [dep["time"] for dep in departures if dep["time"]]
        if times:
            next_service = to_local(parse.parse_time(min(times))).date().isoformat()

    empty = None
    if not departures:
        # say why: a station can be empty while the one next door has all the
        # service (e.g. rail replacement buses leaving from the bus station)
        nearby = nearby_with_service(nearby, start)
        empty = EMPTY_NEARBY if nearby else EMPTY_NO_SERVICE
    elif next_service:
        # nothing today, but later: until then the station next door may be
        # where everything leaves from, and a visitor should be told
        nearby = nearby_with_service(nearby, start)
    else:
        nearby = [public_stop(n) for n in nearby]
    if trains_together:
        put_trains_together(departures)

    return {
        "stop": public_stop(station),
        "nearby": nearby,
        "tabs": [{"id": t["id"], "name": t["name"]} for t in tabs],
        "tab": tab["id"] if tab else None,
        "modes": group_departures(departures),
        "empty": empty,
        "next_service": next_service,
        "platform": None,
        "when": when_text(start),
    }
