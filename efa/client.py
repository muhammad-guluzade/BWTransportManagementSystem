"""
All HTTP traffic to EFA goes through this module.

Data source: the statewide Baden-Württemberg EFA instance (NVBW). It's free
and needs no API key -- but it's unofficial and undocumented, so it can
change without notice. Run debug_efa.py if anything here misbehaves.
"""
import threading
import time
from datetime import datetime

import requests

from efa.localtime import to_local

EFA_BASE = "https://www.efa-bw.de/nvbw/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TransitDemo/0.1)"}
TIMEOUT = 10

# how long a response may be reused, in seconds
STOP_SEARCH_TTL = 600
DEPARTURES_TTL = 30
STATION_TTL = 6 * 3600
TAB_CHECK_TTL = 3600

# one shared session reuses connections to EFA instead of opening a new one
# (TCP + TLS handshake) for every request
_session = requests.Session()
_session.headers.update(HEADERS)

_CACHE_MAX_ENTRIES = 500
_cache = {}  # key -> (expires_at, data)
_cache_lock = threading.Lock()


class EfaError(Exception):
    """EFA could not be reached or returned something that isn't JSON."""


def efa_get(endpoint: str, params: dict, ttl: int = 0) -> dict:
    """Call an EFA endpoint and return parsed JSON, reusing a cached
    response if one younger than `ttl` seconds exists."""
    key = (endpoint, tuple(sorted(params.items())))
    now = time.monotonic()

    if ttl:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and hit[0] > now:
                return hit[1]

    try:
        resp = _session.get(EFA_BASE + endpoint, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        resp.encoding = "utf-8"  # EFA doesn't declare charset correctly; don't let requests guess
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise EfaError(str(e)) from e

    if ttl:
        with _cache_lock:
            if len(_cache) >= _CACHE_MAX_ENTRIES:
                for k in [k for k, (expires, _) in _cache.items() if expires <= now]:
                    del _cache[k]
                if len(_cache) >= _CACHE_MAX_ENTRIES:
                    _cache.clear()
            _cache[key] = (now + ttl, data)
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


def get_station(stop_id: str) -> dict:
    """Raw response describing a station: its name, the transport types
    that stop there, and the stations EFA links to it (e.g. Stuttgart Hbf
    oben / tief / Arnulf-Klett-Platz). `stop_id` is a global id like
    'de:08111:115' or EFA's internal one."""
    return efa_get(
        "XML_DM_REQUEST",
        {
            # the lightest departure request there is: only the station
            # description in `locations` is used
            "outputFormat": "rapidJSON",
            "type_dm": "stop",
            "name_dm": stop_id,
            "mode": "direct",
            "limit": 1,
            "depType": "stopEvents",
            "coordOutputFormat": "WGS84[dd.ddddd]",
        },
        ttl=STATION_TTL,
    )


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
