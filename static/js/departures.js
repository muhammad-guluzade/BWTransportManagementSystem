const input = document.getElementById('stop-input');
const resultsBox = document.getElementById('results');
const stationBox = document.getElementById('station');
const board = document.getElementById('board');
const status = document.getElementById('status');
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
let requestCounter = 0;        // to ignore answers that arrive out of order
const expanded = new Set();    // platforms the user clicked "Show more" on

// departure times arrive in UTC; show them in local German time
const timeFormat = new Intl.DateTimeFormat('de-DE', {
  hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Berlin',
});

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

async function errorMessage(res, fallback) {
  try {
    return (await res.json()).error || fallback;
  } catch {
    return fallback;
  }
}

async function searchStops(q) {
  const res = await fetch(`/api/search_stops?q=${encodeURIComponent(q)}`);
  if (!res.ok) {
    resultsBox.innerHTML = '';
    status.textContent = await errorMessage(res, 'Could not search stops.');
    return;
  }
  const stops = await res.json();
  resultsBox.innerHTML = '';
  stops.forEach(s => {
    const div = el('div', '', s.name);
    div.onclick = () => selectStop(s.id, s.name);
    resultsBox.appendChild(div);
  });
}

function selectStop(id, name) {
  input.value = name;
  resultsBox.innerHTML = '';
  currentStopId = id;
  currentTab = '';
  expanded.clear();
  stationBox.innerHTML = '';
  board.innerHTML = '';
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
  const url = `/api/departures?stop_id=${encodeURIComponent(currentStopId)}&tab=${encodeURIComponent(currentTab)}`;
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
  currentTab = lastData.tab || '';
  render();
  status.textContent = 'Updated ' + new Date().toLocaleTimeString();
}

function render() {
  renderStation(lastData);
  renderBoard(lastData);
}

function renderStation(data) {
  stationBox.innerHTML = '';
  stationBox.appendChild(el('h2', 'station-name', data.stop.name));

  if (data.nearby.length) {
    const nearby = el('div', 'nearby', 'Also nearby: ');
    data.nearby.forEach((stop, i) => {
      if (i > 0) nearby.appendChild(document.createTextNode(' · '));
      const link = el('a', '', stop.name);
      link.href = '#';
      link.onclick = (e) => { e.preventDefault(); selectStop(stop.id, stop.name); };
      nearby.appendChild(link);
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

function modeSlug(name) {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, '-');
}

function formatTime(iso) {
  return iso ? timeFormat.format(new Date(iso)) : '?';
}

function renderDeparture(dep) {
  const row = el('tr', dep.cancelled ? 'cancelled' : '');

  const lineCell = el('td', 'line-cell');
  lineCell.appendChild(el('span', 'line-badge', dep.line));
  row.appendChild(lineCell);

  // destination, D-Ticket label, and the major stops on the way
  const directionCell = el('td');
  const direction = el('div', 'direction');
  direction.appendChild(el('span', 'destination', dep.direction));
  if (dep.dticket) direction.appendChild(el('span', 'dticket', 'D-Ticket'));
  directionCell.appendChild(direction);
  if (dep.via.length) {
    directionCell.appendChild(el('div', 'via', 'via ' + dep.via.join(' · ')));
  }
  row.appendChild(directionCell);

  const timeCell = el('td', 'time');
  if (dep.cancelled) {
    timeCell.appendChild(el('s', '', formatTime(dep.planned || dep.time)));
    timeCell.appendChild(el('span', 'late', ' Cancelled'));
  } else if (dep.delay >= 1) {
    // late: planned time crossed out, then the new time and the delay
    timeCell.appendChild(el('s', '', formatTime(dep.planned)));
    timeCell.appendChild(document.createTextNode(' ' + formatTime(dep.time) + ' '));
    timeCell.appendChild(el('span', 'late', `+${dep.delay} min`));
  } else {
    timeCell.textContent = formatTime(dep.time);
  }
  row.appendChild(timeCell);

  const hasMinutes = !dep.cancelled && dep.minutes !== null && dep.minutes !== undefined;
  row.appendChild(el('td', 'minutes', hasMinutes ? formatWait(dep.minutes) : ''));
  return row;
}

// 45 -> "45 min", 167 -> "2 h 47 min"
function formatWait(minutes) {
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

function renderPlatform(mode, platform) {
  const block = el('div', 'platform-block');
  if (!mode.flat) {
    block.appendChild(el('div', 'platform-title', platform.name));
  }

  const key = `${mode.name}|${platform.name}`;
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

function renderBoard(data) {
  board.innerHTML = '';
  if (data.modes.length === 0) {
    board.appendChild(el('p', '', 'No departures found for this stop right now.'));
    return;
  }

  // modes and platforms within them already arrive pre-sorted from the backend
  data.modes.forEach(mode => {
    const section = el('div', 'mode-section');
    section.appendChild(el('div', `mode-header mode-${modeSlug(mode.name)}`, mode.name));
    mode.platforms.forEach(platform => section.appendChild(renderPlatform(mode, platform)));
    board.appendChild(section);
  });
}
