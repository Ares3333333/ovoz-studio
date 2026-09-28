/* ─── Telegram Mini App bootstrap ─────────────────────────────────
 * Loads telegram-web-app.js and publishes window.TG_WEBAPP_READY, which app.js
 * polls (see telegramBoot). This file exists so index.html needs no inline
 * script and no inline event-handler attribute: script-src can then drop
 * 'unsafe-inline', and a stored XSS in the SPA stops being an executing one.
 *
 * Parsing this file defines the flag synchronously, exactly like the inline
 * statement it replaced, so app.js never sees `undefined` where it expected
 * false. The loader attaches its own callbacks in code, which CSP does not
 * treat as inline.
 * ──────────────────────────────────────────────────────────────────── */
window.TG_WEBAPP_READY = false;

(function loadTelegramWebApp() {
  var script = document.createElement("script");
  script.async = true;
  script.src = "https://telegram.org/js/telegram-web-app.js";
  script.onload = function () { window.TG_WEBAPP_READY = true; };
  // Outside Telegram (a normal browser tab) the host is unreachable or absent;
  // the flag stays false, the watcher in app.js expires, and the page behaves
  // exactly as it did with the old `onerror="void 0"`.
  script.onerror = function () { window.TG_WEBAPP_READY = false; };
  document.head.appendChild(script);
})();
