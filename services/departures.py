"""Departure board: one station's upcoming departures for one transport type
(tab), grouped by mode, then by platform within each mode."""
import re
from concurrent.futures import ThreadPoolExecutor

from efa import client, parse

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
    "On-demand",
]


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


def group_departures(departures: list) -> list:
    """[{name, flat, platforms: [{name, departures: [...]}]}], ready to render.

    Lists rather than dicts so the order survives JSON serialisation."""
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
            name = f"{label} · {area}" if labels.count(label) > 1 and area else label
            rows = sorted(by_platform[(label, area)], key=lambda d: (d["minutes"] is None, d["minutes"]))
            for row in rows:
                del row["mode"], row["platform"], row["platform_area"]
            platforms.append({"name": name, "departures": rows})

        # fewer than 2 distinct platforms within this mode -> flat list
        modes.append({"name": mode, "flat": len(platforms) < 2, "platforms": platforms})
    return modes


def has_departures(stop_id: str, classes) -> bool:
    data = client.get_departures(stop_id, classes, limit=1, with_stops=False, ttl=client.TAB_CHECK_TTL)
    return bool(data.get("stopEvents"))


def available_tabs(station: dict) -> list:
    """The station's tabs, without the ones that have no departures.

    EFA's list of transport types per station isn't reliable -- it lists
    S-Bahn at Stuttgart Hbf (oben), where none stops -- so each candidate
    tab is checked with one tiny request (cached for an hour)."""
    tabs = parse.tabs_for(station["classes"])
    if len(tabs) < 2:
        return tabs
    with ThreadPoolExecutor(max_workers=len(tabs)) as pool:
        served = list(pool.map(lambda tab: has_departures(station["id"], tab["classes"]), tabs))
    return [tab for tab, ok in zip(tabs, served) if ok]


def board(stop_id: str, tab_id: str = "") -> dict:
    """The departure board for one station and tab:
    {stop: {id, name}, nearby: [{id, name}], tabs: [{id, name}], tab, modes: [...]}."""
    station = parse.parse_station(client.get_station(stop_id))
    # EFA resolves an id it doesn't know to whatever stop matches loosely,
    # anywhere in Europe; only accept stations in Baden-Württemberg
    if not station or not parse.in_baden_wuerttemberg(station["id"] or ""):
        raise UnknownStop(stop_id)
    station["nearby"] = [n for n in station["nearby"] if parse.in_baden_wuerttemberg(n["id"])]

    tabs = available_tabs(station)
    tab = next((t for t in tabs if t["id"] == tab_id), tabs[0] if tabs else None)

    # no tabs = EFA didn't say what stops here; ask for everything instead
    data = client.get_departures(station["id"], tab["classes"] if tab else ())
    departures = [parse.parse_departure(event) for event in data.get("stopEvents") or []]

    return {
        "stop": {"id": station["id"], "name": station["name"]},
        "nearby": station["nearby"],
        "tabs": [{"id": t["id"], "name": t["name"]} for t in tabs],
        "tab": tab["id"] if tab else None,
        "modes": group_departures(departures),
    }
