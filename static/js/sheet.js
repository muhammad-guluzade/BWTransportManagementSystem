// The departures panel on phones and tablets (narrower than 1024 px, see
// style.css), in one of two layouts the user picks in the settings (menu.js
// sets body.layout-screens for the second):
//
// "Sheet": a sheet over the map at one of three heights:
//   peek  a small bar: the stop and its next departure (or a hint)
//   half  the map above, the departures below
//   full  only the departures, under the search box
// It moves when its top strip (grip, stop name, small bar) is dragged: a drag
// of SNAP pixels, or a flick, goes on to the next height that way, so it
// needn't be aimed. At the very top of the list, pulling down lowers the
// sheet too; otherwise the list just scrolls (at half and at full height) and
// keeps its position. A tap on the grip goes one height up (full: back to
// half); at full height the strip also has a "Map" button.
//
// "Two screens": no dragging. A bar at the bottom switches between the map
// (with the small bar: peek) and the departures (full).
//
// On wide screens the panel is a plain column and none of this applies.
(function () {
  const panel = document.getElementById('panel');
  const head = document.getElementById('panel-head');
  const grip = document.getElementById('sheet-grip');
  const toMap = document.getElementById('sheet-map');
  const searchBar = document.getElementById('top');
  const screensBar = document.getElementById('screens-bar');
  const narrow = window.matchMedia('(max-width: 1023.98px)');

  // a fast flick (pixels per millisecond) goes on to the next height
  const FLICK = 0.4;
  // ... and so does a drag this long, whatever its speed
  const SNAP = 30;
  // a finger has to move this far before it counts as a drag and not a tap
  const SLOP = 6;
  // the sheet's top at half height, as a share of the screen's height
  const HALF = 0.5;

  let state = 'peek';
  let y = 0;            // how far the sheet is pushed down from its full height, in px
  let drag = null;      // the drag going on: {startX, startY, startSheetY, startState, sideways, moving, lastY, lastT, speed}
  let listTouch = null; // a touch in the list that may become a pull-down: {y}
  let justDragged = false;
  let picked = false;   // a stop has been picked: until then the sheet has nothing to open for
  // the list as the user last saw it, noted when the sheet goes down to the bar
  // (see holdList in departures.js), to be put back when it opens again
  let lastSeen = null;

  const active = () => narrow.matches;
  const screens = () => document.body.classList.contains('layout-screens');
  // Route mode (route.js) has its own content: the connections
  const routeMode = () => document.body.classList.contains('route-mode');
  const hasStop = () => (routeMode() ? Boolean(window.route && window.route.hasContent()) : picked);
  // each mode keeps its own height: going back to one finds the sheet as it was
  const savedState = { departures: 'peek', route: 'peek' };
  // The sheet's parts (the user's design, 19 October). Where a finger comes
  // down decides what the whole touch does, wherever the finger goes after,
  // until it is lifted:
  //   'strip' (the grip, the stop's name, the status line) and 'tabs': up and
  //     down move the sheet; the tabs also scroll sideways, and a touch that
  //     starts sideways there is theirs;
  //   'chips' (the line filter): they only scroll sideways (style.css);
  //   'list': it scrolls; pulled down at its very top, the sheet goes down.
  const zoneOf = target => (!head.contains(target) ? 'list'
    : target.closest('.line-chips') ? 'chips' : target.closest('.tabs') ? 'tabs' : 'strip');
  // the heights there are in the layout, top to bottom
  const heights = () => (screens() ? ['full', 'peek'] : ['full', 'half', 'peek']);

  // where the sheet starts (just below the search box), where it ends (above
  // the bar of the two screens), and its resting places
  function geometry() {
    const top = Math.round(searchBar.getBoundingClientRect().bottom);
    const bottom = screens() ? Math.round(screensBar.getBoundingClientRect().height) : 0;
    const height = window.innerHeight - top - bottom;
    const bottomPad = screens() ? 0 : parseFloat(getComputedStyle(panel).paddingBottom) - 16;   // the phone's home bar, if any
    const peekHeight = Math.min(head.getBoundingClientRect().height + bottomPad + 4, height);
    const half = Math.max(0, Math.round(window.innerHeight * HALF) - top);
    return { top, bottom, height, positions: { full: 0, half: Math.min(half, height - peekHeight), peek: Math.max(0, height - peekHeight) } };
  }

  function place(pixels) {
    y = pixels;
    const g = geometry();
    panel.style.setProperty('--sheet-y', `${Math.round(y)}px`);
    document.documentElement.style.setProperty('--sheet-top', `${g.top}px`);
    document.documentElement.style.setProperty('--sheet-bottom', `${g.bottom}px`);
    // the round buttons on the map stay under the top bar, which is taller in Route mode
    document.documentElement.style.setProperty('--top-bottom', `${g.top}px`);
    // the map's own controls (its credits button) stay above the sheet
    document.documentElement.style.setProperty('--map-covered', `${Math.round(g.height - y + g.bottom)}px`);
  }

  function setState(next, animate = true) {
    if (!active()) return;
    if (!heights().includes(next)) next = next === 'half' ? (screens() ? 'peek' : 'half') : 'peek';
    // without a stop the sheet stays a bar; the two screens may show the empty list (with its hint)
    if (!hasStop() && !screens()) next = 'peek';
    // The list keeps its place: the departure at the top of the view stays
    // where it is when the height changes. Going down to the bar, it is noted
    // as seen with the sheet open, and put back when the sheet opens again,
    // also if the list was redrawn in between (a refresh at the bar).
    const wasOpen = state !== 'peek';
    const putBack = window.holdList ? holdList() : () => {};
    if (wasOpen && next === 'peek') lastSeen = putBack;
    const changed = state !== next;
    state = next;
    document.body.classList.toggle('sheet-animate', animate);
    ['peek', 'half', 'full'].forEach(s => document.body.classList.toggle(`sheet-${s}`, s === state));
    // the head is shorter while the sheet is down (no tabs): measure after the class change
    place(geometry().positions[state]);
    if (state !== 'peek') {
      if (!wasOpen && lastSeen) lastSeen();
      else putBack();
      lastSeen = null;
    }
    grip.setAttribute('aria-label', state === 'full' ? 'Show the map again' : 'Show more of the departures');
    screensBar.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String((b.dataset.screen === 'list') === (state === 'full'))));
    if (changed) document.dispatchEvent(new CustomEvent('sheet-changed'));   // for the Back button (address.js)
  }

  // ---------- dragging (the sheet only) ----------
  // `sideways`: the touch may also turn out to be a sideways one (the tabs)
  function start(clientX, clientY, sideways = false) {
    if (!active() || screens()) return;
    drag = { startX: clientX, startY: clientY, startSheetY: y, startState: state, sideways, moving: false,
      lastY: clientY, lastT: performance.now(), speed: 0 };
  }

  function move(clientX, clientY, event) {
    if (!drag) return;
    const dy = clientY - drag.startY;
    if (!drag.moving) {
      const dx = clientX - drag.startX;
      if (Math.abs(dy) < SLOP && (!drag.sideways || Math.abs(dx) < SLOP)) return;   // not yet: it may still be a tap
      // more sideways than up or down on the tabs: they scroll, the sheet stays
      if (drag.sideways && Math.abs(dx) > Math.abs(dy)) { drag = null; return; }
      drag.moving = true;
      document.body.classList.remove('sheet-animate');
    }
    if (event.cancelable) event.preventDefault();   // the sheet moves, the page doesn't scroll
    place(Math.min(Math.max(drag.startSheetY + dy, 0), geometry().positions.peek));
    const now = performance.now();
    if (now > drag.lastT) drag.speed = (clientY - drag.lastY) / (now - drag.lastT);
    drag.lastY = clientY;
    drag.lastT = now;
  }

  function end() {
    listTouch = null;
    if (!drag) return;
    const { moving, speed, startState } = drag;
    drag = null;
    if (!moving) return;
    justDragged = true;
    setTimeout(() => { justDragged = false; }, 0);
    const { positions } = geometry();
    const order = heights();
    const moved = y - positions[startState];   // down: positive
    const nearest = list => list.reduce((best, s) => (Math.abs(positions[s] - y) < Math.abs(positions[best] - y) ? s : best), list[0]);
    let next = startState;
    if (speed > FLICK) next = order.find(s => positions[s] > y + 1) || 'peek';
    else if (speed < -FLICK) next = [...order].reverse().find(s => positions[s] < y - 1) || 'full';
    else if (moved >= SNAP) next = nearest(order.filter(s => positions[s] > positions[startState])) || startState;
    else if (moved <= -SNAP) next = nearest(order.filter(s => positions[s] < positions[startState])) || startState;
    setState(next);
  }

  // Fingers: what a touch does depends only on where it started (zoneOf)
  panel.addEventListener('touchstart', (e) => {
    listTouch = null;
    drag = null;
    if (!active() || screens()) return;
    const { clientX, clientY } = e.touches[0];
    const zone = zoneOf(e.target);
    if (zone === 'strip' || zone === 'tabs') start(clientX, clientY, zone === 'tabs');
    else if (zone === 'list' && state !== 'peek' && panel.scrollTop <= 0) listTouch = { x: clientX, y: clientY };
  }, { passive: true });
  panel.addEventListener('touchmove', (e) => {
    const { clientX, clientY } = e.touches[0];
    if (!drag && listTouch) {
      const dy = clientY - listTouch.y;
      if (dy > SLOP && panel.scrollTop <= 0) { start(listTouch.x, listTouch.y); listTouch = null; }   // pulled down from the top
      else if (Math.abs(dy) > SLOP) listTouch = null;                                                 // the list's own scrolling
    }
    move(clientX, clientY, e);
  }, { passive: false });
  panel.addEventListener('touchend', end);
  panel.addEventListener('touchcancel', end);
  // a mouse (a narrow browser window on a laptop) drags by the top strip
  head.addEventListener('pointerdown', (e) => {
    const zone = zoneOf(e.target);
    if (e.pointerType !== 'mouse' || e.button !== 0 || (zone !== 'strip' && zone !== 'tabs')) return;
    start(e.clientX, e.clientY, zone === 'tabs');
    const onMove = (ev) => move(ev.clientX, ev.clientY, ev);
    const onUp = () => { window.removeEventListener('pointermove', onMove); window.removeEventListener('pointerup', onUp); end(); };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  });

  // ---------- taps ----------
  grip.addEventListener('click', () => {
    if (justDragged || screens()) return;
    setState(state === 'peek' ? 'half' : state === 'half' ? 'full' : 'half');
  });
  toMap.addEventListener('click', () => setState('half'));
  // a tap on the small bar (the stop, its next departure) opens the departures
  head.addEventListener('click', (e) => {
    if (justDragged || !active() || state !== 'peek' || grip.contains(e.target)) return;
    setState(screens() ? 'full' : 'half');
  });
  // the two screens' bar
  screensBar.addEventListener('click', (e) => {
    const button = e.target.closest('button[data-screen]');
    if (button) setState(button.dataset.screen === 'list' ? 'full' : 'peek');
  });

  // ---------- what the rest of the page asks for ----------
  // a stop was picked: show its departures (a full sheet stays full). Gone
  // back to with the Back button: the sheet stays as it is.
  document.addEventListener('stop-picked', (e) => {
    picked = true;
    lastSeen = null;   // another stop's list
    if (e.detail.source === 'history') return;
    if (screens()) setState('full');
    else if (state === 'peek') setState('half');
  });
  // "Show on map" on a platform heading: get out of the way
  document.addEventListener('platform-marked', (e) => {
    if (e.detail.platform && e.detail.reason === 'heading') setState('peek');
  });
  // a tap on the empty map: the user wants the map (dragging it only looks
  // around); the two screens' map is already the map
  // (in Route mode a tap asks "from here or to here?" instead: map.js)
  document.addEventListener('map-touched', (e) => {
    if (e.detail.kind === 'tap' && !screens() && state !== 'peek' && !routeMode()) setState('peek');
  });
  // Route mode: connections are being looked for: the sheet comes up for them
  document.addEventListener('route-searched', () => {
    if (screens()) setState('full');
    else if (state === 'peek') setState('half');
  });
  // the other mode: its own height again (without content only the bar)
  document.addEventListener('mode-changed', (e) => {
    const now = e.detail.mode;
    savedState[now === 'route' ? 'departures' : 'route'] = state;
    lastSeen = null;
    setState(hasStop() ? savedState[now] : 'peek', false);
  });
  // the stop was given up (departures.js): back to the bar with its hint
  document.addEventListener('stop-cleared', () => {
    picked = false;
    if (routeMode()) savedState.departures = 'peek';
    else setState('peek');
  });
  // a button somewhere asks for a height ("Show platform on map": half, which
  // the two screens make the map; 'list': the departures, half or the list screen)
  document.addEventListener('sheet-request', (e) => {
    const wanted = e.detail.state === 'list' ? (screens() ? 'full' : (state === 'full' ? 'full' : 'half')) : e.detail.state;
    setState(wanted);
  });
  // the layout was changed in the settings: the nearest height there is
  document.addEventListener('layout-changed', () => setState(screens() ? (state === 'peek' ? 'peek' : 'full') : state, false));

  // the sheet's head changes size (another stop, a name on two lines, the
  // tabs shown again) or the screen does (a phone turned): stay at the same
  // height, without cutting short a movement that is under way
  function refit() {
    if (active() !== sheetLayout) { applyLayout(); return; }   // the screen crossed the 1024 px line
    if (!active() || drag) return;
    place(geometry().positions[state]);
  }
  new ResizeObserver(refit).observe(head);
  new ResizeObserver(refit).observe(searchBar);   // the top bar: taller in Route mode, or with a long name
  window.addEventListener('resize', refit);

  let sheetLayout = null;   // the layout last set up: true = sheet, false = side panel
  function applyLayout() {
    sheetLayout = active();
    if (active()) {
      setState(state, false);
    } else {
      // the wide layout: a plain column, nothing of the sheet left behind
      ['peek', 'half', 'full', 'animate'].forEach(s => document.body.classList.remove(`sheet-${s}`));
      panel.style.removeProperty('--sheet-y');
      ['--map-covered', '--sheet-bottom'].forEach(v => document.documentElement.style.removeProperty(v));
    }
  }
  // older iPhones (Safari before iOS 14) only know addListener; resize (refit) checks as well
  if (narrow.addEventListener) narrow.addEventListener('change', applyLayout);
  else narrow.addListener(applyLayout);
  applyLayout();

  window.sheet = {
    state: () => (active() ? state : null),
    mode: () => (active() ? (screens() ? 'screens' : 'sheet') : null),
    // a tap that ends a drag is no tap (departures.js asks)
    wasDragged: () => justDragged,
    set: setState,
    // the height in a mode, also in the one not shown (address.js counts it)
    stateIn: m => (!active() ? null : (routeMode() ? 'route' : 'departures') === m ? state : savedState[m]),
    // how much of the map the search box and the sheet (and the two screens'
    // bar) cover, for map.js to keep what it shows in the free part; null
    // when nothing covers it. A full sheet hides the map: then plan for the
    // height it goes back to (half; the two screens: the map with its bar).
    insets() {
      if (!active()) return null;
      const g = geometry();
      const back = state === 'full' ? (screens() ? 'peek' : 'half') : state;
      return { top: g.top, bottom: Math.max(0, window.innerHeight - (g.top + g.positions[back])) };
    },
  };
})();
