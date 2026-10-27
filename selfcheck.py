"""The site checks itself (on the server every 15 minutes, 03:00-23:45, by
bw-selfcheck.timer) and tells the outside watcher how it went.

    python selfcheck.py                          # everything; print the report, tell the watcher
    python selfcheck.py --quiet --hourly-deep    # (the timer) only problems printed

What is checked, from the server itself (every run):
- the app answers (page and API), the public address answers over https and
  its certificate has more than 14 days left;
- the stop data: built, not expired, not ending within 2 days without a newer
  release (the December timetable change), refreshed by the weekly import
  within the last 9 days, the last import job did not fail;
- departures now at three big stations, through our own API (3 requests to
  the timetable service);
- enough free disk.
And on the run at the top of each hour ("deep", 8 requests more): departures
TOMORROW at 08:00 (it sees the day after a timetable change before it comes)
and a week ahead (a warning only), and (navigator on) a connection tomorrow.

The watcher: ONE free service, Healthchecks.io (the user's choice). Its check's
ping address goes into .env as BW_HEALTHCHECK_URL. Each run calls it ("all
well"), or its /fail address with this report. If the calls stop coming (the
server or the site is down), Healthchecks.io notices that too, within about
half an hour, and sends an e-mail. Without BW_HEALTHCHECK_URL the report is
only printed (systemd keeps it in the journal: journalctl -u bw-selfcheck).

The timetable service has short gaps at night (02:00-02:25 departures, around
midnight connections); the timer does not run then, and a failing timetable
check is tried twice more, a minute apart, before it counts."""
import argparse
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, NamedTuple

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from efa import localtime  # noqa: E402
from serve import read_env_file  # noqa: E402

STATIONS = {"de:08111:6115": "Stuttgart Hbf", "de:08212:90": "Karlsruhe Hbf", "de:08222:2417": "Mannheim Hbf"}
JOURNEY = ("de:08111:6115", "de:08421:1008")   # Stuttgart Hbf -> Ulm Hbf
STOP_DATA_ENDS_WARNING_DAYS = 2
IMPORT_AT_MOST_DAYS_AGO = 9
CERTIFICATE_AT_LEAST_DAYS = 14
DISK_AT_LEAST_GB = 2
TRIES = 3
TRY_AGAIN_SECONDS = 60
TIMEOUT = 45


class Result(NamedTuple):
    name: str
    ok: bool
    detail: str = ""
    warning: bool = False      # reported, but the run still counts as good
    timetable: bool = False    # depends on the timetable service: tried again before it counts


def settings() -> dict:
    found = {**read_env_file(HERE / ".env"), **os.environ}
    return {
        "base": f"http://127.0.0.1:{found.get('BW_PORT', '8000')}",
        "ping": found.get("BW_HEALTHCHECK_URL", "").strip().rstrip("/"),
        "public": found.get("BW_PUBLIC_HOST", "bw-departures.duckdns.org").strip(),
        "navigator": found.get("BW_NAVIGATOR", "on").strip().lower() not in ("off", "0", "no", "false"),
    }


def german_today(now: datetime) -> date:
    return localtime.to_local(now).date()


# ---------- the checks (each gets what it needs, so the tests can hand in fakes) ----------
def check_app(get: Callable) -> list:
    out = []
    try:
        page = get("/")
        out.append(Result("page", page.status_code == 200 and 'name="app-version"' in page.text,
                          f"answered {page.status_code}"))
    except requests.RequestException as exc:
        out.append(Result("page", False, f"no answer: {exc.__class__.__name__}"))
    try:
        index = get("/api/v1/")
        out.append(Result("api", index.status_code == 200 and "endpoints" in index.json(), f"answered {index.status_code}"))
        return out + [index.json()]
    except (requests.RequestException, ValueError) as exc:
        out.append(Result("api", False, f"no answer: {exc.__class__.__name__}"))
        return out + [None]


def check_stop_data(index: dict | None, today: date) -> list:
    data = (index or {}).get("stop_data")
    if not data:
        return [Result("stop data", False, "the API names no stop data (database missing or not readable)")]
    out = []
    until = date.fromisoformat(data["valid_until"]) if data.get("valid_until") else None
    if data.get("expired"):
        out.append(Result("stop data period", False, f"the stop data's timetable period ended on {until}; no newer file imported"))
    elif until and (until - today).days < STOP_DATA_ENDS_WARNING_DAYS:
        out.append(Result("stop data period", False, f"the stop data's timetable period ends on {until} and no newer file has come yet"))
    else:
        out.append(Result("stop data period", True, f"valid until {until}"))
    imported = data.get("imported_at")
    if imported:
        age = (datetime.combine(today, datetime.min.time()) - datetime.strptime(imported[:10], "%Y-%m-%d")).days
        out.append(Result("weekly import", age <= IMPORT_AT_MOST_DAYS_AGO, f"last import {imported[:10]} ({age} days ago)"))
    else:
        out.append(Result("weekly import", False, "no import date in the stop data"))
    return out


def check_import_job(run: Callable) -> list:
    """The weekly import's last result, as systemd remembers it (only where systemd runs it)."""
    try:
        answer = run(["systemctl", "show", "bw-stops-import.service", "-p", "Result", "--value"])
    except (OSError, subprocess.SubprocessError):
        return []
    result = answer.strip()
    if not result:
        return []
    return [Result("last import job", result == "success", f"systemd says: {result}")]


def check_boards(get: Callable, when: str | None, label: str, warning: bool = False) -> Result:
    """Departures at the big stations (now, or at a German local time 'YYYY-MM-DDTHH:MM'):
    good when at least two of the three have some."""
    found, notes = 0, []
    for stop, name in STATIONS.items():
        try:
            res = get(f"/api/v1/stops/{stop}/departures" + (f"?when={when}" if when else ""))
            body = res.json() if res.headers.get("Content-Type", "").startswith("application/json") else {}
            count = sum(len(p["departures"]) for m in body.get("modes", []) for p in m.get("platforms", []))
            if res.status_code == 200 and count:
                found += 1
            else:
                notes.append(f"{name}: {res.status_code} {body.get('empty') or (body.get('error') or {}).get('code') or 'no departures'}")
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            notes.append(f"{name}: {exc.__class__.__name__}")
    ok = found >= 2
    return Result(f"departures {label}", ok, f"{found} of {len(STATIONS)} stations" + (f" ({'; '.join(notes)})" if notes else ""),
                  warning=warning, timetable=True)


def check_journey(get: Callable, when: str) -> Result:
    try:
        res = get(f"/api/v1/journeys?from={JOURNEY[0]}&to={JOURNEY[1]}&when={when}")
        body = res.json()
        count = len(body.get("journeys", []))
        return Result("connection tomorrow", res.status_code == 200 and count > 0,
                      f"{count} connections Stuttgart Hbf -> Ulm Hbf" if res.status_code == 200
                      else f"answered {res.status_code} {(body.get('error') or {}).get('code', '')}", timetable=True)
    except (requests.RequestException, ValueError) as exc:
        return Result("connection tomorrow", False, f"no answer: {exc.__class__.__name__}", timetable=True)


def certificate_days_left(host: str, port: int = 443) -> float:
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=15) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            not_after = tls.getpeercert()["notAfter"]
    ends = datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), timezone.utc)
    return (ends - datetime.now(timezone.utc)).total_seconds() / 86400


def check_public(host: str, days_left: Callable, fetch: Callable) -> list:
    if not host:
        return []
    out = []
    try:
        left = days_left(host)
        out.append(Result("https certificate", left >= CERTIFICATE_AT_LEAST_DAYS, f"{left:.0f} days left"))
    except (OSError, ssl.SSLError, ValueError) as exc:
        out.append(Result("https certificate", False, f"could not check: {exc.__class__.__name__}: {exc}"))
    try:
        res = fetch(f"https://{host}/")
        out.append(Result("public address", res.status_code == 200 and "BW Departures" in res.text, f"answered {res.status_code}"))
    except requests.RequestException as exc:
        out.append(Result("public address", False, f"no answer: {exc.__class__.__name__}"))
    return out


def check_disk(path: Path) -> Result:
    free = shutil.disk_usage(path).free / 1e9
    return Result("disk", free >= DISK_AT_LEAST_GB, f"{free:.1f} GB free")


# ---------- one run ----------
def run_checks(get: Callable, now: datetime, cfg: dict, run=None, days_left=None, fetch=None, deep: bool = True) -> list:
    """`deep`: also the timetable a day and a week ahead and a connection (8 requests
    to the timetable service more; the timer asks for them once an hour)."""
    today = german_today(now)
    tomorrow = (today + timedelta(days=1)).isoformat() + "T08:00"
    week = (today + timedelta(days=7)).isoformat() + "T08:00"
    *results, index = check_app(get)
    results += check_stop_data(index, today)
    results += check_import_job(run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout))
    results.append(check_boards(get, None, "now"))
    if deep:
        results.append(check_boards(get, tomorrow, f"tomorrow ({tomorrow[:10]})"))
        results.append(check_boards(get, week, f"in a week ({week[:10]})", warning=True))
        if cfg["navigator"]:
            results.append(check_journey(get, tomorrow))
    results += check_public(cfg["public"], days_left or certificate_days_left,
                            fetch or (lambda url: requests.get(url, timeout=TIMEOUT)))
    results.append(check_disk(HERE))
    return results


def deep_run(now: datetime, hourly: bool) -> bool:
    """By hand: everything. The timer (every 15 minutes): the deep checks on the run in the
    first quarter of each hour (German time), the light ones on every run."""
    return not hourly or localtime.to_local(now).minute < 15


def problems(results: list) -> list:
    return [r for r in results if not r.ok and not r.warning]


def report_text(results: list, now: datetime) -> str:
    lines = [f"BW Departures self-check {localtime.to_local(now):%Y-%m-%d %H:%M} (German time)"]
    for r in results:
        mark = "ok  " if r.ok else ("warn" if r.warning else "FAIL")
        lines.append(f"{mark} {r.name}: {r.detail}")
    bad = problems(results)
    lines.append("ALL GOOD" if not bad else f"{len(bad)} PROBLEM(S): " + ", ".join(r.name for r in bad))
    return "\n".join(lines)


def tell_watcher(ping: str, ok: bool, text: str, post: Callable) -> str:
    if not ping:
        return "no watcher set (BW_HEALTHCHECK_URL)"
    try:
        post(ping if ok else f"{ping}/fail", data=text.encode("utf-8")[:10000], timeout=15)
        return "watcher told"
    except requests.RequestException as exc:
        return f"could not tell the watcher: {exc.__class__.__name__}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quiet", action="store_true", help="print only when something is wrong")
    parser.add_argument("--hourly-deep", action="store_true",
                        help="(the timer, every 15 minutes) the deep checks only on the run in the first quarter of an hour")
    args = parser.parse_args(argv)
    cfg = settings()
    session = requests.Session()

    def get(path):
        return session.get(cfg["base"] + path, timeout=TIMEOUT)
    if cfg["ping"]:
        try:   # "started": Healthchecks.io then also shows how long a run takes
            session.post(cfg["ping"] + "/start", timeout=15)
        except requests.RequestException:
            pass
    started = datetime.now(timezone.utc)
    deep = deep_run(started, args.hourly_deep)
    results = run_checks(get, started, cfg, deep=deep)
    # the timetable service has short gaps: its checks are tried again before they count
    for _ in range(TRIES - 1):
        again = [i for i, r in enumerate(results) if r.timetable and not r.ok]
        if not again:
            break
        time.sleep(TRY_AGAIN_SECONDS)
        fresh = run_checks(get, datetime.now(timezone.utc), cfg, deep=deep)
        by_name = {r.name: r for r in fresh}
        for i in again:
            results[i] = by_name.get(results[i].name, results[i])
    now = datetime.now(timezone.utc)
    text = report_text(results, now)
    ok = not problems(results)
    told = tell_watcher(cfg["ping"], ok, text, session.post)
    if not args.quiet or not ok:
        print(text)
    print(told)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
