// ─── Ovoz Studio: клиентская логика ───────────────────────────
// landing ↔ studio, Telegram Mini App, jobs, глоссарий, плеер субтитров.

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let token = localStorage.getItem("ovoz_token") || null;
let chosenFile = null;
let jobType = "subtitles";

// Session state that the boot path may touch before the file finished
// evaluating. `Telegram.WebApp` can already be present when app.js runs, so
// tryLogin() → showStudio() → refreshAll() happens mid-script: anything declared
// later with let/const is in its temporal dead zone there, and the failure is an
// unhandled rejection that silently loses the first refresh of the page.
let _jobsFetch = null;    // the /api/jobs GET currently in flight
let _jobsQueued = null;   // opts of requests that arrived while it was running
let _allFetch = null;     // the boot/login refresh pass currently in flight
// Bumped whenever the viewer changes identity. A response that was requested
// under the previous account must be dropped, not painted: clearing the state in
// adopt()/logout() is worthless if the GET already in flight still renders rows,
// which is exactly how a signed-out panel could redisplay the old jobs.
let _sessionGen = 0;

// ─── API-обёртка + toast + локализованные ошибки ───
const SERVER_ERROR_MAP = {
  'Contact already registered': 'err_contact_exists',
  'Invalid contact or secret': 'err_auth_failed',
  'Secret must be at least': 'err_secret_short',
  'Contact is required': 'err_need_contact',
  'Insufficient credits': 'credits_short',
  'Too many active jobs': 'err_rate_limited',
  'Too many failed attempts': 'err_rate_limited',
  'Admin key required': 'err_forbidden',
  'File too large': 'err_file_too_large',
  'Body too large': 'err_file_too_large',
  'Job not found': 'err_not_found',
  'Artifact not ready': 'err_not_ready',
  'Payment webhooks disabled': 'err_pay_disabled',
  'Amount does not match': 'err_amount_unmatched',
  'Unauthorized': 'err_session_expired',
  'Invalid token': 'err_session_expired',
  'Rate limit exceeded': 'err_rate_limited',
  'text too long': 'err_file_too_large',
  // Pipeline failure reasons land in job.error, which used to be printed raw.
  'job exceeded': 'err_timeout',
  'timeout': 'err_timeout',
  // Rows the restart-recovery wrote before this build; the job is neither timed
  // out nor the user's fault, so it needs its own honest sentence.
  'worker restart': 'err_interrupted',
};
function localizeServerError(msg) {
  // FastAPI 422 sends detail as a list, a proxy sends nothing at all.
  const s = String(msg ?? "");
  for (const [pattern, key] of Object.entries(SERVER_ERROR_MAP)) {
    if (s.startsWith(pattern)) return t(key);
  }
  return s;
}
// Endpoints that create a session: a 401 here is a wrong password, not an
// expired session. Logging out mid-login wiped the form and told the visitor
// their (non-existent) session had expired.
const AUTH_ENDPOINTS = ["/api/auth/login", "/api/auth/register", "/api/auth/telegram"];
const isAuthCall = (path) => AUTH_ENDPOINTS.includes(path.split("?")[0]);
async function api(path, opts = {}) {
  const headers = opts.headers || {};
  if (token) headers["Authorization"] = "Bearer " + token;
  const resp = await fetch(path, { ...opts, headers });
  if (resp.status === 401 && !isAuthCall(path)) { logout(); throw new Error(t("err_session_expired")); }
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const raw = body.detail || httpFallback(resp);
    const err = new Error(localizeServerError(raw));
    err.code = resp.status;
    throw err;
  }
  return body;
}
function httpFallback(resp) {
  const known = {
    402: t("credits_short"), 403: t("err_forbidden"),
    404: t("err_not_found"), 429: t("err_rate_limited"),
  };
  // Never surface the server's English status line, and never rely on it:
  // statusText is the empty string over HTTP/2, which would hide the failure.
  return known[resp.status] || t(resp.status >= 500 ? "err_generic" : "err_request_failed");
}
// blob downloads use fetch(), not api(): same localized failure text
const httpError = (status) => new Error(httpFallback({ status }));
function toast(msg, isErr = false) {
  const wrap = $("#toasts");
  // модалка через showModal() лежит над всеми z-index; отзыв во время
  // открытой модалки обязан жить в том же top layer — иначе клиент молчит.
  // Порядок отрисовки в top layer = порядок добавления: если тост уже был
  // открыт ДО модалки, модалка добавлена позже и перекрывает её. Поэтому
  // каждый тост обязан ВЫНУТЬ контейнер из top layer и ВЕРНУТЬ его — иначе
  // повторный showPopover() бросает InvalidStateError и тост снова под модалкой.
  try {
    if (wrap.showPopover) {
      if (wrap.isOpen) wrap.hidePopover();
      wrap.showPopover();
    }
  } catch (e) { /* браузер без popover — обычный fixed-рендер ниже */ }
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  wrap.appendChild(el);
  // one feedback funnel: the phone feels what the screen just said
  haptic(isErr ? "error" : "success");
  setTimeout(() => el.remove(), 4200);
}
// экранирование для любого innerHTML — защита от stored-XSS
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Minutes are billed as floats, so the arithmetic leaks binary noise into copy
// that sits next to the customer's money: 4.430000000000001 must read "4.43".
const fmtMinutes = (n) => {
  const v = Number(n);
  if (!Number.isFinite(v)) return "0";
  const r = Math.round(v * 100) / 100;
  return Number.isInteger(r) ? String(r) : r.toFixed(2).replace(/0+$/, "");
};

// ─── hero waveform: «голос» бренда ───
(function wave() {
  const cv = $("#wave");
  if (!cv || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const ctx = cv.getContext("2d");
  let t = 0, raf;
  function size() { cv.width = cv.clientWidth * devicePixelRatio; cv.height = cv.clientHeight * devicePixelRatio; }
  size(); addEventListener("resize", size);
  function frame() {
    if (document.hidden) { raf = requestAnimationFrame(frame); return; }
    t += 0.014;
    ctx.clearRect(0, 0, cv.width, cv.height);
    const mid = cv.height * 0.55;
    for (let layer = 0; layer < 3; layer++) {
      ctx.beginPath();
      const amp = (26 + layer * 18) * devicePixelRatio;
      const speed = 1 + layer * 0.55, freq = 0.0055 - layer * 0.0011;
      for (let x = 0; x <= cv.width; x += 4) {
        const y = mid + Math.sin(x * freq + t * speed) * amp
          * Math.sin(x * 0.0009 + t * 0.4 + layer);
        x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      }
      ctx.strokeStyle = layer === 1 ? "rgba(52,226,160,.30)" : "rgba(52,226,160,.13)";
      ctx.lineWidth = (1.4 - layer * 0.3) * devicePixelRatio;
      ctx.stroke();
    }
    raf = requestAnimationFrame(frame);
  }
  frame();
})();

// ─── the layers a "back" action can mean ───────────────────────────
// A Mini App is one webview with no browser chrome: on Android the system back
// gesture CLOSES THE APP unless the app claims BackButton. So the stack of what is
// on top of what has to be tracked in one place, and it has to be the same place
// the browser's own back/Escape already uses. Native <dialog> fires `toggle` on
// every open and close — it does not bubble, but it is caught in the capture
// phase, which means a dialog added next month is tracked without anybody
// remembering to wire it.
const _openDialogs = [];
const studioIsOpen = () => !$("#studio").classList.contains("hidden");

function tgWebApp() { return (window.Telegram && window.Telegram.WebApp) || null; }

// Inside a real Mini App client or not? `telegram-web-app.js` also loads in an
// ordinary browser tab, where it reports platform "web", an empty initData and an
// older SDK level — and every BackButton/HapticFeedback call there prints a vendor
// warning into a console that is otherwise clean. Live QA caught exactly that.
// Mini App-only surfaces are claimed only where they exist.
function tgInsideApp() {
  const tg = tgWebApp();
  return !!(tg && tg.platform && tg.platform !== "web" && tg.initData);
}

function tgSyncBack() {
  const tg = tgWebApp();
  const bb = tg && tg.BackButton;
  if (!bb || !tgInsideApp()) return;
  const depth = _openDialogs.length + (studioIsOpen() ? 1 : 0);
  try { depth > 0 ? bb.show() : bb.hide(); } catch (e) { /* older SDK */ }
}

function tgGoBack() {
  if (_openDialogs.length) { _openDialogs[_openDialogs.length - 1].close(); return; }
  // the toggle handler owns the stack; closing is the only edit it needs
  if (studioIsOpen()) showLanding();
}

document.addEventListener("toggle", (e) => {
  const dlg = e.target;
  if (!dlg || dlg.tagName !== "DIALOG") return;
  if (dlg.open) { if (!_openDialogs.includes(dlg)) _openDialogs.push(dlg); }
  else {
    const i = _openDialogs.indexOf(dlg);
    if (i >= 0) _openDialogs.splice(i, 1);
  }
  tgSyncBack();
}, true);

// Haptics belong at the one place feedback is born, not at 14 call sites that can
// forget it. Outside Telegram the SDK is absent and this costs nothing.
function haptic(style) {
  const tg = tgWebApp();
  const hf = tg && tg.HapticFeedback;
  if (!hf || !tgInsideApp()) return;
  try {
    if (style === "error") hf.notificationOccurred("error");
    else if (style === "success") hf.notificationOccurred("success");
    else hf.selectionChanged();
  } catch (e) { /* not supported on this client */ }
}

// ─── навигация landing ↔ studio + hash routing ───
function showStudio() {
  // Only a real landing → studio transition costs a refresh. An authorized load
  // of #studio enters this function twice — once from the hash route, once from
  // the Telegram autologin a few hundred ms later — and the second pass refetched
  // me/jobs/glossary/notifications because the first pass had already resolved.
  const entering = $("#studio").classList.contains("hidden");
  // демо-секции витрины (.turn-demo … .nafis-demo) прячутся вместе с лендингом:
  // публичный QA поймал — студия открывалась на 3200px НИЖЕ живого демо, и
  // нажатие «открыть студию» выглядело как ничего не происходящее.
  $$(".hero, .how, .ling-demo, .turn-demo, .qator-demo, .jimlik-demo, .soz-demo, .nafis-demo, .caps, .pricing, .foot, .nav-links").forEach(el => el.classList.add("hidden"));
  $("#studio").classList.remove("hidden");
  $("#btn-open-studio").classList.add("hidden");
  window.scrollTo(0, 0);   // и для deep-link /#studio, и для кнопки: студию видно сразу
  if (!token) $("#auth-card").classList.remove("hidden");
  history.replaceState(null, "", "#studio");
  tgSyncBack();
  if (entering) refreshAll();
}
function showLanding() {
  $$(".hero, .how, .ling-demo, .turn-demo, .qator-demo, .jimlik-demo, .soz-demo, .nafis-demo, .caps, .pricing, .foot").forEach(el => el.classList.remove("hidden"));
  $(".nav-links").classList.remove("hidden");
  $("#studio").classList.add("hidden");
  $("#btn-open-studio").classList.remove("hidden");
  history.replaceState(null, "", location.pathname);
  tgSyncBack();
}
$("#btn-open-studio").addEventListener("click", showStudio);
$$("[data-open-studio]").forEach(b => b.addEventListener("click", showStudio));
$("#btn-back").addEventListener("click", showLanding);

// Icon-only chrome must never look dead: signed-out clicks answer with the
// auth form instead of silently doing nothing.
function requireAuth() {
  showStudio();
  toast(t("need_login"), true);
  const field = $("#f-contact");
  if (field) setTimeout(() => field.focus(), 250);
}

// ─── live language-engine demo (the moat) ───
(function lingDemo() {
  const ta = $("#ling-text");
  if (!ta) return;
  const out = $("#ling-result");
  const metrics = $("#ling-metrics");
  const official = $("#ling-official");
  let dir = "latin";
  let timer = null;
  let seq = 0;
  $$(".ling-dir").forEach(b => b.addEventListener("click", () => {
    dir = b.dataset.dir;
    $$(".ling-dir").forEach(x => {
      x.classList.toggle("is-active", x === b);
      x.setAttribute("aria-checked", x === b ? "true" : "false");
    });
    // official apostrophes only exist in the legacy-Latin target — the control
    // is inert for Cyrillic/new-Latin, so disable it instead of faking output
    official.disabled = dir !== "latin";
    official.closest("label")?.classList.toggle("is-disabled", dir !== "latin");
    run();
  }));
  official.addEventListener("change", run);
  ta.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 240); });
  const SCRIPT_KEYS = { latin: "scriptLatin", cyrillic: "scriptCyrillic", mixed: "scriptMixed", other: "scriptOther" };
  let last = null; // last analyze payload: lets a locale change repaint without refetching
  function paintMetrics(a) {
    const scr = t(SCRIPT_KEYS[a.script] || "scriptOther");
    metrics.innerHTML =
      `<li><b>${esc(t("lingMetricScript"))}</b><span>${esc(scr)}</span></li>` +
      `<li><b>${esc(t("lingMetricConf"))}</b><span>${Math.round((a.confidence || 0) * 100)}%</span></li>` +
      `<li><b>${esc(t("lingMetricWords"))}</b><span>${a.words || 0}</span></li>` +
      `<li><b>${esc(t("lingMetricSwitch"))}</b><span>${a.code_switch_hints || 0}</span></li>`;
  }
  // UZ/RU/EN switch must relocalise the tiles too, not only the buttons
  window.__lingRepaint = () => { if (last) paintMetrics(last); };
  async function run() {
    const text = ta.value.trim().slice(0, 5000); // matches the server-side cap
    const my = ++seq;
    const useDir = dir, useOff = official.checked; // capture state BEFORE await:
    // an in-flight response must render for the request it was made for, not
    // whatever button happens to be highlighted when it lands
    if (!text) { out.textContent = ""; metrics.innerHTML = ""; last = null; return; }
    try {
      // One request: analyze() already carries both transliterations + official.
      const a = await api("/api/v1/ling/analyze", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text })
      });
      if (my !== seq) return; // a newer keystroke superseded this response
      last = a;
      let result;
      if (useDir === "cyrillic") result = a.cyrillic;
      else if (useDir === "new_latin") result = a.new_latin;
      else result = useOff ? a.official : a.latin;
      out.textContent = result;
      paintMetrics(a);
    } catch (e) {
      if (my !== seq) return;
      out.textContent = "";
      metrics.innerHTML = `<li class="ling-err">${esc(e.message)}</li>`;
    }
  }
})();

// ─── Ovoz Turn demo: who holds the floor (the second moat) ───
(function turnDemo() {
  const box = $("#turn-lines");
  const btn = $("#turn-run");
  if (!box || !btn) return;
  const metrics = $("#turn-metrics");
  // A two-voice interview exactly as an ASR hands it over: timings and words,
  // nothing else. The attribution is computed by the server on every click —
  // hardcoding the answer here would make the demo a picture of the product
  // instead of the product, and the first regression would be invisible.
  const SAMPLE = [
    { start: 0.0, end: 3.4, text: "Assalomu alaykum, ustoz, vaqtingizni olganim uchun uzr, boshlaymizmi?" },
    { start: 3.6, end: 7.9, text: "Va alaykum assalom, xush kelibsiz, men tayyorman." },
    { start: 8.2, end: 12.5, text: "Sizning so'nggi loyihangiz haqida so'rasam bo'ladimi?" },
    { start: 12.8, end: 19.1, text: "Albatta, biz uni Farg'ona vodiyida ikki oyda ishga tushirdik." },
    { start: 19.4, end: 22.0, text: "Qiyin bo'lmadi mi?" },
    { start: 22.3, end: 28.8, text: "Qiyin edi, lekin jamoa tajribali edi, shuning uchun hammasi o'z vaqtida bo'ldi." },
    { start: 29.2, end: 32.5, text: "Oxirgi savol: endi nimani rejalashtirmoqdasiz?" },
    { start: 32.8, end: 37.4, text: "Kelasi yil uchun ikkinchi vodiy loyihasini rejalashtirganmiz." }
  ];
  let seq = 0;
  let last = null; // last answer: a locale switch repaints from it, zero requests

  function paint(answer) {
    // Unattributed lines still render, with an empty author slot: the demo must
    // look like a transcript waiting to be solved, not like a broken widget.
    box.innerHTML = SAMPLE.map((s, i) => {
      const ln = answer ? answer.lines[i] : null;
      const sp = ln ? ln.speaker : 0;
      // Chips cycle over four palettes: a eight-voice panel stays readable and no
      // inline style (CSP) and no extra class per speaker is needed.
      return `<li class="turn-line${sp ? " sp" + ((sp - 1) % 4 + 1) : ""}">` +
        `<span class="turn-who">${sp ? "S" + sp : "·"}</span>` +
        `<span class="turn-text">${esc(ln ? ln.text : s.text)}</span></li>`;
    }).join("");
    if (!answer) { metrics.innerHTML = ""; return; }
    metrics.innerHTML =
      `<li><b>${esc(t("turn_speakers"))}</b><span>${answer.speakers}</span></li>` +
      `<li><b>${esc(t("turn_turns"))}</b><span>${answer.turns}</span></li>` +
      `<li><b>${esc(t("turn_share"))}</b><span>${Math.round((answer.longest_share || 0) * 100)}%</span></li>` +
      `<li><b>${esc(t("turn_conf"))}</b><span>${Math.round((answer.avg_confidence || 0) * 100)}%</span></li>`;
    // The button now says "run again", which is state and not a dictionary entry:
    // lock it, and let __turnRepaint relocalise it on a locale switch.
    btn.dataset.i18nLocked = "1";
    btn.textContent = t("turn_again");
  }
  window.__turnRepaint = () => { if (last) paint(last); };
  async function run() {
    const my = ++seq;   // a second click supersedes the first response, not the button
    btn.disabled = true;
    try {
      const a = await api("/api/v1/ling/diarize", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lines: SAMPLE })
      });
      if (my !== seq) return;
      last = a;
      paint(a);
    } catch (e) {
      if (my !== seq) return;
      metrics.innerHTML = `<li class="ling-err">${esc(e.message)}</li>`;
    } finally {
      if (my === seq) btn.disabled = false;
    }
  }
  btn.addEventListener("click", run);
  paint(null);
})();

// ─── Ovoz Qator demo: is it actually readable (the third moat layer) ───
(function qatorDemo() {
  const box = $("#qator-cards");
  const btn = $("#qator-run");
  if (!box || !btn) return;
  const metrics = $("#qator-metrics");
  // Subtitles as a raw ASR export delivers them: correct words, unreadable layout.
  // The layout is computed by the server on every click for the same reason the
  // Turn demo is: a hardcoded before/after would be a picture of the product.
  const SAMPLE = [
    { start: 0.0, end: 3.2, text: "Assalomu alaykum hurmatli tomoshabinlar, bugun biz yangi loyihani ko'rib chiqamiz." },
    { start: 3.4, end: 7.1, text: "Bu loyiha Farg'ona vodiyida ikki oy ichida ishga tushirildi va butun jamoa juda yaxshi ishladi." },
    { start: 7.3, end: 9.0, text: "Rahmat." }
  ];
  let seq = 0;
  let last = null;

  function fmt(sec) {
    const m = Math.floor(sec / 60), s = sec - m * 60;
    return `${m}:${s.toFixed(2).padStart(5, "0")}`;
  }
  function paint(answer) {
    const rows = answer ? answer.cards : SAMPLE.map((s, i) => ({
      i: i + 1, start: s.start, end: s.end, text: s.text,
      cps: Math.round((s.text.length / Math.max(0.01, s.end - s.start)) * 10) / 10
    }));
    box.innerHTML = rows.map(c => `<li class="q-card${answer ? " fixed" : ""}">` +
      `<span class="q-time">${fmt(c.start)} → ${fmt(c.end)}</span>` +
      `<span class="q-text">${esc(c.text)}</span>` +
      `<span class="q-cps">${c.cps == null ? "—" : c.cps} c/s</span></li>`).join("");
    if (!answer) { metrics.innerHTML = ""; return; }
    const b = answer.before, a = answer.after;
    metrics.innerHTML =
      `<li><b>${esc(t("qator_score"))}</b><span>${b.score} → ${a.score} (${a.grade})</span></li>` +
      `<li><b>${esc(t("qator_cards"))}</b><span>${b.cards} → ${a.cards}</span></li>` +
      `<li><b>${esc(t("qator_flags"))}</b><span>${b.findings.length} → ${a.findings.length}</span></li>` +
      `<li><b>${esc(t("qator_words"))}</b><span>${answer.words_preserved ? "✓" : "✗"}</span></li>`;
    btn.dataset.i18nLocked = "1";
    btn.textContent = t("qator_again");
  }
  window.__qatorRepaint = () => { if (last) paint(last); };
  async function run() {
    const my = ++seq;
    btn.disabled = true;
    try {
      const a = await api("/api/v1/ling/layout", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lines: SAMPLE })
      });
      if (my !== seq) return;
      last = a;
      paint(a);
    } catch (e) {
      if (my !== seq) return;
      metrics.innerHTML = `<li class="ling-err">${esc(e.message)}</li>`;
    } finally {
      if (my === seq) btn.disabled = false;
    }
  }
  btn.addEventListener("click", run);
  paint(null);
})();

// ─── Ovoz Jimlik demo: did the cut land in a real pause (fourth moat layer) ───
// The one language widget that has to hear something to answer, so the tape is
// synthesised here, in the open: three bursts of syllable-modulated tone with
// room tone between them. A base64 recording baked into the bundle would look the
// same on screen and prove nothing — the engine would be answering a prop.
function wavMono16(samples, rate) {
  const n = samples.length;
  const buf = new ArrayBuffer(44 + n * 2);
  const view = new DataView(buf);
  const text = (at, s) => { for (let i = 0; i < s.length; i++) view.setUint8(at + i, s.charCodeAt(i)); };
  text(0, "RIFF"); view.setUint32(4, 36 + n * 2, true); text(8, "WAVE");
  text(12, "fmt "); view.setUint32(16, 16, true);   // PCM chunk, 16 bytes
  view.setUint16(20, 1, true);                       // format 1: uncompressed PCM
  view.setUint16(22, 1, true);                       // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);                // bytes per second
  view.setUint16(32, 2, true);                       // block align
  view.setUint16(34, 16, true);                      // bits per sample
  text(36, "data"); view.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) {
    const v = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

(function jimlikDemo() {
  const box = $("#jimlik-rows");
  const btn = $("#jimlik-run");
  if (!box || !btn) return;
  const metrics = $("#jimlik-metrics");
  const RATE = 8000;
  // speech | pause | speech | pause | speech — the pauses are the truth the
  // subtitles below were timed without.
  const TAPE = [[1.5, 0.30], [0.5, 0.002], [1.5, 0.30], [0.5, 0.002], [1.5, 0.30]];
  // Written back-to-back on purpose: that is the shape every ASR and every editor
  // exports, and it is the case where a cut is one moment with two names — move
  // only one of them and the viewer gets a blank flash instead of a fixed subtitle.
  const CUES = [
    { start: 0.0, end: 1.2, text: "Assalomu alaykum, bugun yangi loyihani ko'rib chiqamiz." },
    { start: 1.2, end: 3.3, text: "Loyiha Farg'ona vodiyida ishga tushirildi." },
    { start: 3.3, end: 5.0, text: "Butun jamoa ajoyib ishladi, rahmat." }
  ];
  let seq = 0;
  let last = null;

  function tape() {
    const s = new Float32Array(Math.round(5.0 * RATE));
    let at = 0;
    for (const [dur, amp] of TAPE) {
      const n = Math.round(dur * RATE);
      for (let i = 0; i < n; i++) {
        // 3.5 Hz amplitude modulation = syllables: real speech dips 6-10 dB inside
        // a word, which is exactly what a naive loudness threshold mistakes for a
        // pause. The demo would be worthless if it only worked on clean tones.
        const syl = 0.75 + 0.25 * Math.sin(2 * Math.PI * 3.5 * (i / RATE));
        s[at + i] = amp * syl * Math.sin(2 * Math.PI * 145 * (i / RATE));
      }
      at += n;
    }
    return s;
  }
  function fmt(sec) {
    const m = Math.floor(sec / 60), s = sec - m * 60;
    return `${m}:${s.toFixed(2).padStart(5, "0")}`;
  }
  function paint(answer) {
    // Without an answer the rows show the subtitles as an ASR export wrote them:
    // cut by eye, over the middle of words.
    const moves = new Map();
    if (answer) for (const mv of answer.moves) moves.set(`${mv.cue}|${mv.edge}`, mv);
    const rows = answer
      ? answer.cues.map((c, i) => ({ ...c, was: CUES[i] }))
      : CUES.map((c, i) => ({ i: i + 1, ...c, was: c }));
    box.innerHTML = rows.map(c => {
      const ms = moves.get(`${c.i}|start`);
      const me = moves.get(`${c.i}|end`);
      const shift = (ms && ms.shift) || (me && me.shift) || 0;
      return `<li class="q-card${answer ? " fixed" : ""}">` +
        `<span class="q-time">${fmt(c.was.start)} → ${fmt(c.was.end)}` +
        (answer ? ` ⟶ <b class="j-new">${fmt(c.start)} → ${fmt(c.end)}</b>` : "") + `</span>` +
        `<span class="q-text">${esc(c.text)}</span>` +
        `<span class="q-cps">${answer ? "+" + shift.toFixed(2) + "s" : "—"}</span></li>`;
    }).join("");
    if (!answer) { metrics.innerHTML = ""; return; }
    const a = answer.audio, s = answer.summary;
    metrics.innerHTML =
      `<li><b>${esc(t("jimlik_moved"))}</b><span>${s.moved}/${s.boundaries}</span></li>` +
      `<li><b>${esc(t("jimlik_pauses"))}</b><span>${a.gaps} · ${fmt(a.duration)}</span></li>` +
      `<li><b>${esc(t("jimlik_max"))}</b><span>${s.max_applied.toFixed(2)}s</span></li>` +
      `<li><b>${esc(t("jimlik_words"))}</b><span>${s.words_preserved ? "✓" : "✗"}</span></li>` +
      `<li><b>${esc(t("jimlik_worse"))}</b><span>${s.never_worse ? "✓" : "✗"}</span></li>`;
    btn.dataset.i18nLocked = "1";
    btn.textContent = t("jimlik_again");
  }
  window.__jimlikRepaint = () => { if (last) paint(last); };
  async function run() {
    const my = ++seq;
    btn.disabled = true;
    try {
      const fd = new FormData();
      fd.append("audio", tapeBlob(), "jimlik-demo.wav");
      fd.append("lines", JSON.stringify(CUES));
      const a = await api("/api/v1/ling/align", { method: "POST", body: fd });
      if (my !== seq) return;
      last = a;
      paint(a);
    } catch (e) {
      if (my !== seq) return;
      metrics.innerHTML = `<li class="ling-err">${esc(e.message)}</li>`;
    } finally {
      if (my === seq) btn.disabled = false;
    }
  }
  let _blob = null;
  function tapeBlob() { return (_blob ||= wavMono16(tape(), RATE)); }
  btn.addEventListener("click", run);
  paint(null);
})();

// ─── Ovoz So'z demo: which word, at what moment (fifth moat layer) ───────────
// Jimlik fixed where a card starts; inside the card ten words still share one
// time. This widget answers the question a follow-along reader actually asks, and
// it answers it from the same synthesised tape — the demo owns no recording, so it
// cannot be showing a pre-baked answer.
(function sozDemo() {
  const box = $("#soz-rows");
  const btn = $("#soz-run");
  if (!box || !btn) return;
  const metrics = $("#soz-metrics");
  const read = $("#soz-read");
  const hint = $("#soz-hint");
  const playBtn = $("#soz-play");
  const RATE = 8000;
  const CUES = [
    { start: 0.0, end: 1.6, text: "Assalomu alaykum, bugun yangi loyihani" },
    { start: 1.9, end: 3.6, text: "Farg'ona vodiyida ishga tushirildi" },
    { start: 3.9, end: 5.0, text: "Jamoa ajoyib ishladi" }
  ];
  // How much air a speaker leaves: one analysis frame is 20 ms and the engine
  // needs two of them before a gap counts as a pause, so 60 ms is the shortest
  // honest number here. A smaller one would buy a demo that cannot earn the
  // measurement it is on the page to show.
  const WORD_GAP = 0.06;
  const CARD_GAP = 0.3;
  const VOICED = 0.30;
  const BREATH = 0.002;
  const METHOD = { valley: "soz_m_valley", quiet: "soz_m_quiet", speech: "soz_m_speech" };
  let seq = 0;
  let last = null;
  let raf = 0;
  let plan = [];
  let selAt = null;          // coordinates of the chosen word, not a copy of its answer

  const VOWEL = /[aeiouə]/i;
  function syllables(word) {
    // The server weighs a word by its vowel clusters plus a small per-letter
    // term; the demo writes its tape with the same law, so the air each word gets
    // here is the air the engine will go looking for.
    let n = 0, prev = false;
    for (const ch of word) { const v = VOWEL.test(ch); if (v && !prev) n++; prev = v; }
    return Math.max(1, n) + 0.04 * Math.max(0, word.length - 1);
  }

  function schedule() {
    // The tape is written from the cue list itself, word by word: every boundary
    // the engine is asked about is a place where this demo really stopped
    // talking. Nothing is pre-baked, and nothing is a steady tone pretending to
    // be speech. Cells count samples, not seconds — rounding each one to a whole
    // sample is what keeps the last word of a card ending where the card ends.
    const cells = [];
    let at = 0;
    const push = (n, amp) => { if (n > 0) cells.push([n, amp]); at += n; };
    const gap = Math.round(WORD_GAP * RATE);
    CUES.forEach((c) => {
      const toks = c.text.split(/\s+/).filter(Boolean);
      const w = toks.map(syllables);
      const weight = w.reduce((a, b) => a + b, 0);
      const from = Math.round(c.start * RATE);
      const voice = (Math.round(c.end * RATE) - from) - (toks.length - 1) * gap;
      if (from > at) push(from - at, BREATH);
      let used = 0;
      toks.forEach((tok, k) => {
        const last = k + 1 === toks.length;
        const n = Math.max(1, last ? voice - used : Math.round(voice * w[k] / weight));
        push(n, VOICED);
        used += n;
        if (!last) push(gap, BREATH);
      });
    });
    push(Math.round(CARD_GAP * RATE), BREATH);
    return cells;
  }
  function tape() {
    const cells = schedule();
    const s = new Float32Array(cells.reduce((a, c) => a + c[0], 0));
    let at = 0;
    for (const [n, amp] of cells) {
      for (let i = 0; i < n; i++) {
        // 3.5 Hz syllables inside a word: the dips a speaker makes *within* a
        // word are exactly what the engine must read as prosody, not as borders.
        const syl = 0.75 + 0.25 * Math.sin(2 * Math.PI * 3.5 * (i / RATE));
        s[at + i] = amp * syl * Math.sin(2 * Math.PI * 145 * (i / RATE));
      }
      at += n;
    }
    return s;
  }
  let _blob = null;
  function tapeBlob() { return (_blob ||= wavMono16(tape(), RATE)); }
  function fmt(sec) {
    const m = Math.floor(sec / 60), s = sec - m * 60;
    return `${m}:${s.toFixed(2).padStart(5, "0")}`;
  }
  // Duration buckets, not inline widths: the strip stays a real proportion of the
  // line while the bundle keeps zero `style=` attributes under the strict CSP.
  function bucket(dur, longest) {
    const r = longest > 0 ? dur / longest : 1;
    return r > 0.75 ? 4 : r > 0.5 ? 3 : r > 0.28 ? 2 : 1;
  }
  function rows(answer) {
    return CUES.map((c, i) => {
      const got = answer ? (answer.cues[i] || {}) : null;
      const timed = got && got.words && got.words.length ? got.words : null;
      const toks = c.text.split(/\s+/).filter(Boolean);
      const longest = timed ? Math.max(...timed.map(w => w.e - w.s)) : 0;
      const cells = toks.map((w, k) => {
        const it = timed ? timed[k] : null;
        const meth = got && got.methods ? got.methods[k] : null;
        const d = it ? bucket(it.e - it.s, longest) : 1;
        return `<li class="wz-slot d${d}"><button type="button" class="wz"` +
          ` data-c="${esc(String(i))}" data-k="${esc(String(k))}"` +
          (meth ? ` data-m="${esc(meth)}"` : "") +
          (it ? ` data-t="${esc(it.s.toFixed(2))}–${esc(it.e.toFixed(2))}"` : "") +
          `>${esc(w)}</button></li>`;
      }).join("");
      const why = got && got.reason ? ` · ${esc(got.reason)}` : "";
      return `<li class="soz-row"><span class="soz-time">${fmt(c.start)} → ${fmt(c.end)}${why}</span>` +
        `<ul class="soz-strip">${cells}</ul></li>`;
    }).join("");
  }
  function announce(item) {
    if (!item) { read.innerHTML = ""; return; }
    // The boundary on show is the one *after* this word — the same one the strip
    // paints a provenance border on. The last word of a card has no measured
    // border after it (the card end belongs to Jimlik), so it gets a dash rather
    // than a method this engine never used.
    const m = item.methods ? item.methods[item.k] : null;
    read.innerHTML =
      `<dt>${esc(item.w)}</dt><dd>${esc(fmt(item.s))} → ${esc(fmt(item.e))}</dd>` +
      `<dt>${esc(t("soz_head_s"))}</dt><dd>${esc(fmt(item.s))}</dd>` +
      `<dt>${esc(t("soz_head_e"))}</dt><dd>${esc(fmt(item.e))}</dd>` +
      `<dt>${esc(t("soz_head_m"))}</dt><dd>${esc(m ? t(METHOD[m]) : "—")}</dd>`;
  }
  function describe(at) {
    // Looked up in the last answer every time, so a repaint in another language
    // can re-read the very same word instead of going blank on the visitor.
    if (!at || !last) return null;
    const cue = (last.cues || [])[at.c];
    const it = cue && cue.words ? cue.words[at.k] : null;
    if (!it) return null;
    return { w: CUES[at.c].text.split(/\s+/).filter(Boolean)[at.k],
             s: it.s, e: it.e, k: at.k, methods: cue.methods };
  }
  function collect(answer) {
    // One flat plan across cues, in tape order: the playhead must not restart its
    // clock at every card, or the sweep drifts off the recording it came from.
    const out = [];
    if (!answer) return out;
    answer.cues.forEach((c, ci) => {
      const toks = CUES[ci].text.split(/\s+/).filter(Boolean);
      (c.words || []).forEach((w, k) => {
        if (toks[k] === w.w) out.push({ c: ci, k, s: w.s, e: w.e, methods: c.methods });
      });
    });
    return out;
  }
  function cellAt(c, k) {
    // Compared as numbers on the dataset, not spliced into a selector string: a
    // built selector would put raw text into an attribute-looking literal, and the
    // CSP/escaping gates read attribute literals in this bundle as a smell.
    return [...box.querySelectorAll(".wz")]
      .find(el => +el.dataset.c === c && +el.dataset.k === k);
  }
  function halt() {
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
    plan = [];
    if (playBtn) {
      playBtn.classList.add("hidden");
      playBtn.textContent = t("soz_play");
    }
  }
  function paint(answer) {
    halt();
    const keep = selAt;
    selAt = null;
    box.innerHTML = rows(answer);
    read.innerHTML = "";
    box.querySelectorAll(".wz").forEach(el => {
      el.addEventListener("click", () => {
        box.querySelectorAll(".wz.on").forEach(p => p.classList.remove("on"));
        el.classList.add("on");
        const c = +el.dataset.c, k = +el.dataset.k;
        selAt = { c, k };
        announce(describe(selAt));
      });
    });
    if (!answer) {
      metrics.innerHTML = "";
      if (hint) hint.classList.remove("hidden");
      return;
    }
    const s = answer.summary;
    metrics.innerHTML =
      `<li><b>${esc(t("soz_words"))}</b><span>${s.words}</span></li>` +
      `<li><b>${esc(t("soz_cues"))}</b><span>${s.cues_measured}/${s.cues}</span></li>` +
      `<li><b>${esc(t("soz_share"))}</b><span>${Math.round(s.valley_share * 100)}%</span></li>` +
      `<li><b>${esc(t("soz_longest"))}</b><span>${s.longest_word_sec.toFixed(2)}s</span></li>`;
    if (hint) hint.classList.add("hidden");
    plan = collect(answer);
    if (playBtn && plan.length) playBtn.classList.remove("hidden");
    btn.dataset.i18nLocked = "1";
    btn.textContent = t("soz_again");
    // A language switch repaints every label on the strip; it must not also cost
    // the visitor the word they had just asked about.
    if (keep) {
      const item = describe(keep);
      const el = item ? cellAt(keep.c, keep.k) : null;
      if (el) { selAt = keep; el.classList.add("on"); announce(item); }
    }
  }
  window.__sozRepaint = () => { if (last) paint(last); else if (btn.dataset.i18nLocked) btn.textContent = t("soz_run"); };
  function sweep() {
    // The sweep is a demonstration of the timings, not a player: no audio is
    // claimed, the head simply walks the seconds the engine measured.
    const t0 = performance.now();
    const total = plan.length ? plan[plan.length - 1].e : 0;
    let cur = null;
    playBtn.textContent = t("soz_stop");
    const stepNow = () => {
      const now = (performance.now() - t0) / 1000;
      const hit = plan.find(p => now >= p.s && now < p.e);
      if (hit !== cur) {
        box.querySelectorAll(".wz.now").forEach(p => p.classList.remove("now"));
        if (hit) {
          const el = cellAt(hit.c, hit.k);
          if (el) el.classList.add("now");
        }
        cur = hit;
      }
      if (now >= total) { halt(); return; }
      raf = requestAnimationFrame(stepNow);
    };
    raf = requestAnimationFrame(stepNow);
  }
  if (playBtn) {
    playBtn.addEventListener("click", () => {
      if (raf) { halt(); return; }
      if (plan.length) sweep();
    });
  }
  async function run() {
    const my = ++seq;
    btn.disabled = true;
    try {
      const fd = new FormData();
      fd.append("audio", tapeBlob(), "soz-demo.wav");
      fd.append("lines", JSON.stringify(CUES));
      fd.append("fmt", "vtt");
      const a = await api("/api/v1/ling/words", { method: "POST", body: fd });
      if (my !== seq) return;
      last = a;
      paint(a);
    } catch (e) {
      if (my !== seq) return;
      metrics.innerHTML = `<li class="ling-err">${esc(e.message)}</li>`;
    } finally {
      if (my === seq) btn.disabled = false;
    }
  }
  btn.addEventListener("click", run);
  paint(null);
})();

// ─── Ovoz Nafis: право реза — the cut ruler ─────────────────────────
// The ruler is the whole argument in one paragraph: every place a subtitle *could*
// be cut, marked by whether Uzbek allows it. It is deliberately not a grade. The
// engine answers per position, only codes cross the wire, and the words beside each
// mark are what convince a studio that a language model is not needed to know that
// `tayyorlab berdi` is one predicate.
(function () {
  const btn = $("#nafis-run"), ruler = $("#nf-ruler"), listBox = $("#nf-list"),
        hint = $("#nf-hint"), metrics = $("#nafis-metrics");
  if (!btn || !ruler) return;
  const DEMO = "Bu loyiha Farg'ona vodiyida ikki oy ichida ishga tushirildi, uni esa " +
    "yigirma kishidan iborat jamoa o'z vaqtida va to'liq tayyorlab berdi. Agar siz ham " +
    "o'zbek tilida subtitr tayyorlayotgan bo'lsangiz, unda avval matnni tekshirib, keyin " +
    "esa tezlikni o'lchab ko'rishingiz shart, chunki boshqa til qoidalari bu yerda " +
    "ishlamaydi. Kelasi yilda biz ikkinchi vodiy loyihasini ham boshlaymiz, uni esa " +
    "xalqaro hamkorlar moliyalashtiradi, shuning uchun rejani hozirdanoq tayyorlab qo'ydik.";
  let last = null, seq = 0;
  const toks = DEMO.split(/\s+/).filter(Boolean);
  // Inline the concatenation: a key built in a variable is invisible to the i18n
  // scans, and an untranslated law name would reach the customer as `pair`.
  const lawLabel = (code) => { const v = t("nf_law_" + code); return v === "nf_law_" + code ? code : v; };

  function paintRuler(answer) {
    const bad = Object.create(null);
    if (answer) (answer.ruler || []).forEach(r => { bad[r.k] = r.codes || []; });
    ruler.innerHTML = toks.map((w, i) => {
      const codes = i ? bad[i] : null;
      const mark = !i ? "" : (codes
        ? `<i class="nf-cut nf-bad" data-code="${esc(codes.join(","))}"` +
          ` title="${esc(codes.map(lawLabel).join(" · "))}" aria-hidden="true"></i>`
        : `<i class="nf-cut" aria-hidden="true"></i>`);
      return `${mark}<span class="nf-w${codes ? " nf-after-bad" : ""}">${esc(w)}</span>`;
    }).join("");
    ruler.setAttribute("aria-busy", answer ? "false" : "true");
  }

  function paintList(answer) {
    if (!answer) { listBox.innerHTML = ""; return; }
    if (!answer.ruler || !answer.ruler.length) {
      listBox.innerHTML = `<span class="nf-item nf-clean">${esc(t("nf_clean"))}</span>`;
      return;
    }
    const items = answer.ruler.slice(0, 6).map(r =>
      `<span class="nf-item"><b>${esc(r.left)} · ${esc(r.right)}</b> — ` +
      `<em>${esc((r.codes || []).map(lawLabel).join(", "))}</em></span>`).join("");
    const more = answer.forbidden > 6
      ? `<span class="nf-item nf-more">${esc(t("nf_more").replaceAll("{n}", answer.forbidden - 6))}</span>` : "";
    const capped = answer.ruler_truncated
      ? `<span class="nf-item nf-more">${esc(t("nf_capped"))}</span>` : "";
    listBox.innerHTML = items + more + capped;
  }

  function paint(answer) {
    paintRuler(answer);
    paintList(answer);
    if (!answer) { metrics.innerHTML = ""; if (hint) hint.classList.remove("hidden"); return; }
    const rows = [
      [t("nafis_legal"), Math.round((answer.legal_share ?? 0) * 100) + "%"],
      [t("nafis_forbidden"), String(answer.forbidden)],
      [t("nafis_positions"), String(answer.cut_positions)],
    ];
    const b = answer.boundaries || {};
    if (b.measured) rows.push([t("nafis_boundaries"), `${b.measured - b.hard}/${b.measured}`]);
    metrics.innerHTML = rows.map(([k, v]) =>
      `<li><b>${esc(k)}</b><span>${esc(v)}</span></li>`).join("");
    if (hint) hint.classList.add("hidden");
    btn.dataset.i18nLocked = "1";
    btn.textContent = t("nafis_again");
  }
  // A locale switch re-labels every mark from the same answer: no second request,
  // and the visitor keeps the paragraph they were just reading.
  window.__nafisRepaint = () => {
    if (last) paint(last);
    else if (btn.dataset.i18nLocked) btn.textContent = t("nafis_run");
  };
  async function run() {
    const my = ++seq;
    btn.disabled = true;
    try {
      const a = await api("/api/v1/ling/nafis",
        { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: DEMO }) });
      if (my !== seq) return;
      last = a;
      paint(a);
    } catch (e) {
      if (my !== seq) return;
      metrics.innerHTML = `<li class="ling-err">${esc(e.message)}</li>`;
    } finally {
      if (my === seq) btn.disabled = false;
    }
  }
  btn.addEventListener("click", run);
  paint(null);
})();

// ─── auth ───
async function register(contact, name, secret) {
  const data = await api("/api/auth/register", { method: "POST", body: form({ name, contact, secret }) });
  adopt(data.token);
}
$("#btn-register").addEventListener("click", () => {
  const contact = $("#f-contact").value.trim();
  if (!contact) return toast(t("need_contact"), true);
  const secret = $("#f-secret").value.trim();
  if (secret.length < 10) return toast(t("secret_short"), true);
  register(contact, $("#f-name").value.trim() || "User", secret)
    .then(() => { toast(t("hello") + "! +10 " + t("minutes")); refreshAll(); })
    .catch(e => toast(e.message, true));
});
$("#btn-login").addEventListener("click", () => {
  const contact = $("#f-contact").value.trim();
  if (!contact) return toast(t("need_contact"), true);
  api("/api/auth/login", { method: "POST", body: form({ contact, secret: $("#f-secret").value.trim() }) })
    .then(d => { adopt(d.token); refreshAll(); })
    .catch(e => toast(e.message, true));
});
function adopt(tok) {
  token = tok; localStorage.setItem("ovoz_token", tok);
  _sessionGen++;             // anything still in flight belongs to the old viewer
  _wsReconnectAttempt = 0;   // a new session starts its backoff from scratch
  _allFetch = null;          // a pass started under the old session is not ours
  // Same reason: a GET already in flight belongs to the previous account, and a
  // queued trailing refresh would paint it over the new one.
  _jobsFetch = null; _jobsQueued = null;
  $("#auth-card").classList.add("hidden");
  $("#job-form").classList.remove("hidden");
  $("#logout").classList.remove("hidden");
}
function logout() {
  // server-side revoke best-effort — даже если сети нет, локальную сессию рвём
  if (token) { fetch("/api/auth/logout", { method: "POST", headers: { Authorization: "Bearer " + token } }).catch(() => {}); }
  stopPolling();
  token = null; localStorage.removeItem("ovoz_token");
  _sessionGen++;             // retire responses requested under this account
  // Every cached payload belongs to the account that just left. A locale switch
  // repaints straight from these caches, so leaving them populated would show
  // the previous visitor's jobs, balance and notifications on a shared device.
  _me = null; _gloss = []; _notifs = []; _unread = 0;
  _allJobs = []; _nextCursor = null; prevStatuses = {};
  for (const k of Object.keys(_typeCache)) delete _typeCache[k];
  for (const k of Object.keys(_progressCache)) delete _progressCache[k];
  $("#notif-panel")?.classList.add("hidden"); _notifOpen = false;
  clearNotifPanel();
  const nb = $("#notif-badge");
  if (nb) { nb.classList.add("hidden"); nb.textContent = ""; } // no stale count hiding
  // The balance line is JS-owned text: hidden now, but a locale switch repaints
  // whatever is in the DOM, and a hidden element is one CSS bug away from visible.
  const bal = $("#balance");
  if (bal) bal.textContent = "";
  $("#auth-card").classList.remove("hidden");
  $("#job-form").classList.add("hidden");
  $("#logout").classList.add("hidden");
  $("#balance").hidden = true;
  const je = $("#jobs-empty"); if (je) je.classList.remove("hidden");
  // The filter is part of the session's view: the next visitor must not inherit
  // a hidden "failed only" selection and wonder why the list looks broken.
  jobFilter = "";
  $$(".filter-pill").forEach(p => p.classList.toggle("on", !p.dataset.filter));
  // Repaint through the only painter that knows how the two empty states and the
  // escape hatch are laid out. Emptying #jobs-list by hand left "No job matches
  // this filter" and a clickable "Show all" on a signed-out screen (browser QA
  // QA2-D1): renderJobs is the single source of truth, so it cannot drift.
  renderJobs([]);
  const gl = $("#glossary-list"); if (gl) gl.innerHTML = "";
  const ub = $("#usage-body"); if (ub) ub.innerHTML = "";
}
$("#logout").addEventListener("click", logout);
function form(obj) { const fd = new FormData(); Object.entries(obj).forEach(([k, v]) => fd.append(k, v)); return fd; }

// ─── Telegram Mini App: автологин + полная тема ───
// The boot call itself lives at the bottom of this file, next to the other
// bootstrap statements: see telegramBoot(). Everything it touches synchronously
// (job state, caches, painters) must already be initialized by then.
function _tgLum(hex) {
  // относительная яркость #rgb/#rrggbb; всё, что нужно — светлая тема или тёмная
  let h = String(hex || "").replace("#", "");
  if (h.length === 3) h = h.split("").map(c => c + c).join("");
  if (h.length !== 6) return 0;
  const n = parseInt(h, 16);
  if (Number.isNaN(n)) return 0;
  return (0.2126 * ((n >> 16) & 255) + 0.7152 * ((n >> 8) & 255) + 0.0722 * (n & 255)) / 255;
}
function _tgMix(hex, towardWhite, amt) {
  // шаг поверхности: на светлой теме — затемняем, на тёмной — подсвечиваем
  let h = String(hex || "").replace("#", "");
  if (h.length === 3) h = h.split("").map(c => c + c).join("");
  if (h.length !== 6) return hex;
  const n = parseInt(h, 16);
  const tgt = towardWhite ? 255 : 0;
  const ch = (sh) => Math.round((((n >> sh) & 255) * (1 - amt)) + (tgt * amt));
  return "#" + [16, 8, 0].map(sh => ch(sh).toString(16).padStart(2, "0")).join("");
}
function _tgContrast(a, b) {
  const la = _tgLum(a), lb = _tgLum(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}
function applyTgTheme(tg) {
  const tp = tg.themeParams || {};
  const root = document.documentElement.style;
  const set = (k, v) => { if (v) root.setProperty(k, v); };
  const light = _tgLum(tp.bg_color || '#0b0d12') > 0.5;
  const bg = tp.bg_color || (light ? '#ffffff' : '#0b0d12');
  // hint_color — цвет подсказок, но приложение красит им и читаемый текст
  // (ссылки навигации, пилюли фильтра, .dim-абзацы). Telegram шлёт #8e8e93 —
  // на белом фоне это 3.26:1, ниже порога 4.5:1 (замер QA 29.09: 13 элементов).
  // Нечитаемую подсказку сдвигаем к цвету текста шаг за шагом, пока контраст
  // не станет законным; в тёмной теме #a6adc0 проходила сразу — не трогаем.
  let hint = tp.hint_color || '';
  if (hint && tp.text_color && _tgLum(hint) !== _tgLum(tp.text_color)) {
    for (let i = 0; i < 24 && _tgContrast(hint, bg) < 4.5; i++) {
      hint = _tgMix(hint, _tgLum(tp.text_color) > _tgLum(hint), 0.08);
    }
  }
  set('--bg', tp.bg_color);
  set('--ink', tp.text_color);
  if (hint) set('--ink-2', hint);
  set('--mint', tp.button_color);
  set('--mint-ink', tp.button_text_color);
  set('--nav-bg', tp.header_bg_color);
  root.colorScheme = light ? 'light' : 'dark';   // нативные select/скроллбары
  // --surface-2 — самая частая панельная переменная (сегменты, селекты, тосты,
  // чипы). Раньше не маппилась: в светлой теме Telegram оставалась тёмной
  // #171b25, и тёмный текст читался по тёмному (скриншот пользователя 29.09).
  const surf = tp.secondary_bg_color || (light ? '#f1f2f5' : '#12151d');
  set('--surface', tp.secondary_bg_color);
  root.setProperty('--surface-2', _tgMix(surf, !light, 0.055));
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', tp.header_bg_color || tp.bg_color || '#0b0d12');
}
function tgTryLogin() {
  const tg = tgWebApp();
  if (!tg) return;
  // ready() first, on every path. It used to sit below the token early-return, so
  // a returning signed-in visitor never told the host the page was up — Telegram
  // keeps the loading overlay and the header spinner until `ready()` arrives.
  try { tg.ready(); } catch (e) {}
  try { tg.expand(); } catch (e) {}
  applyTgTheme(tg);
  try {
    tg.onEvent?.("themeChanged", () => applyTgTheme(tg));
    const bb = tgInsideApp() && tg.BackButton;
    if (bb) { bb.onClick ? bb.onClick(tgGoBack) : bb.addEventInterceptor?.("click", tgGoBack); }
  } catch (e) { /* an older SDK simply never shows the button */ }
  tgSyncBack();
  // если уже есть валидный токен — не перевыпускаем (initData single-use guard)
  if (token) { showStudio(); return; }
  if (!tg.initData || tg.initData === "") return;
  api("/api/auth/telegram", { method: "POST", body: form({ init_data: tg.initData }) })
    .then(d => { adopt(d.token); refreshAll(); showStudio(); })
    .catch(() => toast(t("tg_auth_failed"), true));
}

// ─── wallet + balance ───
let _me = null; // cached /api/me so a locale change repaints instead of refetching
async function refreshMe() {
  if (!token) return;
  let data;
  try {
    data = await api("/api/me");
  } catch (e) {
    // Only the server is allowed to declare the session dead. A laptop that
    // slept, a dropped Wi-Fi or a restarting container must not sign nobody out.
    if (e.code === 401) logout();
    return;
  }
  _me = data;
  try { paintMe(); } catch (e) { console.error("paintMe", e); }
}
function paintMe() {
  if (!_me || !token) return;
  $("#balance").hidden = false;
  // balance_minutes is a float summed from per-job charges: 4.430000000000001 is
  // what the arithmetic says, not what a customer should read next to their money.
  $("#balance").textContent = `${t("balance")}: ${fmtMinutes(_me.balance_minutes)} ${t("minutes")}`;
}

// ─── type segmented + swap + dropzone + text-mode ───
$("#type-seg").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-type]");
  if (!btn) return;
  jobType = btn.dataset.type;
  $$("#type-seg button").forEach(b => b.classList.toggle("on", b === btn));
  const docMode = jobType === "document";
  $("#j-text").classList.toggle("hidden", !docMode);
  $("#dropzone").classList.toggle("hidden", false); // always visible: doc mode allows file OR paste
  // Ovoz Turn needs cues with timings; a pasted document has none, so the option
  // is hidden rather than silently ignored (a checked box that does nothing lies).
  $("#diar-row").classList.toggle("hidden", docMode);
  if (docMode) $("#j-diar").checked = false;
  // Ovoz Qator lays out subtitles; a pasted document has no timings to lay out.
  $("#polish-row").classList.toggle("hidden", docMode);
  if (docMode) $("#j-polish").checked = false;
  // Ovoz Jimlik listens to the recording; a pasted document has no tape to hear.
  $("#align-row").classList.toggle("hidden", docMode);
  if (docMode) $("#j-align").checked = false;
  // Ovoz So'z times words inside subtitle cards; a pasted document has no cards.
  $("#words-row").classList.toggle("hidden", docMode);
  if (docMode) $("#j-words").checked = false;
});
$("#swap").addEventListener("click", () => {
  const a = $("#j-src"); const b = $("#j-tgt");
  [a.value, b.value] = [b.value, a.value];
});
const dz = $("#dropzone");
dz.addEventListener("click", () => $("#j-file").click());
dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") $("#j-file").click(); });
dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
dz.addEventListener("dragleave", () => dz.classList.remove("over"));
dz.addEventListener("drop", (e) => {
  e.preventDefault(); dz.classList.remove("over");
  if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
});
$("#j-file").addEventListener("change", () => { if ($("#j-file").files.length) setFile($("#j-file").files[0]); });
function setFile(f) {
  chosenFile = f;
  dz.classList.add("filled");
  const size = f.size < 1024 ? `${f.size} B`
    : f.size < 1024 * 1024 ? `${(f.size / 1024).toFixed(1)} KB`
    : `${(f.size / 1024 / 1024).toFixed(2)} MB`;
  // This label now carries live state, not a translation: tell applyI18n() so a
  // locale switch cannot overwrite the filename while the file is still chosen.
  $("#drop-label").dataset.i18nLocked = "1";
  $("#drop-label").textContent = `✓ ${f.name} · ${size}`;
}
function clearFileLabel() {
  chosenFile = null; dz.classList.remove("filled");
  delete $("#drop-label").dataset.i18nLocked;
  $("#drop-label").textContent = t("drop_hint");
}

// ─── job submit (with upload progress) ───
$("#job-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const text = $("#j-text").value.trim();
  let file = chosenFile;
  if (!file && text) file = new File([text], "document.txt", { type: "text/plain" });
  if (!file) return toast(t("no_file"), true);
  const fd = new FormData();
  fd.append("file", file);
  fd.append("jtype", jobType);
  fd.append("src", $("#j-src").value);
  fd.append("tgt", $("#j-tgt").value);
  if ($("#j-diar").checked) fd.append("diarize", "1"); // absent = off server-side
  if ($("#j-polish").checked) fd.append("polish", "1");
  if ($("#j-align").checked) fd.append("align", "1");
  if ($("#j-words").checked) fd.append("words", "1");
  $("#btn-run").disabled = true;
  // optimistic: add skeleton row — but only where a new row may actually appear.
  // Prepending it into a list the user filtered to empty flashed a phantom row
  // and contradicted the empty state that was on screen 30 ms earlier.
  const skel = document.createElement("div");
  skel.className = "skel";
  if (!jobFilter) {
    $("#jobs-list").prepend(skel);
    $("#jobs-empty").classList.add("hidden");
  }
  try {
    const created = await uploadJob(fd);
    clearFileLabel();
    $("#j-file").value = ""; $("#j-text").value = "";
    skel.remove();
    // The POST response already carries the row, so draw it from that instead of
    // fetching the list again: this is the moment the live channel needs a
    // `data-jid` to patch. Without it every queued/start/asr frame found no row,
    // refetched the list, and the user still saw only the final state.
    adoptJob(created.job || created);
    startPolling(); refreshMe();
  } catch (e) {
    toast(e.code === 402 ? t("credits_short") : e.message, true);
    skel.remove();
    refreshJobs(); // restore actual state
  } finally { $("#btn-run").disabled = false; }
});

function uploadJob(fd) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs");
    if (token) xhr.setRequestHeader("Authorization", "Bearer " + token);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        const pct = Math.round(e.loaded / e.total * 100);
        $("#btn-run").dataset.i18nLocked = "1"; // progress text outranks the label
        $("#btn-run").textContent = `${t("run")} ${pct}%`;
      }
    };
    xhr.onload = () => {
      delete $("#btn-run").dataset.i18nLocked;
      $("#btn-run").textContent = t("run");
      try {
        const body = JSON.parse(xhr.responseText);
        if (xhr.status >= 200 && xhr.status < 300) resolve(body);
        else {
          if (xhr.status === 401) { logout(); reject(new Error(t("err_session_expired"))); return; }
          // uploadJob bypasses api(), so it needs the same localized fallback:
          // a 413/502 from a proxy must not toast an empty or English line.
          const err = new Error(localizeServerError(body.detail) || httpFallback({ status: xhr.status }));
          err.code = xhr.status;
          reject(err);
        }
      } catch { reject(httpError(xhr.status)); }
    };
    xhr.onerror = () => {
      delete $("#btn-run").dataset.i18nLocked;
      $("#btn-run").textContent = t("run");
      reject(new Error(t("err_network")));
    };
    xhr.send(fd);
  });
}

// ─── jobs list ───
let pollTimer = null;
let pollFailures = 0;
let prevStatuses = {};  // track for completion announcement
let _ws = null;         // WebSocket connection
let _wsReconnectTimer = null;
let jobFilter = "";     // active status filter for jobs list
let _nextCursor = null; // pagination cursor for load-more
let _allJobs = [];      // accumulated job list

// ─── what the paid engine steps actually did ─────────────────────────
// A green pill used to be the whole answer: whether Ovoz Jimlik moved a single
// boundary, refused because there was no tape, or heard only the first five minutes
// of a twenty-minute job lived exclusively in the API. The step's `data` exists so
// this can be said in the customer's language — `message` is an English log line,
// and painting it would ship developer prose to a UZ phone (the same rule the
// translated errors follow). Only the raw rows are cached; the sentences are
// rebuilt on every render, so a locale switch repaints them with zero requests.
const _stepsCache = {};   // jobId -> timeline rows that carry data
const _stepsOpen = new Set();

// The same binary-noise rule as billed minutes applies to engine numbers:
// 0.30000000000000004 must not sit in a metric next to someone's tape.
const fmtNum = fmtMinutes;

// t() with {tokens}: a translation owns the word order, the code only owns numbers.
function tf(key, vars) {
  let s = t(key);
  for (const k in vars) s = s.replaceAll("{" + k + "}", String(vars[k]));
  return s;
}

function stepLines(ev) {
  const d = ev && ev.data;
  if (!d || !d.code) return null;                // a row this build cannot explain
  if (d.code === "skipped") {
    // The composition is inline so the i18n gates can see the prefix it builds.
    const reason = String(d.reason || "").replace(/[^a-z_]/g, "");
    const own = t("st_skip_" + reason);
    return [own === "st_skip_" + reason ? tf("st_skip_generic", { a: d.reason || "?" }) : own];
  }
  const trunc = d.truncation
    ? [tf("st_trunc", { a: d.truncation.heard_sec, b: d.truncation.paid_sec,
                       c: d.truncation.window_sec })]
    : [];
  if (d.code === "aligned") {
    return [tf("st_align_ok", { a: d.moved, b: d.boundaries,
                               c: fmtNum(d.max_applied), d: d.gaps })].concat(trunc);
  }
  if (d.code === "timed") {
    return [tf("st_words_ok", { a: d.words, b: d.cues_measured, c: d.cues,
                               d: Math.round(d.valley_share * 100) })].concat(trunc);
  }
  // The transcript is the one step a customer cannot verify by eye, so its number
  // belongs on the card — and when the text was invented by a demo provider, the
  // report says that in words instead of leaving the count to look like work.
  if (d.code === "transcribed") {
    return [tf("st_asr_ok", { a: d.segments })];
  }
  if (d.code === "demo_transcript") {
    return [tf("st_asr_demo", { a: d.segments })];
  }
  // Speaker counts are measurements too, and a measurement without its scope is a
  // claim: how much of the tape was heard, and how many cues were not on it.
  if (d.code === "turns") {
    return [tf("st_turns", { a: d.speakers, b: d.turns,
                            c: fmtNum(d.heard_sec), d: d.off_tape })].concat(trunc);
  }
  if (d.code === "turns_text") {
    return [tf("st_turns_text", { a: d.speakers, b: d.turns })];
  }
  return null;
}

function renderSteps(jobId) {
  const rows = _stepsCache[jobId] || [];
  const items = rows.map(ev => {
    const lines = stepLines(ev);
    if (!lines) return "";
    return `<li><span class="js-step">${esc(stepLabel(ev.step))}</span>` +
      lines.map(l => `<span class="js-line">${esc(l)}</span>`).join("") + `</li>`;
  }).join("");
  if (!items) return `<ul class="job-steps"><li><span class="js-line">${esc(t("steps_none"))}</span></li></ul>`;
  return `<ul class="job-steps" role="list">${items}</ul>`;
}

let _wsReconnectAttempt = 0; // exponential backoff counter
let _wsOpening = false;      // a ticket round-trip is already dialing for us
const WS_MAX_RECONNECTS = 10;
// jobId -> {step, message, pct}: the last phase we saw on the live channel.
const _progressCache = {};
// jobId -> type. A job that the active status filter hides is absent from
// _allJobs, so the completion announcement could not name it ("— Job completed!"
// with a dangling dash). Types are stable, so we remember them on sight.
const _typeCache = {};
// The cache exists so a live mid-flight frame survives a re-render, not so a
// settled job keeps a bar frozen at 40% forever. Server status outranks the last
// frame we happened to see, so a terminal job drops its cached progress.
function pruneProgress(jobs) {
  for (const j of jobs || []) {
    if (!isActiveJob(j) && _progressCache[j.id]) delete _progressCache[j.id];
  }
}

const isActiveJob = (j) => ["queued", "running"].includes(j.status);

function adoptJob(job) {
  // Insert the freshly created job into the rendered list (see submit handler).
  if (!job || !job.id) return;
  _typeCache[job.id] = job.type;
  // A row must not escape the active filter: with "done" selected, an adopted
  // "queued" job stayed on screen for the whole life of the job because the
  // keepPages merge only replaces rows, it never removes them.
  if (jobFilter && job.status !== jobFilter) {
    // Still remember what it was, or the completion toast and the aria-live
    // announcement can never fire (both compare against the previous status),
    // and repaint so the panel does not stay blank after the skeleton left.
    prevStatuses[job.id] = job.status;
    renderJobs(_allJobs);
    renderLoadMore();
    return;
  }
  _allJobs = [job, ..._allJobs.filter(j => j.id !== job.id)];
  prevStatuses[job.id] = job.status;
  renderJobs(_allJobs);
  renderLoadMore();
}

function startPolling() {
  if (!token) return; // anonymous visitors must not dial out with a null session
  // WebSocket first; HTTP polling is the degradation path, never both at once.
  if (_connectWS()) return;
  ensurePolling();
}

function ensurePolling() {
  if (!token || pollTimer) return;
  pollFailures = 0;
  pollTimer = setInterval(async () => {
    try {
      let url = "/api/jobs?limit=20";
      if (jobFilter) url += "&status=" + encodeURIComponent(jobFilter);
      const { jobs } = await api(url);
      pollFailures = 0;
      _allJobs = jobs;
      pruneProgress(jobs);
      renderJobs(jobs);
      refreshMe();
      for (const j of jobs) {
        const prev = prevStatuses[j.id];
        if (prev && ["queued", "running"].includes(prev) && j.status === "done") {
          announceTerminal(j.id, true, j.type);
        }
        prevStatuses[j.id] = j.status;
      }
      if (!jobs.some(j => ["queued", "running"].includes(j.status))) {
        clearInterval(pollTimer); pollTimer = null;
      }
    } catch (e) {
      pollFailures++;
      if (pollFailures >= 3 && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }
  }, 2500);
}

function _connectWS() {
  if (!token) return false;
  // Re-entrancy is the bug this guard exists for: every open socket receives
  // every event, so dialing a second one doubles the handlers, which dial two
  // more. CONNECTING *and* OPEN both count as "already on the way" — closing a
  // healthy socket because a second caller showed up cost a ticket and left a
  // few hundred milliseconds with neither a channel nor a poller.
  if (_ws && _ws.readyState !== WebSocket.CLOSED && _ws.readyState !== WebSocket.CLOSING) return true;
  if (_wsOpening) return true;
  if (_ws) { try { _ws.close(); } catch {} _ws = null; }
  if (_wsReconnectTimer) { clearTimeout(_wsReconnectTimer); _wsReconnectTimer = null; }
  // The session bearer must not ride in the query string (it is copied into
  // every access log), so the socket is opened with a 15-second one-time ticket.
  // That round-trip is part of the handshake and needs the same watchdog: a
  // proxy that accepts and never answers would otherwise pin _wsOpening forever,
  // and every later caller would return early — no socket, no polling, blind.
  _wsOpening = true;
  const opening = setTimeout(() => {
    if (!_wsOpening) return;
    _wsOpening = false;
    if (token) ensurePolling();
  }, 4000);
  api("/api/auth/ws-ticket", { method: "POST" })
    .then((d) => {
      clearTimeout(opening);
      _wsOpening = false;
      // A WS that cannot even be constructed must degrade, not freeze the UI.
      if (token && !_openWS(d.ticket)) ensurePolling();
    })
    .catch(() => {
      clearTimeout(opening);
      _wsOpening = false;
      if (token) ensurePolling(); // ticket endpoint unreachable: degrade, don't freeze
    });
  return true; // a live channel is being established; polling would duplicate it
}

function _openWS(ticket) {
  try {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws/jobs?ticket=${encodeURIComponent(ticket)}`;
    const ws = new WebSocket(url);
    _ws = ws; // handlers must mutate the socket they belong to, never a newer one
    const mine = () => _ws === ws;
    // A WebSocket that never opens (proxy, offline captive portal) must not
    // leave the studio blind: 4s handshake watchdog, then degrade to polling.
    const watchdog = setTimeout(() => {
      // Close *this* socket even if it was already superseded: an abandoned
      // handshake is ours to reap, not ours to leak.
      if (ws.readyState !== WebSocket.OPEN) { try { ws.close(); } catch {} }
    }, 4000);
    ws.onopen = () => {
      clearTimeout(watchdog);
      if (!mine()) return;
      _wsReconnectAttempt = 0;
      if (_wsReconnectTimer) { clearTimeout(_wsReconnectTimer); _wsReconnectTimer = null; }
      // WS connected: stop polling if active
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    };
    // Живость: сервер шлёт ping каждые 30 с. Любой кадр — ping или событие —
    // доказывает путь жив; 90 с без единого кадра доказывают обратное, даже
    // когда readyState упрямо говорит OPEN (half-open после смены сети,
    // idle-close прокси, фона iOS: onclose не приходит вообще). Тогда progress
    // замерает навсегда; этот смотритель закрывает сам — закрытие возвращает
    // machinery редизола и polling в игру.
    let lastRx = Date.now();
    const liveness = setInterval(() => {
      if (!mine()) { clearInterval(liveness); return; }
      if (ws.readyState === WebSocket.OPEN && Date.now() - lastRx > 90000) {
        try { ws.close(); } catch {}
      }
    }, 15000);
    ws.onmessage = (ev) => {
      if (!mine()) return; // an orphaned socket must not drive the UI twice
      lastRx = Date.now(); // any frame is a heartbeat, ping counts too
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "connected" || msg.type === "ping") return;
        if (msg.type === "job_event") _handleWSEvent(msg);
      } catch {}
    };
    ws.onclose = () => {
      clearTimeout(watchdog);
      clearInterval(liveness);
      if (!mine()) return; // a late close from a superseded socket
      _ws = null;
      if (!token) return;  // signed out: nothing to watch, nothing to redial
      ensurePolling(); // keep the UI live while we wait to retry
      // Exponential backoff reconnect: 2s, 4s, 8s, max 30s. Условия — только
      // «подписан и потолок попыток»: локальный список задач не годится, при
      // фильтре или ещё не загруженном списке клиент замолкал навсегда (аудит
      // рои, F4). Cost stays bounded by WS_MAX_RECONNECTS, polling covers the
      // meanwhile, and a fresh user action re-dials via startPolling anyway.
      if (_wsReconnectAttempt < WS_MAX_RECONNECTS && !_wsReconnectTimer) {
        const delay = Math.min(2000 * Math.pow(2, _wsReconnectAttempt), 30000);
        _wsReconnectAttempt++;
        _wsReconnectTimer = setTimeout(() => {
          _wsReconnectTimer = null;
          if (token) startPolling(); // re-attempt WS or fallback polling
        }, delay);
      }
    };
    ws.onerror = () => {
      // 'close' always follows 'error'; polling starts there, once.
      try { ws.close(); } catch {}
    };
    return true;
  } catch {
    _ws = null;
    return false; // WS not available
  }
}

function _handleWSEvent(msg) {
  const { job_id, step, message } = msg;
  const pct = typeof msg.pct === "number" ? msg.pct : null;
  const prev = prevStatuses[job_id];
  // Determine new status from step
  let newStatus = null;
  if (["done", "failed", "canceled"].includes(step)) newStatus = step;
  else if (step === "start" || step === "asr" || step === "translate" || step === "tts") newStatus = "running";
  else if (step === "queued" || step === "requeued") newStatus = "queued";
  if (newStatus) prevStatuses[job_id] = newStatus;
  // The visible phase text is composed from `step` on the client (ps_* keys), so
  // the row can be patched even when the status itself did not change.
  const patched = patchJobRow(job_id, newStatus, step, message, pct);
  const terminal = newStatus === "done" || newStatus === "failed";
  // Only announce the first time we see this job reach a terminal state: a
  // replayed or duplicated event must not double-count the unread badge.
  const alreadySettled = ["done", "failed", "canceled"].includes(prev);
  if (terminal && !alreadySettled) {
    const done = newStatus === "done";
    announceTerminal(job_id, done);
    refreshMe();     // balance + quota changed
    _unread++;       // the badge is right even if the fetch below wins the race
    paintNotif();
    refreshNotif();
  }
  // The live channel woke us up: never re-enter startPolling() from here.
  // Progress goes through patchJobRow(); only artifacts/pagination need a GET.
  // "orphan" is the server saying "this job moved on without me": we cannot know
  // the truth locally, so ask for it instead of freezing the row on a stale pill.
  if (terminal || !patched || step === "orphan") refreshJobs({ keepPages: true, fromLive: true });
}

function _jobTypeOf(jobId) {
  const j = _allJobs.find(x => x.id === jobId);
  return (j && j.type) || _typeCache[jobId] || "";
}

// One sentence, both live regions. Building it twice let the visible toast say
// "Job completed!" while aria-live said "Document — Job completed!", so with two
// jobs running a sighted user could not tell which one finished (browser QA
// QA2-D2). An unknown type must not leave a dangling "—" either.
function announceTerminal(jobId, done, typeHint) {
  const label = typeLabel(typeHint || _jobTypeOf(jobId) || "");
  const sentence = done ? t("job_done_toast") : t("notif_job_failed");
  const text = label ? label + " — " + sentence : sentence;
  toast(text, !done);
  const live = document.getElementById("aria-announce");
  if (live) live.textContent = text;
}

// Update the row that is already on screen instead of rebuilding the whole list:
// a progress tick must not snap a paged/scrolling user back to page one.
function patchJobRow(jobId, status, step, message, pct) {
  if (!jobId) return false;
  const sel = `#jobs-list .job[data-jid="${CSS.escape(String(jobId))}"]`;
  const row = document.querySelector(sel);
  if (!row) return false;
  const j = _allJobs.find(x => x.id === jobId);
  if (status) {
    if (j) j.status = status;
    const pill = row.querySelector(".st");
    if (pill) {
      pill.className = "st st-" + status;
      pill.textContent = statusLabel(status);
    }
  }
  if (message != null || pct != null || step != null) {
    let line = row.querySelector(".job-progress");
    let fill = row.querySelector(".prog-fill");
    if (!line) {
      line = document.createElement("div");
      line.className = "job-progress";
      const bar = document.createElement("div");
      bar.className = "prog-bar";
      fill = document.createElement("div");
      fill.className = "prog-fill";
      bar.appendChild(fill);
      row.querySelector(".job-head").after(line, bar);
    }
    const text = _progressText(step, j);
    line.textContent = text;
    line.title = message || text;
    const width = Math.max(0, Math.min(100, pct ?? (j && j.progress && j.progress.pct) ?? 30));
    if (fill) fill.style.width = width + "%";
    if (j) {
      const st = step || (j.progress && j.progress.step);
      // Keep the last known phase: the server stops reporting progress once a job
      // is terminal, and a finished job that lost its bar reads as if nothing ran.
      j.progress = { step: st, message, pct: width };
      _progressCache[jobId] = j.progress;
    }
  }
  return true;
}

// Pipeline steps are machine vocabulary ("mock_asr: 42 segments"). The user gets
// a localized phase name; the raw server text survives only as a tooltip, which
// is also what support reads when a job misbehaves.
function stepLabel(step) {
  const key = "ps_" + step;
  const v = t(key);
  return v === key ? step : v;
}
function _progressText(step, j) {
  const s = step || (j && j.progress && j.progress.step);
  return s ? stepLabel(s) : "";
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (_ws) { try { _ws.close(); } catch {} _ws = null; }
  if (_wsReconnectTimer) { clearTimeout(_wsReconnectTimer); _wsReconnectTimer = null; }
  _wsOpening = false;
  _wsReconnectAttempt = 0;
}
const ICONS = {
  subtitles: "M4 6h16M4 12h10M4 18h13", dubbing: "M12 3v12m0 0l-4-4m4 4l4-4M5 21h14",
  transcribe: "M4 6h16M4 10h16M4 14h10M4 18h7", document: "M7 3h7l4 4v14H7V3z M14 3v4h4",
};
function renderJobs(jobs) {
  const empty = $("#jobs-empty");
  empty.classList.toggle("hidden", jobs.length > 0);
  // "No jobs yet — drop a file above" is a false claim when a status filter hid
  // everything: the account does have jobs, this selection does not. Browser QA
  // filed it as misleading copy, so the filtered case gets its own sentence and a
  // one-tap way back to the full list.
  const nothingHere = !jobs.length && !!jobFilter;
  empty.children[0].hidden = !!jobs.length || nothingHere;
  empty.children[1].hidden = !nothingHere;
  $("#jobs-clear-filter").hidden = !nothingHere;
  const list = $("#jobs-list");
  list.innerHTML = "";
  jobs.forEach(j => {
    _typeCache[j.id] = j.type;
    const div = document.createElement("div");
    div.className = "job";
    div.dataset.jid = j.id; // live events patch this row without a full rebuild
    const jid = esc(j.id);
    const arts = Object.entries(j.artifacts || {});
    const links = arts.map(([kind, url]) =>
      `<button class="art" data-dl="${esc(url)}" data-kind="${esc(kind)}" data-name="${esc(kind + '-' + j.id)}" title="${esc(t("download"))}">${esc(kindLabel(kind))}</button>`
    ).join("");
    const preview = j.artifacts?.srt
      ? `<button class="art" data-preview="${esc(j.id)}">${esc(t("preview"))} \u25B8</button>` : "";
    const audioPlay = j.artifacts?.dubbing
      ? `<button class="art" data-play="${esc(j.artifacts.dubbing)}">\u25B6 WAV</button>` : "";
    const retry = ["failed", "canceled"].includes(j.status)
      ? `<button class="art" data-retry="${esc(j.id)}">${esc(t("retry"))}</button>` : "";
    const cancel = j.status === "queued"
      ? `<button class="art" data-cancel="${esc(j.id)}">${esc(t("cancel"))}</button>` : "";
    const share = j.status === "done"
      ? `<button class="art" data-share="${esc(j.id)}">\u2197 ${esc(t("share"))}</button>` : "";
    // The disclosure appears only where an engine was actually asked to work: a
    // toggle that always opens onto "nothing ran" is a lie about the feature,
    // and hiding the answer for a job that did run is the lie this fixes.
    const asked = j.options && (j.options.align || j.options.words);
    const settled = j.status === "done" || j.status === "failed";
    const opened = asked && settled && _stepsOpen.has(j.id);
    const stepsBtn = asked && settled
      ? `<button class="art" data-steps="${esc(j.id)}" aria-expanded="${opened ? "true" : "false"}">${esc(t(opened ? "steps_hide" : "steps_show"))}</button>`
      : "";
    const stepsHtml = opened ? renderSteps(j.id) : "";
    // Only a job that can still move shows a progress line (see pruneProgress).
    const prog = isActiveJob(j) ? (j.progress || _progressCache[j.id] || null) : null;
    const progressHtml = prog
      ? `<div class="job-progress" title="${esc(prog.message || "")}">${esc(_progressText(prog.step, j))}</div><div class="prog-bar"><div class="prog-fill" style="width:${Math.min(100, prog.pct || 30)}%"></div></div>`
      : "";
    const errText = j.error ? (localizeServerError(j.error) || j.error) : "";
    // The pipeline knows whether the words in this file were recognised or made up
    // (real ASR configured, binary missing → the job still completes). A subtitle
    // prefix `[демо]` is invisible in a list of cards, so the fact gets its own
    // chip, with the server's reason as the tooltip.
    const demoAsr = j.engines && j.engines.asr_demo
      ? `<span class="mode-chip" title="${esc(j.engines.asr_reason || "")}">` +
        `${esc(t("asr_demo_chip"))}</span>` : "";
    div.innerHTML = `
      <div class="job-head">
        <div>
          <div class="job-title">${esc(typeLabel(j.type))}</div>
          <div class="job-meta">${esc(j.src)} \u2192 ${esc(j.tgt)} \u00b7 ${esc(fmtMinutes(j.minutes))} ${esc(t("minutes"))} ${demoAsr}</div>
        </div>
        <span class="st st-${esc(j.status)}">${esc(statusLabel(j.status))}</span>
      </div>
      ${progressHtml}
      ${j.error ? `<div class="job-err" title="${esc(j.error)}">${esc(errText)}</div>` : ""}
      <div class="acts">${preview}${audioPlay}${links}${share}${stepsBtn}${retry}${cancel}</div>${stepsHtml}`;
    list.appendChild(div);
  });
  list.querySelectorAll("[data-steps]").forEach(b =>
    b.addEventListener("click", async () => {
      const id = b.dataset.steps;
      if (_stepsOpen.has(id)) {
        _stepsOpen.delete(id);
      } else {
        _stepsOpen.add(id);
        if (!_stepsCache[id]) {
          // One fetch per job, then the cache and the paint are separate: the list
          // endpoint has no timeline, and a collapsed card must not pay for one.
          try {
            const res = await api(`/api/jobs/${id}`);
            _stepsCache[id] = (res.timeline || []).filter(e => e && e.data && e.data.code);
          } catch (e) {
            _stepsCache[id] = [];
            toast(t("steps_failed"), true);
          }
        }
      }
      renderJobs(_allJobs);   // repaint from cache: no second request, locale-correct
      // The repaint replaced this node; a keyboard user must not be thrown back to
      // the top of the page for having asked a question.
      const again = list.querySelector(`[data-steps="${CSS.escape(id)}"]`);
      if (again) again.focus();
    }));
  list.querySelectorAll("[data-preview]").forEach(b =>
    b.addEventListener("click", () => openPlayer(b.dataset.preview)));
  list.querySelectorAll("[data-play]").forEach(b =>
    b.addEventListener("click", () => playAudio(b.dataset.play, b)));
  list.querySelectorAll("[data-retry]").forEach(b =>
    b.addEventListener("click", async () => {
      await api(`/api/jobs/${b.dataset.retry}/retry`, { method: "POST" }).catch(e => toast(e.message, true));
      startPolling(); refreshJobs();
    }));
  list.querySelectorAll("[data-cancel]").forEach(b =>
    b.addEventListener("click", async () => {
      await api(`/api/jobs/${b.dataset.cancel}/cancel`, { method: "POST" }).catch(e => toast(e.message, true));
      refreshJobs(); refreshMe();
    }));
  list.querySelectorAll("[data-dl]").forEach(b =>
    b.addEventListener("click", async (ev) => {
      ev.preventDefault();
      await downloadArtifact(b.dataset.dl, b.dataset.kind, b.dataset.name);
    }));
  list.querySelectorAll("[data-share]").forEach(b =>
    b.addEventListener("click", async () => {
      try {
        const res = await api(`/api/jobs/${b.dataset.share}/share`, { method: "POST",
          body: form({ ttl_hours: "72" }) });
        const url = location.origin + res.share_url;
        await navigator.clipboard.writeText(url);
        toast(t("link_copied"));
      } catch (e) { toast(e.message, true); }
    }));
}
// скачивание через Authorization-заголовок: токен не светится в URL/логах
const DL_EXT = { align: ".json", words: ".json", diarization: ".json",
                 layout: ".json", dubbing: ".wav", ass: ".ass",
                 ass_karaoke: ".ass", srt: ".srt", srt_bilingual: ".srt",
                 document: ".txt", transcript: ".txt" };

function dlName(kind, name, disposition) {
  // The extension comes from the server's own `Content-Disposition` where it can,
  // and from the artifact kind where it cannot: guessing it from a substring in the
  // URL saved the engine report as `align-<id>.txt`, which a person cannot tell from
  // a text file. The basename stays the id-stamped one the chip shows, because a
  // person who downloads three jobs must be able to tell the files apart.
  const disp = (disposition || "").match(/filename="[^"]*(\.[A-Za-z0-9]+)"/);
  return name + (disp ? disp[1] : (DL_EXT[kind] || ".txt"));
}

async function downloadArtifact(url, kind, name) {
  if (!token) return requireAuth();
  try {
    const resp = await fetch(url, { headers: { Authorization: "Bearer " + token } });
    if (!resp.ok) throw httpError(resp.status);
    const blob = await resp.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = dlName(kind, name, resp.headers.get("Content-Disposition"));
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  } catch (e) { toast(e.message, true); }
}

// in-app audio preview for dubbing WAV
let _currentAudio = null;
async function playAudio(url, btn) {
  if (!token) return requireAuth();
  // toggle stop if same button clicked
  if (_currentAudio && _currentAudio._btn === btn) {
    _currentAudio.pause(); _currentAudio = null;
    btn.textContent = "\u25B6 WAV";
    return;
  }
  if (_currentAudio) { _currentAudio.pause(); _currentAudio._btn.textContent = "\u25B6 WAV"; }
  try {
    const resp = await fetch(url, { headers: { Authorization: "Bearer " + token } });
    if (!resp.ok) throw httpError(resp.status);
    const blob = await resp.blob();
    const audio = new Audio(URL.createObjectURL(blob));
    audio._btn = btn;
    btn.textContent = "\u23F8 WAV";
    audio.onended = () => { btn.textContent = "\u25B6 WAV"; _currentAudio = null; };
    await audio.play();
    _currentAudio = audio;
  } catch (e) { toast(e.message, true); btn.textContent = "\u25B6 WAV"; }
}
function typeLabel(ty) {
  // t() echoes the key it could not resolve, so an unknown future job type must
  // show its raw value rather than "t_something_new".
  const key = "t_" + ty;
  const v = t(key);
  return v === key ? ty : v;
}
// Status chips used to print the raw machine value ("done", "failed") in every
// locale. t() returns the key when a status has no translation, so an unknown
// future status still shows its raw value instead of "st_hypothetical".
function statusLabel(st) {
  const key = "st_" + st;
  const v = t(key);
  return v === key ? st : v;
}
// list markup must not outlive the session it belongs to: on a shared device the
// next person to open the panel must not read the previous account's jobs.
function clearNotifPanel() {
  const list = $("#notif-list");
  if (list) list.innerHTML = "";
}
function kindLabel(k) {
  // Brand tokens, not prose — the same reason SRT/ASS/WAV are untranslated. The
  // `|| k` fallback used to render `align`, `words` and `ass_karaoke` to a customer
  // verbatim from the database column: internal names on a paid screen. Every kind
  // the server can hand out is listed here, and a gate fails the build if one is
  // added there without a label here.
  return { srt: "SRT", srt_bilingual: "SRT 2\u00d7", ass: "ASS", dubbing: "WAV",
           transcript: "TXT", document: "TXT", diarization: "TURN", layout: "QATOR",
           align: "JIMLIK", words: "SO\u02bbZ", ass_karaoke: "ASS \u266A" }[k] || k;
}
async function refreshJobs(opts = {}) {
  if (!token) return;
  // One GET per burst, not one per event. A single job pushes five live frames
  // inside ~1.1 s and every one of them called refreshJobs(): browser QA counted
  // six /api/jobs round-trips for one short job. Coalesce them into the request
  // already running plus one trailing refresh, because the last frame is the one
  // whose payload is actually current.
  if (_jobsFetch) {
    // The trailing refresh must be at least as strong as anything queued: a
    // caller asking for a full reset (keepPages off) wins over a paging one.
    _jobsQueued = _jobsQueued === null ? opts : {
      keepPages: !!(_jobsQueued.keepPages && opts.keepPages),
      fromLive: !!(_jobsQueued.fromLive || opts.fromLive),
    };
    return _jobsFetch;
  }
  _jobsFetch = _fetchJobs(opts).finally(() => {
    _jobsFetch = null;
    const queued = _jobsQueued;
    _jobsQueued = null;
    if (queued) refreshJobs(queued);
  });
  return _jobsFetch;
}

async function _fetchJobs(opts = {}) {
  if (!token) return;
  // keepPages: a live-channel refetch must not throw away rows the user paged to.
  // fromLive: never re-dial the socket from inside the channel's own callback.
  const keepPages = !!opts.keepPages;
  const fromLive = !!opts.fromLive;
  const gen = _sessionGen;
  try {
    if (!keepPages) { _nextCursor = null; _allJobs = []; }
    let url = "/api/jobs?limit=20";
    if (jobFilter) url += "&status=" + encodeURIComponent(jobFilter);
    const { jobs, next_cursor } = await api(url);
    // Signed out or switched accounts while this GET was running: the payload is
    // somebody else's, and painting it would undo what logout() just cleared.
    if (gen !== _sessionGen || !token) return;
    if (keepPages && _allJobs.length) {
      const fresh = new Map(jobs.map(j => [j.id, j]));
      for (let i = 0; i < _allJobs.length; i++) {
        const f = fresh.get(_allJobs[i].id);
        if (f) { _allJobs[i] = f; fresh.delete(f.id); }
      }
      _allJobs = [...fresh.values()].concat(_allJobs); // jobs we hadn't seen yet
    } else {
      _allJobs = jobs;
      _nextCursor = next_cursor || null;
    }
    pruneProgress(_allJobs);
    renderJobs(_allJobs);
    renderLoadMore();
    // если есть активные — стартуем polling (после F5 по mid-running job)
    if (!fromLive && jobs.some(isActiveJob)) startPolling();
  } catch (e) {
    // Never swallow here: an empty catch once hid a ReferenceError, and the
    // studio came back up blind after F5 with no list, no socket and no polling.
    console.error("jobs_refresh", e);
  }
}
async function loadMoreJobs() {
  if (!_nextCursor || !token) return;
  try {
    let url = "/api/jobs?limit=20&cursor=" + encodeURIComponent(_nextCursor);
    if (jobFilter) url += "&status=" + encodeURIComponent(jobFilter);
    const { jobs, next_cursor } = await api(url);
    _allJobs = _allJobs.concat(jobs);
    _nextCursor = next_cursor || null;
    pruneProgress(_allJobs);
    renderJobs(_allJobs);
    renderLoadMore();
  } catch (e) { console.error("jobs_load_more", e); }
}
function renderLoadMore() {
  const container = $("#jobs-list");
  const existing = container.querySelector(".load-more");
  if (existing) existing.remove();
  if (_nextCursor) {
    const btn = document.createElement("button");
    btn.className = "btn btn-quiet btn-sm load-more";
    btn.textContent = t("load_more");
    btn.style.cssText = "display:block;width:100%;margin-top:12px";
    btn.addEventListener("click", loadMoreJobs);
    container.appendChild(btn);
  }
}

// ─── предпросмотр результата: материал job'а + подсветка слов движка ─────────
// Просмотр раньше просил клиента заново выбрать свой файл и парсил SRT в браузере.
// Теперь карточки, пословные тайминги и голоса приходят одним ответом с сервера,
// а медиа берётся из самой задачи: два толкования одного файла (парсер в JS и
// парсер в движке) — это расхождение, которое никто не заметит, пока подсветка не
// начнёт опережать речь на слог.
let _pcues = [];         // строки /caption: {start,end,text,words,speaker}
let _pmedia = null;      // URL blob'а медиа или null (записи больше нет)
let _pcur = -1;          // индекс активной карточки: перекрашивать раз в смену
let _pstats = { words: 0, voices: 0, total: 0 };   // считаны один раз на загрузку
let _pjob = null, _pbytes = 0, _pnoteKey = "", _pdemo = false;

// Строки, которые генерирует JS, а не словарь: смена языка обязана переписать и
// их, иначе просмотр останется висеть на языке прошлой сессии.
function paintPlayerNote() {
  $("#p-srtname").textContent = (_pbytes ? t("player_from_job") : t("player_srt")) +
    " · " + (_pbytes ? Math.round(_pbytes / 1024) + " KB" : (_pjob || "").slice(0, 8));
  $("#p-meta").textContent = _pnoteKey ? t(_pnoteKey) : "";
}

window.__playerRepaint = () => {
  if (!_pjob || !$("#player-dlg").open) return;
  paintPlayerNote();
  if (_pdemo) $("#p-demo").textContent = t("asr_demo_chip");
  if (_pnoteKey) return;                      // the notice is already the message
  _pcur = -1;
  const el = $($("#p-video").hidden ? "#p-audio" : "#p-video");
  if (_pcues.length) playerTime(el.currentTime || _pcues[0].start);
}

function paintCue(c) {
  const box = $("#p-cue"), sp = $("#p-speaker"), dm = $("#p-demo");
  if (!c) {
    // Between the last card and the end of the tape there is nothing to show —
    // but the notice line must not go blank: it is the only place that says how
    // many cards and words this preview holds, and silence reads as a bug.
    box.textContent = ""; sp.hidden = true; _pcur = -1;
    _pnoteKey = ""; paintPlayerNote(); paintStats(null);
    return;
  }
  const idx = _pcues.indexOf(c);
  if (idx === _pcur) return;                 // timeupdate comes 4×/second
  _pcur = idx;
  box.innerHTML = (c.words && c.words.length)
    ? c.words.map(w => `<span class="p-word">${esc(w.w)}</span>`).join(" ")
    : esc(c.text);
  // Diarization numbers speakers from 1 (`[S1]` in the file itself); adding one
  // here invented a second voice on a job the engine heard as one.
  sp.hidden = c.speaker === null || c.speaker === undefined;
  if (!sp.hidden) sp.textContent = "S" + c.speaker;
  dm.hidden = !_pdemo;
  if (_pdemo) dm.textContent = t("asr_demo_chip");
  paintStats(idx + 1);
}

function paintStats(cardNo) {
  $("#p-meta").textContent = tf("player_stats", {
    a: cardNo === null ? "—" : cardNo, b: _pstats.total || _pcues.length,
    c: _pstats.words, d: _pstats.voices });
}

function paintWord(c, tsec) {
  if (!c || !c.words || !c.words.length) return;
  const spans = $("#p-cue").children;
  for (let i = 0; i < c.words.length; i++) {
    const w = c.words[i], el = spans[i];
    if (!el) continue;
    el.classList.toggle("on", tsec >= w.s && tsec < w.e);
  }
}

function playerTime(sec) {
  const c = _pcues.find(x => sec >= x.start && sec <= x.end);
  paintCue(c || null);
  if (c) paintWord(c, sec);
}

async function openPlayer(jobId) {
  const dlg = $("#player-dlg");
  dlg.showModal();
  _pjob = jobId; _pbytes = 0; _pnoteKey = ""; _pdemo = false;
  _pcues = []; _pcur = -1; _pstats = { words: 0, voices: 0, total: 0 };
  $("#p-cue").textContent = "";
  $("#p-speaker").hidden = true;
  $("#p-demo").hidden = true;
  $("#p-meta").textContent = "";
  $("#p-srtname").textContent = t("player_srt") + " · " + jobId.slice(0, 8);
  hideMedia();
  let cap;
  try {
    cap = await api(`/api/jobs/${jobId}/caption`);
  } catch (e) { toast(e.message || t("err_not_ready"), true); return; }
  _pcues = cap.cues || [];
  _pdemo = !!cap.demo;
  _pstats = {
    words: cap.words || _pcues.reduce((n, x) => n + ((x.words || []).length), 0),
    voices: new Set(_pcues.filter(x => x.speaker !== null && x.speaker !== undefined)
                    .map(x => x.speaker)).size,
    total: cap.total || _pcues.length };
  if (!cap.count) { _pnoteKey = "steps_none"; $("#p-meta").textContent = t("steps_none"); }
  if (cap.media) {
    try {
      const resp = await fetch(`/api/jobs/${jobId}/media`,
        { headers: { Authorization: "Bearer " + token } });
      if (!resp.ok) throw new Error(String(resp.status));
      _pmedia = URL.createObjectURL(await resp.blob());
      showMedia(cap.media.kind, _pmedia, cap.media.bytes);
    } catch (err) {
      _pmedia = null;
      _pnoteKey = "player_no_media";
      $("#p-meta").textContent = t("player_no_media");
    }
  } else {
    _pmedia = null;
    _pnoteKey = "player_no_media";
    $("#p-meta").textContent = t("player_no_media");
  }
}

function showMedia(kind, url, bytes) {
  const v = $("#p-video"), a = $("#p-audio");
  const el = kind === "video" ? v : a;
  v.hidden = el !== v; a.hidden = el !== a;
  if (el.src !== url) { el.src = url; el.load(); }
  // assign, never addEventListener: this function runs again on every manual file
  // pick, and a listener stack would paint the same cue from three handlers.
  el.ontimeupdate = () => playerTime(el.currentTime);
  _pbytes = bytes || 0;
  paintPlayerNote();
}

function hideMedia() {
  [$("#p-video"), $("#p-audio")].forEach(el => {
    el.pause(); el.removeAttribute("src"); el.load(); el.hidden = true;
  });
  if (_pmedia) { URL.revokeObjectURL(_pmedia); _pmedia = null; }
}

$("#p-load-video").addEventListener("click", () => $("#p-vfile").click());
$("#p-vfile").addEventListener("change", () => {
  const f = $("#p-vfile").files[0];
  if (!f) return;
  // Ручной выбор остаётся запасным путём: запись клиента могла быть стёрта
  // retention-очисткой, и тогда просмотр должен честно сказать об этом, а не
  // притворяться сломанным.
  _pmedia = URL.createObjectURL(f);
  showMedia(f.type.startsWith("video") ? "video" : "audio", _pmedia, f.size);
});
// The `close` event is the clean answer, but it is not the only one: a browser
// that never fires it (or a dialog dismissed by Esc) must not leave the tape
// playing behind a closed window — that is a stranger's meeting audio, audible.
$("#player-dlg").addEventListener("close", stopPlayer);
$("#player-dlg").addEventListener("cancel", stopPlayer);
$("#player-close").addEventListener("click", () => {
  stopPlayer();                          // stop first: close may not fire anywhere
  $("#player-dlg").close();
});

function stopPlayer() {
  hideMedia(); _pcues = []; _pjob = null; _pbytes = 0; _pnoteKey = ""; _pdemo = false;
}
$("#pay-close").addEventListener("click", () => $("#pay-dlg").close());

// ─── pricing (landing) + top-up (studio) из /api/plans ───
let _plans = null; // cached /api/plans: the landing must relocalize with no request
async function renderPricing() {
  try { _plans = (await api("/api/plans")).plans; paintPricing(); } catch {}
}
function paintPricing() {
  if (!_plans) return;
  const plans = _plans;
  const order = ["free", "pro", "studio"];
  const cur = currentLang === "uz" ? "120 000 UZS" : currentLang === "ru" ? "900 ₽" : "$9";
  const feats = {
    free: ["10 " + t("minutes")],
    pro: ["120 " + t("minutes") + t("mo"), t("cap_glo_t"), "SRT + ASS + WAV"],
    studio: ["500 " + t("minutes") + t("mo"), t("feat_api"), t("cap_glo_t")],
  };
  $("#tiers").innerHTML = order.map(key => {
    const p = plans[key]; if (!p) return "";
    const price = key === "free" ? t("free") : key === "pro" ? cur : ("$" + p.price_usd + t("mo"));
    return `<div class="tier ${key === "pro" ? "hot" : ""}">
      ${key === "pro" ? `<span class="tag">${esc(t("popular"))}</span>` : ""}
      <h3>${esc(t("plan_" + key))}</h3>
      <div class="price">${esc(price)}</div>
      <ul>${feats[key].map(f => `<li>${esc(f)}</li>`).join("")}</ul>
      <button class="btn ${key === "pro" ? "btn-solid" : "btn-quiet"} btn-block" data-tier="${key}">${esc(t("buy"))}</button>
    </div>`;
  }).join("");
  $("#pay-body").dataset.plans = JSON.stringify(plans);
  $$("#tiers [data-tier]").forEach(b => b.addEventListener("click", () => {
    if (!token) { showStudio(); return; }
    openPay(b.dataset.tier);
  }));
}
function openPay(planKey) {
  const plans = JSON.parse($("#pay-body").dataset.plans || "{}");
  const p = plans[planKey]; if (!p) return;
  const minutes = p.minutes;
  $("#pay-body").innerHTML = `
    <div class="pay-line"><span>${t("plan_" + planKey)}</span><b>${t("pay_line")}: +${minutes} ${t("minutes")}</b></div>
    <p class="pay-note">${t("pay_soon")}</p>
    <div class="pay-methods">
      <button class="btn btn-quiet btn-block" disabled>Payme</button>
      <button class="btn btn-quiet btn-block" disabled>Click</button>
    </div>`;
  $("#pay-dlg").showModal();
}
$("#btn-topup").addEventListener("click", () => {
  if (!token) { showStudio(); return; }
  openPay("pro");
});

// ─── job filter pills ───
$$(".filter-pill").forEach(pill => {
  pill.addEventListener("click", () => {
    $$(".filter-pill").forEach(p => p.classList.remove("on"));
    pill.classList.add("on");
    jobFilter = pill.dataset.filter || "";
    refreshJobs();
  });
});
// The empty-state escape hatch reuses the pill, so the active state, the filter
// variable and the refetch cannot drift apart.
$("#jobs-clear-filter").addEventListener("click", () =>
  document.querySelector('.filter-pill[data-filter=""]').click());

// ─── notifications bell ───
let _notifOpen = false;
let _notifs = [], _unread = 0;
async function refreshNotif() {
  if (!token) return;
  try {
    const d = await api("/api/notifications?limit=30");
    _notifs = d.notifications ?? []; _unread = d.unread_count ?? 0;
    paintNotif();
  } catch (e) { console.error("notifications_refresh", e); }
}
function paintNotif() {
  const badge = $("#notif-badge");
  if (!badge) return;
  if (!token) { badge.classList.add("hidden"); return; } // never paint a left-over session
  if (_unread > 0) {
    badge.textContent = _unread > 9 ? "9+" : String(_unread);
    badge.classList.remove("hidden");
  } else {
    badge.classList.add("hidden");
  }
  // render panel content
  const list = $("#notif-list");
  if (!Array.isArray(_notifs)) _notifs = [];
  if (!_notifs.length) {
    list.innerHTML = `<p style="padding:16px;text-align:center;color:var(--ink-2);font-size:13px">${esc(t("notif_empty"))}</p>`;
    return;
  }
  list.innerHTML = _notifs.map(n => {
    const unread = !n.read_at;
    // The server stores one machine title per kind; the visitor's language wins.
    // t() echoes the key when a kind has no translation, so an unknown kind
    // still falls back to whatever the backend wrote. The body is separate:
    // for a failed job it is the only diagnostic the visitor can report to us.
    const key = "notif_" + n.kind;
    const label = t(key);
    const title = label === key ? (n.title || "") : label;
    // n.body is the server's own prose ("Job completed", "mock_tts: 12 phrases"):
    // machine vocabulary that must not become visible copy in uz/ru/en. It stays
    // on the row as a tooltip for the user and for support.
    return `<div class="notif-item ${unread ? 'unread' : 'read'}" data-nid="${esc(n.id)}" title="${esc(n.body || "")}">
      <span class="notif-dot"></span>
      <div class="notif-content"><strong>${esc(title)}</strong>
        <span class="notif-time">${esc(timeAgo(n.created_at))}</span></div>
    </div>`;
  }).join("");
  // click to mark read
  list.querySelectorAll(".notif-item").forEach(el => {
    el.addEventListener("click", async () => {
      if (el.classList.contains("unread")) {
        await api(`/api/notifications/${el.dataset.nid}/read`, { method: "POST" }).catch(() => {});
        el.classList.remove("unread"); el.classList.add("read");
        refreshNotif();
      }
    });
  });
}
function timeAgo(iso) {
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return t("ago_now");
  // replaceAll: a translation may mention the unit twice ("{n} мин назад {n}")
  if (s < 3600) return t("ago_min").replaceAll("{n}", Math.floor(s / 60));
  if (s < 86400) return t("ago_hour").replaceAll("{n}", Math.floor(s / 3600));
  return t("ago_day").replaceAll("{n}", Math.floor(s / 86400));
}
$("#btn-notif").addEventListener("click", () => {
  if (!token) return requireAuth();
  _notifOpen = !_notifOpen;
  $("#notif-panel").classList.toggle("hidden", !_notifOpen);
  if (_notifOpen) refreshNotif();
});
// relative timestamps must age while the panel stays open — only the time span
// changes; rebuilding the list would reset scroll position and hover state.
setInterval(() => {
  if (!_notifOpen || !token || !Array.isArray(_notifs)) return;
  $$("#notif-list .notif-item").forEach((el, i) => {
    const n = _notifs[i];
    const span = el.querySelector(".notif-time");
    if (n && span) span.textContent = timeAgo(n.created_at);
  });
}, 60000);
$("#notif-mark-all").addEventListener("click", async () => {
  try {
    await api("/api/notifications/read-all", { method: "POST" });
    refreshNotif();
  } catch {}
});
// close panel on outside click
document.addEventListener("click", (e) => {
  if (_notifOpen && !e.target.closest("#notif-panel") && !e.target.closest("#btn-notif")) {
    _notifOpen = false;
    $("#notif-panel").classList.add("hidden");
  }
});

// ─── settings dialog ───
$("#btn-settings").addEventListener("click", () => {
  if (!token) return requireAuth();
  // pre-fill name from current user
  api("/api/me").then(me => { $("#set-name").value = me.user.name || ""; });
  loadUsage();
  $("#settings-dlg").showModal();
});
$("#settings-close").addEventListener("click", () => $("#settings-dlg").close());
$("#set-name-save").addEventListener("click", async () => {
  const name = $("#set-name").value.trim();
  if (!name) return toast(t("set_name_required"), true);
  try {
    await api("/api/account/name", { method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast(t("set_name_updated"));
    refreshMe();
  } catch (e) { toast(e.message, true); }
});
$("#set-secret-save").addEventListener("click", async () => {
  const old_s = $("#set-old").value.trim();
  const new_s = $("#set-new").value.trim();
  if (!old_s || !new_s) return toast(t("set_fields_required"), true);
  if (new_s.length < 10) return toast(t("set_secret_short"), true);
  // Revoking every session server-side means our current token dies mid-request:
  // silence the live channel first, otherwise a poll lands a 401 and logs out
  // the very account that just changed its secret.
  stopPolling();
  try {
    const resp = await api("/api/account/change-secret", { method: "POST",
      body: form({ old_secret: old_s, new_secret: new_s }) });
    $("#set-old").value = ""; $("#set-new").value = "";
    toast(t("set_secret_changed"));
    if (resp.token) { adopt(resp.token); refreshAll(); } // fresh session + repaint
  } catch (e) {
    toast(e.message, true);
    if (token) { startPolling(); refreshMe(); } // still signed in: resume watching
  }
});
$("#set-export").addEventListener("click", async () => {
  if (!token) return requireAuth();
  try {
    const resp = await fetch("/api/account/export", { headers: { Authorization: "Bearer " + token } });
    if (!resp.ok) throw httpError(resp.status);
    const blob = await resp.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "ovoz-data-export.json";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    toast(t("export_done"));
  } catch (e) { toast(e.message, true); }
});
$("#set-delete").addEventListener("click", async () => {
  if (!token) return;
  const confirmed = confirm(t("set_delete_confirm"));
  if (!confirmed) return;
  try {
    await api("/api/account", { method: "DELETE" });
    logout();
    toast(t("account_deleted"));
  } catch (e) { toast(e.message, true); }
});

// ─── usage & billing analytics ───
let _usage = null, _usageErr = ""; // cached so a locale switch repaints the labels
async function loadUsage() {
  if (!token) return;
  _usage = null; _usageErr = "";
  paintUsage();
  try {
    _usage = await api("/api/account/usage?days=30");
  } catch (e) { _usageErr = e.message; }
  paintUsage();
}
function paintUsage() {
  const box = $("#usage-body");
  if (!box) return;
  if (!token) { box.innerHTML = ""; return; }
  if (_usageErr) { box.innerHTML = `<span class="dim">${esc(_usageErr)}</span>`; return; }
  if (!_usage) { box.innerHTML = `<span class="dim">${esc(t("usage_loading"))}</span>`; return; }
  const u = _usage;
  const days = Object.keys(u.daily_minutes || {}).length;
  if (!u.total_jobs && !days) { box.innerHTML = `<span class="dim">${esc(t("usage_no_data"))}</span>`; return; }
  // build a tiny bar chart of last 14 activity days
  const entries = Object.entries(u.daily_minutes).sort().slice(-14);
  const maxm = Math.max(1, ...entries.map(([, m]) => m));
  const bars = entries.map(([d, m]) =>
    `<div class="usage-bar" title="${esc(d + ": " + m + " " + t("minutes"))}">` +
    `<div class="usage-bar-fill" style="height:${Math.round((m / maxm) * 100)}%"></div></div>`).join("");
  const spend = (u.total_paid_minor / 100).toLocaleString() + " UZS";
  box.innerHTML = `
    <div class="usage-stats">
      <div><b>${esc(u.total_jobs)}</b><span>${esc(t("usage_jobs"))}</span></div>
      <div><b>${esc(Math.round(u.total_minutes))}</b><span>${esc(t("usage_minutes"))}</span></div>
      <div><b>${esc(spend)}</b><span>${esc(t("usage_spend"))}</span></div>
      <div><b>${esc(u.streak_days)}</b><span>${esc(t("usage_streak"))}</span></div>
    </div>
    <div class="usage-chart" aria-hidden="true">${bars}</div>`;
}

// ─── focus trap (for non-native modals) ───
function trapFocus(container) {
  const sel = 'a[href],button:not([disabled]),textarea,input,select,[tabindex]:not([tabindex="-1"])';
  function handler(e) {
    if (e.key !== "Tab") return;
    const nodes = [...container.querySelectorAll(sel)].filter(n => n.offsetParent !== null);
    if (!nodes.length) return;
    const first = nodes[0], last = nodes[nodes.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
  container.addEventListener("keydown", handler);
  return () => container.removeEventListener("keydown", handler);
}

// ─── coming back from the background ───────────────────────────────
// A hidden tab has its timers throttled to roughly one a minute, and a Telegram
// Mini App is hidden the moment the user opens the chat list. The live socket often
// dies with it. Without this handler the visitor returns to a finished job still
// reading "queued", a balance that is not theirs, and a badge that never counted
// the completion — every one of those is a lie about work the server already did.
document.addEventListener("visibilitychange", () => {
  if (document.hidden || !token) return;
  refreshAll().catch(() => {});       // jobs, me, notifications — coalesced, once
  _connectWS();                       // idempotent: it no-ops on a live socket
});
addEventListener("pageshow", () => { if (token) { refreshAll().catch(() => {}); _connectWS(); } });

// ─── first-run onboarding tour ───
const ONB_STEPS = [
  { icon: "🎬", title: "onb1_t", body: "onb1_d" },
  { icon: "📤", title: "onb2_t", body: "onb2_d" },
  { icon: "📚", title: "onb3_t", body: "onb3_d" },
  { icon: "💳", title: "onb4_t", body: "onb4_d" },
];
let _onbIdx = 0, _onbRelease = null;
function _onbRender() {
  const s = ONB_STEPS[_onbIdx], last = _onbIdx === ONB_STEPS.length - 1;
  $("#onb-icon").textContent = s.icon;
  $("#onb-title").textContent = t(s.title);
  $("#onb-body").textContent = t(s.body);
  $("#onb-next").textContent = last ? t("onb_start") : t("onb_next");
  $("#onb-dots").innerHTML = ONB_STEPS.map((_, i) =>
    `<span class="onb-dot${i === _onbIdx ? " on" : ""}"></span>`).join("");
}
function onboardingOpen() {
  _onbIdx = 0;
  $("#onboarding").classList.remove("hidden");
  _onbRender();
  _onbRelease = trapFocus($("#onboarding"));
  $("#onb-next").focus();
}
function onboardingClose(markSeen) {
  $("#onboarding").classList.add("hidden");
  if (_onbRelease) { _onbRelease(); _onbRelease = null; }
  localStorage.setItem("ovoz_onb_done", "1");
  if (markSeen) api("/api/account/onboarding", { method: "POST" }).catch(() => {});
}
$("#onb-next").addEventListener("click", () => {
  if (_onbIdx < ONB_STEPS.length - 1) { _onbIdx++; _onbRender(); $("#onb-next").focus(); }
  else onboardingClose(true);
});
$("#onb-skip").addEventListener("click", () => onboardingClose(true));
$("#onboarding").addEventListener("keydown", (e) => { if (e.key === "Escape") onboardingClose(true); });
async function maybeShowOnboarding() {
  if (!token || localStorage.getItem("ovoz_onb_done")) return;
  try {
    const st = await api("/api/account/onboarding");
    if (st.needs_tour) onboardingOpen();
    else localStorage.setItem("ovoz_onb_done", "1");
  } catch {}
}

// ─── glossary ───
$("#g-add").addEventListener("click", async () => {
  const src = $("#g-src").value.trim(), tgt = $("#g-tgt").value.trim();
  if (!src || !tgt) return;
  await api("/api/glossary", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify([{ src, tgt }]) }).catch(e => toast(e.message, true));
  $("#g-src").value = ""; $("#g-tgt").value = "";
  refreshGlossary();
});
let _gloss = []; // cached terms: locale change repaints, no refetch
async function refreshGlossary() {
  if (!token) return;
  try {
    _gloss = (await api("/api/glossary")).terms ?? [];
    paintGlossary();
  } catch (e) { console.error("glossary_refresh", e); }
}
function paintGlossary() {
  const list = $("#glossary-list");
  if (!list) return;
  if (!token) { list.innerHTML = ""; return; }
  if (!Array.isArray(_gloss)) _gloss = [];
  list.innerHTML = _gloss.map(x =>
    `<span class="chip">${esc(x.src_term)} → <b>${esc(x.tgt_term)}` +
    `<button class="chip-x" data-del="${esc(x.src_term)}" aria-label="${esc(t("del_term"))}">✕</button></span>`).join("");
  list.querySelectorAll("[data-del]").forEach(b =>
    b.addEventListener("click", async () => {
      await api("/api/glossary", { method: "DELETE", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ src: b.dataset.del }) }).catch(e => toast(e.message, true));
      refreshGlossary();
    }));
}

// ─── bootstrap ───
async function refreshAll() {
  // An authorized load of #studio reaches here twice — once from the hash route
  // and once from tryLogin() — and each pass pulled me/jobs/glossary/notifications,
  // so the page cost eight requests where five belonged. One pass at a time; the
  // second caller joins the first instead of refetching the identical payloads.
  if (_allFetch) return _allFetch;
  _allFetch = Promise.all([refreshMe(), refreshJobs(), refreshGlossary(), refreshNotif()])
    .finally(() => { _allFetch = null; });
  maybeShowOnboarding();
  return _allFetch;
}
// Switching language is a pure repaint: everything on screen came from a cached
// payload, so not one request may fire (this used to cost 6 round-trips a click).
// Account data repaints only while a session is alive, and every painter is
// isolated — one throwing must not leave the page half-localized.
window.onLangChange = () => {
  const painters = [
    paintPricing,
    () => window.__lingRepaint?.(),
    () => window.__turnRepaint?.(),
    () => window.__qatorRepaint?.(),
    () => window.__jimlikRepaint?.(),
    () => window.__sozRepaint?.(),
    () => window.__nafisRepaint?.(),
    () => window.__playerRepaint?.(),
    () => { if (_onbRelease) _onbRender(); }, // tour text is ours, not the dictionary's
  ];
  if (token) {
    painters.push(paintMe, paintGlossary, paintNotif,
      () => { if (_allJobs.length) { renderJobs(_allJobs); renderLoadMore(); } },
      () => { if ($("#settings-dlg")?.open) paintUsage(); });
  }
  for (const fn of painters) {
    try { fn(); } catch (e) { console.error("locale repaint", e); }
  }
};
renderPricing();
if (token) { $("#auth-card").classList.add("hidden"); $("#job-form").classList.remove("hidden"); $("#logout").classList.remove("hidden"); }
// hash routing: открываем studio если #studio в URL (deep-link от Telegram)
if (location.hash === "#studio") showStudio();
addEventListener("hashchange", () => {
  const h = location.hash;
  if (h === "#studio" && $("#studio").classList.contains("hidden")) showStudio();
  else if (!h && !$("#studio").classList.contains("hidden")) showLanding();
  else if (h && h !== "#studio" && !$("#studio").classList.contains("hidden")) {
    // A deep link to a landing section (#ling, #engine, #prices) typed or shared
    // while the studio was open used to land nowhere: every target section lives on
    // the landing page, which was display:none at that moment, so the browser had
    // nothing to scroll to and the visitor saw a blank studio with a changed URL.
    showLanding();
    const el = $(h);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  }
});
// Telegram autologin runs last on purpose. `telegram-web-app.js` can already
// define window.Telegram by the time app.js is evaluated, so tgTryLogin() used to
// fire from the middle of this file: showStudio() → refreshAll() → refreshJobs()
// then wrote to module state (`_allJobs`, `_nextCursor`) that was still in its
// temporal dead zone. The ReferenceError died inside the async chain, the studio
// came up with an empty list and never opened a live channel.
if (window.Telegram && window.Telegram.WebApp) tgTryLogin();
else telegramBoot();
// The script is loaded async, so it can also arrive after app.js finished. The
// flag in tg-boot.js says when it landed; watching that flag beats the old
// "wait 400 ms after load and hope". Outside Telegram the flag never turns true and
// the watcher simply expires, so the browser path pays nothing.
function telegramBoot() {
  let tries = 0;
  const wait = () => {
    if (window.TG_WEBAPP_READY && window.Telegram && window.Telegram.WebApp) tgTryLogin();
    else if (++tries < 20) setTimeout(wait, 200);
  };
  setTimeout(wait, 200);
}
