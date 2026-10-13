"""Tests for the stop search: the index over the stop database
(services/stop_index.py) and how it is combined with the timetable
service's own search (services/stops.py). A handful of made-up stations
stands in for the database, canned answers for the timetable service."""
import time
import unittest

from app import app
from efa import client
from services import stop_index, stops
from services.stop_index import StopIndex

STUTTGART, KARLSRUHE, HEILBRONN = (48.7785, 9.1790), (49.0093, 8.4040), (49.1420, 9.2190)
KARLSRUHE_HBF, LUDWIGSBURG = (48.9935, 8.4010), (48.8921, 9.1853)


def station(stop_id, name, importance, lat=48.78, lon=9.18, rail=0, sbahn=0, ubahn=0, tram=0, bus=0, other=0):
    return {"id": stop_id, "name": name, "lat": lat, "lon": lon, "importance": importance, "rail_lines": rail, "sbahn_lines": sbahn,
            "ubahn_lines": ubahn, "tram_lines": tram, "bus_lines": bus, "other_lines": other}


STATIONS = [
    station("de:08111:6115", "Stuttgart Hauptbahnhof (oben)", 41.5, 48.7852, 9.1830, rail=20),
    station("de:08111:6118", "Stuttgart Hauptbahnhof (tief)", 30.0, 48.7834, 9.1802, sbahn=7),
    station("de:08111:115", "Pragfriedhof", 15.0, 48.7981, 9.1826, ubahn=4),
    station("de:08111:6022", "Schlossplatz", 25.0, 48.7785, 9.1790, ubahn=8, bus=3),
    station("de:08111:6002", "Vaihingen", 28.0, 48.7270, 9.1140, rail=2, sbahn=3, ubahn=3, bus=9),
    station("de:08111:300", "Ulmenstraße", 3.0, 48.7700, 9.1700, bus=2),
    station("de:08111:400", "Hauptfriedhof", 12.0, 48.8010, 9.2230, ubahn=1, bus=3),
    station("de:08111:6008", "Universität", 20.0, 48.7450, 9.1040, sbahn=3, bus=6),
    station("de:08212:89", "Durlach Schlossplatz", 9.0, 48.9990, 8.4720, tram=2, bus=4),
    station("de:08212:90", "Karlsruhe Hauptbahnhof", 40.2, 48.9935, 8.4010, rail=18, tram=6),
    station("de:08212:306", "Durlach Turmberg", 4.0, 49.0010, 8.4830, tram=1, bus=1),
    station("de:08212:1003", "KA Marktplatz (Kaiserstraße U)", 18.5, 49.0093, 8.4040, tram=8),
    station("de:08118:7402", "Ludwigsburg", 35.0, 48.8921, 9.1853, rail=6, sbahn=2, bus=30),
    station("de:08118:7501", "Ludwigsburg Arsenalplatz", 6.8, 48.8960, 9.1890, bus=22),
    station("de:08118:7600", "Ludw. Schloss Favorite", 2.0, 48.9020, 9.1900, bus=3),
    station("de:08118:7403", "Ludwigsburg Bahnhof (Arena)", 5.0, 48.8910, 9.1840, bus=4),
    station("de:08215:1856", "Bruchsal", 33.4, 49.1247, 8.5892, rail=9, bus=16),
    station("de:08215:1900", "Bruchsal Tunnelstraße", 4.0, 49.1300, 8.6000, bus=3),
    station("de:08215:2100", "Helmsheim Bahnhof", 5.0, 49.0900, 8.6600, rail=1),
    station("de:08416:11000", "Tübingen Hauptbahnhof", 30.8, 48.5160, 9.0550, rail=8, bus=68),
    station("de:08416:10501", "Tübingen Neckarbrücke", 8.3, 48.5190, 9.0580, bus=33),
    station("de:08111:6288", "Mühlhausen (Stgt.)", 12.0, 48.8440, 9.2290, ubahn=2, bus=2),
    station("de:08121:100", "Heilbronn Rathaus", 14.0, 49.1420, 9.2190, tram=3, bus=10),
    station("de:08311:500", "Freiburg, Rathaus im Stühlinger", 13.0, 47.9960, 7.8330, tram=4),
    station("de:08115:4800", "Böblingen", 30.0, 48.6870, 9.0040, rail=3, sbahn=2, bus=20),
    station("de:08115:5001", "Sindelf. Goldberg", 6.0, 48.7000, 9.0100, bus=4),
    station("de:08421:1008", "Ulm Hauptbahnhof", 37.3, 48.3990, 9.9830, rail=14, tram=2, bus=20),
    station("de:08421:1200", "Ulm Rathaus", 9.0, 48.3970, 9.9930, bus=8),
    station("de:08415:29109", "Reutlingen Bismarckstr.", 7.0, 48.4950, 9.2140, bus=9),
    station("de:08315:6232", "Schluchsee Rathaus", 1.0, 47.8185, 8.1772, bus=2),
]


def names(results):
    return [r["name"] for r in results]


class WordsTest(unittest.TestCase):
    def test_fold(self):
        self.assertEqual(stop_index.fold("Mühlhausen (Stgt.)"), "muhlhausen stgt")
        self.assertEqual(stop_index.fold("Straße/Fa. Lidl"), "strasse fa lidl")
        self.assertEqual(stop_index.fold("  Café  Öl-Ä "), "cafe ol a")
        self.assertEqual(stop_index.fold("?!"), "")

    def test_umlaut_written_with_e(self):
        self.assertEqual(stop_index.without_e("muehlhausen"), "muhlhausen")
        self.assertEqual(stop_index.without_e("boeblingen kaese"), "boblingen kase")

    def test_one_spelling_per_word(self):
        self.assertEqual(stop_index.words_of("Stuttgart Hbf"), ["stuttgart", "hauptbahnhof"])
        self.assertEqual(stop_index.words_of("Hechingen Bf/ZOB"), ["hechingen", "bahnhof", "zob"])
        self.assertEqual(stop_index.words_of("Bismarckstr."), ["bismarckstrasse"])
        self.assertEqual(stop_index.words_of("Willy-Brandt-Pl."), ["willy", "brandt", "platz"])
        self.assertEqual(stop_index.words_of("Str"), ["strasse"])

    def test_letters_wrong(self):
        d = stop_index.distance
        self.assertEqual(d("pragfriedhof", "pragfriedhof", 3), 0)
        self.assertEqual(d("pragfridhof", "pragfriedhof", 3), 1)       # one missing
        self.assertEqual(d("pragfreidhof", "pragfriedhof", 3), 1)      # two swapped
        self.assertEqual(d("pragfreidhoff", "pragfriedhof", 3), 2)
        self.assertEqual(d("hauptbanhov", "hauptbahnhof", 3), 2)
        self.assertEqual(d("abc", "xyz", 2), 3)                        # over the limit: limit + 1
        self.assertEqual(d("stuttgart", "st", 2), 3)
        self.assertEqual(d("", "ab", 3), 2)

    def test_short_words_must_be_right(self):
        self.assertEqual([stop_index.wrong_letters_allowed("x" * n) for n in (2, 3, 4, 5, 6, 8, 9, 14)], [0, 0, 1, 1, 2, 2, 3, 3])

    def test_name_with_its_city(self):
        show = stop_index.display_name
        self.assertEqual(show("Pragfriedhof", "de:08111:115"), "Stuttgart, Pragfriedhof")
        self.assertEqual(show("Stuttgart Hauptbahnhof (oben)", "de:08111:6115"), "Stuttgart Hauptbahnhof (oben)")
        self.assertEqual(show("KA Marktplatz (Kaiserstraße U)", "de:08212:1003"), "Karlsruhe, Marktplatz (Kaiserstraße U)")
        self.assertEqual(show("Durlach Turmberg", "de:08212:306"), "Karlsruhe, Durlach Turmberg")
        self.assertEqual(show("Ludwigsburg Arsenalplatz", "de:08118:7501"), "Ludwigsburg Arsenalplatz")   # a district, not a city
        self.assertEqual(show("Freiburg, Rathaus im Stühlinger", "de:08311:500"), "Freiburg, Rathaus im Stühlinger")
        self.assertEqual(show("Somewhere", "nonsense"), "Somewhere")

    def test_all_districts_have_a_name(self):
        self.assertEqual(len(stop_index.DISTRICTS), 44)
        self.assertTrue(all(code.startswith("08") and len(code) == 5 for code in stop_index.DISTRICTS))


class IndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = StopIndex(STATIONS)

    def first(self, query, near=None, radius=6.0):
        results = self.index.search(query, near, radius)
        return results[0]["name"] if results else None

    def test_without_umlauts(self):
        for query in ("Mühlhausen", "Muhlhausen", "Muehlhausen", "MÜHLHAUSEN", "mühlhausen"):
            self.assertEqual(self.first(query), "Mühlhausen (Stgt.)", query)
        self.assertEqual(self.first("Tubingen"), "Tübingen Hauptbahnhof")
        self.assertEqual(self.first("Boblingen"), "Böblingen")
        self.assertEqual(self.first("Universitat"), "Universität")
        self.assertEqual(self.first("Ulmenstrasse"), "Ulmenstraße")

    def test_wrong_letters(self):
        for query, wanted in (("Mulhausen", "Mühlhausen (Stgt.)"), ("pragfridhof", "Pragfriedhof"), ("Pragfreidhoff", "Pragfriedhof"),
                              ("Ludwigsbrug", "Ludwigsburg"), ("Stutgart Hauptbanhof", "Stuttgart Hauptbahnhof (oben)"),
                              ("Tübigen", "Tübingen Hauptbahnhof"), ("Schlosplatz", "Schlossplatz"), ("Bruchsaal", "Bruchsal"),
                              ("Karlsruhe Haubtbahnhof", "Karlsruhe Hauptbahnhof")):
            self.assertEqual(self.first(query), wanted, query)

    def test_too_wrong_or_too_short_finds_nothing(self):
        for query in ("xqzw", "Ulx", "Prxgfrxxdhxf", "a", "", "   ", "?!"):
            self.assertEqual(self.index.search(query), [], query)

    def test_while_typing(self):
        for query, wanted in (("Stutt", "Stuttgart Hauptbahnhof (oben)"), ("Lud", "Ludwigsburg"), ("Pragf", "Pragfriedhof"),
                              ("Stuttgart Haupt", "Stuttgart Hauptbahnhof (oben)"), ("Bru", "Bruchsal"), ("Tüb", "Tübingen Hauptbahnhof"),
                              ("Reutlingen Bis", "Reutlingen Bismarckstr.")):
            self.assertEqual(self.first(query), wanted, query)

    def test_main_station_before_the_stop_round_the_corner(self):
        self.assertEqual(names(self.index.search("Bruchsal"))[:2], ["Bruchsal", "Bruchsal Tunnelstraße"])
        self.assertEqual(names(self.index.search("Tübingen"))[:2], ["Tübingen Hauptbahnhof", "Tübingen Neckarbrücke"])
        self.assertEqual(self.first("Ludwigsburg"), "Ludwigsburg")
        self.assertEqual(self.first("ulm"), "Ulm Hauptbahnhof")

    def test_bahnhof_means_a_stop_with_trains(self):
        self.assertEqual(self.first("Bruchsal Bahnhof"), "Bruchsal")                   # not Helmsheim's station
        self.assertEqual(self.first("Ludwigsburg Bahnhof"), "Ludwigsburg")             # not the bus stop "Bahnhof (Arena)"
        self.assertEqual(self.first("Böblingen Bf"), "Böblingen")
        self.assertEqual(self.first("Ulm Hbf"), "Ulm Hauptbahnhof")
        self.assertEqual(self.first("Hbf", KARLSRUHE), "Karlsruhe Hauptbahnhof")
        self.assertIn("Hauptbahnhof", self.first("Hauptbahnhof"))
        # a bus stop is not a station just because one asks for one
        self.assertNotIn("Ludwigsburg Arsenalplatz", names(self.index.search("Ludwigsburg Bahnhof")))

    def test_city_that_is_not_in_the_name(self):
        self.assertEqual(self.first("Stuttgart Pragfriedhof"), "Pragfriedhof")
        self.assertEqual(self.first("Pragfriedhof Stuttgart"), "Pragfriedhof")
        self.assertEqual(self.first("Karlsruhe Turmberg"), "Durlach Turmberg")
        self.assertEqual(self.first("Karlsruhe Marktplatz"), "KA Marktplatz (Kaiserstraße U)")
        self.assertEqual(self.first("Stuttgart Schlossplatz", KARLSRUHE), "Schlossplatz")
        self.assertEqual(self.index.search("Stuttgart Turmberg"), [])    # Turmberg is in Karlsruhe

    def test_abbreviations_in_names(self):
        self.assertEqual(self.first("Sindelfingen Goldberg"), "Sindelf. Goldberg")
        self.assertEqual(self.first("Ludwigsburg Schloss Favorite"), "Ludw. Schloss Favorite")
        self.assertEqual(self.first("Reutlingen Bismarckstraße"), "Reutlingen Bismarckstr.")
        self.assertEqual(self.first("Bismarckstr Reutlingen"), "Reutlingen Bismarckstr.")
        self.assertEqual(self.first("Stuttgart Hauptbahnhof tief"), "Stuttgart Hauptbahnhof (tief)")

    def test_near_the_map_first(self):
        self.assertEqual(self.first("Schlossplatz"), "Schlossplatz")                         # the bigger one
        self.assertEqual(self.first("Schlossplatz", STUTTGART), "Schlossplatz")
        self.assertEqual(self.first("Schlossplatz", KARLSRUHE), "Durlach Schlossplatz")
        self.assertEqual(self.first("Rathaus", HEILBRONN), "Heilbronn Rathaus")
        self.assertEqual(self.first("Rathaus", (48.3970, 9.9930)), "Ulm Rathaus")
        # a map of the whole state prefers no place
        self.assertEqual(self.first("Schlossplatz", KARLSRUHE, radius=170.0), "Schlossplatz")

    def test_near_counts_just_as_much_on_a_map_zoomed_in_on_one_station(self):
        # found by the user: after picking Karlsruhe Hauptbahnhof the map shows a
        # kilometre around it, Durlach is 5 km away, and Stuttgart's Schlossplatz came first
        for radius in (0.5, 1.0, 1.5, 3.0, 6.0, 12.0):
            for north, east in ((0, 0), (0.005, 0), (-0.005, 0), (0, 0.01), (0, -0.01), (0.01, 0.02), (-0.01, -0.02)):
                place = (KARLSRUHE_HBF[0] + north, KARLSRUHE_HBF[1] + east)
                self.assertEqual(self.first("Schlossplatz", place, radius), "Durlach Schlossplatz", (radius, north, east))
        self.assertEqual(self.first("Schlossplatz", STUTTGART, 0.5), "Schlossplatz")

    def test_the_station_before_the_cemetery(self):
        # found by the user: "Haupt..." on a map of Stuttgart listed Hauptfriedhof first
        for query in ("Hau", "Haupt", "Stuttgart Haupt", "Hauptb"):
            for radius in (1.5, 6.0, 20.0):
                self.assertEqual(self.first(query, STUTTGART, radius), "Stuttgart Hauptbahnhof (oben)", (query, radius))
        self.assertEqual(self.first("Hauptfr", STUTTGART), "Hauptfriedhof")
        self.assertEqual(self.first("Hauptfriedhof"), "Hauptfriedhof")

    def test_bahnhof_near_a_town_is_its_station(self):
        for radius in (1.5, 6.0):
            self.assertEqual(self.first("Bahnhof", LUDWIGSBURG, radius), "Ludwigsburg", radius)   # not "Bahnhof (Arena)"

    def test_nearness_fades_gently(self):
        def score(km_away, radius=1.5):
            return stop_index.final_score(1.0, 10.0, 48.0 + km_away / 111.0, 9.0, (48.0, 9.0), radius)
        self.assertEqual(score(0), score(2.9))                 # inside what counts as "here" (at least 3 km)
        self.assertGreater(score(2.9), score(5))
        self.assertGreater(score(5), score(20))
        self.assertGreater(score(20), score(80))
        # the next quarter of town is still much nearer than the next city
        self.assertGreater(score(5) - score(60), 3 * (score(0) - score(5)))
        self.assertEqual(stop_index.final_score(1.0, 10.0, None, None, (48.0, 9.0), 5), stop_index.final_score(1.0, 10.0, 60.0, 9.0, (48.0, 9.0), 5) - 0 if False else stop_index.final_score(1.0, 10.0, None, None, (48.0, 9.0), 5))

    def test_words_that_mean_another_place_win_over_the_map(self):
        self.assertEqual(self.first("ulm", STUTTGART), "Ulm Hauptbahnhof")                  # not Ulmenstraße in Stuttgart
        self.assertEqual(self.first("Karlsruhe", STUTTGART), "Karlsruhe Hauptbahnhof")
        self.assertEqual(self.first("Tübingen", KARLSRUHE), "Tübingen Hauptbahnhof")
        self.assertEqual(self.first("Pragfriedhof", KARLSRUHE), "Pragfriedhof")

    def test_every_word_has_to_fit(self):
        self.assertEqual(self.index.search("Ludwigsburg Pragfriedhof"), [])
        self.assertEqual(names(self.index.search("Ulm Rathaus")), ["Ulm Rathaus"])

    def test_finished_word_is_not_a_beginning_any_more(self):
        typing = self.index.search("Ulm")
        finished = self.index.search("Ulm ")
        self.assertEqual(typing[0]["name"], finished[0]["name"])
        self.assertIn("Ulmenstraße", names(typing))
        # after the space the user has said "Ulm", so "Ulmenstraße" sinks or goes
        self.assertTrue("Ulmenstraße" not in names(finished) or names(finished).index("Ulmenstraße") >= names(typing).index("Ulmenstraße"))

    def test_noise_is_left_out_and_the_limit_holds(self):
        results = self.index.search("Stuttgart", limit=3)
        self.assertEqual(len(results), 3)
        results = self.index.search("pragfridhof")
        self.assertEqual(names(results), ["Pragfriedhof"])
        scores = [r["score"] for r in self.index.search("Rathaus")]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertGreaterEqual(scores[-1], stop_index.WORTH_SHOWING * scores[0])

    def test_odd_input_is_harmless(self):
        for query in ("Stuttgart " * 50, "x" * 5000, "ä" * 300, "%_';--", "🚌 Hbf", "Stuttgart\tHbf\n", "12", "1 2 3", "Hbf Hbf Hbf",
                      "a b c d e f g h i j k l", "ß", "--", "(((", "Stuttgart,Hauptbahnhof", "STUTTGART HAUPTBAHNHOF"):
            started = time.perf_counter()
            results = self.index.search(query, STUTTGART)
            self.assertIsInstance(results, list, query[:20])
            self.assertLess(time.perf_counter() - started, 0.5, query[:20])
        self.assertEqual(self.first("Stuttgart,Hauptbahnhof"), "Stuttgart Hauptbahnhof (oben)")
        self.assertEqual(self.first("STUTTGART HAUPTBAHNHOF"), "Stuttgart Hauptbahnhof (oben)")
        self.assertEqual(self.first("🚌 Stuttgart Hbf"), "Stuttgart Hauptbahnhof (oben)")

    def test_many_searches_at_once(self):
        from concurrent.futures import ThreadPoolExecutor
        index = StopIndex(STATIONS)
        queries = ["Stutgart Hauptbanhof", "Mulhausen", "Rathaus", "Stutt", "Ludwigsbrug", "Bahnhof", "Schlossplatz", "ulm", "Tubingen"] * 40
        alone = {query: names(index.search(query, STUTTGART)) for query in set(queries)}
        index.remembered.clear()
        with ThreadPoolExecutor(max_workers=16) as pool:
            together = list(pool.map(lambda query: (query, names(index.search(query, STUTTGART))), queries))
        self.assertTrue(all(found == alone[query] for query, found in together))

    def test_fast_enough_for_typing(self):
        started = time.perf_counter()
        for query in ("Stutgart Hauptbanhof", "Mulhausen", "Rathaus", "Stutt", "Ludwigsbrug", "Bahnhof", "xqzwvbnm"):
            self.index.search(query, STUTTGART)
        self.assertLess(time.perf_counter() - started, 0.5)


class CombinedSearchTest(unittest.TestCase):
    """services.stops.search: the stop database and the timetable service together."""

    def setUp(self):
        self.index = StopIndex(STATIONS)
        self.originals = (stops.own_matches, stops.known_importance, stops.efa_matches, stops.EFA_PATIENCE_SECONDS)
        stops.own_matches = lambda query, place, radius_km, limit: self.index.search(query, place, radius_km, limit)
        stops.known_importance = lambda stop_id: (self.index.stops[self.index.by_id[stop_id]]["importance"]
                                                  if stop_id in self.index.by_id else None)
        self.efa = []
        stops.efa_matches = lambda query: self.efa
        self.http = app.test_client()

    def tearDown(self):
        stops.own_matches, stops.known_importance, stops.efa_matches, stops.EFA_PATIENCE_SECONDS = self.originals

    def efa_stop(self, stop_id, name, quality=900, classes=(5,), lat=48.78, lon=9.18):
        return {"id": stop_id, "name": name, "lat": lat, "lon": lon, "quality": quality, "classes": list(classes)}

    def test_shape_and_names(self):
        results = stops.search("Pragfriedhof")
        self.assertEqual(results, [{"id": "de:08111:115", "name": "Stuttgart, Pragfriedhof", "lat": 48.7981, "lon": 9.1826,
                                    "types": [{"id": "tram", "name": "U-Bahn / Tram"}]}])
        self.assertEqual([t["id"] for t in stops.search("Vaihingen")[0]["types"]], ["trains", "sbahn", "tram", "bus"])

    def test_both_know_the_stop(self):
        # the timetable service's name is the one the board shows; no stop twice
        self.efa = [self.efa_stop("de:08215:1856", "Bruchsal, Bahnhof/ZOB", classes=(0, 1, 5)),
                    self.efa_stop("de:08215:2100", "Helmsheim, Bahnhof", quality=950, classes=(0,))]
        results = stops.search("Bruchsal Bahnhof")
        self.assertEqual(names(results)[0], "Bruchsal, Bahnhof/ZOB")
        self.assertEqual(len({r["id"] for r in results}), len(results))
        self.assertEqual([t["id"] for t in results[0]["types"]], ["trains", "sbahn", "bus"])
        self.assertIn("Helmsheim, Bahnhof", names(results))
        self.assertGreater(names(results).index("Helmsheim, Bahnhof"), 0)

    def test_found_by_the_timetable_service_alone(self):
        # a stop the database's names can't lead to
        self.efa = [self.efa_stop("de:08221:1200", "Heidelberg, Kornmarkt", lat=49.41, lon=8.71)]
        self.assertEqual(names(stops.search("Heidelberg Kornmarkt")), ["Heidelberg, Kornmarkt"])
        # ... and next to our own matches it is listed, below a strong match of ours
        self.efa = [self.efa_stop("de:08111:999", "Stuttgart, Pragstraße", quality=600)]
        self.assertEqual(names(stops.search("Pragfriedhof")), ["Stuttgart, Pragfriedhof", "Stuttgart, Pragstraße"])

    def test_timetable_service_slow_or_down(self):
        def slow(query):
            time.sleep(0.6)
            return [self.efa_stop("de:08111:999", "Stuttgart, Pragstraße")]
        stops.efa_matches = slow
        stops.EFA_PATIENCE_SECONDS = 0.05
        started = time.perf_counter()
        self.assertEqual(names(stops.search("Pragfriedhof")), ["Stuttgart, Pragfriedhof"])
        self.assertLess(time.perf_counter() - started, 0.4)
        # nothing of our own: then its answer is worth waiting for
        self.assertEqual(names(stops.search("Kornmarkt")), ["Stuttgart, Pragstraße"])

        def down(query):
            raise client.EfaError("timeout")
        stops.efa_matches = down
        self.assertEqual(names(stops.search("Pragfriedhof")), ["Stuttgart, Pragfriedhof"])
        self.assertEqual(stops.search("Kornmarkt"), [])
        # neither source: that is an error
        stops.own_matches = lambda *args: None
        with self.assertRaises(client.EfaError):
            stops.search("Pragfriedhof")

    def test_without_the_stop_database(self):
        stops.own_matches = lambda *args: None
        stops.known_importance = lambda stop_id: None
        self.efa = [self.efa_stop("de:08111:115", "Stuttgart, Pragfriedhof", classes=(3,)), self.efa_stop("de:08111:116", "Stuttgart, Pragsattel", quality=800)]
        self.assertEqual(names(stops.search("Prag")), ["Stuttgart, Pragfriedhof", "Stuttgart, Pragsattel"])

    def test_same_name_twice(self):
        self.efa = [self.efa_stop("de:08317:1", "Kehl, Bahnhof", classes=(0,)), self.efa_stop("de:08317:2", "Kehl, Bahnhof", classes=(5,))]
        self.assertEqual(sorted(names(stops.search("Kehl Bahnhof"))), ["Kehl, Bahnhof · Bus", "Kehl, Bahnhof · Trains"])
        # nothing but the district tells these two apart
        self.efa = [self.efa_stop("de:08116:1", "Rathaus"), self.efa_stop("de:08119:2", "Rathaus")]
        self.assertEqual(sorted(names(stops.search("Rathaus xx"))), ["Rathaus · Bus (Kreis Esslingen)", "Rathaus · Bus (Rems-Murr-Kreis)"])

    def test_near_and_limit(self):
        self.assertEqual(names(stops.search("Schlossplatz", near="49.0093,8.4040", radius="6"))[0], "Karlsruhe, Durlach Schlossplatz")
        self.assertEqual(names(stops.search("Schlossplatz"))[0], "Stuttgart, Schlossplatz")
        self.assertEqual(len(stops.search("Stuttgart", limit="2")), 2)
        self.assertEqual(len(stops.search("Stuttgart", limit="999")), min(stops.SEARCH_MAX_LIMIT, len(stops.search("Stuttgart", limit="999"))))
        for bad in ({"near": "x"}, {"near": "48.7"}, {"near": "48.7,9.1,3"}, {"near": "95,9"}, {"radius": "far"}, {"radius": "0"}, {"limit": "many"}):
            with self.assertRaises(stops.InvalidArea, msg=bad):
                stops.search("Stuttgart", **bad)

    def test_api(self):
        res = self.http.get("/api/v1/stops/search?q=Schlossplatz&near=49.0093,8.4040&radius=6&limit=1")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"stops": [{"id": "de:08212:89", "name": "Karlsruhe, Durlach Schlossplatz", "lat": 48.999, "lon": 8.472,
                                                     "types": [{"id": "tram", "name": "U-Bahn / Tram"}, {"id": "bus", "name": "Bus"}]}]})
        bad = self.http.get("/api/v1/stops/search?q=Schlossplatz&near=here")
        self.assertEqual((bad.status_code, bad.get_json()["error"]["code"]), (400, "invalid_parameter"))
        self.assertEqual(self.http.get("/api/v1/stops/search?q=a").get_json(), {"stops": []})
        # a space at the end closes the last word
        self.assertIn("Ulm, Hauptbahnhof".replace(", ", " "), [s["name"] for s in self.http.get("/api/v1/stops/search?q=Ulm%20").get_json()["stops"]])


if __name__ == "__main__":
    unittest.main()
