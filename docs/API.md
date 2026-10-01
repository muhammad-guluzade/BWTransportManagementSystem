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
    "departures": "/api/v1/stops/{stop_id}/departures?tab={tab_id}"
  }
}
```

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
  "empty": null
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
| `empty` | `null` if there are departures. Otherwise why there are none: `"nearby"` (nothing departs here, but linked stations in `nearby` have service) or `"no_service"` (the timetable has nothing for this station or the ones next to it). |

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
| `line` | Line label: `"U7"`, `"RE5"`, `"ICE 1291"`. |
| `destination` | Where the vehicle is signed to. The town is left out if it is the station's own. |
| `via` | Up to two major stops on the way, in travel order. Can be empty. |
| `dticket` | `true` if the Deutschlandticket is valid. `false` means not valid or not known. |
| `time` | Expected departure: the live time if there is one, else the planned time. |
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
  "empty": "nearby"
}
```

## Errors

Errors use the matching HTTP status and always have this shape:

```json
{"error": {"code": "unknown_stop", "message": "This stop could not be found."}}
```

| Status | `code` | When |
|---|---|---|
| 404 | `unknown_stop` | The stop id doesn't exist or is outside Baden-Württemberg. |
| 404 | `not_found` | No such API address. |
| 405 | `method_not_allowed` | Anything other than `GET`. |
| 502 | `timetable_unavailable` | The timetable service behind this API didn't answer. Try again later. |

`code` is for programs, `message` for people.

## Where the data comes from

The statewide timetable service of Baden-Württemberg (EFA). It is used
without an official agreement and its format can change; this API hides
that format, so clients only depend on what is documented here.
