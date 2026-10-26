// The stop's own web address and the Back button.
//
// Every stop has its own address, /stop/<id> (e.g. /stop/de:08111:6115): it
// can be bookmarked or shared, and opens that stop. The start (no stop) is /.
//
// Back steps through what is open, the most recent thing first:
//   1. an open departure (its list of stops) is closed;
//   2. a full sheet goes down to half (the two screens: the departures
//      screen goes back to the map; a wide screen has no such step);
//   3. the stop shown before, then the start, then the page left.
// Route mode (route.js) lies on top of that: Back first closes an open
// connection, then lowers a full sheet, then goes back to the departures.
//
// How: each stop has one entry in the browser's history, and above it one
// more entry for each thing open (`layers`, 0 to 2), all with the same
// address. Back takes the browser to the entry below: one thing is closed.
// When the user closes a thing themselves, its entry is taken off again.
(function () {
  const START = '/';

  // the address of a stop (stopAddress in departures.js), or the start
  const addressOf = id => (id ? stopAddress(id) : START);
  function stopInAddress() {
    const found = location.pathname.match(/^\/stop\/(.+)$/);
    if (!found) return null;
    try { return decodeURIComponent(found[1]); } catch { return found[1]; }
  }
  // a history entry of this page: {bw: 1, stop, name, layers}
  const entry = (stop, name, layers) => ({ bw: 1, stop, name, layers });
  const here = () => (history.state && history.state.bw ? history.state : null);

  // the things open now that Back closes, counted
  const routeMode = () => Boolean(window.route && window.route.active());
  function openLayers() {
    let n = openDeparture ? 1 : 0;
    if (window.sheet && window.sheet.stateIn('departures') === 'full') n += 1;
    if (routeMode()) {
      // (a search on its way counts as its connection: the first one opens by itself
      // when the answer comes, too late for the browser to let a page add a step then)
      n += 1 + (window.route.openId() || window.route.state() === 'loading' ? 1 : 0);
      if (window.sheet && window.sheet.state() === 'full') n += 1;
    }
    return n;
  }
  // close the one Back closes first
  function closeOne() {
    if (routeMode()) {
      if (window.route.openId()) window.route.close();
      else if (window.route.state() === 'loading') window.route.cancel();
      else if (window.sheet && window.sheet.state() === 'full') window.sheet.set(window.sheet.mode() === 'screens' ? 'peek' : 'half');
      else window.route.setMode('departures');
    } else if (openDeparture) closeOpen();
    else if (window.sheet && window.sheet.state() === 'full') window.sheet.set(window.sheet.mode() === 'screens' ? 'peek' : 'half');
  }

  // ---------- going back by itself ----------
  // A step back this file takes (a closed thing's entry taken off) also
  // reports itself as 'popstate'; those are not the user's.
  let ownSteps = 0;
  let stepsDone = null;
  function goBack(n) {
    return new Promise((resolve) => {
      ownSteps += 1;
      const done = () => { clearTimeout(timer); stepsDone = null; resolve(); };
      // (the browser can't go back that far: then no 'popstate' comes)
      const timer = setTimeout(() => { ownSteps = Math.max(0, ownSteps - 1); done(); }, 600);
      stepsDone = done;
      history.go(-n);
    });
  }

  // A new entry needs the user's own tap or key: browsers skip entries a
  // page adds by itself when Back is pressed. Without one (the page opened at
  // a stop on the two screens, which open the departures screen), the entry is
  // added at the user's next tap.
  const mayAdd = () => !navigator.userActivation || navigator.userActivation.isActive;
  let waitingForTap = false;
  function addLater() {
    if (waitingForTap) return;
    waitingForTap = true;
    const go = () => {
      waitingForTap = false;
      ['pointerdown', 'keydown'].forEach(type => window.removeEventListener(type, go, true));
      update();
    };
    ['pointerdown', 'keydown'].forEach(type => window.addEventListener(type, go, true));
  }

  // One change at a time: the entries are brought in line with the page in
  // order, also when the user presses Back meanwhile.
  let queue = Promise.resolve();
  const later = job => { queue = queue.then(job).catch(() => {}); };

  // ---------- the page changed: the history follows ----------
  let pending = false;
  function update() {
    if (pending) return;
    pending = true;
    later(async () => {
      pending = false;
      let at = here();
      const stop = currentStopId;
      if (!at) { history.replaceState(entry(stop, pickedName, 0), '', addressOf(stop)); at = here(); }
      if (at.stop !== stop) {
        // another stop (or the start): its entry goes right above the
        // previous stop's own entry, without the things open there
        if (at.layers > 0) { await goBack(at.layers); at = here(); }
        if (at.stop !== stop) {
          if (!mayAdd()) { addLater(); return; }
          history.pushState(entry(stop, pickedName, 0), '', addressOf(stop));
          at = here();
        }
      }
      // the name arrives with the departures (an address only has the id)
      if (at.layers === 0 && pickedName && at.name !== pickedName) {
        history.replaceState(entry(stop, pickedName, 0), '', location.href);
        at = here();
      }
      const open = openLayers();
      if (open > at.layers) {
        for (let n = at.layers + 1; n <= open; n++) {
          if (!mayAdd()) { addLater(); return; }
          history.pushState(entry(stop, at.name, n), '', location.href);
        }
      } else if (open < at.layers) {
        await goBack(at.layers - open);
      }
    });
  }
  ['stop-picked', 'stop-shown', 'stop-cleared', 'departure-changed', 'sheet-changed', 'mode-changed', 'route-changed']
    .forEach(type => document.addEventListener(type, update));
  // a screen turned or resized across the phone / wide line: the sheet's step comes or goes
  window.addEventListener('resize', update);

  // ---------- Back (or Forward) was pressed: the page follows ----------
  window.addEventListener('popstate', () => {
    if (ownSteps > 0) {
      ownSteps -= 1;
      if (stepsDone) stepsDone();
      return;
    }
    later(async () => {
      const at = here() || entry(stopInAddress(), '', 0);
      if (at.stop !== currentStopId) {
        if (routeMode()) window.route.setMode('departures');
        if (at.stop) selectStop(at.stop, at.name || '', 'history');
        else closeStop();
        // an entry above a stop's own one (Forward) can't be opened again
        if (at.layers > 0) await goBack(at.layers);
        return;
      }
      // the same stop: close things until as many are open as the entry has
      let open = openLayers();
      while (open > at.layers) {
        closeOne();
        const now = openLayers();
        if (now === open) break;   // (nothing could be closed)
        open = now;
      }
      if (open < at.layers) await goBack(at.layers - open);
    });
  });

  // ---------- the page opens ----------
  const first = stopInAddress();
  const before = here();
  if (before && before.layers > 0 && before.stop === first) {
    // reloaded with something open: nothing is open now, so back to the stop's own entry
    later(() => goBack(before.layers));
  } else {
    // (the address holds the stop only: anything after it is dropped)
    history.replaceState(entry(first, before && before.stop === first ? before.name : '', 0), '', addressOf(first));
  }
  if (first) selectStop(first, (before && before.stop === first && before.name) || '', 'address');

  // for the tests
  window.address = { addressOf, layers: () => (here() ? here().layers : null), entry: here };
})();
