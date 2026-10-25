"""The navigator's journeys (efa/parse.py journey part, services/journeys.py,
GET /api/v1/journeys). EFA's journey planner is replaced by answers in its
real shape (rapidJSON, XML_TRIP_REQUEST2), so no network is needed."""
import unittest
from datetime import datetime, timedelta, timezone

from app import app
from efa import client, parse
from gtfs import store
from services import departures, journeys

app.config["RATE_LIMIT"] = False

PRAG = "de:08111:115"
ULM = "de:08421:1008"
LATER = datetime.now(timezone.utc) + timedelta(hours=2)


def at(minutes):
    return (LATER + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def platform(stop_id, name, number, coord, **times):
    return {"isGlobalId": True, "id": f"{stop_id}:1:{number}", "name": name, "type": "platform", "coord": coord,
            "properties": {"platformName": f"Gleis {number}", "platform": number},
            "parent": {"isGlobalId": True, "id": stop_id, "name": name, "type": "stop",
                       "parent": {"name": "Stuttgart", "type": "locality"}}, **times}


U15 = {"id": "vvs:20015: :H:j26", "name": "Stadtbahn U15", "disassembledName": "U15", "number": "U15",
       "product": {"id": 2, "class": 3, "name": "Stadtbahn"}, "destination": {"name": "Heumaden"}, "properties": {"tripCode": 1004}}
RE5 = {"id": "ddb:90T05: :H:j26", "name": "R-Bahn RE5", "disassembledName": "RE5", "number": "RE5",
       "product": {"id": 8, "class": 13, "name": "R-Bahn"}, "destination": {"name": "Ulm Hauptbahnhof"}, "properties": {"tripCode": 19005}}
ICE = {"id": "ddb:11569: :H:j26", "name": "ICE 569 InterCityExpress", "number": "569",
       "product": {"id": 0, "class": 16, "name": "Zug"}, "destination": {"name": "München Hbf"},
       "properties": {"tripCode": 569, "trainType": "ICE", "trainNumber": "569"}}
NOTICE = {"priority": "high", "type": "lineInfo", "infoLinks": [{
    "urlText": "Stuttgart-Mitte/Degerloch: Streckenunterbrechung",
    "content": "<div><strong>Von Samstag</strong>&nbsp;bis Sonntag<br />gesperrt.</div><ul><li>Linie U5: Leinfelden</li></ul>"}]}


def ride(transportation, start, end, infos=()):
    return {"duration": 240, "distance": 3000, "origin": start, "destination": end, "transportation": transportation,
            "stopSequence": [start, {**platform("de:08111:6056", "Charlottenplatz", "1", [48.77, 9.18],
                                                 arrivalTimePlanned=at(2), departureTimePlanned=at(2))}, end],
            "infos": list(infos), "coords": [start["coord"], end["coord"]]}


def walk(start, end, minutes, cls=99):
    return {"duration": minutes * 60, "distance": minutes * 80, "origin": start, "destination": end,
            "transportation": {"product": {"class": cls, "name": "footpath"}}, "coords": [start["coord"], end["coord"]], "infos": []}


def journey(second=RE5, change_at=14, walk_minutes=10, infos=(NOTICE,)):
    """Pragfriedhof --U15--> Arnulf-Klett-Platz --walk--> Hauptbahnhof --second--> Ulm."""
    first = ride(U15, platform(PRAG, "Pragfriedhof", "1", [48.798, 9.182], departureTimePlanned=at(0), departureTimeEstimated=at(0)),
                 platform("de:08111:6112", "Hauptbahnhof (A.-Klett-Platz)", "4", [48.783, 9.181], arrivalTimePlanned=at(4), arrivalTimeEstimated=at(4)),
                 infos)
    legs = [first]
    if walk_minutes:
        legs.append(walk(first["destination"], platform("de:08111:6115", "Stuttgart Hauptbahnhof", "15", [48.784, 9.183]), walk_minutes))
    legs.append(ride(second, platform("de:08111:6115", "Stuttgart Hauptbahnhof", "15", [48.784, 9.183],
                                      departureTimePlanned=at(change_at), departureTimeEstimated=at(change_at)),
                     platform(ULM, "Ulm Hauptbahnhof", "2", [48.399, 9.983], arrivalTimePlanned=at(70), arrivalTimeEstimated=at(70))))
    return {"rating": 0, "isAdditional": False, "interchanges": 1, "legs": legs, "fare": {}}


class ParseJourneyTest(unittest.TestCase):
    def test_a_connection(self):
        j = parse.parse_journey(journey())
        self.assertEqual([leg["kind"] for leg in j["legs"]], ["ride", "walk", "ride"])
        self.assertEqual((j["changes"], j["minutes"], j["dticket"]), (1, 70, True))
        u15, foot, re5 = j["legs"]
        self.assertEqual((u15["line"], u15["destination"], u15["from"]["platform"]["code"], u15["to"]["platform"]["code"]), ("U15", "Heumaden", "1", "4"))
        self.assertEqual((foot["minutes"], foot["metres"]), (10, 800))
        self.assertEqual([s["name"] for s in u15["stops"]], ["Charlottenplatz"])        # the stops in between
        self.assertEqual(u15["path"], [[48.798, 9.182], [48.783, 9.181]])
        # the same id as the ride on the board of the stop it leaves from: the live checks look it up there
        self.assertEqual(u15["departure_id"], parse.departure_id(parse.leg_as_event(journey()["legs"][0])))
        self.assertEqual(u15["board_stop"], PRAG)

    def test_train_names_and_d_ticket_as_on_the_boards(self):
        j = parse.parse_journey(journey(second=ICE, change_at=14))
        ice = j["legs"][-1]
        self.assertEqual((ice["line"], ice["dticket"], ice["brand"]), ("ICE 569", False, "ice"))
        self.assertFalse(j["dticket"])

    def test_walks_of_both_kinds(self):
        for cls in (99, 100):
            leg = walk(platform(PRAG, "a", "1", [48.0, 9.0]), platform(ULM, "b", "2", [48.1, 9.1]), 5, cls)
            self.assertTrue(parse.is_walk(leg), cls)
        self.assertFalse(parse.is_walk(ride(U15, platform(PRAG, "a", "1", [48.0, 9.0]), platform(ULM, "b", "2", [48.1, 9.1]))))

    def test_changes(self):
        # U15 arrives at +4, a 10 min walk, the train leaves at +14: no minute to spare, possible
        t = parse.parse_journey(journey(change_at=14))["transfers"][0]
        self.assertEqual((t["walk_minutes"], t["needed_minutes"], t["spare_minutes"], t["possible"]), (10, 10, 0, True))
        # leaving at +13: one minute short
        t = parse.parse_journey(journey(change_at=13))["transfers"][0]
        self.assertEqual((t["spare_minutes"], t["possible"]), (-1, False))
        # no walk in between: at least 2 minutes to another platform
        t = parse.parse_journey(journey(change_at=6, walk_minutes=0))["transfers"][0]
        self.assertEqual((t["needed_minutes"], t["spare_minutes"], t["possible"]), (2, 0, True))

    def test_notices_as_plain_text(self):
        notice = parse.parse_journey(journey())["legs"][0]["notices"][0]
        self.assertEqual(notice["title"], "Stuttgart-Mitte/Degerloch: Streckenunterbrechung")
        self.assertEqual(notice["text"], "Von Samstag bis Sonntag\ngesperrt.\n• Linie U5: Leinfelden")
        self.assertEqual(notice["priority"], "high")
        self.assertNotIn("<", notice["text"])

    def test_a_point_on_the_map_is_a_place_without_a_board(self):
        raw = journey()
        address = {"id": "streetID:1", "name": "Mitte (Stuttgart), Kleiner Schloßplatz 13", "type": "address", "coord": [48.7789, 9.1767]}
        raw["legs"].insert(0, walk(address, raw["legs"][0]["origin"], 5, cls=100))
        start = parse.parse_journey(raw)["legs"][0]["from"]
        self.assertEqual((start["kind"], start["id"], start["has_board"], start["name"]),
                         ("place", "", False, "Mitte (Stuttgart), Kleiner Schloßplatz 13"))

    def test_same_place(self):
        self.assertEqual(parse.journey_error({"systemMessages": [{"type": "error", "code": -4007, "text": "origin too close to destination"}]}), "too_close")
        self.assertIsNone(parse.journey_error({"systemMessages": []}))


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.real = (client.get_journeys, departures.stop_importance, store.in_area, departures.outside_timetable)
        self.answer = {"journeys": [journey()]}
        self.asked = []

        def fake(origin, destination, start=None, arrive=False, classes=(), count=5):
            self.asked.append((origin, destination, start, arrive, tuple(classes), count))
            return self.answer
        client.get_journeys = fake
        departures.stop_importance = lambda: {}

        def no_db(*a, **kw):
            raise store.StopsNotImported("test")
        store.in_area = no_db

    def tearDown(self):
        client.get_journeys, departures.stop_importance, store.in_area, departures.outside_timetable = self.real

    def test_stops_and_points(self):
        result = journeys.plan(PRAG, ULM)
        self.assertEqual(self.asked[0][:2], (PRAG, ULM))
        self.assertEqual((result["from"]["kind"], result["from"]["name"], result["to"]["name"]), ("stop", "Pragfriedhof", "Ulm Hauptbahnhof"))
        journeys.plan("48.7789, 9.1767", ULM)
        self.assertEqual(self.asked[1][0], "9.17670:48.77890:WGS84[dd.ddddd]")

    def test_places_refused(self):
        for bad in ("", "Stuttgart", "de:09162:100", "48.5734,7.7521x", "91,9"):
            with self.assertRaises(journeys.InvalidPlace, msg=bad):
                journeys.plan(bad, ULM)
        with self.assertRaises(journeys.InvalidPlace):
            journeys.plan("52.52,13.40", ULM)                 # Berlin: outside the state's box
        # with the stop database: a point is in the state when a stop of it is near
        store.in_area = lambda *a, **kw: ([], 0)
        with self.assertRaises(journeys.InvalidPlace):
            journeys.plan("48.5734,7.7521", ULM)              # Strasbourg, inside the box
        store.in_area = lambda *a, **kw: ([{"id": "x"}], 1)
        journeys.plan("48.7789,9.1767", ULM)
        # a stop id the database does not know
        departures.stop_importance = lambda: {PRAG: 1.0}
        with self.assertRaises(journeys.InvalidPlace):
            journeys.plan(PRAG, "de:08111:99999")

    def test_a_point_across_the_border(self):
        """Neu-Ulm is next to Ulm's stops: the town the planner found there
        decides (its municipality key; 08 = Baden-Württemberg)."""
        store.in_area = lambda *a, **kw: ([{"id": "x"}], 1)
        neu_ulm = journey()
        neu_ulm["legs"][0]["origin"]["parent"] = {"id": "placeID:9775135:-1", "name": "Neu-Ulm", "type": "locality"}
        self.answer = {"journeys": [neu_ulm]}
        with self.assertRaisesRegex(journeys.InvalidPlace, "from is outside"):
            journeys.plan("48.3955,10.0115", ULM)
        self.assertEqual(len(journeys.plan(PRAG, ULM)["journeys"]), 1)    # a stop id is never asked
        neu_ulm["legs"][-1]["destination"]["parent"] = {"id": "placeID:9775135:-1"}
        with self.assertRaisesRegex(journeys.InvalidPlace, "to is outside"):
            journeys.plan(PRAG, "48.3955,10.0115")
        ulm = journey()
        ulm["legs"][0]["origin"]["parent"] = {"id": "placeID:8421000:-1", "name": "Ulm", "type": "locality"}
        self.answer = {"journeys": [ulm]}
        self.assertEqual(len(journeys.plan("48.3984,9.9916", ULM)["journeys"]), 1)
        self.answer = {"journeys": ["nonsense", {"legs": "x"}]}             # nothing says: allowed
        self.assertEqual(journeys.plan("48.3984,9.9916", ULM)["journeys"], [])

    def test_trains_are_one_kind_as_on_the_boards(self):
        self.answer = {"journeys": [journey(second=ICE)]}
        legs = journeys.plan(PRAG, ULM)["journeys"][0]["legs"]
        self.assertEqual(legs[-1]["mode"], {"id": "trains", "name": "Trains"})
        self.assertEqual(legs[0]["mode"], {"id": "u_bahn_tram", "name": "U-Bahn / Tram"})

    def test_d_ticket_only(self):
        self.answer = {"journeys": [journey(second=ICE), journey(second=RE5)]}
        result = journeys.plan(PRAG, ULM, dticket=True)
        self.assertEqual([j["legs"][-1]["line"] for j in result["journeys"]], ["RE5"])
        self.assertNotIn(16, self.asked[0][4])                # long-distance trains not asked for
        self.assertIn(13, self.asked[0][4])
        self.assertTrue(result["dticket"])

    def test_connections_that_left_already_are_dropped(self):
        gone = journey()
        for leg in gone["legs"]:
            for end in (leg["origin"], leg["destination"]):
                for key in [k for k in end if k.endswith("Planned") or k.endswith("Estimated")]:
                    end[key] = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.answer = {"journeys": [gone, journey()]}
        self.assertEqual(len(journeys.plan(PRAG, ULM)["journeys"]), 1)

    def test_arrive_by(self):
        late = journey()
        late["legs"][-1]["destination"]["arrivalTimePlanned"] = at(200)
        self.answer = {"journeys": [journey(), late]}
        result = journeys.plan(PRAG, ULM, start=LATER + timedelta(minutes=80), arrive=True)
        self.assertEqual(len(result["journeys"]), 1)
        self.assertTrue(result["arrive"])
        self.assertTrue(self.asked[0][3])

    def test_flights_and_on_demand_are_not_offered(self):
        demand = journey(second={**RE5, "product": {"class": 10, "name": "AST"}})
        self.answer = {"journeys": [demand]}
        self.assertEqual(journeys.plan(PRAG, ULM)["journeys"], [])

    def test_why_nothing(self):
        self.answer = {"journeys": [], "systemMessages": [{"type": "error", "code": -4007}]}
        self.assertEqual(journeys.plan(PRAG, ULM)["empty"], "too_close")
        self.answer = {"journeys": []}
        self.assertEqual(journeys.plan(PRAG, ULM)["empty"], "none")
        departures.outside_timetable = lambda start: True
        self.assertEqual(journeys.plan(PRAG, ULM, start=LATER)["empty"], "no_timetable")

    def test_one_unreadable_connection_is_skipped(self):
        broken = journey()
        broken["legs"][0]["origin"] = "nonsense"
        self.answer = {"journeys": [broken, journey()]}
        self.assertEqual(len(journeys.plan(PRAG, ULM)["journeys"]), 1)


class JourneysApiTest(unittest.TestCase):
    def setUp(self):
        self.real = (client.get_journeys, departures.stop_importance, store.in_area)
        client.get_journeys = lambda *a, **kw: {"journeys": [journey()]}
        departures.stop_importance = lambda: {}

        def no_db(*a, **kw):
            raise store.StopsNotImported("test")
        store.in_area = no_db
        self.http = app.test_client()

    def tearDown(self):
        client.get_journeys, departures.stop_importance, store.in_area = self.real

    def test_shape(self):
        res = self.http.get(f"/api/v1/journeys?from={PRAG}&to={ULM}")
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(list(body), ["from", "to", "when", "arrive", "dticket", "journeys", "empty"])
        j = body["journeys"][0]
        self.assertEqual(list(j), ["id", "departure", "arrival", "minutes", "changes", "dticket", "legs", "transfers"])
        self.assertEqual(list(j["legs"][0]), ["kind", "line", "mode", "brand", "destination", "dticket", "replacement", "departure_id",
                                              "board_stop", "from", "to", "stops", "path", "notices"])
        self.assertEqual(list(j["legs"][1]), ["kind", "minutes", "metres", "from", "to", "path"])
        self.assertEqual(list(j["transfers"][0]), ["at", "to", "walk_minutes", "needed_minutes", "spare_minutes", "possible"])

    def test_options(self):
        asked = []
        client.get_journeys = lambda *a, **kw: asked.append((a, kw)) or {"journeys": []}
        self.http.get(f"/api/v1/journeys?from={PRAG}&to={ULM}&when=2026-11-02T08:00&arrive=1&dticket=1")
        (origin, destination, start, arrive, classes, count), _ = asked[0]
        self.assertEqual((start, arrive), (datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc), True))
        self.assertTrue(classes)

    def test_errors(self):
        for query in ("", f"from={PRAG}", f"from=Stuttgart&to={ULM}", f"from={PRAG}&to={ULM}&when=tomorrow"):
            res = self.http.get(f"/api/v1/journeys?{query}")
            self.assertEqual((res.status_code, res.get_json()["error"]["code"]), (400, "invalid_parameter"), query)

    def test_counts_as_a_timetable_request(self):
        from api import limits
        self.assertEqual(limits.KIND_OF_ENDPOINT["api_v1.plan_journeys"], "timetable")


if __name__ == "__main__":
    unittest.main()
