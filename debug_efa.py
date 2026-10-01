"""
Probe the EFA APIs and report what data they actually give us.

For each sample station it checks, per endpoint:
  - stop search: is the stop found, what is its global id (gid), coordinates?
  - departures (classic JSON, what app.py uses): how many, which platform
    fields are filled, which transport modes appear.
  - departures (rapidJSON): per-platform ids/coordinates and the next stops
    of each trip (needed to label a platform's direction, e.g. "towards
    Hauptbahnhof").

This is an unofficial API, so field names can change -- run this whenever
something in app.py misbehaves, before changing code.

Usage:
    python debug_efa.py                      # all sample stations, both endpoints
    python debug_efa.py "Stuttgart Pragfriedhof"
    python debug_efa.py --endpoint bw        # only the statewide endpoint
    python debug_efa.py --dump               # also save raw JSON to debug_output/
"""
import argparse
import json
from collections import Counter
from pathlib import Path

import requests

ENDPOINTS = {
    "bw": "https://www.efa-bw.de/nvbw/",
    "vvs": "https://www3.vvs.de/mngvvs/",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TransitDemo/0.1)"}

# a mix of big hubs, a two-platform U-Bahn stop, other cities and rural stops
SAMPLE_STATIONS = [
    "Stuttgart Pragfriedhof",
    "Stuttgart Hauptbahnhof",
    "Stuttgart Charlottenplatz",
    "Karlsruhe Hauptbahnhof",
    "Freiburg Hauptbahnhof",
    "Heidelberg Bismarckplatz",
    "Ulm Hauptbahnhof",
    "Schluchsee Rathaus",
]

# departure keys that might carry platform information
PLATFORM_KEYS = ("platform", "platformName", "pointType", "stopName", "nameWO")

DUMP_DIR = Path(__file__).parent / "debug_output"


def as_list(value) -> list:
    """EFA returns a bare dict instead of a list when there is one item."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def efa_get(base: str, endpoint: str, params: dict) -> dict:
    params = {**params, "outputFormat": "JSON"}
    r = requests.get(base + endpoint, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    r.encoding = "utf-8"  # EFA doesn't declare charset correctly
    return r.json()


def stopfinder(base: str, query: str) -> dict:
    return efa_get(
        base,
        "XML_STOPFINDER_REQUEST",
        {
            "type_sf": "any",
            "name_sf": query,
            "stateless": 1,
            "locationServerActive": 1,
            "coordOutputFormat": "WGS84[dd.ddddd]",
        },
    )


def departures(base: str, stop_id: str) -> dict:
    return efa_get(
        base,
        "XML_DM_REQUEST",
        {
            "type_dm": "stop",
            "name_dm": stop_id,
            "mode": "direct",
            "useRealtime": 1,
            "limit": 40,
            "coordOutputFormat": "WGS84[dd.ddddd]",
        },
    )


def departures_rapid(base: str, stop_id: str) -> dict:
    """Same request in rapidJSON format. Unlike classic JSON (where
    onwardStopSeq comes back as an empty placeholder), this fills in the
    onward stops of each trip -- what we need to label a platform's
    direction -- plus per-platform ids and coordinates."""
    params = {
        "outputFormat": "rapidJSON",
        "type_dm": "stop",
        "name_dm": stop_id,
        "mode": "direct",
        "useRealtime": 1,
        "limit": 40,
        "depType": "stopEvents",
        "includeCompleteStopSeq": 1,
        "coordOutputFormat": "WGS84[dd.ddddd]",
    }
    r = requests.get(base + "XML_DM_REQUEST", params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.json()


def extract_points(sf: dict) -> list:
    points = sf.get("stopFinder", {}).get("points")
    # single match: {"point": {...}}; multiple matches: [{...}, {...}]
    if isinstance(points, dict):
        points = points.get("point", points)
    return as_list(points)


def dump(name: str, data: dict) -> None:
    DUMP_DIR.mkdir(exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in name)
    path = DUMP_DIR / f"{safe}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"    raw JSON saved to {path.relative_to(Path(__file__).parent)}")


def summarize_departures(deps: list) -> None:
    print(f"    departures returned: {len(deps)}")
    if not deps:
        return

    # which top-level keys exist at all (helps spot renamed fields)
    all_keys = Counter(k for d in deps for k in d)
    print(f"    departure keys: {', '.join(sorted(all_keys))}")

    for key in PLATFORM_KEYS:
        values = [str(d.get(key, "")).strip() for d in deps]
        filled = [v for v in values if v]
        if key in all_keys:
            distinct = sorted(set(filled))[:10]
            print(f"    {key:<13} filled {len(filled)}/{len(deps)}  values: {distinct}")

    seq_keys = [k for k in all_keys if "stopseq" in k.lower()]
    print(f"    stop-sequence keys: {seq_keys or 'NONE'}")

    modes = Counter((d.get("servingLine") or {}).get("name", "?") for d in deps)
    print(f"    modes (servingLine.name): {dict(modes)}")



def summarize_rapid(events: list) -> None:
    """platform -> line, final destination and the next two stops."""
    print(f"    [rapidJSON] stop events returned: {len(events)}")
    if not events:
        return

    with_onward = sum(1 for e in events if e.get("onwardLocations"))
    print(f"    [rapidJSON] events with onward stops: {with_onward}/{len(events)}")

    by_platform = {}
    for e in events:
        loc = e.get("location") or {}
        props = loc.get("properties") or {}
        platform = (props.get("platform") or "").strip() or "?"
        key = f"{platform}  (id {loc.get('id')}, coord {loc.get('coord')})"

        t = e.get("transportation") or {}
        # onward entries can be platforms; the stop name lives on the parent
        next_stops = []
        for o in e.get("onwardLocations") or []:
            name = (o.get("parent") or {}).get("name") or o.get("name")
            if name and name not in next_stops:
                next_stops.append(name)
        entry = (
            f"{t.get('disassembledName') or t.get('number', '?')} -> "
            f"{(t.get('destination') or {}).get('name', '?')} "
            f"(next: {', '.join(next_stops[:2]) or '-'})"
        )
        by_platform.setdefault(key, set()).add(entry)

    print("    [rapidJSON] platform -> line, destination, next stops:")
    for platform in sorted(by_platform):
        print(f"      [{platform}]")
        for entry in sorted(by_platform[platform])[:8]:
            print(f"        {entry}")


def probe(endpoint_name: str, query: str, do_dump: bool) -> None:
    base = ENDPOINTS[endpoint_name]
    print(f"\n=== [{endpoint_name}] {query} ===")

    try:
        sf = stopfinder(base, query)
    except (requests.RequestException, ValueError) as e:
        print(f"    stopfinder FAILED: {e}")
        return

    stops = [p for p in extract_points(sf) if p.get("anyType") == "stop"]
    if not stops:
        print("    no stop found")
        if do_dump:
            dump(f"{endpoint_name}_{query}_stopfinder", sf)
        return

    stop = stops[0]
    ref = stop.get("ref") or {}
    stop_id = stop.get("stateless") or ref.get("id")
    print(f"    found: {stop.get('name')}  (of {len(stops)} stop matches)")
    print(f"    stateless id: {stop_id}   gid: {ref.get('gid')}   coords: {ref.get('coords')}")
    print(f"    modes: {stop.get('modes')}   quality: {stop.get('quality')}")

    try:
        dm = departures(base, stop_id)
    except (requests.RequestException, ValueError) as e:
        print(f"    departures FAILED: {e}")
        return

    summarize_departures(as_list(dm.get("departureList")))

    try:
        rj = departures_rapid(base, stop_id)
    except (requests.RequestException, ValueError) as e:
        print(f"    rapidJSON departures FAILED: {e}")
        rj = {}
    summarize_rapid(rj.get("stopEvents") or [])

    if do_dump:
        dump(f"{endpoint_name}_{query}_stopfinder", sf)
        dump(f"{endpoint_name}_{query}_departures", dm)
        dump(f"{endpoint_name}_{query}_departures_rapid", rj)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stations", nargs="*", help="station names to probe (default: built-in samples)")
    parser.add_argument("--endpoint", choices=[*ENDPOINTS, "all"], default="all")
    parser.add_argument("--dump", action="store_true", help="save raw JSON responses to debug_output/")
    args = parser.parse_args()

    stations = args.stations or SAMPLE_STATIONS
    endpoints = list(ENDPOINTS) if args.endpoint == "all" else [args.endpoint]

    for query in stations:
        for name in endpoints:
            probe(name, query, args.dump)


if __name__ == "__main__":
    main()
