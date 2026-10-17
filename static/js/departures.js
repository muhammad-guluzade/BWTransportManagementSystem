// This page is one client of the JSON API described in docs/API.md.
const API = '/api/v1';

const input = document.getElementById('stop-input');
const resultsBox = document.getElementById('results');
const stationBox = document.getElementById('station');
const board = document.getElementById('board');
const status = document.getElementById('status');
const rawBox = document.getElementById('raw');
const panelBox = document.getElementById('panel');
const peekBox = document.getElementById('peek');
let debounceTimer;
let refreshTimer;

// How often the departures board gets updated
const REFRESH_MS = 30000;
// How many departures a platform shows at first, and how many more each
// click on "Show more" adds
const PER_PLATFORM = 10;

// what is currently on screen
let currentStopId = null;
let currentTab = '';
let lastData = null;
let lastUrl = '';              // the API address lastData came from
let showRaw = false;           // "Show API response" is switched on
let requestCounter = 0;        // to ignore answers that arrive out of order
let selectedFrom = null;       // where the current stop was picked: 'search', 'link' or 'map'
let announced = true;          // the map has been told about the current stop
const shownCount = new Map();  // platform -> how many departures it shows (after "Show more" clicks)

// the departure whose stops are listed under its row (one at a time)
let openDeparture = null;      // its id
let openTrip = null;           // the API's answer for it; null while it loads
let openTripUrl = '';          // the API address openTrip came from
let openTripError = '';
let showEarlier = false;       // the stops it has come from are shown as well
let openShown = false;         // its row was found while drawing the board
let openPlatform = null;       // the platform it leaves from, see platformKey()

// The platform marked on the map: the one whose heading or departure was
// clicked last. map.js draws the mark when it hears 'platform-marked'.
let markedPlatform = null;     // its key, see platformKey()
let markShown = false;         // it was found while drawing the board

// departure times arrive in UTC; show them in local German time
const timeFormat = new Intl.DateTimeFormat('de-DE', {
  hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Berlin',
});
// the local calendar day of a moment, as YYYY-MM-DD
const dayFormat = new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Berlin' });
// "Mon" and "05.10." for departures that are not today
const weekdayFormat = new Intl.DateTimeFormat('en-GB', { weekday: 'short', timeZone: 'Europe/Berlin' });
const dateFormat = new Intl.DateTimeFormat('de-DE', { day: '2-digit', month: '2-digit', timeZone: 'Europe/Berlin' });
const longDateFormat = new Intl.DateTimeFormat('en-GB', { weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC' });
// when the board was last updated, in the same time as the departures
const clockFormat = new Intl.DateTimeFormat('de-DE', {
  hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'Europe/Berlin',
});
// a clock time alone is ambiguous from this many minutes ahead on
const SHOW_DAY_FROM_MINUTES = 12 * 60;

let searchCounter = 0;         // to ignore search answers that arrive out of order

// After a stop is picked the box shows its name. Tapping the box then makes
// room to type (the name stays as a grey hint); leaving it empty brings the
// name back. Text the user typed is never cleared by a tap, only by the x.
const searchBox = document.getElementById('search-box');
const clearButton = document.getElementById('search-clear');
const PLACEHOLDER = input.placeholder;
let pickedName = '';           // the picked stop's name, as the box shows it

function showClear() {
  searchBox.classList.toggle('has-text', input.value !== '');
}

input.addEventListener('focus', () => {
  if (pickedName && input.value === pickedName) {
    input.placeholder = pickedName;
    input.value = '';
    showClear();
  }
});
input.addEventListener('blur', () => {
  if (pickedName && input.value.trim() === '') input.value = pickedName;
  input.placeholder = PLACEHOLDER;
  showClear();
});
clearButton.addEventListener('click', () => {
  input.value = '';
  searchCounter++;
  showResults([]);
  showClear();
  input.focus();
  input.placeholder = pickedName || PLACEHOLDER;
});

// The results can be chosen from the keyboard: the arrow keys move a mark
// through them, Enter (a phone's return key) takes the marked one, or the
// first one if none is marked. Enter pressed before the results for what was
// typed have arrived takes the first of them once they are there.
let shownStops = [];           // the results in the list, in its order
let shownFor = '';             // the text they were found for
let markedResult = -1;         // the one marked with the arrow keys, -1: none
let enterWaiting = '';         // Enter was pressed for this text before its results came

function markResult(index) {
  markedResult = index;
  [...resultsBox.querySelectorAll('.result')].forEach((row, i) => {
    row.classList.toggle('active', i === markedResult);
    row.setAttribute('aria-selected', String(i === markedResult));
    if (i === markedResult) row.scrollIntoView({ block: 'nearest' });
  });
}

input.addEventListener('keydown', (e) => {
  if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && shownStops.length) {
    e.preventDefault();   // the cursor stays where it is
    const step = e.key === 'ArrowDown' ? 1 : -1;
    markResult(markedResult === -1 ? (step === 1 ? 0 : shownStops.length - 1) : (markedResult + step + shownStops.length) % shownStops.length);
  } else if (e.key === 'Enter') {
    e.preventDefault();
    const typed = input.value.trim();
    if (typed.length < 2) return;
    if (shownFor === typed) {
      const stop = shownStops[markedResult >= 0 ? markedResult : 0];
      if (stop) selectStop(stop.id, stop.name, 'search');
    } else {
      // the results for this text aren't there yet: ask now, take the first when they come
      enterWaiting = typed;
      clearTimeout(debounceTimer);
      searchStops(input.value.replace(/^\s+/, ''));
    }
  }
});

// on a phone: scrolling through the results puts the keyboard away, for more room
resultsBox.addEventListener('touchmove', () => {
  if (document.activeElement === input) input.blur();
}, { passive: true });

input.addEventListener('input', () => {
  enterWaiting = '';   // something else is typed now
  showClear();
  clearTimeout(debounceTimer);
  // a space at the end says the last word is finished, so it is kept
  const q = input.value.replace(/^\s+/, '');
  if (q.trim().length < 2) { searchCounter++; showResults([]); return; }
  debounceTimer = setTimeout(() => searchStops(q), 200);
});


// close the dropdown if you click elsewhere
document.addEventListener('click', (e) => {
  if (!document.getElementById('search-box').contains(e.target)) {
    showResults([]);
  }
});

// small helper: create an element with a class and text
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// API errors look like {error: {code, message}}
async function errorMessage(res, fallback) {
  try {
    return (await res.json()).error.message || fallback;
  } catch {
    return fallback;
  }
}

// where the map is and how far it shows: stops there come first in the
// search. '' if the map could not be loaded.
function mapPlace() {
  const map = window.stopMap && window.stopMap.map;
  if (!map) return '';
  const centre = map.getCenter();
  const radius = map.distance(centre, map.getBounds().getNorthEast()) / 1000;
  return `&near=${centre.lat.toFixed(5)},${centre.lng.toFixed(5)}&radius=${radius.toFixed(1)}`;
}

// the list under the search box; `nothingFor` is the text nothing was found for
function showResults(stops, nothingFor = '') {
  resultsBox.innerHTML = '';
  shownStops = stops;
  shownFor = nothingFor;
  markedResult = -1;
  input.setAttribute('aria-expanded', String(stops.length > 0));
  stops.forEach(stop => {
    const row = el('div', 'result');
    row.setAttribute('role', 'option');
    row.appendChild(el('span', 'result-name', stop.name));
    // what stops there tells a railway station from the bus stop in front of it
    if (stop.types.length) row.appendChild(el('span', 'result-types', stop.types.map(t => t.name).join(', ')));
    row.onclick = () => selectStop(stop.id, stop.name, 'search');
    resultsBox.appendChild(row);
  });
  if (!stops.length && nothingFor) resultsBox.appendChild(el('div', 'no-result', `No stop found for "${nothingFor}".`));
  // no stop fits every word: these fit some of them
  if (stops.length && stops.every(stop => stop.partial)) {
    resultsBox.prepend(el('div', 'no-result', `No stop matches every word of "${nothingFor}". Stops that match part of it:`));
  }
  // Enter was pressed for exactly this text before its results came: take the first
  if (enterWaiting && enterWaiting === nothingFor) {
    enterWaiting = '';
    if (stops.length) selectStop(stops[0].id, stops[0].name, 'search');
  }
}

async function searchStops(q) {
  const request = ++searchCounter;
  let res;
  try {
    res = await fetch(`${API}/stops/search?q=${encodeURIComponent(q)}${mapPlace()}`);
  } catch {
    if (request === searchCounter) status.textContent = 'Could not search stops.';
    return;
  }
  if (request !== searchCounter) return;  // an answer to what was typed before
  if (!res.ok) {
    showResults([]);
    status.textContent = await errorMessage(res, 'Could not search stops.');
    return;
  }
  const { stops } = await res.json();
  if (request !== searchCounter) return;
  showResults(stops, q.trim());
}

// `source` says where the stop was picked ('search', 'link' or 'map'), so the
// map knows whether it should move there
function selectStop(id, name, source = 'search') {
  input.value = name;
  pickedName = name;
  input.blur();      // on a phone: the keyboard goes away
  showClear();
  document.body.classList.add('has-stop');   // wide screens: the panel appears
  searchCounter++;   // an answer still on its way must not open the list again
  showResults([]);
  currentStopId = id;
  selectedFrom = source;
  // on a phone the sheet comes up to show the departures (sheet.js)
  document.dispatchEvent(new CustomEvent('stop-picked', { detail: { id, source } }));
  announced = false;
  currentTab = '';
  shownCount.clear();
  closeStops();
  clearPlatformMark();
  stationBox.innerHTML = '';
  board.innerHTML = '';
  rawBox.innerHTML = '';
  peekBox.innerHTML = '';
  lastData = null;
  status.textContent = `Loading departures for ${name}...`;
  loadDepartures();
  clearInterval(refreshTimer);
  refreshTimer = setInterval(loadDepartures, REFRESH_MS);
}

// the panel's close button (wide screens): back to the start, the map alone
function closeStop() {
  requestCounter++;   // an answer still on its way is not wanted any more
  clearInterval(refreshTimer);
  currentStopId = null;
  currentTab = '';
  lastData = null;
  shownCount.clear();
  closeStops();
  clearPlatformMark();
  stationBox.innerHTML = '';
  board.innerHTML = '';
  rawBox.innerHTML = '';
  peekBox.innerHTML = '';
  status.textContent = 'Search a stop or tap one on the map.';
  input.value = '';
  pickedName = '';
  showClear();
  document.body.classList.remove('has-stop');
  document.dispatchEvent(new CustomEvent('stop-cleared'));
}
document.getElementById('panel-close').addEventListener('click', closeStop);

// On a phone, a tap on the empty map first lowers the sheet (sheet.js); a
// second tap, with the sheet already down, gives up the stop: its open
// departure, the platform's pin and the stop itself, back to the start.
// Dragging the map to look around changes nothing. (This listener runs
// before sheet.js's, so it sees the sheet as it was before the tap.)
document.addEventListener('map-touched', (e) => {
  // (only in the "Sheet" layout: the two screens' map always shows the small bar, so a single tap would do it)
  if (e.detail.kind !== 'tap' || !window.sheet || window.sheet.mode() !== 'sheet' || window.sheet.state() !== 'peek' || !currentStopId) return;
  closeStop();
});

// closes the open departure and takes its platform's mark off the map
function closeOpen() {
  closeStops();
  clearPlatformMark();
  render();
}

function selectTab(tabId) {
  currentTab = tabId;
  document.querySelectorAll('.tab').forEach(button => button.classList.toggle('active', button.dataset.tab === tabId));
  shownCount.clear();
  closeStops();
  clearPlatformMark();
  board.innerHTML = '';
  status.textContent = 'Loading...';
  loadDepartures();
}

async function loadDepartures() {
  const request = ++requestCounter;
  // trains=together: a platform's ICEs among its regional trains (the user's choice)
  const url = `${API}/stops/${encodeURIComponent(currentStopId)}/departures?tab=${encodeURIComponent(currentTab)}&trains=together`;
  let res;
  try {
    res = await fetch(url);
  } catch {
    if (request === requestCounter) showLoadError('Could not load departures.', true);
    return;
  }
  if (request !== requestCounter) return;  // the user has moved on
  if (!res.ok) {
    const message = await errorMessage(res, 'Could not load departures.');
    if (request === requestCounter) showLoadError(message, res.status >= 500);
    return;
  }
  lastData = await res.json();
  lastUrl = url;
  currentTab = lastData.tab || '';
  render();
  // an open list of stops gets fresh times together with the board
  if (openDeparture) loadStops();
  if (!announced) {
    // tell the map which stop is shown now (once per selection, not per refresh)
    announced = true;
    // the box shows the stop's full name, unless the user is typing in it already
    pickedName = lastData.stop.name;
    if (document.activeElement !== input) input.value = pickedName;
    showClear();
    document.dispatchEvent(new CustomEvent('stop-shown', { detail: { stop: lastData.stop, source: selectedFrom } }));
  }
  status.textContent = 'Updated ' + clockFormat.format(new Date());
}

// A failed refresh keeps the board that is shown; a board that was still
// empty (a new stop or tab) shows the message in its place. `retrying`: the
// failure may pass (no connection, the timetable service down)
function showLoadError(message, retrying) {
  status.textContent = message;
  if (!board.hasChildNodes()) {
    board.appendChild(el('div', 'notice', retrying ? `${message} The page tries again every 30 seconds.` : message));
  }
}

function render() {
  // Drawn anew every time. What the user looks at stays where it is: the
  // departures at the top of the visible list are noted with their places,
  // and the first of them still there is put back at its place (departures
  // above it may have left in the meantime; it may have left itself).
  const scrolled = panelBox.scrollTop;
  const putBack = holdList();
  openShown = false;
  markShown = false;
  renderStation(lastData);
  renderBoard(lastData);
  // the departure whose stops were open is no longer on the board (it has left)
  if (openDeparture && !openShown) closeStops();
  // ... and so is the marked platform, once nothing departs from it any more
  if (markedPlatform && !markShown) clearPlatformMark();
  renderPeek(lastData);
  renderRaw();
  panelBox.scrollTop = scrolled;
  putBack();
}

// Notes the first few departures in the visible part of the list (below the
// panel's head, which stays on top) with their places in the panel; the
// function it returns scrolls the list so that the first of them still there
// is at its place again. Used around redrawing the board, and by sheet.js
// around a change of the sheet's height (its head is shorter at the bar).
function holdList() {
  const panelTop = () => panelBox.getBoundingClientRect().top;
  const below = document.getElementById('panel-head').getBoundingClientRect().bottom - panelTop();
  const held = [...board.querySelectorAll('tr.departure')]
    .map(row => [row.dataset.id, row.getBoundingClientRect().top - panelTop()])
    .filter(([, top]) => top >= below - 1)
    .slice(0, 8);
  return () => {
    for (const [id, top] of held) {
      const row = board.querySelector(`tr.departure[data-id="${CSS.escape(id)}"]`);
      if (row) { panelBox.scrollTop += (row.getBoundingClientRect().top - panelTop()) - top; return; }
    }
  };
}

// the small bar of the sheet on a phone (style.css shows it only there): the
// next departure of the marked platform, or of the whole stop
let peekShows = null;          // [departure, mode, platform] the small bar shows, or null

function renderPeek(data) {
  peekBox.innerHTML = '';
  peekShows = null;
  // a departure is open: the bar shows it, with a button to close it
  const open = openDeparture && data.modes.flatMap(mode => mode.platforms.flatMap(platform =>
    platform.departures.filter(dep => dep.id === openDeparture).map(dep => [dep, mode, platform])))[0];
  if (open) {
    peekShows = open;
    const [dep, mode] = open;
    peekBox.appendChild(el('span', badgeClass(dep, mode), dep.line));
    peekBox.appendChild(el('span', 'destination', dep.destination));
    peekBox.appendChild(el('span', 'countdown', waitText(dep)));
    const close = el('button', 'peek-close', '✕');
    close.type = 'button';
    close.setAttribute('aria-label', 'Close this departure');
    close.onclick = (e) => { e.stopPropagation(); closeOpen(); };
    peekBox.appendChild(close);
    return;
  }
  const candidates = [];
  data.modes.forEach(mode => mode.platforms.forEach(platform => {
    if (markedPlatform && platformKey(mode, platform) !== markedPlatform) return;
    platform.departures.forEach(dep => { if (!dep.cancelled) candidates.push([dep, mode, platform]); });
  }));
  if (!candidates.length) {
    peekBox.appendChild(el('span', 'destination', data.modes.length ? 'No further departures shown' : 'No departures soon'));
    return;
  }
  peekShows = candidates.reduce((best, c) => (c[0].time < best[0].time ? c : best));
  const [dep, mode] = peekShows;
  peekBox.appendChild(el('span', badgeClass(dep, mode), dep.line));
  peekBox.appendChild(el('span', 'destination', dep.destination));
  peekBox.appendChild(el('span', 'countdown', waitText(dep)));
}

// A tap on the departure in the small bar: the departures come up, the list
// goes to that departure and its stops open, as if it had been tapped there.
// (A tap on the stop's name in the bar only opens the departures: sheet.js.)
peekBox.addEventListener('click', (e) => {
  if (!peekShows || e.target.closest('.peek-close') || (window.sheet && window.sheet.wasDragged())) return;
  e.stopPropagation();
  const [dep, mode, platform] = peekShows;
  document.dispatchEvent(new CustomEvent('sheet-request', { detail: { state: 'list' } }));
  if (openDeparture !== dep.id) toggleStops(dep, mode, platform);
  scrollToOpen();
});

// a line's badge: coloured by its kind of transport, ICE and Flix by their own colours
function badgeClass(dep, mode) {
  return `line-badge mode-${mode.id}${dep.brand ? ` brand-${dep.brand}` : ''}`;
}

// "3 min", "now", "2 h 5 min"; '' without a time
function waitText(dep) {
  if (dep.minutes === null || dep.minutes === undefined) return '';
  return dep.minutes === 0 ? 'now' : formatWait(dep.minutes);
}

// what tells one platform of the board from the others: two platforms can
// share a name (two bus bays numbered 1), and two names can share a place
function platformKey(mode, platform) {
  return `${mode.id}|${platform.id}|${platform.name}`;
}

// `reason`: 'heading' (its "Show on map" was tapped) or 'departure' (one of
// its departures was): on a phone the sheet makes room only for the first
function markPlatform(mode, platform, reason) {
  if (platform.lat === null || platform.lon === null) {
    clearPlatformMark();  // the timetable service doesn't say where it is
    return;
  }
  markedPlatform = platformKey(mode, platform);
  document.dispatchEvent(new CustomEvent('platform-marked', { detail: { platform, reason } }));
}

function clearPlatformMark() {
  if (markedPlatform === null) return;
  markedPlatform = null;
  document.dispatchEvent(new CustomEvent('platform-marked', { detail: { platform: null } }));
}

function closeStops() {
  openDeparture = null;
  openPlatform = null;
  openTrip = null;
  openTripUrl = '';
  openTripError = '';
  showEarlier = false;
}

// a click on a departure lists its stops under it and marks its platform on
// the map; a second click closes the list and takes the mark away
function toggleStops(dep, mode, platform) {
  if (openDeparture === dep.id) {
    closeStops();
    if (markedPlatform === platformKey(mode, platform)) clearPlatformMark();
    render();
    return;
  }
  closeStops();
  openDeparture = dep.id;
  openPlatform = platformKey(mode, platform);
  markPlatform(mode, platform, 'departure');
  render();
  // on a phone with the sheet at half height, bring the opened departure up so its stops can be seen
  if (window.sheet && window.sheet.state() === 'half') scrollToOpen();
  loadStops();
}

function scrollToOpen() {
  const row = board.querySelector('tr.departure.open');
  if (!row) return;
  const below = document.getElementById('panel-head').getBoundingClientRect().bottom;
  panelBox.scrollTop += row.getBoundingClientRect().top - below - 4;
}

async function loadStops() {
  const id = openDeparture;
  const stopId = currentStopId;
  const url = `${API}/stops/${encodeURIComponent(stopId)}/departures/${encodeURIComponent(id)}/stops`;
  const current = () => id === openDeparture && stopId === currentStopId;
  let res;
  try {
    res = await fetch(url);
  } catch {
    // keep a list that is already shown; only a first load has nothing to show
    if (current() && !openTrip) { openTripError = 'Could not load the stops.'; render(); }
    return;
  }
  if (!current()) return;  // the user has moved on
  if (!res.ok) {
    const message = await errorMessage(res, 'Could not load the stops.');
    if (!current()) return;
    openTrip = null;
    openTripError = message;
  } else {
    const trip = await res.json();
    if (!current()) return;
    openTrip = trip;
    openTripUrl = url;
    openTripError = '';
  }
  render();
}

// "Show API response": the exact JSON this page was built from, for
// checking what the API sends without any extra tools
function renderRaw() {
  rawBox.innerHTML = '';
  const toggle = el('button', 'raw-toggle', showRaw ? 'Hide API response' : 'Show API response');
  toggle.onclick = () => { showRaw = !showRaw; renderRaw(); };
  rawBox.appendChild(toggle);
  if (!showRaw) return;

  const source = el('div', 'raw-url', 'GET ');
  const link = el('a', '', lastUrl);
  link.href = lastUrl;
  link.target = '_blank';
  source.appendChild(link);
  rawBox.appendChild(source);
  rawBox.appendChild(el('pre', 'raw-json', JSON.stringify(lastData, null, 2)));

  if (openTrip) {
    // the stops of the departure that is open come from a request of their own
    const tripSource = el('div', 'raw-url', 'GET ');
    const tripLink = el('a', '', openTripUrl);
    tripLink.href = openTripUrl;
    tripLink.target = '_blank';
    tripSource.appendChild(tripLink);
    rawBox.appendChild(tripSource);
    rawBox.appendChild(el('pre', 'raw-json raw-trip', JSON.stringify(openTrip, null, 2)));
  }
}

// a link that opens another station
function stopLink(stop) {
  const link = el('a', '', stop.name);
  link.href = '#';
  link.onclick = (e) => { e.preventDefault(); selectStop(stop.id, stop.name, 'link'); };
  return link;
}

function renderStation(data) {
  stationBox.innerHTML = '';
  stationBox.appendChild(el('h2', 'station-name', data.stop.name));

  // when the board is empty, or only has departures days ahead, the nearby
  // stations are part of the notice on the board instead
  if (data.nearby.length && !data.empty && !data.next_service) {
    const nearby = el('div', 'nearby', 'Also nearby: ');
    data.nearby.forEach((stop, i) => {
      if (i > 0) nearby.appendChild(document.createTextNode(' · '));
      nearby.appendChild(stopLink(stop));
    });
    stationBox.appendChild(nearby);
  }

  if (data.tabs.length > 1) {
    const tabs = el('div', 'tabs');
    data.tabs.forEach(tab => {
      const button = el('button', tab.id === data.tab ? 'tab active' : 'tab', tab.name);
      button.dataset.tab = tab.id;
      button.onclick = () => selectTab(tab.id);
      tabs.appendChild(button);
    });
    stationBox.appendChild(tabs);
  }
}

function formatTime(iso) {
  return iso ? timeFormat.format(new Date(iso)) : '?';
}

// "Mon 05.10." if the departure is on another day and far enough away that
// the clock time alone could be misread; otherwise ''
function dayLabel(dep) {
  if (!dep.time || dep.minutes === null || dep.minutes < SHOW_DAY_FROM_MINUTES) return '';
  const when = new Date(dep.time);
  if (dayFormat.format(when) === dayFormat.format(new Date())) return '';
  return `${weekdayFormat.format(when)} ${dateFormat.format(when)}`;
}

// the time of a departure or of one stop of a trip: late = planned time
// crossed out, then the new time
function fillTime(cell, item) {
  if (item.cancelled) {
    cell.appendChild(el('s', '', formatTime(item.planned || item.time)));
    cell.appendChild(el('span', 'late', ' Cancelled'));
  } else if (item.delay >= 1) {
    cell.appendChild(el('s', '', formatTime(item.planned)));
    cell.appendChild(document.createTextNode(' '));
    cell.appendChild(el('span', 'late', formatTime(item.time)));
  } else {
    cell.appendChild(document.createTextNode(formatTime(item.time)));
  }
}

// one stop of the open departure's trip; `kind` is 'earlier', 'here' or ''
function renderStop(stop, kind) {
  const row = el('tr', ['stop', kind, stop.cancelled ? 'cancelled' : ''].filter(Boolean).join(' '));
  const timeCell = el('td', 'stop-time');
  fillTime(timeCell, stop);
  row.appendChild(timeCell);

  const nameCell = el('td', 'stop-name');
  // stops in Baden-Württemberg have a board of their own; the others are just named
  if (stop.has_board && kind !== 'here') nameCell.appendChild(stopLink(stop));
  else nameCell.textContent = stop.name;
  row.appendChild(nameCell);

  row.appendChild(el('td', 'stop-platform', stop.platform.code ? stop.platform.name : ''));
  return row;
}

// the row under an open departure: the stops still ahead, and on request the
// ones the vehicle has come from
function renderStops() {
  const row = el('tr', 'stops-row');
  const cell = el('td', 'stops-cell');
  cell.colSpan = 3;
  row.appendChild(cell);

  // on a phone, when the sheet covers the map (style.css shows it only then)
  if (markedPlatform) {
    const toMap = el('button', 'stops-map', 'Show platform on map');
    toMap.onclick = (e) => {
      e.stopPropagation();
      document.dispatchEvent(new CustomEvent('sheet-request', { detail: { state: 'half' } }));
    };
    cell.appendChild(toMap);
  }

  if (openTripError) {
    cell.appendChild(el('div', 'stops-note', openTripError));
    return row;
  }
  if (!openTrip) {
    cell.appendChild(el('div', 'stops-note', 'Loading stops...'));
    return row;
  }

  const trip = openTrip;
  if (trip.previous.length) {
    const count = trip.previous.length;
    const label = showEarlier ? 'Hide earlier stops' : `Show ${count} earlier ${count === 1 ? 'stop' : 'stops'}`;
    const earlier = el('button', 'stops-earlier', label);
    earlier.onclick = () => { showEarlier = !showEarlier; render(); };
    cell.appendChild(earlier);
  }

  const list = el('table', 'stops-list');
  if (showEarlier) {
    trip.previous.forEach(stop => list.appendChild(renderStop(stop, 'earlier')));
    // this station, as the line between where it came from and where it goes
    list.appendChild(renderStop(trip.here, 'here'));
  }
  trip.onward.forEach(stop => list.appendChild(renderStop(stop, '')));
  if (list.rows.length) cell.appendChild(list);
  if (!trip.onward.length) cell.appendChild(el('div', 'stops-note', 'No further stops are known for this departure.'));
  return row;
}

function renderDeparture(dep, mode, platform) {
  const row = el('tr', dep.cancelled ? 'cancelled' : '');
  row.classList.add('departure');
  row.dataset.id = dep.id;
  if (dep.id === openDeparture) row.classList.add('open');
  row.title = dep.id === openDeparture ? 'Hide the stops' : 'Show the stops, and the platform on the map';
  row.onclick = () => toggleStops(dep, mode, platform);

  const lineCell = el('td', 'line-cell');
  lineCell.appendChild(el('span', badgeClass(dep, mode), dep.line));
  row.appendChild(lineCell);

  // destination, D-Ticket label, and the major stops on the way
  const directionCell = el('td');
  const direction = el('div', 'direction');
  direction.appendChild(el('span', 'destination', dep.destination));
  if (dep.dticket) direction.appendChild(el('span', 'dticket', 'D-Ticket'));
  // a bus running in place of a train, even where it is listed among the trains
  if (dep.replacement) direction.appendChild(el('span', 'replacement', 'Replacement bus'));
  directionCell.appendChild(direction);
  if (dep.via.length) {
    directionCell.appendChild(el('div', 'via', 'via ' + dep.via.join(' · ')));
  }
  row.appendChild(directionCell);

  // the wait first, large; the clock time under it (late: planned crossed out, new in red)
  const when = el('td', 'when');
  when.appendChild(el('div', dep.cancelled ? 'countdown gone' : 'countdown', dep.cancelled ? 'Cancelled' : waitText(dep)));
  const clock = el('div', 'clock');
  const day = dayLabel(dep);
  if (day) clock.appendChild(el('span', 'day', day + ' '));
  if (dep.cancelled) clock.appendChild(el('s', '', formatTime(dep.planned || dep.time)));
  else fillTime(clock, dep);
  when.appendChild(clock);
  row.appendChild(when);
  return row;
}

// 45 -> "45 min", 167 -> "2 h 47 min", 3269 -> "2 d 6 h"
function formatWait(minutes) {
  if (minutes < 60) return `${minutes} min`;
  if (minutes < 24 * 60) return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
  return `${Math.floor(minutes / 1440)} d ${Math.floor((minutes % 1440) / 60)} h`;
}

function renderPlatform(mode, platform) {
  const block = el('div', 'platform-block');
  const key = platformKey(mode, platform);
  const marked = key === markedPlatform;
  if (marked) markShown = true;
  if (!mode.flat) {
    // the heading shows the platform on the map; its hint text is in the stylesheet
    const title = el('div', marked ? 'platform-title marked' : 'platform-title', platform.name);
    title.title = marked ? 'Take the mark off the map' : 'Show this platform on the map';
    title.onclick = () => {
      if (marked) clearPlatformMark();
      else {
        // another platform is wanted now: a departure open elsewhere closes (its mark is gone anyway)
        if (openDeparture && openPlatform !== key) closeStops();
        markPlatform(mode, platform, 'heading');
      }
      render();
    };
    block.appendChild(title);
  }

  const count = Math.min(shownCount.get(key) || PER_PLATFORM, platform.departures.length);
  const hidden = platform.departures.length - count;

  const table = el('table');
  platform.departures.slice(0, count).forEach(dep => {
    table.appendChild(renderDeparture(dep, mode, platform));
    if (dep.id === openDeparture && !openShown) {
      openShown = true;
      table.appendChild(renderStops());
    }
  });
  block.appendChild(table);

  if (hidden > 0) {
    // ten more per click, so the list grows step by step
    const more = el('button', 'show-more', `Show ${Math.min(PER_PLATFORM, hidden)} more`);
    more.onclick = () => { shownCount.set(key, count + PER_PLATFORM); render(); };
    block.appendChild(more);
  }
  return block;
}

// the stations next door that have departures, with what leaves there
function nearbyList(data) {
  const list = el('ul');
  data.nearby.forEach(stop => {
    const item = el('li');
    item.appendChild(stopLink(stop));
    if (stop.types && stop.types.length) {
      item.appendChild(document.createTextNode(' (' + stop.types.map(t => t.name).join(', ') + ')'));
    }
    list.appendChild(item);
  });
  return list;
}

// an empty board says why it is empty
function renderEmpty(data) {
  const notice = el('div', 'notice');
  if (data.empty === 'nearby') {
    notice.appendChild(el('p', '', 'Nothing departs from this station itself, but there are departures right next to it:'));
    notice.appendChild(nearbyList(data));
  } else {
    notice.appendChild(el('p', '', 'The timetable has no departures for this station in the next 7 days. '
      + 'It may be served only in certain periods (school terms, a season), or the line may be closed.'));
  }
  return notice;
}

function renderBoard(data) {
  board.innerHTML = '';
  if (data.modes.length === 0) {
    board.appendChild(renderEmpty(data));
    return;
  }

  if (data.next_service) {
    // nothing in the next 24 hours: these are the next departures after that
    const notice = el('div', 'notice');
    const date = longDateFormat.format(new Date(data.next_service + 'T12:00:00Z'));
    if (data.nearby.length) {
      // e.g. a railway station whose trains are replaced by buses from the bus station
      notice.appendChild(el('p', '', 'Nothing departs here in the next 24 hours. Right now there are departures next to this station:'));
      notice.appendChild(nearbyList(data));
      notice.appendChild(el('p', '', `Here, the next departures are on ${date}:`));
    } else {
      notice.appendChild(el('p', '', `Nothing departs here in the next 24 hours. The next departures are on ${date}:`));
    }
    board.appendChild(notice);
  }

  // modes and platforms within them already arrive pre-sorted from the backend
  data.modes.forEach(mode => {
    const section = el('div', 'mode-section');
    section.appendChild(el('div', `mode-header mode-${mode.id}`, mode.name));
    mode.platforms.forEach(platform => section.appendChild(renderPlatform(mode, platform)));
    board.appendChild(section);
  });
}
