// Light or dark, as on gentlebits.net: the visitor's choice if they made one, else the system's. Runs before the
// stylesheet paints (a classic script in <head>), so the page never flashes the other theme.
(function () {
  var root = document.documentElement;
  var KEY = 'gb-theme';
  var mq = window.matchMedia('(prefers-color-scheme: dark)');
  function stored() {
    var t = null;
    try { t = localStorage.getItem(KEY); } catch (e) {}
    return t === 'light' || t === 'dark' ? t : null;
  }
  root.dataset.theme = stored() || (mq.matches ? 'dark' : 'light');
  // Follow the system while the visitor has not chosen.
  mq.addEventListener('change', function (e) { if (!stored()) root.dataset.theme = e.matches ? 'dark' : 'light'; });
})();
