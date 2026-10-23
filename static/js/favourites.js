// The favourites window: the stops and platforms saved with a star (mine.js),
// opened with the star button beside the "⋯" on the map, closed with its x or
// Escape, like the settings. A tap on one opens it (departures.js). Its star
// takes it out of the favourites; the row stays, greyed, with "Undo" until
// the window closes. With more than a few favourites a search field filters
// them.
//
// Opening the window also asks the server for the departures of the first
// favourites, so that it has them at hand (it keeps them half a minute) when
// one is tapped.
(function () {
  const toggle = document.getElementById('favourites-toggle');
  const win = document.getElementById('favourites');
  const card = win.querySelector('.settings-card');
  const closeButton = document.getElementById('favourites-close');
  const filter = document.getElementById('favourites-filter');
  const list = document.getElementById('favourites-list');

  // the search field appears with more favourites than this
  const FILTER_FROM = 5;
  // how many favourites (from the top) are prepared when the window opens
  const PREPARE = 8;
  // ... and not again for the same one within this time (the server keeps them 30 s)
  const PREPARE_AGAIN_MS = 25000;

  let removed = [];          // taken out while the window is open: [{favourite, index}]
  const prepared = new Map();   // favourite's board address -> when it was asked for

  // lower case, without accents and umlauts, so "muhl" finds "Mühl..."
  const fold = text => text.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/ß/g, 'ss');

  const detailOf = f => (f.platform ? `${f.platform.name} · ${f.platform.modeName}` : 'Stop');
  const boardAddress = f => `/api/v1/stops/${encodeURIComponent(f.stop.id)}/departures?tab=${encodeURIComponent(f.platform ? f.platform.tab || '' : '')}&trains=together${window.when.param()}`;

  function isOpen() {
    return !win.hidden;
  }

  function row(favourite, gone) {
    const item = document.createElement('div');
    item.className = gone ? 'fav-row removed' : 'fav-row';
    item.setAttribute('role', 'listitem');
    const text = document.createElement('span');
    text.className = 'fav-text';
    const name = document.createElement('span');
    name.className = 'fav-name';
    name.textContent = favourite.stop.name;
    const detail = document.createElement('span');
    detail.className = 'fav-detail';
    detail.textContent = gone ? 'Removed from favourites' : detailOf(favourite);
    text.append(name, detail);
    item.appendChild(text);

    if (gone) {
      const undo = document.createElement('button');
      undo.type = 'button';
      undo.className = 'fav-undo';
      undo.textContent = 'Undo';
      undo.onclick = () => {
        const back = removed.find(r => window.mine.same(r.favourite, favourite));
        removed = removed.filter(r => r !== back);
        window.mine.insert(favourite, back ? back.index : 0);
      };
      item.appendChild(undo);
      return item;
    }
    // the stop's or platform's name opens it
    const openIt = document.createElement('button');
    openIt.type = 'button';
    openIt.className = 'fav-open';
    openIt.setAttribute('aria-label', `Open ${favourite.stop.name}${favourite.platform ? ', ' + favourite.platform.name : ''}`);
    openIt.onclick = () => { close(); openFavourite(favourite); };
    item.prepend(openIt);
    item.appendChild(starButton(true, 'Remove from favourites', () => {
      const index = window.mine.favourites().findIndex(f => window.mine.same(f, favourite));
      removed.push({ favourite, index });
      window.mine.remove(favourite);
    }));
    return item;
  }

  function render() {
    const favourites = window.mine.favourites();
    list.innerHTML = '';
    // the favourites, with the ones just taken out where they were
    const shown = favourites.map(f => [f, false]);
    removed.slice().sort((a, b) => a.index - b.index).forEach(({ favourite, index }) => {
      shown.splice(Math.min(index, shown.length), 0, [favourite, true]);
    });
    filter.hidden = shown.length <= FILTER_FROM;
    if (!shown.length) {
      const empty = document.createElement('p');
      empty.className = 'fav-empty';
      empty.textContent = 'No favourites yet. Tap the ☆ next to a stop’s name, or on a platform’s heading, to keep it here.';
      list.appendChild(empty);
      return;
    }
    const words = filter.hidden ? [] : fold(filter.value).split(/\s+/).filter(Boolean);
    const matching = shown.filter(([f]) => {
      const text = fold(`${f.stop.name} ${detailOf(f)}`);
      return words.every(word => text.includes(word));
    });
    matching.forEach(([f, gone]) => list.appendChild(row(f, gone)));
    if (!matching.length) {
      const none = document.createElement('p');
      none.className = 'fav-empty';
      none.textContent = `No favourite matches "${filter.value.trim()}".`;
      list.appendChild(none);
    }
  }

  // ask the server for the departures of the first favourites now
  function prepare() {
    const now = Date.now();
    window.mine.favourites().slice(0, PREPARE).forEach(f => {
      const address = boardAddress(f);
      if (now - (prepared.get(address) || 0) < PREPARE_AGAIN_MS) return;
      prepared.set(address, now);
      fetch(address, { priority: 'low' }).catch(() => { /* only a head start */ });
    });
  }

  function open() {
    removed = [];
    filter.value = '';
    render();
    win.hidden = false;
    document.body.classList.add('settings-open');
    toggle.setAttribute('aria-expanded', 'true');
    closeButton.focus();
    prepare();
  }

  function close() {
    if (!isOpen()) return;
    win.hidden = true;
    removed = [];
    document.body.classList.remove('settings-open');
    toggle.setAttribute('aria-expanded', 'false');
    toggle.focus();
  }

  toggle.addEventListener('click', (e) => {
    e.stopPropagation();
    open();
  });
  closeButton.addEventListener('click', close);
  filter.addEventListener('input', render);
  document.addEventListener('mine-changed', () => { if (isOpen()) render(); });
  document.addEventListener('keydown', (e) => {
    if (!isOpen()) return;
    if (e.key === 'Escape') { e.preventDefault(); close(); }
    // keep the keyboard inside the window while it is open
    if (e.key === 'Tab') {
      const focusable = [...card.querySelectorAll('button, input:not([hidden])')];
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  window.favouritesWindow = { open, close, isOpen };
})();
