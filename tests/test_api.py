"""Tests for the JSON API (api/v1.py): addresses, response shapes, errors.

These pin down what docs/API.md promises. A future client (e.g. a mobile
app) depends on these field names, so a failing test here means the API
changed in a way that would break it. EFA is replaced by canned responses,
so the tests need no network."""
import unittest

from app import app
from efa import client

STOP = "de:08111:6115"
NEIGHBOUR = "de:08111:6118"

STATION = {"locations": [{
    "id": STOP, "name": "Stuttgart, Hauptbahnhof (oben)", "type": "stop", "coord": [48.784729, 9.183172],
    "assignedStops": [
        {"id": STOP, "name": "Stuttgart Hauptbahnhof (oben)", "productClasses": [13, 16]},
        {"id": NEIGHBOUR, "name": "Stuttgart Hauptbahnhof (tief)", "productClasses": [1], "coord": [48.783385, 9.180225]},
    ],
}]}

EVENT = {
    "location": {
        "name": "Stuttgart Hauptbahnhof (oben)", "type": "platform", "properties": {"platformName": "Gleis 3"},
        "parent": {"name": "Stuttgart Hauptbahnhof (oben)", "type": "stop", "parent": {"name": "Stuttgart", "type": "locality"}},
    },
    "departureTimePlanned": "2026-10-01T08:00:00Z",
    "departureTimeEstimated": "2026-10-01T08:04:00Z",
    "transportation": {
        "disassembledName": "RE5", "product": {"class": 13, "name": "R-Bahn"}, "destination": {"name": "Ulm Hauptbahnhof"},
    },
    "onwardLocations": [
        {"name": "Plochingen", "productClasses": [0, 1, 5], "parent": {"name": "Plochingen"}},
        {"name": "Göppingen", "productClasses": [5], "parent": {"name": "Göppingen"}},
        {"name": "Ulm Hauptbahnhof", "productClasses": [0, 5], "parent": {"name": "Ulm Hauptbahnhof"}},
    ],
}

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


class IndexTest(ApiTestCase):
    def test_index_lists_the_endpoints(self):
        body = self.http.get("/api/v1/").get_json()
        self.assertEqual(body["version"], 1)
        self.assertEqual(set(body["endpoints"]), {"search_stops", "stops_in_area", "departures"})


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
        self.assertEqual(list(body), ["stop", "nearby", "tabs", "tab", "modes", "empty"])
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
        self.assertEqual(list(platform), ["code", "name", "area", "departures"])
        self.assertEqual((platform["code"], platform["name"], platform["area"]), ("3", "Platform 3", None))

        departure = platform["departures"][0]
        self.assertEqual(list(departure), ["line", "destination", "via", "dticket", "time", "planned", "minutes",
                                           "delay", "realtime", "cancelled"])
        minutes = departure.pop("minutes")  # depends on the clock
        self.assertIsInstance(minutes, int)
        self.assertEqual(departure, {
            "line": "RE5",
            "destination": "Ulm Hauptbahnhof",
            "via": ["Plochingen", "Göppingen"],
            "dticket": True,
            "time": "2026-10-01T08:04:00Z",
            "planned": "2026-10-01T08:00:00Z",
            "delay": 4,
            "realtime": True,
            "cancelled": False,
        })

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

    def test_empty_station_without_service(self):
        client.get_departures = lambda stop_id, classes=(), **kw: {}
        body = self.get().get_json()
        self.assertEqual((body["empty"], body["nearby"]), ("no_service", []))


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

    def test_pages_are_not_affected(self):
        self.assertEqual(self.http.get("/").status_code, 200)
        missing = self.http.get("/nothing")
        self.assertEqual(missing.status_code, 404)
        self.assertFalse(missing.is_json)


if __name__ == "__main__":
    unittest.main()
