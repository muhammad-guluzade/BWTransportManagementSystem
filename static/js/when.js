// The time the departures start from: now (the default), or a date and time
// the user chooses, as in DB Navigator. A setting while browsing: it applies
// to every stop until "Now" is pressed, and a reload starts at now again.
//
// In the panel's head: "‹" one hour earlier, the chip ("Now", or the chosen
// time; it opens a window with the browser's own date and time field), "✕"
// back to now, "›" one hour later. Times are German time, like everything on
// the page. The board asks for them with `window.when.param()` and redraws
// on 'when-changed'.
(function () {
  const bar = document.getElementById('when-bar');
  const chip = document.getElementById('when-chip');
  const reset = document.getElementById('when-reset');
  const earlier = document.getElementById('when-earlier');
  const later = document.getElementById('when-later');
  const win = document.getElementById('when-window');
  const card = win.querySelector('.settings-card');
  const closeButton = document.getElementById('when-close');
  const nowButton = document.getElementById('when-now');
  const field = document.getElementById('when-input');
  const showButton = document.getElementById('when-show');
  const zone = document.getElementById('when-zone');
  const range = document.getElementById('when-range');

  const HOUR_MS = 3600 * 1000;
  // German local time as the field writes it: "2026-10-25T08:00"
  const partsFormat = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Berlin', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });
  const weekday = new Intl.DateTimeFormat('en-GB', { weekday: 'short', timeZone: 'UTC' });
  const longDay = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' });

  let chosen = null;          // "YYYY-MM-DDTHH:MM" (German time), or null for now
  let first = null;           // the timetable period, "YYYY-MM-DD" (from /api/v1/)
  let last = null;

  function germanNow() {
    const p = Object.fromEntries(partsFormat.formatToParts(new Date()).map(x => [x.type, x.value]));
    return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
  }

  // plain clock arithmetic on "YYYY-MM-DDTHH:MM" (what the clock on the wall shows)
  const asDate = text => new Date(text + ':00Z');
  const asText = date => date.toISOString().slice(0, 16);
  const add = (text, hours) => asText(new Date(asDate(text).getTime() + hours * HOUR_MS));

  const earliest = () => (first ? `${first}T00:00` : null);
  const latest = () => (last ? `${last}T23:59` : null);
  const inRange = text => (!earliest() || text >= earliest()) && (!latest() || text <= latest());
  const clamp = text => (earliest() && text < earliest() ? earliest() : latest() && text > latest() ? latest() : text);

  // "Today, 08:00", "Tomorrow, 08:00", "Sat 25.10., 08:00" (no year: the
  // timetable covers less than one)
  function label(text) {
    const [day, time] = text.split('T');
    const today = germanNow().split('T')[0];
    if (day === today) return `Today, ${time}`;
    if (day === add(`${today}T12:00`, 24).split('T')[0]) return `Tomorrow, ${time}`;
    const [, m, d] = day.split('-');
    return `${weekday.format(asDate(`${day}T12:00`))} ${d}.${m}., ${time}`;
  }

  function draw() {
    chip.textContent = chosen ? label(chosen) : 'Now';
    chip.classList.toggle('chosen', Boolean(chosen));
    chip.title = chosen ? 'Choose another time' : 'Choose a date and time';
    reset.hidden = !chosen;
    const base = chosen || germanNow();
    earlier.disabled = Boolean(earliest()) && add(base, -1) < earliest();
    later.disabled = Boolean(latest()) && add(base, 1) > latest();
  }

  function set(text) {
    const next = text ? clamp(text) : null;
    if (next === chosen) return;
    chosen = next;
    draw();
    document.dispatchEvent(new CustomEvent('when-changed', { detail: { when: chosen } }));
  }

  // the device's clock shows another time than Germany's: the field means German time
  function otherClock() {
    const device = new Date();
    const local = `${device.getFullYear()}-${String(device.getMonth() + 1).padStart(2, '0')}-${String(device.getDate()).padStart(2, '0')}T`
      + `${String(device.getHours()).padStart(2, '0')}:${String(device.getMinutes()).padStart(2, '0')}`;
    return local !== germanNow();
  }

  function isOpen() {
    return !win.hidden;
  }

  function open() {
    field.value = chosen || germanNow();
    if (earliest()) field.min = earliest();
    if (latest()) field.max = latest();
    zone.hidden = !otherClock();
    range.textContent = first && last
      ? `The timetable covers ${longDay.format(asDate(`${first}T12:00`))} to ${longDay.format(asDate(`${last}T12:00`))}.`
      : '';
    win.hidden = false;
    document.body.classList.add('settings-open');
    chip.setAttribute('aria-expanded', 'true');
    field.focus();
  }

  function close() {
    if (!isOpen()) return;
    win.hidden = true;
    document.body.classList.remove('settings-open');
    chip.setAttribute('aria-expanded', 'false');
    chip.focus();
  }

  function show() {
    // a half-filled field (date without time, or the reverse) has no value
    const value = field.value.slice(0, 16);
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) {
      range.textContent = 'Please choose a date and a time.';
      field.focus();
      return;
    }
    if (!inRange(value)) {
      range.textContent = first && last
        ? `There is no timetable for that time yet: it covers ${longDay.format(asDate(`${first}T12:00`))} to ${longDay.format(asDate(`${last}T12:00`))}.`
        : 'There is no timetable for that time.';
      field.focus();
      return;
    }
    close();
    set(value);
  }

  chip.addEventListener('click', (e) => { e.stopPropagation(); open(); });
  reset.addEventListener('click', (e) => { e.stopPropagation(); set(null); });
  earlier.addEventListener('click', (e) => { e.stopPropagation(); set(add(chosen || germanNow(), -1)); });
  later.addEventListener('click', (e) => { e.stopPropagation(); set(add(chosen || germanNow(), 1)); });
  closeButton.addEventListener('click', close);
  nowButton.addEventListener('click', () => { close(); set(null); });
  showButton.addEventListener('click', show);
  field.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); show(); } });
  document.addEventListener('keydown', (e) => {
    if (!isOpen()) return;
    if (e.key === 'Escape') { e.preventDefault(); close(); }
    // keep the keyboard inside the window while it is open
    if (e.key === 'Tab') {
      const focusable = [...card.querySelectorAll('button, input')];
      const firstOne = focusable[0], lastOne = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === firstOne) { e.preventDefault(); lastOne.focus(); }
      else if (!e.shiftKey && document.activeElement === lastOne) { e.preventDefault(); firstOne.focus(); }
    }
  });

  // the timetable period: which times can be chosen at all
  fetch('/api/v1/').then(res => res.json()).then(data => {
    if (data.stop_data) {
      first = data.stop_data.valid_from;
      last = data.stop_data.valid_until;
      draw();
    }
  }).catch(() => { /* no limits known: every time can be asked for */ });

  draw();
  // the arrows' limits move with the clock
  setInterval(draw, 60000);

  window.when = {
    value: () => chosen,                     // null: now
    label: () => (chosen ? label(chosen) : 'now'),
    param: () => (chosen ? `&when=${encodeURIComponent(chosen)}` : ''),
    range: () => [first, last],
    set,
    open,
    close,
    isOpen,
  };
})();
