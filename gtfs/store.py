"""Read the stop database built by gtfs/importer.py (`python import_stops.py`)."""
import math
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "stops.sqlite"

KIND_COLUMNS = (("rail", "rail_lines"), ("urban_rail", "urban_lines"), ("bus", "bus_lines"), ("other", "other_lines"))
PLATFORM_KIND_COLUMNS = (("rail", "rail_calls"), ("urban_rail", "urban_calls"), ("bus", "bus_calls"), ("other", "other_calls"))
# the same, with urban rail split up: for map colours
MODE_COLUMNS = (("rail", "rail_lines"), ("sbahn", "sbahn_lines"), ("ubahn", "ubahn_lines"), ("tram", "tram_lines"),
                ("bus", "bus_lines"), ("other", "other_lines"))

SOURCE_PAGE = "https://www.nvbw.de/open-data"
# The layout this code reads: the importer's SCHEMA_VERSION. A database built
# before a change to it is refused, with a hint to run the import again.
SCHEMA = "3"

# In Baden-Württemberg one degree of longitude is about two thirds as long
# as one degree of latitude; grid cells this much flatter come out square.
LAT_PER_LON = 0.66


class StopsNotImported(Exception):
    """The stop database hasn't been built yet, or was built by an older
    version of the importer and lacks something the app needs now."""


def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise StopsNotImported(str(DB_PATH))
    # read-only: the app never changes the database
    db = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def query(sql: str, params: tuple = ()) -> list:
    db = connect()
    try:
        return db.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        # a file that exists but has no tables yet, or an older layout
        if "no such table" in str(e) or "no such column" in str(e):
            raise StopsNotImported(str(DB_PATH)) from None
        raise
    finally:
        db.close()


def file_stamp() -> tuple:
    """Changes whenever the database file does (a new import)."""
    try:
        stat = DB_PATH.stat()
    except OSError:
        raise StopsNotImported(str(DB_PATH)) from None
    return str(DB_PATH), stat.st_mtime_ns, stat.st_size


_current = {"stamp": None}


def require_current() -> None:
    """Raise StopsNotImported unless the database has today's layout.
    Looked up once per import, not on every request."""
    stamp = file_stamp()
    if _current["stamp"] != stamp:
        if meta().get("schema") != SCHEMA:
            raise StopsNotImported(f"{DB_PATH} was built by an older version; run the import again")
        _current["stamp"] = stamp


def to_platform(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "code": row["code"] or None,
        "lat": row["lat"],
        "lon": row["lon"],
        "kinds": [kind for kind, column in PLATFORM_KIND_COLUMNS if row[column]],
        "towards": row["towards"].split("\n") if row["towards"] else [],
    }


def platform(platform_id: str) -> dict | None:
    """One platform by its id, or None."""
    rows = query("SELECT * FROM platforms WHERE id = ?", (platform_id,))
    return to_platform(rows[0]) if rows else None


def platforms_of(station_ids: list) -> dict:
    """{station id: its served platforms, the busiest first}."""
    found = {station_id: [] for station_id in station_ids}
    ids = list(found)
    for start in range(0, len(ids), 500):  # SQLite takes a limited number of values per statement
        part = ids[start:start + 500]
        rows = query(f"SELECT * FROM platforms WHERE station IN ({', '.join('?' * len(part))}) ORDER BY calls DESC, id", tuple(part))
        for row in rows:
            found[row["station"]].append(to_platform(row))
    return found


def to_stop(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "lat": row["lat"],
        "lon": row["lon"],
        "lines": row["lines"],
        "kinds": [kind for kind, column in KIND_COLUMNS if row[column]],
        "modes": [mode for mode, column in MODE_COLUMNS if row[column]],
    }


def to_stops(rows: list) -> list:
    if rows and "tram_lines" not in rows[0].keys():
        raise StopsNotImported(f"{DB_PATH} was built by an older version; run the import again")
    return [to_stop(row) for row in rows]


def cell_sizes(west: float, east: float, south: float, north: float, limit: int):
    """Candidate grid cell widths (degrees of longitude) for spreading
    `limit` stations over the rectangle, from fine to coarse.

    Sizes come from a fixed ladder (powers of two and the steps half-way
    between) and the grid is anchored to the globe, not to the rectangle:
    moving the rectangle a little (panning a map) then keeps the same cells,
    and so the same stations. The first candidate would give about four
    times `limit` cells; each next one halves the number."""
    exact = math.sqrt((east - west) * (north - south) / (limit * LAT_PER_LON))   # exactly `limit` cells
    rung = math.floor(2 * math.log2(exact)) - 2
    while True:
        yield 2.0 ** (rung / 2)
        rung += 1


def in_area(west: float, south: float, east: float, north: float, limit: int, spread: bool = False,
            platforms: bool = False) -> tuple:
    """(stops, total): up to `limit` stations inside the rectangle, most
    important first, and how many there are in all.

    Normally these are simply the most important ones. With `spread`, and
    more stations than fit, the rectangle is divided into a grid and the
    most important station of each cell is taken: on a map that covers every
    region, where the plain top list would crowd into the big cities.

    With `platforms` each stop also lists its platforms, and a station
    counts as inside if any of its platforms is: a bus bay at the edge of a
    map belongs on it even when the middle of its station does not."""
    require_current()
    where = "lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
    box = (south, north, west, east)
    if platforms:
        where = f"(({where}) OR id IN (SELECT station FROM platforms WHERE {where}))"
        box = box * 2
    total = query(f"SELECT COUNT(*) FROM stations WHERE {where}", box)[0][0]
    if spread and total > limit:
        # the finest grid whose occupied cells all fit: as many stations as the
        # limit allows, without having to drop whole regions
        for width in cell_sizes(west, east, south, north, limit):
            occupied = query(
                f"""SELECT COUNT(*) FROM (SELECT DISTINCT CAST(lon / ? AS INTEGER), CAST(lat / ? AS INTEGER)
                                         FROM stations WHERE {where})""",
                (width, width * LAT_PER_LON, *box))[0][0]
            if occupied <= limit:
                break
        rows = query(
            f"""SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY CAST(lon / ? AS INTEGER), CAST(lat / ? AS INTEGER)
                        ORDER BY importance DESC, id) AS place_in_cell
                    FROM stations WHERE {where})
                WHERE place_in_cell = 1 ORDER BY importance DESC, id LIMIT ?""",
            (width, width * LAT_PER_LON, *box, limit))
    else:
        rows = query(f"SELECT * FROM stations WHERE {where} ORDER BY importance DESC, id LIMIT ?", (*box, limit))
    stops = to_stops(rows)
    if platforms:
        own = platforms_of([stop["id"] for stop in stops])
        for stop in stops:
            stop["platforms"] = own[stop["id"]]
    return stops, total


_importance = {"stamp": None, "values": {}}
_importance_lock = threading.Lock()


def importance() -> dict:
    """{station id: importance} for every station, e.g. to tell the major
    stops of a trip from the minor ones.

    Kept in memory (a board asks for hundreds of stops at a time) and read
    again when the database file has changed, i.e. after a new import."""
    stamp = file_stamp()
    with _importance_lock:
        if _importance["stamp"] != stamp:
            _importance["values"] = {row["id"]: row["importance"] for row in query("SELECT id, importance FROM stations")}
            _importance["stamp"] = stamp
        return _importance["values"]


def meta() -> dict:
    return dict(query("SELECT key, value FROM meta"))


def info(today: date | None = None) -> dict:
    """Where the stop data comes from and how fresh it is, for showing to
    people: {attribution, source, version, valid_until, imported_at,
    stations, expired}."""
    data = meta()
    valid_until = None
    try:
        valid_until = datetime.strptime(data.get("feed_end_date", ""), "%Y%m%d").date()
    except ValueError:
        pass
    return {
        "attribution": data.get("attribution", ""),
        "source": SOURCE_PAGE,
        "version": data.get("feed_version") or None,
        "valid_until": valid_until.isoformat() if valid_until else None,
        "imported_at": data.get("imported_at") or None,
        "stations": int(data.get("stations") or 0),
        # the timetable period the file was made for is over; the stops are
        # probably still right, but it is time to import a newer file
        "expired": bool(valid_until and (today or date.today()) > valid_until),
    }
