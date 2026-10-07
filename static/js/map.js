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
  // marker colours per kind of transport, most important first; a stop gets the
  // colour of the first one that serves it (the legend in index.html matches)
  const MODE_COLOURS = [['rail', '#c1121c'], ['sbahn', '#2e8b47'], ['ubahn', '#005aa9'], ['tram', '#e07b00'], ['bus', '#a1338f']];
  const OTHER_COLOUR = '#666';
  // the stop data gives a few different stops the very same position; their
  // markers are drawn this many pixels apart so that each can be clicked
  const SAME_SPOT_METRES = 3;
  const SAME_SPOT_SHIFT_PX = 16;

  const map = L.map('map', {
    maxBounds: BW.pad(0.1),       // the map can't be dragged away from BW
    maxBoundsViscosity: 1.0,
  });
  map.fitBounds(BW);
  map.setMinZoom(map.getZoom());  // ...or zoomed out beyond it

  // the map images (roads, place names) come from OpenStreetMap's tile servers;
  // their usage policy requires the attribution to stay visible
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19,
  }).addTo(map);

  const stopsLayer = L.layerGroup().addTo(map);
  const selectedLayer = L.layerGroup().addTo(map);
  let requestCounter = 0;   // to ignore answers that arrive out of order
  let selectedId = null;

  // the map measures its box once; measure again whenever the layout changes it
  new ResizeObserver(() => map.invalidateSize()).observe(document.getElementById('map'));

  // a ring around the selected stop
  function drawRing(lat, lon) {
    selectedLayer.clearLayers();
    L.circleMarker([lat, lon], {
      radius: 14, color: '#222', weight: 3, fill: false, interactive: false,
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

  function drawStops(stops) {
    stopsLayer.clearLayers();
    const positions = markerPositions(stops);
    // least important first, so the important markers end up on top
    stops.slice().reverse().forEach(stop => {
      const marker = L.circleMarker(positions.get(stop.id), {
        radius: 5 + Math.min(stop.lines, 30) / 6,   // more lines = bigger, 5 to 10 px
        color: 'white',
        weight: 1.5,
        fillColor: colourOf(stop),
        fillOpacity: 0.9,
      });
      marker.stopId = stop.id;
      marker.bindTooltip(stop.name, { direction: 'top' });
      marker.on('click', () => selectStop(stop.id, stop.name, 'map'));
      stopsLayer.addLayer(marker);
      // the timetable service and the stop database place a station a few
      // metres apart; keep the ring exactly on the marker
      if (stop.id === selectedId) drawRing(marker.getLatLng().lat, marker.getLatLng().lng);
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

  // departures.js announces the stop it is showing
  document.addEventListener('stop-shown', (e) => {
    const { stop, source } = e.detail;
    selectedLayer.clearLayers();
    selectedId = stop.id;
    if (stop.lat === null || stop.lon === null) return;
    const shown = stopsLayer.getLayers().find(marker => marker.stopId === stop.id);
    const at = shown ? shown.getLatLng() : L.latLng(stop.lat, stop.lon);
    drawRing(at.lat, at.lng);
    // picked from the search or a link: go there. Picked on the map: stay put.
    if (source !== 'map') {
      map.setView([stop.lat, stop.lon], Math.max(map.getZoom(), STOP_ZOOM));
    }
  });

  // the stop data's licence requires naming its source wherever it is shown
  async function loadCredits() {
    try {
      const data = (await (await fetch(`${API}/`)).json()).stop_data;
      if (!data) return;
      credits.appendChild(document.createTextNode('Stop data: '));
      const link = el('a', '', data.attribution);
      link.href = data.source;
      link.target = '_blank';
      credits.appendChild(link);
      if (data.version) {
        credits.appendChild(document.createTextNode(` (timetable of ${data.version.replace(/(\d{4})(\d\d)(\d\d)/, '$3.$2.$1')})`));
      }
      if (data.expired) {
        credits.appendChild(el('span', 'late', ' The timetable period of this data has ended; it should be imported again.'));
      }
    } catch { /* the credit line is not worth an error message */ }
  }

  // for looking at the map from the browser console
  window.stopMap = { map, stopsLayer, selectedId: () => selectedId };
})();
