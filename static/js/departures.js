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

// the browser tab's title; with a stop shown: "<stop> · BW Departures"
const PAGE_TITLE = document.title;

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

// A favourite platform was opened (mine.js): only that platform is shown,
// until "Show all platforms", another tab or another stop.
let onlyPlatform = null;       // the favourite, or null

// The line filter: the lines picked from the chips above the board, for each
// tab, kept while the stop is shown. None picked = every line.
const pickedLines = new Map();  // tab id -> Set of chip names (see chipOf)
let markOnlyPlatform = false;  // its platform still has to be marked on the map (first load)

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
// the timetable period's first and last day: "12 July 2026"
const periodFormat = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' });
// when the board was last updated, in the same time as the departures
const clockFormat = new Intl.DateTimeFormat('de-DE', {
  hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'Europe/Berlin',
});
// the device's own clock, whatever time zone it is set to
const deviceClockFormat = new Intl.DateTimeFormat('de-DE', { hour: '2-digit', minute: '2-digit' });
// All times on the page are German time. A device whose clock shows another
// time (a visitor abroad, a phone still on home time) is told so; in Germany
// (and wherever the clocks show the same time) nothing is added.
function germanTimeNote(now) {
  return timeFormat.format(now) === deviceClockFormat.format(now) ? '' : ' · German time';
}
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
  if (input.value.trim() === '') showMine();
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
  showMine();
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
  const rows = resultsBox.querySelectorAll('.result').length;
  if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && rows) {
    e.preventDefault();   // the cursor stays where it is
    const step = e.key === 'ArrowDown' ? 1 : -1;
    markResult(markedResult === -1 ? (step === 1 ? 0 : rows - 1) : (markedResult + step + rows) % rows);
  } else if (e.key === 'Enter') {
    e.preventDefault();
    const typed = input.value.trim();
    // the favourites and recent stops (nothing typed yet): the marked one
    if (typed === '') {
      const row = resultsBox.querySelectorAll('.result')[markedResult];
      if (row) row.click();
      return;
    }
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
  // nothing typed: the favourites and recent stops; one letter: nothing yet
  if (q.trim().length < 2) { searchCounter++; if (q.trim() === '') showMine(); else showResults([]); return; }
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
  mineShown = false;
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

// While nothing is typed, the list under the search box shows the stops
// opened last (mine.js); the favourites have their own window (favourites.js)
let mineShown = false;

function showMine() {
  showResults([]);
  const recent = window.mine.recent();
  if (!recent.length) return;
  mineShown = true;
  input.setAttribute('aria-expanded', 'true');
  const head = el('div', 'results-head');
  head.appendChild(el('span', '', 'Recent'));
  const clear = el('button', 'clear-recent', 'Clear all');
  clear.type = 'button';
  clear.onclick = (e) => { e.stopPropagation(); window.mine.forgetAll(); input.focus(); };
  head.appendChild(clear);
  resultsBox.appendChild(head);
  recent.forEach(stop => {
    const row = mineRow(stop.name);
    const forget = el('button', 'forget');
    forget.type = 'button';
    forget.setAttribute('aria-label', `Remove ${stop.name} from the recent stops`);
    forget.onclick = (e) => { e.stopPropagation(); window.mine.forget(stop.id); };
    row.appendChild(forget);
    row.onclick = () => selectStop(stop.id, stop.name, 'search');
    resultsBox.appendChild(row);
  });
}

function mineRow(name) {
  const row = el('div', 'result mine');
  row.setAttribute('role', 'option');
  row.appendChild(el('span', 'result-name', name));
  return row;
}

// a star that saves a favourite or takes it away again (its look is in the stylesheet)
function starButton(on, label, onclick) {
  const star = el('button', on ? 'fav-star on' : 'fav-star');
  star.type = 'button';
  star.title = label;
  star.setAttribute('aria-label', label);
  star.setAttribute('aria-pressed', String(on));
  star.onclick = (e) => { e.stopPropagation(); onclick(); };
  return star;
}

// open a favourite: a stop as usual; a platform on its tab, with only it shown
function openFavourite(favourite) {
  if (!favourite.platform) { selectStop(favourite.stop.id, favourite.stop.name, 'search'); return; }
  selectStop(favourite.stop.id, favourite.stop.name, 'search', { tab: favourite.platform.tab || '', platform: favourite });
}

// a star was set or taken away (here or in another tab): show it everywhere
document.addEventListener('mine-changed', () => {
  if (mineShown) showMine();
  if (lastData) render();
});

async function searchStops(q) {
  const request = ++searchCounter;
  let res;
  try {
    res = await fetch(`${API}/stops/search?q=${encodeURIComponent(q)}${mapPlace()}`);
  } catch {
    if (request === searchCounter) status.textContent = 'Could not search stops.';
    return;
  }
  if (window.noticeVersion) noticeVersion(res);   // a newer version online? (update.js)
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

// `source` says where the stop was picked ('search', 'link' or 'map'; or
// address.js: 'address' = the page was opened at the stop's own address,
// 'history' = the Back button), so the map knows whether it should move there
// `options` (a favourite platform): {tab, platform: the favourite}
function selectStop(id, name, source = 'search', options = {}) {
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
  currentTab = options.tab || '';
  onlyPlatform = options.platform || null;
  markOnlyPlatform = Boolean(onlyPlatform);
  pickedLines.clear();
  shownCount.clear();
  closeStops();
  clearPlatformMark();
  stationBox.innerHTML = '';
  board.innerHTML = '';
  rawBox.innerHTML = '';
  peekBox.innerHTML = '';
  lastData = null;
  status.textContent = name ? `Loading departures for ${name}...` : 'Loading departures...';
  showLoading();
  loadDepartures();
  clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshBoard, REFRESH_MS);
}

// the 30 s refresh pauses while Route mode hides the departures; back from
// it, the board is brought up to date at once
const routeShown = () => document.body.classList.contains('route-mode');
function refreshBoard() {
  if (!routeShown()) loadDepartures();
}
document.addEventListener('mode-changed', (e) => {
  if (e.detail.mode === 'departures' && currentStopId) loadDepartures();
});

// the panel's close button (wide screens): back to the start, the map alone
function closeStop() {
  requestCounter++;   // an answer still on its way is not wanted any more
  clearInterval(refreshTimer);
  currentStopId = null;
  currentTab = '';
  onlyPlatform = null;
  pickedLines.clear();
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
  document.title = PAGE_TITLE;
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
  if (document.body.classList.contains('route-mode')) return;   // (Route mode asks "from here or to here?")
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
  onlyPlatform = null;
  document.querySelectorAll('.tab').forEach(button => button.classList.toggle('active', button.dataset.tab === tabId));
  shownCount.clear();
  closeStops();
  clearPlatformMark();
  status.textContent = 'Loading...';
  showLoading();
  loadDepartures();
}

async function loadDepartures() {
  const request = ++requestCounter;
  // trains=together: a platform's ICEs among its regional trains (the user's choice)
  // when.js: from now, or from a chosen date and time
  const url = `${API}/stops/${encodeURIComponent(currentStopId)}/departures?tab=${encodeURIComponent(currentTab)}&trains=together${window.when.param()}`;
  let res;
  try {
    res = await fetch(url);
  } catch {
    if (request === requestCounter) showLoadError('Could not load departures.', true);
    return;
  }
  if (window.noticeVersion) noticeVersion(res);   // a newer version online? (update.js)
  if (request !== requestCounter) return;  // the user has moved on
  if (!res.ok) {
    const message = await errorMessage(res, 'Could not load departures.');
    // the service not answering, or too many requests from this connection
    // (429): both pass, and the next refresh tries again
    if (request === requestCounter) showLoadError(message, res.status >= 500 || res.status === 429);
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
    document.title = `${pickedName} · ${PAGE_TITLE}`;
    window.mine.remember(lastData.stop);
    document.dispatchEvent(new CustomEvent('stop-shown', { detail: { stop: lastData.stop, source: selectedFrom } }));
  }
  // a favourite platform: the map shows where it is (once; the user may take the mark off)
  if (markOnlyPlatform) {
    markOnlyPlatform = false;
    const found = window.mine.findPlatform(onlyPlatform, lastData);
    if (found) { markPlatform(found[0], found[1], 'favourite'); render(); }
  }
  const now = new Date();
  status.textContent = 'Updated ' + clockFormat.format(now) + germanTimeNote(now);
}

// A failed refresh keeps the board that is shown; a board that was still
// empty (a new stop or tab) shows the message in its place. `retrying`: the
// failure may pass (no connection, the timetable service down)
// another time chosen (when.js): the stop's departures from then, from the top
document.addEventListener('when-changed', () => {
  if (!currentStopId) return;
  shownCount.clear();
  closeStops();
  status.textContent = 'Loading...';
  // the departures of the old time go at once: nobody should take them for the new ones
  showLoading();
  loadDepartures();
});

// While a new list loads (another stop, tab or time) the old departures are
// gone and the board says what is coming; the 30 s refresh keeps the list.
function showLoading() {
  board.innerHTML = '';
  const time = window.when.value() ? ` for ${window.when.label()}` : '';
  const notice = el('div', 'notice loading');
  notice.setAttribute('role', 'status');
  notice.appendChild(el('span', 'spinner'));
  notice.appendChild(el('span', '', `Loading departures${time}...`));
  board.appendChild(notice);
  peekBox.innerHTML = '';
  peekBox.appendChild(el('span', 'destination', 'Loading departures...'));
}

function showLoadError(message, retrying) {
  status.textContent = message;
  const loading = board.querySelector('.loading');
  if (loading && board.childElementCount === 1) board.innerHTML = '';
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
  // the rows of tabs and line chips scroll sideways: they stay where they were
  const sideways = ['.tabs', '.line-chips'].map(selector => [selector, (stationBox.querySelector(selector) || {}).scrollLeft || 0]);
  // ... and a tab or chip that has the keyboard's focus keeps it
  const focused = stationBox.contains(document.activeElement) && document.activeElement.matches('.tab, .line-chip')
    ? [document.activeElement.className.split(' ')[0], document.activeElement.textContent] : null;
  dropGoneLines(lastData);
  renderStation(lastData);
  sideways.forEach(([selector, left]) => { const row = stationBox.querySelector(selector); if (row) row.scrollLeft = left; });
  if (focused) {
    const again = [...stationBox.querySelectorAll(`.${focused[0]}`)].find(button => button.textContent === focused[1]);
    if (again) again.focus({ preventScroll: true });
  }
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
    peekBox.appendChild(el('span', 'countdown', peekTime(dep)));
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
    if (onlyPlatform && !onlyPlatformIs(mode, platform, data)) return;
    platform.departures.forEach(dep => { if (!dep.cancelled && linePicked(dep, mode)) candidates.push([dep, mode, platform]); });
  }));
  if (!candidates.length) {
    peekBox.appendChild(el('span', 'destination', data.modes.length ? 'No further departures shown' : 'No departures soon'));
    return;
  }
  peekShows = candidates.reduce((best, c) => (c[0].time < best[0].time ? c : best));
  const [dep, mode] = peekShows;
  peekBox.appendChild(el('span', badgeClass(dep, mode), dep.line));
  peekBox.appendChild(el('span', 'destination', dep.destination));
  peekBox.appendChild(el('span', 'countdown', peekTime(dep)));
}

// the small bar: the wait from now, or at a chosen time the clock time (with the day if not today)
function peekTime(dep) {
  if (!chosenTime()) return waitText(dep);
  return `${dayLabel(dep)} ${formatTime(dep.time)}`.trim();
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
  const wasOpen = openDeparture !== null;
  openDeparture = null;
  openPlatform = null;
  openTrip = null;
  openTripUrl = '';
  openTripError = '';
  showEarlier = false;
  if (wasOpen) document.dispatchEvent(new CustomEvent('departure-changed'));   // for the Back button (address.js)
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
  document.dispatchEvent(new CustomEvent('departure-changed'));
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
  if (window.noticeVersion) noticeVersion(res);   // a newer version online? (update.js)
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

// a stop's own address (address.js); its colons stay readable
const stopAddress = id => '/stop/' + encodeURIComponent(id).replace(/%3A/gi, ':');

// a link that opens another station: here on the page, or (Ctrl-click,
// middle click, a long press on a phone) in a new tab at the stop's address
function stopLink(stop) {
  const link = el('a', '', stop.name);
  link.href = stopAddress(stop.id);
  link.onclick = (e) => {
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    selectStop(stop.id, stop.name, 'link');
  };
  return link;
}

function renderStation(data) {
  stationBox.innerHTML = '';
  const name = el('h2', 'station-name', data.stop.name);
  const saved = window.mine.isFavouriteStop(data.stop.id);
  name.appendChild(starButton(saved, saved ? 'Remove this stop from favourites' : 'Save this stop as a favourite', () => {
    const favourite = { stop: { id: data.stop.id, name: data.stop.name } };
    if (window.mine.isFavouriteStop(data.stop.id)) window.mine.remove(favourite);
    else window.mine.add(favourite);
  }));
  stationBox.appendChild(name);

  // the navigator: a journey from or to this stop (route.js)
  if (window.route) {
    const links = el('div', 'route-links');
    const place = { kind: 'stop', id: data.stop.id, name: data.stop.name, lat: data.stop.lat, lon: data.stop.lon };
    [['from', 'Route from here'], ['to', 'Route to here']].forEach(([end, text]) => {
      const button = el('button', '', text);
      button.type = 'button';
      button.onclick = (e) => { e.stopPropagation(); window.route.startWith(end, place); };
      links.appendChild(button);
    });
    stationBox.appendChild(links);
  }

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

  // the line filter: one chip per line of this tab (two at least), "All lines" first
  const chips = lineChips(data);
  if (chips.length > 1 || picked().size) {
    const row = el('div', 'line-chips');
    row.setAttribute('role', 'group');
    row.setAttribute('aria-label', 'Show only these lines');
    const all = el('button', picked().size ? 'line-chip all' : 'line-chip all on');
    all.appendChild(el('span', 'line-badge line-all', 'All lines'));
    all.type = 'button';
    all.setAttribute('aria-pressed', String(!picked().size));
    all.onclick = () => { picked().clear(); render(); };
    row.appendChild(all);
    chips.forEach(chip => {
      const on = picked().has(chip.name);
      const button = el('button', on ? 'line-chip on' : 'line-chip');
      button.type = 'button';
      button.setAttribute('aria-pressed', String(on));
      button.title = on ? `Show ${chip.name} no longer` : `Show only ${chip.name}`;
      button.appendChild(el('span', badgeClass(chip.dep, chip.mode), chip.name));
      button.onclick = () => {
        const lines = picked();
        if (lines.has(chip.name)) lines.delete(chip.name);
        else lines.add(chip.name);
        render();
      };
      row.appendChild(button);
    });
    stationBox.appendChild(row);
  }
}

// Long-distance trains are named by their train number ("ICE 378", "IC 2013"):
// their chip is their kind. Every other line is a chip of its own.
const TRAIN_KINDS = { ICE: 'ICE', IC: 'IC/EC', EC: 'IC/EC', ECE: 'IC/EC', ICD: 'IC/EC', TGV: 'TGV', FLX: 'FLX',
  NJ: 'NJ', EN: 'EN', RJ: 'RJ', RJX: 'RJ', IR: 'IR', EST: 'EST' };
const TRAIN_MODES = ['trains', 'long_distance_train', 'regional_train'];

function chipOf(dep, mode) {
  const kind = TRAIN_MODES.includes(mode.id) && /^([A-Z]+) ?\d/.exec(dep.line);
  return kind && TRAIN_KINDS[kind[1]] ? TRAIN_KINDS[kind[1]] : dep.line;
}

// the lines picked on the tab shown (a set that the chips change)
function picked() {
  const tab = (lastData && lastData.tab) || '';
  if (!pickedLines.has(tab)) pickedLines.set(tab, new Set());
  return pickedLines.get(tab);
}

const linePicked = (dep, mode) => !picked().size || picked().has(chipOf(dep, mode));

// the chips of the board shown: [{name, dep, mode}], one per line with a
// departure in the list (of the one platform, when only a favourite platform
// is shown), in the board's order of kinds, train kinds first, then by name
// ("U5" before "U15", "RE2" before "RE 87")
function lineChips(data) {
  const chips = new Map();
  data.modes.forEach((mode, order) => mode.platforms.forEach(platform => {
    if (onlyPlatform && !onlyPlatformIs(mode, platform, data)) return;
    platform.departures.forEach(dep => {
      const name = chipOf(dep, mode);
      if (!chips.has(name)) chips.set(name, { name, dep, mode, order, kind: name !== dep.line });
    });
  }));
  // (spaces don't count: the timetable writes "RE 87" next to "RE2")
  const bare = name => name.replace(/\s+/g, '');
  return [...chips.values()].sort((a, b) => a.order - b.order || b.kind - a.kind
    || bare(a.name).localeCompare(bare(b.name), 'de', { numeric: true }));
}

function formatTime(iso) {
  return iso ? timeFormat.format(new Date(iso)) : '?';
}

// the board shows the departures from a chosen time (when.js), not from now
const chosenTime = () => Boolean(lastData && lastData.when);

// "Mon 05.10." if the departure is on another day (German time) and, from
// now, far enough away that the clock time alone could be misread; at a
// chosen time whenever it is not today; otherwise ''
function dayLabel(dep) {
  if (!dep.time) return '';
  const when = new Date(dep.time);
  if (dayFormat.format(when) === dayFormat.format(new Date())) return '';
  if (!chosenTime() && (dep.minutes === null || dep.minutes < SHOW_DAY_FROM_MINUTES)) return '';
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

  const when = el('td', 'when');
  if (chosenTime() && !dep.cancelled) {
    // a chosen time: the clock time large (late: planned crossed out, new in
    // red), the day under it when it is not today; no wait from now
    const time = el('div', 'countdown at-time');
    fillTime(time, dep);
    when.appendChild(time);
    const day = dayLabel(dep);
    if (day) when.appendChild(el('div', 'clock', day));
    row.appendChild(when);
    return row;
  }
  // the wait first, large; the clock time under it (late: planned crossed out, new in red)
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

// `departures`: the ones to show (the line filter may leave some out)
function renderPlatform(mode, platform, departures = platform.departures) {
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
    const favourite = window.mine.favouriteFor(lastData.stop.id, mode, platform, lastData);
    title.appendChild(starButton(Boolean(favourite), favourite ? 'Remove this platform from favourites' : 'Save this platform as a favourite', () => {
      if (favourite) window.mine.remove(favourite);
      else window.mine.add({ stop: { id: lastData.stop.id, name: lastData.stop.name },
        platform: { mode: mode.id, modeName: mode.name, id: platform.id, name: platform.name, tab: lastData.tab || '' } });
    }));
    block.appendChild(title);
  }

  const count = Math.min(shownCount.get(key) || PER_PLATFORM, departures.length);
  const hidden = departures.length - count;

  const table = el('table');
  departures.slice(0, count).forEach(dep => {
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
  const days = data.when ? `in the 7 days from ${window.when.label()}` : 'in the next 7 days';
  if (data.empty === 'no_timetable') {
    const [first, last] = window.when.range();
    const day = d => periodFormat.format(new Date(d + 'T12:00:00Z'));
    const period = first && last ? ` The current one covers ${day(first)} to ${day(last)}.` : '';
    notice.appendChild(el('p', '', `There is no timetable for ${window.when.label()} yet.${period} Choose another time, or go back to now.`));
  } else if (data.empty === 'nearby') {
    notice.appendChild(el('p', '', `Nothing departs from this station itself ${days}, but there are departures right next to it:`));
    notice.appendChild(nearbyList(data));
  } else {
    notice.appendChild(el('p', '', `The timetable has no departures for this station ${days}. `
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
    const hours = data.when ? `in the 24 hours from ${window.when.label()}` : 'in the next 24 hours';
    if (data.nearby.length) {
      // e.g. a railway station whose trains are replaced by buses from the bus station
      notice.appendChild(el('p', '', `Nothing departs here ${hours}. ${data.when ? 'At that time' : 'Right now'} there are departures next to this station:`));
      notice.appendChild(nearbyList(data));
      notice.appendChild(el('p', '', `Here, the next departures are on ${date}:`));
    } else {
      notice.appendChild(el('p', '', `Nothing departs here ${hours}. The next departures are on ${date}:`));
    }
    board.appendChild(notice);
  }

  if (onlyPlatform) board.appendChild(onlyPlatformBar(data));

  // modes and platforms within them already arrive pre-sorted from the backend
  data.modes.forEach(mode => {
    const platforms = mode.platforms
      .filter(platform => !onlyPlatform || onlyPlatformIs(mode, platform, data))
      .map(platform => [platform, platform.departures.filter(dep => linePicked(dep, mode))])
      .filter(([, departures]) => departures.length);
    if (!platforms.length) return;
    const section = el('div', 'mode-section');
    section.appendChild(el('div', `mode-header mode-${mode.id}`, mode.name));
    platforms.forEach(([platform, departures]) => section.appendChild(renderPlatform(mode, platform, departures)));
    board.appendChild(section);
  });
}

// A picked line that has no departure in the list any more (its last one has
// left) loses its chip and leaves the choice; with none left, every line shows.
function dropGoneLines(data) {
  const lines = new Set(lineChips(data).map(chip => chip.name));
  [...picked()].forEach(name => { if (!lines.has(name)) picked().delete(name); });
}

// the platform of the favourite opened
function onlyPlatformIs(mode, platform, data) {
  const found = window.mine.findPlatform(onlyPlatform, data);
  return Boolean(found) && found[0] === mode && found[1] === platform;
}

// "Only Platform 2 is shown. Show all platforms"
function onlyPlatformBar(data) {
  const bar = el('div', 'only-platform');
  const found = window.mine.findPlatform(onlyPlatform, data);
  bar.appendChild(el('span', '', found ? `Only ${found[1].name} is shown.`
    : `${onlyPlatform.platform.name} has no departures in this list right now.`));
  const all = el('button', 'show-all', 'Show all platforms');
  all.type = 'button';
  all.onclick = () => { onlyPlatform = null; render(); };
  bar.appendChild(all);
  return bar;
}
