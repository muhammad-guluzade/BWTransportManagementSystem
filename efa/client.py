"""
All HTTP traffic to EFA goes through this module.

Data source: the statewide Baden-Württemberg EFA instance (NVBW). It's free
and needs no API key -- but it's unofficial and undocumented, so it can
change without notice. Run debug_efa.py if anything here misbehaves.
"""
import threading
import time
from collections import OrderedDict
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter

from efa.localtime import to_local

EFA_BASE = "https://www.efa-bw.de/nvbw/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TransitDemo/0.1)"}
TIMEOUT = 10
# The journey planner can be slow: measured 0.8-6 s by day, 17-26 s at 01:00 (Reutlingen),
# sometimes a quick 502 from its gateway; it gets more time and one more try.
JOURNEYS_TIMEOUT = 40

# how long a response may be reused, in seconds
STOP_SEARCH_TTL = 600
DEPARTURES_TTL = 30
STATION_TTL = 6 * 3600
TAB_CHECK_TTL = 3600

# one shared session reuses connections to EFA instead of opening a new one
# (TCP + TLS handshake) for every request
_session = requests.Session()
_session.headers.update(HEADERS)
# up to this many connections are kept for reuse: one per worker of the server
# (serve.py) and some more; with 10, busy moments threw connections away
_session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=32))

_CACHE_MAX_ENTRIES = 500
_cache = {}  # key -> (expires_at, data)
_cache_lock = threading.Lock()
# A list of departures with every trip's stops is big: 1.5-5 MB of text,
# 5-12 MB once read (measured 22 October 2026). At most this many are kept,
# the newest; the small answers (stations, tab checks) don't count. Without
# the cap, boards of 140 different stops took the server's app past 1 GB;
# with 60, 150 stops peaked at 620 MB, and 60 visitors were as quick as
# without a cap (30 was too few: answers were fetched and read again).
_CACHE_MAX_BIG = 60
_BIG_FROM_DEPARTURES = 10
_big = OrderedDict()  # keys of the big answers in _cache, oldest first
# answers whose time is up are cleared out at most this often
_SWEEP_EVERY_SECONDS = 5
_last_sweep = [0.0]


def _is_big(data) -> bool:
    return isinstance(data, dict) and (len(data.get("stopEvents") or []) > _BIG_FROM_DEPARTURES or bool(data.get("journeys")))


def _remember(key, data, expires, now):
    """Keep an answer until `expires` (under _cache_lock): expired answers are
    cleared out every few seconds, and of the big ones only the newest
    _CACHE_MAX_BIG stay."""
    if now - _last_sweep[0] >= _SWEEP_EVERY_SECONDS or len(_cache) >= _CACHE_MAX_ENTRIES:
        _last_sweep[0] = now
        for k in [k for k, (until, _) in _cache.items() if until <= now]:
            del _cache[k]
            _big.pop(k, None)
        if len(_cache) >= _CACHE_MAX_ENTRIES:
            _cache.clear()
            _big.clear()
    _cache[key] = (expires, data)
    _big.pop(key, None)
    if _is_big(data):
        _big[key] = None
        while len(_big) > _CACHE_MAX_BIG:
            oldest, _ = _big.popitem(last=False)
            _cache.pop(oldest, None)


class EfaError(Exception):
    """EFA could not be reached or returned something that isn't JSON."""


def efa_get(endpoint: str, params: dict, ttl: int = 0, keep=None, timeout: float = TIMEOUT, retry: bool = False) -> dict:
    """Call an EFA endpoint and return parsed JSON, reusing a cached
    response if one younger than `ttl` seconds exists. `keep(data)` can say
    that an answer is not worth remembering: it is returned, but the next
    call asks EFA again. `retry`: a quick server error (5xx within 10 s) is
    asked once more."""
    key = (endpoint, tuple(sorted(params.items())))
    now = time.monotonic()

    if ttl:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and hit[0] > now:
                return hit[1]

    try:
        started = time.monotonic()
        resp = _session.get(EFA_BASE + endpoint, params=params, timeout=timeout)
        if retry and resp.status_code >= 500 and time.monotonic() - started < 10:
            resp = _session.get(EFA_BASE + endpoint, params=params, timeout=timeout)
        resp.raise_for_status()
        resp.encoding = "utf-8"  # EFA doesn't declare charset correctly; don't let requests guess
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise EfaError(str(e)) from e

    if ttl and (keep is None or keep(data)):
        with _cache_lock:
            _remember(key, data, now + ttl, time.monotonic())
    return data


def find_stops(query: str) -> dict:
    """Raw stop-search response for a free-text query."""
    return efa_get(
        "XML_STOPFINDER_REQUEST",
        {
            "outputFormat": "JSON",
            "type_sf": "any",
            "name_sf": query,
            "stateless": 1,
            "locationServerActive": 1,
            "coordOutputFormat": "WGS84[dd.ddddd]",
        },
        ttl=STOP_SEARCH_TTL,
    )


def _station_params(stop_id: str) -> dict:
    # the lightest departure request there is: only the station description
    # in `locations` is used
    return {
        "outputFormat": "rapidJSON",
        "type_dm": "stop",
        "name_dm": stop_id,
        "mode": "direct",
        "limit": 1,
        "depType": "stopEvents",
        "coordOutputFormat": "WGS84[dd.ddddd]",
    }


def get_station(stop_id: str) -> dict:
    """Raw response describing a station: its name, the transport types
    that stop there, and the stations EFA links to it (e.g. Stuttgart Hbf
    oben / tief / Arnulf-Klett-Platz). `stop_id` is a global id like
    'de:08111:115' or EFA's internal one."""
    return efa_get(
        "XML_DM_REQUEST",
        _station_params(stop_id),
        ttl=STATION_TTL,
        # Remembered for hours, so only if it really is this station: when EFA
        # has trouble it can answer with some other stop (seen on 16 October
        # 2026), and that must not hide the station for the next six hours.
        keep=lambda data: describes(data, stop_id),
    )


# A station EFA always knows: if EFA describes it, EFA is answering normally.
REFERENCE_STOP = "de:08111:6115"   # Stuttgart Hauptbahnhof
ANSWERING_TTL = 60
_answering = [float("-inf"), True]   # [when last asked, answer]


def answering() -> bool:
    """Is EFA answering normally right now? It is asked for a well-known
    station, at most once a minute. Tells a stop that EFA really doesn't know
    (a few in NVBW's list, e.g. de:08117:10121) from an EFA outage."""
    now = time.monotonic()
    if now - _answering[0] < ANSWERING_TTL:
        return _answering[1]
    try:
        ok = describes(efa_get("XML_DM_REQUEST", _station_params(REFERENCE_STOP)), REFERENCE_STOP)
    except EfaError:
        ok = False
    _answering[:] = [now, ok]
    return ok


def describes(data: dict, stop_id: str) -> bool:
    """Is this answer about the station `stop_id`?"""
    locations = data.get("locations") if isinstance(data, dict) else None
    return bool(locations) and isinstance(locations[0], dict) and locations[0].get("id") == stop_id


def get_departures(stop_id: str, classes=(), limit: int = 100, with_stops: bool = True, ttl: int = DEPARTURES_TTL,
                   start: datetime | None = None) -> dict:
    """Raw departure-monitor response for one station.

    EFA answers with departures of the 24 hours after `start` (default: now).

    `classes` restricts it to those EFA product classes (e.g. (1,) for
    S-Bahn), so a busy mode can't crowd the others out of `limit`.
    `with_stops=False` leaves out each trip's stop list, which makes the
    response much smaller when only the departures' existence matters."""
    params = {
        # rapidJSON, unlike the classic JSON format, includes each trip's
        # onward stops and per-platform ids
        "outputFormat": "rapidJSON",
        "type_dm": "stop",
        "name_dm": stop_id,
        "mode": "direct",
        "useRealtime": 1,
        "limit": limit,
        "depType": "stopEvents",
        # only this station's own departures, not those of linked stations
        "deleteAssignedStops_dm": 1,
        "coordOutputFormat": "WGS84[dd.ddddd]",
    }
    if with_stops:
        params["includeCompleteStopSeq"] = 1
    if start:
        local = to_local(start)  # EFA takes the time in German local time
        params.update({"itdDate": local.strftime("%Y%m%d"), "itdTime": local.strftime("%H%M"), "itdTripDateTimeDepArr": "dep"})
    if classes:
        params["includedMeans"] = "checkbox"
        for cls in classes:
            params[f"inclMOT_{cls}"] = "on"
    return efa_get("XML_DM_REQUEST", params, ttl=ttl)


JOURNEYS_TTL = 30


def get_journeys(origin: str, destination: str, start: datetime | None = None, arrive: bool = False,
                 classes=(), count: int = 5) -> dict:
    """Raw journey-planner response: connections from `origin` to
    `destination`, each a stop id ("de:08111:115") or a point
    ("lat,lon" already turned into EFA's "lon:lat:WGS84[dd.ddddd]" by the
    caller, see `place_param`). `start` (UTC; None: now) is the departure,
    or with `arrive` the latest arrival. `classes`: only these product
    classes (the D-Ticket switch)."""
    params = {
        "outputFormat": "rapidJSON",
        "coordOutputFormat": "WGS84[dd.ddddd]",
        "calcNumberOfTrips": count,
        "useRealtime": 1,
        "type_origin": "coord" if origin.endswith("WGS84[dd.ddddd]") else "any",
        "name_origin": origin,
        "type_destination": "coord" if destination.endswith("WGS84[dd.ddddd]") else "any",
        "name_destination": destination,
    }
    if start:
        local = to_local(start)  # EFA takes the time in German local time
        params.update({"itdDate": local.strftime("%Y%m%d"), "itdTime": local.strftime("%H%M")})
    params["itdTripDateTimeDepArr"] = "arr" if arrive else "dep"
    if classes:
        params["includedMeans"] = "checkbox"
        for cls in classes:
            params[f"inclMOT_{cls}"] = "on"
    return efa_get("XML_TRIP_REQUEST2", params, ttl=JOURNEYS_TTL, timeout=JOURNEYS_TIMEOUT, retry=True)


def place_param(lat: float, lon: float) -> str:
    """A point on the map as EFA wants it."""
    return f"{lon:.5f}:{lat:.5f}:WGS84[dd.ddddd]"

