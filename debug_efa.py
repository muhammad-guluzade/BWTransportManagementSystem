"""
Probe the EFA APIs and report what data they actually give us.

For each sample station it checks, per endpoint:
  - stop search: is the stop found, what is its global id (gid), coordinates?
  - departures (classic JSON, the older format): how many, which platform
    fields are filled, which transport modes appear.
  - departures (rapidJSON, what the app uses): per-platform ids/coordinates
    and the next stops of each trip.

--survey runs the app's own normalisation rules (efa/parse.py) over many
stations, to judge them on real data.

This is an unofficial API, so field names can change -- run this whenever
something in the app misbehaves, before changing code.

Usage:
    python debug_efa.py                      # all sample stations, both endpoints
    python debug_efa.py "Stuttgart Pragfriedhof"
    python debug_efa.py --endpoint bw        # only the statewide endpoint
    python debug_efa.py --dump               # also save raw JSON to debug_output/
    python debug_efa.py --structure          # how stations across BW are built:
                                             # linked stations, zones, modes
    python debug_efa.py --survey             # try out the normalisation rules
                                             # (modes, platforms, D-Ticket,
                                             # via stops) on many stations
"""
import argparse
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import requests

from efa.parse import (
    SPECIAL_FARE_ATTRS,
    as_list,
    dticket_valid,
    extract_points,
    line_name,
    locality,
    mode_group,
    platform_label,
    short_name,
    transport_attrs,
    via_stops,
)
from services.departures import stop_importance

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


def departures_rapid(base: str, stop_id: str, when: datetime | None = None, limit: int = 40, **extra) -> dict:
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
        "limit": limit,
        "depType": "stopEvents",
        "includeCompleteStopSeq": 1,
        "coordOutputFormat": "WGS84[dd.ddddd]",
    }
    if when:
        # ask for departures at a fixed time instead of "now" (e.g. a
        # weekday morning, so a night-time run isn't all night buses)
        params.update(
            {"itdDate": when.strftime("%Y%m%d"), "itdTime": when.strftime("%H%M"), "itdTripDateTimeDepArr": "dep"}
        )
    params.update(extra)
    r = requests.get(base + "XML_DM_REQUEST", params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.json()


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


# ---------------------------------------------------------------------------
# Survey: run the app's normalisation rules (efa/parse.py) over many stations
# so we can judge them on real data.
# ---------------------------------------------------------------------------

SURVEY_STATIONS = SAMPLE_STATIONS + [
    "Stuttgart Schlossplatz",
    "Stuttgart Marienplatz",
    "Stuttgart Flughafen/Messe",
    "Mannheim Hauptbahnhof",
    "Mannheim Paradeplatz",
    "Karlsruhe Marktplatz",
    "Heidelberg Hauptbahnhof",
    "Heilbronn Hauptbahnhof",
    "Pforzheim Hauptbahnhof",
    "Tübingen Hauptbahnhof",
    "Reutlingen Hauptbahnhof",
    "Konstanz Bahnhof",
    "Friedrichshafen Stadtbahnhof",
    "Meersburg Fähre",
    "Offenburg Bahnhof",
    "Villingen Bahnhof",
    "Aalen Hauptbahnhof",
    "Titisee Bahnhof",
]

def next_weekday_morning() -> datetime:
    day = datetime.now() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.replace(hour=8, minute=0, second=0, microsecond=0)


def survey(stations: list) -> None:
    base = ENDPOINTS["bw"]
    when = next_weekday_morning()
    print(f"Survey of {len(stations)} stations, departures from {when:%a %Y-%m-%d %H:%M}\n")

    modes = defaultdict(lambda: {"n": 0, "examples": []})       # (class, name, group) -> ...
    fares = defaultdict(lambda: {"n": 0, "examples": []})       # (verdict, group, type, attrs) -> ...
    platforms = defaultdict(lambda: {"n": 0, "examples": []})   # raw pattern -> ...
    labels = []                                                 # (station, group, platform, rows)
    total = 0
    importance = stop_importance()                              # the app ranks via stops with the stop database

    for query in stations:
        try:
            stops = [p for p in extract_points(stopfinder(base, query)) if p.get("anyType") == "stop"]
            if not stops:
                print(f"  {query}: no stop found")
                continue
            gid = (stops[0].get("ref") or {}).get("gid") or stops[0].get("stateless")
            events = departures_rapid(base, gid, when=when, limit=80).get("stopEvents") or []
        except (requests.RequestException, ValueError) as e:
            print(f"  {query}: FAILED {e}")
            continue
        print(f"  {stops[0].get('name')}: {len(events)} departures")
        total += len(events)
        time.sleep(0.3)  # be polite to the API

        per_platform = defaultdict(list)
        for e in events:
            t = e.get("transportation") or {}
            product = t.get("product") or {}
            tprops = t.get("properties") or {}
            line = t.get("disassembledName") or tprops.get("trainType") or t.get("name") or "?"
            group = mode_group(t)

            m = modes[(product.get("class"), product.get("name"), group)]
            m["n"] += 1
            if line not in m["examples"] and len(m["examples"]) < 4:
                m["examples"].append(line)

            verdict = {True: "VALID", False: "not valid", None: "unsure (no label)"}[dticket_valid(t)]
            attrs = ", ".join(sorted(transport_attrs(t) & SPECIAL_FARE_ATTRS)) or "-"
            f = fares[(verdict, group, tprops.get("trainType") or "-", attrs)]
            f["n"] += 1
            if line not in f["examples"] and len(f["examples"]) < 4:
                f["examples"].append(line)

            loc = e.get("location") or {}
            lprops = loc.get("properties") or {}
            raw = (lprops.get("platformName") or lprops.get("platform") or "").strip()
            pattern = re.sub(r"\d+", "N", raw) if raw else "(empty)"
            pl = platforms[pattern]
            pl["n"] += 1
            example = f"'{raw}' -> '{platform_label(loc)}'"
            if example not in pl["examples"] and len(pl["examples"]) < 3:
                pl["examples"].append(example)

            # a platform is identified by its own id, not its label: a tram
            # "Gleis 1" and a bus "Pos. 1" at one stop are different places
            per_platform[(group, platform_label(loc), loc.get("id"))].append(e)

        for (group, label, _), evs in sorted(per_platform.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            # one row per distinct line + destination + via on this platform
            rows = Counter()
            for x in evs:
                t = x.get("transportation") or {}
                line = line_name(t)
                dest = short_name((t.get("destination") or {}).get("name") or "?", locality(x.get("location") or {}))
                rows[(line, dest, " · ".join(via_stops(x, importance)))] += 1
            labels.append((stops[0].get("name"), group, label, rows))

    print(f"\n{total} departures in total")

    print("\n\n=== 1. TRANSPORT MODES: what EFA sends -> our group ===")
    print(f"{'class':>5}  {'EFA name':<28} {'our group':<22} {'count':>5}  examples")
    for (cls, name, group), v in sorted(modes.items(), key=lambda kv: (kv[0][2], str(kv[0][0]))):
        print(f"{str(cls):>5}  {str(name):<28} {group:<22} {v['n']:>5}  {', '.join(v['examples'])}")

    print("\n\n=== 2. PLATFORM LABELS: raw format (N = number) -> normalised ===")
    print(f"{'raw pattern':<16} {'count':>5}  examples")
    for pattern, v in sorted(platforms.items(), key=lambda kv: -kv[1]["n"]):
        print(f"{pattern:<16} {v['n']:>5}  {'; '.join(v['examples'])}")

    print("\n\n=== 3. DEUTSCHLANDTICKET: rule result per kind of service ===")
    print(f"{'verdict':<18} {'group':<20} {'type':<6} {'count':>5}  {'fare attributes':<52} examples")
    for (verdict, group, ttype, attrs), v in sorted(fares.items()):
        print(f"{verdict:<18} {group:<20} {ttype:<6} {v['n']:>5}  {attrs:<52} {', '.join(v['examples'])}")

    print("\n\n=== 4. VIA STOPS per departure (line -> destination, via major stops) ===")
    current = None
    with_via = without_via = 0
    for station, group, label, rows in labels:
        if station != current:
            print(f"\n{station}")
            current = station
        print(f"  {group} / {label}")
        for (line, dest, via), n in sorted(rows.items()):
            print(f"      {line:<7} -> {dest:<38} {('via ' + via) if via else '(no via)':<60} x{n}")
            with_via += n if via else 0
            without_via += 0 if via else n

    print(f"\n{with_via} departures with via stops, {without_via} without")


# ---------------------------------------------------------------------------
# Structure survey: how are stations across BW built? Some hubs are several
# linked stations (Stuttgart Hbf oben / tief / Arnulf-Klett-Platz), some are
# one station covering every mode (Freiburg Hbf), most are simple stops.
# ---------------------------------------------------------------------------

STRUCTURE_STATIONS = [
    # Stuttgart region
    "Stuttgart Hauptbahnhof", "Stuttgart Bad Cannstatt", "Stuttgart Vaihingen", "Stuttgart Feuerbach",
    "Stuttgart Charlottenplatz", "Stuttgart Rotebühlplatz", "Stuttgart Flughafen/Messe", "Stuttgart Degerloch",
    "Stuttgart Pragfriedhof", "Esslingen Bahnhof", "Ludwigsburg Bahnhof", "Böblingen Bahnhof",
    "Sindelfingen ZOB", "Waiblingen Bahnhof", "Göppingen Bahnhof", "Plochingen Bahnhof", "Herrenberg Bahnhof",
    "Backnang Bahnhof", "Bietigheim-Bissingen Bahnhof", "Leonberg Bahnhof",
    # Karlsruhe / Rhine-Neckar
    "Karlsruhe Hauptbahnhof", "Karlsruhe Marktplatz", "Karlsruhe Europaplatz", "Karlsruhe Durlach Bahnhof",
    "Mannheim Hauptbahnhof", "Mannheim Paradeplatz", "Heidelberg Hauptbahnhof", "Heidelberg Bismarckplatz",
    "Pforzheim Hauptbahnhof", "Bruchsal Bahnhof", "Rastatt Bahnhof", "Baden-Baden Bahnhof", "Weinheim Hauptbahnhof",
    "Mosbach Bahnhof", "Sinsheim Hauptbahnhof",
    # South / Black Forest / Lake Constance
    "Freiburg Hauptbahnhof", "Freiburg Bertoldsbrunnen", "Offenburg Bahnhof", "Lörrach Hauptbahnhof",
    "Emmendingen Bahnhof", "Kehl Bahnhof", "Villingen Bahnhof", "Schwenningen Bahnhof", "Titisee Bahnhof",
    "Freudenstadt Hauptbahnhof", "Rottweil Bahnhof", "Tuttlingen Bahnhof", "Singen Bahnhof", "Radolfzell Bahnhof",
    "Konstanz Bahnhof", "Friedrichshafen Stadtbahnhof", "Ravensburg Bahnhof", "Meersburg Fähre",
    # East / North
    "Ulm Hauptbahnhof", "Biberach Bahnhof", "Sigmaringen Bahnhof", "Reutlingen Hauptbahnhof", "Tübingen Hauptbahnhof",
    "Aalen Hauptbahnhof", "Schwäbisch Gmünd Bahnhof", "Heidenheim Bahnhof", "Heilbronn Hauptbahnhof",
    "Heilbronn Harmonie", "Schwäbisch Hall Bahnhof", "Crailsheim Bahnhof", "Öhringen Hauptbahnhof",
    "Bad Mergentheim Bahnhof", "Wertheim Bahnhof",
    # small stops
    "Schluchsee Rathaus", "Feldberg Bärental Bahnhof",
]

# mode group -> tab the station page would offer
TAB_OF = {
    "Regional train": "Trains",
    "Long-distance train": "Trains",
    "S-Bahn": "S-Bahn",
    "U-Bahn / Tram": "U-Bahn / Tram",
    "Bus": "Bus",
    "Long-distance bus": "Bus",
    "On-demand": "Bus",
}


def structure(stations: list) -> None:
    base = ENDPOINTS["bw"]
    when = next_weekday_morning()
    print(f"Structure of {len(stations)} stations, departures from {when:%a %Y-%m-%d %H:%M}\n")

    rows = []
    notes = []
    for query in stations:
        try:
            found = [p for p in extract_points(stopfinder(base, query)) if p.get("anyType") == "stop"]
            found = [p for p in found if ((p.get("ref") or {}).get("gid") or "").startswith("de:08")]
            if not found:
                notes.append(f"{query}: no stop found in BW")
                continue
            stop = found[0]
            gid = stop["ref"]["gid"]
            linked_events = departures_rapid(base, gid, when=when, limit=100).get("stopEvents") or []
            own_events = departures_rapid(base, gid, when=when, limit=100, deleteAssignedStops_dm=1).get("stopEvents") or []
        except (requests.RequestException, ValueError, KeyError) as e:
            notes.append(f"{query}: FAILED {e}")
            continue
        time.sleep(0.3)  # be polite to the API

        def station_of(e):
            parent = (e.get("location") or {}).get("parent") or {}
            return parent.get("id"), parent.get("name")

        # other stations whose departures EFA mixes in by default
        linked = sorted({name or "?" for sid, name in map(station_of, linked_events) if sid != gid})
        foreign = [e for e in own_events if station_of(e)[0] != gid]

        zones = defaultdict(lambda: {"modes": Counter(), "platforms": set()})
        modes = Counter()
        labels = defaultdict(set)  # (mode, platform label) -> zones using it
        unknown = 0
        for e in own_events:
            loc = e.get("location") or {}
            area = (loc.get("properties") or {}).get("area")
            group = mode_group(e.get("transportation") or {})
            label = platform_label(loc)
            zones[area]["modes"][group] += 1
            zones[area]["platforms"].add(label)
            modes[group] += 1
            labels[(group, label)].add(area)
            unknown += label == "Unknown platform"

        tabs = sorted({TAB_OF.get(m, m) for m in modes})
        mixed = [a for a, z in zones.items() if len({TAB_OF.get(m, m) for m in z["modes"]}) > 1]
        clashes = [f"{m} {l}" for (m, l), areas in labels.items() if len(areas) > 1 and l != "Unknown platform"]
        top_share = max(modes.values()) / len(own_events) if own_events else 0

        if linked:
            kind = "linked hub"
        elif len(tabs) >= 3:
            kind = "single hub"
        elif len(tabs) == 2:
            kind = "two modes"
        else:
            kind = "simple"

        rows.append({
            "name": stop.get("name"), "kind": kind, "linked": linked, "tabs": tabs, "zones": len(zones),
            "mixed": len(mixed), "clashes": clashes, "unknown": unknown, "n": len(own_events),
            "top": f"{modes.most_common(1)[0][0]} {top_share:.0%}" if modes else "-",
            "others": [p.get("name") for p in found[1:4]],
        })
        if foreign:
            notes.append(f"{stop.get('name')}: {len(foreign)} departures from another station even with linking off")
        if not own_events:
            notes.append(f"{stop.get('name')}: no departures of its own (linked: {linked})")
        print(f"  {stop.get('name')}: {kind}")

    print("\n\n=== STATIONS ===")
    print(f"{'station':<40} {'kind':<11} {'deps':>4} {'zones':>5} {'mixed':>5} {'unkn':>4}  {'tabs':<38} biggest share")
    for r in rows:
        print(f"{r['name'][:39]:<40} {r['kind']:<11} {r['n']:>4} {r['zones']:>5} {r['mixed']:>5} {r['unknown']:>4}  "
              f"{', '.join(r['tabs'])[:37]:<38} {r['top']}")

    print("\n\n=== SUMMARY ===")
    for kind, n in Counter(r["kind"] for r in rows).most_common():
        print(f"  {kind:<11} {n}")

    print("\n\n=== LINKED HUBS: stations EFA mixes in by default ===")
    for r in rows:
        if r["linked"]:
            print(f"  {r['name']}\n      linked: {', '.join(r['linked'])}\n      own tabs: {', '.join(r['tabs']) or '-'}")

    print("\n\n=== SAME PLATFORM LABEL IN DIFFERENT ZONES (same mode) ===")
    for r in rows:
        if r["clashes"]:
            print(f"  {r['name']}: {', '.join(r['clashes'][:8])}")

    print("\n\n=== OTHER SEARCH RESULTS FOR THE SAME QUERY ===")
    for r in rows:
        if r["others"]:
            print(f"  {r['name']}: {'; '.join(r['others'])}")

    print("\n\n=== NOTES ===")
    for note in notes:
        print(f"  {note}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stations", nargs="*", help="station names to probe (default: built-in samples)")
    parser.add_argument("--endpoint", choices=[*ENDPOINTS, "all"], default="all")
    parser.add_argument("--dump", action="store_true", help="save raw JSON responses to debug_output/")
    parser.add_argument("--survey", action="store_true", help="try the normalisation rules on many stations")
    parser.add_argument("--structure", action="store_true", help="how stations across BW are built")
    args = parser.parse_args()

    if args.structure:
        structure(args.stations or STRUCTURE_STATIONS)
        return
    if args.survey:
        survey(args.stations or SURVEY_STATIONS)
        return

    stations = args.stations or SAMPLE_STATIONS
    endpoints = list(ENDPOINTS) if args.endpoint == "all" else [args.endpoint]

    for query in stations:
        for name in endpoints:
            probe(name, query, args.dump)


if __name__ == "__main__":
    main()
