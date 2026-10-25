// The navigator: Route mode (step 2c). The top bar switches between the two
// modes: a stop's departures (departures.js) and a journey from one place to
// another, from the timetable service's journey planner (GET /api/v1/journeys).
//
// In Route mode the top bar holds From and To (a stop, a sight, an address or
// a street from the search, "My location", or a point tapped on the map),
// the swap button, the time (now, leave at, arrive by) and "D-Ticket only".
// The panel (the sheet on a phone) lists the connections; a tap opens one
// step by step, and the map draws its route (map.js, 'route-drawn').
//
// Uses API, el(), errorMessage(), mapPlace(), fillTime(), formatTime(),
// badgeClass(), clockFormat, germanTimeNote() and panelBox from departures.js.
(function () {
  const switchBox = document.getElementById('mode-switch');
  const form = document.getElementById('route-form');
  const head = document.getElementById('route-head');
  const routeBoard = document.getElementById('route-board');
  const swapButton = document.getElementById('route-swap');
  const chip = document.getElementById('route-when-chip');
  const dticketBox = document.getElementById('route-dticket');
  const screensList = document.querySelector('#screens-bar [data-screen="list"]');
  const wide = window.matchMedia('(min-width: 1024px)');
  const summary = document.getElementById('route-summary');
  const goButton = document.getElementById('route-go');
  // phones: the form folded into one line (after a search, a touch of the map, or its fold button)
  let folded = false;
  // phones: the map was touched: the sheet is just its grip, with nothing written on it
  let quiet = false;
  const tools = window.when.tools;

  // the list of connections is asked again this often while it is on screen
  const REFRESH_MS = 60000;
  // ... and at once when the page is looked at again after this long
  const STALE_MS = 30000;
  const SEARCH_DELAY_MS = 200;
  const PLACE_KINDS = { place: 'Place', address: 'Address', street: 'Street' };
  const OTHER = { from: 'to', to: 'from' };
  const END_NAMES = { from: 'The start', to: 'The destination' };

  let mode = 'departures';
  const scrollOf = { departures: 0, route: 0 };   // where the panel was scrolled to in each mode
  // start and destination: {kind: 'stop' | 'place' | 'me', id, name, lat, lon,
  // address (what the planner calls the spot, for places), locating}
  const ends = { from: null, to: null };
  // the time: now (value null), or "YYYY-MM-DDTHH:MM" German time to leave at or arrive by
  const time = { arrive: false, value: null };
  let dticket = false;
  try { dticket = localStorage.getItem('dticketOnly') === '1'; } catch { /* no storage: off */ }

  // the connections found: state 'idle' (not both places yet), 'loading',
  // 'ready', 'error' (the request failed), 'problem' (e.g. no location)
  let state = 'idle';
  let problem = null;        // {message, retry}
  let pages = [];            // the requests made for this search: [{when, arrive}], the first is the user's
  let journeys = [];         // all connections of those pages, by departure
  let empty = null;          // why there are none (the API's `empty`)
  let updatedAt = null;
  let updateProblem = '';    // the last refresh failed (the list shown is older)
  let lastUrl = '';
  let searchId = 0;          // a new search: answers to older ones are dropped
  let loadCounter = 0;
  let loadingMore = null;    // 'earlier' | 'later' while that page loads
  let moreNote = { earlier: '', later: '' };
  // what is open: one connection, and in it some lists of stops and notices
  let openId = null;
  const openStops = new Set();
  const openNotices = new Set();

  // ---------- times ----------
  const germanParts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Berlin', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });
  // a moment as German clock time, "YYYY-MM-DDTHH:MM" (what the API's `when` takes)
  function germanText(date) {
    const p = Object.fromEntries(germanParts.formatToParts(date).map(x => [x.type, x.value]));
    return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
  }
  const minutesBetween = (a, b) => Math.round((new Date(b) - new Date(a)) / 60000);
  // "Mon 26.10." when a connection is not today (German time), else ''
  function dayOf(iso) {
    if (!iso) return '';
    const at = new Date(iso);
    if (dayFormat.format(at) === dayFormat.format(new Date())) return '';
    return `${weekdayFormat.format(at)} ${dateFormat.format(at)}`;
  }
  function duration(minutes) {
    if (minutes === null || minutes === undefined) return '';
    return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
  }
  const changesText = n => (n === 0 ? 'direct' : n === 1 ? '1 change' : `${n} changes`);

  // ---------- the two modes ----------
  function setMode(next) {
    if (next === mode) return;
    scrollOf[mode] = panelBox.scrollTop;
    mode = next;
    const route = mode === 'route';
    quiet = false;
    if (route && !(ends.from && ends.to)) folded = false;   // the form open, to choose From and To
    document.body.classList.toggle('route-mode', route);
    switchBox.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === mode)));
    form.hidden = !route;
    head.hidden = !route;
    routeBoard.hidden = !route;
    screensList.textContent = route ? 'Connections' : 'Departures';
    closeLists();
    renderHead();
    renderBoard();
    document.dispatchEvent(new CustomEvent('mode-changed', { detail: { mode } }));
    panelBox.scrollTop = scrollOf[mode];
    drawn();
    if (route) {
      // a list that has been away a while is asked again
      if (state === 'ready' && updatedAt && Date.now() - updatedAt > STALE_MS) load(true);
      // on a laptop the first empty field is ready for typing (a phone would push its keyboard up)
      // (after the click that got here: it would close the field's list again)
      const empty = !ends.from ? 'from' : !ends.to ? 'to' : null;
      if (wide.matches && empty) setTimeout(() => { if (mode === 'route') fields[empty].input.focus(); }, 0);
    }
  }
  switchBox.addEventListener('click', (e) => {
    const button = e.target.closest('button[data-mode]');
    if (button) setMode(button.dataset.mode);
  });

  // ---------- From and To ----------
  const fields = {};
  ['from', 'to'].forEach(end => {
    const box = form.querySelector(`.route-field[data-end="${end}"]`);
    fields[end] = {
      box,
      input: box.querySelector('input'),
      list: box.querySelector('.route-results'),
      clear: box.querySelector('.route-clear'),
      placeholder: box.querySelector('input').placeholder,
      rows: [],        // what each row of the list picks: [place, ...]
      shownFor: null,  // the text the list was found for ('' = the suggestions)
      marked: -1,
      waiting: '',     // Enter was pressed for this text before its results came
      counter: 0,
      timer: null,
    };
  });

  // `keepTyped`: text typed but not chosen stays (as in the stop search, a tap never clears it)
  function showField(end, keepTyped = false) {
    const f = fields[end];
    const place = ends[end];
    if (document.activeElement !== f.input && (place || !keepTyped)) f.input.value = place ? (place.locating ? 'Finding your location...' : place.name) : '';
    f.box.classList.toggle('is-me', Boolean(place && place.kind === 'me'));
    f.box.classList.toggle('has-text', Boolean(place) || f.input.value !== '');
  }

  function closeLists() {
    ['from', 'to'].forEach(end => {
      const f = fields[end];
      f.counter++;
      f.list.innerHTML = '';
      f.rows = [];
      f.shownFor = null;
      f.marked = -1;
      f.input.setAttribute('aria-expanded', 'false');
    });
  }

  function listRow(end, place, text, detail, className = '') {
    const f = fields[end];
    const row = el('div', `result ${className}`.trim());
    row.setAttribute('role', 'option');
    row.appendChild(el('span', 'result-name', text));
    if (detail) row.appendChild(el('span', 'result-types', detail));
    row.onclick = () => choose(end, place, { keys: true });
    f.rows.push(place);
    f.list.appendChild(row);
    return row;
  }

  // nothing typed yet: "My location" and the stops opened last
  function suggest(end) {
    const f = fields[end];
    closeLists();
    f.shownFor = '';
    listRow(end, { kind: 'me', name: 'My location' }, '◉ My location', '', 'route-me');
    const recent = window.mine.recent();
    if (recent.length) {
      f.list.appendChild(el('div', 'results-head', 'Recent'));
      recent.forEach(stop => listRow(end, { kind: 'stop', id: stop.id, name: stop.name, lat: null, lon: null }, stop.name, '', 'mine'));
    }
    f.input.setAttribute('aria-expanded', 'true');
  }

  async function find(end, q) {
    const f = fields[end];
    const request = ++f.counter;
    let res;
    try {
      res = await fetch(`${API}/stops/search?q=${encodeURIComponent(q)}&places=1${mapPlace()}`);
    } catch {
      if (request === f.counter) showFound(end, [], [], q.trim(), 'Could not search (no connection?).');
      return;
    }
    if (window.noticeVersion) noticeVersion(res);
    if (request !== f.counter) return;
    if (!res.ok) {
      const message = await errorMessage(res, 'Could not search.');
      if (request === f.counter) showFound(end, [], [], q.trim(), message);
      return;
    }
    const data = await res.json();
    if (request !== f.counter) return;
    showFound(end, data.stops, data.places || [], q.trim());
  }

  function showFound(end, stops, places, typed, message = '') {
    const f = fields[end];
    const counter = f.counter;
    closeLists();
    f.counter = counter;
    f.shownFor = typed;
    if (message) f.list.appendChild(el('div', 'no-result', message));
    else if (!stops.length && !places.length) f.list.appendChild(el('div', 'no-result', `Nothing found for "${typed}".`));
    else if (stops.length && stops.every(s => s.partial) && !places.length) {
      f.list.appendChild(el('div', 'no-result', `No stop matches every word of "${typed}". Stops that match part of it:`));
    }
    stops.forEach(stop => listRow(end, { kind: 'stop', id: stop.id, name: stop.name, lat: stop.lat, lon: stop.lon },
      stop.name, stop.types.map(t => t.name).join(', ')));
    places.forEach(place => listRow(end, { kind: 'place', id: null, name: place.name, lat: place.lat, lon: place.lon },
      place.name, PLACE_KINDS[place.kind] || '', 'route-place'));
    f.input.setAttribute('aria-expanded', String(f.rows.length > 0));
    if (f.waiting && f.waiting === typed) {
      f.waiting = '';
      if (f.rows.length) choose(end, f.rows[0], { keys: true });
    }
  }

  function mark(end, index) {
    const f = fields[end];
    f.marked = index;
    [...f.list.querySelectorAll('.result')].forEach((row, i) => {
      row.classList.toggle('active', i === index);
      row.setAttribute('aria-selected', String(i === index));
      if (i === index) row.scrollIntoView({ block: 'nearest' });
    });
  }

  ['from', 'to'].forEach(end => {
    const f = fields[end];
    f.input.addEventListener('focus', () => {
      const place = ends[end];
      // the chosen place stays written, selected: typing replaces it, Enter keeps it
      if (place && f.input.value === place.name) setTimeout(() => { if (document.activeElement === f.input) f.input.select(); }, 0);
      showField(end);
      if (f.input.value.trim() === '') suggest(end);
    });
    f.input.addEventListener('blur', () => {
      f.input.placeholder = f.placeholder;
      if (f.input.value.trim() === '') f.input.value = '';
      showField(end, true);
    });
    f.input.addEventListener('input', () => {
      f.waiting = '';
      f.box.classList.toggle('has-text', Boolean(ends[end]) || f.input.value !== '');
      clearTimeout(f.timer);
      const q = f.input.value.replace(/^\s+/, '');
      if (q.trim() === '') { suggest(end); return; }
      if (q.trim().length < 2) { closeLists(); return; }
      f.timer = setTimeout(() => find(end, q), SEARCH_DELAY_MS);
    });
    f.input.addEventListener('keydown', (e) => {
      const rows = f.list.querySelectorAll('.result').length;
      if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && rows) {
        e.preventDefault();
        const step = e.key === 'ArrowDown' ? 1 : -1;
        mark(end, f.marked === -1 ? (step === 1 ? 0 : rows - 1) : (f.marked + step + rows) % rows);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        const typed = f.input.value.trim();
        if (f.marked >= 0 && f.rows[f.marked]) { choose(end, f.rows[f.marked], { keys: true }); return; }
        // the chosen place is in the box as it was: From goes on to To, To asks for the connections
        if (ends[end] && (typed === '' || typed === ends[end].name || ends[end].locating)) {
          closeLists();
          if (end === 'from') fields.to.input.focus();
          else search();
          return;
        }
        if (typed.length < 2) return;
        if (f.shownFor === typed) { if (f.rows.length) choose(end, f.rows[0], { keys: true }); return; }
        f.waiting = typed;
        clearTimeout(f.timer);
        find(end, f.input.value.replace(/^\s+/, ''));
      }
    });
    f.list.addEventListener('touchmove', () => { if (document.activeElement === f.input) f.input.blur(); }, { passive: true });
    f.clear.addEventListener('click', () => {
      f.input.value = '';
      if (ends[end]) { ends[end] = null; changed(); }
      showField(end);
      f.input.focus();
      suggest(end);
    });
  });
  // a tap anywhere else closes the lists
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.route-field')) closeLists();
  });

  // Choosing a place never shows connections by itself (the user's rule): they
  // come with Enter / the search key in To, or the Search button.
  // `keys`: chosen in the field's list: From goes on to To (whose place, if
  // any, is shown as it is); To keeps the keyboard, ready for Enter / search.
  function choose(end, place, { keys = false } = {}) {
    const f = fields[end];
    closeLists();
    ends[end] = { ...place };
    if (document.activeElement === f.input) f.input.blur();
    showField(end);
    changed();
    if (place.kind === 'me') locate(end);
    if (!keys) return;
    if (end === 'from') fields.to.input.focus();
    else f.input.focus();
  }

  // "My location": the device is asked where it is (the browser asks the user first)
  function locate(end) {
    const place = ends[end];
    place.locating = true;
    place.lat = place.lon = null;
    showField(end);
    const fail = (message) => {
      if (ends[end] !== place) return;
      ends[end] = null;
      showField(end);
      searchWanted = false;
      changed();
      state = 'problem';
      problem = { message, retry: false };
      renderAll();
    };
    if (!window.isSecureContext || !navigator.geolocation) {
      fail('Your location can only be used when the page is opened over a secure connection (https), as on the public site.');
      return;
    }
    renderAll();
    navigator.geolocation.getCurrentPosition((position) => {
      if (ends[end] !== place) return;   // something else was chosen meanwhile
      place.locating = false;
      place.lat = position.coords.latitude;
      place.lon = position.coords.longitude;
      showField(end);
      // Enter / Search was pressed while it was being found: now the connections
      if (searchWanted) { searchWanted = false; search(); } else renderAll();
    }, (error) => {
      fail(error.code === 1 ? 'This page may not use your location. Allow it in the browser\'s settings, or type a stop or a place.'
        : error.code === 3 ? 'Finding your location took too long. Try again, or type a stop or a place.'
          : 'Your location could not be found. Type a stop or a place instead.');
    }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
  }

  swapButton.addEventListener('click', () => {
    [ends.from, ends.to] = [ends.to, ends.from];
    showField('from');
    showField('to');
    changed({ again: true });
  });

  dticketBox.checked = dticket;
  dticketBox.addEventListener('change', () => {
    dticket = dticketBox.checked;
    try { localStorage.setItem('dticketOnly', dticket ? '1' : '0'); } catch { /* not remembered */ }
    changed({ again: true });
  });

  // ---------- the time ----------
  const win = document.getElementById('route-when-window');
  const winCard = win.querySelector('.settings-card');
  const timeButtons = [...win.querySelectorAll('[data-route-time]')];
  const nowButton = document.getElementById('route-when-now');
  const timeField = document.getElementById('route-when-input');
  const timeZone = document.getElementById('route-when-zone');
  const timeRange = document.getElementById('route-when-range');
  let arriveChoice = false;   // the choice in the open window

  function drawChip() {
    chip.textContent = time.value ? `${time.arrive ? 'Arrive by' : 'Leave'} ${tools.label(time.value)}` : 'Leave now';
    chip.classList.toggle('chosen', Boolean(time.value));
  }
  function chooseArrive(arrive) {
    arriveChoice = arrive;
    timeButtons.forEach(b => b.setAttribute('aria-pressed', String((b.dataset.routeTime === 'arrive') === arrive)));
    nowButton.hidden = arrive;   // "arrive by now" means nothing
  }
  function openTime() {
    chooseArrive(time.arrive);
    // arriving: an hour from now is the likelier wish than now
    timeField.value = time.value || (time.arrive ? tools.add(tools.germanNow(), 1) : tools.germanNow());
    const [first, last] = window.when.range();
    if (first) timeField.min = `${first}T00:00`;
    if (last) timeField.max = `${last}T23:59`;
    timeZone.hidden = !germanTimeNote(new Date());
    timeRange.textContent = '';
    win.hidden = false;
    document.body.classList.add('settings-open');
    chip.setAttribute('aria-expanded', 'true');
    timeField.focus();
  }
  function closeTime() {
    if (win.hidden) return;
    win.hidden = true;
    document.body.classList.remove('settings-open');
    chip.setAttribute('aria-expanded', 'false');
    chip.focus();
  }
  function takeTime() {
    const value = timeField.value.slice(0, 16);
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) {
      timeRange.textContent = 'Please choose a date and a time.';
      timeField.focus();
      return;
    }
    if (!tools.inRange(value)) {
      timeRange.textContent = 'There is no timetable for that time yet.';
      timeField.focus();
      return;
    }
    closeTime();
    time.value = value;
    time.arrive = arriveChoice;
    drawChip();
    changed({ again: true });
  }
  chip.addEventListener('click', openTime);
  timeButtons.forEach(b => b.addEventListener('click', () => {
    chooseArrive(b.dataset.routeTime === 'arrive');
    if (arriveChoice && timeField.value === tools.germanNow()) timeField.value = tools.add(tools.germanNow(), 1);
  }));
  nowButton.addEventListener('click', () => {
    closeTime();
    time.value = null;
    time.arrive = false;
    drawChip();
    changed({ again: true });
  });
  document.getElementById('route-when-close').addEventListener('click', closeTime);
  document.getElementById('route-when-show').addEventListener('click', takeTime);
  timeField.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); takeTime(); } });
  document.addEventListener('keydown', (e) => {
    if (win.hidden) return;
    if (e.key === 'Escape') { e.preventDefault(); closeTime(); }
    if (e.key === 'Tab') {
      const focusable = [...winCard.querySelectorAll('button:not([hidden]), input')];
      const firstOne = focusable[0], lastOne = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === firstOne) { e.preventDefault(); lastOne.focus(); }
      else if (!e.shiftKey && document.activeElement === lastOne) { e.preventDefault(); firstOne.focus(); }
    }
  });

  // ---------- asking for connections ----------
  const ready = () => ends.from && ends.to && !ends.from.locating && !ends.to.locating
    && (ends.from.kind === 'stop' || ends.from.lat !== null) && (ends.to.kind === 'stop' || ends.to.lat !== null);
  const param = place => (place.kind === 'stop' ? place.id : `${place.lat.toFixed(5)},${place.lon.toFixed(5)}`);
  const samePlace = (a, b) => (a.kind === 'stop' && b.kind === 'stop' ? a.id === b.id
    : a.kind !== 'stop' && b.kind !== 'stop' && Math.abs(a.lat - b.lat) < 1e-5 && Math.abs(a.lon - b.lon) < 1e-5);

  function journeysUrl(page) {
    return `${API}/journeys?from=${encodeURIComponent(param(ends.from))}&to=${encodeURIComponent(param(ends.to))}`
      + (page.when ? `&when=${encodeURIComponent(page.when)}` : '') + (page.arrive ? '&arrive=1' : '') + (dticket ? '&dticket=1' : '');
  }

  // the API's words for a place it refuses, in the page's
  function friendlier(message) {
    const outside = /^(from|to) is outside/.exec(message);
    if (!outside) return message;
    const end = outside[1];
    if (ends[end] && ends[end].kind === 'me') return 'You are outside Baden-Württemberg: journeys start and end in Baden-Württemberg.';
    return `${END_NAMES[end]} is outside Baden-Württemberg: journeys start and end in Baden-Württemberg.`;
  }

  async function fetchPage(page) {
    const url = journeysUrl(page);
    let res;
    try {
      res = await fetch(url);
    } catch {
      return { ok: false, message: 'Could not load connections (no connection?).', retry: true };
    }
    if (window.noticeVersion) noticeVersion(res);
    if (!res.ok) {
      const message = friendlier(await errorMessage(res, 'Could not load connections.'));
      return { ok: false, message, retry: res.status >= 500 || res.status === 429 };
    }
    try {
      return { ok: true, data: await res.json(), url };
    } catch {
      return { ok: false, message: 'Could not load connections.', retry: true };
    }
  }

  function merge(list) {
    const byId = new Map();
    list.forEach(j => byId.set(j.id, j));
    return [...byId.values()].sort((a, b) => (a.departure.time || '').localeCompare(b.departure.time || '')
      || (a.arrival.time || '').localeCompare(b.arrival.time || ''));
  }

  // Start, destination, time or the switch changed: the connections shown are
  // gone. `again`: a list that was on screen is asked for anew at once (swap,
  // time, D-Ticket); a new place waits for Enter / Search.
  let searchWanted = false;   // asked for while "My location" was still being found
  function changed({ again = false, plan = false } = {}) {
    const listed = state === 'loading' || state === 'ready' || state === 'error';
    searchId++;
    loadingMore = null;
    moreNote = { earlier: '', later: '' };
    openId = null;
    openStops.clear();
    openNotices.clear();
    journeys = [];
    pages = [];
    empty = null;
    updatedAt = null;
    updateProblem = '';
    problem = null;
    state = 'idle';
    if ((plan || (again && listed)) && ready()) {
      if (samePlace(ends.from, ends.to)) { state = 'problem'; problem = { message: 'Start and destination are the same.', retry: false }; }
      else {
        pages = [{ when: time.value, arrive: time.arrive && Boolean(time.value) }];
        load(false);
        return;
      }
    }
    renderAll();
    document.dispatchEvent(new CustomEvent('route-changed'));
  }

  // Enter / the search key in To, or the Search button: the connections
  function search() {
    closeLists();
    if (locating()) { searchWanted = true; return; }
    if (!ends.from || !ends.to) { fields[ends.from ? 'to' : 'from'].input.focus(); return; }
    if (document.activeElement && form.contains(document.activeElement)) document.activeElement.blur();   // (phones: the keyboard goes)
    changed({ plan: true });
  }
  goButton.addEventListener('click', search);

  // asks for every page of this search; `refreshing`: the list stays while it loads
  async function load(refreshing) {
    const search = searchId;
    const request = ++loadCounter;
    if (!refreshing) {
      folded = true;
      quiet = false;
      state = 'loading';
      renderAll();
      // on a phone the sheet comes up for the list (sheet.js)
      document.dispatchEvent(new CustomEvent('route-searched'));
      document.dispatchEvent(new CustomEvent('route-changed'));
    }
    const answers = await Promise.all(pages.map(fetchPage));
    if (search !== searchId || request !== loadCounter) return;
    const base = answers[0];
    if (!base.ok) {
      if (refreshing && state === 'ready') { updateProblem = base.message; renderHead(); return; }
      state = 'error';
      problem = { message: base.message, retry: base.retry };
      renderAll();
      return;
    }
    const data = base.data;
    lastUrl = base.url;
    // what the planner calls the places (an address for a point on the map)
    ['from', 'to'].forEach(end => {
      const place = ends[end];
      const found = data[end];
      if (!place || !found) return;
      if (place.kind !== 'stop' && found.name) place.address = found.name;
      if (place.lat === null && found.lat !== null) { place.lat = found.lat; place.lon = found.lon; }
    });
    const found = merge(answers.filter(a => a.ok).flatMap(a => a.data.journeys));
    // a refresh keeps connections that only an earlier page had found
    journeys = refreshing ? merge([...journeys.filter(j => !found.some(f => f.id === j.id) && pages.length > 1), ...found]) : found;
    empty = journeys.length ? null : data.empty;
    if (openId && !journeys.some(j => j.id === openId)) openId = null;
    state = 'ready';
    updatedAt = Date.now();
    updateProblem = '';
    const held = holdCards();
    renderAll();
    held();
  }

  async function more(direction) {
    if (loadingMore || !journeys.length) return;
    const search = searchId;
    const edge = direction === 'later' ? journeys[journeys.length - 1] : journeys[0];
    const page = direction === 'later'
      ? { when: germanText(new Date(new Date(edge.departure.planned).getTime() + 60000)), arrive: false }
      : { when: germanText(new Date(new Date(edge.arrival.planned).getTime() - 60000)), arrive: true };
    loadingMore = direction;
    moreNote[direction] = '';
    renderBoard();
    const answer = await fetchPage(page);
    if (search !== searchId) return;
    loadingMore = null;
    if (!answer.ok) {
      moreNote[direction] = answer.message;
    } else {
      pages.push(page);
      const before = journeys.length;
      const held = holdCards();
      journeys = merge([...journeys, ...answer.data.journeys]);
      if (journeys.length === before) moreNote[direction] = `No ${direction} connections found.`;
      renderAll();
      held();
      return;
    }
    renderBoard();
  }

  // the list is asked again now and then while it is looked at
  setInterval(() => {
    if (mode === 'route' && state === 'ready' && !document.hidden) load(true);
  }, REFRESH_MS);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && mode === 'route' && state === 'ready' && updatedAt && Date.now() - updatedAt > STALE_MS) load(true);
  });

  // ---------- drawing the panel ----------
  // the connection at the top of the visible list stays where it is when the list is drawn again
  function holdCards() {
    const top = () => panelBox.getBoundingClientRect().top;
    const below = document.getElementById('panel-head').getBoundingClientRect().bottom - top();
    const held = [...routeBoard.querySelectorAll('.conn')].map(card => [card.dataset.id, card.getBoundingClientRect().top - top()])
      .filter(([, at]) => at >= below - 1).slice(0, 5);
    const scrolled = panelBox.scrollTop;
    return () => {
      panelBox.scrollTop = scrolled;
      for (const [id, at] of held) {
        const card = routeBoard.querySelector(`.conn[data-id="${CSS.escape(id)}"]`);
        if (card) { panelBox.scrollTop += (card.getBoundingClientRect().top - top()) - at; return; }
      }
    };
  }

  function renderAll() {
    renderHead();
    renderBoard();
    fold();
    drawn();
  }

  // Phones: the form as one line ("Pragfriedhof → Museum", the time) after a
  // search, a touch of the map, or its fold button; a tap on the line opens it.
  function fold() {
    goButton.disabled = !(ends.from && ends.to);
    document.body.classList.toggle('route-compact', mode === 'route' && !wide.matches && folded);
    document.body.classList.toggle('route-quiet', mode === 'route' && !wide.matches && quiet);
    summary.innerHTML = '';
    const places = ends.from && ends.to ? `${nameOf(ends.from)} → ${nameOf(ends.to)}`
      : ends.from ? `${nameOf(ends.from)} → ?` : ends.to ? `? → ${nameOf(ends.to)}` : 'Plan a journey';
    summary.appendChild(el('span', 'route-summary-places', places));
    summary.appendChild(el('span', 'route-summary-time', ends.from && ends.to
      ? `${chip.textContent}${dticket ? ' · D-Ticket only' : ''}` : 'Tap to choose From and To'));
  }
  summary.addEventListener('click', () => { folded = false; fold(); });
  document.getElementById('route-hide').addEventListener('click', () => { closeLists(); folded = true; fold(); });
  // The map touched (a tap or a drag) in Route mode: the map is wanted. The
  // form folds (unless a field is in use) and the sheet's bar says nothing,
  // until the sheet is opened again or connections come.
  document.addEventListener('map-touched', () => {
    if (mode !== 'route' || wide.matches) return;
    if (!form.contains(document.activeElement)) folded = true;
    quiet = true;
    fold();
  });
  document.addEventListener('sheet-changed', () => {
    if (quiet && window.sheet && window.sheet.state() !== 'peek') { quiet = false; fold(); }
  });
  // touching the list while the form is open folds it too (a field in use keeps it open)
  document.getElementById('panel').addEventListener('touchstart', () => {
    if (mode !== 'route' || folded || form.contains(document.activeElement) || state === 'idle') return;
    folded = true;
    fold();
  }, { passive: true });
  wide.addEventListener('change', fold);

  const nameOf = place => (place ? place.name : '');
  const locating = () => Boolean((ends.from && ends.from.locating) || (ends.to && ends.to.locating));

  function statusText() {
    if (state === 'idle' && ends.from && ends.to) return locating() ? 'Finding your location...' : 'Ready: press Search to see the connections.';
    if (state === 'idle') return 'Type a stop, a place or an address, use your location, or tap the map.';
    if (state === 'loading') return locating() ? 'Finding your location...' : 'Looking for connections...';
    if (state === 'error' || state === 'problem') return problem.message;
    const now = new Date(updatedAt);
    const count = journeys.length === 1 ? '1 connection' : `${journeys.length} connections`;
    return updateProblem ? `${count} · Could not update: ${updateProblem}` : `${count} · Updated ${clockFormat.format(now)}${germanTimeNote(now)}`;
  }

  // (the panel's Route part is filled only in Route mode: hidden content would still be in the page)
  function renderHead() {
    head.innerHTML = '';
    if (mode !== 'route') return;
    if (ends.from && ends.to) {
      const title = el('h2', 'route-title');
      title.appendChild(el('span', '', nameOf(ends.from)));
      title.appendChild(el('span', 'route-arrow', ' → '));
      title.appendChild(el('span', '', nameOf(ends.to)));
      head.appendChild(title);
    } else {
      head.appendChild(el('h2', 'route-title', 'Plan a journey'));
    }
    // the small bar of the sheet (phones): the open connection, or the next one
    const shown = journeys.find(j => j.id === openId) || (state === 'ready' ? journeys.find(j => !departed(j)) : null);
    const peek = el('div', 'route-peek');
    if (shown) {
      peek.appendChild(timesOf(shown));
      peek.appendChild(chainOf(shown, true));
      if (openId) {
        const close = el('button', 'peek-close', '✕');
        close.type = 'button';
        close.setAttribute('aria-label', 'Close this connection');
        close.onclick = (e) => { e.stopPropagation(); toggle(null); };
        peek.appendChild(close);
      }
    } else {
      peek.appendChild(el('span', 'route-peek-text', state === 'ready' ? (empty ? 'No connections' : 'No connections left') : statusText()));
    }
    head.appendChild(peek);
    head.appendChild(el('div', state === 'error' || state === 'problem' ? 'route-status late' : 'route-status', statusText()));
  }

  function notice(text, className = 'notice') {
    const box = el('div', className);
    if (text) box.appendChild(el('p', '', text));
    return box;
  }

  function renderBoard() {
    routeBoard.innerHTML = '';
    if (mode !== 'route') return;
    if (state === 'idle' && ends.from && ends.to) {
      routeBoard.appendChild(notice(locating() ? 'Finding your location...'
        : 'Press Search (or Enter, or the keyboard\'s search key in To) to see the connections.', 'notice route-hint'));
      return;
    }
    if (state === 'idle') {
      const box = notice('Where do you want to go?', 'notice route-hint');
      const how = el('ul');
      ['Type a stop, a sight, an address or a street in From and To,',
        'or pick "My location" (the browser asks first),',
        'or tap a stop or any spot on the map.'].forEach(t => how.appendChild(el('li', '', t)));
      box.appendChild(how);
      routeBoard.appendChild(box);
      return;
    }
    if (state === 'loading') {
      const box = el('div', 'notice loading');
      box.setAttribute('role', 'status');
      box.appendChild(el('span', 'spinner'));
      box.appendChild(el('span', '', statusText()));
      routeBoard.appendChild(box);
      return;
    }
    if (state === 'error' || state === 'problem') {
      const box = notice(problem.message);
      if (problem.retry) {
        const again = el('button', 'route-more', 'Try again');
        again.type = 'button';
        again.onclick = () => { problem = null; load(false); };
        box.appendChild(again);
      }
      routeBoard.appendChild(box);
      return;
    }
    if (!journeys.length) {
      routeBoard.appendChild(notice(
        empty === 'too_close' ? 'Start and destination are too close together for a connection: it is quicker to walk.'
          : empty === 'no_timetable' ? 'There is no timetable for that time yet.'
            : dticket ? 'No connections with the D-Ticket found for this time. Try another time, or switch "D-Ticket only" off.'
              : 'No connections found for this time. Try another time.'));
      return;
    }
    routeBoard.appendChild(moreButtons());
    journeys.forEach(j => routeBoard.appendChild(card(j)));
  }

  // Earlier and Later side by side, above the connections (the user's wish)
  function moreButtons() {
    const box = el('div', 'route-more-box');
    ['earlier', 'later'].forEach(direction => {
      const button = el('button', 'route-more', loadingMore === direction ? 'Loading...' : direction === 'earlier' ? 'Earlier' : 'Later');
      button.type = 'button';
      button.dataset.more = direction;
      button.disabled = Boolean(loadingMore);
      button.onclick = () => more(direction);
      box.appendChild(button);
    });
    ['earlier', 'later'].forEach(direction => { if (moreNote[direction]) box.appendChild(el('span', 'route-more-note', moreNote[direction])); });
    return box;
  }

  const departed = j => j.departure.time && new Date(j.departure.time) < Date.now() - 60000;
  const rides = j => j.legs.filter(leg => leg.kind === 'ride');

  // "16:30 → 16:58" (late: the planned time crossed out, the new one in red)
  function timesOf(j) {
    const box = el('span', 'conn-times');
    const day = dayOf(j.departure.time);
    if (day) box.appendChild(el('span', 'conn-day', `${day} `));
    fillTime(box, j.departure);
    box.appendChild(document.createTextNode(' → '));
    fillTime(box, j.arrival);
    return box;
  }

  // the lines in order, with the walks: "🚶 3 min › U7 › 🚶 4 min › S2"
  function chainOf(j, compact = false) {
    const chain = el('span', 'conn-chain');
    let first = true;
    j.legs.forEach(leg => {
      if (leg.kind === 'walk' && (!leg.minutes || compact)) return;
      if (!first) chain.appendChild(el('span', 'sep', '›'));
      first = false;
      if (leg.kind === 'walk') chain.appendChild(el('span', 'walk', `🚶 ${leg.minutes} min`));
      else chain.appendChild(el('span', `${badgeClass(leg, leg.mode)}${leg.from.cancelled ? ' cancelled' : ''}`, leg.line));
    });
    return chain;
  }

  // what may go wrong with a connection, most serious first: [text, class], or null.
  // (The operators' ordinary notices are only in the opened connection.)
  function warningOf(j) {
    const cancelled = rides(j).find(leg => leg.from.cancelled || leg.to.cancelled);
    if (cancelled) return [`✗ ${cancelled.line} is cancelled`, 'conn-warning'];
    const change = j.transfers.find(t => !t.possible);
    if (change) return [`⚠ The change at ${change.at} may not work`, 'conn-warning'];
    const notified = rides(j).find(leg => leg.notices.some(n => n.priority !== 'normal'));
    if (notified) return [`⚠ ${notified.line}: ${notified.notices.find(n => n.priority !== 'normal').title}`, 'conn-notice'];
    return null;
  }

  function card(j) {
    const open = j.id === openId;
    const box = el('div', `conn${open ? ' open' : ''}${departed(j) ? ' departed' : ''}`);
    box.dataset.id = j.id;
    const summary = el('button', 'conn-summary');
    summary.type = 'button';
    summary.setAttribute('aria-expanded', String(open));
    const top = el('div', 'conn-top');
    top.appendChild(timesOf(j));
    top.appendChild(el('span', 'conn-dur', `${duration(j.minutes)} · ${changesText(j.changes)}`));
    summary.appendChild(top);
    summary.appendChild(chainOf(j));
    const meta = el('div', 'conn-meta');
    const warning = warningOf(j);
    const first = rides(j)[0];
    if (departed(j)) meta.appendChild(el('span', 'conn-gone', 'Departed'));
    else if (warning) meta.appendChild(el('span', warning[1], warning[0]));
    else if (first) meta.appendChild(el('span', '', `from ${first.from.name}${first.from.platform.code ? `, ${first.from.platform.name.toLowerCase()}` : ''}`));
    else meta.appendChild(el('span', '', 'on foot'));
    if (j.dticket && rides(j).length) meta.appendChild(el('span', 'dticket', 'D-Ticket'));
    summary.appendChild(meta);
    summary.onclick = () => toggle(open ? null : j.id);
    box.appendChild(summary);
    if (open) box.appendChild(details(j));
    return box;
  }

  // opens a connection (null: closes it); the map draws its route
  function toggle(id) {
    openId = id;
    quiet = false;
    openStops.clear();
    openNotices.clear();
    renderAll();
    document.dispatchEvent(new CustomEvent('route-changed'));
    if (!id) return;
    if (window.sheet && window.sheet.state() === 'peek') document.dispatchEvent(new CustomEvent('sheet-request', { detail: { state: 'list' } }));
    // on a phone with the sheet at half height: the opened connection moves up
    const opened = routeBoard.querySelector('.conn.open');
    if (opened && window.sheet && window.sheet.state()) {
      const below = document.getElementById('panel-head').getBoundingClientRect().bottom;
      panelBox.scrollTop += opened.getBoundingClientRect().top - below - 4;
    }
  }

  // ---------- one connection, step by step ----------
  const MODE_VARS = { u_bahn_tram: 'var(--ubahn)', s_bahn: 'var(--sbahn)', trains: 'var(--rail-light)', regional_train: 'var(--rail-light)',
    long_distance_train: 'var(--rail)', bus: 'var(--bus)', long_distance_bus: 'var(--bus)' };
  const colourOf = leg => (leg.kind === 'walk' ? 'var(--faint)'
    : leg.brand === 'ice' ? 'var(--rail)' : leg.brand === 'flix' ? 'var(--flix)' : MODE_VARS[leg.mode.id] || 'var(--other)');

  // the name of a stop, or of the place chosen (with the address the planner found there)
  function placeName(stop, end) {
    const chosen = end && ends[end];
    if (stop.kind === 'place' && chosen && chosen.kind !== 'stop') return [chosen.name, stop.name !== chosen.name ? stop.name : ''];
    return [stop.name, ''];
  }
  const platformText = stop => (stop.platform && stop.platform.code ? ` · ${stop.platform.name.toLowerCase()}` : '');

  function step(steps, s) { steps.push(s); }

  function transferText(t) {
    if (!t || t.spare_minutes === null) return null;
    if (t.possible) return el('span', 'ok', `${t.spare_minutes} min to spare`);
    const left = Math.max(0, t.spare_minutes + t.needed_minutes);
    return el('span', 'late', `Only ${left} min to change (${t.needed_minutes} needed)`);
  }

  function details(j) {
    const wrap = el('div', 'conn-details');
    // on a phone, when the sheet covers the map (style.css shows it only then)
    const toMap = el('button', 'stops-map', 'Show on map');
    toMap.type = 'button';
    toMap.onclick = (e) => { e.stopPropagation(); document.dispatchEvent(new CustomEvent('sheet-request', { detail: { state: 'half' } })); };
    wrap.appendChild(toMap);

    const steps = [];
    let rideCount = 0;
    j.legs.forEach((leg, i) => {
      const prev = j.legs[i - 1];
      if (leg.kind === 'walk') {
        if (!leg.minutes && !leg.metres) return;
        const [name, address] = placeName(leg.from, i === 0 ? 'from' : null);
        const transfer = prev && prev.kind === 'ride' ? j.transfers[rideCount - 1] : null;
        const to = leg.to.kind === 'place' ? placeName(leg.to, i === j.legs.length - 1 ? 'to' : null)[0] : leg.to.name;
        step(steps, { time: leg.from, name, address, platform: i ? platformText(leg.from) : '', colour: 'walk', dot: prev ? colourOf(prev) : 'var(--text)',
          sub: [`🚶 ${leg.minutes} min walk${leg.metres ? ` (${leg.metres} m)` : ''} to ${to}`, transfer && j.legs[i + 1] ? transferText(transfer) : null] });
        return;
      }
      // a change at the same stop: first where the last ride ends
      if (prev && prev.kind === 'ride') {
        step(steps, { time: prev.to, name: prev.to.name, platform: platformText(prev.to), colour: 'change', dot: colourOf(prev),
          sub: ['Change', transferText(j.transfers[rideCount - 1])] });
      }
      rideCount += 1;
      const [name, address] = placeName(leg.from, i === 0 ? 'from' : null);
      step(steps, { time: leg.from, name, address, platform: platformText(leg.from), colour: colourOf(leg), dot: colourOf(leg), ride: leg, index: i, journey: j });
    });
    const last = j.legs[j.legs.length - 1];
    const [name, address] = placeName(last.to, 'to');
    step(steps, { time: last.to, name, address, platform: platformText(last.to), colour: null, dot: colourOf(last), last: true });

    const list = el('div', 'legs');
    steps.forEach((s, n) => list.appendChild(stepRow(s, n === 0)));
    wrap.appendChild(list);
    return wrap;
  }

  function stepRow(s, first) {
    const row = el('div', `step${first ? ' first' : ''}${s.last ? ' last' : ''}`);
    const t = el('div', 't');
    fillTime(t, s.time);
    row.appendChild(t);
    const rail = el('div', `rail${s.colour === 'walk' ? ' dash' : s.colour === 'change' ? ' thin' : ''}`);
    if (s.colour && s.colour !== 'walk' && s.colour !== 'change') rail.style.setProperty('--leg', s.colour);
    const dot = el('span', 'dot');
    dot.style.setProperty('--leg', s.dot);
    rail.appendChild(dot);
    row.appendChild(rail);
    const what = el('div', 'what');
    what.appendChild(el('span', 'name', `${s.name}${s.platform || ''}`));
    if (s.address) what.appendChild(el('span', 'sub', s.address));
    (s.sub || []).filter(Boolean).forEach(part => {
      if (typeof part === 'string') { what.appendChild(el('span', 'sub', part)); return; }
      const line = el('span', 'sub');
      line.appendChild(part);
      what.appendChild(line);
    });
    if (s.ride) rideInfo(what, s.ride, s.index, s.journey);
    row.appendChild(what);
    return row;
  }

  function rideInfo(what, leg, index, j) {
    const line = el('span', 'ride');
    line.appendChild(el('span', `${badgeClass(leg, leg.mode)}${leg.from.cancelled ? ' cancelled' : ''}`, leg.line));
    line.appendChild(el('span', '', `to ${leg.destination}`));
    if (leg.from.cancelled || leg.to.cancelled) line.appendChild(el('span', 'late', 'Cancelled'));
    if (leg.replacement) line.appendChild(el('span', 'replacement', 'Replacement bus'));
    what.appendChild(line);
    const minutes = leg.from.time && leg.to.time ? minutesBetween(leg.from.time, leg.to.time) : null;
    const count = leg.stops.length + 1;
    const sub = el('span', 'sub', `${count} ${count === 1 ? 'stop' : 'stops'}${minutes !== null ? ` · ${minutes} min` : ''}`);
    const key = `${j.id}|${index}`;
    if (leg.stops.length) {
      sub.appendChild(document.createTextNode(' · '));
      const toggleStops = el('button', 'link-button', openStops.has(key) ? 'hide stops' : 'show stops');
      toggleStops.type = 'button';
      toggleStops.onclick = () => {
        if (openStops.has(key)) openStops.delete(key); else openStops.add(key);
        const held = holdCards();
        renderBoard();
        held();
      };
      sub.appendChild(toggleStops);
    }
    what.appendChild(sub);
    if (openStops.has(key)) {
      const list = el('table', 'stops-list');
      leg.stops.forEach(stop => {
        const row = el('tr', `stop${stop.cancelled ? ' cancelled' : ''}`);
        const cell = el('td', 'stop-time');
        fillTime(cell, stop);
        row.appendChild(cell);
        row.appendChild(el('td', 'stop-name', stop.name));
        list.appendChild(row);
      });
      what.appendChild(list);
    }
    const byTitle = new Map();
    leg.notices.forEach(n => {
      const title = n.title || 'Notice';
      if (!byTitle.has(title)) byTitle.set(title, []);
      byTitle.get(title).push(n);
    });
    [...byTitle.entries()].forEach(([title, notices], k) => {
      const noticeKey = `${key}|${k}`;
      const box = el('details', `route-notice${notices.some(n => n.priority !== 'normal') ? ' high' : ''}`);
      box.open = openNotices.has(noticeKey);
      box.appendChild(el('summary', '', `⚠ ${title}`));
      notices.forEach(n => { if (n.text) box.appendChild(el('p', '', n.text)); });
      box.addEventListener('toggle', () => { if (box.open) openNotices.add(noticeKey); else openNotices.delete(noticeKey); });
      what.appendChild(box);
    });
  }

  // ---------- the map (map.js) ----------
  function drawn() {
    const journey = mode === 'route' ? journeys.find(j => j.id === openId) || null : null;
    document.dispatchEvent(new CustomEvent('route-drawn', {
      detail: { active: mode === 'route', journey, from: mode === 'route' ? ends.from : null, to: mode === 'route' ? ends.to : null },
    }));
  }

  // ---------- from elsewhere ----------
  // "Route from here" / "Route to here" on a stop's departures, and the map's popup
  function startWith(end, place) {
    setMode('route');
    folded = false;
    choose(end, place);
    // the other end is next (a phone's keyboard only comes with a tap in it)
    if (wide.matches && !ends[OTHER[end]]) fields[OTHER[end]].input.focus();
    fold();
  }

  drawChip();
  renderAll();

  window.route = {
    active: () => mode === 'route',
    setMode,
    // the sheet has something to open for (not just the hint)
    hasContent: () => mode === 'route' && state !== 'idle',
    openId: () => openId,
    close: () => toggle(null),
    choose: (end, place) => choose(end, place),
    search,
    folded: () => folded,
    quiet: () => quiet,
    startWith,
    ends: () => ({ from: ends.from, to: ends.to }),
    journeys: () => journeys,
    state: () => state,
    url: () => lastUrl,
  };
})();
