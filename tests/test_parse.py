"""Tests for the normalisation rules. Inputs mirror real EFA responses
(field names and values as seen via debug_efa.py)."""
import unittest
from datetime import datetime, timedelta, timezone

from efa import parse
from services.departures import group_departures, platform_sort_key


def transport(cls, name="", attributes=None, **props):
    if attributes:
        props["attributes"] = attributes
    return {"product": {"class": cls, "name": name}, "properties": props}


def platform(raw_name="", number="", stop="Pragfriedhof", city="Stuttgart"):
    props = {}
    if raw_name:
        props["platformName"] = raw_name
    if number:
        props["platform"] = number
    return {
        "name": stop,
        "type": "platform",
        "properties": props,
        "parent": {"name": stop, "type": "stop", "parent": {"name": city, "type": "locality"}},
    }


def onward(name, classes):
    return {"name": name, "type": "platform", "productClasses": classes, "parent": {"name": name, "type": "stop"}}


ICE_ATTRS = ["HIGHSPEEDTRAIN", "LONG_DISTANCE_TRAINS", "SUPPLEMENT"]
SPECIAL_ATTRS = ["LONG_DISTANCE_TRAINS", "SUPPLEMENT", "DIFFERENT_FARES"]


class ModeGroupTest(unittest.TestCase):
    def test_regional_names_collapse_into_groups(self):
        self.assertEqual(parse.mode_group(transport(3, "Stadtbahn")), "U-Bahn / Tram")
        self.assertEqual(parse.mode_group(transport(4, "Straßenbahn Linie")), "U-Bahn / Tram")
        self.assertEqual(parse.mode_group(transport(1, "S-Bahn")), "S-Bahn")
        self.assertEqual(parse.mode_group(transport(13, "Metropolexpress")), "Regional train")
        self.assertEqual(parse.mode_group(transport(5, "Stadtbus Konstanz")), "Bus")
        self.assertEqual(parse.mode_group(transport(6, "SBG-Bus")), "Bus")

    def test_long_distance(self):
        self.assertEqual(parse.mode_group(transport(16, "Zug", ICE_ATTRS)), "Long-distance train")
        # a TGV arrives with the regional class but long-distance attributes
        self.assertEqual(parse.mode_group(transport(13, "Zug", SPECIAL_ATTRS)), "Long-distance train")
        self.assertEqual(parse.mode_group(transport(7, "Flixbus (Sondertarif)", SPECIAL_ATTRS)), "Long-distance bus")

    def test_unknown_class_is_other(self):
        self.assertEqual(parse.mode_group(transport(99, "Hovercraft")), "Other")
        self.assertEqual(parse.mode_group({}), "Other")


class DticketTest(unittest.TestCase):
    def test_local_and_regional_transport_is_valid(self):
        for cls in (1, 3, 4, 5, 6, 13):
            self.assertIs(parse.dticket_valid(transport(cls)), True)

    def test_long_distance_is_not_valid(self):
        self.assertIs(parse.dticket_valid(transport(16, "Zug", ICE_ATTRS)), False)
        self.assertIs(parse.dticket_valid(transport(15, "Zug", SPECIAL_ATTRS)), False)
        self.assertIs(parse.dticket_valid(transport(7, "Flixbus (Sondertarif)", SPECIAL_ATTRS)), False)
        self.assertIs(parse.dticket_valid(transport(9, "Fähre", SPECIAL_ATTRS)), False)

    def test_unsure_gets_no_verdict(self):
        self.assertIsNone(parse.dticket_valid(transport(10, "Ruftaxi/-bus")))
        self.assertIsNone(parse.dticket_valid({}))


class LineNameTest(unittest.TestCase):
    def test_line_label(self):
        self.assertEqual(parse.line_name({"disassembledName": "U7", "number": "U7"}), "U7")

    def test_long_distance_train_uses_type_and_number(self):
        t = transport(16, "Zug", ICE_ATTRS, trainType="ICE", trainNumber="1291")
        t["name"] = "ICE 1291 InterCityExpress"
        self.assertEqual(parse.line_name(t), "ICE 1291")

    def test_fallback(self):
        self.assertEqual(parse.line_name({"name": "Zug"}), "Zug")
        self.assertEqual(parse.line_name({}), "?")


class PlatformLabelTest(unittest.TestCase):
    def test_prefixes_are_normalised(self):
        cases = {
            "Gleis 3": "Platform 3",
            "Bstg. A": "Platform A",
            "Pos. 2": "Platform 2",
            "101": "Platform 101",
            "Gleis 1(U)": "Platform 1(U)",
            "4 Nord": "Platform 4 Nord",
            "5a": "Platform 5a",
            "Bstg. H3": "Platform H3",
        }
        for raw, expected in cases.items():
            self.assertEqual(parse.platform_label(platform(raw)), expected)

    def test_platform_code_is_the_bare_value(self):
        self.assertEqual(parse.platform_code(platform("Gleis 3")), "3")
        self.assertEqual(parse.platform_code(platform("Bstg. A")), "A")
        self.assertEqual(parse.platform_code(platform("4 Nord")), "4 Nord")
        self.assertIsNone(parse.platform_code(platform()))
        self.assertIsNone(parse.platform_code(platform("Flix")))

    def test_slug(self):
        self.assertEqual(parse.slug("U-Bahn / Tram"), "u_bahn_tram")
        self.assertEqual(parse.slug("S-Bahn"), "s_bahn")
        self.assertEqual(parse.slug("Long-distance train"), "long_distance_train")

    def test_falls_back_to_platform_number(self):
        self.assertEqual(parse.platform_label(platform(number="2")), "Platform 2")

    def test_missing_or_junk_is_unknown(self):
        self.assertEqual(parse.platform_label(platform()), "Unknown platform")
        self.assertEqual(parse.platform_label(platform("Flix")), "Unknown platform")
        self.assertEqual(parse.platform_label(platform("Ein")), "Unknown platform")
        self.assertEqual(parse.platform_label({}), "Unknown platform")


class ShortNameTest(unittest.TestCase):
    def test_drops_own_city(self):
        self.assertEqual(parse.short_name("Heidelberg, Seegarten", "Heidelberg"), "Seegarten")
        self.assertEqual(parse.short_name("Karlsruhe Ebertstraße", "Karlsruhe"), "Ebertstraße")
        self.assertEqual(parse.short_name("Freiburg, Eschholzstraße", "Freiburg im Breisgau"), "Eschholzstraße")

    def test_keeps_other_cities(self):
        self.assertEqual(parse.short_name("Mannheim, Hauptbahnhof", "Heidelberg"), "Mannheim, Hauptbahnhof")
        self.assertEqual(parse.short_name("Stuttgart", "Stuttgart"), "Stuttgart")
        self.assertEqual(parse.short_name("Pragsattel", ""), "Pragsattel")


class ViaStopsTest(unittest.TestCase):
    def event(self, *stops):
        return {"location": platform("Gleis 1"), "onwardLocations": [onward(n, c) for n, c in stops]}

    def test_picks_major_stops_in_travel_order(self):
        # U6 from Pragfriedhof towards Flughafen/Messe
        event = self.event(
            ("Stadtbibliothek", [3, 5]),
            ("Hauptbf (A.-Klett-Pl.)", [0, 3, 5, 6, 11]),
            ("Schlossplatz", [3, 5, 11]),
            ("Charlottenplatz", [3, 5, 6, 11]),
            ("Olgaeck", [3, 5]),
            ("Flughafen/Messe", [1, 3, 5]),
        )
        self.assertEqual(parse.via_stops(event), ["Hauptbf (A.-Klett-Pl.)", "Charlottenplatz"])

    def test_every_second_stop_when_all_equal(self):
        event = self.event(*[(name, [5]) for name in "ABCDEF"])
        self.assertEqual(parse.via_stops(event), ["B", "D"])

    def test_nothing_when_destination_is_next(self):
        self.assertEqual(parse.via_stops(self.event(("Killesberg", [3, 5]))), [])
        self.assertEqual(parse.via_stops({"location": platform("Gleis 1")}), [])

    def test_destination_is_not_a_via_stop(self):
        event = self.event(("Stadtbibliothek", [3, 5]), ("Pragsattel", [3]), ("Hauptbahnhof", [0, 1, 3, 5]))
        self.assertEqual(parse.via_stops(event), ["Stadtbibliothek", "Pragsattel"])


class StopSearchTest(unittest.TestCase):
    POINT = {
        "anyType": "stop",
        "name": "Freiburg im Breisgau, Hauptbahnhof",
        "object": "Hauptbahnhof",
        "mainLoc": "Freiburg im Breisgau",
        "stateless": "6906508:$Z1",
        "quality": "965",
        "modes": "0,1,4,5",
        "ref": {"id": "6906508", "gid": "de:08311:6508", "coords": "7.840954,47.997458"},
    }

    def test_single_match_is_not_dropped(self):
        # EFA wraps a single match as {"point": {...}} instead of a list
        single = {"stopFinder": {"points": {"point": self.POINT}}}
        self.assertEqual(parse.extract_points(single), [self.POINT])
        many = {"stopFinder": {"points": [self.POINT, self.POINT]}}
        self.assertEqual(len(parse.extract_points(many)), 2)
        self.assertEqual(parse.extract_points({"stopFinder": {}}), [])

    def test_parse_stop(self):
        stop = parse.parse_stop(self.POINT)
        self.assertEqual(stop["id"], "de:08311:6508")
        # the full name, so the city stays visible after picking a result
        self.assertEqual(stop["name"], "Freiburg im Breisgau, Hauptbahnhof")
        self.assertEqual((stop["quality"], stop["classes"]), (965, [0, 1, 4, 5]))
        # EFA's search gives "lon,lat"
        self.assertEqual((stop["lat"], stop["lon"]), (47.997458, 7.840954))

    def test_stop_without_coordinates(self):
        point = {**self.POINT, "ref": {"gid": "de:08311:6508", "coords": "garbage"}}
        stop = parse.parse_stop(point)
        self.assertEqual((stop["lat"], stop["lon"]), (None, None))

    def test_non_stops_are_skipped(self):
        self.assertIsNone(parse.parse_stop({**self.POINT, "anyType": "street"}))

    def test_restricted_to_baden_wuerttemberg(self):
        self.assertTrue(parse.in_baden_wuerttemberg("de:08311:6508"))
        self.assertFalse(parse.in_baden_wuerttemberg("de:09162:6"))  # Bavaria
        self.assertFalse(parse.in_baden_wuerttemberg("6906508:$Z1"))


class StationTest(unittest.TestCase):
    # as returned for Stuttgart Hbf (oben)
    RESPONSE = {
        "locations": [{
            "id": "de:08111:6115",
            "name": "Stuttgart, Hauptbahnhof (oben)",
            "type": "stop",
            "coord": [48.784729, 9.183172],
            "assignedStops": [
                {"id": "de:08111:6115", "name": "Stuttgart Hauptbahnhof (oben)", "productClasses": [0, 1, 13, 16]},
                {"id": "de:08111:6118", "name": "Stuttgart Hauptbahnhof (tief)", "productClasses": [1],
                 "coord": [48.783385, 9.180225]},
                {"id": "de:08111:6112", "name": "Hauptbf (Arnulf-Klett-Platz)", "productClasses": [0, 3, 5, 6, 11]},
            ],
        }],
    }

    def test_station_and_nearby(self):
        station = parse.parse_station(self.RESPONSE)
        self.assertEqual(station["id"], "de:08111:6115")
        self.assertEqual(station["name"], "Stuttgart, Hauptbahnhof (oben)")
        self.assertEqual(station["classes"], [0, 1, 13, 16])
        self.assertEqual((station["lat"], station["lon"]), (48.784729, 9.183172))
        self.assertEqual(station["nearby"], [
            {"id": "de:08111:6118", "name": "Stuttgart Hauptbahnhof (tief)", "lat": 48.783385, "lon": 9.180225,
             "classes": [1]},
            # no coordinates from EFA -> None, not a crash
            {"id": "de:08111:6112", "name": "Hauptbf (Arnulf-Klett-Platz)", "lat": None, "lon": None,
             "classes": [0, 3, 5, 6, 11]},
        ])

    def test_unknown_stop(self):
        self.assertIsNone(parse.parse_station({}))
        self.assertIsNone(parse.parse_station({"locations": []}))

    def test_tabs(self):
        tabs = parse.tabs_for([0, 1, 4, 5, 6, 7, 13, 16])  # Freiburg Hbf
        self.assertEqual([t["id"] for t in tabs], ["trains", "sbahn", "tram", "bus"])
        # the whole train family is requested, even classes the station didn't list
        self.assertEqual(tabs[0]["classes"], [0, 13, 14, 15, 16])

    def test_unknown_classes_get_their_own_tab(self):
        tabs = parse.tabs_for([3, 11])
        self.assertEqual([(t["id"], t["classes"]) for t in tabs], [("tram", [2, 3, 4]), ("other", [11])])
        self.assertEqual(parse.tabs_for([]), [])

    def test_flights_and_on_demand_are_left_out(self):
        # Stuttgart airport: S-Bahn, Stadtbahn, buses, flights (12)
        tabs = parse.tabs_for([1, 3, 5, 6, 7, 12])
        self.assertEqual([t["id"] for t in tabs], ["sbahn", "tram", "bus"])
        self.assertEqual(parse.tabs_for([10, 12]), [])
        self.assertNotIn(10, parse.tabs_for([5, 10])[0]["classes"])
        self.assertTrue(parse.is_excluded({"transportation": transport(12, "Flugzeug")}))
        self.assertTrue(parse.is_excluded({"transportation": transport(10, "Ruftaxi/-bus")}))
        self.assertFalse(parse.is_excluded({"transportation": transport(7, "Flixbus (Sondertarif)")}))
        self.assertFalse(parse.is_excluded({}))

    def test_tab_names(self):
        self.assertEqual(parse.tab_names([0, 13, 16]), "Trains")
        self.assertEqual(parse.tab_names([5, 6]), "Bus")


class ParseDepartureTest(unittest.TestCase):
    def test_times_and_delay(self):
        event = {
            "location": platform("Gleis 2"),
            "departureTimePlanned": "2026-10-01T08:00:00Z",
            "departureTimeEstimated": "2026-10-01T08:03:00Z",
            "transportation": {
                **transport(3, "Stadtbahn"),
                "disassembledName": "U6",
                "destination": {"name": "Gerlingen"},
            },
        }
        now = datetime(2026, 10, 1, 7, 58, tzinfo=timezone.utc)
        dep = parse.parse_departure(event, now)
        self.assertEqual(dep["mode"], "U-Bahn / Tram")
        self.assertEqual(dep["platform"], "Platform 2")
        self.assertEqual(dep["line"], "U6")
        self.assertEqual(dep["destination"], "Gerlingen")
        self.assertEqual(dep["platform_code"], "2")
        self.assertEqual(dep["time"], "2026-10-01T08:03:00Z")
        self.assertEqual(dep["planned"], "2026-10-01T08:00:00Z")
        self.assertEqual((dep["minutes"], dep["delay"], dep["realtime"]), (5, 3, True))
        self.assertTrue(dep["dticket"])
        self.assertFalse(dep["cancelled"])

    def test_ring_line_does_not_repeat_the_destination(self):
        # the sign says "Bismarckplatz", but the tram carries on round the ring
        event = {
            "location": platform("Bstg. A", stop="Heidelberg, Hauptbahnhof", city="Heidelberg"),
            "transportation": {**transport(4), "disassembledName": "5", "destination": {"name": "Heidelberg, Bismarckplatz"}},
            "onwardLocations": [
                onward("Heidelberg, Stadtwerke", [4]),
                onward("Heidelberg, Seegarten", [4, 5]),
                onward("Heidelberg, Bismarckplatz", [4, 5, 6]),
                onward("Heidelberg, Brückenstraße", [4]),
                onward("Weinheim, Hauptbahnhof", [0, 4]),
            ],
        }
        dep = parse.parse_departure(event)
        self.assertEqual(dep["destination"], "Bismarckplatz")
        self.assertEqual(dep["via"], ["Seegarten"])

    def test_delay_counts_full_minutes_only(self):
        def delay(estimated):
            event = {"departureTimePlanned": "2026-10-01T08:00:00Z", "departureTimeEstimated": estimated}
            return parse.parse_departure(event)["delay"]
        self.assertEqual(delay("2026-10-01T08:00:42Z"), 0)   # 42 s late: still "08:00"
        self.assertEqual(delay("2026-10-01T08:01:30Z"), 1)
        self.assertEqual(delay("2026-10-01T08:45:00Z"), 45)
        self.assertEqual(delay("2026-10-01T07:59:30Z"), 0)   # 30 s early
        self.assertEqual(delay("2026-10-01T07:58:00Z"), -2)

    def test_cancelled(self):
        event = {"isCancelled": True, "departureTimePlanned": "2026-10-01T08:00:00Z"}
        dep = parse.parse_departure(event, datetime(2026, 10, 1, 7, 58, tzinfo=timezone.utc))
        self.assertTrue(dep["cancelled"])
        self.assertEqual((dep["time"], dep["delay"], dep["realtime"]), ("2026-10-01T08:00:00Z", None, False))

    def test_missing_fields_do_not_crash(self):
        dep = parse.parse_departure({})
        self.assertEqual(dep["platform"], "Unknown platform")
        self.assertEqual(dep["mode"], "Other")
        self.assertIsNone(dep["time"])
        self.assertIsNone(dep["minutes"])
        self.assertFalse(dep["dticket"])
        self.assertFalse(dep["cancelled"])


class GroupingTest(unittest.TestCase):
    def dep(self, mode, platform_name, minutes, area="Hbf"):
        code = None if platform_name == "Unknown platform" else platform_name.replace("Platform ", "")
        return {"mode": mode, "platform": platform_name, "platform_code": code, "platform_area": area,
                "minutes": minutes, "line": "x"}

    def test_modes_then_platforms_in_order(self):
        modes = group_departures([
            self.dep("Bus", "Platform 2", 4),
            self.dep("U-Bahn / Tram", "Platform 10", 9),
            self.dep("U-Bahn / Tram", "Platform 2", 7),
            self.dep("U-Bahn / Tram", "Platform 2", 1),
            self.dep("U-Bahn / Tram", "Unknown platform", 3),
        ])
        self.assertEqual([m["name"] for m in modes], ["U-Bahn / Tram", "Bus"])
        self.assertEqual([m["id"] for m in modes], ["u_bahn_tram", "bus"])
        tram, bus = modes
        self.assertEqual([p["name"] for p in tram["platforms"]], ["Platform 2", "Platform 10", "Unknown platform"])
        self.assertEqual([p["code"] for p in tram["platforms"]], ["2", "10", None])
        self.assertEqual([p["area"] for p in tram["platforms"]], [None, None, None])
        self.assertEqual([d["minutes"] for d in tram["platforms"][0]["departures"]], [1, 7])
        self.assertFalse(tram["flat"])
        self.assertTrue(bus["flat"])

    def test_same_label_in_different_parts_of_a_station(self):
        modes = group_departures([
            self.dep("Bus", "Platform A", 1, area="Hauptbahnhof Süd"),
            self.dep("Bus", "Platform A", 2, area="Hauptbahnhof (Vorplatz)"),
        ])
        names = [p["name"] for p in modes[0]["platforms"]]
        self.assertEqual(names, ["Platform A · Hauptbahnhof (Vorplatz)", "Platform A · Hauptbahnhof Süd"])
        self.assertEqual([(p["code"], p["area"]) for p in modes[0]["platforms"]],
                         [("A", "Hauptbahnhof (Vorplatz)"), ("A", "Hauptbahnhof Süd")])

    def test_platform_sort_key(self):
        names = ["Platform B", "Unknown platform", "Platform 10", "Platform 2", "Platform A"]
        self.assertEqual(
            sorted(names, key=platform_sort_key),
            ["Platform 2", "Platform 10", "Platform A", "Platform B", "Unknown platform"],
        )




def event(cls, line="RE5", planned="2026-10-01T08:00:00Z", estimated=None, gleis="Gleis 3", dest="Ulm"):
    e = {
        "location": platform(gleis),
        "departureTimePlanned": planned,
        "transportation": {**transport(cls), "disassembledName": line, "destination": {"name": dest}},
    }
    if estimated:
        e["departureTimeEstimated"] = estimated
    return e


class BoardTest(unittest.TestCase):
    """The board with EFA replaced by canned responses (no network)."""

    HBF = "de:08111:6115"
    TIEF = "de:08111:6118"

    def setUp(self):
        from efa import client
        self.client = client
        self.originals = (client.get_station, client.get_departures)
        self.requests = []
        # station id -> product class -> events; classes missing = no departures
        self.events = {self.HBF: {13: [event(13)], 5: [event(5, line="42")]}, self.TIEF: {1: [event(1, line="S1")]}}
        self.stations = {
            self.HBF: {"locations": [{
                "id": self.HBF, "name": "Stuttgart, Hauptbahnhof (oben)", "type": "stop",
                "coord": [48.784729, 9.183172],
                "assignedStops": [
                    # EFA lists S-Bahn (1) here although none stops
                    {"id": self.HBF, "name": "x", "productClasses": [0, 1, 5, 13]},
                    {"id": self.TIEF, "name": "Stuttgart Hauptbahnhof (tief)", "productClasses": [1]},
                ],
            }]},
        }

        # station id -> events EFA only has from this moment on (beyond its 24-hour window)
        self.later = {}

        def get_departures(stop_id, classes=(), limit=100, with_stops=True, ttl=0, start=None):
            self.requests.append((stop_id, tuple(classes), limit))
            if start is not None:
                first, events = self.later.get(stop_id, (None, []))
                found = events if first and start <= first < start + timedelta(hours=24) else []
                return {"stopEvents": found[:limit]} if found else {}
            by_class = self.events.get(stop_id, {})
            found = [e for cls, evs in by_class.items() if not classes or cls in classes for e in evs]
            return {"stopEvents": found[:limit]} if found else {}

        client.get_station = lambda stop_id: self.stations.get(stop_id, {})
        client.get_departures = get_departures

    def tearDown(self):
        self.client.get_station, self.client.get_departures = self.originals

    def board(self, *args):
        from services.departures import board
        return board(*args)

    def test_empty_tabs_are_dropped_and_first_tab_is_default(self):
        result = self.board(self.HBF)
        self.assertEqual([t["id"] for t in result["tabs"]], ["trains", "bus"])
        self.assertEqual(result["tab"], "trains")
        self.assertEqual(result["stop"], {"id": self.HBF, "name": "Stuttgart, Hauptbahnhof (oben)",
                                          "lat": 48.784729, "lon": 9.183172})
        self.assertEqual(result["nearby"], [{"id": self.TIEF, "name": "Stuttgart Hauptbahnhof (tief)",
                                             "lat": None, "lon": None}])
        self.assertEqual(result["modes"][0]["name"], "Regional train")
        self.assertIsNone(result["empty"])
        # the board itself asked only for the train classes, in full
        self.assertIn((self.HBF, (0, 13, 14, 15, 16), 100), self.requests)
        # ...and spent no second request on checking the tab it loaded
        self.assertNotIn((self.HBF, (0, 13, 14, 15, 16), 1), self.requests)

    def test_requested_tab(self):
        result = self.board(self.HBF, "bus")
        self.assertEqual(result["tab"], "bus")
        self.assertEqual(result["modes"][0]["name"], "Bus")

    def test_requested_tab_without_departures_falls_back(self):
        result = self.board(self.HBF, "sbahn")
        self.assertEqual(result["tab"], "trains")
        self.assertEqual(result["modes"][0]["name"], "Regional train")

    def test_unknown_stop(self):
        from services.departures import UnknownStop
        with self.assertRaises(UnknownStop):
            self.board("nonsense")

    def test_stop_outside_baden_wuerttemberg_is_refused(self):
        from services.departures import UnknownStop
        self.stations["x"] = {"locations": [{"id": "de:07315:1234", "name": "Mainz, Somewhere", "type": "stop"}]}
        with self.assertRaises(UnknownStop):
            self.board("x")

    def test_duplicate_trips_are_shown_once(self):
        # timetable entry + the copy EFA creates from live data
        self.events[self.HBF][13] = [
            event(13),
            event(13, estimated="2026-10-01T08:04:00Z"),
            event(13, planned="2026-10-01T09:00:00Z"),
        ]
        rows = self.board(self.HBF)["modes"][0]["platforms"][0]["departures"]
        self.assertEqual([(r["planned"], r["realtime"]) for r in rows],
                         [("2026-10-01T08:00:00Z", True), ("2026-10-01T09:00:00Z", False)])

    def test_same_time_on_another_platform_is_not_a_duplicate(self):
        self.events[self.HBF][13] = [event(13), event(13, gleis="Gleis 4")]
        platforms = self.board(self.HBF)["modes"][0]["platforms"]
        self.assertEqual([p["name"] for p in platforms], ["Platform 3", "Platform 4"])

    def test_flights_and_on_demand_never_show(self):
        self.stations[self.HBF]["locations"][0]["assignedStops"][0]["productClasses"] = [5, 10, 12]
        self.events[self.HBF] = {5: [event(5, line="42")], 10: [event(10, line="RT6")], 12: [event(12, line="DE 1532")]}
        result = self.board(self.HBF)
        self.assertEqual([t["id"] for t in result["tabs"]], ["bus"])
        lines = [r["line"] for m in result["modes"] for p in m["platforms"] for r in p["departures"]]
        self.assertEqual(lines, ["42"])

    def test_unreadable_departure_is_skipped_not_fatal(self):
        broken = event(13, line="RE1")
        broken["transportation"] = "not an object"
        worse = event(13, line="RE2")
        worse["departureTimePlanned"] = 12345
        timeless = event(13, line="RE3")
        del timeless["departureTimePlanned"]
        self.events[self.HBF][13] = [broken, event(13), worse, "junk", None, {}, timeless]
        with self.assertLogs("services.departures", level="WARNING"):
            result = self.board(self.HBF)
        lines = [r["line"] for m in result["modes"] for p in m["platforms"] for r in p["departures"]]
        self.assertEqual(lines, ["RE5"])

    def test_empty_station_points_to_nearby_service(self):
        # like Waldshut Bahnhof: nothing here, replacement buses next door
        self.events[self.HBF] = {}
        result = self.board(self.HBF)
        self.assertEqual(result["empty"], "nearby")
        self.assertEqual(result["modes"], [])
        self.assertEqual(result["tabs"], [])
        self.assertEqual(result["nearby"], [{"id": self.TIEF, "name": "Stuttgart Hauptbahnhof (tief)", "lat": None,
                                             "lon": None, "types": [{"id": "sbahn", "name": "S-Bahn"}]}])

    def test_stop_without_service_today_shows_its_next_service_day(self):
        # like a school-day bus stop on a Saturday: nothing in EFA's 24 hours,
        # but departures three days from now
        self.events[self.HBF] = {}
        in_three_days = datetime.now(timezone.utc) + timedelta(days=3)
        when = in_three_days.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.later[self.HBF] = (in_three_days, [event(5, line="503", planned=when)])
        result = self.board(self.HBF)
        self.assertIsNone(result["empty"])
        self.assertEqual(result["tabs"], [])
        lines = [r["line"] for m in result["modes"] for p in m["platforms"] for r in p["departures"]]
        self.assertEqual(lines, ["503"])
        from efa.localtime import to_local
        self.assertEqual(result["next_service"], to_local(in_three_days).date().isoformat())

    def test_look_ahead_is_only_used_for_empty_boards(self):
        self.later[self.HBF] = (datetime.now(timezone.utc) + timedelta(days=3), [event(5, line="503")])
        result = self.board(self.HBF)
        self.assertIsNone(result["next_service"])
        self.assertEqual(result["tab"], "trains")

    def test_look_ahead_skips_days_with_only_hidden_services(self):
        self.events[self.HBF] = {}
        self.later[self.HBF] = (datetime.now(timezone.utc) + timedelta(days=2), [event(10, line="RT6")])  # on-demand only
        result = self.board(self.HBF)
        self.assertEqual((result["next_service"], result["empty"]), (None, "nearby"))

    def test_empty_station_without_any_service(self):
        # like Ehingen: the timetable has nothing, here or nearby
        self.events = {}
        result = self.board(self.HBF)
        self.assertEqual(result["empty"], "no_service")
        self.assertEqual(result["nearby"], [])
        self.assertIsNone(result["next_service"])


class SearchTest(unittest.TestCase):
    def test_unreadable_match_is_skipped(self):
        from services.stops import parse_points
        good = StopSearchTest.POINT
        with self.assertLogs("services.stops", level="WARNING"):
            stops = parse_points([good, {"anyType": "stop", "ref": "not an object"}, {"anyType": "street"}])
        self.assertEqual([s["id"] for s in stops], ["de:08311:6508"])


class LocalTimeTest(unittest.TestCase):
    def local(self, *utc):
        from efa.localtime import to_local
        return to_local(datetime(*utc, tzinfo=timezone.utc))

    def test_summer_and_winter(self):
        self.assertEqual(self.local(2026, 7, 1, 12, 0), datetime(2026, 7, 1, 14, 0))
        self.assertEqual(self.local(2026, 12, 1, 12, 0), datetime(2026, 12, 1, 13, 0))
        self.assertEqual(self.local(2026, 12, 31, 23, 30), datetime(2027, 1, 1, 0, 30))

    def test_the_clock_changes_at_one_utc_on_the_last_sunday(self):
        # 25 October 2026 and 28 March 2027 are the last Sundays
        self.assertEqual(self.local(2026, 10, 25, 0, 59), datetime(2026, 10, 25, 2, 59))
        self.assertEqual(self.local(2026, 10, 25, 1, 0), datetime(2026, 10, 25, 2, 0))
        self.assertEqual(self.local(2027, 3, 28, 0, 59), datetime(2027, 3, 28, 1, 59))
        self.assertEqual(self.local(2027, 3, 28, 1, 0), datetime(2027, 3, 28, 3, 0))


class ClientRequestTest(unittest.TestCase):
    """What the EFA client asks for (the request itself is replaced)."""

    def setUp(self):
        from efa import client
        self.client = client
        self.original = client.efa_get
        self.asked = []
        client.efa_get = lambda endpoint, params, ttl=0: self.asked.append(params) or {}

    def tearDown(self):
        self.client.efa_get = self.original

    def test_now_by_default(self):
        self.client.get_departures("de:08111:115")
        self.assertNotIn("itdDate", self.asked[0])
        self.assertEqual(self.asked[0]["deleteAssignedStops_dm"], 1)

    def test_start_is_sent_in_german_local_time(self):
        self.client.get_departures("de:08111:115", start=datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc))
        self.client.get_departures("de:08111:115", start=datetime(2026, 12, 1, 23, 30, tzinfo=timezone.utc))
        self.assertEqual((self.asked[0]["itdDate"], self.asked[0]["itdTime"]), ("20261005", "0600"))
        self.assertEqual((self.asked[1]["itdDate"], self.asked[1]["itdTime"]), ("20261202", "0030"))

    def test_classes_and_stop_sequence(self):
        self.client.get_departures("de:08111:115", (1, 13), limit=1, with_stops=False)
        params = self.asked[0]
        self.assertEqual((params["inclMOT_1"], params["inclMOT_13"], params["limit"]), ("on", "on", 1))
        self.assertNotIn("includeCompleteStopSeq", params)


if __name__ == "__main__":
    unittest.main()
