"""Stop search: find stops in Baden-Württemberg by name."""
import logging
from collections import Counter

from efa import client, parse
from gtfs import store

log = logging.getLogger(__name__)

MAX_RESULTS = 15

AREA_DEFAULT_LIMIT = 100
AREA_MAX_LIMIT = 500


class InvalidArea(ValueError):
    """The requested rectangle or limit isn't usable."""


def data_info() -> dict | None:
    """Where the stop database comes from and how fresh it is, or None if
    it hasn't been built: {attribution, source, version, valid_until,
    imported_at, stations, expired}."""
    try:
        return store.info()
    except store.StopsNotImported:
        return None


def in_area(bbox: str, limit: str = "", spread: str = "") -> dict:
    """Stations inside a rectangle, most important first:
    {stops: [{id, name, lat, lon, lines, kinds}], total, limit}.

    `bbox` is "west,south,east,north" in degrees. Asking for the stations
    of whatever a map currently shows gives the main stations when zoomed
    out and every stop when zoomed in. `spread` ("1"/"true") picks them
    evenly across the rectangle instead of strictly by importance."""
    try:
        west, south, east, north = (float(part) for part in bbox.split(","))
    except ValueError:
        raise InvalidArea("bbox must be four numbers: west,south,east,north") from None
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise InvalidArea("bbox must be west,south,east,north with west < east and south < north")
    try:
        count = int(limit) if limit else AREA_DEFAULT_LIMIT
    except ValueError:
        raise InvalidArea("limit must be a whole number") from None
    count = max(1, min(count, AREA_MAX_LIMIT))

    stops, total = store.in_area(west, south, east, north, count, spread.lower() in ("1", "true", "yes"))
    return {"stops": stops, "total": total, "limit": count}


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
