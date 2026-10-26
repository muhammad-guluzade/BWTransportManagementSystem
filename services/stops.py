"""Stop search: find stops in Baden-Württemberg by name."""
import logging
import re
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError

from efa import client, parse
from gtfs import store
from services import stop_index

log = logging.getLogger(__name__)

SEARCH_DEFAULT_LIMIT = 10
SEARCH_MAX_LIMIT = 25
# how far a map "shows" when the caller only says where it is
SEARCH_DEFAULT_RADIUS_KM = 10.0
# how long a search waits for the timetable service when it has matches of its own
EFA_PATIENCE_SECONDS = 1.2
# the timetable service is asked while the stop database is searched
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="stop-search")

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


def in_area(bbox: str, limit: str = "", spread: str = "", platforms: str = "") -> dict:
    """Stations inside a rectangle, most important first:
    {stops: [{id, name, lat, lon, lines, kinds}], total, limit}.

    `bbox` is "west,south,east,north" in degrees. Asking for the stations
    of whatever a map currently shows gives the main stations when zoomed
    out and every stop when zoomed in. `spread` ("1"/"true") picks them
    evenly across the rectangle instead of strictly by importance.
    `platforms` ("1"/"true") adds each station's platforms."""
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

    yes = ("1", "true", "yes")
    stops, total = store.in_area(west, south, east, north, count, spread.lower() in yes, platforms.lower() in yes)
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


def efa_matches(query: str) -> list:
    """What the timetable service's own search finds in Baden-Württemberg, as
    [{id, name, lat, lon, quality, classes}], its best match first."""
    points = parse.extract_points(client.find_stops(query))
    stops = [s for s in parse_points(points) if parse.in_baden_wuerttemberg(s["id"])]
    # quality is EFA's own match-confidence score (0-1000)
    return sorted(stops, key=lambda s: (-s["quality"], -len(s["classes"])))


# ---------- the navigator's search: places too, ranked with the stops ----------
PLACES_LIMIT = 6
# A place is ranked like a stop of this importance (Mannheim Hbf is about 48):
# a street or sight that fits the words well comes before a stop that fits
# them less well, and the nearer one first
PLACE_IMPORTANCE = 2.0
# a house number typed ("Eichstraße 9"): the best streets of that name are asked for it
HOUSE_NUMBER = re.compile(r"(?<![\w-])(\d{1,4}[a-zA-Z]?)(?![\w-])")
STREETS_FOR_A_NUMBER = 2
# ... and with no map to go by (none given, or one of the whole state), more of them
STREETS_FOR_A_NUMBER_ANYWHERE = 5
WIDE_MAP_KM = 30
# the navigator waits a little longer for the timetable service, which knows the places
PLACES_PATIENCE_SECONDS = 3.0


def place_text(quality: float) -> float:
    """How well a place fits the words, on the stops' scale (0-1), from the
    timetable service's own match score (0-1000)."""
    return 0.95 * max(0.0, min(quality, 1000.0)) / 1000


def ranked_places(points: list, place: tuple | None, radius_km: float) -> list:
    """The sights, addresses and streets among the timetable service's
    matches, in Baden-Württemberg, each with a `score` (best first; one per
    name)."""
    from services import journeys   # (it imports this module's neighbours)
    best = {}
    for point in points:
        try:
            found = parse.parse_place(point)
        except Exception:  # noqa: BLE001 -- skip just this match
            continue
        if not found:
            continue
        # outside the state by its town's key: parse_place left it out; no key: a stop of the state must be near
        if parse.municipality_in_bw((point.get("ref") or {}).get("omc")) is None and not journeys.in_baden_wuerttemberg(found["lat"], found["lon"]):
            continue
        try:
            quality = float(point.get("quality") or 0)
        except (TypeError, ValueError):
            quality = 0.0
        found["score"] = stop_index.final_score(place_text(quality), PLACE_IMPORTANCE, found["lat"], found["lon"], place, radius_km)
        if found["name"] not in best or best[found["name"]]["score"] < found["score"]:
            best[found["name"]] = found
    return sorted(best.values(), key=lambda p: (-p["score"], p["name"]))


def house_numbers(query: str, places: list, how_many: int = STREETS_FOR_A_NUMBER) -> list:
    """A house number typed without a town ("Eichstraße 9"): the timetable
    service finds the house only with the town, so the best streets of that
    name are asked for it ("Eichstraße 9 Stuttgart"). Each house found is
    ranked just above its street."""
    number = HOUSE_NUMBER.search(query)
    if not number:
        return []
    streets = [p for p in places if p["kind"] == "street" and ", " in p["name"]][:how_many]

    def look(street):
        town, road = street["name"].split(", ", 1)
        wanted = stop_index.fold(f"{road} {number.group(1)}")
        try:
            points = parse.extract_points(client.find_stops(f"{road} {number.group(1)} {town}"))
        except client.EfaError:
            return None
        for point in points:
            try:
                found = parse.parse_place(point)
            except Exception:  # noqa: BLE001
                continue
            if found and found["kind"] == "address" and stop_index.fold(found["name"]).endswith(wanted):
                found["score"] = round(street["score"] * 1.03, 4)
                return found
        return None

    return [house for house in _pool.map(look, streets) if house]


# an address exactly as typed (street and house number) comes first: whoever types
# "Aldinger Straße 212" means that house, not a stop on some Aldinger Straße
EXACT_HOUSE_BONUS = 1.3


def same_spelling(text: str) -> str:
    """Folded, without spaces, "Straße" / "Str." alike: "Aldinger Str. 212" = "Aldingerstraße 212"."""
    return stop_index.fold(text).replace(" ", "").replace("strasse", "str")


def exact_houses_first(query: str, places: list) -> list:
    if not HOUSE_NUMBER.search(query):
        return places
    typed = same_spelling(query)
    for p in places:
        street = p["name"].split(", ", 1)[-1]
        if p["kind"] == "address" and HOUSE_NUMBER.search(street) and same_spelling(street) in typed:
            p["score"] = round(p["score"] * EXACT_HOUSE_BONUS, 4)
    return places


def route_search(query: str, near: str = "", radius: str = "", limit: str = "") -> dict:
    """The navigator's search (`places=1`): {stops, places, results}.
    `stops` as search() gives them; `places` the sights, addresses and
    streets, best first; `results` both together in one order, by how well
    the words fit and (with `near`) how close each one is: what a map app
    shows."""
    finished = query[-1:].isspace()
    query = " ".join(query.split())
    place, radius_km = search_place(near, radius)
    count = search_count(limit)
    if len(query) < 2:
        return {"stops": [], "places": [], "results": []}
    ranked = ranked_stops(query, place, radius_km, count, finished, PLACES_PATIENCE_SECONDS)
    try:
        points = parse.extract_points(client.find_stops(query))   # (kept by the stop search a moment ago)
    except client.EfaError:
        points = []
    places = ranked_places(points, place, radius_km)
    how_many = STREETS_FOR_A_NUMBER if place and radius_km < WIDE_MAP_KM else STREETS_FOR_A_NUMBER_ANYWHERE
    places = sorted(exact_houses_first(query, house_numbers(query, places, how_many) + places), key=lambda p: (-p["score"], p["name"]))[:PLACES_LIMIT]
    stops = public_stops(ranked)
    mixed = [(c["score"], {"type": "stop", **s}) for c, s in zip(ranked, stops)]
    mixed += [(p["score"], {"type": "place", **{k: v for k, v in p.items() if k != "score"}}) for p in places]
    mixed.sort(key=lambda m: -m[0])
    if mixed:
        mixed = [m for m in mixed if m[0] >= stop_index.WORTH_SHOWING * mixed[0][0]]
    return {"stops": stops, "places": [{k: v for k, v in p.items() if k != "score"} for p in places],
            "results": [m[1] for m in mixed[:count]]}


def own_matches(query: str, place: tuple | None, radius_km: float, limit: int) -> list | None:
    """What the stop database finds, or None if it hasn't been built."""
    try:
        return stop_index.current().search(query, place, radius_km, limit)
    except (store.StopsNotImported, sqlite3.Error):
        return None


def known_importance(stop_id: str) -> float | None:
    """The stop database's importance of a stop, or None if it has none."""
    try:
        index = stop_index.current()
    except (store.StopsNotImported, sqlite3.Error):
        return None
    number = index.by_id.get(stop_id)
    return None if number is None else index.stops[number]["importance"]


def types_of(row: dict) -> list:
    """The kinds of transport of a station of the stop database, in the
    words the search has always used: [{id, name}]."""
    has = {"trains": row.get("rail_lines"), "sbahn": row.get("sbahn_lines"),
           "tram": row.get("ubahn_lines") or row.get("tram_lines"), "bus": row.get("bus_lines")}
    types = [{"id": tab_id, "name": name} for tab_id, name, _ in parse.TABS if has.get(tab_id)]
    if row.get("other_lines"):
        types.append({"id": parse.OTHER_TAB[0], "name": parse.OTHER_TAB[1]})
    return types


def search_place(near: str, radius: str) -> tuple:
    """((lat, lon) or None, radius in km) from the request's `near` and `radius`."""
    place = None
    if near.strip():
        try:
            lat, lon = (float(part) for part in near.split(","))
        except ValueError:
            raise InvalidArea("near must be two numbers: lat,lon") from None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise InvalidArea("near must be lat,lon in degrees")
        place = (lat, lon)
    try:
        radius_km = float(radius) if radius.strip() else SEARCH_DEFAULT_RADIUS_KM
    except ValueError:
        raise InvalidArea("radius must be a number of kilometres") from None
    if not radius_km > 0:
        raise InvalidArea("radius must be more than 0")
    return place, radius_km


def search_count(limit: str) -> int:
    try:
        count = int(limit) if limit.strip() else SEARCH_DEFAULT_LIMIT
    except ValueError:
        raise InvalidArea("limit must be a whole number") from None
    return max(1, min(count, SEARCH_MAX_LIMIT))


def search(query: str, near: str = "", radius: str = "", limit: str = "") -> list:
    """Stops matching `query`, best match first, as
    [{id, name, lat, lon, types: [{id, name}], partial}].
    `partial` is true for a stop that fits only some of the words, which is
    all there is when no stop fits every word ("Insel Mainau").

    Two searches run side by side and are ranked together: the one over the
    stop database (services/stop_index.py: forgiving of spelling, knows how
    important a stop is) and the timetable service's own (knows the town of
    every stop, and stops the database lacks). `near` ("lat,lon") is where
    the user's map is and `radius` how many kilometres it shows: stops there
    come first, unless the words clearly mean a stop elsewhere.

    Without the stop database this is the timetable service's search alone;
    without the timetable service, the stop database's."""
    # a space at the end says the last word is finished, not still being typed
    finished = query[-1:].isspace()
    query = " ".join(query.split())
    place, radius_km = search_place(near, radius)
    count = search_count(limit)
    if len(query) < 2:
        return []
    return public_stops(ranked_stops(query, place, radius_km, count, finished))


def ranked_stops(query: str, place: tuple | None, radius_km: float, count: int, finished: bool = False,
                 patience: float | None = None) -> list:
    """The stops for `query`, best first, each with its `score` (see search())."""
    patience = EFA_PATIENCE_SECONDS if patience is None else patience
    asked = _pool.submit(efa_matches, query)
    own = own_matches(query + " " if finished else query, place, radius_km, 3 * count)
    try:
        # With matches of our own there is no reason to make someone who is
        # typing wait for a slow answer; it is remembered for the next key.
        found = asked.result(timeout=patience if own else None)
    except TimeoutError:
        found = []
    except client.EfaError:
        if own is None:
            raise   # nothing to search with at all
        found = []

    candidates = {}
    for row in own or []:
        candidates[row["id"]] = {"id": row["id"], "name": stop_index.display_name(row["name"], row["id"]), "lat": row["lat"],
                                 "lon": row["lon"], "types": types_of(row), "score": row["score"], "partial": row.get("partial", False)}
    for rank, stop in enumerate(found):
        entry = candidates.get(stop["id"])
        types = [{"id": t["id"], "name": t["name"]} for t in parse.tabs_for(stop["classes"])]
        if entry:
            # both agree: the timetable service's name is the one the board will show
            # (and it found it for all the words, even if our own search did not)
            entry.update(name=stop["name"], score=round(entry["score"] * 1.05, 4), partial=False)
            entry["types"] = types or entry["types"]
            continue
        # Found by the timetable service alone (it resolved a typo, or knows
        # the town): a decent match by its own judgement, a little less the
        # further down its list
        text = 0.5 + 0.3 * stop["quality"] / 1000 - 0.01 * min(rank, 10)
        importance = known_importance(stop["id"]) or 1.0
        candidates[stop["id"]] = {"id": stop["id"], "name": stop["name"], "lat": stop["lat"], "lon": stop["lon"], "types": types,
                                  "score": stop_index.final_score(text, importance, stop["lat"], stop["lon"], place, radius_km),
                                  "partial": False}

    ranked = sorted(candidates.values(), key=lambda c: (-c["score"], c["name"], c["id"]))
    if ranked:
        ranked = [c for c in ranked if c["score"] >= stop_index.WORTH_SHOWING * ranked[0]["score"]]
    return ranked[:count]


def public_stops(ranked: list) -> list:
    """The ranked stops as the API gives them, names made unmistakable."""
    # a train station and the bus stop next to it can carry the same name
    # ("Kehl, Bahnhof" twice): say what stops at each; two villages can have
    # the same stop name ("Rathaus"): say which district
    results = []
    names = Counter(c["name"] for c in ranked)
    for c in ranked:
        name = c["name"]
        if names[name] > 1 and c["types"]:
            name = f"{name} · {', '.join(t['name'] for t in c['types'])}"
        results.append({"id": c["id"], "name": name, "lat": c["lat"], "lon": c["lon"], "types": c["types"], "partial": c["partial"]})
    names = Counter(r["name"] for r in results)
    for r in results:
        district = stop_index.DISTRICTS.get(r["id"].split(":")[1] if r["id"].count(":") >= 2 else "")
        if names[r["name"]] > 1 and district:
            r["name"] = f"{r['name']} ({district})"
    return results
