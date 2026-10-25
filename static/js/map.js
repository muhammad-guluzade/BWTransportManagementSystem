// The map: stops in Baden-Württemberg as markers. Clicking one shows its
// departures in the panel (departures.js); picking a stop there moves the map.
// Uses API, el() and selectStop() from departures.js.
(function () {
  const note = document.getElementById('map-note');
  const credits = document.getElementById('credits');

  function showNote(text) {
    note.textContent = text;
    note.hidden = !text;
  }

  loadCredits();

  // L is the global object Leaflet's script creates; without internet it is missing
  if (typeof L === 'undefined') {
    showNote('The map could not be loaded (no internet connection?). Search and departures still work.');
    return;
  }

  // Baden-Württemberg fits inside this rectangle
  const BW = L.latLngBounds([47.5, 7.5], [49.8, 10.5]);
  // how many stops to show at once: enough to be useful, few enough to read
  const MAX_STOPS = 150;
  // how far to zoom in when a stop is picked from the search
  const STOP_ZOOM = 15;
  // ... and when one platform of it is to be shown: close enough to tell
  // platforms 20 metres apart from each other
  const PLATFORM_ZOOM = 17;
  // marker colours per kind of transport, most important first; a stop gets the
  // colour of the first one that serves it (the legend in index.html matches)
  const MODE_COLOURS = [['rail', '#c1121c'], ['sbahn', '#2e8b47'], ['ubahn', '#005aa9'], ['tram', '#e07b00'], ['bus', '#a1338f']];
  const OTHER_COLOUR = '#666';
  // the stop data gives a few different stops the very same position; their
  // markers are drawn this many pixels apart so that each can be clicked
  const SAME_SPOT_METRES = 3;
  const SAME_SPOT_SHIFT_PX = 16;
  // fingers are less exact than a mouse: on touch screens the small dots are
  // drawn a little bigger, and a tap this many pixels beside a dot still hits it
  const TOUCH = window.matchMedia('(pointer: coarse)').matches;
  const TAP_TOLERANCE_PX = TOUCH ? 12 : 3;
  const SMALLEST_DOT_PX = TOUCH ? 7 : 5;

  // Route mode (route.js): a tap on a stop or the map asks "from here or to here?"
  const routeMode = () => document.body.classList.contains('route-mode');

  const map = L.map('map', {
    maxBounds: BW.pad(0.1),       // the map can't be dragged away from BW
    maxBoundsViscosity: 1.0,
    attributionControl: false,    // its credits are in our own control below (shown, then folded into an (i))
  });
  map.fitBounds(BW);
  map.setMinZoom(map.getZoom());  // ...or zoomed out beyond it

  // The map images (roads, place names) come from Stadia Maps, made from
  // OpenStreetMap data: their coloured "OSM Bright" style, or "Alidade Smooth
  // Dark" when the page is dark (menu.js). The credits must stay visible.
  // Without an account Stadia serves pages opened from this computer or the
  // home network; a public address needs a (free) account with them.
  const tileUrl = dark => `https://tiles.stadiamaps.com/tiles/${dark ? 'alidade_smooth_dark' : 'osm_bright'}/{z}/{x}/{y}{r}.png`;
  const tiles = L.tileLayer(tileUrl(window.theme && window.theme.isDark()), {
    maxZoom: 19,
    updateWhenIdle: false,   // load the map's pieces while it is dragged, not only once it stops (phones)
    keepBuffer: 4,           // and keep more of them around the picture, ready for the next move
  }).addTo(map);

  // The licences of the map and the stop data ask for credits on the map.
  // OpenStreetMap's rules: visible when the map appears; they may then fold
  // away (after five seconds, or when the map is first touched), as long as
  // a small (i) in the corner brings them back. The full ones are in the
  // settings.
  const CREDITS_SHOWN_MS = 5000;
  const CREDITS = '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a> contributors '
    + '&middot; &copy; <a href="https://stadiamaps.com/" target="_blank">Stadia Maps</a> '
    + '&middot; &copy; <a href="https://openmaptiles.org/" target="_blank">OpenMapTiles</a> '
    + '&middot; stops: <a href="https://www.nvbw.de/open-data" target="_blank">NVBW</a>';
  const Credits = L.Control.extend({
    options: { position: 'bottomright' },
    onAdd() {
      const box = L.DomUtil.create('div', 'map-credits');
      const text = L.DomUtil.create('div', 'map-credits-text', box);
      text.innerHTML = CREDITS;
      const button = L.DomUtil.create('button', 'map-credits-button', box);
      button.type = 'button';
      button.textContent = 'i';
      button.setAttribute('aria-label', 'Map credits');
      const show = (shown) => {
        text.hidden = !shown;
        button.setAttribute('aria-expanded', String(shown));
      };
      show(true);
      // folded once: after a while, or at the user's first touch of the map
      // (not when the page itself moves the map, e.g. to a stop's address)
      const container = map.getContainer();
      const fold = () => {
        clearTimeout(timer);
        ['pointerdown', 'wheel', 'keydown'].forEach(type => container.removeEventListener(type, foldOnTouch, true));
        if (!box.matches(':hover')) show(false);
      };
      const foldOnTouch = (e) => { if (!box.contains(e.target)) fold(); };
      const timer = setTimeout(fold, CREDITS_SHOWN_MS);
      ['pointerdown', 'wheel', 'keydown'].forEach(type => container.addEventListener(type, foldOnTouch, true));
      button.addEventListener('click', () => show(text.hidden));
      L.DomEvent.disableClickPropagation(box);   // a tap on it is not a tap on the map
      return box;
    },
  });
  new Credits().addTo(map);
  document.addEventListener('theme-changed', (e) => tiles.setUrl(tileUrl(e.detail.dark)));

  // the glow around the selected stop lies under the stops' dots
  map.createPane('glow').style.zIndex = 390;
  // the dots are drawn on a canvas, which can widen the spot a tap hits
  const dotRenderer = L.canvas({ tolerance: TAP_TOLERANCE_PX });
  const stopsLayer = L.layerGroup().addTo(map);
  const selectedLayer = L.layerGroup().addTo(map);
  const platformLayer = L.layerGroup().addTo(map);
  let requestCounter = 0;   // to ignore answers that arrive out of order
  let selectedId = null;
  let platformMark = null;  // the platform marked right now: {id, code, lat, lon}

  // the map measures its box once; measure again whenever the layout changes it
  new ResizeObserver(() => map.invalidateSize()).observe(document.getElementById('map'));

  // On a phone the search box and the departures sheet lie over the map
  // (sheet.js says how much of it they cover). What the map is asked to show
  // goes in the middle of the part that is left free.
  function covered() {
    return (window.sheet && window.sheet.insets()) || { top: 0, bottom: 0 };
  }

  // What the page asks the map to show, it shows at once: a zoom without its
  // animation (on iPhones that animation can get stuck, and the map then
  // draws nothing until it is touched), a far place without gliding there
  // across the state. Only a short move at the same zoom still glides.
  function jumpTo(centre, zoom) {
    const short = zoom === map.getZoom() && map.getBounds().contains(centre);
    map.setView(centre, zoom, { animate: short });
  }

  function showAt(at, zoom) {
    const { top, bottom } = covered();
    if (!top && !bottom) { jumpTo(at, zoom); return; }   // nothing covers the map (wide screens)
    const size = map.getSize();
    const freeMiddle = (top + (size.y - bottom)) / 2;
    const centre = map.project(at, zoom).add([0, size.y / 2 - freeMiddle]);
    jumpTo(map.unproject(centre, zoom), zoom);
  }

  // is this place in the free part of the map, with a little room around it?
  function inSight(at) {
    const { top, bottom } = covered();
    const point = map.latLngToContainerPoint(at);
    const size = map.getSize();
    return point.x > 20 && point.x < size.x - 20 && point.y > top + 20 && point.y < size.y - bottom - 20;
  }

  // touching the map (a tap on an empty spot, or dragging it) means the user
  // wants the map: on a phone the sheet goes down (sheet.js), and a second tap
  // closes an open departure (departures.js); dragging only looks around
  const touched = kind => document.dispatchEvent(new CustomEvent('map-touched', { detail: { kind } }));
  // (a tap that only closes the question of Route mode asks nothing new)
  let popupWasOpen = false;
  map.on('preclick', () => { popupWasOpen = Boolean(routePopup && map.hasLayer(routePopup)); });
  map.on('click', (e) => {
    if (routeMode() && !popupWasOpen) askRoute(e.latlng, { kind: 'place', id: null, name: 'Point on the map', lat: e.latlng.lat, lon: e.latlng.lng });
    touched('tap');
  });
  map.on('dragstart', () => touched('drag'));

  function dotRadius(stop) {
    return Math.max(SMALLEST_DOT_PX, 5 + Math.min(stop.lines, 30) / 6);   // more lines = bigger, up to 10 px
  }

  // the selected stop: a soft glow in its own colour around its dot (whose
  // edge drawStops makes thicker)
  function drawGlow(at, stop) {
    selectedLayer.clearLayers();
    L.circleMarker(at, {
      pane: 'glow', radius: dotRadius(stop) + 13, color: colourOf(stop), weight: 1.5, opacity: 0.55,
      fillColor: colourOf(stop), fillOpacity: 0.28, interactive: false,
    }).addTo(selectedLayer);
  }

  function colourOf(stop) {
    const match = MODE_COLOURS.find(([mode]) => stop.modes.includes(mode));
    return match ? match[1] : OTHER_COLOUR;
  }

  // where to draw each stop: its own position, or a little to the right of it
  // if a more important stop already sits on exactly that spot
  function markerPositions(stops) {
    const positions = new Map();
    const taken = [];
    stops.forEach(stop => {
      const spot = L.latLng(stop.lat, stop.lon);
      const before = taken.filter(other => map.distance(other, spot) < SAME_SPOT_METRES).length;
      taken.push(spot);
      positions.set(stop.id, before === 0 ? spot
        : map.layerPointToLatLng(map.latLngToLayerPoint(spot).add([SAME_SPOT_SHIFT_PX * before, 0])));
    });
    return positions;
  }

  let lastStops = [];   // the stops drawn now, to draw them again when the selection changes

  function drawStops(stops) {
    lastStops = stops;
    stopsLayer.clearLayers();
    const positions = markerPositions(stops);
    // least important first, so the important markers end up on top
    stops.slice().reverse().forEach(stop => {
      const selected = stop.id === selectedId && !routeMode();
      const marker = L.circleMarker(positions.get(stop.id), {
        renderer: dotRenderer,
        radius: dotRadius(stop) + (selected ? 2 : 0),
        color: 'white',
        weight: selected ? 3 : 1.5,
        fillColor: colourOf(stop),
        fillOpacity: 0.9,
        bubblingMouseEvents: false,   // a tap on a stop is not a tap on the empty map
      });
      marker.stopId = stop.id;
      // the name as plain text: Leaflet would read a string as HTML
      const label = document.createElement('span');
      label.textContent = stop.name;
      marker.bindTooltip(label, { direction: 'top' });
      marker.on('click', () => {
        if (routeMode()) askRoute(marker.getLatLng(), { kind: 'stop', id: stop.id, name: stop.name, lat: stop.lat, lon: stop.lon });
        else selectStop(stop.id, stop.name, 'map');
      });
      stopsLayer.addLayer(marker);
      // the timetable service and the stop database place a station a few
      // metres apart; keep the glow exactly on the marker
      if (selected) drawGlow(marker.getLatLng(), stop);
    });
  }

  // ask the API for the stops in view: the best one of each part of the
  // screen when zoomed out, every stop once they all fit
  async function loadStops() {
    const request = ++requestCounter;
    const b = map.getBounds();
    const bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map(n => n.toFixed(5)).join(',');
    let res;
    try {
      res = await fetch(`${API}/stops?bbox=${bbox}&limit=${MAX_STOPS}&spread=1`);
    } catch {
      if (request === requestCounter) showNote('The stops could not be loaded.');
      return;
    }
    if (window.noticeVersion) noticeVersion(res);   // a newer version online? (update.js)
    if (request !== requestCounter) return;  // the map has moved on
    if (!res.ok) {
      let message = 'The stops could not be loaded.';
      try { message = (await res.json()).error.message || message; } catch { /* keep the default */ }
      showNote(message);
      return;
    }
    showNote('');
    drawStops((await res.json()).stops);
  }

  let moveTimer;
  map.on('moveend', () => {
    clearTimeout(moveTimer);
    moveTimer = setTimeout(loadStops, 150);
  });
  loadStops();

  // departures.js says which platform to mark (its heading or one of its
  // departures was clicked), or null to take the mark away
  document.addEventListener('platform-marked', (e) => {
    const platform = e.detail.platform;
    platformLayer.clearLayers();
    platformMark = null;
    if (!platform || platform.lat === null || platform.lon === null) return;

    const at = L.latLng(platform.lat, platform.lon);
    const pin = document.createElement('span');
    pin.className = 'platform-pin';
    pin.textContent = platform.code || '';
    L.marker(at, {
      icon: L.divIcon({ className: 'platform-mark', html: pin, iconSize: [0, 0] }),
      interactive: false, keyboard: false, zIndexOffset: 1000,
    }).addTo(platformLayer);
    platformMark = { id: platform.id, code: platform.code, lat: platform.lat, lon: platform.lon };

    // go there only if it can't be seen as it is: too far out to tell the
    // platforms apart, or outside the free part of the picture
    if (map.getZoom() < PLATFORM_ZOOM) showAt(at, PLATFORM_ZOOM);
    else if (!inSight(at)) showAt(at, map.getZoom());
  });

  // departures.js announces the stop it is showing
  document.addEventListener('stop-shown', (e) => {
    const { stop, source } = e.detail;
    selectedLayer.clearLayers();
    selectedId = stop.id;
    drawStops(lastStops);   // the selected dot gets its thicker edge and its glow
    if (stop.lat === null || stop.lon === null) return;
    const shown = stopsLayer.getLayers().find(marker => marker.stopId === stop.id);
    const at = shown ? shown.getLatLng() : L.latLng(stop.lat, stop.lon);
    if (!shown) drawGlow(at, lastStops.find(s => s.id === stop.id) || { modes: [], lines: 0 });
    // picked from the search or a link: go there. Picked on the map: stay
    // put, unless the panel that came up now covers it (on a phone the
    // sheet; on a wide screen the panel appearing beside the map)
    if (source !== 'map') showAt(L.latLng(stop.lat, stop.lon), Math.max(map.getZoom(), STOP_ZOOM));
    else if (window.sheet && window.sheet.insets()) { if (!inSight(at)) showAt(at, map.getZoom()); }
    else if (!map.getBounds().contains(at)) map.panTo(at);
  });

  // the panel was closed (wide screens): no stop is selected any more
  document.addEventListener('stop-cleared', () => {
    selectedId = null;
    selectedLayer.clearLayers();
    drawStops(lastStops);
  });

  // the stop data's licence requires naming its source wherever it is shown
  async function loadCredits() {
    try {
      const data = (await (await fetch(`${API}/`)).json()).stop_data;
      if (!data) return;
      // in the settings: the stop data's source, as its licence asks
      const line = el('p', '', 'Stops: ');
      const link = el('a', '', data.attribution);
      link.href = data.source;
      link.target = '_blank';
      line.appendChild(link);
      if (data.version) {
        line.appendChild(document.createTextNode(`, timetable of ${data.version.replace(/(\d{4})(\d\d)(\d\d)/, '$3.$2.$1')}`));
      }
      line.appendChild(document.createTextNode(' (licence '));
      const licence = el('a', '', 'dl-de/by-2-0');
      licence.href = 'https://www.govdata.de/dl-de/by-2-0';
      licence.target = '_blank';
      line.appendChild(licence);
      line.appendChild(document.createTextNode(')'));
      if (data.expired) {
        line.appendChild(el('span', 'late', ' The timetable period of this data has ended; it should be imported again.'));
      }
      credits.prepend(line);
    } catch { /* the credit line is not worth an error message */ }
  }

  // ---------- Route mode (route.js) ----------
  // A tap on a stop or on any spot asks whether the journey starts or ends
  // there; the route of the connection opened is drawn in the lines' colours
  // (dashed: on foot), with the start and the destination marked.
  const ROUTE_COLOURS = { u_bahn_tram: '#005aa9', s_bahn: '#2e8b47', trains: '#d63c3c', regional_train: '#d63c3c', long_distance_train: '#c1121c',
    bus: '#a1338f', long_distance_bus: '#a1338f' };
  const WALK_COLOUR = '#6b7280';
  const routeLayer = L.layerGroup();
  // the route lies above the stops' dots (and lets taps through to them)
  const routePane = map.createPane('route');
  routePane.style.zIndex = 450;
  routePane.style.pointerEvents = 'none';
  let routePopup = null;
  let routeShown = '';   // what the map was last fitted to: it moves only when that changes

  function askRoute(at, place) {
    const box = el('div', 'route-pop');
    box.appendChild(el('div', 'route-pop-name', place.name));
    const buttons = el('div', 'route-pop-buttons');
    [['from', 'From here'], ['to', 'To here']].forEach(([end, text]) => {
      const button = el('button', '', text);
      button.type = 'button';
      button.onclick = () => { map.closePopup(); window.route.choose(end, place); };
      buttons.appendChild(button);
    });
    box.appendChild(buttons);
    const { top, bottom } = covered();
    routePopup = L.popup({ autoPanPaddingTopLeft: [12, top + 12], autoPanPaddingBottomRight: [12, bottom + 12], minWidth: 180 })
      .setLatLng(at).setContent(box).openOn(map);
  }

  const lineColour = leg => (leg.kind === 'walk' ? WALK_COLOUR : leg.brand === 'ice' ? '#c1121c' : leg.brand === 'flix' ? '#73d700'
    : ROUTE_COLOURS[leg.mode.id] || '#5b6470');

  function endMark(place, which) {
    if (!place || place.lat === null || place.lon === null || place.lat === undefined) return null;
    const dot = document.createElement('span');
    const marker = L.marker([place.lat, place.lon], {
      icon: L.divIcon({ className: `route-end ${which}${place.kind === 'me' ? ' me' : ''}`, html: dot, iconSize: [0, 0] }),
      keyboard: false, zIndexOffset: 1200,
    });
    const label = document.createElement('span');
    label.textContent = `${which === 'from' ? 'From' : 'To'}: ${place.name}`;
    marker.bindTooltip(label, { direction: 'top', offset: [0, -10] });
    return marker;
  }

  function drawRoute({ active, journey, from, to }) {
    routeLayer.clearLayers();
    if (!active) return;
    const points = [];
    if (journey) {
      journey.legs.forEach(leg => {
        const path = leg.path && leg.path.length > 1 ? leg.path
          : [[leg.from.lat, leg.from.lon], [leg.to.lat, leg.to.lon]].filter(([a, b]) => a !== null && b !== null);
        if (path.length < 2) return;
        points.push(...path);
        const walk = leg.kind === 'walk';
        // a light edge under each line keeps it readable on any map
        if (!walk) L.polyline(path, { pane: 'route', color: '#fff', weight: 9, opacity: 0.8, interactive: false }).addTo(routeLayer);
        L.polyline(path, { pane: 'route', color: lineColour(leg), weight: walk ? 4 : 6, opacity: 0.95, dashArray: walk ? '2 8' : null, lineCap: 'round',
          interactive: false }).addTo(routeLayer);
      });
      // where one ride ends and the next begins
      journey.legs.slice(1).forEach(leg => {
        if (leg.from.lat === null) return;
        L.circleMarker([leg.from.lat, leg.from.lon], { pane: 'route', radius: 5, color: '#1d2228', weight: 2.5, fillColor: '#fff', fillOpacity: 1, interactive: false })
          .addTo(routeLayer);
      });
    }
    [[from, 'from'], [to, 'to']].forEach(([place, which]) => {
      const mark = endMark(place, which);
      if (mark) { mark.addTo(routeLayer); points.push([place.lat, place.lon]); }
    });
    // the map moves only when there is something new to see
    const key = journey ? `j:${journey.id}` : `e:${[from, to].map(p => (p && p.lat !== null ? `${p.lat},${p.lon}` : '')).join('|')}`;
    if (key === routeShown || !points.length) return;
    routeShown = key;
    if (!journey && points.length === 1) {
      if (!inSight(L.latLng(points[0]))) showAt(L.latLng(points[0]), Math.max(map.getZoom(), STOP_ZOOM - 2));
      return;
    }
    // (the free part of the map can be small on a phone: then the margins shrink, or the route is fitted to the whole map)
    let { top, bottom } = covered();
    const free = map.getSize().y - top - bottom;
    const margin = free > 160 ? 30 : 8;
    if (free < 80) top = bottom = 0;
    map.fitBounds(L.latLngBounds(points), { paddingTopLeft: [margin, top + margin], paddingBottomRight: [margin, bottom + margin], maxZoom: 16, animate: false });
  }

  document.addEventListener('route-drawn', (e) => drawRoute(e.detail));
  // the other mode: the departures' selection and pin, or the route
  document.addEventListener('mode-changed', (e) => {
    const route = e.detail.mode === 'route';
    map.closePopup();
    [selectedLayer, platformLayer].forEach(layer => { if (route) map.removeLayer(layer); else layer.addTo(map); });
    if (route) routeLayer.addTo(map); else map.removeLayer(routeLayer);
    routeShown = '';
    drawStops(lastStops);
  });

  // for looking at the map from the browser console
  window.stopMap = { map, stopsLayer, routeLayer, selectedId: () => selectedId, platformMark: () => platformMark };
})();
