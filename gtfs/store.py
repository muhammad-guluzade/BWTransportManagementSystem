"""Read the stop database built by gtfs/importer.py (`python import_stops.py`)."""
import math
import sqlite3
from datetime import date, datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "stops.sqlite"

KIND_COLUMNS = (("rail", "rail_lines"), ("urban_rail", "urban_lines"), ("bus", "bus_lines"), ("other", "other_lines"))
# the same, with urban rail split up: for map colours
MODE_COLUMNS = (("rail", "rail_lines"), ("sbahn", "sbahn_lines"), ("ubahn", "ubahn_lines"), ("tram", "tram_lines"),
                ("bus", "bus_lines"), ("other", "other_lines"))

SOURCE_PAGE = "https://www.nvbw.de/open-data"

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


def in_area(west: float, south: float, east: float, north: float, limit: int, spread: bool = False) -> tuple:
    """(stops, total): up to `limit` stations inside the rectangle, most
    important first, and how many there are in all.

    Normally these are simply the most important ones. With `spread`, and
    more stations than fit, the rectangle is divided into a grid and the
    most important station of each cell is taken: on a map that covers every
    region, where the plain top list would crowd into the big cities."""
    where = "lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
    box = (south, north, west, east)
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
    return to_stops(rows), total


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
