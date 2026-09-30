"""
Simple Flask backend for looking up VVS (Stuttgart) stops and showing
live departures grouped by transport mode, then by platform within
each mode.

Data source: the unofficial EFA endpoint that powers vvs.de. It's free,
requires no API key, and needs no registration -- but it's undocumented
and can change without notice. Run debug_efa.py first if anything here
misbehaves.
"""

from flask import Flask, jsonify, render_template, request
import requests

app = Flask(__name__)

EFA_BASE = "https://www3.vvs.de/mngvvs/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TransitDemo/0.1)"}

# EFA's servingLine.name is the human-readable mode (e.g. "Stadtbahn").
# Map the raw values we've seen to a small, consistent set of categories.
# Anything not listed here passes through unchanged rather than being
# silently mislabeled -- if you see an odd category name in the UI, add
# its mapping here.
MODE_LABELS = {
    "Stadtbahn": "U-Bahn",
    "S-Bahn": "S-Bahn",
    "Bus": "Bus",
    "Nachtbus": "Bus",
    "Stadtbus": "Bus",
    "Regionalbus": "Bus",
    "Schnellbus": "Bus",
    "Zug": "Train",
    "Regionalzug": "Train",
    "Fernzug": "Train",
    "Fähre": "Ferry",
    "Seilbahn": "Cable car",
    "Zahnradbahn": "Cable car",
}

# Display order for recognized modes; anything else is appended alphabetically.
MODE_ORDER = ["U-Bahn", "S-Bahn", "Train", "Bus", "Ferry", "Cable car"]


def normalize_mode(raw_name: str) -> str:
    raw_name = (raw_name or "").strip()
    return MODE_LABELS.get(raw_name, raw_name or "Other")


def efa_get(endpoint: str, params: dict) -> dict:
    """Call an EFA endpoint and return parsed JSON."""
    params = {**params, "outputFormat": "JSON"}
    resp = requests.get(EFA_BASE + endpoint, params=params, headers=HEADERS, timeout=8)
    resp.raise_for_status()
    resp.encoding = "utf-8"  # EFA doesn't declare charset correctly; don't let requests guess
    return resp.json()


@app.route("/api/search_stops")
def search_stops():
    query = request.args.get("q", "").strip()
    if len(query) < 2:
        return jsonify([])

    data = efa_get(
        "XML_STOPFINDER_REQUEST",
        {
            "type_sf": "any",
            "name_sf": query,
            "stateless": 1,
            "locationServerActive": 1,
        },
    )

    raw_points = data.get("stopFinder", {}).get("points", [])
    # EFA returns a bare dict instead of a list when there's exactly one match
    if isinstance(raw_points, dict):
        raw_points = [raw_points]

    candidates = []
    for p in raw_points:
        # keep only actual stops -- the stopfinder also returns addresses/POIs
        if p.get("anyType") not in ("stop", None):
            continue
        candidates.append(p)

    def relevance_key(p):
        # quality is EFA's own match-confidence score (0-1000). A major
        # interchange scores far higher than a street-level bus stop that
        # merely shares part of the search text -- this is the main signal.
        try:
            quality = int(p.get("quality", 0))
        except (TypeError, ValueError):
            quality = 0
        # "modes" is a comma-separated list of distinct mode-type codes
        # served at that stop (e.g. "0,1,5" for train+S-Bahn+bus vs just
        # "5" for a bus-only stop). More modes served = more likely to be
        # the main hub the user meant, used as a tiebreaker.
        modes_str = p.get("modes") or ""
        mode_count = len([m for m in modes_str.split(",") if m.strip()])
        return (-quality, -mode_count)

    candidates.sort(key=relevance_key)  # highest quality / most modes first

    results = []
    for p in candidates:
        stop_id = p.get("stateless") or (p.get("ref") or {}).get("id")
        if not stop_id:
            continue
        results.append(
            {
                "id": stop_id,
                "name": p.get("object") or p.get("name", ""),
                "city": p.get("mainLoc", ""),
            }
        )

    # de-duplicate by id, keep order
    seen = set()
    unique = []
    for r in results:
        if r["id"] not in seen:
            seen.add(r["id"])
            unique.append(r)

    return jsonify(unique[:15])


@app.route("/api/departures")
def departures():
    stop_id = request.args.get("stop_id", "").strip()
    if not stop_id:
        return jsonify({"error": "missing stop_id"}), 400

    data = efa_get(
        "XML_DM_REQUEST",
        {
            "type_dm": "stop",
            "name_dm": stop_id,
            "mode": "direct",
            "useRealtime": 1,
            "limit": 40,
        },
    )

    raw_departures = data.get("departureList", [])
    if isinstance(raw_departures, dict):
        raw_departures = [raw_departures]

    # group by mode first (U-Bahn/S-Bahn/Bus/...), then by platform within each mode
    grouped = {}
    for dep in raw_departures:
        line = dep.get("servingLine", {})
        mode = normalize_mode(line.get("name"))

        # "platform" carries the actual track/platform number here;
        # "platformName" is empty for this stop -- fall back to it anyway
        # in case other stops populate it instead.
        platform_raw = (dep.get("platform") or dep.get("platformName") or "").strip()
        platform_name = f"Platform {platform_raw}" if platform_raw else "No platform info"

        # prefer the live time if EFA sent a correction, else the scheduled one
        dt = dep.get("realDateTime") or dep.get("dateTime", {})
        hour = dt.get("hour", "?")
        minute = dt.get("minute", "?")
        time_str = f"{hour:0>2}:{minute:0>2}" if dt else "?"

        countdown = dep.get("countdown")
        try:
            countdown = int(countdown)
        except (TypeError, ValueError):
            countdown = None

        entry = {
            # symbol is the rider-facing line label (e.g. "U7");
            # "number" can be a generic value like "U" for some entries
            "line": line.get("symbol") or line.get("number", "?"),
            "direction": line.get("direction", "?"),
            "time": time_str,
            "minutes": countdown,
            "realtime": line.get("realtime") == "1",
        }

        grouped.setdefault(mode, {}).setdefault(platform_name, []).append(entry)

    def platform_sort_key(name: str):
        digits = "".join(c for c in name if c.isdigit())
        return (0, int(digits)) if digits else (1, name)

    def mode_sort_key(name: str):
        return (MODE_ORDER.index(name), name) if name in MODE_ORDER else (len(MODE_ORDER), name)

    modes_out = {}
    for mode, platforms in grouped.items():
        for entries in platforms.values():
            entries.sort(key=lambda e: (e["minutes"] is None, e["minutes"]))
        # your rule: fewer than 2 distinct platforms within this mode -> flat list
        ordered_platforms = {name: platforms[name] for name in sorted(platforms, key=platform_sort_key)}
        modes_out[mode] = {"flat": len(ordered_platforms) < 2, "platforms": ordered_platforms}

    ordered_modes = {name: modes_out[name] for name in sorted(modes_out, key=mode_sort_key)}

    return jsonify({"modes": ordered_modes})


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)