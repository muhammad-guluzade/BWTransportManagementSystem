"""The server's self-check (selfcheck.py): every check with made-up answers,
never the network."""
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import requests

import selfcheck

NOW = datetime(2026, 12, 10, 9, 40, tzinfo=timezone.utc)   # 10:40 German time, 10 December
TODAY = date(2026, 12, 10)


class Answer:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text
        self.headers = {"Content-Type": "application/json"} if body is not None else {"Content-Type": "text/html"}

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


def board(n=3):
    return {"modes": [{"id": "trains", "platforms": [{"departures": [{}] * n}]}]}


def site(index=None, boards=None, journeys=1, page_ok=True, fail=()):
    """A fake of our own API: boards[when] = number of departures per station (default 3)."""
    index = index if index is not None else {"endpoints": {}, "stop_data": {
        "valid_until": "2027-12-12", "imported_at": "2026-12-09T03:05:00Z", "expired": False}}
    asked = []

    def get(path):
        asked.append(path)
        if any(f in path for f in fail):
            raise requests.ConnectionError("down")
        if path == "/":
            return Answer(200 if page_ok else 502, text='<meta name="app-version" content="1">' if page_ok else "Bad Gateway")
        if path == "/api/v1/":
            return Answer(200, index)
        if "/departures" in path:
            when = path.split("when=")[1] if "when=" in path else "now"
            n = (boards or {}).get(when, 3)
            return Answer(200, board(n) if n else {"modes": [], "empty": "no_timetable"})
        if path.startswith("/api/v1/journeys"):
            return Answer(200, {"journeys": [{}] * journeys})
        return Answer(404, {"error": {"code": "not_found"}})
    get.asked = asked
    return get


CFG = {"base": "", "ping": "", "public": "bw.example", "navigator": True}


def run(get, **kw):
    return selfcheck.run_checks(get, NOW, {**CFG, **kw.pop("cfg", {})}, run=kw.pop("run", lambda cmd: "success\n"),
                                days_left=kw.pop("days_left", lambda host: 60.0),
                                fetch=kw.pop("fetch", lambda url: Answer(200, text="<title>BW Departures</title>")))


def named(results):
    return {r.name: r for r in results}


class SelfCheckTest(unittest.TestCase):
    def test_all_good(self):
        get = site()
        results = run(get)
        self.assertEqual(selfcheck.problems(results), [], selfcheck.report_text(results, NOW))
        self.assertTrue(selfcheck.report_text(results, NOW).endswith("ALL GOOD"))
        # departures now, tomorrow 08:00 and a week ahead (German dates), a connection tomorrow
        self.assertTrue(any("when=2026-12-11T08:00" in p for p in get.asked))
        self.assertTrue(any("when=2026-12-17T08:00" in p for p in get.asked))
        self.assertTrue(any(p.startswith("/api/v1/journeys") and "when=2026-12-11T08:00" in p for p in get.asked))

    def test_app_down(self):
        results = named(run(site(fail=("/",))))
        self.assertFalse(results["page"].ok)
        self.assertFalse(results["api"].ok)
        self.assertFalse(results["stop data"].ok)

    def test_the_december_change_seen_a_day_ahead(self):
        # the timetable service has nothing for tomorrow: a problem; a week ahead: only a warning
        results = named(run(site(boards={"2026-12-11T08:00": 0, "2026-12-17T08:00": 0})))
        self.assertFalse(results["departures tomorrow (2026-12-11)"].ok)
        self.assertTrue(results["departures tomorrow (2026-12-11)"].timetable)
        self.assertIn("no_timetable", results["departures tomorrow (2026-12-11)"].detail)
        week = results["departures in a week (2026-12-17)"]
        self.assertTrue(week.warning and not week.ok)
        only_week = run(site(boards={"2026-12-17T08:00": 0}))
        self.assertEqual(selfcheck.problems(only_week), [])
        self.assertIn("warn departures in a week", selfcheck.report_text(only_week, NOW))

    def test_one_station_quiet_is_fine(self):
        calls = {"n": 0}
        inner = site()

        def get(path):
            if "/departures" in path and "when=" not in path:
                calls["n"] += 1
                if calls["n"] == 1:
                    return Answer(200, {"modes": [], "empty": "nearby"})
            return inner(path)
        self.assertTrue(named(run(get))["departures now"].ok)

    def test_stop_data(self):
        def with_data(**data):
            base = {"valid_until": "2027-12-12", "imported_at": "2026-12-09T03:05:00Z", "expired": False}
            return named(run(site(index={"endpoints": {}, "stop_data": {**base, **data}})))
        self.assertTrue(with_data()["stop data period"].ok)
        # ends tomorrow, no newer file: told before it runs out
        self.assertFalse(with_data(valid_until="2026-12-11")["stop data period"].ok)
        self.assertFalse(with_data(valid_until="2026-12-01", expired=True)["stop data period"].ok)
        self.assertFalse(with_data(imported_at="2026-11-25T03:05:00Z")["weekly import"].ok)
        self.assertTrue(with_data(imported_at="2026-12-02T03:05:00Z")["weekly import"].ok)
        self.assertFalse(named(run(site(index={"endpoints": {}, "stop_data": None})))["stop data"].ok)

    def test_import_job(self):
        self.assertFalse(named(run(site(), run=lambda cmd: "exit-code\n"))["last import job"].ok)
        self.assertNotIn("last import job", named(run(site(), run=lambda cmd: "")))   # no systemd: not checked

        def no_systemctl(cmd):
            raise FileNotFoundError("systemctl")
        self.assertNotIn("last import job", named(run(site(), run=no_systemctl)))

    def test_public_address_and_certificate(self):
        self.assertFalse(named(run(site(), days_left=lambda host: 5.0))["https certificate"].ok)

        def broken(host):
            raise OSError("refused")
        self.assertFalse(named(run(site(), days_left=broken))["https certificate"].ok)
        self.assertFalse(named(run(site(), fetch=lambda url: Answer(502, text="Bad Gateway")))["public address"].ok)
        self.assertNotIn("public address", named(run(site(), cfg={"public": ""})))

    def test_navigator_off_no_connection_check(self):
        self.assertNotIn("connection tomorrow", named(run(site(), cfg={"navigator": False})))
        self.assertFalse(named(run(site(journeys=0)))["connection tomorrow"].ok)

    def test_watcher(self):
        sent = []

        def post(url, data=None, timeout=None):
            sent.append((url, data))
        self.assertEqual(selfcheck.tell_watcher("https://hc-ping.com/abc", True, "ALL GOOD", post), "watcher told")
        selfcheck.tell_watcher("https://hc-ping.com/abc", False, "x" * 20000, post)
        self.assertEqual(sent[0][0], "https://hc-ping.com/abc")
        self.assertEqual(sent[1][0], "https://hc-ping.com/abc/fail")
        self.assertEqual(len(sent[1][1]), 10000)   # Healthchecks.io keeps up to 10 KB of a report
        self.assertIn("no watcher", selfcheck.tell_watcher("", True, "", post))

        def down(url, data=None, timeout=None):
            raise requests.ConnectionError("no")
        self.assertIn("could not", selfcheck.tell_watcher("https://hc-ping.com/abc", True, "", down))

    def test_light_runs_and_the_hourly_deep_run(self):
        # the timer runs every 15 minutes: the deep checks only in the first quarter of an hour (German time)
        self.assertTrue(selfcheck.deep_run(datetime(2026, 12, 10, 9, 1, tzinfo=timezone.utc), hourly=True))    # 10:01
        self.assertFalse(selfcheck.deep_run(datetime(2026, 12, 10, 9, 16, tzinfo=timezone.utc), hourly=True))  # 10:16
        self.assertFalse(selfcheck.deep_run(datetime(2026, 12, 10, 9, 45, tzinfo=timezone.utc), hourly=True))
        self.assertTrue(selfcheck.deep_run(datetime(2026, 12, 10, 9, 45, tzinfo=timezone.utc), hourly=False))  # by hand: all
        get = site()
        light = named(selfcheck.run_checks(get, NOW, CFG, run=lambda cmd: "success", days_left=lambda h: 60.0,
                                           fetch=lambda url: Answer(200, text="BW Departures"), deep=False))
        self.assertIn("departures now", light)
        self.assertFalse({"connection tomorrow", "departures tomorrow (2026-12-11)"} & set(light))
        self.assertFalse(any("when=" in p or "journeys" in p for p in get.asked))   # 3 boards, nothing ahead

    def test_disk(self):
        self.assertTrue(selfcheck.check_disk(Path(selfcheck.HERE)).name == "disk")


if __name__ == "__main__":
    unittest.main()
