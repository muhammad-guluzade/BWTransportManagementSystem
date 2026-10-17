// Settings: a window over the blurred page, opened with the "⋯" button on
// the map, closed with its x or Escape (not by a tap beside it: the user's
// choice, so a stray tap doesn't close it). It holds the theme,
// what the map's colours mean, and where the data comes from.
// Theme: "Auto" follows the device; "Light" or "Dark" is remembered on this
// device (localStorage) and set as data-theme on <html> (style.css swaps the
// colours; index.html applies it before the page is drawn). Whenever the
// theme in effect changes, a 'theme-changed' event says {dark}: map.js
// changes the map to match.
(function () {
  const toggle = document.getElementById('menu-toggle');
  const settings = document.getElementById('settings');
  const card = settings.querySelector('.settings-card');
  const closeButton = document.getElementById('settings-close');
  const buttons = [...document.querySelectorAll('[data-theme-choice]')];
  const layoutButtons = [...document.querySelectorAll('[data-layout-choice]')];
  const deviceDark = window.matchMedia('(prefers-color-scheme: dark)');

  function chosen() {
    return document.documentElement.dataset.theme || 'auto';
  }

  // is the page dark right now?
  function isDark() {
    const theme = chosen();
    return theme === 'dark' || (theme === 'auto' && deviceDark.matches);
  }

  function announce() {
    buttons.forEach(b => b.setAttribute('aria-pressed', String(b.dataset.themeChoice === chosen())));
    document.dispatchEvent(new CustomEvent('theme-changed', { detail: { dark: isDark() } }));
  }

  function choose(theme) {
    if (theme === 'auto') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
    try {
      if (theme === 'auto') localStorage.removeItem('theme');
      else localStorage.setItem('theme', theme);
    } catch { /* no storage (private window): the choice lasts until the page is closed */ }
    announce();
  }

  function isOpen() {
    return !settings.hidden;
  }

  function open() {
    settings.hidden = false;
    document.body.classList.add('settings-open');
    toggle.setAttribute('aria-expanded', 'true');
    closeButton.focus();
  }

  function close() {
    if (!isOpen()) return;
    settings.hidden = true;
    document.body.classList.remove('settings-open');
    toggle.setAttribute('aria-expanded', 'false');
    toggle.focus();
  }

  toggle.addEventListener('click', (e) => {
    e.stopPropagation();
    open();
  });
  closeButton.addEventListener('click', close);
  document.addEventListener('keydown', (e) => {
    if (!isOpen()) return;
    if (e.key === 'Escape') { e.preventDefault(); close(); }
    // keep the keyboard inside the window while it is open
    if (e.key === 'Tab') {
      const focusable = [...card.querySelectorAll('button, a[href]')];
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
  buttons.forEach(b => b.addEventListener('click', () => choose(b.dataset.themeChoice)));
  // "Auto" follows the device when its setting changes
  const onDeviceChange = () => { if (chosen() === 'auto') announce(); };
  if (deviceDark.addEventListener) deviceDark.addEventListener('change', onDeviceChange);
  else deviceDark.addListener(onDeviceChange);

  // Phone layout: "Sheet" (the default) or "Two screens", remembered on this
  // device; body.layout-screens tells style.css and sheet.js
  function chosenLayout() {
    try { return localStorage.getItem('phoneLayout') === 'screens' ? 'screens' : 'sheet'; } catch { return 'sheet'; }
  }
  function showLayout(layout) {
    document.body.classList.toggle('layout-screens', layout === 'screens');
    layoutButtons.forEach(b => b.setAttribute('aria-pressed', String(b.dataset.layoutChoice === layout)));
  }
  function chooseLayout(layout) {
    try { localStorage.setItem('phoneLayout', layout); } catch { /* no storage: for this visit only */ }
    showLayout(layout);
    document.dispatchEvent(new CustomEvent('layout-changed', { detail: { layout } }));
  }
  layoutButtons.forEach(b => b.addEventListener('click', () => chooseLayout(b.dataset.layoutChoice)));
  showLayout(chosenLayout());   // before sheet.js sets the sheet up

  window.theme = { isDark, choose, chosen };
  window.phoneLayout = { choose: chooseLayout, chosen: () => (document.body.classList.contains('layout-screens') ? 'screens' : 'sheet') };
  window.settings = { open, close, isOpen };
  buttons.forEach(b => b.setAttribute('aria-pressed', String(b.dataset.themeChoice === chosen())));
})();
