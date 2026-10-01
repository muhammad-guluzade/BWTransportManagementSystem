"""Tests for the normalisation rules. Inputs mirror real EFA responses
(field names and values as seen via debug_efa.py)."""
import unittest
from datetime import datetime, timezone

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
        "ref": {"id": "6906508", "gid": "de:08311:6508"},
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
            "assignedStops": [
                {"id": "de:08111:6115", "name": "Stuttgart Hauptbahnhof (oben)", "productClasses": [0, 1, 13, 16]},
                {"id": "de:08111:6118", "name": "Stuttgart Hauptbahnhof (tief)", "productClasses": [1]},
                {"id": "de:08111:6112", "name": "Hauptbf (Arnulf-Klett-Platz)", "productClasses": [0, 3, 5, 6, 11]},
            ],
        }],
    }

    def test_station_and_nearby(self):
        station = parse.parse_station(self.RESPONSE)
        self.assertEqual(station["id"], "de:08111:6115")
        self.assertEqual(station["name"], "Stuttgart, Hauptbahnhof (oben)")
        self.assertEqual(station["classes"], [0, 1, 13, 16])
        self.assertEqual(station["nearby"], [
            {"id": "de:08111:6118", "name": "Stuttgart Hauptbahnhof (tief)"},
            {"id": "de:08111:6112", "name": "Hauptbf (Arnulf-Klett-Platz)"},
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
        self.assertEqual(dep["direction"], "Gerlingen")
        self.assertEqual(dep["time"], "2026-10-01T08:03:00Z")
        self.assertEqual(dep["planned"], "2026-10-01T08:00:00Z")
        self.assertEqual((dep["minutes"], dep["delay"], dep["realtime"]), (5, 3, True))
        self.assertTrue(dep["dticket"])
        self.assertFalse(dep["cancelled"])

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
        return {"mode": mode, "platform": platform_name, "platform_area": area, "minutes": minutes, "line": "x"}

    def test_modes_then_platforms_in_order(self):
        modes = group_departures([
            self.dep("Bus", "Platform 2", 4),
            self.dep("U-Bahn / Tram", "Platform 10", 9),
            self.dep("U-Bahn / Tram", "Platform 2", 7),
            self.dep("U-Bahn / Tram", "Platform 2", 1),
            self.dep("U-Bahn / Tram", "Unknown platform", 3),
        ])
        self.assertEqual([m["name"] for m in modes], ["U-Bahn / Tram", "Bus"])
        tram, bus = modes
        self.assertEqual([p["name"] for p in tram["platforms"]], ["Platform 2", "Platform 10", "Unknown platform"])
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

    def test_platform_sort_key(self):
        names = ["Platform B", "Unknown platform", "Platform 10", "Platform 2", "Platform A"]
        self.assertEqual(
            sorted(names, key=platform_sort_key),
            ["Platform 2", "Platform 10", "Platform A", "Platform B", "Unknown platform"],
        )




class BoardTest(unittest.TestCase):
    """The board with EFA replaced by canned responses (no network)."""

    def setUp(self):
        from efa import client
        self.client = client
        self.originals = (client.get_station, client.get_departures)
        self.requests = []

        def get_departures(stop_id, classes=(), limit=100, with_stops=True, ttl=0):
            self.requests.append((tuple(classes), limit))
            # this station has trains and buses, but no S-Bahn despite EFA listing it
            if 1 in classes:
                return {}
            cls = 13 if 13 in classes else 5
            return {"stopEvents": [{
                "location": platform("Gleis 3"),
                "departureTimePlanned": "2026-10-01T08:00:00Z",
                "transportation": {**transport(cls), "disassembledName": "RE5", "destination": {"name": "Ulm"}},
            }]}

        client.get_station = lambda stop_id: {"locations": [{
            "id": "de:08111:6115", "name": "Stuttgart, Hauptbahnhof (oben)", "type": "stop",
            "assignedStops": [
                {"id": "de:08111:6115", "name": "x", "productClasses": [0, 1, 5, 13]},
                {"id": "de:08111:6118", "name": "Stuttgart Hauptbahnhof (tief)", "productClasses": [1]},
            ],
        }]}
        client.get_departures = get_departures

    def tearDown(self):
        self.client.get_station, self.client.get_departures = self.originals

    def test_empty_tabs_are_dropped_and_first_tab_is_default(self):
        from services.departures import board
        result = board("de:08111:6115")
        self.assertEqual([t["id"] for t in result["tabs"]], ["trains", "bus"])
        self.assertEqual(result["tab"], "trains")
        self.assertEqual(result["stop"], {"id": "de:08111:6115", "name": "Stuttgart, Hauptbahnhof (oben)"})
        self.assertEqual(result["nearby"], [{"id": "de:08111:6118", "name": "Stuttgart Hauptbahnhof (tief)"}])
        self.assertEqual(result["modes"][0]["name"], "Regional train")
        # the board itself asked only for the train classes
        self.assertEqual(self.requests[-1], ((0, 13, 14, 15, 16), 100))

    def test_requested_tab(self):
        from services.departures import board
        result = board("de:08111:6115", "bus")
        self.assertEqual(result["tab"], "bus")
        self.assertEqual(result["modes"][0]["name"], "Bus")

    def test_unknown_stop(self):
        from services.departures import UnknownStop, board
        self.client.get_station = lambda stop_id: {}
        with self.assertRaises(UnknownStop):
            board("nonsense")

    def test_stop_outside_baden_wuerttemberg_is_refused(self):
        from services.departures import UnknownStop, board
        self.client.get_station = lambda stop_id: {"locations": [{
            "id": "de:07315:1234", "name": "Mainz, Somewhere", "type": "stop", "assignedStops": [],
        }]}
        with self.assertRaises(UnknownStop):
            board("nonsense")


if __name__ == "__main__":
    unittest.main()
