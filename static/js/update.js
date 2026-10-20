// A new version of the app was put online while this page was open. Every
// answer of the API names the version of the page (the X-App-Version header,
// app.py); when it is not the one this page was built with, a small bar
// offers to reload. A page nobody is looking at (a tab in the background)
// reloads by itself. The address keeps the stop, so a reload opens it again.
(function () {
  const built = (document.querySelector('meta[name="app-version"]') || {}).content || '';
  const bar = document.getElementById('update-bar');
  let outdated = false;

  // given each API answer (departures.js, map.js)
  function notice(res) {
    const version = res && res.headers ? res.headers.get('X-App-Version') : null;
    if (outdated || !built || !version || version === built) return;
    outdated = true;
    if (document.hidden) { location.reload(); return; }
    bar.hidden = false;
  }

  // the bar was shown, and now the page is left in the background: reload unseen
  document.addEventListener('visibilitychange', () => { if (outdated && document.hidden) location.reload(); });
  bar.querySelector('button').addEventListener('click', () => location.reload());

  window.noticeVersion = notice;
})();
