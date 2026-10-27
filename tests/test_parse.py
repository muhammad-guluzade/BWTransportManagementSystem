"""Tests for the normalisation rules. Inputs mirror real EFA responses
(field names and values as seen via debug_efa.py)."""
import unittest
from datetime import datetime, timedelta, timezone

from efa import parse
from services import departures
from services.departures import group_departures, platform_sort_key


def setUpModule():
    # these tests must give the same result with or without a stop database on
    # the machine; the via stops' use of it is tested with made-up numbers
    global _real_importance
    _real_importance = departures.stop_importance
    departures.stop_importance = lambda: {}


def tearDownModule():
    departures.stop_importance = _real_importance


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


def onward(name, classes, stop_id=None):
    return {"name": name, "type": "platform", "productClasses": classes,
            "parent": {"id": stop_id, "name": name, "type": "stop"}}


ICE_ATTRS = ["HIGHSPEEDTRAIN", "LONG_DISTANCE_TRAINS", "SUPPLEMENT"]
SPECIAL_ATTRS = ["LONG_DISTANCE_TRAINS", "SUPPLEMENT", "DIFFERENT_FARES"]




def in_minutes(minutes: int) -> str:
    """A departure time `minutes` after a fixed moment, as the API writes it."""
    return (datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

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

    def test_tgv_under_its_own_name(self):
        # DB's code for the TGV is "RHT"; riders know it as "TGV 9570" (as EFA's name says too)
        t = {"name": "RHT 9570 TGV", "number": "9570", "product": {"class": 16, "name": "Zug"},
             "properties": {"trainName": "TGV", "trainType": "RHT", "trainNumber": "9570"}}
        self.assertEqual(parse.line_name(t), "TGV 9570")

    def test_fallback(self):
        self.assertEqual(parse.line_name({"name": "Zug"}), "Zug")
        self.assertEqual(parse.line_name({}), "?")


class BrandTest(unittest.TestCase):
    """ICE and Flix are told apart (the page colours them); seen in EFA's answers on 17 October 2026."""

    def t(self, cls, product, operator=None, train_type=None):
        transportation = {"product": {"class": cls, "name": product}}
        if operator:
            transportation["operator"] = {"name": operator}
        if train_type:
            transportation["properties"] = {"trainType": train_type}
        return parse.brand(transportation)

    def test_ice(self):
        self.assertEqual(self.t(16, "Zug", "DB Fernverkehr AG", "ICE"), "ice")
        self.assertEqual(self.t(16, "ICE", "DB Fernverkehr AG", "ICE"), "ice")
        self.assertEqual(self.t(16, "ICE"), "ice")

    def test_flix_trains_from_both_timetables_and_flixbus(self):
        self.assertEqual(self.t(16, "FlixTrain (Sondertarif)", "FlixTrain-de"), "flix")
        self.assertEqual(self.t(0, "Zug", "FlixTrain", "FLX"), "flix")
        self.assertEqual(self.t(7, "Flixbus (Sondertarif)", "FlixBus-de"), "flix")

    def test_everything_else_has_none(self):
        for args in ((13, "R-Bahn", "DB AG", "NRE"), (15, "Zug", "DB Fernverkehr AG", "IC"), (15, "Zug", "DB Fernverkehr AG", "EC"),
                     (16, "Zug", "SNCF", "TGD"), (0, "Zug", "Österreichische Bundesbahnen", "NJ"), (0, "Zug", "WESTbahn", "WB"),
                     (6, "Bus", "SBG"), (17, "Ersatzverkehr", "DB Regio AG Baden-Württemberg", "SEV"), (None, "")):
            self.assertIsNone(self.t(*args), args)
        self.assertIsNone(parse.brand({}))


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

    def test_a_number_written_twice_gives_way_to_the_planned_platform(self):
        # live data of the RE1 (October 2026): the VVS timetable server had the planned platform each time
        def props(name, planned=None, number=None):
            return {"properties": {k: v for k, v in (("platformName", name), ("plannedPlatformName", planned), ("platform", number)) if v is not None}}
        self.assertEqual(parse.platform_code(props("1010", "6", "6")), "6")
        self.assertEqual(parse.platform_code(props("1212", "14", "14")), "14")
        self.assertEqual(parse.platform_code(props("1515", "15", "15")), "15")
        self.assertEqual(parse.platform_code(props("22", "7", "7")), "7")
        self.assertEqual(parse.platform_code(props("Gleis 33", "Gleis 4", "4")), "4")
        # nothing planned: no name on the signs, the timetable service's own number
        self.assertEqual(parse.platform_code(props("22", None, "11")), "11")
        self.assertEqual(parse.signposted_platform(props("22", None, "11")["properties"]), "")
        # left alone: track 11, a real change, a name that is the planned one, other numbers
        self.assertEqual(parse.platform_code(props("11", "1", "1")), "11")
        self.assertEqual(parse.platform_code(props("1111", "1111", "1111")), "1111")
        self.assertEqual(parse.platform_code(props("5", "8", "8")), "5")
        self.assertEqual(parse.platform_code(props("101", "102", "102")), "101")
        self.assertEqual(parse.platform_code(props("1210", "12", "12")), "1210")
        self.assertEqual(parse.platform_code(props("4 Nord", "4", "4")), "4 Nord")
        self.assertEqual(parse.platform_code(props("A", "B", "B")), "A")

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

    # --- ranking by the stop database -----------------------------------------

    # Pragfriedhof towards Gerlingen: importance as in the stop database (the ids are made up)
    U6 = [("Löwentorbrücke", "de:08111:6114", 15.5), ("Pragsattel", "de:08111:35", 19.0),
          ("Maybachstraße", "de:08111:153", 6.1), ("Feuerbach Bf", "de:08111:6157", 23.3),
          ("Wilhelm-Geiger-Platz", "de:08111:155", 7.0), ("Föhrich", "de:08111:156", 4.2)]

    def line(self, cls, destination, stops, attributes=None):
        """A departure of product class `cls`; stops are (name, id, classes)."""
        return {"location": platform("Gleis 1"),
                "transportation": {**transport(cls, attributes=attributes), "destination": {"name": destination}},
                "onwardLocations": [onward(name, classes, stop_id) for name, stop_id, classes in stops]}

    def test_database_importance_decides(self):
        stops = [(name, stop_id, [3]) for name, stop_id, _ in self.U6] + [("Gerlingen", "de:08115:7100", [3, 5])]
        event = self.line(3, "Gerlingen", stops)
        importance = {stop_id: value for _, stop_id, value in self.U6}
        self.assertEqual(parse.via_stops(event, importance), ["Pragsattel", "Feuerbach Bf"])
        # other numbers, other stops: nothing about the names is built in
        importance["de:08111:6114"], importance["de:08111:156"] = 30, 25
        self.assertEqual(parse.via_stops(event, importance), ["Löwentorbrücke", "Föhrich"])
        # and it reaches the departure row
        self.assertEqual(parse.parse_departure(event, importance=importance)["via"], ["Löwentorbrücke", "Föhrich"])

    def test_city_lines_do_not_name_two_neighbours(self):
        stops = [("A", "a", [3]), ("B", "b", [3]), ("C", "c", [3]), ("D", "d", [3]), ("E", "e", [3]), ("Z", "z", [3])]
        importance = {"a": 10, "b": 9, "c": 1, "d": 5, "e": 1}
        for cls in (1, 3, 5):  # S-Bahn, tram, bus
            self.assertEqual(parse.via_stops(self.line(cls, "Z", stops), importance), ["A", "D"], cls)
        # the neighbour is named only if there is nothing else
        short = [("A", "a", [3]), ("B", "b", [3]), ("C", "c", [3]), ("Z", "z", [3])]
        self.assertEqual(parse.via_stops(self.line(3, "Z", short), {"a": 1, "b": 9, "c": 5}), ["B", "C"])

    def test_trains_name_their_two_most_important_stops(self):
        stops = [("A", "a", [0]), ("B", "b", [0]), ("C", "c", [0]), ("D", "d", [0]), ("E", "e", [0]), ("Z", "z", [0])]
        importance = {"a": 10, "b": 9, "c": 1, "d": 5, "e": 1}
        self.assertEqual(parse.via_stops(self.line(13, "Z", stops), importance), ["A", "B"])   # regional train
        self.assertEqual(parse.via_stops(self.line(16, "Z", stops), importance), ["A", "B"])   # ICE
        flixbus = self.line(7, "Z", stops, attributes=["LONG_DISTANCE_TRAINS"])
        self.assertEqual(parse.via_stops(flixbus, importance), ["A", "B"])

    def test_stops_outside_the_database_are_estimated(self):
        # ICE from Stuttgart: Mannheim is in the database, the rest is outside Baden-Württemberg
        stops = [("Vaihingen (Enz)", "de:08118:5911", [0, 5, 13]),
                 ("Mannheim, Hauptbahnhof", "de:08222:2417", [0, 1, 4, 5, 6, 7, 13]),
                 ("Frankfurt (Main) Flughafen Fernbahnhof", "de:06412:7010", [0, 5]),
                 ("Frankfurt (Main) Hauptbahnhof", "de:06412:10", [0, 1, 2, 4, 5, 6, 13]),
                 ("Fulda", "de:06631:1", [0, 5, 13]),
                 ("Hamburg Hbf", "de:02000:10950", [0, 1, 2, 5, 13, 16])]
        importance = {"de:08118:5911": 11.0, "de:08222:2417": 47.8}
        event = self.line(16, "Hamburg Hbf", stops)
        self.assertEqual(parse.via_stops(event, importance), ["Mannheim, Hauptbahnhof", "Frankfurt (Main) Hauptbahnhof"])
        # Frankfurt beats a small station that the database knows
        del stops[1]
        self.assertEqual(parse.via_stops(self.line(16, "Hamburg Hbf", stops), importance),
                         ["Vaihingen (Enz)", "Frankfurt (Main) Hauptbahnhof"])

    def test_estimate_grows_with_the_old_score(self):
        village = onward("Dorf, Kirche", [5])
        town = onward("Fulda", [0, 5, 13])
        city = onward("Frankfurt (Main) Hauptbahnhof", [0, 1, 2, 4, 5, 6, 13])
        self.assertEqual((parse.stop_score(village), parse.stop_score(town), parse.stop_score(city)), (1, 6, 13))
        values = [parse.estimated_importance(stop) for stop in (village, town, city)]
        self.assertEqual(values, sorted(values))
        self.assertAlmostEqual(values[0], 1.385)
        self.assertAlmostEqual(values[2], 32.465)

    def test_without_a_database_the_estimate_ranks_everything(self):
        stops = [("Stadtbibliothek", None, [3, 5]), ("Hauptbf (A.-Klett-Pl.)", None, [0, 3, 5, 6, 11]),
                 ("Schlossplatz", None, [3, 5, 11]), ("Charlottenplatz", None, [3, 5, 6, 11]), ("Olgaeck", None, [3, 5]),
                 ("Flughafen/Messe", None, [1, 3, 5])]
        event = self.line(3, "Flughafen/Messe", stops)
        for importance in (None, {}):
            self.assertEqual(parse.via_stops(event, importance), ["Hauptbf (A.-Klett-Pl.)", "Charlottenplatz"])

    def test_one_stop_on_the_way_is_named(self):
        stops = [("Reutlingen Hauptbahnhof", "de:08415:28300", [0, 5, 13]), ("Tübingen Hauptbahnhof", "de:08416:10808", [0, 5, 13])]
        self.assertEqual(parse.via_stops(self.line(13, "Tübingen Hauptbahnhof", stops)), ["Reutlingen Hauptbahnhof"])
        self.assertEqual(parse.via_stops(self.line(13, "Tübingen Hauptbahnhof", stops[1:])), [])

    def test_nothing_from_beyond_the_signed_destination(self):
        # at Charlottenplatz the U4 "Rathaus" carries on after Rathaus
        stops = [("Stuttgart, Rathaus", "r", [3]), ("Stuttgart, Österreichischer Platz", "o", [3, 5]), ("Stuttgart, Marienplatz", "m", [3, 5, 8])]
        importance = {"r": 12, "o": 14, "m": 20}
        self.assertEqual(parse.via_stops(self.line(3, "Rathaus", stops), importance), [])
        self.assertEqual(parse.via_stops(self.line(3, "Stuttgart, Rathaus", stops), importance), [])
        # two stops further it is the same destination, now with something on the way
        further = [("Stuttgart, Schlossplatz", "s", [3]), ("Stuttgart, Hauptbahnhof", "h", [3])] + stops
        self.assertEqual(parse.via_stops(self.line(3, "Rathaus", further), importance), ["Schlossplatz", "Hauptbahnhof"])

    def test_sign_in_other_words_means_the_last_stop(self):
        # Ulm's line 1 is signed "Böfingen" and ends at "Ostpreußenweg"
        stops = [("Ulm Theater", "t", [4]), ("Ulm Ostpreußenweg", "o", [4])]
        self.assertEqual(parse.via_stops(self.line(4, "Böfingen", stops), {"t": 5, "o": 9}), ["Ulm Theater"])

    def test_trains_do_not_stop_at_the_border(self):
        stops = [("Appenweier", "a", [0, 13]), ("Kehl Grenze", "g", [0, 5, 13]), ("Strasbourg Gare Centrale", "s", [0]),
                 ("Paris Gare de l'Est", "p", [0])]
        importance = {"a": 3.0, "g": 5.7}
        self.assertEqual(parse.via_stops(self.line(16, "Paris Gare de l'Est", stops), importance),
                         ["Appenweier", "Strasbourg Gare Centrale"])
        # a bus stop called that is a real stop
        bus = [("Konstanz, Hauptzoll", "a", [5]), ("Kreuzlingen, Grenze", "g", [5]), ("Kreuzlingen, Bahnhof", "s", [5, 0])]
        self.assertIn("Kreuzlingen, Grenze", parse.via_stops(self.line(5, "Kreuzlingen, Bahnhof", bus), {"a": 1, "g": 9}))

    def test_points_a_long_distance_service_only_passes(self):
        def station(name, stop_id, arrival, departure, classes=(7,)):
            return {"id": stop_id, "name": name, "type": "stop", "productClasses": list(classes),
                    "arrivalTimePlanned": arrival, "departureTimePlanned": departure,
                    "parent": {"name": name.split(",")[0], "type": "locality"}}
        # a coach from Mannheim to Prague drives past a tram depot on its way to Heidelberg's station
        stops = [station("Heidelberg, Betriebshof", "de:08221:1144", "2026-10-08T03:44:00Z", "2026-10-08T03:44:00Z", (4, 5, 6, 7)),
                 station("Heidelberg, Hauptbahnhof", "de:08221:1160", "2026-10-08T03:45:00Z", "2026-10-08T03:50:00Z", (0, 1, 4, 5, 13)),
                 station("Neckarsulm, Bahnhof West", "de:08125:1621", "2026-10-08T04:45:00Z", "2026-10-08T04:50:00Z"),
                 station("Praha Florenc", "cz:1", "2026-10-08T10:45:00Z", None)]
        importance = {"de:08221:1144": 21.0, "de:08221:1160": 44.6, "de:08125:1621": 4.0}
        coach = {"location": platform("Gleis 1"), "onwardLocations": stops,
                 "transportation": {**transport(7, attributes=["LONG_DISTANCE_TRAINS"]), "destination": {"name": "Praha Florenc"}}}
        self.assertEqual(parse.via_stops(coach, importance), ["Heidelberg, Hauptbahnhof", "Neckarsulm, Bahnhof West"])
        self.assertEqual([s["name"] for s in parse.parse_trip(coach)["onward"]],
                         ["Heidelberg, Hauptbahnhof", "Neckarsulm, Bahnhof West", "Praha Florenc"])
        # a city bus's stops look just like that point, and they are stops
        city_bus = {**coach, "transportation": {**transport(5), "destination": {"name": "Praha Florenc"}}}
        self.assertIn("Heidelberg, Betriebshof", parse.via_stops(city_bus, importance))
        self.assertEqual(len(parse.parse_trip(city_bus)["onward"]), 4)
        # with a platform it is a stop, however short the halt
        with_platform = [{**stops[0], "type": "platform"}] + stops[1:]
        self.assertIn("Heidelberg, Betriebshof", parse.via_stops({**coach, "onwardLocations": with_platform}, importance))

    def test_station_without_platforms_keeps_its_own_name(self):
        # long-distance stops often come as the station itself, with the town
        # as its parent; the town's name alone ("Freiburg im Breisgau") is not the stop
        def station(name, town, classes):
            return {"name": name, "type": "stop", "productClasses": classes, "parent": {"name": town, "type": "locality"}}
        event = {"location": platform("Gleis 1"), "onwardLocations": [
            station("Mannheim, Hauptbahnhof", "Mannheim", [0, 5, 13]),
            station("Heidelberg, Post Waypoint", "Heidelberg", [7]),
            station("Emmerich (Gr)", "Emmerich", [0, 13]),
            station("Frankfurt (Main) Hbf", "Frankfurt am Main", [0, 13]),
            station("Dortmund Hbf", "Dortmund", [0, 13]),
        ]}
        self.assertEqual(parse.via_stops(event), ["Mannheim, Hauptbahnhof", "Frankfurt (Main) Hbf"])


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


class IdShapeTest(unittest.TestCase):
    def test_station_ids(self):
        for good in ("de:08111:6115", "de:08118:nvbw_bus_11198", "de:08221:1144"):
            self.assertTrue(parse.is_station_id(good), good)
        for bad in ("", "..", "Stuttgart Hbf", "de:09162:100", "de:08111", "de:08111:", "de:08111:6115:1:2",
                    "de:0811:6115", "de:08111:61 15", "de:08111:../x", "x" * 5000, "de:08111:" + "1" * 41):
            self.assertFalse(parse.is_station_id(bad), bad)

    def test_platform_ids(self):
        stop = "de:08111:6115"
        for good in (stop, stop + ":1:2", stop + ":0:Hp. 2", stop + ":2:RiN/O", stop + ":0:>Berw"):
            self.assertTrue(parse.is_platform_id_of(good, stop), good)
        for bad in ("de:08111:6116:1:1", "de:08111:61150", stop + ":", stop + ":../../etc", stop + ":a\\b",
                    stop + ":1\x00", stop + ":" + "1" * 41):
            self.assertFalse(parse.is_platform_id_of(bad, stop), bad)


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
        # the two stops before Bismarckplatz, and nothing from beyond it
        self.assertEqual(dep["via"], ["Stadtwerke", "Seegarten"])

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


class TripTest(unittest.TestCase):
    """A departure's id, and the stops of its trip."""

    def stop(self, stop_id, name, number="", kind="platform", **times):
        location = {"id": f"{stop_id}:1:1", "name": name, "type": "platform", "coord": [48.8, 9.2],
                    "properties": {"platform": number} if number else {},
                    "parent": {"id": stop_id, "name": name, "type": "stop"}, **times}
        if kind == "stop":  # the station itself, its parent is the town
            location.update(id=stop_id, type="stop", parent={"name": name.split(",")[0], "type": "locality"})
        return location

    def event(self, **changes):
        event = {
            "location": {**platform("Gleis 2"), "id": "de:08111:115:1:2", "coord": [48.80, 9.18]},
            "departureTimePlanned": "2026-10-01T08:00:00Z",
            "departureTimeEstimated": "2026-10-01T08:03:00Z",
            "transportation": {**transport(3, "Stadtbahn", tripCode=77), "id": "vvs:20006: :H:j26",
                               "disassembledName": "U6", "destination": {"name": "Gerlingen"}},
            "previousLocations": [
                self.stop("de:08111:6112", "Stuttgart, Hauptbf (A.-Klett-Pl.)", "2",
                          arrivalTimePlanned="2026-10-01T07:55:00Z", departureTimePlanned="2026-10-01T07:56:00Z"),
            ],
            "onwardLocations": [
                self.stop("de:08111:6114", "Stuttgart, Löwentorbrücke", "2", arrivalTimePlanned="2026-10-01T08:01:00Z",
                          arrivalTimeEstimated="2026-10-01T08:04:00Z", departureTimePlanned="2026-10-01T08:02:00Z"),
                self.stop("de:08111:6114", "Stuttgart, Löwentorbrücke", "2", arrivalTimePlanned="2026-10-01T08:01:00Z"),
                self.stop("de:08111:35", "Stuttgart, Pragsattel", arrivalTimePlanned="2026-10-01T08:03:00Z"),
                self.stop("de:08115:7100", "Gerlingen", "1", departureTimePlanned="2026-10-01T08:25:00Z"),
            ],
        }
        event["location"]["parent"]["id"] = "de:08111:115"
        event.update(changes)
        return event

    def test_id_is_stable_and_tells_departures_apart(self):
        event = self.event()
        departure_id = parse.departure_id(event)
        self.assertEqual(departure_id, parse.departure_id(self.event()))
        self.assertRegex(departure_id, r"^[A-Za-z0-9_-]+$")
        # a live time arriving later must not change the id
        self.assertEqual(departure_id, parse.departure_id(self.event(departureTimeEstimated="2026-10-01T08:09:00Z")))
        later = self.event(departureTimePlanned="2026-10-01T08:10:00Z")
        other_platform = self.event(location={**event["location"], "id": "de:08111:115:1:1"})
        other_trip = self.event(transportation={**event["transportation"], "properties": {"tripCode": 78}})
        other_line = self.event(transportation={**event["transportation"], "id": "vvs:20007: :H:j26"})
        ids = {parse.departure_id(e) for e in (event, later, other_platform, other_trip, other_line)}
        self.assertEqual(len(ids), 5)
        self.assertEqual(parse.parse_departure(event)["id"], departure_id)

    def test_id_can_be_read_back(self):
        wanted = parse.decode_departure_id(parse.departure_id(self.event()))
        self.assertEqual(wanted, {"planned": datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc),
                                  "platform_id": "de:08111:115:1:2", "line_id": "vvs:20006: :H:j26", "trip": "77"})

    def test_a_station_level_id_finds_the_ride_at_any_platform(self):
        # a journey's ride whose platform the planner doesn't know yet (only the
        # station's id): the same time, line and trip on the board is that ride
        from services.departures import same_ride
        board = self.event()
        board_id = parse.departure_id(board)
        station_only = parse.departure_id({**board, "location": {**board["location"], "id": "de:08111:115"}})
        wanted = parse.decode_departure_id(station_only)
        self.assertTrue(same_ride(board_id, station_only, wanted))
        later = parse.departure_id({**board, "departureTimePlanned": "2026-10-01T08:10:00Z"})
        self.assertFalse(same_ride(later, station_only, wanted))
        other_station = parse.departure_id({**board, "location": {**board["location"], "id": "de:08111:6115:1:2"}})
        self.assertFalse(same_ride(other_station, station_only, wanted))
        # ... and an id naming another platform of the same station (the
        # planner's 3:3 where the board has 3:2, or a train moved): the same ride
        exact = parse.decode_departure_id(board_id)
        other_platform = parse.departure_id({**board, "location": {**board["location"], "id": "de:08111:115:1:1"}})
        self.assertTrue(same_ride(other_platform, board_id, exact))
        # another trip number at the same time and line is another ride
        other_trip = parse.departure_id({**board, "transportation": {**board["transportation"], "properties": {"tripCode": 78}}})
        self.assertFalse(same_ride(other_trip, board_id, exact))

    def test_anything_else_is_not_an_id(self):
        for junk in ("", "nonsense", "!!!", "ä", "YXxi", "MjAyNi0xMC0wMVQwODowMDowMFp8fHw", "x" * 500):
            self.assertIsNone(parse.decode_departure_id(junk), junk)

    def test_stops_ahead_show_the_arrival(self):
        stops = parse.parse_trip(self.event())["onward"]
        self.assertEqual([s["name"] for s in stops], ["Löwentorbrücke", "Pragsattel", "Gerlingen"])  # no repeat
        self.assertEqual(stops[0], {
            "id": "de:08111:6114", "name": "Löwentorbrücke", "lat": 48.8, "lon": 9.2, "has_board": True,
            "platform": {"code": "2", "name": "Platform 2"},
            "time": "2026-10-01T08:04:00Z", "planned": "2026-10-01T08:01:00Z", "delay": 3, "realtime": True,
            "cancelled": False,
        })
        # no platform, no live time
        self.assertEqual(stops[1]["platform"], {"code": None, "name": "Unknown platform"})
        self.assertEqual((stops[1]["time"], stops[1]["delay"], stops[1]["realtime"]), ("2026-10-01T08:03:00Z", None, False))
        # only a departure time is known: better than none
        self.assertEqual(stops[2]["time"], "2026-10-01T08:25:00Z")

    def test_stops_behind_show_the_departure(self):
        stops = parse.parse_trip(self.event())["previous"]
        self.assertEqual([(s["name"], s["time"]) for s in stops], [("Hauptbf (A.-Klett-Pl.)", "2026-10-01T07:56:00Z")])

    def test_this_station(self):
        trip = parse.parse_trip(self.event())
        self.assertEqual(list(trip), ["id", "line", "destination", "here", "previous", "onward"])
        self.assertEqual((trip["line"], trip["destination"]), ("U6", "Gerlingen"))
        here = trip["here"]
        self.assertEqual((here["id"], here["name"], here["platform"]["code"]), ("de:08111:115", "Pragfriedhof", "2"))
        self.assertEqual((here["time"], here["planned"], here["delay"]), ("2026-10-01T08:03:00Z", "2026-10-01T08:00:00Z", 3))
        self.assertFalse(here["cancelled"])
        self.assertTrue(parse.parse_trip(self.event(isCancelled=True))["here"]["cancelled"])

    def test_ring_line_does_not_list_this_station_again(self):
        round_trip = self.event()
        round_trip["onwardLocations"].append(self.stop("de:08111:115", "Stuttgart, Pragfriedhof", "2"))
        self.assertNotIn("Pragfriedhof", [s["name"] for s in parse.parse_trip(round_trip)["onward"]])

    def test_stops_outside_the_state_and_points_that_are_no_stops(self):
        event = self.event(onwardLocations=[
            self.stop("de:08222:2417", "Mannheim, Hauptbahnhof", kind="stop", arrivalTimePlanned="2026-10-01T09:00:00Z"),
            self.stop("de:08221:11991", "Heidelberg, Post Waypoint", kind="stop"),
            self.stop("de:05154:31503", "Emmerich, Emmerich (Gr)", kind="stop"),
            self.stop("de:06412:10", "Frankfurt (Main) Hauptbahnhof", arrivalTimePlanned="2026-10-01T09:40:00Z"),
            self.stop("80020798", "Amsterdam Centraal", kind="stop", arrivalTimePlanned="2026-10-01T13:00:00Z"),
        ])
        stops = parse.parse_trip(event)["onward"]
        self.assertEqual([(s["id"], s["name"], s["has_board"]) for s in stops], [
            ("de:08222:2417", "Mannheim, Hauptbahnhof", True),   # the station, not just "Mannheim"
            ("de:06412:10", "Frankfurt (Main) Hauptbahnhof", False),
            ("80020798", "Amsterdam Centraal", False),
        ])

    def test_a_trip_without_stop_lists(self):
        trip = parse.parse_trip(self.event(previousLocations=None, onwardLocations={}))
        self.assertEqual((trip["previous"], trip["onward"]), ([], []))


class PlatformPlaceTest(unittest.TestCase):
    """Which departures leave from the same physical platform, where it is and
    which way its services go."""

    HERE = (48.494959, 9.210220)

    def dep(self, pid, at=None, label="Platform 1", named=False, moved=False, towards="Stadtmitte", minutes=5, mode="Bus",
            area="Hbf/Listplatz"):
        lat, lon = at or (None, None)
        return {"id": f"{pid}-{minutes}", "mode": mode, "platform": label, "platform_code": label.replace("Platform ", ""),
                "platform_area": area, "platform_id": pid, "platform_lat": lat, "platform_lon": lon,
                "platform_named": named, "platform_moved": moved, "next_stop": towards, "minutes": minutes, "time": in_minutes(minutes),
                "line": "1"}

    def north(self, metres):
        return (self.HERE[0] + metres / 111_000, self.HERE[1])

    def platforms(self, deps):
        modes = group_departures(deps)
        self.assertEqual(len(modes), 1)
        return modes[0]["platforms"]

    def test_two_bays_with_the_same_internal_number(self):
        # Reutlingen Hbf/Listplatz: both bays are "1" to the timetable service, 17 m apart
        deps = ([self.dep("de:1:s:1:1", self.HERE, towards="Hbf/U.d.Linden", minutes=m) for m in (1, 2, 3)]
                + [self.dep("de:1:s:1:1", self.HERE, towards="Stadtmitte", minutes=m) for m in (4, 5)]
                + [self.dep("de:1:s:2:1", self.north(17), towards="Bismarckstr.", minutes=m) for m in (6, 7, 8)]
                + [self.dep("de:1:s:2:1", self.north(17), towards="Stadtmitte", minutes=9)])
        first, second = self.platforms(deps)
        self.assertEqual((first["name"], first["id"], first["towards"]),
                         ("Platform 1 · towards Hbf/U.d.Linden or Stadtmitte", "de:1:s:1:1", ["Hbf/U.d.Linden", "Stadtmitte"]))
        self.assertEqual((second["name"], second["id"], second["towards"]),
                         ("Platform 1 · towards Bismarckstr. or Stadtmitte", "de:1:s:2:1", ["Bismarckstr.", "Stadtmitte"]))
        self.assertEqual((first["lat"], first["lon"]), self.HERE)
        self.assertEqual((second["lat"], second["lon"]), self.north(17))
        self.assertEqual((len(first["departures"]), len(second["departures"])), (5, 4))
        self.assertEqual((first["code"], first["area"], second["code"], second["area"]), ("1", None, "1", None))
        # nothing of the bookkeeping reaches the rows
        self.assertEqual(set(first["departures"][0]), {"id", "minutes", "time", "line"})

    def test_a_departure_with_only_the_stations_id_joins_its_platform(self):
        # Wiesloch-Walldorf: one S2 came with the station's id, 8.5 m from platform 2 itself
        def deps():
            return ([self.dep("de:1:s:3:2", self.HERE, "Platform 2", towards="Rot-Malsch", minutes=m, mode="S-Bahn") for m in (1, 2, 3)]
                    + [self.dep("de:1:s", self.north(8.5), "Platform 2", towards="Rot-Malsch", minutes=4, mode="S-Bahn")])
        (only,) = self.platforms(deps())
        self.assertEqual((only["name"], only["id"], len(only["departures"])), ("Platform 2", "de:1:s:3:2", 4))
        # ... while two real bays with one number stay apart, and it joins the nearer one
        two = self.platforms(deps() + [self.dep("de:1:s:4:2", self.north(40), "Platform 2", towards="Wiesloch", minutes=5, mode="S-Bahn")])
        self.assertEqual(sorted((p["id"], len(p["departures"])) for p in two), [("de:1:s:3:2", 4), ("de:1:s:4:2", 1)])

    def test_a_signposted_platform_is_one_platform_whatever_the_ids(self):
        # Ulm Hbf "7 Süd" comes under two ids 20 m apart; Karlsruhe's tunnel stops are in the data twice
        deps = [self.dep("de:1:s:34:7", self.HERE, "Platform 7 Süd", named=True, minutes=m, mode="Regional train") for m in (1, 2, 3)]
        deps += [self.dep("de:1:s:33:4", self.north(20), "Platform 7 Süd", named=True, minutes=9, mode="Regional train")]
        (only,) = self.platforms(deps)
        self.assertEqual((only["name"], only["id"], len(only["departures"])), ("Platform 7 Süd", "de:1:s:34:7", 4))
        self.assertEqual((only["lat"], only["lon"]), self.HERE)

    def test_a_train_moved_here_does_not_decide_where_the_platform_is(self):
        # Aalen: platform 5 has its own trains (id ...:5) and one moved over from platform 4 (id ...:4)
        own = [self.dep("de:1:s:4:5", self.north(9), "Platform 5", named=True, minutes=20, mode="Regional train")]
        moved = [self.dep("de:1:s:4:4", self.HERE, "Platform 5", named=True, moved=True, minutes=m, mode="Regional train") for m in (1, 2)]
        (only,) = self.platforms(moved + own)
        self.assertEqual((only["id"], only["lat"], only["lon"], len(only["departures"])), ("de:1:s:4:5", *self.north(9), 3))
        # only moved trains: better their planned platform than no position at all
        (only,) = self.platforms([self.dep("de:1:s:4:4", self.HERE, "Platform 5", named=True, moved=True, mode="Regional train")])
        self.assertEqual(only["id"], "de:1:s:4:4")

    def test_the_same_spot_is_the_same_platform(self):
        # Zwingenberg: one S-Bahn departure came without the signposted name, from the very same place
        deps = [self.dep("de:1:s:2:2", self.HERE, "Platform 2", named=True, minutes=m, mode="S-Bahn") for m in (1, 2, 3)]
        deps += [self.dep("de:1:s:9:2", self.north(4), "Platform 2", minutes=4, mode="S-Bahn")]
        (only,) = self.platforms(deps)
        self.assertEqual((only["name"], only["id"], len(only["departures"])), ("Platform 2", "de:1:s:2:2", 4))
        # two unnamed ids five metres apart, too
        deps = [self.dep("de:1:s:1:1", self.HERE, minutes=1), self.dep("de:1:s:1:1", self.HERE, minutes=2), self.dep("de:1:s:2:1", self.north(5), minutes=3)]
        (only,) = self.platforms(deps)
        self.assertEqual((only["name"], only["id"]), ("Platform 1", "de:1:s:1:1"))
        # ... and six metres apart they are two
        deps = [self.dep("de:1:s:1:1", self.HERE, minutes=1), self.dep("de:1:s:2:1", self.north(6), minutes=3)]
        self.assertEqual(len(self.platforms(deps)), 2)

    def test_without_id_or_position_nothing_is_split(self):
        deps = [self.dep(None, None, minutes=1), self.dep("de:1:s:1:1", None, minutes=2), self.dep("de:1:s:2:1", None, minutes=3)]
        (only,) = self.platforms(deps)
        self.assertEqual((only["name"], only["lat"], only["lon"], len(only["departures"])), ("Platform 1", None, None, 3))
        self.assertEqual(only["id"], "de:1:s:1:1")
        (only,) = self.platforms([self.dep(None, None)])
        self.assertEqual((only["id"], only["lat"], only["lon"]), (None, None, None))

    def test_parts_of_a_station_are_still_told_apart_by_their_name(self):
        deps = [self.dep("de:1:s:1:1", self.HERE, "Platform A", area="Hauptbahnhof", minutes=1),
                self.dep("de:1:s:5:1", self.north(200), "Platform A", area="Hauptbahnhof Süd", minutes=2)]
        first, second = self.platforms(deps)
        self.assertEqual((first["name"], first["area"]), ("Platform A · Hauptbahnhof", "Hauptbahnhof"))
        self.assertEqual((second["name"], second["area"]), ("Platform A · Hauptbahnhof Süd", "Hauptbahnhof Süd"))

    def test_towards(self):
        from services.departures import towards
        rows = [{"next_stop": name} for name in ["A"] * 6 + ["B"] * 3 + ["C"] * 2 + [None]]
        self.assertEqual(towards(rows), ["A", "B"])           # C is under a quarter
        self.assertEqual(towards([{"next_stop": "B"}, {"next_stop": "A"}]), ["A", "B"])  # equals in alphabetical order
        self.assertEqual(towards([{"next_stop": None}, {}]), [])
        # no direction known: the twins simply keep the same name, the map tells them apart
        deps = [self.dep("de:1:s:1:1", self.HERE, towards=None, minutes=1), self.dep("de:1:s:2:1", self.north(17), towards=None, minutes=2)]
        self.assertEqual([p["name"] for p in self.platforms(deps)], ["Platform 1", "Platform 1"])
        # one platform alone needs no direction in its name, but says it in `towards`
        (only,) = self.platforms([self.dep("de:1:s:1:1", self.HERE, towards="Stadtmitte")])
        self.assertEqual((only["name"], only["towards"]), ("Platform 1", ["Stadtmitte"]))

    def test_what_a_departure_knows_about_its_platform(self):
        def event(properties, onward_stops=("Stuttgart, Löwentorbrücke", "Gerlingen"), cls=3):
            return {"location": {**platform(), "id": "de:08111:115:1:2", "coord": [48.80, 9.18], "properties": properties},
                    "departureTimePlanned": "2026-10-01T08:00:00Z",
                    "transportation": {**transport(cls), "disassembledName": "U6", "destination": {"name": "Gerlingen"}},
                    "onwardLocations": [onward(name, [3]) for name in onward_stops]}
        dep = parse.parse_departure(event({"platform": "2", "platformName": "Gleis 2", "plannedPlatformName": "Gleis 2"}))
        self.assertEqual((dep["platform_id"], dep["platform_lat"], dep["platform_lon"]), ("de:08111:115:1:2", 48.80, 9.18))
        self.assertEqual((dep["platform_named"], dep["platform_moved"], dep["next_stop"]), (True, False, "Löwentorbrücke"))
        # only the timetable service's own number
        dep = parse.parse_departure(event({"platform": "2"}))
        self.assertEqual((dep["platform"], dep["platform_named"], dep["platform_moved"]), ("Platform 2", False, False))
        # moved to another platform today
        dep = parse.parse_departure(event({"platform": "4", "platformName": "5", "plannedPlatformName": "4"}))
        self.assertEqual((dep["platform"], dep["platform_named"], dep["platform_moved"]), ("Platform 5", True, True))
        # a number written twice (the RE1's live data): the planned platform, not moved
        dep = parse.parse_departure(event({"platform": "6", "platformName": "1010", "plannedPlatformName": "6"}))
        self.assertEqual((dep["platform"], dep["platform_code"], dep["platform_named"], dep["platform_moved"]), ("Platform 6", "6", True, False))
        dep = parse.parse_departure(event({"platform": "11", "platformName": "22"}))
        self.assertEqual((dep["platform"], dep["platform_named"], dep["platform_moved"]), ("Platform 11", False, False))
        # the end of the line, and a list that starts with this station again
        self.assertIsNone(parse.parse_departure(event({"platform": "2"}, onward_stops=()))["next_stop"])
        self.assertEqual(parse.next_stop(event({}, onward_stops=("Pragfriedhof", "Heidelberg, Post Waypoint", "Killesberg"))), "Killesberg")


class SecondTimetableTest(unittest.TestCase):
    """Near the Swiss border EFA also has the Swiss timetable's copy of a train."""

    def test_line_from_the_number_when_the_label_is_missing(self):
        swiss = {"number": "S14", "name": "S-Bahn S14", "product": {"class": 1}, "properties": {"trainType": "S", "trainNumber": "23811"}}
        self.assertEqual(parse.line_name(swiss), "S14")          # not "S 23811"
        bus = {"number": "908", "name": "Bus 908", "properties": {"trainType": "B", "trainNumber": "90801"}}
        self.assertEqual(parse.line_name(bus), "908")
        # long-distance trains: the number IS the train number
        ice = {"number": "101", "name": "ICE 101 InterCityExpress", "properties": {"trainType": "ICE", "trainNumber": "101"}}
        self.assertEqual(parse.line_name(ice), "ICE 101")
        self.assertEqual(parse.line_name({"number": "ICE 1291", "properties": {"trainType": "ICE", "trainNumber": 1291}}), "ICE 1291")
        self.assertEqual(parse.line_name({"number": "19", "name": "Regionalbus 19"}), "19")
        self.assertEqual(parse.line_name({"disassembledName": "S27", "number": "S27", "properties": {"trainType": "S", "trainNumber": "20723"}}), "S27")
        self.assertEqual(parse.line_name({}), "?")

    def dep(self, line="S27", platform="5", planned="2026-10-17T03:35:00Z", destination="Baden", realtime=False, mode="S-Bahn"):
        return {"mode": mode, "platform": f"Platform {platform}" if platform else parse.UNKNOWN_PLATFORM, "platform_code": platform,
                "platform_area": "Bahnhof", "line": line, "destination": destination, "planned": planned, "realtime": realtime}

    def test_the_copy_without_a_platform_goes(self):
        from services.departures import dedupe
        own, copy = self.dep(), self.dep(platform=None)
        self.assertEqual(dedupe([copy, own]), [own])
        self.assertEqual(dedupe([own, copy]), [own])
        # even if only the copy has live data: the platform is worth more to someone at the station
        self.assertEqual(dedupe([own, self.dep(platform=None, realtime=True)]), [own])

    def test_the_same_copy_whatever_the_order(self):
        # EFA sends some trips twice with different trip numbers, in an order that
        # changes between requests; the board must keep the same one (its id)
        from services.departures import dedupe
        first, second = {**self.dep(), "id": "A"}, {**self.dep(), "id": "B"}
        self.assertEqual(dedupe([first, second]), [first])
        self.assertEqual(dedupe([second, first]), [first])
        # ... unless only one has live data
        live = {**self.dep(realtime=True), "id": "C"}
        self.assertEqual(dedupe([first, live]), [live])
        self.assertEqual(dedupe([live, second]), [live])

    def test_the_numberless_copy_of_a_numbered_train_goes(self):
        # the French timetable's Paris trains: no number ("Zug"), another platform
        from services.departures import dedupe
        ice = self.dep(line="ICE 9578", platform="4", destination="Paris Est", mode="Long-distance train")
        copy = self.dep(line="Zug", platform="10", destination="Paris Est", mode="Regional train")
        self.assertEqual(dedupe([copy, ice]), [ice])
        # a train whose label has no number, without a numbered twin, stays ("SAB" to Gammertingen)
        sab = self.dep(line="SAB", platform="5a", destination="Gammertingen Bahnhof", mode="Regional train")
        self.assertEqual(dedupe([sab, ice]), [sab, ice])
        # ... and so does a numberless one to another place or at another time
        other_time = {**copy, "planned": "2026-10-17T05:00:00Z"}
        self.assertEqual(dedupe([other_time, ice]), [other_time, ice])

    def test_the_numberless_copy_with_the_destination_spelt_otherwise_goes(self):
        # Stuttgart Hbf, TGV 9577: DB says "München Hbf", the French timetable "München
        # Hauptbahnhof"; both trips end at the same station
        from services.departures import dedupe
        tgv = {**self.dep(line="TGV 9577", platform="16", destination="München Hbf", mode="Long-distance train"), "last_station": "de:09162:100"}
        copy = {**self.dep(line="Zug", platform="10", destination="München Hauptbahnhof", mode="Regional train"), "last_station": "de:09162:100"}
        self.assertEqual(dedupe([copy, tgv]), [tgv])
        # another last station and another name: another train
        elsewhere = {**copy, "destination": "Ulm Hbf", "last_station": "de:08421:1008"}
        self.assertEqual(dedupe([elsewhere, tgv]), [elsewhere, tgv])
        # no stop after it (last station unknown): only the name can make it a copy
        unknown = {**copy, "last_station": None}
        self.assertEqual(dedupe([unknown, {**tgv, "last_station": None}]), [unknown, {**tgv, "last_station": None}])

    def test_the_numberless_copy_to_another_destination_goes_and_gives_its_longer_route(self):
        # Stuttgart Hbf, 23 October 2026: DB's TGV 9574 "to Strasbourg" (platform 4), the
        # French copy "to Paris Est" (platform 10), same minute, both on to Karlsruhe first;
        # DB Navigator: TGV 9574 to Paris Est from platform 4 (the user's choice B)
        from services import departures
        departures._trips.clear()
        stop = lambda sid, name: {"id": sid, "name": name}
        karlsruhe, strasbourg, paris = stop("de:08212:90", "Karlsruhe Hbf"), stop("fr:1", "Strasbourg"), stop("fr:2", "Paris Est")
        tgv = {**self.dep(line="TGV 9574", platform="4", destination="Strasbourg", mode="Long-distance train"),
               "id": "tgv", "next_stop": "Karlsruhe Hbf", "via": ["Karlsruhe Hbf"], "last_station": "fr:1", "destination_full": "Strasbourg"}
        copy = {**self.dep(line="Zug", platform="10", destination="Paris Est", mode="Regional train"),
                "id": "zug", "next_stop": "Karlsruhe Hbf", "via": ["Karlsruhe Hbf", "Strasbourg"], "last_station": "fr:2",
                "destination_full": "Paris Est"}
        departures.remember_trip({"id": "tgv", "line": "TGV 9574", "destination": "Strasbourg", "here": {"id": "de:08111:6115"},
                                  "previous": [], "onward": [karlsruhe, strasbourg]})
        departures.remember_trip({"id": "zug", "line": "Zug", "destination": "Paris Est", "here": {"id": "de:08111:6115"},
                                  "previous": [], "onward": [karlsruhe, strasbourg, paris]})
        try:
            shown = departures.dedupe([copy, dict(tgv)])
            self.assertEqual([(d["line"], d["platform"], d["destination"], d["via"]) for d in shown],
                             [("TGV 9574", "Platform 4", "Paris Est", ["Karlsruhe Hbf", "Strasbourg"])])
            self.assertEqual(departures.trip("de:08111:6115", "tgv")["onward"], [karlsruhe, strasbourg, paris])
            # a copy whose route is not the same one going further: DB's row stays as it is
            departures.remember_trip({"id": "zug", "line": "Zug", "destination": "Paris Est", "here": {"id": "de:08111:6115"},
                                      "previous": [], "onward": [karlsruhe, paris]})
            departures.remember_trip({"id": "tgv", "line": "TGV 9574", "destination": "Strasbourg", "here": {"id": "de:08111:6115"},
                                      "previous": [], "onward": [karlsruhe, strasbourg]})
            shown = departures.dedupe([copy, dict(tgv)])
            self.assertEqual([(d["line"], d["destination"]) for d in shown], [("TGV 9574", "Strasbourg")])
            # the same minute but another way out: another train, both stay
            other = {**copy, "next_stop": "Ludwigsburg", "last_station": "de:08118:7402", "destination": "Heilbronn"}
            self.assertEqual(len(departures.dedupe([other, dict(tgv)])), 2)
        finally:
            departures._trips.clear()

    def test_a_train_ending_here_is_no_departure(self):
        from services.departures import departing, ends_here
        station = {"id": "de:08111:6115", "name": "Stuttgart, Hauptbahnhof"}
        ends = {**self.dep(line="RE90", destination="Hauptbahnhof", mode="Regional train"), "last_station": None,
                "destination_full": "Stuttgart Hauptbahnhof", "id": "a"}
        self.assertTrue(ends_here(ends, station))
        self.assertTrue(ends_here(ends, {**station, "name": "Stuttgart Hauptbahnhof (oben)"}))
        # stops after it: it goes on (a ring line's sign can name this station)
        self.assertFalse(ends_here({**ends, "last_station": "de:08111:6008"}, station))
        # EFA left out the stops but the sign names another place: still shown
        self.assertFalse(ends_here({**ends, "destination_full": "Aalen, Hauptbahnhof"}, station))
        going = {**self.dep(line="RE1", destination="Aalen, Hauptbahnhof", mode="Regional train"), "last_station": "de:08136:1000",
                 "destination_full": "Aalen, Hauptbahnhof", "id": "b"}
        self.assertEqual(departing([ends, going], station), [going])

    def test_what_is_not_a_copy_stays(self):
        from services.departures import dedupe
        alone = self.dep(platform=None)
        self.assertEqual(dedupe([alone]), [alone])                       # no platform known at all: still a departure
        for other in (self.dep(line="S36"), self.dep(destination="Basel"), self.dep(planned="2026-10-17T04:05:00Z"), self.dep(mode="Bus")):
            self.assertEqual(len(dedupe([alone, other])), 2, other)
        # two platforms at the same minute are two departures
        self.assertEqual(len(dedupe([self.dep(), self.dep(platform="6")])), 2)

    def test_replacement_bus(self):
        sev = {"number": "SEV", "disassembledName": "SEV", "product": {"class": 13, "name": "Ersatzverkehr"},
               "properties": {"trainName": "Schienenersatzverkehr", "trainType": "BSV", "trainNumber": "80080"}}
        self.assertTrue(parse.is_replacement(sev))
        self.assertTrue(parse.is_replacement({"disassembledName": "SEV S41", "product": {"class": 17, "name": "Bus"}}))
        self.assertTrue(parse.is_replacement({"disassembledName": "701", "product": {"class": 5, "name": "Schienenersatzverkehr"}}))
        for normal in ({"disassembledName": "RE5", "product": {"class": 13, "name": "R-Bahn"}}, {"disassembledName": "Seven", "product": {"class": 5}},
                       {"disassembledName": "42", "product": {"class": 5, "name": "Bus"}}, {}):
            self.assertFalse(parse.is_replacement(normal), normal)
        event = {"location": platform("Bstg 1"), "departureTimePlanned": "2026-10-01T08:00:00Z", "transportation": {**sev, "destination": {"name": "Koblenz"}}}
        self.assertIs(parse.parse_departure(event)["replacement"], True)


class TabOfTest(unittest.TestCase):
    def test_tab_of_a_service(self):
        for cls, tab in ((5, "bus"), (7, "bus"), (17, "bus"), (0, "trains"), (16, "trains"), (1, "sbahn"), (3, "tram"), (9, "ferry"),
                         (99, "other"), (None, "other")):
            self.assertEqual(parse.tab_of({"product": {"class": cls}}), tab, cls)
        self.assertEqual(parse.tab_of({}), "other")


class GroupingTest(unittest.TestCase):
    def dep(self, mode, platform_name, minutes, area="Hbf"):
        code = None if platform_name == "Unknown platform" else platform_name.replace("Platform ", "")
        return {"mode": mode, "platform": platform_name, "platform_code": code, "platform_area": area,
                "minutes": minutes, "time": in_minutes(minutes), "line": "x"}

    def test_same_minute_in_the_order_of_the_seconds(self):
        early = {**self.dep("Bus", "Platform 1", 7), "time": "2026-10-14T08:52:00Z", "minutes": 7}
        late = {**self.dep("Bus", "Platform 1", 7), "time": "2026-10-14T08:52:06Z", "minutes": 7}
        departures = group_departures([late, early])[0]["platforms"][0]["departures"]
        self.assertEqual([d["time"] for d in departures], ["2026-10-14T08:52:00Z", "2026-10-14T08:52:06Z"])

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
        # until then: the station next door has departures now, and what kind
        # (like Waldshut, whose trains are replaced by buses from the bus station)
        self.assertEqual(result["nearby"], [{"id": self.TIEF, "name": "Stuttgart Hauptbahnhof (tief)", "lat": None, "lon": None,
                                             "types": [{"id": "sbahn", "name": "S-Bahn"}]}])
        # a neighbour without departures is not somewhere to send people
        self.events[self.TIEF] = {}
        self.assertEqual(self.board(self.HBF)["nearby"], [])

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
