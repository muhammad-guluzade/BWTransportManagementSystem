"""
Run this FIRST, before touching app.py.

It calls the same two EFA endpoints app.py uses and prints the raw JSON.
This is an unofficial API, so field names can occasionally change -- this
script lets you check that in 10 seconds instead of debugging a blank UI.

Usage:
    python debug_efa.py
"""
import json
import sys

import requests

BASE = "https://www3.vvs.de/mngvvs/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TransitDemo/0.1)"}


def stopfinder(query: str) -> dict:
    params = {
        "outputFormat": "JSON",
        "type_sf": "any",
        "name_sf": query,
        "stateless": 1,
        "locationServerActive": 1,
        # ask EFA to return real GPS coordinates instead of its internal
        # projected coordinate system -- this is what we're testing today
        "coordOutputFormat": "WGS84[dd.ddddd]",
    }
    r = requests.get(BASE + "XML_STOPFINDER_REQUEST", params=params, headers=HEADERS, timeout=8)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.json()


def departures(stop_id: str) -> dict:
    params = {
        "outputFormat": "JSON",
        "type_dm": "stop",
        "name_dm": stop_id,
        "mode": "direct",
        "useRealtime": 1,
        "limit": 10,
    }
    r = requests.get(BASE + "XML_DM_REQUEST", params=params, headers=HEADERS, timeout=8)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.json()


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "Pragfriedhof"

    print(f"=== STOPFINDER for '{query}' ===")
    sf = stopfinder(query)
    print(json.dumps(sf, indent=2, ensure_ascii=False)[:3000])

    points = sf.get("stopFinder", {}).get("points", [])
    if isinstance(points, dict):
        points = [points]

    # keep only actual stops -- stopfinder also returns POIs, addresses, etc.
    stops = [p for p in points if p.get("anyType") == "stop"]

    if not stops:
        print("\nNo stop found. Look at the raw JSON above -- the response")
        print("shape may not match what this script expects.")
        return

    stop_id = stops[0].get("stateless") or stops[0].get("ref", {}).get("id")
    print(f"\nFound stop id: {stop_id} ({stops[0].get('name')})")

    print("\n--- Coordinate check ---")
    ref = stops[0].get("ref", {})
    print("ref.coords:", ref.get("coords"))
    print("(Expecting something like '9.17702,48.78471' -- a longitude,")
    print(" latitude pair roughly in the 7-11 / 47-50 range for BW.")
    print(" If it's still a big number like '3513488.00,753250.00', the")
    print(" coordOutputFormat param didn't take effect.)")

    print("\n=== DEPARTURES ===")
    dm = departures(stop_id)

    print("Top-level keys:", list(dm.keys()))

    dep_list = dm.get("departureList", [])
    if isinstance(dep_list, dict):
        dep_list = [dep_list]

    print(f"\nNumber of departures returned: {len(dep_list)}")
    print("\n--- First 2 full departure entries ---")
    print(json.dumps(dep_list[:2], indent=2, ensure_ascii=False))

    print("\n--- Check ---")
    print("1. 'Number of departures returned' above should be > 0.")
    print("2. Each entry above should have 'servingLine' and 'dateTime'.")
    print("3. Look for whatever field carries the platform/track number --")
    print("   it may be called 'platformName', 'platform', 'pole', or nested")
    print("   inside 'servingLine' or another sub-object. Whatever it's")
    print("   called here is what app.py's departures() needs to read.")


if __name__ == "__main__":
    main()