// The theme chosen in Settings (menu.js), applied before anything is drawn;
// none chosen: the device's. Loaded in the page's head, before the styles, so
// the page never flashes in the other theme. (Its own file, not written into
// the page: the page's security policy runs no script written into it.)
try {
  const t = localStorage.getItem('theme');
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
} catch (e) { /* no storage: the device's theme */ }
