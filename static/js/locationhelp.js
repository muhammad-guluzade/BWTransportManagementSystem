// "How to allow your location": a window with the steps for each kind of
// device (the user's wish: people who are not good with devices can fix it
// themselves). The device in hand is guessed and its steps are open; the
// others are one tap away (helping someone else). Opened from the message
// shown when the location could not be used (route.js); closed with its x
// or Escape, like the other windows.
(function () {
  const win = document.getElementById('location-help');
  const card = win.querySelector('.settings-card');
  const body = document.getElementById('location-help-body');
  const closeButton = document.getElementById('location-help-close');
  let opener = null;

  // [id, title, [[what, [steps]], ...]]
  const GUIDES = [
    ['ios', 'iPhone and iPad', [
      ['Safari (and the icon on your Home Screen)', [
        'Open the Settings app.',
        'Tap "Privacy & Security", then "Location Services". Make sure Location Services is switched on.',
        'In the same list, scroll down and tap "Safari Websites". Choose "While Using the App" (or "Ask Next Time Or When I Share"). Switch "Precise Location" on.',
        'Go back to Safari and open this page. Tap "aA" on the left of the address bar, then "Website Settings", then "Location", and choose "Allow" (or "Ask").',
        'Reload the page and tap "My location" again. If Safari asks, tap "Allow".',
      ]],
      ['Chrome', [
        'Open the Settings app, tap "Privacy & Security", then "Location Services". Make sure it is switched on.',
        'In the same list, tap "Chrome" and choose "While Using the App". Switch "Precise Location" on.',
        'Go back to Chrome, reload this page and tap "My location" again. If Chrome asks, tap "Allow".',
      ]],
    ]],
    ['android', 'Android phones and tablets', [
      ['Chrome', [
        'Swipe down from the top of the screen and make sure "Location" is switched on.',
        'Open the Settings app, tap "Apps", then "Chrome", then "Permissions", then "Location", and choose "Allow only while using the app". Switch "Use precise location" on.',
        'Go back to Chrome and open this page. Tap the small icon on the left of the address, then "Permissions" (or "Site settings"), and switch "Location" on.',
        'If that is not there: tap the three dots at the top right, then "Settings", "Site settings", "Location". Switch it on and make sure this page is not under "Blocked".',
        'Reload the page and tap "My location" again. If Chrome asks, tap "Allow".',
      ]],
      ['Samsung Internet', [
        'Swipe down from the top of the screen and make sure "Location" is switched on.',
        'Open the Settings app, tap "Apps", then "Samsung Internet", then "Permissions", then "Location", and choose "Allow only while using the app".',
        'In Samsung Internet, tap the menu (three lines), then "Settings", "Sites and downloads", "Site permissions", "Location", and remove this page from the blocked sites.',
        'Reload the page and tap "My location" again.',
      ]],
      ['Firefox', [
        'Make sure "Location" is switched on (swipe down from the top of the screen).',
        'Open the Settings app, tap "Apps", "Firefox", "Permissions", "Location", and choose "Allow only while using the app".',
        'In Firefox, tap the lock on the left of the address and allow "Location" for this page.',
        'Reload the page and tap "My location" again.',
      ]],
    ]],
    ['windows', 'Windows computers', [
      ['Windows itself', [
        'Open "Settings" (the gear in the Start menu), then "Privacy & security", then "Location".',
        'Switch on "Location services", "Let apps access your location" and "Let desktop apps access your location".',
      ]],
      ['Chrome or Edge', [
        'On this page, click the icon on the left of the address (a lock, or two sliders).',
        'Find "Location" and set it to "Allow" (in Edge: "Permissions for this site", then "Location").',
        'Reload the page and click "My location" again.',
      ]],
      ['Firefox', [
        'On this page, click the icon on the left of the address and remove the block next to "Access your location".',
        'Reload the page, click "My location" again and choose "Allow".',
      ]],
      ['Good to know', [
        'Most computers have no GPS: the position is estimated from Wi-Fi and can be a few hundred metres off.',
      ]],
    ]],
    ['mac', 'Mac computers', [
      ['macOS itself', [
        'Click the Apple menu, "System Settings", then "Privacy & Security", then "Location Services".',
        'Switch "Location Services" on, and switch it on for your browser (Safari, Chrome or Firefox) in the list.',
      ]],
      ['Safari', [
        'On this page, open the "Safari" menu, then "Settings for This Website" (or "Settings", then "Websites", then "Location").',
        'Set "Location" for this page to "Allow" (or "Ask").',
        'Reload the page and click "My location" again.',
      ]],
      ['Chrome or Firefox', [
        'Click the icon on the left of the address and set "Location" to "Allow".',
        'Reload the page and click "My location" again.',
      ]],
    ]],
    ['other', 'Linux and other devices', [
      ['In the browser', [
        'Click or tap the icon on the left of the address and allow "Location" for this page.',
        'Check that the device has location (or "location services") switched on in its own settings.',
        'Reload the page and choose "My location" again.',
      ]],
    ]],
  ];

  function deviceOf() {
    const ua = navigator.userAgent;
    if (/iPhone|iPad|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)) return 'ios';
    if (/Android/.test(ua)) return 'android';
    if (/Windows/.test(ua)) return 'windows';
    if (/Macintosh|Mac OS X/.test(ua)) return 'mac';
    return 'other';
  }

  function draw() {
    body.innerHTML = '';
    const mine = deviceOf();
    body.appendChild(el('p', 'settings-note', 'Your device or browser does not let this page use your location. Follow the steps for your device. '
      + 'You can always type a stop or an address instead.'));
    // the device in hand first, open
    const order = [...GUIDES.filter(g => g[0] === mine), ...GUIDES.filter(g => g[0] !== mine)];
    order.forEach(([id, title, parts]) => {
      const box = el('details', 'help-device');
      box.dataset.device = id;
      box.open = id === mine;
      box.appendChild(el('summary', '', id === mine ? `${title} (this device)` : title));
      parts.forEach(([what, steps]) => {
        box.appendChild(el('h3', '', what));
        const list = el('ol');
        steps.forEach(step => list.appendChild(el('li', '', step)));
        box.appendChild(list);
      });
      body.appendChild(box);
    });
  }

  function open() {
    opener = document.activeElement;
    draw();
    win.hidden = false;
    document.body.classList.add('settings-open');
    closeButton.focus();
  }

  function close() {
    if (win.hidden) return;
    win.hidden = true;
    document.body.classList.remove('settings-open');
    if (opener && opener.focus) opener.focus();
  }

  closeButton.addEventListener('click', close);
  document.addEventListener('keydown', (e) => {
    if (win.hidden) return;
    if (e.key === 'Escape') { e.preventDefault(); close(); }
    if (e.key === 'Tab') {
      const focusable = [...card.querySelectorAll('button, summary')];
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  window.locationHelp = { open, close, device: deviceOf };
})();
