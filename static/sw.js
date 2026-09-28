/* ─── Ovoz AI Studio: Service Worker ──────────────────────────────
 * Strategy:
 *   – Static shell + code (HTML/CSS/JS): network-first, cache only as offline
 *     fallback. A returning visitor must never run the previous build: the
 *     realtime client, its step labels and its i18n dictionary ship together,
 *     so a stale bundle is not "an old look" — it is broken behaviour.
 *   – API calls: network-first, cache fallback only for GET /api/plans, /api/v1/info
 *   – Everything else: network-only (no cache poisoning)
 * ──────────────────────────────────────────────────────────────────── */
const BUILD = '0.27.0';
const VERSION = 'ovoz-v' + BUILD;
const SHELL_CACHE = VERSION + '-shell';
const API_CACHE = VERSION + '-api';

// Code is requested with a build stamp (?v=BUILD) from index.html, so a deploy
// changes the cache key too: even a service worker still running the previous
// release's policy cannot answer /app.js from yesterday's bytes. Browser QA saw
// exactly that mixed-bundle reload when the key was a bare path.
const CODE_PATHS = ['/styles.css', '/i18n.js', '/app.js', '/tg-boot.js', '/sw-boot.js'];

const PRECACHE_URLS = [
  '/',
  `/styles.css?v=${BUILD}`,
  `/i18n.js?v=${BUILD}`,
  `/app.js?v=${BUILD}`,
  `/tg-boot.js?v=${BUILD}`,
  `/sw-boot.js?v=${BUILD}`,
  '/manifest.webmanifest',
];

// ─── Install: precache shell ───
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

// ─── Activate: clean old caches ───
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => !k.startsWith(VERSION))
          .map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
      // Tell every open page which build just took over. sw-boot.js reloads a
      // document whose own ?v= stamps disagree with it — the page that booted on
      // yesterday's code learns about today's instead of waiting for the next
      // navigation. A bare "reload" broadcast would hammer a visitor on their very
      // first install, so the payload is the build and the page decides.
      .then(() => self.clients.matchAll({ type: 'window' }))
      .then((list) => {
        // postMessage returns undefined, not a promise: a closed client has to be
        // caught, not chained.
        for (const c of list) {
          try { c.postMessage('build:' + BUILD); } catch (e) { /* client gone */ }
        }
      })
  );
});

// ─── Messages ───
// A page that boots on top of an already-active worker never sees an 'activate'
// event, so it asks which build is in charge instead of waiting for a navigation to
// notice. Answering is the whole protocol: the page compares and decides.
self.addEventListener('message', (event) => {
  if (event.data === 'whatBuild') {
    try { event.source.postMessage('build:' + BUILD); } catch (e) { /* gone */ }
  }
});

// ─── Fetch: routing strategy ───
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Skip non-GET
  if (event.request.method !== 'GET') return;

  // Skip cross-origin (Telegram CDN, etc.)
  if (url.origin !== self.location.origin) return;

  // API: network-first with graceful offline
  if (url.pathname.startsWith('/api/') || url.pathname === '/metrics') {
    event.respondWith(networkFirst(event.request, API_CACHE));
    return;
  }

  // Code and shell: always revalidate. Cache is the offline escape hatch,
  // never the default answer to a request the network could serve.
  // Matched by pathname so a versioned URL lands in the same branch (and in a
  // cache entry keyed by that versioned URL).
  if (url.pathname === '/' || url.pathname === '/manifest.webmanifest'
      || CODE_PATHS.includes(url.pathname)) {
    event.respondWith(networkFirst(event.request, SHELL_CACHE));
    return;
  }

  // Everything else: network-only
});

// ─── Strategies ───
async function networkFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const response = await fetch(request);
    // Only cache successful GET responses (same-origin code, whitelisted API paths)
    if (response.ok) {
      if (cacheName === SHELL_CACHE) {
        // Pruning must never delay or break the response the page is waiting for.
        pruneAndPut(cache, request, response.clone()).catch(() => {});
      } else if (isCacheableApi(request.url)) {
        cache.put(request, response.clone());
      }
    }
    return response;
  } catch {
    const cached = await cache.match(request);
    if (cached) return cached;
    // Offline fallback for navigations
    if (request.mode === 'navigate') {
      return new Response(
        '<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ovoz — Offline</title><style>body{font-family:system-ui;background:#0b0d12;color:#f2f3f5;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}main{text-align:center;padding:24px}h1{color:#34e2a0;font-size:28px}p{color:#a6adc0;font-size:16px;max-width:38ch;margin:12px auto 0}</style></head><body><main><h1>Ovoz</h1><p>No connection. The studio will resume when you\'re back online.</p></main></body></html>',
        { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
      );
    }
    return new Response('Offline', { status: 503 });
  }
}

async function pruneAndPut(cache, request, response) {
  // One entry per code path, not one per release: the build stamp changes the
  // cache key every deploy, and nothing ever deleted the old one. Left alone the
  // shell cache grows a copy of app.js/i18n.js/styles.css per visit history,
  // including stale ?cb= keys from earlier revisions of this page.
  const path = new URL(request.url).pathname;
  for (const entry of await cache.keys()) {
    const url = entry.request.url;
    if (new URL(url).pathname === path && url !== request.url) await cache.delete(entry);
  }
  await cache.put(request, response);
}

function isCacheableApi(url) {
  const path = new URL(url).pathname;
  return path === '/api/plans' || path === '/api/v1/info' || path === '/healthz';
}
