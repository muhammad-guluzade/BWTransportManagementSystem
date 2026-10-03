// This page is one client of the JSON API described in docs/API.md.
const API = '/api/v1';

const input = document.getElementById('stop-input');
const resultsBox = document.getElementById('results');
const stationBox = document.getElementById('station');
const board = document.getElementById('board');
const status = document.getElementById('status');
const rawBox = document.getElementById('raw');
let debounceTimer;
let refreshTimer;

// How often the departures board gets updated
const REFRESH_MS = 30000;
// How many departures a platform shows before "Show more"
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
const expanded = new Set();    // platforms the user clicked "Show more" on

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
// a clock time alone is ambiguous from this many minutes ahead on
const SHOW_DAY_FROM_MINUTES = 12 * 60;

input.addEventListener('input', () => {
  clearTimeout(debounceTimer);
  const q = input.value.trim();
  if (q.length < 2) { resultsBox.innerHTML = ''; return; }
  debounceTimer = setTimeout(() => searchStops(q), 300);
});

// close the dropdown if you click elsewhere
document.addEventListener('click', (e) => {
  if (!document.getElementById('search-box').contains(e.target)) {
    resultsBox.innerHTML = '';
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

async function searchStops(q) {
  const res = await fetch(`${API}/stops/search?q=${encodeURIComponent(q)}`);
  if (!res.ok) {
    resultsBox.innerHTML = '';
    status.textContent = await errorMessage(res, 'Could not search stops.');
    return;
  }
  const { stops } = await res.json();
  resultsBox.innerHTML = '';
  stops.forEach(s => {
    const div = el('div', '', s.name);
    div.onclick = () => selectStop(s.id, s.name, 'search');
    resultsBox.appendChild(div);
  });
}

// `source` says where the stop was picked ('search', 'link' or 'map'), so the
// map knows whether it should move there
function selectStop(id, name, source = 'search') {
  input.value = name;
  resultsBox.innerHTML = '';
  currentStopId = id;
  selectedFrom = source;
  announced = false;
  currentTab = '';
  expanded.clear();
  stationBox.innerHTML = '';
  board.innerHTML = '';
  rawBox.innerHTML = '';
  lastData = null;
  status.textContent = `Loading departures for ${name}...`;
  loadDepartures();
  clearInterval(refreshTimer);
  refreshTimer = setInterval(loadDepartures, REFRESH_MS);
}

function selectTab(tabId) {
  currentTab = tabId;
  expanded.clear();
  board.innerHTML = '';
  status.textContent = 'Loading...';
  loadDepartures();
}

async function loadDepartures() {
  const request = ++requestCounter;
  const url = `${API}/stops/${encodeURIComponent(currentStopId)}/departures?tab=${encodeURIComponent(currentTab)}`;
  let res;
  try {
    res = await fetch(url);
  } catch {
    if (request === requestCounter) status.textContent = 'Could not load departures.';
    return;
  }
  if (request !== requestCounter) return;  // the user has moved on
  if (!res.ok) {
    status.textContent = await errorMessage(res, 'Could not load departures.');
    return;
  }
  lastData = await res.json();
  lastUrl = url;
  currentTab = lastData.tab || '';
  render();
  if (!announced) {
    // tell the map which stop is shown now (once per selection, not per refresh)
    announced = true;
    input.value = lastData.stop.name;
    document.dispatchEvent(new CustomEvent('stop-shown', { detail: { stop: lastData.stop, source: selectedFrom } }));
  }
  status.textContent = 'Updated ' + new Date().toLocaleTimeString();
}

function render() {
  renderStation(lastData);
  renderBoard(lastData);
  renderRaw();
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

  // when the board is empty the nearby stations are shown in its place instead
  if (data.nearby.length && !data.empty) {
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

function renderDeparture(dep) {
  const row = el('tr', dep.cancelled ? 'cancelled' : '');

  const lineCell = el('td', 'line-cell');
  lineCell.appendChild(el('span', 'line-badge', dep.line));
  row.appendChild(lineCell);

  // destination, D-Ticket label, and the major stops on the way
  const directionCell = el('td');
  const direction = el('div', 'direction');
  direction.appendChild(el('span', 'destination', dep.destination));
  if (dep.dticket) direction.appendChild(el('span', 'dticket', 'D-Ticket'));
  directionCell.appendChild(direction);
  if (dep.via.length) {
    directionCell.appendChild(el('div', 'via', 'via ' + dep.via.join(' · ')));
  }
  row.appendChild(directionCell);

  const timeCell = el('td', 'time');
  const day = dayLabel(dep);
  if (day) timeCell.appendChild(el('span', 'day', day + ' '));
  if (dep.cancelled) {
    timeCell.appendChild(el('s', '', formatTime(dep.planned || dep.time)));
    timeCell.appendChild(el('span', 'late', ' Cancelled'));
  } else if (dep.delay >= 1) {
    // late: planned time crossed out, then the new time and the delay
    timeCell.appendChild(el('s', '', formatTime(dep.planned)));
    timeCell.appendChild(document.createTextNode(' ' + formatTime(dep.time) + ' '));
    timeCell.appendChild(el('span', 'late', `+${dep.delay} min`));
  } else {
    timeCell.appendChild(document.createTextNode(formatTime(dep.time)));
  }
  row.appendChild(timeCell);

  const hasMinutes = !dep.cancelled && dep.minutes !== null && dep.minutes !== undefined;
  row.appendChild(el('td', 'minutes', hasMinutes ? formatWait(dep.minutes) : ''));
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
  if (!mode.flat) {
    block.appendChild(el('div', 'platform-title', platform.name));
  }

  const key = `${mode.id}|${platform.name}`;
  const hidden = expanded.has(key) ? 0 : Math.max(0, platform.departures.length - PER_PLATFORM);
  const shown = platform.departures.slice(0, platform.departures.length - hidden);

  const table = el('table');
  shown.forEach(dep => table.appendChild(renderDeparture(dep)));
  block.appendChild(table);

  if (hidden > 0) {
    const more = el('button', 'show-more', `Show ${hidden} more`);
    more.onclick = () => { expanded.add(key); render(); };
    block.appendChild(more);
  }
  return block;
}

// an empty board says why it is empty
function renderEmpty(data) {
  const notice = el('div', 'notice');
  if (data.empty === 'nearby') {
    notice.appendChild(el('p', '', 'Nothing departs from this station itself, but there are departures right next to it:'));
    const list = el('ul');
    data.nearby.forEach(stop => {
      const item = el('li');
      item.appendChild(stopLink(stop));
      if (stop.types.length) {
        item.appendChild(document.createTextNode(' (' + stop.types.map(t => t.name).join(', ') + ')'));
      }
      list.appendChild(item);
    });
    notice.appendChild(list);
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
    notice.appendChild(el('p', '', `Nothing departs here in the next 24 hours. The next departures are on ${date}:`));
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
