"""Tests for the JSON API (api/v1.py): addresses, response shapes, errors.

These pin down what docs/API.md promises. A future client (e.g. a mobile
app) depends on these field names, so a failing test here means the API
changed in a way that would break it. EFA is replaced by canned responses,
so the tests need no network."""
import unittest

from datetime import datetime, timezone

from app import app
from efa import client, parse
from services import departures, stops

STOP = "de:08111:6115"
NEIGHBOUR = "de:08111:6118"

STATION = {"locations": [{
    "id": STOP, "name": "Stuttgart, Hauptbahnhof (oben)", "type": "stop", "coord": [48.784729, 9.183172],
    "assignedStops": [
        {"id": STOP, "name": "Stuttgart Hauptbahnhof (oben)", "productClasses": [13, 16]},
        {"id": NEIGHBOUR, "name": "Stuttgart Hauptbahnhof (tief)", "productClasses": [1], "coord": [48.783385, 9.180225]},
    ],
}]}

def platform_of(stop_id, name, number, coord=None, **times):
    """A platform of a station the way EFA lists the stops of a trip."""
    return {"id": f"{stop_id}:1:{number}", "name": name, "type": "platform", "coord": coord,
            "properties": {"platform": number, "platformName": f"Gleis {number}"},
            "parent": {"id": stop_id, "name": name, "type": "stop"}, **times}


EVENT = {
    "location": {
        "id": STOP + ":3:3", "name": "Stuttgart Hauptbahnhof (oben)", "type": "platform",
        "coord": [48.784729, 9.183172], "properties": {"platformName": "Gleis 3"},
        "parent": {"id": STOP, "name": "Stuttgart Hauptbahnhof (oben)", "type": "stop",
                   "parent": {"name": "Stuttgart", "type": "locality"}},
    },
    "departureTimePlanned": "2026-10-01T08:00:00Z",
    "departureTimeEstimated": "2026-10-01T08:04:00Z",
    "transportation": {
        "id": "ddb:90T05: :H:j26", "disassembledName": "RE5", "product": {"class": 13, "name": "R-Bahn"},
        "destination": {"name": "Ulm Hauptbahnhof"}, "properties": {"tripCode": 19005},
    },
    "previousLocations": [
        {**platform_of("de:08115:5774", "Böblingen", "2", [48.687, 9.004], departureTimePlanned="2026-10-01T07:38:00Z"),
         "productClasses": [0, 1, 5]},
    ],
    "onwardLocations": [
        {**platform_of("de:08116:7800", "Plochingen", "4", [48.713, 9.411], arrivalTimePlanned="2026-10-01T08:15:00Z",
                       arrivalTimeEstimated="2026-10-01T08:18:00Z", departureTimePlanned="2026-10-01T08:16:00Z"),
         "productClasses": [0, 1, 5]},
        {**platform_of("de:08117:5000", "Göppingen", "1", [48.699, 9.652], arrivalTimePlanned="2026-10-01T08:30:00Z"),
         "productClasses": [5]},
        {**platform_of("de:08421:1008", "Ulm Hauptbahnhof", "2", [48.399, 9.983], arrivalTimePlanned="2026-10-01T09:02:00Z"),
         "productClasses": [0, 5]},
    ],
}

def setUpModule():
    # these tests must give the same result with or without a stop database on
    # the machine; the via stops' and the search's use of it are tested with
    # made-up data (the search in tests/test_search.py)
    global _real_importance, _real_own_matches, _real_known_importance
    _real_importance = departures.stop_importance
    departures.stop_importance = lambda: {}
    _real_own_matches, _real_known_importance = stops.own_matches, stops.known_importance
    stops.own_matches = lambda query, place, radius_km, limit: None
    stops.known_importance = lambda stop_id: None


def tearDownModule():
    departures.stop_importance = _real_importance
    stops.own_matches, stops.known_importance = _real_own_matches, _real_known_importance


SEARCH = {"stopFinder": {"points": [
    {"anyType": "stop", "name": "Stuttgart, Hauptbahnhof", "quality": "1000", "modes": "0,13,16",
     "ref": {"gid": STOP, "coords": "9.183172,48.784729"}},
    {"anyType": "stop", "name": "München, Hbf", "quality": "990", "modes": "0", "ref": {"gid": "de:09162:100"}},
    {"anyType": "street", "name": "Bahnhofstraße"},
]}}


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.originals = (client.get_station, client.get_departures, client.find_stops)
        self.station = STATION
        self.events = [EVENT]
        client.get_station = lambda stop_id: self.station if stop_id == STOP else {}
        client.get_departures = lambda stop_id, classes=(), **kw: (
            {"stopEvents": self.events} if stop_id == STOP and (not classes or 13 in classes) else {})
        client.find_stops = lambda query: SEARCH
        self.http = app.test_client()

    def tearDown(self):
        client.get_station, client.get_departures, client.find_stops = self.originals


class StationCacheTest(unittest.TestCase):
    """A station's description is remembered for hours, so only if it is the
    station asked for (16 October 2026: EFA, in trouble, answered with other stops)."""

    def setUp(self):
        self.real_get = client._session.get
        client._cache.clear()
        self.answers = []
        self.asked = 0

        class Answer:
            def __init__(self, data):
                self.data = data
                self.encoding = None

            def raise_for_status(self):
                pass

            def json(self):
                return self.data

        def fake_get(url, params=None, timeout=None):
            self.asked += 1
            return Answer(self.answers.pop(0))
        client._session.get = fake_get

    def tearDown(self):
        client._session.get = self.real_get
        client._cache.clear()

    def test_a_wrong_station_is_not_remembered(self):
        wrong = {"locations": [{"id": "de:08125:921", "name": "Untergruppenbach, MAGNA", "type": "stop"}]}
        right = {"locations": [{"id": STOP, "name": "Stuttgart, Hauptbahnhof", "type": "stop"}]}
        self.answers = [wrong, right]
        self.assertEqual(client.get_station(STOP), wrong)     # returned (the board then says: unknown stop) ...
        self.assertEqual(client.get_station(STOP), right)     # ... but not remembered: EFA is asked again
        self.assertEqual(client.get_station(STOP), right)     # the right one is remembered
        self.assertEqual(self.asked, 2)


class TrainsTogetherTest(ApiTestCase):
    """?trains=together: a platform's ICEs among its regional trains (the page asks for it)."""

    def setUp(self):
        super().setUp()
        ice = {**EVENT, "departureTimePlanned": "2026-10-01T07:55:00Z", "departureTimeEstimated": "2026-10-01T07:55:00Z",
               "transportation": {**EVENT["transportation"], "id": "ddb:ICE512: :H:j26", "disassembledName": "ICE 512",
                                  "product": {"class": 16, "name": "ICE"}, "properties": {"tripCode": 512}}}
        self.events = [EVENT, ice]

    def modes(self, query=""):
        body = self.http.get(f"/api/v1/stops/{STOP}/departures{query}").get_json()
        return [(m["id"], m["name"], [(p["name"], [d["line"] for d in p["departures"]]) for p in m["platforms"]]) for m in body["modes"]]

    def test_two_modes_unless_asked(self):
        self.assertEqual(self.modes(), [("regional_train", "Regional train", [("Platform 3", ["RE5"])]),
                                        ("long_distance_train", "Long-distance train", [("Platform 3", ["ICE 512"])])])

    def test_together_one_platform_in_time_order(self):
        self.assertEqual(self.modes("?trains=together"), [("trains", "Trains", [("Platform 3", ["ICE 512", "RE5"])])])
        self.assertEqual(self.modes("?trains=Together"), self.modes("?trains=together"))
        self.assertEqual(self.modes("?trains=apart"), self.modes())


class IndexTest(ApiTestCase):
    def test_index_lists_the_endpoints(self):
        body = self.http.get("/api/v1/").get_json()
        self.assertEqual(body["version"], 1)
        self.assertEqual(set(body["endpoints"]), {"search_stops", "stops_in_area", "departures", "platform_departures", "departure_stops"})


class SearchTest(ApiTestCase):
    def test_shape(self):
        res = self.http.get("/api/v1/stops/search?q=Stuttgart")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"stops": [{
            "id": STOP,
            "name": "Stuttgart, Hauptbahnhof",
            "lat": 48.784729,
            "lon": 9.183172,
            "types": [{"id": "trains", "name": "Trains"}],
            "partial": False,
        }]})

    def test_only_stops_in_baden_wuerttemberg(self):
        ids = [s["id"] for s in self.http.get("/api/v1/stops/search?q=Hbf").get_json()["stops"]]
        self.assertEqual(ids, [STOP])

    def test_short_or_missing_query_gives_an_empty_list(self):
        for url in ("/api/v1/stops/search", "/api/v1/stops/search?q=", "/api/v1/stops/search?q=a"):
            res = self.http.get(url)
            self.assertEqual((res.status_code, res.get_json()), (200, {"stops": []}))


class DeparturesTest(ApiTestCase):
    def get(self, query=""):
        return self.http.get(f"/api/v1/stops/{STOP}/departures{query}")

    def test_top_level_shape(self):
        res = self.get()
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(list(body), ["stop", "nearby", "tabs", "tab", "modes", "empty", "next_service", "platform"])
        self.assertIsNone(body["next_service"])
        self.assertIsNone(body["platform"])   # the whole stop, not one platform of it
        self.assertEqual(body["stop"], {"id": STOP, "name": "Stuttgart, Hauptbahnhof (oben)", "lat": 48.784729, "lon": 9.183172})
        self.assertEqual(body["nearby"], [{"id": NEIGHBOUR, "name": "Stuttgart Hauptbahnhof (tief)", "lat": 48.783385, "lon": 9.180225}])
        self.assertEqual(body["tabs"], [{"id": "trains", "name": "Trains"}])
        self.assertEqual(body["tab"], "trains")
        self.assertIsNone(body["empty"])

    def test_mode_platform_and_departure_shape(self):
        mode = self.get().get_json()["modes"][0]
        self.assertEqual(list(mode), ["id", "name", "flat", "platforms"])
        self.assertEqual((mode["id"], mode["name"], mode["flat"]), ("regional_train", "Regional train", True))

        platform = mode["platforms"][0]
        self.assertEqual(list(platform), ["code", "name", "area", "id", "lat", "lon", "towards", "departures"])
        self.assertEqual((platform["code"], platform["name"], platform["area"]), ("3", "Platform 3", None))
        # the platform as a place: its own id, where it is, where its trains go next
        self.assertEqual((platform["id"], platform["lat"], platform["lon"]), (STOP + ":3:3", 48.784729, 9.183172))
        self.assertEqual(platform["towards"], ["Plochingen"])

        departure = platform["departures"][0]
        self.assertEqual(list(departure), ["id", "line", "destination", "via", "dticket", "replacement", "brand", "time", "planned",
                                           "minutes", "delay", "realtime", "cancelled"])
        minutes = departure.pop("minutes")  # depends on the clock
        self.assertIsInstance(minutes, int)
        # the id goes into an address, so it must need no escaping
        self.assertRegex(departure.pop("id"), r"^[A-Za-z0-9_-]+$")
        self.assertEqual(departure, {
            "line": "RE5",
            "destination": "Ulm Hauptbahnhof",
            "via": ["Plochingen", "Göppingen"],
            "dticket": True,
            "replacement": False,
            "brand": None,
            "time": "2026-10-01T08:04:00Z",
            "planned": "2026-10-01T08:00:00Z",
            "delay": 4,
            "realtime": True,
            "cancelled": False,
        })

    def test_via_stops_are_ranked_with_the_stop_database(self):
        # a third stop on the way, so that two have to be chosen
        geislingen = {**platform_of("de:08117:2000", "Geislingen (Steige)", "2"), "productClasses": [0, 5]}
        self.events = [{**EVENT, "onwardLocations": EVENT["onwardLocations"][:2] + [geislingen] + EVENT["onwardLocations"][2:]}]
        original = departures.stop_importance
        try:
            departures.stop_importance = lambda: {"de:08116:7800": 32.7, "de:08117:5000": 28.0, "de:08117:2000": 12.0}
            via = self.get().get_json()["modes"][0]["platforms"][0]["departures"][0]["via"]
            self.assertEqual(via, ["Plochingen", "Göppingen"])
            # no stop database on the server: the board still works, going by
            # what the timetable service says (Geislingen has trains, Göppingen only "buses")
            departures.stop_importance = lambda: {}
            via = self.get().get_json()["modes"][0]["platforms"][0]["departures"][0]["via"]
            self.assertEqual(via, ["Plochingen", "Geislingen (Steige)"])
        finally:
            departures.stop_importance = original

    def test_two_bus_bays_with_the_same_number_are_two_platforms(self):
        # like Reutlingen Hbf/Listplatz: no signposted names, and the timetable
        # service numbers both bays "1"
        def bus(bay, coord, minute, next_name):
            location = {"id": f"{STOP}:{bay}:1", "name": "Stuttgart Hauptbahnhof (oben)", "type": "platform", "coord": coord,
                        "properties": {"platform": "1"}, "parent": EVENT["location"]["parent"]}
            return {"location": location, "departureTimePlanned": f"2026-10-01T08:{minute:02d}:00Z",
                    "transportation": {"id": f"bus:{bay}", "disassembledName": "42", "product": {"class": 5, "name": "Bus"},
                                       "destination": {"name": "Endstation"}, "properties": {"tripCode": minute}},
                    "onwardLocations": [{**platform_of("de:08111:1", next_name, "1"), "productClasses": [5]},
                                        {**platform_of("de:08111:2", "Endstation", "1"), "productClasses": [5]}]}
        self.events = [bus(1, [48.78470, 9.18310], 1, "Schlossplatz"), bus(1, [48.78470, 9.18310], 11, "Schlossplatz"),
                       bus(2, [48.78490, 9.18330], 5, "Neckartor")]
        self.station = {"locations": [{**STATION["locations"][0], "assignedStops": [
            {"id": STOP, "name": "Stuttgart Hauptbahnhof (oben)", "productClasses": [5]}]}]}
        client.get_departures = lambda stop_id, classes=(), **kw: {"stopEvents": self.events} if stop_id == STOP else {}
        mode = self.get().get_json()["modes"][0]
        self.assertFalse(mode["flat"])
        self.assertEqual([(p["name"], p["id"], p["towards"], len(p["departures"])) for p in mode["platforms"]], [
            ("Platform 1 · towards Schlossplatz", STOP + ":1:1", ["Schlossplatz"], 2),
            ("Platform 1 · towards Neckartor", STOP + ":2:1", ["Neckartor"], 1),
        ])
        self.assertEqual([(p["lat"], p["lon"]) for p in mode["platforms"]], [(48.7847, 9.1831), (48.7849, 9.1833)])
        self.assertEqual({p["code"] for p in mode["platforms"]}, {"1"})

    def test_unknown_tab_falls_back_to_the_first(self):
        self.assertEqual(self.get("?tab=nonsense").get_json()["tab"], "trains")

    def test_unknown_platform_has_no_code(self):
        self.events = [{**EVENT, "location": {**EVENT["location"], "properties": {}}}]
        platform = self.get().get_json()["modes"][0]["platforms"][0]
        self.assertEqual((platform["code"], platform["name"]), (None, "Unknown platform"))

    def test_empty_station_with_service_nearby(self):
        self.events = []
        client.get_departures = lambda stop_id, classes=(), **kw: (
            {"stopEvents": [EVENT]} if stop_id == NEIGHBOUR else {})
        body = self.get().get_json()
        self.assertEqual((body["empty"], body["modes"], body["tabs"], body["tab"]), ("nearby", [], [], None))
        self.assertEqual(body["nearby"], [{
            "id": NEIGHBOUR, "name": "Stuttgart Hauptbahnhof (tief)", "lat": 48.783385, "lon": 9.180225,
            "types": [{"id": "sbahn", "name": "S-Bahn"}],
        }])

    def test_next_service_day_when_nothing_departs_today(self):
        def only_later(stop_id, classes=(), start=None, **kw):
            return {"stopEvents": [EVENT]} if stop_id == STOP and start is not None else {}
        client.get_departures = only_later
        body = self.get().get_json()
        # EVENT is planned for 1 October 2026, 08:00 UTC = 10:00 local time
        self.assertEqual((body["next_service"], body["empty"], body["tabs"], body["tab"]), ("2026-10-01", None, [], None))
        self.assertEqual(body["modes"][0]["platforms"][0]["departures"][0]["line"], "RE5")

    def test_empty_station_without_service(self):
        client.get_departures = lambda stop_id, classes=(), **kw: {}
        body = self.get().get_json()
        self.assertEqual((body["empty"], body["nearby"]), ("no_service", []))


class PlatformBoardTest(ApiTestCase):
    """The departures of one platform: /stops/{stop}/departures?platform={id}"""

    BAY = STOP + ":5:15"

    def bus(self, minute, bay=None, next_name="Schlossplatz"):
        location = {"id": bay or self.BAY, "name": "Stuttgart Hauptbahnhof (oben)", "type": "platform", "coord": [48.7841, 9.1820],
                    "properties": {"platform": "15", "platformName": "Bstg 15"}, "parent": EVENT["location"]["parent"]}
        return {"location": location, "departureTimePlanned": f"2026-10-01T08:{minute:02d}:00Z",
                "transportation": {"id": "bus:42", "disassembledName": "42", "product": {"class": 5, "name": "Bus"},
                                   "destination": {"name": "Endstation"}, "properties": {"tripCode": minute}},
                "onwardLocations": [{**platform_of("de:08111:1", next_name, "1"), "productClasses": [5]},
                                    {**platform_of("de:08111:2", "Endstation", "1"), "productClasses": [5]}]}

    def setUp(self):
        super().setUp()
        departures._trips.clear()
        self.asked = []
        self.answer = [self.bus(5), self.bus(25)]

        def get_departures(stop_id, classes=(), **kw):
            self.asked.append(stop_id)
            return {"stopEvents": self.answer} if stop_id == self.BAY else {"stopEvents": self.events}
        client.get_departures = get_departures
        self.known = departures.known_platform
        departures.known_platform = lambda platform_id: {}

    def tearDown(self):
        departures.known_platform = self.known
        departures._trips.clear()
        super().tearDown()

    def get(self, platform=None, stop=STOP):
        return self.http.get(f"/api/v1/stops/{stop}/departures?platform={platform or self.BAY}")

    def test_shape(self):
        res = self.get()
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(list(body), ["stop", "nearby", "tabs", "tab", "modes", "empty", "next_service", "platform"])
        self.assertEqual(body["stop"]["id"], STOP)
        # one platform has no tabs to choose from; `tab` says what kind of transport it is
        self.assertEqual((body["tabs"], body["tab"], body["empty"], body["next_service"]), ([], "bus", None, None))
        self.assertEqual(body["platform"], {"id": self.BAY, "code": "15", "name": "Platform 15", "lat": 48.7841, "lon": 9.182,
                                            "towards": ["Schlossplatz"]})
        (mode,) = body["modes"]
        (platform,) = mode["platforms"]
        self.assertEqual((mode["id"], mode["flat"], platform["id"], len(platform["departures"])), ("bus", True, self.BAY, 2))

    def test_the_timetable_service_is_asked_for_the_platform(self):
        self.get()
        self.assertEqual(self.asked, [self.BAY])   # not for the whole stop and its tabs

    def test_only_that_platform_s_departures(self):
        # should the service answer with more than was asked for
        self.answer.append(self.bus(9, bay=STOP + ":5:16"))
        self.answer.append(EVENT)
        body = self.get().get_json()
        rows = [dep for mode in body["modes"] for platform in mode["platforms"] for dep in platform["departures"]]
        self.assertEqual(len(rows), 2)
        self.assertEqual({platform["id"] for mode in body["modes"] for platform in mode["platforms"]}, {self.BAY})

    def test_its_departures_open_their_stops(self):
        dep = self.get().get_json()["modes"][0]["platforms"][0]["departures"][0]
        res = self.http.get(f"/api/v1/stops/{STOP}/departures/{dep['id']}/stops")
        self.assertEqual(res.status_code, 200)
        self.assertEqual([stop["name"] for stop in res.get_json()["onward"]], ["Schlossplatz", "Endstation"])

    def test_a_quiet_platform(self):
        self.answer = []
        body = self.get().get_json()
        self.assertEqual((body["modes"], body["empty"], body["tab"]), ([], "platform", None))
        self.assertEqual(body["platform"], {"id": self.BAY, "code": None, "name": "Unknown platform", "lat": None, "lon": None,
                                            "towards": []})
        # with a stop database, that says where the platform is
        departures.known_platform = lambda platform_id: {"id": platform_id, "code": "15", "lat": 48.7841, "lon": 9.182,
                                                         "kinds": ["bus"], "towards": ["Stuttgart Schlossplatz"]}
        self.assertEqual(self.get().get_json()["platform"], {"id": self.BAY, "code": "15", "name": "Platform 15", "lat": 48.7841,
                                                             "lon": 9.182, "towards": ["Stuttgart Schlossplatz"]})

    def test_a_platform_of_another_stop(self):
        for platform in (NEIGHBOUR + ":1:1", NEIGHBOUR, "nonsense", STOP + "5:15"):
            res = self.get(platform)
            self.assertEqual((res.status_code, res.get_json()["error"]["code"]), (404, "unknown_platform"), platform)
        self.assertEqual(self.asked, [])   # refused without bothering the timetable service
        # and an unknown stop is still an unknown stop
        res = self.get(stop="nonsense")
        self.assertEqual(res.get_json()["error"]["code"], "unknown_stop")

    def test_a_stop_that_is_its_own_platform(self):
        # some simple stops have no platform level: the platform's id is the stop's
        def get_departures(stop_id, classes=(), **kw):
            return {"stopEvents": [self.bus(5, bay=STOP), self.bus(9)]}
        client.get_departures = get_departures
        body = self.get(STOP).get_json()
        self.assertEqual(body["platform"]["id"], STOP)
        self.assertEqual([p["id"] for mode in body["modes"] for p in mode["platforms"]], [STOP])

    def test_without_the_parameter_the_whole_stop(self):
        for query in ("", "?platform=", "?platform=%20"):
            body = self.http.get(f"/api/v1/stops/{STOP}/departures{query}").get_json()
            self.assertIsNone(body["platform"], query)
            self.assertEqual(body["tab"], "trains")

    def test_timetable_service_down(self):
        def down(*args, **kwargs):
            raise client.EfaError("timeout")
        client.get_departures = down
        res = self.get()
        self.assertEqual((res.status_code, res.get_json()["error"]["code"]), (502, "timetable_unavailable"))


class DepartureStopsTest(ApiTestCase):
    """The stops of one departure: /stops/{stop}/departures/{id}/stops"""

    def setUp(self):
        super().setUp()
        departures._trips.clear()
        self.requests = []  # every question to the timetable service
        answer = client.get_departures

        def counted(stop_id, classes=(), **kw):
            self.requests.append(kw)
            return answer(stop_id, classes, **kw)
        client.get_departures = counted

    def tearDown(self):
        departures._trips.clear()
        super().tearDown()

    def board_id(self):
        board = self.http.get(f"/api/v1/stops/{STOP}/departures").get_json()
        return board["modes"][0]["platforms"][0]["departures"][0]["id"]

    def get(self, departure_id, stop=STOP):
        return self.http.get(f"/api/v1/stops/{stop}/departures/{departure_id}/stops")

    def test_shape(self):
        departure_id = self.board_id()
        res = self.get(departure_id)
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(list(body), ["id", "line", "destination", "here", "previous", "onward"])
        self.assertEqual((body["id"], body["line"], body["destination"]), (departure_id, "RE5", "Ulm Hauptbahnhof"))
        self.assertEqual(body["here"], {
            "id": STOP, "name": "Hauptbahnhof (oben)", "lat": 48.784729, "lon": 9.183172, "has_board": True,
            "platform": {"code": "3", "name": "Platform 3"},
            "time": "2026-10-01T08:04:00Z", "planned": "2026-10-01T08:00:00Z", "delay": 4, "realtime": True,
            "cancelled": False,
        })
        self.assertEqual(body["previous"], [{
            "id": "de:08115:5774", "name": "Böblingen", "lat": 48.687, "lon": 9.004, "has_board": True,
            "platform": {"code": "2", "name": "Platform 2"},
            "time": "2026-10-01T07:38:00Z", "planned": "2026-10-01T07:38:00Z", "delay": None, "realtime": False,
            "cancelled": False,
        }])
        self.assertEqual([stop["name"] for stop in body["onward"]], ["Plochingen", "Göppingen", "Ulm Hauptbahnhof"])
        # stops ahead carry the time the vehicle gets there
        self.assertEqual(body["onward"][0], {
            "id": "de:08116:7800", "name": "Plochingen", "lat": 48.713, "lon": 9.411, "has_board": True,
            "platform": {"code": "4", "name": "Platform 4"},
            "time": "2026-10-01T08:18:00Z", "planned": "2026-10-01T08:15:00Z", "delay": 3, "realtime": True,
            "cancelled": False,
        })

    def test_the_row_and_its_stops_agree(self):
        board = self.http.get(f"/api/v1/stops/{STOP}/departures").get_json()
        row = board["modes"][0]["platforms"][0]["departures"][0]
        trip = self.get(row["id"]).get_json()
        for field in ("time", "planned", "delay", "realtime", "cancelled"):
            self.assertEqual(trip["here"][field], row[field], field)
        names = [stop["name"] for stop in trip["onward"]]
        self.assertEqual([name for name in names if name in row["via"]], row["via"])

    def test_answered_from_memory_after_a_board(self):
        departure_id = self.board_id()
        asked = len(self.requests)
        self.assertEqual(self.get(departure_id).status_code, 200)
        self.assertEqual(len(self.requests), asked)

    def test_asks_the_timetable_service_when_not_remembered(self):
        departure_id = self.board_id()
        remembered = self.get(departure_id).get_json()
        departures._trips.clear()  # as after a restart of the server
        asked = len(self.requests)
        res = self.get(departure_id)
        self.assertEqual((res.status_code, res.get_json()), (200, remembered))
        self.assertEqual(len(self.requests), asked + 1)
        # it looks around the departure's planned time
        self.assertEqual(self.requests[-1]["start"], datetime(2026, 10, 1, 7, 59, tzinfo=timezone.utc))
        # ... and remembers the answer
        self.get(departure_id)
        self.assertEqual(len(self.requests), asked + 1)

    def test_forgotten_after_a_while(self):
        departure_id = self.board_id()
        expires, trip = departures._trips[departure_id]
        departures._trips[departure_id] = (expires - departures.TRIP_TTL - 1, trip)
        asked = len(self.requests)
        self.assertEqual(self.get(departure_id).status_code, 200)
        self.assertEqual(len(self.requests), asked + 1)

    def test_memory_is_limited(self):
        for number in range(departures.TRIP_MEMORY + 5):
            departures.remember_trip({"id": str(number)})
        self.assertEqual(len(departures._trips), departures.TRIP_MEMORY)
        self.assertNotIn("4", departures._trips)   # the oldest went
        self.assertIn("5", departures._trips)

    def assert_unknown(self, res):
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.get_json()["error"]["code"], "unknown_departure")

    def test_unknown_departure(self):
        self.board_id()
        self.assert_unknown(self.get("nonsense"))
        self.assert_unknown(self.get("%20"))
        # well-formed, but no such departure in the timetable
        other = parse.departure_id({**EVENT, "departureTimePlanned": "2026-10-01T09:00:00Z"})
        self.assert_unknown(self.get(other))

    def test_departure_of_another_station(self):
        departure_id = self.board_id()
        self.assert_unknown(self.get(departure_id, stop=NEIGHBOUR))   # remembered, but not there
        departures._trips.clear()
        self.assert_unknown(self.get(departure_id, stop=NEIGHBOUR))   # not remembered either
        self.assert_unknown(self.get(departure_id, stop="de:08111:61"))  # an id that only starts the same

    def test_timetable_service_down(self):
        departure_id = self.board_id()
        departures._trips.clear()

        def down(*args, **kwargs):
            raise client.EfaError("timeout")
        client.get_departures = down
        res = self.get(departure_id)
        self.assertEqual((res.status_code, res.get_json()["error"]["code"]), (502, "timetable_unavailable"))

    def test_excluded_services_have_no_stops(self):
        # a flight is not on any board, so its id must not open either
        flight = {**EVENT, "transportation": {**EVENT["transportation"], "product": {"class": 12, "name": "Flugzeug"}}}
        self.events = [flight]
        self.assert_unknown(self.get(parse.departure_id(flight)))


class ErrorTest(ApiTestCase):
    def assert_error(self, res, status, code):
        self.assertEqual(res.status_code, status)
        body = res.get_json()
        self.assertEqual(list(body), ["error"])
        self.assertEqual(list(body["error"]), ["code", "message"])
        self.assertEqual(body["error"]["code"], code)
        self.assertTrue(body["error"]["message"])

    def test_unknown_stop(self):
        self.assert_error(self.http.get("/api/v1/stops/nonsense/departures"), 404, "unknown_stop")

    def test_only_the_stop_asked_for(self):
        # the timetable service makes a stop out of any text ("..", a name): that is not this stop
        client.get_station = lambda stop_id: self.station
        for wrong in ("..", "Stuttgart Hbf", "de:08111:61150", f"{STOP}:2:2"):
            self.assert_error(self.http.get(f"/api/v1/stops/{wrong}/departures"), 404, "unknown_stop")
        self.assertEqual(self.http.get(f"/api/v1/stops/{STOP}/departures").status_code, 200)

    def test_stop_outside_baden_wuerttemberg(self):
        client.get_station = lambda stop_id: {"locations": [{"id": "de:09162:100", "name": "München, Hbf", "type": "stop"}]}
        self.assert_error(self.http.get("/api/v1/stops/de:09162:100/departures"), 404, "unknown_stop")

    def test_timetable_service_down(self):
        def down(*args, **kwargs):
            raise client.EfaError("timeout")
        client.get_station = client.find_stops = down
        self.assert_error(self.http.get(f"/api/v1/stops/{STOP}/departures"), 502, "timetable_unavailable")
        self.assert_error(self.http.get("/api/v1/stops/search?q=Stuttgart"), 502, "timetable_unavailable")

    def test_unknown_address_and_method(self):
        self.assert_error(self.http.get("/api/v1/nothing"), 404, "not_found")
        self.assert_error(self.http.get("/api/departures?stop_id=x"), 404, "not_found")  # the pre-v1 address
        self.assert_error(self.http.post("/api/v1/stops/search"), 405, "method_not_allowed")

    def test_main_page_has_map_and_panel(self):
        page = self.http.get("/").get_data(as_text=True)
        for part in ('id="map"', 'id="panel"', 'id="stop-input"', 'id="credits"', "js/departures.js", "js/sheet.js", "js/map.js",
                     'name="viewport"'):
            self.assertIn(part, page)
        # the scripts and the stylesheet the page asks for exist
        for asset in ("/static/js/departures.js", "/static/js/sheet.js", "/static/js/map.js", "/static/css/style.css"):
            with self.http.get(asset) as res:
                self.assertEqual(res.status_code, 200, asset)

    def test_the_separate_map_page_is_gone(self):
        self.assertEqual(self.http.get("/map").status_code, 404)

    def test_pages_are_not_affected(self):
        self.assertEqual(self.http.get("/").status_code, 200)
        missing = self.http.get("/nothing")
        self.assertEqual(missing.status_code, 404)
        self.assertFalse(missing.is_json)


if __name__ == "__main__":
    unittest.main()


class PageTest(unittest.TestCase):
    """The page itself: its addresses and how its files are cached (app.py)."""

    def setUp(self):
        self.http = app.test_client()

    def test_start_and_stop_addresses_are_the_page(self):
        start = self.http.get("/")
        self.assertEqual(start.status_code, 200)
        for address in ("/stop/de:08111:6115", "/stop/de%3A08111%3A6115", "/stop/anything"):
            res = self.http.get(address)
            self.assertEqual(res.status_code, 200, address)
            self.assertEqual(res.data, start.data, address)
            self.assertEqual(res.headers["Cache-Control"], "no-cache")
        self.assertEqual(self.http.get("/stop/").status_code, 404)

    def test_page_files_carry_their_version(self):
        page = self.http.get("/").get_data(as_text=True)
        for name in ("css/style.css", "js/menu.js", "js/departures.js", "js/sheet.js", "js/map.js", "js/address.js"):
            self.assertRegex(page, rf'/static/{name}\?v=\d+"', name)
        self.assertLess(page.index("js/map.js"), page.index("js/address.js"))   # address.js opens the stop: last

    def test_versioned_files_are_kept_long(self):
        page = self.http.get("/").get_data(as_text=True)
        versioned = page[page.index("/static/js/map.js"):].split('"')[0]
        res = self.http.get(versioned)
        self.assertEqual(res.status_code, 200)
        self.assertIn("max-age=31536000", res.headers["Cache-Control"])
        res.close()
        res = self.http.get("/static/js/map.js")   # without a version: asked again every time
        self.assertNotIn("max-age=31536000", res.headers.get("Cache-Control", ""))
        res.close()
