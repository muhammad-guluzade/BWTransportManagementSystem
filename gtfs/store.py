"""Read the stop database built by gtfs/importer.py (`python import_stops.py`)."""
import sqlite3
from datetime import date, datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "stops.sqlite"

KIND_COLUMNS = (("rail", "rail_lines"), ("urban_rail", "urban_lines"), ("bus", "bus_lines"), ("other", "other_lines"))

SOURCE_PAGE = "https://www.nvbw.de/open-data"


class StopsNotImported(Exception):
    """The stop database hasn't been built yet."""


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
    }


def in_area(west: float, south: float, east: float, north: float, limit: int) -> tuple:
    """(stops, total): the `limit` most important stations inside the
    rectangle, most important first, and how many there are in all."""
    where = "lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
    box = (south, north, west, east)
    total = query(f"SELECT COUNT(*) FROM stations WHERE {where}", box)[0][0]
    rows = query(f"SELECT * FROM stations WHERE {where} ORDER BY importance DESC, id LIMIT ?", (*box, limit))
    return [to_stop(row) for row in rows], total


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
