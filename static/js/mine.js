// "Mine": the user's favourite stops and platforms, and the stops opened
// last. Kept in this browser only (each device has its own; no account). If
// the browser keeps nothing (a private window, blocked site data), they last
// as long as the page is open.
(function () {
  const FAVOURITES = 'favourites';
  const RECENT = 'recentStops';
  // how many stops opened last are kept
  const RECENT_KEPT = 8;

  const memory = {};   // used when the browser's storage can't be used
  function read(key) {
    try {
      const text = localStorage.getItem(key);
      if (text !== null) return JSON.parse(text);
    } catch { /* nothing stored, or storage blocked */ }
    return memory[key] || [];
  }
  function write(key, list) {
    memory[key] = list;
    try { localStorage.setItem(key, JSON.stringify(list)); } catch { /* kept in memory only */ }
    document.dispatchEvent(new CustomEvent('mine-changed'));
  }
  // another tab of this site changed them
  window.addEventListener('storage', (e) => {
    if (e.key === FAVOURITES || e.key === RECENT) document.dispatchEvent(new CustomEvent('mine-changed'));
  });

  // A favourite is a whole stop:
  //   {stop: {id, name}}
  // or one platform of it, as the board names it, with the tab it is on:
  //   {stop: {id, name}, platform: {mode, modeName, id, name, tab}}
  const valid = f => f && f.stop && typeof f.stop.id === 'string' && typeof f.stop.name === 'string'
    && (!f.platform || (typeof f.platform.mode === 'string' && typeof f.platform.name === 'string'));
  const favourites = () => read(FAVOURITES).filter(valid);
  // the same favourite: the same stop, or the same platform of it (by its
  // name; its id only helps to find it again, see findPlatform)
  const sameFavourite = (a, b) => a.stop.id === b.stop.id && !a.platform === !b.platform
    && (!a.platform || (a.platform.mode === b.platform.mode && a.platform.name === b.platform.name));

  // the platform of a board a platform favourite means: the one with its
  // name; if no platform has that name any more (a direction label can
  // change), the one with its id. Names first, because train tracks can
  // share an id under different names. Returns [mode, platform] or null.
  function findPlatform(favourite, data) {
    const mode = data.modes.find(m => m.id === favourite.platform.mode);
    if (!mode) return null;
    const platform = mode.platforms.find(p => p.name === favourite.platform.name)
      || (favourite.platform.id && mode.platforms.find(p => p.id === favourite.platform.id));
    return platform ? [mode, platform] : null;
  }

  window.mine = {
    favourites,
    isFavouriteStop: id => favourites().some(f => f.stop.id === id && !f.platform),
    // the favourite (if any) that means this platform of the board shown
    favouriteFor(stopId, mode, platform, data) {
      return favourites().find(f => f.stop.id === stopId && f.platform && f.platform.mode === mode.id
        && (found => found && found[1] === platform)(findPlatform(f, data))) || null;
    },
    add(favourite) {
      if (!valid(favourite)) return;
      const list = favourites().filter(f => !sameFavourite(f, favourite));
      list.push(favourite);
      write(FAVOURITES, list);
    },
    remove(favourite) {
      write(FAVOURITES, favourites().filter(f => !sameFavourite(f, favourite)));
    },
    // back at its old place in the list (Undo in the favourites window)
    insert(favourite, index) {
      if (!valid(favourite)) return;
      const list = favourites().filter(f => !sameFavourite(f, favourite));
      list.splice(Math.max(0, Math.min(index, list.length)), 0, favourite);
      write(FAVOURITES, list);
    },
    same: sameFavourite,
    findPlatform,

    // the stops opened last, newest first: [{id, name}]
    recent: () => read(RECENT).filter(s => s && typeof s.id === 'string' && typeof s.name === 'string'),
    remember(stop) {
      const list = window.mine.recent().filter(s => s.id !== stop.id);
      list.unshift({ id: stop.id, name: stop.name });
      write(RECENT, list.slice(0, RECENT_KEPT));
    },
    forget(id) {
      write(RECENT, window.mine.recent().filter(s => s.id !== id));
    },
    forgetAll() {
      write(RECENT, []);
    },
  };
})();
