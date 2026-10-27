// Where the user is, live on the map (navigator, step 2d). The user's choice:
// live tracking, as in map apps. A round button under the menu shows the
// position and keeps it up to date while the page is open; it is also kept
// up to date by itself while a connection is followed (follow.js), but only
// when the device has already allowed the location (no question pops up
// unasked). The position only helps to find one's way: the following itself
// goes by the clock (GPS fails underground).
//
// A blue dot with a pale circle for how exact it is; grey, "position from N
// min ago", when no new position came for a minute. Watching stops while the
// page is hidden (and saves the battery), and starts again when it is back.
// A second tap on the button moves the map to the dot again.
(function () {
  const button = document.getElementById('locate-me');
  const noteBox = document.getElementById('position-note');
  if (!button || !window.stopMap || !navigator.geolocation) {
    if (button) button.hidden = true;
    return;
  }
  const map = window.stopMap.map;
  const STALE_MS = 60000;
  const NOTE_MS = 9000;
  const MIN_ACCURACY_CIRCLE = 15;   // metres: a smaller circle hides under the dot

  const layer = L.layerGroup().addTo(map);
  let watchId = null;
  let wanted = false;        // the user asked (the button): watched while the page is open
  let last = null;           // {lat, lon, accuracy, at}
  let centreNext = false;    // the next position moves the map there
  let staleTimer = null;
  let noteTimer = null;

  const following = () => Boolean(window.follow && window.follow.active());

  function draw() {
    layer.clearLayers();
    if (!last) return;
    const stale = Date.now() - last.at > STALE_MS;
    const at = [last.lat, last.lon];
    if (last.accuracy > MIN_ACCURACY_CIRCLE) {
      L.circle(at, { radius: last.accuracy, color: stale ? '#8a929c' : '#1a73e8', weight: 1, opacity: 0.4,
        fillColor: stale ? '#8a929c' : '#1a73e8', fillOpacity: 0.1, interactive: false }).addTo(layer);
    }
    const dot = document.createElement('span');
    const mark = L.marker(at, { icon: L.divIcon({ className: `me-live${stale ? ' stale' : ''}`, html: dot, iconSize: [0, 0] }),
      keyboard: false, zIndexOffset: 1300 });
    const label = document.createElement('span');
    const minutes = Math.round((Date.now() - last.at) / 60000);
    label.textContent = stale ? `You were here (position from ${minutes} min ago)` : 'You are here';
    mark.bindTooltip(label, { direction: 'top', offset: [0, -10] });
    mark.addTo(layer);
    button.classList.toggle('stale', stale);
  }

  function found(pos) {
    last = { lat: pos.coords.latitude, lon: pos.coords.longitude, accuracy: Math.round(pos.coords.accuracy || 0), at: Date.now() };
    button.classList.add('on');
    button.classList.remove('busy');
    draw();
    clearTimeout(staleTimer);
    staleTimer = setTimeout(draw, STALE_MS + 500);
    if (centreNext) {
      centreNext = false;
      // map.js moves the map there, in the free part of the screen, at street level
      document.dispatchEvent(new CustomEvent('me-located', { detail: { lat: last.lat, lon: last.lon, accuracy: last.accuracy } }));
    }
    document.dispatchEvent(new CustomEvent('position-changed', { detail: { ...last } }));
  }

  function failed(err) {
    button.classList.remove('busy');
    if (!wanted) { stopWatching(); return; }   // (watched for a followed connection: quietly nothing)
    if (err && err.code === 1) {
      wanted = false;
      stopWatching();
      button.classList.remove('on');
      note('This device does not let the page use your location.', true);
    } else if (!last) {
      note('Your location could not be found right now.', false);
    }
    // (a passing failure with a position shown: the dot turns grey after a minute)
  }

  function note(text, help) {
    if (!noteBox) return;
    noteBox.innerHTML = '';
    noteBox.appendChild(document.createTextNode(`${text} `));
    if (help && window.locationHelp) {
      const link = document.createElement('button');
      link.type = 'button';
      link.className = 'help-link';
      link.textContent = 'How to allow it';
      link.onclick = () => { noteBox.hidden = true; window.locationHelp.open(); };
      noteBox.appendChild(link);
    }
    noteBox.hidden = false;
    clearTimeout(noteTimer);
    noteTimer = setTimeout(() => { noteBox.hidden = true; }, NOTE_MS);
  }

  function startWatching() {
    if (watchId !== null) return;
    watchId = navigator.geolocation.watchPosition(found, failed, { enableHighAccuracy: true, maximumAge: 10000, timeout: 30000 });
  }
  function stopWatching() {
    if (watchId === null) return;
    navigator.geolocation.clearWatch(watchId);
    watchId = null;
  }

  // the location already allowed? (then a followed connection may use it without asking)
  async function allowed() {
    try {
      const status = await navigator.permissions.query({ name: 'geolocation' });
      return status.state === 'granted';
    } catch {
      return false;   // (no way to know: only the button asks)
    }
  }

  async function update() {
    if (document.hidden) { stopWatching(); return; }
    if (wanted) { startWatching(); return; }
    if (following() && await allowed()) { startWatching(); return; }
    stopWatching();
    if (!following() && last && !wanted) { last = null; layer.clearLayers(); button.classList.remove('on', 'stale'); }
  }

  button.addEventListener('click', () => {
    if (last && watchId !== null) {
      // the second tap: back to the dot
      centreNext = false;
      document.dispatchEvent(new CustomEvent('me-located', { detail: { lat: last.lat, lon: last.lon, accuracy: last.accuracy } }));
      wanted = true;
      return;
    }
    wanted = true;
    centreNext = true;
    button.classList.add('busy');
    if (last) found({ coords: { latitude: last.lat, longitude: last.lon, accuracy: last.accuracy } });
    startWatching();
  });

  document.addEventListener('visibilitychange', update);
  document.addEventListener('follow-changed', update);
  update();

  window.position = {
    showing: () => Boolean(last),
    last: () => (last ? { ...last } : null),
    watching: () => watchId !== null,
  };
})();
