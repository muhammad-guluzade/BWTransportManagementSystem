# BW Departures API (v1)

A JSON API for stops and live departures in Baden-Württemberg. The web page
of this project uses it, and it is meant to be usable by other clients too
(for example a mobile app).

- Base address: `/api/v1` (locally `http://localhost:5000/api/v1`)
- All responses are JSON, UTF-8. Only `GET` is used.
- No key or login.
- Only stops inside Baden-Württemberg, and only scheduled public transport:
  no flights, no on-demand taxis or call buses.

## Conventions

**Display text and stable values.** Wherever the API sends a text meant for
people, it also sends the stable value behind it:

| Display text (`name`) | Stable value | Use the stable value for |
|---|---|---|
| `"Regional train"` | `"id": "regional_train"` | translations, icons, colours |
| `"Platform 3"` | `"code": "3"` | your own platform label |
| `"Trains"` (tab) | `"id": "trains"` | requesting that tab |

The display text is English and may be reworded; the stable values will not change within v1.

**Times** are UTC in ISO 8601, e.g. `2026-10-01T08:04:00Z`. Convert them to
local time (Europe/Berlin) when displaying.

**Coordinates** are WGS84 `lat` / `lon`. They are `null` if the timetable
service gives none.

**Versioning.** Within v1, fields may be added but are never renamed or
removed. Clients should ignore fields they don't know.

**Freshness.** Departures are cached for 30 seconds, so asking more often
than that returns the same data.

## Endpoints

### `GET /api/v1/`

Lists the endpoints.

```json
{
  "name": "BW Departures API",
  "version": 1,
  "endpoints": {
    "search_stops": "/api/v1/stops/search?q={text}",
    "stops_in_area": "/api/v1/stops?bbox={west},{south},{east},{north}&limit={n}&spread={0|1}",
    "departures": "/api/v1/stops/{stop_id}/departures?tab={tab_id}",
    "departure_stops": "/api/v1/stops/{stop_id}/departures/{departure_id}/stops"
  },
  "stop_data": {
    "attribution": "Datensatz der NVBW GmbH",
    "source": "https://www.nvbw.de/open-data",
    "version": "20260929",
    "valid_until": "2026-12-12",
    "imported_at": "2026-10-02T01:37:45Z",
    "stations": 29387,
    "expired": false
  }
}
```

`stop_data` describes the stop database behind the "stops in an area"
endpoint: which release of NVBW's timetable file it was built from
(`version`), the last day of the timetable period that file covers
(`valid_until`), and when it was imported. `expired` turns `true` after
`valid_until`: the stops are probably still right, but a newer file should
be imported. `stop_data` is `null` until the database has been built. A
client that shows stops from this API must show `attribution` with a link
to `source`.

### `GET /api/v1/stops/search?q={text}`

Finds stops by name, best match first (at most 15).

| Parameter | | |
|---|---|---|
| `q` | required | Search text. Fewer than 2 characters gives an empty list. |

Example: `/api/v1/stops/search?q=Stuttgart Hauptbahnhof`

```json
{
  "stops": [
    {
      "id": "de:08111:6115",
      "name": "Stuttgart, Hauptbahnhof",
      "lat": 48.784729,
      "lon": 9.183172,
      "types": [
        {"id": "trains", "name": "Trains"},
        {"id": "sbahn", "name": "S-Bahn"}
      ]
    },
    {
      "id": "de:08111:6118",
      "name": "Stuttgart, Hauptbahnhof (tief)",
      "lat": 48.783385,
      "lon": 9.180225,
      "types": [
        {"id": "sbahn", "name": "S-Bahn"}
      ]
    }
  ]
}
```

| Field | |
|---|---|
| `id` | Stop id; use it in the departures endpoint. |
| `name` | Full name including the town. When two results share a name, what stops there is appended (`"Kehl, Bahnhof · Bus"`). |
| `lat`, `lon` | Position of the stop. |
| `types` | Transport types the timetable service lists for the stop. A hint only: it can include a type that has no departures. The departures endpoint returns the verified list as `tabs`. |

### `GET /api/v1/stops?bbox={west},{south},{east},{north}&limit={n}&spread={0|1}`

The stations inside a rectangle, most important first. Made for maps: ask
for the stations of whatever the map currently shows, and you get the
main stations when zoomed out and every stop when zoomed in.

| Parameter | | |
|---|---|---|
| `bbox` | required | The rectangle as `west,south,east,north` in degrees (WGS84), e.g. `9.10,48.74,9.26,48.83`. |
| `limit` | optional | How many stations to return at most. Default 100, maximum 500. |
| `spread` | optional | `1` to pick the stations evenly across the rectangle instead of strictly by importance. Default `0`. |

Without `spread`, a rectangle holding more stations than `limit` returns the
most important ones, which on a map of the whole state all sit in the big
cities. With `spread=1` the rectangle is divided into a grid and the most
important station of each cell is returned, so every region is covered. The
finest grid whose cells all fit into `limit` is used, and the grid is fixed
to the globe, so panning a map keeps (nearly) the same stations. Once all stations of the
rectangle fit into `limit`, both variants return all of them. The response
has the same shape either way; with `spread=1` it can hold fewer than
`limit` stations.

Example: `/api/v1/stops?bbox=9.10,48.74,9.26,48.83&limit=3` (central Stuttgart)

```json
{
  "stops": [
    {
      "id": "de:08111:6115",
      "name": "Stuttgart Hauptbahnhof (oben)",
      "lat": 48.78523,
      "lon": 9.183086,
      "lines": 32,
      "kinds": ["rail", "urban_rail"],
      "modes": ["rail", "ubahn"]
    },
    {
      "id": "de:08111:6112",
      "name": "Hauptbf (Arnulf-Klett-Platz)",
      "lat": 48.783124,
      "lon": 9.181263,
      "lines": 34,
      "kinds": ["rail", "urban_rail", "bus"],
      "modes": ["rail", "ubahn", "bus"]
    },
    {
      "id": "de:08111:6333",
      "name": "Bad Cannstatt",
      "lat": 48.801515,
      "lon": 9.217323,
      "lines": 23,
      "kinds": ["rail", "urban_rail", "bus"],
      "modes": ["rail", "sbahn", "ubahn", "bus"]
    }
  ],
  "total": 427,
  "limit": 3
}
```

| Field | |
|---|---|
| `stops[].id` | Stop id; the same ids as in the search and the departures endpoint. |
| `stops[].name` | Name from the timetable file. Shorter than in the search: often without the town (`"Pragfriedhof"`). |
| `stops[].lat`, `lon` | Position: the middle of the station's platforms (the busiest platform, if they are more than 300 m apart). |
| `stops[].lines` | How many different lines call at the station. |
| `stops[].kinds` | Coarse kinds of transport, from: `rail`, `urban_rail` (S-Bahn, U-Bahn, tram), `bus`, `other`. |
| `stops[].modes` | The same with urban rail split up: `rail`, `sbahn`, `ubahn` (Stuttgart's Stadtbahn), `tram`, `bus`, `other`. Told apart by line name ("S4", "U6", anything else is a tram), which matched the live service for 40 of 41 lines checked. For map symbols; the departures endpoint has the exact types. |
| `total` | How many stations the rectangle contains in all. |
| `limit` | The limit that was applied. |

Stations are ranked by an importance score built from how busy a station
is (scheduled trips calling there) and how connected (lines). Rail counts
most, then urban rail, then bus; rail replacement buses count as buses.

This endpoint reads the stop database, which is built from NVBW's open
timetable file with `python import_stops.py` (see "Where the data comes
from"). Until that has been run, it answers `503 stops_not_imported`.

### `GET /api/v1/stops/{stop_id}/departures?tab={tab_id}`

The departure board of one station: upcoming departures of one transport
type (tab), grouped by mode, then by platform.

| Parameter | | |
|---|---|---|
| `stop_id` | required | A stop `id` from the search. |
| `tab` | optional | A tab `id` from `tabs`. Missing or unknown: the first tab. |

A station shows only its own departures. Stations right next to it (e.g. the
underground part of a main station, or the bus station in front of it) are
listed in `nearby` and have boards of their own.

Example: `/api/v1/stops/de:08111:6115/departures` (shortened to one platform
and one departure)

```json
{
  "stop": {
    "id": "de:08111:6115",
    "name": "Stuttgart, Hauptbahnhof (oben)",
    "lat": 48.784729,
    "lon": 9.183172
  },
  "nearby": [
    {"id": "de:08111:6118", "name": "Stuttgart Hauptbahnhof (tief)", "lat": 48.783385, "lon": 9.180225},
    {"id": "de:08111:6112", "name": "Hauptbf (Arnulf-Klett-Platz)", "lat": 48.78316, "lon": 9.181124}
  ],
  "tabs": [
    {"id": "trains", "name": "Trains"},
    {"id": "sbahn", "name": "S-Bahn"}
  ],
  "tab": "trains",
  "modes": [
    {
      "id": "regional_train",
      "name": "Regional train",
      "flat": false,
      "platforms": [
        {
          "code": "2",
          "name": "Platform 2",
          "area": null,
          "departures": [
            {
              "id": "MjAyNi0xMC0wMVQyMjozNDowMFp8ZGU6MDgxMTE6NjExNToyOjJ8ZGRiOjkwUjkwOiA6SDpqMjZ8MTk5MzQ",
              "line": "MEX90",
              "destination": "Crailsheim",
              "via": ["Bad Cannstatt", "Waiblingen"],
              "dticket": true,
              "time": "2026-10-01T22:38:00Z",
              "planned": "2026-10-01T22:34:00Z",
              "minutes": 63,
              "delay": 4,
              "realtime": true,
              "cancelled": false
            }
          ]
        }
      ]
    }
  ],
  "empty": null,
  "next_service": null
}
```

**Top level**

| Field | |
|---|---|
| `stop` | The station: `id`, `name`, `lat`, `lon`. |
| `nearby` | Stations linked to this one: `id`, `name`, `lat`, `lon`. When `empty` is `"nearby"`, only those that have departures are listed, each with `types` (`[{id, name}]`). |
| `tabs` | Transport types that have departures here: `[{id, name}]`. Empty if the station has none. |
| `tab` | The `id` of the tab this response shows, or `null`. |
| `modes` | The departures, see below. In display order. |
| `empty` | `null` if there are departures. Otherwise why there are none: `"nearby"` (nothing departs here in the next 7 days, but linked stations in `nearby` have service) or `"no_service"` (the timetable has nothing for this station in the next 7 days, nor for the ones next to it). |
| `next_service` | Normally `null`. The timetable service only knows the next 24 hours; a stop served on school days only has nothing there on a Saturday. In that case the API looks up to a week ahead, returns the first departures it finds in `modes` (all transport types together, `tabs` empty) and sets `next_service` to their local date, e.g. `"2026-10-05"`. A client should say so, and show the date with these departures. |

Tab ids: `trains`, `sbahn`, `tram`, `bus`, `ferry`, `cablecar`, `other`.

**`modes[]`**

| Field | |
|---|---|
| `id` | `u_bahn_tram`, `s_bahn`, `regional_train`, `long_distance_train`, `bus`, `long_distance_bus`, `ferry`, `cable_car`, `other`. |
| `name` | Display text, e.g. `"Regional train"`. |
| `flat` | `true` if the mode has fewer than two platforms here; a client may then leave out the platform heading. |
| `platforms` | In display order: numbered, then lettered, unknown last. |

**`modes[].platforms[]`**

| Field | |
|---|---|
| `code` | The bare platform (`"3"`, `"A"`, `"4 Nord"`, `"1(U)"`), or `null` if unknown. |
| `name` | Display text: `"Platform 3"`, `"Unknown platform"`. If two different platforms of a station share a code, the part of the station is appended (`"Platform A · Hauptbahnhof Süd"`). |
| `area` | That part of the station, when it was needed to tell platforms apart; otherwise `null`. |
| `departures` | Sorted by departure time. Up to the next 100 departures of the tab are loaded in total. |

**`modes[].platforms[].departures[]`**

| Field | |
|---|---|
| `id` | Identifies this departure on this station's board. Use it to ask for the departure's stops (next endpoint). It stays the same from one refresh to the next, also when the live time changes. Treat it as an opaque text. |
| `line` | Line label: `"U7"`, `"RE5"`, `"ICE 1291"`. |
| `destination` | Where the vehicle is signed to. The town is left out if it is the station's own. |
| `via` | Up to two major stops on the way, in travel order. Can be empty. |
| `dticket` | `true` if the Deutschlandticket is valid. `false` means not valid or not known. |
| `time` | Expected departure: the live time if there is one, else the planned time. Can be days ahead when `next_service` is set; show the date then. A client shows `planned` and `time` for late departures; `delay` is there for convenience. |
| `planned` | Planned departure. |
| `minutes` | Minutes from now until `time`, never negative. |
| `delay` | Minutes late (negative = early), or `null` without live data. |
| `realtime` | `true` if `time` comes from live data. |
| `cancelled` | `true` if the trip is cancelled. |

**A station without departures** (`/api/v1/stops/de:08337:6575/departures`,
a station whose trains are currently replaced by buses from the bus station):

```json
{
  "stop": {"id": "de:08337:6575", "name": "Waldshut, Bahnhof", "lat": 47.621565, "lon": 8.21919},
  "nearby": [
    {
      "id": "de:08337:4001",
      "name": "Waldshut Busbahnhof",
      "lat": 47.620608,
      "lon": 8.218731,
      "types": [{"id": "trains", "name": "Trains"}, {"id": "bus", "name": "Bus"}]
    }
  ],
  "tabs": [],
  "tab": null,
  "modes": [],
  "empty": "nearby",
  "next_service": null
}
```

### `GET /api/v1/stops/{stop_id}/departures/{departure_id}/stops`

The stops of one departure: where the vehicle goes from this station, and
where it has come from. Meant to be asked for when a user picks a departure
on the board; the board itself only carries the short `via`.

| Parameter | | |
|---|---|---|
| `stop_id` | required | The station whose board the departure is on. |
| `departure_id` | required | The departure's `id` from that board. |

Example: a U6 at Stuttgart, Pragfriedhof (shortened to one stop per list)

```json
{
  "id": "MjAyNi0xMC0wN1QyMTowMDowMFp8ZGU6MDgxMTE6MTE1OjE6MnxzdmU6MjAwMDY6IDpIOmoyNnwyNzQ",
  "line": "U6",
  "destination": "Gerlingen",
  "here": {
    "id": "de:08111:115",
    "name": "Pragfriedhof",
    "lat": 48.79923,
    "lon": 9.18373,
    "has_board": true,
    "platform": {"code": "2", "name": "Platform 2"},
    "time": "2026-10-07T21:02:00Z",
    "planned": "2026-10-07T21:00:00Z",
    "delay": 2,
    "realtime": true,
    "cancelled": false
  },
  "previous": [
    {
      "id": "de:08111:6112",
      "name": "Hauptbf (Arnulf-Klett-Platz)",
      "lat": 48.78316,
      "lon": 9.181124,
      "has_board": true,
      "platform": {"code": "2", "name": "Platform 2"},
      "time": "2026-10-07T20:57:00Z",
      "planned": "2026-10-07T20:56:00Z",
      "delay": 1,
      "realtime": true,
      "cancelled": false
    }
  ],
  "onward": [
    {
      "id": "de:08111:6114",
      "name": "Löwentorbrücke",
      "lat": 48.803683,
      "lon": 9.18354,
      "has_board": true,
      "platform": {"code": "2", "name": "Platform 2"},
      "time": "2026-10-07T21:03:00Z",
      "planned": "2026-10-07T21:01:00Z",
      "delay": 2,
      "realtime": true,
      "cancelled": false
    }
  ]
}
```

| Field | |
|---|---|
| `id`, `line`, `destination` | As on the board. |
| `here` | This station, as one stop of the trip. Its `time`, `planned`, `delay`, `realtime` and `cancelled` are the departure's own. |
| `previous` | The stops the vehicle has come from, in travel order (the first is where the trip started). Empty if the trip starts here. |
| `onward` | The stops still ahead, in travel order (the last is where the trip ends). Can be empty if the timetable gives none. |

**A stop of a trip** (`here`, `previous[]`, `onward[]`)

| Field | |
|---|---|
| `id` | The station's stop id. Use it with the departures endpoint if `has_board` is `true`. |
| `name` | Station name. The town is left out if it is the town of the station the board belongs to. |
| `lat`, `lon` | Where the vehicle stops there (the platform if known). `null` if the timetable gives no position. |
| `has_board` | `true` if the stop is in Baden-Württemberg, so this API has a departure board for it. Stops elsewhere (`"Frankfurt (Main) Hbf"`) are listed, but can't be opened. |
| `platform` | `code` and `name` as on the board; `code` is `null` and `name` `"Unknown platform"` if the timetable names none (common for bus stops). |
| `time` | For stops in `onward`: when the vehicle is expected to arrive there. For stops in `previous` and for `here`: when it leaves (or left). The live time if there is one, else the planned time. |
| `planned` | The planned time. |
| `delay` | Minutes late (negative = early), or `null` without live data. |
| `realtime` | `true` if `time` comes from live data. |
| `cancelled` | `true` if the vehicle doesn't call at this stop. |

Points of a trip where nobody can get on or off (border points of the
railway, routing points of long-distance buses) are left out, as is this
station itself if the vehicle comes round to it again.

The board remembers its departures' stops for about ten minutes, so this
answer normally comes at once. After that the timetable service is asked
again; once a departure has left and the service no longer lists it, the
answer is `unknown_departure`.

## Errors

Errors use the matching HTTP status and always have this shape:

```json
{"error": {"code": "unknown_stop", "message": "This stop could not be found."}}
```

| Status | `code` | When |
|---|---|---|
| 400 | `invalid_parameter` | A parameter is missing or malformed, e.g. `bbox`. The message says which. |
| 404 | `unknown_stop` | The stop id doesn't exist or is outside Baden-Württemberg. |
| 404 | `unknown_departure` | The departure id isn't one of this station's, or the departure is no longer in the timetable (it left a while ago). Load the board again. |
| 404 | `not_found` | No such API address. |
| 405 | `method_not_allowed` | Anything other than `GET`. |
| 502 | `timetable_unavailable` | The timetable service behind this API didn't answer. Try again later. |
| 503 | `stops_not_imported` | The stop database hasn't been built on this server, or was built by an older version (`python import_stops.py`). |

`code` is for programs, `message` for people.

## Where the data comes from

**Search and departures:** the statewide timetable service of
Baden-Württemberg (EFA), asked live. It is used without an official
agreement and its format can change; this API hides that format, so clients
only depend on what is documented here.

**Stops in an area:** a local database built from the open timetable file of
NVBW (Nahverkehrsgesellschaft Baden-Württemberg), which is published twice a
month. Attribution: "Datensatz der NVBW GmbH", <https://www.nvbw.de/open-data>,
licence Datenlizenz Deutschland - Namensnennung - Version 2.0. A client that
shows this data must show that attribution too.

The two sources use the same stop ids. The NVBW file lists a few stops twice,
under a second id the live service doesn't know; the import asks the live
service about such duplicates (same name, less than 30 m apart) and leaves out
the ids it doesn't know, so every stop of this endpoint can be opened in the
departures endpoint.
