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
  let drag = null;      // the drag going on: {startY, startSheetY, startState, moving, lastY, lastT, speed}
  let listTouch = null; // a touch in the list that may become a pull-down: {y}
  let justDragged = false;
  let picked = false;   // a stop has been picked: until then the sheet has nothing to open for
  // the list as the user last saw it, noted when the sheet goes down to the bar
  // (see holdList in departures.js), to be put back when it opens again
  let lastSeen = null;

  const active = () => narrow.matches;
  const screens = () => document.body.classList.contains('layout-screens');
  const hasStop = () => picked;
  // the top strip: the head, except its tabs, links and buttons (they are tapped, the tabs also scroll sideways)
  const grabbable = target => head.contains(target) && !target.closest('.tabs, a, #sheet-map, .peek-close, .fav-star');
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
  function start(clientY) {
    if (!active() || screens()) return;
    drag = { startY: clientY, startSheetY: y, startState: state, moving: false, lastY: clientY, lastT: performance.now(), speed: 0 };
  }

  function move(clientY, event) {
    if (!drag) return;
    const dy = clientY - drag.startY;
    if (!drag.moving) {
      if (Math.abs(dy) < SLOP) return;   // not yet: it may still be a tap
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

  // Fingers: a touch on the top strip drags the sheet; a touch in the list
  // scrolls it, except a pull downwards that starts at the list's very top.
  panel.addEventListener('touchstart', (e) => {
    if (!active() || screens()) return;
    const at = e.touches[0].clientY;
    if (grabbable(e.target)) start(at);
    else if (state !== 'peek' && panel.scrollTop <= 0) listTouch = { y: at };
  }, { passive: true });
  panel.addEventListener('touchmove', (e) => {
    const at = e.touches[0].clientY;
    if (!drag && listTouch) {
      const dy = at - listTouch.y;
      if (dy > SLOP && panel.scrollTop <= 0) { start(listTouch.y); listTouch = null; }   // pulled down from the top
      else if (Math.abs(dy) > SLOP) listTouch = null;                                     // the list's own scrolling
    }
    move(at, e);
  }, { passive: false });
  panel.addEventListener('touchend', end);
  panel.addEventListener('touchcancel', end);
  // a mouse (a narrow browser window on a laptop) drags by the top strip
  head.addEventListener('pointerdown', (e) => {
    if (e.pointerType !== 'mouse' || e.button !== 0 || !grabbable(e.target)) return;
    start(e.clientY);
    const onMove = (ev) => move(ev.clientY, ev);
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
  document.addEventListener('map-touched', (e) => {
    if (e.detail.kind === 'tap' && !screens() && state !== 'peek') setState('peek');
  });
  // the stop was given up (departures.js): back to the bar with its hint
  document.addEventListener('stop-cleared', () => {
    picked = false;
    setState('peek');
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
