// Following a connection (navigator, step 2d). "Follow" on an opened
// connection (route.js) picks it; from then on, while the page is open, this
// file keeps it up to date and the whole app stays usable as before:
//
// - Progress by the clock (no GPS needed, it fails underground): rides and
//   walks whose end has passed are done, the one under way is "now".
// - Live checks every 30 s, and at once when the page is looked at again:
//   every ride's live departure, its live arrival where the user gets off,
//   cancelled or not (GET /api/v1/journeys/live, one request for all).
// - What can go wrong (the user's design): a change that can no longer be
//   made, or a cancelled ride. Then new connections from where the user will
//   be are looked for, each with "Take it" (follow that one instead). There
//   is no "tight change" warning: the live times are shown, the user judges.
// - Remembered on the device until 30 minutes after the arrival, or "Stop
//   following"; a reopened page shows it and checks it at once.
//
// route.js draws it (its board and small bar), map.js its route; the
// pop-up when something goes wrong in Departures mode is here too. Events:
// 'follow-changed' (anything new). Uses API, el(), errorMessage(),
// formatTime() and clockFormat from departures.js.
(function () {
  const KEY = 'followedConnection';
  const CHECK_EVERY_MS = 30000;
  const TICK_MS = 15000;                  // the clock moves on: progress and "in N min"
  const KEEP_AFTER_ARRIVAL_MS = 30 * 60000;
  const LIVE_AHEAD_MS = 3 * 3600000;      // rides further ahead have no live data yet
  const DONE_GRACE_MS = 2 * 60000;        // a ride is still checked this long after its arrival
  const LIVE_PATIENCE_MS = 25000;
  const ALTERNATIVES = 3;
  const ALTERNATIVES_AGAIN_MS = 2 * 60000;   // the same problem: new connections looked for at most this often

  let followed = null;     // {v, journey, from: {param, name, address, kind}, to: {...}, startedAt}
  let live = {};           // leg index -> answer of the live check for that ride ({ok, trip} or {ok: false, error})
  let checkedAt = null;    // Date of the last good check
  let checking = false;
  let checkError = '';
  let alternatives = null; // {key, state: 'loading' | 'ready' | 'none' | 'error', list, message, at}
  let checkTimer = null;
  let tickTimer = null;
  let lastProblemKey = '';  // the pop-up tells about each new problem once
  let seenProblemKey = '';  // ... and not again after the user closed it or looked at it

  const now = () => Date.now();
  const ms = iso => (iso ? new Date(iso).getTime() : NaN);
  const germanParts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Berlin', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });
  function germanText(moment) {
    const p = Object.fromEntries(germanParts.formatToParts(new Date(moment)).map(x => [x.type, x.value]));
    return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
  }
  const announce = () => document.dispatchEvent(new CustomEvent('follow-changed'));

  // ---------- remembered on the device ----------
  function save() {
    try {
      if (followed) localStorage.setItem(KEY, JSON.stringify(followed));
      else localStorage.removeItem(KEY);
    } catch { /* no storage: followed while the page is open */ }
  }
  // What is stored may have been changed by hand or be damaged: only a
  // connection with everything this page reads from it is followed again.
  const isObject = x => Boolean(x) && typeof x === 'object' && !Array.isArray(x);
  const isTime = x => typeof x === 'string' && Number.isFinite(ms(x));
  const maybeTime = x => x === undefined || x === null || isTime(x);
  const maybeList = x => x === undefined || x === null || (Array.isArray(x) && x.every(isObject));
  const place = x => isObject(x) && typeof x.name === 'string';
  const end = x => place(x) && maybeTime(x.time) && maybeTime(x.planned) && (isTime(x.time) || isTime(x.planned))
    && (x.platform === undefined || x.platform === null || isObject(x.platform));
  const usableLeg = leg => isObject(leg) && end(leg.from) && end(leg.to) && maybeList(leg.stops) && maybeList(leg.notices)
    && (leg.kind === 'walk' ? leg.minutes === undefined || Number.isFinite(leg.minutes)
      : leg.kind === 'ride' && typeof leg.line === 'string' && (leg.mode === undefined || leg.mode === null || isObject(leg.mode)));
  const usable = stored => isObject(stored) && stored.v === 1 && place(stored.from) && place(stored.to)
    && isObject(stored.journey) && (typeof stored.journey.id === 'string' || Number.isFinite(stored.journey.id))
    && Array.isArray(stored.journey.legs) && stored.journey.legs.length > 0 && stored.journey.legs.every(usableLeg)
    && isObject(stored.journey.departure) && isObject(stored.journey.arrival)
    && (isTime(stored.journey.arrival.time) || isTime(stored.journey.arrival.planned)) && maybeList(stored.journey.transfers);
  // ... and it must draw: one trial of what the page shows of it
  function draws(stored) {
    const before = followed;
    followed = stored;
    try { summary(view()); return true; } catch { return false; } finally { followed = before; }
  }

  function load() {
    let stored = null;
    try { stored = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch { stored = null; }
    if (!stored) return null;
    // damaged, or arrived more than 30 minutes ago: done with
    const usableNow = usable(stored) && draws(stored);
    if (!usableNow || !(ms(stored.journey.arrival.time || stored.journey.arrival.planned) + KEEP_AFTER_ARRIVAL_MS > now())) {
      try { localStorage.removeItem(KEY); } catch { /* fine */ }
      return null;
    }
    return stored;
  }

  // ---------- the connection with its live times ----------
  // A copy of the journey with what the live checks know: each ride's live
  // departure and arrival, cancellations, the stops in between, the walks
  // after a ride moved with it, the changes worked out anew.
  function liveJourney() {
    if (!followed) return null;
    const j = JSON.parse(JSON.stringify(followed.journey));
    j.legs.forEach((leg, i) => {
      if (leg.kind !== 'ride') return;
      const answer = live[i];
      if (!answer || !answer.ok) return;
      const here = answer.trip.here;
      leg.from = { ...leg.from, time: here.time || leg.from.time, planned: here.planned || leg.from.planned, delay: here.delay,
        realtime: here.realtime, cancelled: Boolean(here.cancelled) };
      if (here.platform && here.platform.code) leg.from.platform = here.platform;
      const onward = answer.trip.onward || [];
      const off = onward.find(s => s.id === leg.to.id);
      if (off) {
        leg.to = { ...leg.to, time: off.time || leg.to.time, planned: off.planned || leg.to.planned, delay: off.delay,
          realtime: off.realtime, cancelled: Boolean(off.cancelled) };
        if (off.platform && off.platform.code) leg.to.platform = off.platform;
      }
      leg.stops = (leg.stops || []).map(stop => {
        const found = onward.find(s => s.id === stop.id);
        return found ? { ...stop, time: found.time, delay: found.delay, cancelled: found.cancelled, realtime: found.realtime } : stop;
      });
      leg.live = true;
    });
    // a walk after a ride starts when that ride arrives
    j.legs.forEach((leg, i) => {
      const prev = j.legs[i - 1];
      if (leg.kind !== 'walk' || !prev || prev.kind !== 'ride' || !prev.to.time) return;
      const start = ms(prev.to.time);
      const length = (leg.minutes || 0) * 60000;
      leg.from = { ...leg.from, time: prev.to.time };
      leg.to = { ...leg.to, time: new Date(start + length).toISOString().replace('.000', '') };
    });
    const first = j.legs[0], last = j.legs[j.legs.length - 1];
    const minutesLate = (time, planned) => (time && planned ? Math.round((ms(time) - ms(planned)) / 60000) : 0);
    j.departure = { ...j.departure, time: first.from.time || j.departure.time };
    j.departure.delay = minutesLate(j.departure.time, j.departure.planned);
    j.arrival = { ...j.arrival, time: last.to.time || j.arrival.time };
    j.arrival.delay = minutesLate(j.arrival.time, j.arrival.planned);
    j.minutes = Math.round((ms(j.arrival.time) - ms(j.departure.time)) / 60000);
    // the changes, with the live times
    const rideIndexes = j.legs.map((leg, i) => (leg.kind === 'ride' ? i : -1)).filter(i => i >= 0);
    j.changesLive = [];
    j.transfers = (j.transfers || []).map((t, k) => {
      const a = j.legs[rideIndexes[k]], b = j.legs[rideIndexes[k + 1]];
      if (!a || !b) return t;
      const plannedGap = (ms(b.from.planned) - ms(a.to.planned)) / 60000;
      const gap = (ms(b.from.time) - ms(a.to.time)) / 60000;
      const needed = t.needed_minutes || 0;
      const spare = Math.floor(gap - needed);
      // missed: the next ride leaves before this one arrives, or the time for the change shrank below what it
      // needs (a change the planner planned tighter than our rule is not called missed while it stays as planned)
      const missed = gap < 0 || (gap < needed && gap < plannedGap - 0.5);
      j.changesLive.push({ from: rideIndexes[k], to: rideIndexes[k + 1], gap, needed, missed });
      return { ...t, spare_minutes: Number.isFinite(spare) ? spare : t.spare_minutes, possible: !missed };
    });
    return j;
  }

  // ---------- where the user is on it, by the clock ----------
  // each leg: 'done', 'now' or 'next'; the phase: before | ride | walk | wait | arrived
  function progress(j) {
    const t = now();
    const legs = j.legs.map(leg => {
      const start = ms(leg.from.time || leg.from.planned), end = ms(leg.to.time || leg.to.planned);
      return { start, end, state: end <= t ? 'done' : start <= t ? 'now' : 'next' };
    });
    const nowIndex = legs.findIndex(l => l.state === 'now');
    const nextIndex = legs.findIndex(l => l.state === 'next');
    let phase;
    if (legs.every(l => l.state === 'done')) phase = 'arrived';
    else if (nowIndex >= 0) phase = j.legs[nowIndex].kind === 'ride' ? 'ride' : 'walk';
    else if (legs.every(l => l.state === 'next')) phase = 'before';
    else phase = 'wait';
    return { legs, phase, nowIndex, nextIndex };
  }

  // ---------- what went wrong ----------
  // the first problem of the journey still ahead: {kind, leg, text, from: {param, name}, when}
  function problemOf(j, prog) {
    const t = now();
    for (let i = 0; i < j.legs.length; i++) {
      const leg = j.legs[i];
      // (a cancelled ride stays a problem after its time: the user cannot have been on it)
      if (leg.kind !== 'ride') continue;
      if (leg.from.cancelled || leg.to.cancelled) {
        // from where the user will be: the start (the first ride, reached on foot from it), else the stop the ride leaves from
        const fromStart = i === firstRideIndex(j) && j.legs[0].kind === 'walk' && t < ms(j.legs[0].to.time || j.legs[0].to.planned);
        const from = fromStart ? { param: followed.from.param, name: followed.from.name, lat: followed.from.lat, lon: followed.from.lon }
          : { param: stopParam(leg.from), name: leg.from.name, id: leg.from.id, lat: leg.from.lat, lon: leg.from.lon };
        return { kind: 'cancelled', leg: i, text: `The ${leg.line} at ${formatTime(leg.from.planned)} is cancelled`, from,
          when: germanText(fromStart ? t : Math.max(t, ms(leg.from.planned) - 60000)), key: `cancelled|${i}|${leg.departure_id}` };
      }
    }
    for (const change of j.changesLive || []) {
      const a = j.legs[change.from], b = j.legs[change.to];
      // (stays a problem after the next ride has left: the user is not on it; new connections from where they are)
      if (!change.missed) continue;
      return { kind: 'missed', leg: change.to, text: `The ${b.line} at ${formatTime(b.from.time)} can no longer be reached`,
        detail: `${a.line} arrives at ${formatTime(a.to.time)}${a.to.delay >= 1 ? ` (${a.to.delay} min late)` : ''}`,
        from: { param: stopParam(a.to), name: a.to.name, id: a.to.id, lat: a.to.lat, lon: a.to.lon }, when: germanText(Math.max(t, ms(a.to.time))),
        key: `missed|${change.to}|${b.departure_id}` };
    }
    return null;
  }
  const firstRideIndex = j => j.legs.findIndex(l => l.kind === 'ride');
  // a stop of the journey as the journeys API wants it: its id when it is a station of the state
  const stopParam = stop => (stop.id && /^de:08\d{3}:/.test(stop.id) ? stop.id : null);

  // ---------- new connections when something went wrong ----------
  async function lookForAlternatives(problem) {
    if (!problem.from.param) {
      alternatives = { key: problem.key, state: 'none', list: [], at: now(),
        message: `New connections from ${problem.from.name} cannot be looked up here (it is outside Baden-Württemberg).` };
      announce();
      return;
    }
    alternatives = { key: problem.key, state: 'loading', list: [], at: now() };
    announce();
    const url = `${API}/journeys?from=${encodeURIComponent(problem.from.param)}&to=${encodeURIComponent(followed.to.param)}`
      + `&when=${encodeURIComponent(problem.when)}`;
    let res;
    try {
      res = await fetch(url);
    } catch {
      if (alternatives && alternatives.key === problem.key) alternatives = { ...alternatives, state: 'error', message: 'Could not look for new connections (no connection?).' };
      announce();
      return;
    }
    if (window.noticeVersion) noticeVersion(res);
    if (!alternatives || alternatives.key !== problem.key) return;
    if (!res.ok) {
      alternatives = { ...alternatives, state: 'error', message: await errorMessage(res, 'Could not look for new connections.') };
      announce();
      return;
    }
    const data = await res.json();
    if (!alternatives || alternatives.key !== problem.key) return;
    // not the ride that is cancelled or out of reach again
    const avoid = followed.journey.legs[problem.leg] && followed.journey.legs[problem.leg].departure_id;
    const list = (data.journeys || []).filter(j => !j.legs.some(leg => leg.departure_id && leg.departure_id === avoid)
      && !j.legs.some(leg => leg.kind === 'ride' && (leg.from.cancelled || leg.to.cancelled))).slice(0, ALTERNATIVES);
    alternatives = { key: problem.key, state: list.length ? 'ready' : 'none', list, at: now(), from: problem.from,
      message: list.length ? '' : 'No other connection found right now.' };
    announce();
  }

  // ---------- the live checks ----------
  function ridesToCheck() {
    const t = now();
    return followed.journey.legs.map((leg, i) => [leg, i]).filter(([leg]) => leg.kind === 'ride' && leg.board_stop
      && ms(leg.to.time || leg.to.planned) + DONE_GRACE_MS > t && ms(leg.from.planned) - t < LIVE_AHEAD_MS);
  }

  async function check() {
    if (!followed || checking) return;
    const rides = ridesToCheck();
    if (!rides.length) { evaluate(); return; }
    checking = true;
    announce();
    const started = followed.startedAt;
    const url = `${API}/journeys/live?${rides.map(([leg]) => `ride=${encodeURIComponent(`${leg.board_stop}/${leg.departure_id}`)}`).join('&')}`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), LIVE_PATIENCE_MS);
    let res;
    try {
      res = await fetch(url, { signal: controller.signal, cache: 'no-store' });
    } catch {
      clearTimeout(timer);
      checking = false;
      if (followed && followed.startedAt === started) { checkError = 'Could not check (no connection?)'; evaluate(); }
      return;
    }
    clearTimeout(timer);
    if (window.noticeVersion) noticeVersion(res);
    if (!followed || followed.startedAt !== started) { checking = false; return; }   // stopped or replaced meanwhile
    if (!res.ok) {
      checking = false;
      checkError = `Could not check: ${await errorMessage(res, 'the server did not answer')}`;
      evaluate();
      return;
    }
    let data;
    try { data = await res.json(); } catch { data = null; }
    checking = false;
    if (!data || !Array.isArray(data.rides)) { checkError = 'Could not check: a broken answer'; evaluate(); return; }
    rides.forEach(([, i], k) => { if (data.rides[k]) live[i] = data.rides[k]; });
    checkedAt = new Date();
    checkError = '';
    evaluate();
  }

  // after a check or a tick: the problem (and new connections for it), the end of the journey
  function evaluate() {
    if (!followed) { announce(); return; }
    const j = liveJourney();
    const prog = progress(j);
    if (prog.phase === 'arrived' && now() > ms(j.arrival.time) + KEEP_AFTER_ARRIVAL_MS) { stop(); return; }
    const problem = problemOf(j, prog);
    if (problem && (!alternatives || alternatives.key !== problem.key
        || (alternatives.state !== 'loading' && now() - alternatives.at > ALTERNATIVES_AGAIN_MS))) {
      lookForAlternatives(problem);
    } else if (!problem) {
      alternatives = null;
    }
    lastProblemKey = problem ? problem.key : '';
    announce();
  }

  function schedule() {
    clearInterval(checkTimer);
    clearInterval(tickTimer);
    if (!followed) return;
    checkTimer = setInterval(() => { if (!document.hidden) check(); }, CHECK_EVERY_MS);
    tickTimer = setInterval(() => { if (!document.hidden) evaluate(); }, TICK_MS);
  }
  // looked at again (phone unlocked, tab back): checked at once
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && followed) check();
  });

  // ---------- starting and stopping ----------
  // info: {from: {param, name, address, kind}, to: {...}}
  function start(journey, info) {
    followed = { v: 1, journey, from: info.from, to: info.to, startedAt: new Date().toISOString() };
    live = {};
    checkedAt = null;
    checkError = '';
    alternatives = null;
    lastProblemKey = seenProblemKey = '';
    save();
    schedule();
    announce();
    check();
  }

  function stop() {
    followed = null;
    live = {};
    alternatives = null;
    checkedAt = null;
    checkError = '';
    lastProblemKey = seenProblemKey = '';
    save();
    schedule();
    hideAlert();
    announce();
  }

  // "Take it": follow the new connection from where the problem left the user
  function take(journey) {
    const first = journey.legs[0].from;
    const from = alternatives && alternatives.from ? alternatives.from : { param: null, name: first.name };
    start(journey, { from: { param: from.param, name: from.name, kind: from.id ? 'stop' : 'place', id: from.id || null,
      lat: from.lat !== undefined ? from.lat : first.lat, lon: from.lon !== undefined ? from.lon : first.lon }, to: followed.to });
  }

  // another tab started or stopped following
  window.addEventListener('storage', (e) => {
    if (e.key !== KEY) return;
    const stored = load();
    if (!stored && followed) { followed = null; live = {}; alternatives = null; schedule(); hideAlert(); announce(); }
    else if (stored && (!followed || stored.startedAt !== followed.startedAt)) {
      followed = stored; live = {}; alternatives = null; schedule(); announce(); check();
    }
  });

  // ---------- the pop-up in Departures mode (2d-3) ----------
  // When something new goes wrong while the user looks at departures: a small
  // note at the top; closed with its x, or tapped to see the connection.
  const alertBox = document.getElementById('follow-alert');
  const wideScreen = window.matchMedia('(min-width: 1024px)');
  // Wide screens: at the top of the map, between its zoom buttons and its round buttons
  // (never over them). Phones: style.css (under the search, beside the round buttons).
  function placeAlert() {
    if (!alertBox) return;
    const map = document.getElementById('map-area');
    if (!wideScreen.matches || !map) { alertBox.style.top = alertBox.style.left = alertBox.style.maxWidth = ''; return; }
    const r = map.getBoundingClientRect();
    alertBox.style.top = `${Math.round(r.top + 12)}px`;
    alertBox.style.left = `${Math.round(r.left + 56)}px`;
    alertBox.style.maxWidth = `${Math.max(160, Math.round(r.width - 56 - 112))}px`;
  }
  window.addEventListener('resize', () => { if (alertBox && !alertBox.hidden) placeAlert(); });
  function showAlert(text) {
    if (!alertBox) return;
    alertBox.querySelector('.follow-alert-text').textContent = text;
    alertBox.hidden = false;
    placeAlert();
  }
  function hideAlert() { if (alertBox) alertBox.hidden = true; }
  if (alertBox) {
    alertBox.querySelector('.follow-alert-close').addEventListener('click', (e) => {
      e.stopPropagation();
      seenProblemKey = lastProblemKey;
      hideAlert();
    });
    alertBox.addEventListener('click', () => {
      seenProblemKey = lastProblemKey;
      hideAlert();
      if (window.route) window.route.showFollowed();
    });
  }
  document.addEventListener('follow-changed', () => {
    const inRoute = window.route && window.route.active() && window.route.followShown();
    if (!followed || !lastProblemKey || inRoute) {
      if (inRoute && lastProblemKey) seenProblemKey = lastProblemKey;   // seen where it is shown
      hideAlert();
      return;
    }
    if (lastProblemKey !== seenProblemKey) {
      const j = liveJourney();
      const problem = problemOf(j, progress(j));
      if (problem) showAlert(`Your connection: ${problem.text}.`);
    }
  });

  // ---------- what route.js and map.js read ----------
  function view() {
    if (!followed) return null;
    const j = liveJourney();
    const prog = progress(j);
    const problem = problemOf(j, prog);
    return { journey: j, original: followed.journey, from: followed.from, to: followed.to, progress: prog, problem,
      alternatives: problem && alternatives && alternatives.key === problem.key ? alternatives : null,
      checkedAt, checking, checkError, anyLive: Object.values(live).some(a => a && a.ok),
      noLive: j.legs.some(leg => leg.kind === 'ride' && !leg.board_stop) };
  }

  // the first line of the follow card and of the small bar: what is now, or next
  function summary(v) {
    const { journey: j, progress: p } = v;
    const t = now();
    const minutesTo = time => Math.max(0, Math.round((ms(time) - t) / 60000));
    // "in 5 min", or "now" within the minute (the user: not "in 0 min")
    const inMinutes = time => (minutesTo(time) === 0 ? 'now' : `in ${minutesTo(time)} min`);
    if (p.phase === 'arrived') return `Arrived at ${v.to.name}`;
    if (p.phase === 'before') {
      const first = j.legs[0];
      const ride = j.legs.find(l => l.kind === 'ride');
      if (first.kind === 'walk') return `Start walking ${inMinutes(first.from.time)} (${formatTime(first.from.time)}) to ${first.to.name}`;
      return `${ride.line} leaves ${inMinutes(ride.from.time)} (${formatTime(ride.from.time)}${ride.from.platform && ride.from.platform.code ? `, ${ride.from.platform.name.toLowerCase()}` : ''})`;
    }
    const leg = j.legs[p.nowIndex >= 0 ? p.nowIndex : p.nextIndex];
    if (p.phase === 'ride') return `Now: ${leg.line} to ${leg.to.name}, off at ${formatTime(leg.to.time)}`;
    if (p.phase === 'walk') return `Now: walk to ${leg.to.kind === 'place' ? v.to.name : leg.to.name}, ${leg.minutes} min`;
    // waiting at a change
    return `Next: ${leg.line} at ${formatTime(leg.from.time)}${leg.from.platform && leg.from.platform.code ? `, ${leg.from.platform.name.toLowerCase()}` : ''} (${inMinutes(leg.from.time)})`;
  }

  // ---------- on load: a connection followed before ----------
  followed = load();
  if (followed) {
    schedule();
    // checked at once (after the page has drawn itself)
    setTimeout(() => { if (followed) check(); }, 300);
  }

  window.follow = {
    active: () => Boolean(followed),
    id: () => (followed ? followed.journey.id : null),
    start,
    stop,
    take,
    check,
    view,
    summary,
    hideAlert,
    // for the tests
    _state: () => ({ followed, live, alternatives, checkedAt, checkError, lastProblemKey, seenProblemKey }),
  };
})();
