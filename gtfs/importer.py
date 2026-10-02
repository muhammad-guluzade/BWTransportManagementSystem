"""
Build the stop database from NVBW's timetable file (GTFS).

The file lists every platform in and around Baden-Württemberg plus the
whole timetable. From it we keep one row per station in BW: name,
position, and how much service it has (used to rank stops on the map, in
search and as via stops).

Data: "Datensatz der NVBW GmbH", https://www.nvbw.de/open-data
Licence: Datenlizenz Deutschland - Namensnennung - Version 2.0
Updated by NVBW on the 1st and 3rd Tuesday of each month.
"""
import csv
import io
import math
import re
import sqlite3
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

# the timetable without route shapes (about 55 MB; with shapes it is 850 MB)
SOURCE_URL = "https://www.nvbw.de/fileadmin/user_upload/service/open_data/fahrplandaten_ohne_liniennetz/bwgesamt.zip"

BW_PREFIX = "de:08"  # global stop ids start with country and state; 08 is BW
# Baden-Württemberg fits inside this rectangle (west, south, east, north).
# The file has a few platforms with a wrong position; anything outside is one.
BW_BOUNDS = (7.5, 47.5, 10.5, 49.8)

# BW has about 29,000 served stations. A result far below that means the
# file is broken, and must not replace a good database.
MIN_STATIONS = 10000

# "Stuttgart Hauptbahnhof (oben) Gleis 1" -> "Stuttgart Hauptbahnhof (oben)"
PLATFORM_SUFFIX = re.compile(r"\s+(Gleis|Bstg\.?|Pos\.?|Steig|Bussteig|Bahnsteig|Mast)\s*\S*$")

# GTFS route_type -> our coarse kind. NVBW codes S-Bahn, U-Bahn and tram
# all as 0, so the file can't tell those apart.
KIND_OF_ROUTE_TYPE = {"2": "rail", "0": "urban_rail", "1": "urban_rail", "3": "bus"}
KINDS = ("rail", "urban_rail", "bus", "other")

# Rail replacement buses are coded as rail (route_type 2) in the file. They
# are named "SEV ...", or have no line name and run between bus stations.
REPLACEMENT_NAME = re.compile(r"^\s*SEV\b", re.IGNORECASE)
BUS_PLACE = re.compile(r"ZOB|Busbahnhof|Rathaus|straße|Platz|Südausgang", re.IGNORECASE)

# Platforms of one station further apart than this are really different
# places; their average would be a point where nothing stops.
WIDE_STATION_METRES = 300

COLUMNS = ("id", "name", "lat", "lon",
           "rail_lines", "urban_lines", "bus_lines", "other_lines", "lines",
           "rail_calls", "urban_calls", "bus_calls", "other_calls", "calls",
           "importance")
CREATE_TABLES = (
    """CREATE TABLE stations (
        id          TEXT PRIMARY KEY,   -- global stop id, e.g. de:08111:115 (same as EFA)
        name        TEXT NOT NULL,
        lat         REAL NOT NULL,
        lon         REAL NOT NULL,
        rail_lines  INTEGER NOT NULL,   -- distinct lines per kind that call here
        urban_lines INTEGER NOT NULL,
        bus_lines   INTEGER NOT NULL,
        other_lines INTEGER NOT NULL,
        lines       INTEGER NOT NULL,   -- all of the above together
        rail_calls  INTEGER NOT NULL,   -- departures per kind in the whole timetable period
        urban_calls INTEGER NOT NULL,
        bus_calls   INTEGER NOT NULL,
        other_calls INTEGER NOT NULL,
        calls       INTEGER NOT NULL,   -- all of the above together
        importance  REAL NOT NULL       -- one number to rank stations by, see importance()
    )""",
    "CREATE INDEX stations_lat ON stations (lat)",
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


class BadFeed(Exception):
    """The timetable file is damaged, not the expected file, or its content
    is too thin to be right. The message is meant for the person running
    the import."""


def station_id(stop_id: str) -> str:
    """The station a platform belongs to: 'de:08111:6115:1:2' -> 'de:08111:6115'.
    Some stations also have a row of their own, 'de:08111:6114_Parent'."""
    return ":".join(stop_id.removesuffix("_Parent").split(":")[:3])


def clean_name(name: str) -> str:
    """Trim and collapse the stray spaces some names have ('Heilbronn  Rathaus')."""
    return " ".join(name.split())


def station_name(platform_names: list, parent_name: str | None) -> str:
    """One name for a station from the names of its platforms."""
    if parent_name and clean_name(parent_name):
        return clean_name(parent_name)
    stripped = Counter(PLATFORM_SUFFIX.sub("", clean_name(name)).strip() for name in platform_names)
    # the most common name; among equally common ones, the shortest
    return min(stripped, key=lambda name: (-stripped[name], len(name), name)) if stripped else ""


def is_real_name(name: str) -> bool:
    """A name needs at least one letter; the file has a stop called '.'."""
    return any(ch.isalpha() for ch in name)


def route_kind(route_type: str, short_name: str, long_name: str) -> str:
    """rail / urban_rail / bus / other for one route of the file."""
    kind = KIND_OF_ROUTE_TYPE.get(route_type, "other")
    if kind == "rail":
        if REPLACEMENT_NAME.search(short_name) or (not short_name.strip() and BUS_PLACE.search(long_name)):
            return "bus"  # a rail replacement bus
    return kind


def in_bounds(lat: float, lon: float) -> bool:
    west, south, east, north = BW_BOUNDS
    return south <= lat <= north and west <= lon <= east


def metres(a: tuple, b: tuple) -> float:
    """Distance between two (lat, lon) points; fine for short distances."""
    return 111000 * math.hypot(a[0] - b[0], (a[1] - b[1]) * math.cos(math.radians(a[0])))


def station_position(platforms: list) -> tuple:
    """(lat, lon) for a station from its platforms, given as
    [(lat, lon, calls)]. Normally the middle of them. If they are far apart
    they are really different places, and the busiest one is used."""
    width = max((metres(a[:2], b[:2]) for a in platforms for b in platforms), default=0)
    if width > WIDE_STATION_METRES:
        lat, lon, _ = max(platforms, key=lambda p: p[2])
    else:
        lat = sum(p[0] for p in platforms) / len(platforms)
        lon = sum(p[1] for p in platforms) / len(platforms)
    return round(lat, 6), round(lon, 6)


def importance(lines: dict, calls: dict) -> float:
    """How important a station is, as one number; `lines` and `calls` are
    counts per kind.

    Two things count: how busy it is (departures) and how connected (lines).
    Rail counts most, then urban rail, then bus. Departures go in as a square
    root and lines are capped, so that a bus station with 60 lines doesn't
    outrank a main railway station. The weights were chosen against pairs of
    stations where the right order is known (see tests)."""
    bus_calls = calls["bus"] + calls["other"]
    bus_lines = lines["bus"] + lines["other"]
    busy = (3 * math.sqrt(calls["rail"]) + 1.5 * math.sqrt(calls["urban_rail"]) + 0.6 * math.sqrt(bus_calls)) / 10
    connected = min(lines["rail"], 12) + 0.6 * min(lines["urban_rail"], 12) + 0.15 * min(bus_lines, 20)
    return round(busy + connected, 3)


def read_table(archive: zipfile.ZipFile, name: str):
    return csv.DictReader(io.TextIOWrapper(archive.open(name), encoding="utf-8-sig"))


def download(dest: Path, url: str = SOURCE_URL, progress=None) -> None:
    """Fetch the timetable file to `dest`. `progress(done_bytes, total_bytes)`
    is called now and then if given. Raises requests.RequestException if the
    download fails and BadFeed if what arrived is not the file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            with open(partial, "wb") as out:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    out.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        if total and done != total:
            raise BadFeed(f"the download stopped early ({done / 1e6:.1f} of {total / 1e6:.1f} MB arrived). Please try again.")
        if not zipfile.is_zipfile(partial):
            raise BadFeed("the server did not send the timetable file (what arrived is not a zip file). Please try again later.")
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(dest)


def read_feed(zip_path: Path) -> tuple:
    """(stations as tuples in COLUMNS order, feed info) from a GTFS file."""
    with zipfile.ZipFile(zip_path) as archive:
        # 1. platforms -> stations
        names = defaultdict(list)
        parent_names = {}
        positions = {}  # platform id -> (lat, lon)
        for row in read_table(archive, "stops.txt"):
            stop_id = row["stop_id"]
            if not stop_id.startswith(BW_PREFIX):
                continue
            sid = station_id(stop_id)
            if row.get("location_type") == "1":
                parent_names[sid] = row["stop_name"]
                continue
            names[sid].append(row["stop_name"])
            try:
                lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
            except ValueError:
                continue
            if in_bounds(lat, lon):
                positions[stop_id] = (lat, lon)

        # 2. which lines call at each station, and how often
        routes = {}
        for r in read_table(archive, "routes.txt"):
            kind = route_kind(r["route_type"], r["route_short_name"], r["route_long_name"])
            routes[r["route_id"]] = (r["route_short_name"].strip() or r["route_long_name"].strip(), kind)
        trip_route = {r["trip_id"]: r["route_id"] for r in read_table(archive, "trips.txt")}
        lines = defaultdict(set)             # station -> {(line name, kind)}
        calls = defaultdict(Counter)         # station -> kind -> departures
        platform_calls = Counter()           # platform -> departures
        with archive.open("stop_times.txt") as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
            header = next(reader, [])
            trip_col, stop_col = header.index("trip_id"), header.index("stop_id")
            for row in reader:
                stop_id = row[stop_col]
                if not stop_id.startswith(BW_PREFIX):
                    continue
                route = routes.get(trip_route.get(row[trip_col]))
                if not route:
                    continue
                sid = station_id(stop_id)
                lines[sid].add(route)
                calls[sid][route[1]] += 1
                platform_calls[stop_id] += 1

        feed = {}
        if "feed_info.txt" in archive.namelist():
            feed = next(iter(read_table(archive, "feed_info.txt")), {})

    # 3. one row per station that is served, has a position and a name
    platforms = defaultdict(list)
    for stop_id, (lat, lon) in positions.items():
        platforms[station_id(stop_id)].append((lat, lon, platform_calls[stop_id]))

    stations = []
    for sid, station_platforms in platforms.items():
        if not calls[sid]:
            continue
        name = station_name(names[sid], parent_names.get(sid))
        if not is_real_name(name):
            continue
        per_kind_lines = Counter(kind for _, kind in lines[sid])
        line_counts = {kind: per_kind_lines[kind] for kind in KINDS}
        call_counts = {kind: calls[sid][kind] for kind in KINDS}
        lat, lon = station_position(station_platforms)
        stations.append((
            sid, name, lat, lon,
            *line_counts.values(), sum(line_counts.values()),
            *call_counts.values(), sum(call_counts.values()),
            importance(line_counts, call_counts),
        ))
    return stations, feed


def existing_station_count(db: sqlite3.Connection) -> int:
    try:
        return db.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
    except sqlite3.DatabaseError:
        return 0  # no database yet (or not one of ours)


def build(zip_path: Path, db_path: Path, source: str = SOURCE_URL, min_stations: int = MIN_STATIONS,
          force: bool = False) -> dict:
    """Read the GTFS file at `zip_path` and fill the stop database at
    `db_path` with it, replacing what it held. Returns some numbers about it.

    Raises BadFeed, and leaves an existing database as it was, if the file
    can't be read or holds far fewer stations than expected. `force` skips
    that last check."""
    try:
        stations, feed = read_feed(zip_path)
    except zipfile.BadZipFile:
        raise BadFeed("the file is not a zip file. It may be damaged or an error page; download it again.") from None
    except KeyError as e:
        raise BadFeed(f"this is not the timetable file the import expects: {e.args[0]} is missing. "
                      "NVBW may have changed the format.") from None
    except (ValueError, IndexError, csv.Error, UnicodeDecodeError) as e:
        raise BadFeed(f"the file has content the import can't read ({e}). NVBW may have changed the format.") from None

    meta = {
        "source": source,
        "attribution": "Datensatz der NVBW GmbH",
        "feed_version": feed.get("feed_version", ""),
        "feed_start_date": feed.get("feed_start_date", ""),
        "feed_end_date": feed.get("feed_end_date", ""),
        "imported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stations": str(len(stations)),
    }

    db_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not db_path.exists()
    # The database is refilled inside one transaction instead of being
    # swapped for a new file: Windows refuses to replace a file the running
    # app has open, while this way readers simply see the old content until
    # the new one is complete. A failure rolls back to the old content.
    db = sqlite3.connect(db_path, timeout=60, isolation_level=None)
    try:
        if not force:
            before = existing_station_count(db)
            if len(stations) < min_stations:
                raise BadFeed(f"the file holds only {len(stations)} stations in Baden-Württemberg (expected about 29,000), "
                              "so it looks incomplete. The existing database was left as it is. Use --force to import it anyway.")
            if len(stations) < before / 2:
                raise BadFeed(f"the file holds {len(stations)} stations but the current database has {before}; "
                              "that is too big a drop to trust. The existing database was left as it is. "
                              "Use --force to import it anyway.")
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("DROP TABLE IF EXISTS stations")
            db.execute("DROP TABLE IF EXISTS meta")
            for statement in CREATE_TABLES:
                db.execute(statement)
            db.executemany(f"INSERT INTO stations VALUES ({', '.join('?' * len(COLUMNS))})", stations)
            db.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    except BaseException:
        db.close()
        if is_new:
            db_path.unlink(missing_ok=True)  # don't leave an empty database behind
        raise
    db.close()
    return meta
