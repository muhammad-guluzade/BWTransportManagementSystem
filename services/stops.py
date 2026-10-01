"""Stop search: find stops in Baden-Württemberg by name."""
import logging
from collections import Counter

from efa import client, parse

log = logging.getLogger(__name__)

MAX_RESULTS = 15


def parse_points(points: list) -> list:
    """Search matches as stop dicts, skipping non-stops and any match that
    can't be read (one malformed entry must not break the whole search)."""
    stops = []
    for point in points:
        try:
            stop = parse.parse_stop(point)
        except Exception:  # noqa: BLE001 -- whatever EFA sent, skip just this match
            log.warning("Skipping a search match that could not be read", exc_info=True)
            continue
        if stop:
            stops.append(stop)
    return stops


def search(query: str) -> list:
    """Stops matching `query`, best match first, as
    [{id, name, lat, lon, types: [{id, name}]}].

    `types` is what EFA's search says stops there; it can include a type
    that has no departures (the station page checks that, the search can't
    afford to)."""
    query = query.strip()
    if len(query) < 2:
        return []

    points = parse.extract_points(client.find_stops(query))
    stops = [s for s in parse_points(points) if parse.in_baden_wuerttemberg(s["id"])]

    # quality is EFA's own match-confidence score (0-1000): a major
    # interchange scores far higher than a street-level bus stop that merely
    # shares part of the search text. More modes served = more likely the
    # main hub the user meant, used as a tiebreaker.
    stops.sort(key=lambda s: (-s["quality"], -len(s["classes"])))

    seen = set()
    unique = []
    for s in stops:
        if s["id"] not in seen:
            seen.add(s["id"])
            unique.append(s)
    unique = unique[:MAX_RESULTS]

    # a train station and the bus stop next to it can carry the same name
    # ("Kehl, Bahnhof" twice); say what stops at each to tell them apart
    name_counts = Counter(s["name"] for s in unique)
    results = []
    for s in unique:
        name = s["name"]
        types = [{"id": t["id"], "name": t["name"]} for t in parse.tabs_for(s["classes"])]
        if name_counts[name] > 1 and types:
            name = f"{name} · {', '.join(t['name'] for t in types)}"
        results.append({"id": s["id"], "name": name, "lat": s["lat"], "lon": s["lon"], "types": types})
    return results
