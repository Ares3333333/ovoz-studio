/* ─── Service Worker registration ─────────────────────────────────
 * Extracted from an inline block in index.html so script-src can drop
 * 'unsafe-inline'. Behaviour is unchanged: register /sw.js at window load,
 * adopt a waiting worker immediately when one exists, and reload when the
 * worker announces a new build. Failures stay silent — no worker, no PWA,
 * the page still works — because a first-party storage hiccup must never
 * become a red console error on a marketing page.
 *
 * "announces a new build" is the whole point of the message protocol: the
 * worker sends 'build:<version>' when it activates, and this file compares it
 * with the <version> the document was actually served with (the ?v= stamp on
 * app.js). Equal or unknown → stay put, because a visitor on their very first
 * install must not be reloaded for no reason. Different → reload once, and
 * remember that in sessionStorage so a server serving a stale document next to
 * a fresh worker cannot put the page into a reload loop.
 * ──────────────────────────────────────────────────────────────────── */
if ("serviceWorker" in navigator) {
  var SERVED_BUILD = (function () {
    var s = document.querySelector("script[src*='app.js?v=']");
    var m = s && (s.getAttribute("src") || "").match(/[?&]v=([\d.]+)/);
    return m ? m[1] : "";
  })();

  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js", { scope: "/" })
      .then(function (reg) { if (reg.waiting) reg.waiting.postMessage("skipWaiting"); })
      .catch(function () {});
  });

  navigator.serviceWorker.addEventListener("message", function (e) {
    var data = e.data;
    if (typeof data !== "string" || data.indexOf("build:") !== 0) return;
    var live = data.slice(6);
    if (!SERVED_BUILD || live === SERVED_BUILD) return;
    var GUARD = "ovoz-sw-reload";
    try {
      if (sessionStorage.getItem(GUARD) === live) return;
      sessionStorage.setItem(GUARD, live);
    } catch (err) {
      return;                      // no storage, no reload: never loop
    }
    window.location.reload();
  });
}
