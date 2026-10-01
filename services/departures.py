"""Departure board: one station's upcoming departures for one transport type
(tab), grouped by mode, then by platform within each mode."""
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from efa import client, parse

log = logging.getLogger(__name__)

# Display order for mode groups; anything else is appended alphabetically.
MODE_ORDER = [
    "U-Bahn / Tram",
    "S-Bahn",
    "Regional train",
    "Long-distance train",
    "Bus",
    "Long-distance bus",
    "Ferry",
    "Cable car",
]

# why a board has no departures
EMPTY_NEARBY = "nearby"          # nothing here, but linked stations have service
EMPTY_NO_SERVICE = "no_service"  # the timetable has nothing for this station


class UnknownStop(Exception):
    """EFA has no stop with this id."""


def mode_sort_key(name: str):
    return (MODE_ORDER.index(name), name) if name in MODE_ORDER else (len(MODE_ORDER), name)


def platform_sort_key(name: str):
    """Numbered platforms in numeric order, then lettered ones, unknown last."""
    if name == parse.UNKNOWN_PLATFORM:
        return (2, 0, name)
    number = re.search(r"\d+", name)
    return (0, int(number.group()), name) if number else (1, 0, name)


def parse_events(events) -> list:
    """Stop events as departure dicts. Flights and on-demand services are
    left out, and so is any event that can't be read: one malformed
    departure from EFA must not take the whole board down."""
    departures = []
    for event in events if isinstance(events, list) else []:
        try:
            if parse.is_excluded(event):
                continue
            departure = parse.parse_departure(event)
            # without a departure time there is nothing to show
            if departure["time"]:
                departures.append(departure)
        except Exception:  # noqa: BLE001 -- whatever EFA sent, skip just this row
            log.warning("Skipping a departure that could not be read", exc_info=True)
    return departures


def dedupe(departures: list) -> list:
    """EFA sometimes sends one trip twice: the timetable entry plus a copy
    created from live data. Keep one, preferring the copy with live data."""
    kept = {}
    for dep in departures:
        key = (dep["mode"], dep["platform"], dep["platform_area"], dep["line"], dep["destination"], dep["planned"])
        if key not in kept or (dep["realtime"] and not kept[key]["realtime"]):
            kept[key] = dep
    return list(kept.values())


def group_departures(departures: list) -> list:
    """[{id, name, flat, platforms: [{code, name, area, departures: [...]}]}].

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
        by_platform = grouped[mode]
        labels = [label for label, _ in by_platform]

        platforms = []
        for label, area in sorted(by_platform, key=lambda k: (platform_sort_key(k[0]), k[1])):
            # only spell out the sub-stop when the label alone is ambiguous
            ambiguous = labels.count(label) > 1 and area
            rows = sorted(by_platform[(label, area)], key=lambda d: (d["minutes"] is None, d["minutes"]))
            code = rows[0]["platform_code"]
            for row in rows:
                del row["mode"], row["platform"], row["platform_code"], row["platform_area"]
            platforms.append({
                "code": code,
                "name": f"{label} · {area}" if ambiguous else label,
                "area": area if ambiguous else None,
                "departures": rows,
            })

        # fewer than 2 distinct platforms within this mode -> flat list
        modes.append({"id": parse.slug(mode), "name": mode, "flat": len(platforms) < 2, "platforms": platforms})
    return modes


def has_departures(stop_id: str, classes) -> bool:
    data = client.get_departures(stop_id, classes, limit=1, with_stops=False, ttl=client.TAB_CHECK_TTL)
    return bool(data.get("stopEvents"))


def available_tabs(stop_id: str, classes) -> list:
    """The tabs of a station, without the ones that have no departures.

    EFA's list of transport types per station isn't reliable -- it lists
    trains at Stuttgart's Arnulf-Klett-Platz, where none stop -- so each
    candidate tab is checked with one tiny request (cached for an hour)."""
    tabs = parse.tabs_for(classes)
    if not tabs:
        return []
    with ThreadPoolExecutor(max_workers=len(tabs)) as pool:
        served = list(pool.map(lambda tab: has_departures(stop_id, tab["classes"]), tabs))
    return [tab for tab, ok in zip(tabs, served) if ok]


def load_tab(station: dict, tab_id: str):
    """(tabs with departures, selected tab, its departures).

    The wanted tab's departures and the checks of the other tabs are
    fetched at the same time, so opening a station costs one round of
    requests instead of two."""
    candidates = parse.tabs_for(station["classes"])
    if not candidates:
        # EFA didn't say what stops here; ask for everything instead
        return [], None, parse_events(client.get_departures(station["id"]).get("stopEvents"))

    wanted = next((t for t in candidates if t["id"] == tab_id), candidates[0])
    others = [t for t in candidates if t is not wanted]
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        fetch = pool.submit(client.get_departures, station["id"], wanted["classes"])
        checks = [pool.submit(has_departures, station["id"], t["classes"]) for t in others]
        departures = parse_events(fetch.result().get("stopEvents"))
        served = {t["id"] for t, check in zip(others, checks) if check.result()}
    if departures:
        served.add(wanted["id"])

    tabs = [t for t in candidates if t["id"] in served]
    if not departures and tabs:
        # the wanted tab turned out to be empty: show the first one that isn't
        wanted = tabs[0]
        departures = parse_events(client.get_departures(station["id"], wanted["classes"]).get("stopEvents"))
    return tabs, (wanted if departures else None), departures


def nearby_with_service(nearby: list) -> list:
    """Of the linked stations, those that have departures, each with the
    transport types that really stop there: [{id, name, types: [...]}]."""
    if not nearby:
        return []
    with ThreadPoolExecutor(max_workers=len(nearby)) as pool:
        tabs = list(pool.map(lambda n: available_tabs(n["id"], n["classes"]), nearby))
    return [
        {**public_stop(n), "types": [{"id": t["id"], "name": t["name"]} for t in station_tabs]}
        for n, station_tabs in zip(nearby, tabs)
        if station_tabs
    ]


def public_stop(stop: dict) -> dict:
    return {"id": stop["id"], "name": stop["name"], "lat": stop["lat"], "lon": stop["lon"]}


def board(stop_id: str, tab_id: str = "") -> dict:
    """The departure board for one station and tab:
    {stop: {id, name, lat, lon}, nearby: [{id, name, lat, lon}],
     tabs: [{id, name}], tab, modes: [...],
     empty: None | "nearby" | "no_service"}."""
    station = parse.parse_station(client.get_station(stop_id))
    # EFA resolves an id it doesn't know to whatever stop matches loosely,
    # anywhere in Europe; only accept stations in Baden-Württemberg
    if not station or not parse.in_baden_wuerttemberg(station["id"] or ""):
        raise UnknownStop(stop_id)
    nearby = [n for n in station["nearby"] if parse.in_baden_wuerttemberg(n["id"])]

    tabs, tab, departures = load_tab(station, tab_id)
    departures = dedupe(departures)

    empty = None
    if not departures:
        # say why: a station can be empty while the one next door has all the
        # service (e.g. rail replacement buses leaving from the bus station)
        nearby = nearby_with_service(nearby)
        empty = EMPTY_NEARBY if nearby else EMPTY_NO_SERVICE
    else:
        nearby = [public_stop(n) for n in nearby]

    return {
        "stop": public_stop(station),
        "nearby": nearby,
        "tabs": [{"id": t["id"], "name": t["name"]} for t in tabs],
        "tab": tab["id"] if tab else None,
        "modes": group_departures(departures),
        "empty": empty,
    }
