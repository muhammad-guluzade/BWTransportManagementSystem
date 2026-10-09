"""Tests for the stop database: the import from a GTFS file (gtfs/importer.py),
reading it (gtfs/store.py), the import command (import_stops.py) and the
"stops in this area" API endpoint.

A tiny GTFS file is built in a temporary folder, shaped like NVBW's real
one, so the tests need neither the 55 MB download nor the network."""
import csv
import io
import math
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

import requests

import import_stops
from app import app
from gtfs import importer, store

STOPS = [
    # Stuttgart Hbf (oben): platforms only, names carry the platform
    ("de:08111:6115:1:1", "Stuttgart Hauptbahnhof (oben) Gleis 1", "48.7850", "9.1830", "0"),
    ("de:08111:6115:1:2", "Stuttgart Hauptbahnhof (oben) Gleis 2", "48.7854", "9.1834", "0"),
    # Pragsattel: two platforms plus a station row of its own
    ("de:08111:6113:1:1", "Pragsattel", "48.8094", "9.1828", ""),
    ("de:08111:6113:1:2", "Pragsattel", "48.8096", "9.1830", ""),
    ("de:08111:6113_Parent", "Stuttgart Pragsattel", "48.8095", "9.1829", "1"),
    # Löwentorbrücke: one platform, one with a wrong position
    ("de:08111:6114:1:1", "Löwentorbrücke", "48.8038", "9.1835", ""),
    ("de:08111:6114:1:2", "Löwentorbrücke", "4.8667", "1.0161", ""),
    # a village stop, with stray spaces in its name
    ("de:08315:6232:0:1", "Schluchsee  Rathaus ", "47.8185", "8.1772", ""),
    # a roadside bus stop where only rail replacement buses call
    ("de:08125:700:0:1", "Neckarsulm, Viktorshöhe B27", "49.1900", "9.2300", ""),
    # two different places 3 km apart under one station id; the second is busier
    ("de:08135:133:0:1", "Heidenheim Alte Bleiche Bstg 1", "48.6500", "10.1345", ""),
    ("de:08135:133:0:2", "Heidenheim AOK Bstg 2", "48.6800", "10.1345", ""),
    # a stop whose name is just a dot
    ("de:08212:9009:0:1", ".", "49.0082", "8.4360", ""),
    # in the file but never served
    ("de:08111:9999:0:1", "Abandoned Stop", "48.7000", "9.1000", ""),
    # served, but its only position is wrong
    ("de:08135:187:0:1", "Heidenheim Friedrich-Voith-Str", "0.0", "0.0", ""),
    # outside Baden-Württemberg
    ("de:09162:100:1:1", "München Hbf Gleis 1", "48.1402", "11.5600", ""),
    # one stop listed twice: the timetable service only knows de:08226:582
    ("de:08226:582:0:1", "Gaimühle, Bahnhof", "49.4500", "8.9800", ""),
    ("de:08226:400582:0:1", "Gaimühle, Bahnhof", "49.4500", "8.9800", ""),
    # two real stations with the same name across a road: both are known
    ("de:08336:11126:0:1", "Märkt Kirche", "47.6200", "7.6000", ""),
    ("de:08336:11226:0:1", "Märkt Kirche", "47.6201", "7.6001", ""),
]
GHOST = "de:08226:400582"
UNKNOWN_TO_EFA = {GHOST}
ROUTES = [  # id, short name, long name, route_type
    ("r-ice", "ICE", "", "2"), ("r-re5", "RE5", "", "2"),
    ("r-u6", "U6", "", "0"), ("r-u6-back", "U6", "", "0"), ("r-u7", "U7", "", "0"),
    ("r-57", "57", "", "3"), ("r-7343", "7343", "", "3"), ("r-ship", "Schiff", "", "4"),
    ("r-s4", "S4", "", "0"), ("r-tram5", "RNV 5", "", "0"),
    # rail replacement buses, coded as rail in the file
    ("r-sev", "SEV S41", "", "2"), ("r-sev2", "", "Hauptbahnhof/Busbahnhof - Osterburken", "2"),
    # a real train that has no line name
    ("r-tgv", "", "Paris Est - Hauptbahnhof", "2"),
]
STOP_TIMES = [  # route, stop
    ("r-ice", "de:08111:6115:1:1"), ("r-ice", "de:09162:100:1:1"),
    ("r-re5", "de:08111:6115:1:2"), ("r-tgv", "de:08111:6115:1:1"),
    ("r-u6", "de:08111:6113:1:1"), ("r-u6", "de:08111:6114:1:1"),
    ("r-u6-back", "de:08111:6114:1:2"), ("r-u6-back", "de:08111:6113:1:2"),
    ("r-u7", "de:08111:6113:1:1"),
    ("r-57", "de:08111:6113:1:2"), ("r-57", "de:08111:6114:1:1"),
    ("r-7343", "de:08315:6232:0:1"), ("r-7343", "de:08135:187:0:1"), ("r-7343", "de:08212:9009:0:1"),
    ("r-ship", "de:08315:6232:0:1"),
    ("r-sev", "de:08125:700:0:1"), ("r-sev2", "de:08125:700:0:1"),
    ("r-57", "de:08135:133:0:1"), ("r-57", "de:08135:133:0:2"), ("r-7343", "de:08135:133:0:2"),
    ("r-7343", "de:08226:582:0:1"), ("r-7343", "de:08226:400582:0:1"),
    ("r-57", "de:08336:11126:0:1"), ("r-57", "de:08336:11226:0:1"),
    # Löwentorbrücke also gets an S-Bahn and a tram (made up, to see the split)
    ("r-s4", "de:08111:6114:1:1"), ("r-tram5", "de:08111:6114:1:1"),
]
SERVED = {"de:08111:6115", "de:08111:6113", "de:08111:6114", "de:08315:6232", "de:08125:700", "de:08135:133",
          "de:08226:582", "de:08336:11126", "de:08336:11226"}
TWINS = ["de:08226:400582", "de:08226:582", "de:08336:11126", "de:08336:11226"]


def fake_efa(stop_id):
    """Stands in for the live timetable service in the import."""
    fake_efa.asked.append(stop_id)
    return stop_id not in UNKNOWN_TO_EFA


fake_efa.asked = []


def table(header, rows):
    out = io.StringIO()
    writer = csv.writer(out, quoting=csv.QUOTE_ALL)
    writer.writerow(header)
    writer.writerows(rows)
    return "\ufeff" + out.getvalue()  # NVBW's files start with a byte order mark


def write_gtfs(path: Path, change=None) -> Path:
    """Write the tiny GTFS file. `change(name, text)` may alter one of its
    tables, or return None to leave the table out."""
    tables = {
        "stops.txt": table(
            ["stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon", "stop_url", "location_type",
             "parent_station", "wheelchair_boarding", "platform_code"],
            [(sid, "", name, lat, lon, "", loc, "", "0", "") for sid, name, lat, lon, loc in STOPS]),
        "routes.txt": table(
            ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"],
            [(rid, "a", short, long_name, rtype) for rid, short, long_name, rtype in ROUTES]),
        "trips.txt": table(["route_id", "service_id", "trip_id"], [(rid, "s", "t-" + rid) for rid, *_ in ROUTES]),
        "stop_times.txt": table(
            ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
            [("t-" + rid, "08:00:00", "08:00:00", sid, i) for i, (rid, sid) in enumerate(STOP_TIMES)]),
        "feed_info.txt": table(
            ["feed_publisher_name", "feed_publisher_url", "feed_lang", "feed_start_date", "feed_end_date", "feed_version"],
            [("NVBW", "https://www.nvbw.de", "DE", "20260712", "20261212", "20260929")]),
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in tables.items():
            if change:
                text = change(name, text)
            if text is not None:
                archive.writestr(name, text)
    return path


def build(zip_path, **options):
    """importer.build for the tiny file (which has far fewer stations than the
    real minimum), with a fake timetable service."""
    return importer.build(zip_path, store.DB_PATH, **{"min_stations": 1, "knows": fake_efa, **options})


class StopsDbTestCase(unittest.TestCase):
    """Works in a temporary folder with a freshly built database."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.zip = write_gtfs(self.folder / "gtfs.zip")
        self.original_path = store.DB_PATH
        store.DB_PATH = self.folder / "data" / "stops.sqlite"
        self.meta = build(self.zip)

    def tearDown(self):
        store.DB_PATH = self.original_path
        self.tmp.cleanup()

    def stations(self):
        return {row["id"]: dict(row) for row in store.query("SELECT * FROM stations")}

    def leftovers(self):
        return sorted(p.name for p in store.DB_PATH.parent.iterdir() if p.name != store.DB_PATH.name)


class ImportTest(StopsDbTestCase):
    def test_one_row_per_served_station_in_bw(self):
        self.assertEqual(set(self.stations()), SERVED)
        self.assertEqual(self.meta["stations"], str(len(SERVED)))

    def test_station_names(self):
        stations = self.stations()
        # the platform is stripped off
        self.assertEqual(stations["de:08111:6115"]["name"], "Stuttgart Hauptbahnhof (oben)")
        # a station row of its own wins over the platform names
        self.assertEqual(stations["de:08111:6113"]["name"], "Stuttgart Pragsattel")
        # stray spaces are removed
        self.assertEqual(stations["de:08315:6232"]["name"], "Schluchsee Rathaus")

    def test_a_stop_named_with_a_dot_is_left_out(self):
        self.assertNotIn("de:08212:9009", self.stations())

    def test_position_is_the_middle_of_the_platforms(self):
        station = self.stations()["de:08111:6115"]
        self.assertEqual((station["lat"], station["lon"]), (48.7852, 9.1832))

    def test_wrong_positions_are_ignored(self):
        # one of Löwentorbrücke's platforms is far outside BW
        station = self.stations()["de:08111:6114"]
        self.assertEqual((station["lat"], station["lon"]), (48.8038, 9.1835))

    def test_far_apart_platforms_use_the_busiest_one(self):
        # 3 km apart: the middle would be a point where nothing stops
        station = self.stations()["de:08135:133"]
        self.assertEqual((station["lat"], station["lon"]), (48.68, 10.1345))

    def test_lines_and_departures_per_kind(self):
        stations = self.stations()
        hbf = stations["de:08111:6115"]  # ICE, RE5 and a train without a line name
        self.assertEqual((hbf["rail_lines"], hbf["rail_calls"], hbf["bus_lines"], hbf["lines"], hbf["calls"]), (3, 3, 0, 3, 3))
        # U6 has a route id per direction but is one line; plus U7 and bus 57
        pragsattel = stations["de:08111:6113"]
        self.assertEqual((pragsattel["urban_lines"], pragsattel["urban_calls"], pragsattel["bus_lines"], pragsattel["bus_calls"]),
                         (2, 3, 1, 1))
        village = stations["de:08315:6232"]  # a bus and a ship
        self.assertEqual((village["bus_lines"], village["other_lines"], village["lines"], village["calls"]), (1, 1, 2, 2))

    def test_rail_replacement_buses_count_as_buses(self):
        stop = self.stations()["de:08125:700"]
        self.assertEqual((stop["rail_lines"], stop["rail_calls"]), (0, 0))
        self.assertEqual((stop["bus_lines"], stop["bus_calls"]), (2, 2))

    def test_kinds(self):
        stops, _ = store.in_area(-180, -90, 180, 90, 100)
        kinds = {s["id"]: s["kinds"] for s in stops}
        self.assertEqual(kinds["de:08111:6115"], ["rail"])
        self.assertEqual(kinds["de:08111:6113"], ["urban_rail", "bus"])
        self.assertEqual(kinds["de:08315:6232"], ["bus", "other"])
        self.assertEqual(kinds["de:08125:700"], ["bus"])

    def test_modes_split_urban_rail(self):
        stops, _ = store.in_area(-180, -90, 180, 90, 100)
        modes = {s["id"]: s["modes"] for s in stops}
        self.assertEqual(modes["de:08111:6113"], ["ubahn", "bus"])                     # U6, U7, bus 57
        self.assertEqual(modes["de:08111:6114"], ["sbahn", "ubahn", "tram", "bus"])    # U6, S4, RNV 5, bus 57
        self.assertEqual(modes["de:08111:6115"], ["rail"])
        self.assertEqual(modes["de:08315:6232"], ["bus", "other"])
        row = self.stations()["de:08111:6114"]
        # the split adds up to the urban lines, and the ranking is unchanged by it
        self.assertEqual(row["sbahn_lines"] + row["ubahn_lines"] + row["tram_lines"], row["urban_lines"])

    def test_old_database_is_refused_with_a_hint(self):
        # a database from before the split has no tram_lines column
        db = sqlite3.connect(store.DB_PATH)
        db.execute("CREATE TABLE old AS SELECT id, name, lat, lon, rail_lines, urban_lines, bus_lines, other_lines, lines, "
                   "rail_calls, urban_calls, bus_calls, other_calls, calls, importance FROM stations")
        db.execute("DROP TABLE stations")
        db.execute("ALTER TABLE old RENAME TO stations")
        db.commit()
        db.close()
        with self.assertRaises(store.StopsNotImported):
            store.in_area(-180, -90, 180, 90, 100)
        res = app.test_client().get("/api/v1/stops?bbox=7.5,47.5,10.5,49.8")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.get_json()["error"]["code"], "stops_not_imported")
        self.assertIn("older version", res.get_json()["error"]["message"])
        # rebuilding fixes it
        build(self.zip)
        self.assertTrue(store.in_area(-180, -90, 180, 90, 100)[0])

    def test_meta_records_the_schema(self):
        self.assertEqual(store.meta()["schema"], importer.SCHEMA_VERSION)

    def test_meta(self):
        meta = store.meta()
        self.assertEqual(meta["feed_version"], "20260929")
        self.assertEqual(meta["attribution"], "Datensatz der NVBW GmbH")
        self.assertRegex(meta["imported_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_rebuilding_replaces_the_content(self):
        other = write_gtfs(self.folder / "other.zip", lambda name, text: text.replace("Schluchsee  Rathaus ", "Schluchsee Kirche")
                           if name == "stops.txt" else text)
        build(other)
        self.assertEqual(self.stations()["de:08315:6232"]["name"], "Schluchsee Kirche")
        self.assertEqual(len(self.stations()), len(SERVED))
        self.assertEqual(self.leftovers(), [])

    def test_rebuilding_works_while_the_app_has_the_database_open(self):
        # Windows refuses to replace an open file; the import must not need to
        reader = store.connect()
        try:
            reader.execute("SELECT COUNT(*) FROM stations").fetchone()
            build(self.zip)
        finally:
            reader.close()
        self.assertEqual(len(self.stations()), len(SERVED))
        self.assertEqual(self.leftovers(), [])

    def test_same_file_gives_the_same_database(self):
        before = self.stations()
        build(self.zip)
        self.assertEqual(self.stations(), before)


class GhostTest(StopsDbTestCase):
    """Stops the file lists twice, under an id the timetable service doesn't know."""

    def test_the_unknown_twin_is_dropped(self):
        self.assertNotIn(GHOST, self.stations())
        self.assertIn("de:08226:582", self.stations())
        self.assertIn("removed 1", self.meta["duplicates"])

    def test_two_real_stations_with_the_same_name_both_stay(self):
        self.assertIn("de:08336:11126", self.stations())
        self.assertIn("de:08336:11226", self.stations())

    def test_only_twins_are_asked_about(self):
        fake_efa.asked.clear()
        build(self.zip)
        self.assertEqual(sorted(fake_efa.asked), TWINS)

    def test_service_unreachable_keeps_everything(self):
        def down(stop_id):
            raise ConnectionError("no internet")
        meta = build(self.zip, knows=down)
        self.assertIn(GHOST, self.stations())
        self.assertIn("could not check 4", meta["duplicates"])

    def test_if_neither_twin_is_known_both_stay(self):
        meta = build(self.zip, knows=lambda stop_id: stop_id.startswith("de:08336"))
        self.assertIn(GHOST, self.stations())
        self.assertIn("de:08226:582", self.stations())
        self.assertIn("removed 0", meta["duplicates"])

    def test_check_can_be_switched_off(self):
        meta = build(self.zip, knows=None)
        self.assertIn(GHOST, self.stations())
        self.assertEqual(meta["duplicates"], "not checked")

    def test_twin_candidates(self):
        def st(sid, name, lat, lon):
            return (sid, name, lat, lon)
        found = importer.twin_candidates([
            st("a", "Gaimühle, Bahnhof", 49.45, 8.98), st("b", "Gaimühle, Bahnhof", 49.45001, 8.98),   # 1 m apart
            st("c", "Feuerbach", 48.82, 9.17), st("d", "Feuerbach", 47.70, 8.10),                     # 160 km apart
            st("e", "Rathaus", 48.0, 9.0), st("f", "Kirche", 48.0, 9.0),                              # same spot, other name
        ])
        self.assertEqual(found, ["a", "b"])


class RulesTest(unittest.TestCase):
    def test_station_id(self):
        self.assertEqual(importer.station_id("de:08111:6115:1:2"), "de:08111:6115")
        self.assertEqual(importer.station_id("de:08111:6114_Parent"), "de:08111:6114")
        self.assertEqual(importer.station_id("de:08212:90"), "de:08212:90")

    def test_station_name(self):
        name = importer.station_name
        self.assertEqual(name(["Freiburg Hauptbahnhof", "Freiburg Hauptbahnhof Bstg 1", "Freiburg Hauptbahnhof Bstg 10"], None),
                         "Freiburg Hauptbahnhof")
        self.assertEqual(name(["Hauptbf (A.-Klett-Pl.) Gleis 1", "Hauptbf (A.-Klett-Pl.) Pos 3"], None), "Hauptbf (A.-Klett-Pl.)")
        self.assertEqual(name(["Bad Cannstatt", "Bad Cannstatt Bf Pos 1", "Bad Cannstatt Gleis 1"], None), "Bad Cannstatt")
        self.assertEqual(name(["Ulm Hauptbahnhof", "Ulm Hauptbahnhof Bstg A", "Ulm Hbf"], None), "Ulm Hauptbahnhof")
        self.assertEqual(name(["Pragsattel"], "Stuttgart Pragsattel "), "Stuttgart Pragsattel")
        self.assertEqual(name(["Heilbronn  Rathaus"], None), "Heilbronn Rathaus")
        self.assertEqual(name(["Pragsattel"], "   "), "Pragsattel")
        self.assertEqual(name([], None), "")

    def test_real_names_have_a_letter(self):
        self.assertTrue(importer.is_real_name("Jux"))
        self.assertTrue(importer.is_real_name("Öhringen"))
        for junk in (".", "", "  ", "12", "-"):
            self.assertFalse(importer.is_real_name(junk), junk)

    def test_urban_mode(self):
        mode = importer.urban_mode
        for name in ("S1", "S 62", "S60", "SN14", "FEX", "s4"):
            self.assertEqual(mode(name), "sbahn", name)
        for name in ("U6", "U 13", "U15"):
            self.assertEqual(mode(name), "ubahn", name)
        for name in ("1", "6A", "RNV 5", "E", "D", "SC", "NL1", ""):
            self.assertEqual(mode(name), "tram", name)

    def test_route_kind(self):
        kind = importer.route_kind
        self.assertEqual(kind("2", "RE5", ""), "rail")
        self.assertEqual(kind("2", "ICE", ""), "rail")
        self.assertEqual(kind("0", "U6", ""), "urban_rail")
        self.assertEqual(kind("0", "S1", ""), "urban_rail")  # the file codes S-Bahn like trams
        self.assertEqual(kind("3", "57", ""), "bus")
        self.assertEqual(kind("4", "Fähre", ""), "other")
        # rail replacement buses
        self.assertEqual(kind("2", "SEV", ""), "bus")
        self.assertEqual(kind("2", "SEV S41", ""), "bus")
        self.assertEqual(kind("2", "", "Hauptbahnhof/Busbahnhof - Osterburken"), "bus")
        self.assertEqual(kind("2", "", "ZOB Lauda - Hbf (Arnulf-Klett-Platz)"), "bus")
        # real trains without a line name stay trains
        self.assertEqual(kind("2", "", "Paris Est - Hauptbahnhof"), "rail")
        self.assertEqual(kind("2", "", "Ochsenhausen - Warthausen"), "rail")
        # a bus line that happens to be called SEV is a bus anyway
        self.assertEqual(kind("3", "SEV S5", ""), "bus")

    def test_station_position(self):
        position = importer.station_position
        self.assertEqual(position([(48.0, 9.0, 5)]), (48.0, 9.0))
        self.assertEqual(position([(48.0000, 9.0, 1), (48.0010, 9.0, 99)]), (48.0005, 9.0))  # 111 m apart: the middle
        self.assertEqual(position([(48.00, 9.0, 1), (48.03, 9.0, 99)]), (48.03, 9.0))        # 3.3 km apart: the busiest

    def test_bounds(self):
        self.assertTrue(importer.in_bounds(48.78, 9.18))
        self.assertFalse(importer.in_bounds(0.0, 0.0))
        self.assertFalse(importer.in_bounds(48.14, 11.56))  # Munich


class ImportanceTest(unittest.TestCase):
    """The ranking, checked against real stations (numbers from NVBW's file
    of 2026-09-29) where it is clear which one should come first."""

    @staticmethod
    def score(rail=(0, 0), urban=(0, 0), bus=(0, 0), other=(0, 0)):
        kinds = dict(zip(importer.KINDS, (rail, urban, bus, other)))
        return importer.importance({k: v[0] for k, v in kinds.items()}, {k: v[1] for k, v in kinds.items()})

    def setUp(self):
        s = self.score
        self.stuttgart_hbf = s(rail=(26, 4099), urban=(6, 2005))
        self.ulm_hbf = s(rail=(20, 2519), urban=(2, 1056), bus=(7, 2665))
        self.tuebingen_hbf = s(rail=(8, 1718), bus=(68, 15254))
        self.offenburg = s(rail=(15, 1285), bus=(1, 421))
        self.osterburken = s(rail=(5, 427), urban=(4, 265), bus=(25, 1086))
        self.goeppingen_zob = s(bus=(52, 2389))
        self.karlsruhe_hbf_sued = s(bus=(62, 875))
        self.pragsattel = s(urban=(5, 8116), bus=(2, 1308))
        self.loewentorbruecke = s(urban=(3, 7154), bus=(4, 41))
        self.schluchsee_rathaus = s(bus=(4, 76))

    def test_main_stations_beat_bus_hubs(self):
        # 68 bus lines must not outrank the state's biggest railway station
        self.assertGreater(self.stuttgart_hbf, self.tuebingen_hbf)
        self.assertGreater(self.stuttgart_hbf, self.goeppingen_zob)
        self.assertGreater(self.offenburg, self.karlsruhe_hbf_sued)
        self.assertGreater(self.offenburg, self.goeppingen_zob)

    def test_big_junctions_beat_small_ones(self):
        self.assertGreater(self.stuttgart_hbf, self.ulm_hbf)
        self.assertGreater(self.ulm_hbf, self.osterburken)
        self.assertGreater(self.ulm_hbf, self.tuebingen_hbf)

    def test_interchange_beats_the_stop_before_it(self):
        # the case from Stuttgart Pragfriedhof, platform 2
        self.assertGreater(self.pragsattel, self.loewentorbruecke)

    def test_any_tram_stop_beats_a_village_bus_stop(self):
        self.assertGreater(self.loewentorbruecke, self.schluchsee_rathaus)
        self.assertGreater(self.schluchsee_rathaus, 0)

    def test_kind_weights(self):
        s = self.score
        self.assertGreater(s(rail=(1, 100)), s(urban=(1, 100)))
        self.assertGreater(s(urban=(1, 100)), s(bus=(1, 100)))
        self.assertEqual(s(bus=(1, 100)), s(other=(1, 100)))
        self.assertGreater(s(bus=(1, 200)), s(bus=(1, 100)))   # busier
        self.assertGreater(s(bus=(2, 100)), s(bus=(1, 100)))   # better connected
        self.assertEqual(s(), 0)


class SafetyTest(StopsDbTestCase):
    """A bad file must never damage a good database."""

    def assert_refused(self, zip_path, message_part, **options):
        before = self.stations()
        with self.assertRaises(importer.BadFeed) as caught:
            build(zip_path, **options)
        self.assertIn(message_part, str(caught.exception))
        self.assertEqual(self.stations(), before)
        self.assertEqual(self.leftovers(), [])

    def variant(self, name, change):
        return write_gtfs(self.folder / f"{name}.zip", change)

    def test_damaged_file(self):
        damaged = self.folder / "damaged.zip"
        damaged.write_bytes(self.zip.read_bytes()[:300])
        self.assert_refused(damaged, "not a zip file")

    def test_error_page_instead_of_the_file(self):
        page = self.folder / "page.zip"
        page.write_text("<html>503 Service Unavailable</html>")
        self.assert_refused(page, "not a zip file")

    def test_missing_table(self):
        self.assert_refused(self.variant("no_times", lambda n, t: None if n == "stop_times.txt" else t), "stop_times.txt")
        self.assert_refused(self.variant("no_stops", lambda n, t: None if n == "stops.txt" else t), "stops.txt")

    def test_renamed_column(self):
        self.assert_refused(self.variant("renamed", lambda n, t: t.replace('"stop_id"', '"id"', 1) if n == "stops.txt" else t),
                            "stop_id is missing")
        self.assert_refused(self.variant("renamed2", lambda n, t: t.replace('"route_type"', '"type"', 1) if n == "routes.txt" else t),
                            "route_type is missing")
        self.assert_refused(self.variant("renamed3", lambda n, t: t.replace('"trip_id"', '"trip"', 1) if n == "stop_times.txt" else t),
                            "can't read")

    def test_row_with_too_few_columns(self):
        self.assert_refused(self.variant("short", lambda n, t: t + '"t-r-ice"\n' if n == "stop_times.txt" else t), "can't read")

    def test_empty_tables_do_not_wipe_the_database(self):
        header_only = lambda table_name: (lambda n, t: t.splitlines()[0] + "\n" if n == table_name else t)  # noqa: E731
        self.assert_refused(self.variant("no_rows", header_only("stops.txt")), "only 0 stations")
        self.assert_refused(self.variant("no_rows2", header_only("stop_times.txt")), "only 0 stations")

    def test_too_few_stations_for_the_real_minimum(self):
        # the tiny file has 9 stations; the real file has about 29,000
        self.assert_refused(self.zip, f"only {len(SERVED)} stations", min_stations=importer.MIN_STATIONS)

    def test_a_big_drop_is_refused(self):
        one_station = self.variant("one", lambda n, t: "\n".join(
            line for line in t.splitlines() if "stop_id" in line or "de:08111:6115" in line) + "\n" if n == "stops.txt" else t)
        self.assert_refused(one_station, "too big a drop")

    def test_force_imports_anyway(self):
        one_station = self.variant("one", lambda n, t: "\n".join(
            line for line in t.splitlines() if "stop_id" in line or "de:08111:6115" in line) + "\n" if n == "stops.txt" else t)
        build(one_station, force=True)
        self.assertEqual(set(self.stations()), {"de:08111:6115"})

    def test_optional_parts_may_be_missing(self):
        build(self.variant("no_feed_info", lambda n, t: None if n == "feed_info.txt" else t))
        self.assertEqual(store.meta()["feed_version"], "")
        build(self.variant("no_location_type", lambda n, t: t.replace('"location_type",', "", 1) if n == "stops.txt" else t))
        self.assertTrue(self.stations())

    def test_text_instead_of_a_position_skips_that_platform(self):
        build(self.variant("text", lambda n, t: t.replace('"48.7850"', '"abc"') if n == "stops.txt" else t))
        station = self.stations()["de:08111:6115"]
        self.assertEqual((station["lat"], station["lon"]), (48.7854, 9.1834))

    def test_failed_first_import_leaves_no_database_behind(self):
        store.DB_PATH = self.folder / "fresh" / "stops.sqlite"
        with self.assertRaises(importer.BadFeed):
            importer.build(self.zip, store.DB_PATH, knows=None)  # real minimum: far too few stations
        self.assertFalse(store.DB_PATH.exists())
        with self.assertRaises(store.StopsNotImported):
            store.meta()


class FakeResponse:
    """What importer.download needs from requests.get."""

    def __init__(self, chunks=(), status=200, length=None, drop_after=None):
        self.chunks, self.status, self.drop_after = chunks, status, drop_after
        self.headers = {"content-length": str(length)} if length is not None else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status} Server Error")

    def iter_content(self, chunk_size=0):
        for i, chunk in enumerate(self.chunks):
            if self.drop_after is not None and i >= self.drop_after:
                raise requests.ConnectionError("connection dropped")
            yield chunk


class DownloadTest(StopsDbTestCase):
    def setUp(self):
        super().setUp()
        self.real_get = importer.requests.get
        self.dest = store.DB_PATH.parent / "bwgesamt.zip"
        self.good = self.zip.read_bytes()

    def tearDown(self):
        importer.requests.get = self.real_get
        super().tearDown()

    def serve(self, response):
        if isinstance(response, Exception):
            def get(*args, **kwargs):
                raise response
        else:
            def get(*args, **kwargs):
                return response
        importer.requests.get = get

    def test_download(self):
        half = len(self.good) // 2
        self.serve(FakeResponse([self.good[:half], self.good[half:]], length=len(self.good)))
        seen = []
        importer.download(self.dest, progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(self.dest.read_bytes(), self.good)
        self.assertEqual(seen[-1], (len(self.good), len(self.good)))
        self.assertEqual(self.leftovers(), ["bwgesamt.zip"])

    def test_failures_leave_nothing_behind(self):
        cases = [
            (requests.ConnectionError("no internet"), requests.RequestException),
            (FakeResponse(status=404), requests.RequestException),
            (FakeResponse(status=500), requests.RequestException),
            (FakeResponse([b"x" * 100] * 5, length=500, drop_after=2), requests.RequestException),
            # the connection closes early without an error
            (FakeResponse([self.good[:100]], length=len(self.good)), importer.BadFeed),
            # an error page with status 200
            (FakeResponse([b"<html>maintenance</html>"]), importer.BadFeed),
        ]
        for response, expected in cases:
            self.serve(response)
            with self.assertRaises(expected):
                importer.download(self.dest)
            self.assertEqual(self.leftovers(), [])

    def test_failed_download_keeps_an_earlier_file(self):
        self.dest.write_bytes(self.good)
        self.serve(FakeResponse([b"<html>maintenance</html>"]))
        with self.assertRaises(importer.BadFeed):
            importer.download(self.dest)
        self.assertEqual(self.dest.read_bytes(), self.good)


class CommandTest(DownloadTest):
    """python import_stops.py, as a person runs it: clear messages, no crashes."""

    def setUp(self):
        super().setUp()
        self.originals = (import_stops.DOWNLOAD_PATH, importer.MIN_STATIONS, sys.argv)
        import_stops.DOWNLOAD_PATH = self.dest
        importer.MIN_STATIONS = 1
        # importer.build reads its default from the function signature
        self.real_build = importer.build
        importer.build = lambda zip_path, db_path, force=False: self.real_build(zip_path, db_path, min_stations=1, force=force,
                                                                                 knows=fake_efa)

    def tearDown(self):
        import_stops.DOWNLOAD_PATH, importer.MIN_STATIONS, sys.argv = self.originals
        importer.build = self.real_build
        super().tearDown()

    def run_command(self, *args):
        sys.argv = ["import_stops.py", *args]
        out = io.StringIO()
        with redirect_stdout(out):
            code = import_stops.main()
        return code, out.getvalue()

    def test_from_a_file(self):
        code, said = self.run_command("--file", str(self.zip))
        self.assertEqual(code, 0)
        self.assertIn(f"{len(SERVED)} stations", said)
        self.assertIn("duplicates: checked 4 possible duplicate stops", said)
        self.assertIn("20260929", said)

    def test_download_and_build(self):
        self.serve(FakeResponse([self.good], length=len(self.good)))
        code, said = self.run_command()
        self.assertEqual(code, 0)
        self.assertEqual(self.leftovers(), [])  # the download is removed afterwards
        code, _ = self.run_command("--keep")
        self.assertEqual((code, self.leftovers()), (0, ["bwgesamt.zip"]))

    def test_problems_are_explained_without_a_crash(self):
        damaged = self.folder / "damaged.zip"
        damaged.write_bytes(self.good[:300])
        renamed = write_gtfs(self.folder / "renamed.zip", lambda n, t: t.replace('"stop_id"', '"id"', 1) if n == "stops.txt" else t)
        before = self.stations()
        for args, expected in (
            (("--file", str(self.folder / "missing.zip")), "File not found"),
            (("--file", str(damaged)), "not a zip file"),
            (("--file", str(renamed)), "stop_id is missing"),
        ):
            code, said = self.run_command(*args)
            self.assertEqual(code, 1, args)
            self.assertIn(expected, said)
            self.assertNotIn("Traceback", said)
        self.assertEqual(self.stations(), before)

    def test_download_problems_are_explained(self):
        before = self.stations()
        for response, expected in (
            (requests.ConnectionError("no internet"), "Check your internet connection"),
            (FakeResponse([self.good[:100]], length=len(self.good)), "stopped early"),
            (FakeResponse([b"<html>maintenance</html>"]), "not a zip file"),
        ):
            self.serve(response)
            code, said = self.run_command()
            self.assertEqual(code, 1)
            self.assertIn(expected, said)
            self.assertIn("was not changed", said)
            self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.stations(), before)

    def test_incomplete_file_needs_force(self):
        # the real minimum of 10,000 stations
        importer.build = lambda zip_path, db_path, force=False: self.real_build(zip_path, db_path, force=force, knows=fake_efa)
        code, said = self.run_command("--file", str(self.zip))
        self.assertEqual(code, 1)
        self.assertIn("--force", said)
        code, _ = self.run_command("--file", str(self.zip), "--force")
        self.assertEqual(code, 0)


class StoreTest(StopsDbTestCase):
    def test_info(self):
        info = store.info(today=date(2026, 10, 2))
        self.assertEqual(list(info), ["attribution", "source", "version", "valid_until", "imported_at", "stations", "expired"])
        self.assertEqual((info["attribution"], info["source"]), ("Datensatz der NVBW GmbH", "https://www.nvbw.de/open-data"))
        self.assertEqual((info["version"], info["valid_until"], info["stations"]), ("20260929", "2026-12-12", len(SERVED)))
        self.assertFalse(info["expired"])

    def test_expired_after_the_timetable_period(self):
        self.assertFalse(store.info(today=date(2026, 12, 12))["expired"])
        self.assertTrue(store.info(today=date(2026, 12, 13))["expired"])

    def test_no_end_date_never_expires(self):
        build(write_gtfs(self.folder / "x.zip", lambda n, t: None if n == "feed_info.txt" else t))
        info = store.info(today=date(2030, 1, 1))
        self.assertEqual((info["valid_until"], info["version"], info["expired"]), (None, None, False))

    def test_missing_or_empty_database(self):
        store.DB_PATH = self.folder / "nowhere.sqlite"
        with self.assertRaises(store.StopsNotImported):
            store.in_area(0, 0, 1, 1, 10)
        with self.assertRaises(store.StopsNotImported):
            store.importance()
        sqlite3.connect(store.DB_PATH).close()  # a file without tables
        with self.assertRaises(store.StopsNotImported):
            store.in_area(0, 0, 1, 1, 10)
        with self.assertRaises(store.StopsNotImported):
            store.importance()

    def test_importance_of_every_station(self):
        stations = self.stations()
        self.assertEqual(store.importance(), {stop_id: row["importance"] for stop_id, row in stations.items()})
        self.assertEqual(len(store.importance()), len(SERVED))
        self.assertIs(store.importance(), store.importance())  # kept in memory, not read every time

    def test_importance_is_read_again_after_an_import(self):
        before = dict(store.importance())
        db = sqlite3.connect(store.DB_PATH)
        db.execute("UPDATE stations SET importance = importance + 100")
        db.commit()
        db.close()
        # some file systems keep times only to the second
        stat = store.DB_PATH.stat()
        os.utime(store.DB_PATH, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        after = store.importance()
        self.assertEqual({stop_id: round(value - 100, 6) for stop_id, value in after.items()},
                         {stop_id: round(value, 6) for stop_id, value in before.items()})

    def test_boards_get_the_importance_or_nothing(self):
        from services.departures import stop_importance
        self.assertEqual(stop_importance(), store.importance())
        store.DB_PATH = self.folder / "nowhere.sqlite"
        self.assertEqual(stop_importance(), {})
        store.DB_PATH.write_text("this is not a database")
        with self.assertLogs("services.departures", level="WARNING"):
            self.assertEqual(stop_importance(), {})


class AreaTest(StopsDbTestCase):
    def test_most_important_first(self):
        stops, total = store.in_area(9.0, 48.7, 9.3, 48.9, 10)
        self.assertEqual(total, 3)
        # in this test file Löwentorbrücke has more lines (U6, S4, a tram, a bus) than Pragsattel
        self.assertEqual([s["id"] for s in stops], ["de:08111:6115", "de:08111:6114", "de:08111:6113"])

    def test_limit_keeps_the_most_important(self):
        stops, total = store.in_area(9.0, 48.7, 9.3, 48.9, 1)
        self.assertEqual((total, [s["id"] for s in stops]), (3, ["de:08111:6115"]))

    def test_only_inside_the_rectangle(self):
        stops, total = store.in_area(8.0, 47.7, 8.3, 47.9, 10)
        self.assertEqual((total, [s["name"] for s in stops]), (1, ["Schluchsee Rathaus"]))
        self.assertEqual(store.in_area(0, 0, 1, 1, 10), ([], 0))


class SpreadTest(StopsDbTestCase):
    """Picking stations evenly across a map instead of strictly by importance."""

    STUTTGART = (9.0, 48.7, 9.3, 48.9)   # three stations, all in the city
    WIDE = (7.5, 47.5, 10.5, 49.8)       # the whole state: six stations

    def test_plain_top_list_crowds_into_the_city(self):
        stops, total = store.in_area(*self.WIDE, 3)
        self.assertEqual(total, len(SERVED))
        self.assertEqual({s["id"] for s in stops}, {"de:08111:6115", "de:08111:6113", "de:08111:6114"})

    def test_spread_takes_the_best_station_of_each_region(self):
        stops, total = store.in_area(*self.WIDE, 3, spread=True)
        self.assertEqual(total, len(SERVED))
        ids = [s["id"] for s in stops]
        # one for Stuttgart (its most important station), then other regions
        self.assertEqual(ids[0], "de:08111:6115")
        self.assertNotIn("de:08111:6113", ids)
        self.assertNotIn("de:08111:6114", ids)
        self.assertEqual(len(ids), 3)

    def test_spread_keeps_importance_order(self):
        stops, _ = store.in_area(*self.WIDE, 3, spread=True)
        scores = [self.stations()[s["id"]]["importance"] for s in stops]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_everything_is_returned_once_it_fits(self):
        # zoomed in far enough, spreading must not hide stops
        plain, _ = store.in_area(*self.STUTTGART, 10)
        spread, _ = store.in_area(*self.STUTTGART, 10, spread=True)
        self.assertEqual(spread, plain)
        self.assertEqual(len(spread), 3)

    def test_never_more_than_the_limit(self):
        for limit in (1, 2, 4):
            stops, _ = store.in_area(*self.WIDE, limit, spread=True)
            self.assertLessEqual(len(stops), limit)
            self.assertGreaterEqual(len(stops), 1)

    @staticmethod
    def sizes(west, east, south, north, limit=150, count=4):
        candidates = store.cell_sizes(west, east, south, north, limit)
        return [next(candidates) for _ in range(count)]

    def test_cell_sizes_come_from_a_fixed_ladder(self):
        sizes = self.sizes(9.00, 9.60, 48.60, 48.95)
        for size in sizes:
            # powers of two and the steps half-way between
            self.assertAlmostEqual((2 * math.log2(size)) % 1, 0)
        for finer, coarser in zip(sizes, sizes[1:]):
            self.assertAlmostEqual(coarser / finer, math.sqrt(2))

    def test_grid_does_not_move_with_the_map(self):
        # the same view moved a little (panning) uses the same ladder
        self.assertEqual(self.sizes(9.00, 9.60, 48.60, 48.95), self.sizes(9.03, 9.63, 48.62, 48.97))
        # zooming out makes the cells bigger, zooming in smaller
        city = self.sizes(9.00, 9.60, 48.60, 48.95)[0]
        self.assertGreater(self.sizes(7.5, 10.5, 47.5, 49.8)[0], city)
        self.assertLess(self.sizes(9.17, 9.20, 48.79, 48.81)[0], city)

    def test_first_candidate_is_finer_than_needed(self):
        # it would hold more cells than the limit, so the search starts on the fine side
        west, east, south, north, limit = 7.5, 10.5, 47.5, 49.8, 150
        first = self.sizes(west, east, south, north, limit)[0]
        cells = ((east - west) / first) * ((north - south) / (first * store.LAT_PER_LON))
        self.assertGreater(cells, 2 * limit)

    def test_a_slightly_different_view_gives_a_similar_number_of_stations(self):
        # the statewide map in two window sizes: the count must not jump
        stations = [(f"de:08111:{i}", f"Stop {i}", 47.6 + (i % 40) * 0.05, 7.6 + (i // 40) * 0.07) for i in range(1600)]
        db = sqlite3.connect(store.DB_PATH)
        db.execute("DELETE FROM stations")
        db.executemany("INSERT INTO stations VALUES (?, ?, ?, ?, 0, 0, 1, 0, 1, 0, 0, ?, 0, ?, ?, 0, 0, 0)",
                       [(sid, name, lat, lon, i % 97, i % 97, (i % 97) / 10) for i, (sid, name, lat, lon) in enumerate(stations)])
        db.commit()
        db.close()
        counts = [len(store.in_area(*view, 150, spread=True)[0])
                  for view in ((6.58, 47.25, 11.42, 50.05), (6.59, 47.26, 11.41, 50.04), (6.3, 47.1, 11.7, 50.2))]
        for count in counts:
            self.assertGreater(count, 75)      # more than half the limit...
            self.assertLessEqual(count, 150)   # ...and never above it


class AreaApiTest(StopsDbTestCase):
    def setUp(self):
        super().setUp()
        self.http = app.test_client()

    def test_shape(self):
        res = self.http.get("/api/v1/stops?bbox=9.0,48.7,9.3,48.9&limit=2")
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(list(body), ["stops", "total", "limit"])
        self.assertEqual((body["total"], body["limit"], len(body["stops"])), (3, 2, 2))
        stop = body["stops"][0]
        self.assertEqual(list(stop), ["id", "name", "lat", "lon", "lines", "kinds", "modes"])
        self.assertEqual((stop["id"], stop["name"], stop["lines"], stop["kinds"], stop["modes"]),
                         ("de:08111:6115", "Stuttgart Hauptbahnhof (oben)", 3, ["rail"], ["rail"]))

    def test_limit_defaults_and_is_capped(self):
        self.assertEqual(self.http.get("/api/v1/stops?bbox=9.0,48.7,9.3,48.9").get_json()["limit"], 100)
        self.assertEqual(self.http.get("/api/v1/stops?bbox=9.0,48.7,9.3,48.9&limit=99999").get_json()["limit"], 500)
        self.assertEqual(self.http.get("/api/v1/stops?bbox=9.0,48.7,9.3,48.9&limit=0").get_json()["limit"], 1)

    def test_invalid_requests(self):
        for query in ("", "?bbox=", "?bbox=1,2,3", "?bbox=a,b,c,d", "?bbox=9.3,48.7,9.0,48.9", "?bbox=9.0,48.9,9.3,48.7",
                      "?bbox=9.0,48.7,9.3,480", "?bbox=9.0,48.7,9.3,48.9&limit=many"):
            res = self.http.get("/api/v1/stops" + query)
            self.assertEqual(res.status_code, 400, query)
            self.assertEqual(res.get_json()["error"]["code"], "invalid_parameter", query)

    def test_spread_parameter(self):
        whole_state = "/api/v1/stops?bbox=7.5,47.5,10.5,49.8&limit=3"
        plain = [s["id"] for s in self.http.get(whole_state).get_json()["stops"]]
        self.assertEqual(set(plain), {"de:08111:6115", "de:08111:6113", "de:08111:6114"})
        for value in ("1", "true"):
            body = self.http.get(f"{whole_state}&spread={value}").get_json()
            ids = [s["id"] for s in body["stops"]]
            self.assertEqual(list(body), ["stops", "total", "limit"])   # same shape as without
            self.assertEqual((ids[0], body["total"]), ("de:08111:6115", len(SERVED)))
            self.assertNotIn("de:08111:6113", ids)
        for value in ("0", "", "no"):
            ids = [s["id"] for s in self.http.get(f"{whole_state}&spread={value}").get_json()["stops"]]
            self.assertEqual(ids, plain)

    def test_index_says_where_the_stop_data_comes_from(self):
        data = self.http.get("/api/v1/").get_json()["stop_data"]
        self.assertEqual((data["attribution"], data["version"], data["valid_until"], data["stations"]),
                         ("Datensatz der NVBW GmbH", "20260929", "2026-12-12", len(SERVED)))
        self.assertIn("expired", data)

    def test_database_not_built(self):
        store.DB_PATH = self.folder / "missing.sqlite"
        res = self.http.get("/api/v1/stops?bbox=9.0,48.7,9.3,48.9")
        self.assertEqual(res.status_code, 503)
        error = res.get_json()["error"]
        self.assertEqual(error["code"], "stops_not_imported")
        self.assertIn("import_stops.py", error["message"])

    def test_the_rest_works_without_the_database(self):
        store.DB_PATH = self.folder / "missing.sqlite"
        index = self.http.get("/api/v1/")
        self.assertEqual(index.status_code, 200)
        self.assertIsNone(index.get_json()["stop_data"])
        self.assertEqual(self.http.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
