"""
How often one visitor may ask the API. Each board can cost the timetable
service several requests (a stop with nothing in the next 24 hours about
eight), so one visitor asking without pause could make the app hammer it,
and get the app itself blocked there.

A "bucket" of tokens per visitor and kind of request: every request takes
one, and the bucket fills up again at a steady rate. A person using the page
never empties it (typing a name, opening stops, a board refreshing every 30
seconds); a program asking many times a second does, and gets
429 too_many_requests until tokens are back.

A visitor is an IP address; IPv6 addresses count by their /64 network, since
one connection usually has a whole /64 to choose from. Behind Caddy the
address is the visitor's only if serve.py believes Caddy's headers
(BW_PROXY), otherwise every visitor would look like 127.0.0.1 and share
one bucket.
"""
import ipaddress
import math
import threading
import time

# kind of request -> (bucket size, tokens back per second)
LIMITS = {
    # asks the timetable service: boards, a departure's stops, the search, journeys
    "timetable": (60, 0.5),
    # answered from the stop database: the map's stops, the API's index
    "local": (120, 4.0),
}
KIND_OF_ENDPOINT = {
    "api_v1.stop_departures": "timetable",
    "api_v1.departure_stops": "timetable",
    "api_v1.search_stops": "timetable",
    "api_v1.plan_journeys": "timetable",
    "api_v1.stops_in_area": "local",
    "api_v1.index": "local",
}
# buckets are forgotten once they would be full again anyway
FORGET_EVERY_SECONDS = 60


class Limiter:
    def __init__(self, limits=LIMITS, clock=time.monotonic):
        self.limits = limits
        self.clock = clock
        self.buckets = {}      # (visitor, kind) -> [tokens, time of the last look]
        self.lock = threading.Lock()
        self.tidied = clock()

    def wait(self, visitor: str, kind: str) -> float:
        """Take a token for this request: 0 if it may go ahead, else the
        seconds until the next token is back."""
        size, rate = self.limits[kind]
        key = (visitor_key(visitor), kind)
        with self.lock:
            now = self.clock()
            self.tidy(now)
            tokens, then = self.buckets.get(key, (size, now))
            tokens = min(size, tokens + (now - then) * rate)
            if tokens < 1:
                self.buckets[key] = [tokens, now]
                return (1 - tokens) / rate
            self.buckets[key] = [tokens - 1, now]
            return 0.0

    def tidy(self, now):
        if now - self.tidied < FORGET_EVERY_SECONDS:
            return
        self.tidied = now
        for key, (tokens, then) in list(self.buckets.items()):
            size, rate = self.limits[key[1]]
            if tokens + (now - then) * rate >= size:
                del self.buckets[key]

    def reset(self):
        with self.lock:
            self.buckets.clear()


def visitor_key(address: str) -> str:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address or "?"
    if ip.version == 4:
        return str(ip)
    if ip.ipv4_mapped:
        return str(ip.ipv4_mapped)
    return str(ipaddress.ip_network(f"{ip}/64", strict=False))


def retry_after(seconds: float) -> str:
    """The Retry-After header: whole seconds, at least one."""
    return str(max(1, math.ceil(seconds)))
