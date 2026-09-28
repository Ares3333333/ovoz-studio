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
      .then(function (reg) {
        if (reg.waiting) reg.waiting.postMessage("skipWaiting");
        // A worker checks for a new version on navigation, and a tab that is never
        // navigated — opened from a launcher, left in the background — keeps running
        // yesterday's shell indefinitely. Browser QA saw exactly that: the server
        // had moved to a new build, the tab still executed the old one, and only a
        // manual reload fixed it. So ask, once, on load.
        return reg.update ? reg.update().catch(function () {}) : null;
      })
      .catch(function () {});
  });

  function adoptBuild(live) {
    if (!live || !SERVED_BUILD || live === SERVED_BUILD) return;
    var GUARD = "ovoz-sw-reload";
    try {
      if (sessionStorage.getItem(GUARD) === live) return;
      sessionStorage.setItem(GUARD, live);
    } catch (err) {
      return;                      // no storage, no reload: never loop
    }
    window.location.reload();
  }

  navigator.serviceWorker.addEventListener("message", function (e) {
    var data = e.data;
    if (typeof data !== "string" || data.indexOf("build:") !== 0) return;
    adoptBuild(data.slice(6));
  });

  // The announce-on-activate path only fires for a page that is already open when
  // a worker takes over. A page that boots on top of an already-active worker of a
  // different build hears nothing at all — so it asks. `controller` is the worker
  // actually in charge of this document, which is exactly the thing worth knowing.
  navigator.serviceWorker.ready
    .then(function (reg) {
      if (reg.active) reg.active.postMessage("whatBuild");
    })
    .catch(function () {});
}
