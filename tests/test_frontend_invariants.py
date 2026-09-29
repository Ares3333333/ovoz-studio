"""Frontend structural invariants for the vanilla-JS studio (no JS test runner).

These lock behaviours that are invisible to pytest-style tests but were each a
real production bug:

* a second `_ws.onopen = ...` assignment silently replaced the first, so polling
  never stopped when the WebSocket connected (double traffic, two renderers);
* `_ws.onerror` cleared the polling timer instead of starting it, and
  `startPolling()` returned as soon as a socket object existed — so a socket that
  never opened left the job list permanently frozen;
* `_connectWS()` re-dialled on every call, and the live channel called
  `refreshJobs()` which called `startPolling()`: one job event spawned a second
  socket, two spawned four, and every socket received every event;
* the locale switcher called refreshAll(), firing six API round-trips per click;
* logout cleared the DOM but left the cached payloads, so a locale switch after
  signing out repainted the previous account's jobs and balance;
* `refreshMe()` logged the user out on any failure, including a network blip;
* the session bearer travelled in the WebSocket query string, which every access
  log between the browser and the container copies verbatim;
* the Service Worker answered code requests stale-while-revalidate, so every
  returning visitor ran the previous release's client against the new API;
* Telegram autologin fired from an IIFE in the middle of app.js, so the boot path
  wrote module state that was still in its temporal dead zone and an empty catch
  hid the ReferenceError: an authorized F5 showed an empty job list with no live
  channel and nothing in the console.

Tests are source-shape assertions: there is no JS runtime in this repo, so the
cheapest honest gate is to make the broken shape un-compilable in CI.
"""
import re
from pathlib import Path

import app as app_pkg

STATIC = Path(__file__).resolve().parents[1] / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8")
SW = (STATIC / "sw.js").read_text(encoding="utf-8")
I18N = (STATIC / "i18n.js").read_text(encoding="utf-8")
PIPELINE = (STATIC.parent / "app" / "pipeline.py").read_text(encoding="utf-8")

WS_START = JS.index("function _connectWS()")
HANDLER_START = JS.index("function _handleWSEvent(msg)")
# _connectWS() hands the socket to _openWS(): the dial block covers both, or the
# assertions would silently describe only half of the connection path.
DIAL_BLOCK = JS[WS_START:HANDLER_START]
HANDLER_BLOCK = JS[HANDLER_START:JS.index("\nfunction ", HANDLER_START + 1)]
LANG_START = JS.index("window.onLangChange = () => {")
LANG_BLOCK = JS[LANG_START:JS.index("\n};", LANG_START)]


def _code(source: str) -> str:
    """Strip // comments: prose about a call must not count as a call."""
    return re.sub(r"(?<![:\"'])//[^\n]*", "", source)


def _handler_body(name: str) -> str:
    """Source of the `ws.name = (...) => { ... };` handler (indent-tolerant)."""
    m = re.search(rf"\bws\.{name} = \([^)]*\) => \{{(.*?)\n\s{{2,}}\}};", DIAL_BLOCK, re.S)
    assert m, f"ws.{name} handler missing or reformatted in app.js"
    return m.group(1)


def _fn_body(name: str) -> str:
    m = re.search(rf"\bfunction {name}\(", JS)
    assert m, f"{name}() vanished from app.js"
    # Step over the parameter list first: a default argument (`opts = {}`) holds
    # a brace, and reading it as the body returns an empty "function" silently.
    depth, i = 1, m.end()
    while i < len(JS):
        if JS[i] == "(":
            depth += 1
        elif JS[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    assert depth == 0, f"unbalanced parameter list in {name}()"
    start = JS.index("{", i)
    depth, i = 0, start
    while i < len(JS):
        if JS[i] == "{":
            depth += 1
        elif JS[i] == "}":
            depth -= 1
            if depth == 0:
                return JS[start:i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces in {name}()")


# ─── real-time channel ───────────────────────────────────────────────────────

def test_each_socket_handler_is_bound_exactly_once():
    # Duplicate assignment is the bug: the later one wins, silently.
    for name in ("onopen", "onmessage", "onclose", "onerror"):
        assert len(re.findall(rf"\bws\.{name} =", JS)) == 1, f"ws.{name} assigned twice"
        assert f"_ws.{name} =" not in JS, f"app.js still assigns _ws.{name} directly"


def test_socket_is_captured_per_connection():
    """Handlers must mutate their own socket, never the (possibly newer) global."""
    assert "_ws = ws;" in DIAL_BLOCK, "socket not captured locally"
    assert "const mine = () => _ws === ws;" in DIAL_BLOCK, "superseded-socket guard missing"


def test_every_handler_ignores_superseded_sockets():
    """Declaring mine() is worthless if the handlers do not consult it: an orphan
    socket that keeps driving _handleWSEvent is the duplicate-work bug itself."""
    for name in ("onopen", "onmessage", "onclose"):
        assert "mine()" in _handler_body(name), f"ws.{name} does not check mine()"


def test_connect_ws_refuses_to_dial_while_a_channel_is_alive():
    """The storm: every open socket gets every event, and each event used to call
    startPolling() again, so sockets doubled per progress tick.

    The first fix for that compared `readyState <= WebSocket.CONNECTING`, which
    counted OPEN as dead — browser QA then saw ~2 extra dials per submit, each
    one closing a healthy socket, spending a ticket and leaving a few hundred
    milliseconds with neither a channel nor a poller. Alive means CONNECTING or
    OPEN; only CLOSING/CLOSED may be replaced.
    """
    guard = re.search(
        r"if \(_ws && _ws\.readyState !== WebSocket\.CLOSED && "
        r"_ws\.readyState !== WebSocket\.CLOSING\) return true;", DIAL_BLOCK)
    assert guard, "no re-entrancy guard before new WebSocket()"
    assert "<= WebSocket.CONNECTING" not in DIAL_BLOCK, \
        "readyState <= CONNECTING treats an OPEN socket as dead and re-dials it"
    assert "if (_wsOpening) return true;" in DIAL_BLOCK, "in-flight ticket dial not deduplicated"
    assert DIAL_BLOCK.count("new WebSocket(") == 1


def test_handshake_failure_always_degrades_to_polling():
    """Three ways the handshake can die, each must leave the user informed.

    A ticket POST that never returns (proxy black-hole) pinned _wsOpening, and
    every later caller returned early on `if (_wsOpening)` — permanently blind.
    A discarded _openWS() return value was the same failure in a different shape.
    """
    body = _code(DIAL_BLOCK)
    assert "_wsOpening = true;" in body
    watchdog = re.search(r"const opening = setTimeout\(\(\) => \{(.*?)\}, (\d+)\);", body, re.S)
    assert watchdog, "ticket fetch has no watchdog: a stalled handshake pins the client blind"
    assert "ensurePolling()" in watchdog.group(1), "watchdog does not degrade to polling"
    assert int(watchdog.group(2)) <= 10000, "handshake watchdog is too slow to matter"
    assert body.count("clearTimeout(opening)") >= 2, "watchdog must be cleared on success too"
    assert re.search(r"!_openWS\([^)]*\)\)\s*ensurePolling\(\)", body), \
        "a socket that cannot be constructed must fall back to polling"
    assert body.count("ensurePolling()") >= 3, \
        "every handshake exit needs the polling fallback"


def test_live_channel_does_not_reopen_the_live_channel():
    """_handleWSEvent must never reach startPolling(): it is the callback *of* the
    channel, so re-entering it is the amplification loop."""
    body = _code(HANDLER_BLOCK)
    assert "startPolling()" not in body, "the live channel re-dials itself"
    assert "refreshJobs({ keepPages: true, fromLive: true })" in body, \
        "live refetch must preserve pagination and not restart the channel"


def test_progress_events_patch_the_row_instead_of_rebuilding_the_list():
    assert "patchJobRow(" in HANDLER_BLOCK
    body = _fn_body("renderJobs")
    assert 'div.dataset.jid = j.id;' in body, "rows are not addressable by job id"
    assert 'data-jid="' in _fn_body("patchJobRow"), "patchJobRow does not select by data-jid"


def test_terminal_announce_is_not_double_counted():
    """A replayed `done` event must not inflate the unread badge twice."""
    assert "alreadySettled" in HANDLER_BLOCK


def test_unreachable_socket_degrades_to_polling():
    """A socket that never opens must not leave the studio blind."""
    close = _handler_body("onclose")
    assert "ensurePolling();" in close, "onclose does not restart polling"
    assert re.search(r"setTimeout\(\(\) => \{[\s\S]*?startPolling\(\)", close), \
        "onclose lost its backoff reconnect"
    assert "watchdog" in DIAL_BLOCK, "no handshake watchdog"
    # the watchdog reaps its own socket even after it was superseded
    watchdog = DIAL_BLOCK[DIAL_BLOCK.index("const watchdog"):DIAL_BLOCK.index("ws.onopen")]
    assert "mine()" not in _code(watchdog), "superseded socket can never be reaped"
    start = JS[JS.index("function startPolling()"):JS.index("function ensurePolling()")]
    assert "_connectWS()" in start and "ensurePolling()" in start


def test_ws_open_stops_polling_and_cancels_pending_redial():
    open_body = _handler_body("onopen")
    assert "clearInterval(pollTimer)" in open_body, "WS open must stop HTTP polling"
    assert "_wsReconnectAttempt = 0" in open_body
    assert "_wsReconnectTimer" in open_body, "a healthy socket must cancel its retry"


def test_no_infinite_reconnect_and_no_anonymous_dial():
    assert "WS_MAX_RECONNECTS" in JS, "backoff has no ceiling"
    # Round-35 swarm audit (F4): the old gate defended a real bug — reconnect
    # conditioned on the local job list means a filtered view or a not-yet-
    # fetched list silences the client forever while a job runs server-side.
    # The bound is the attempts ceiling plus the signed-out early return.
    assert "_allJobs.some(isActiveJob)" not in _handler_body("onclose"), \
        "reconnect must not depend on local job contents"
    assert "if (!token) return;" in _fn_body("startPolling")
    assert re.search(r"if \(!token \|\| pollTimer\) return;", _fn_body("ensurePolling"))


def test_half_open_socket_is_killed_by_liveness_not_by_close_events():
    """Audit F3: after a network change or an iOS background-kill the socket is
    half-open — readyState stays OPEN and onclose never fires, so every
    self-healing path below it is unreachable and progress freezes at 40%.
    Liveness must be proven by FRAMES (pings count), and 90 s of silence must
    force the close that re-arms the machinery."""
    body = _fn_body("_openWS")
    assert "lastRx = Date.now();" in _handler_body("onmessage"), \
        "incoming frames do not stamp liveness"
    assert "Date.now() - lastRx > 90000" in body, \
        "three missed 30 s server pings must force a close"
    assert "clearInterval(liveness)" in _handler_body("onclose"), \
        "the watchdog of a dead socket must not leak"


def test_precache_installs_even_when_one_url_fails():
    """Audit F6: cache.addAll is all-or-nothing — a single non-200 (edge rule,
    deploy 502) rejected the whole install, skipWaiting never ran, and the
    product silently had no service worker at all."""
    install = SW[SW.index("addEventListener('install'"):SW.index("addEventListener('activate'")]
    assert "Promise.allSettled" in install, \
        "install must not be defeated by one failing precache URL"
    assert "cache.addAll" not in install


def test_code_assets_fall_back_across_build_stamps_offline():
    """Audit F5: pruneAndPut deletes superseded ?v= keys, so an old cached
    document requesting its old assets hits an exact-match miss and used to
    get 503-'Offline' as JavaScript — a white screen offline after any deploy.
    Shell/code requests must retry the cache ignoring the query string."""
    fn = SW[SW.index("async function networkFirst"):SW.index("async function pruneAndPut")]
    assert "ignoreSearch: true" in fn, \
        "stale stamped asset requests must resolve to any cached build"


def test_websocket_handshake_uses_a_ticket_not_the_session_token():
    """A query string is copied into every access log between browser and server."""
    assert "?ticket=" in DIAL_BLOCK, "WS url no longer carries a ticket"
    assert "?token=" not in JS, "the bearer must never appear in a URL"
    assert 'api("/api/auth/ws-ticket"' in DIAL_BLOCK
    main_src = (STATIC.parent / "app" / "main.py").read_text(encoding="utf-8")
    assert "token: str = Query" not in main_src, \
        "the WS route still authenticates from the query string"


def test_stop_polling_leaves_no_timer_or_socket_behind():
    body = _fn_body("stopPolling")
    for needed in ("clearInterval(pollTimer)", "_ws = null",
                   "clearTimeout(_wsReconnectTimer)", "_wsOpening = false"):
        assert needed in body, f"stopPolling() does not reset {needed}"


# ─── locale switching: repaint from cache, never from the network ────────────

def test_locale_switch_repaints_without_network():
    forbidden = ("refreshAll(", "api(", "fetch(", "renderPricing(", "loadUsage(",
                 "refreshJobs(", "refreshMe(", "refreshNotif(", "refreshGlossary(",
                 "startPolling(")
    offenders = [f for f in forbidden if f in LANG_BLOCK]
    assert not offenders, f"locale switch performs network work: {offenders}"
    for needed in ("paintPricing", "paintMe", "paintGlossary", "paintNotif",
                   "__lingRepaint", "_onbRender"):
        assert needed in LANG_BLOCK, f"locale switch does not repaint {needed}"


def test_locale_switch_is_token_scoped():
    """Account data must not repaint for a visitor who has signed out."""
    assert "if (token)" in LANG_BLOCK


def test_each_repaint_is_isolated():
    """One throwing painter must not leave the page half-localized."""
    assert "try { fn(); }" in LANG_BLOCK, "painters are not isolated"
    assert "console.error" in LANG_BLOCK


def test_logout_clears_every_cached_payload():
    """Caches outlive the DOM: leaving them populated shows account A's data to
    account B after a locale switch on a shared device."""
    body = _fn_body("logout")
    for cache in ("_me = null", "_gloss = []", "_notifs = []", "_unread = 0",
                  "_allJobs = []", "_nextCursor = null", "prevStatuses = {}",
                  "_notifOpen = false", "delete _typeCache"):
        assert cache in body, f"logout() does not reset {cache}"


def test_painting_is_null_safe_and_fetching_only_logs_out_on_401():
    """A laptop that slept must not sign anybody out; only the server may."""
    body = _fn_body("refreshMe")
    assert "e.code === 401" in body, "refreshMe logs out on any error"
    assert "logout()" in body
    for paint in ("paintMe", "paintGlossary", "paintNotif", "paintUsage"):
        assert "!token" in _fn_body(paint), f"{paint}() has no session guard"


def test_cached_painters_exist_for_every_fetcher():
    """Each refreshX that caches a payload must render from that cache."""
    for fetch, paint in (("refreshMe", "paintMe"), ("refreshGlossary", "paintGlossary"),
                         ("refreshNotif", "paintNotif"), ("renderPricing", "paintPricing"),
                         ("loadUsage", "paintUsage")):
        assert paint + "()" in _fn_body(fetch), f"{fetch}() never calls {paint}()"


def test_change_secret_silences_the_channel_before_revoking_sessions():
    body = JS[JS.index('$("#set-secret-save")'):]
    body = body[:body.index('$("#set-export")')]
    assert "stopPolling();" in body, "a poll can 401 the session being changed"
    assert "adopt(resp.token)" in body, "the new token must flow through adopt()"


# ─── escaping: no server value reaches innerHTML unprotected ──────────────────

_SAFE_INTERP = re.compile(
    r"^(?:"
    r"esc\(|Math\.\w+\([\s\S]*\)(?:\s*\+\s*\"%\"\s*)?|\d+%?|"
    r"[\w.]+\s*\?\s*\"[^\"]*\"\s*:\s*\"\"|"
    r"key === \"pro\" \? \"hot\" : \"\"|"
    r"unread \? 'unread' : 'read'|key"
    r")$"
)


def _html_templates(source: str):
    """Yield (label, body) for each template literal assigned to .innerHTML."""
    for m in re.finditer(r"[\w$.]+\.innerHTML = (`|\"|')", source):
        quote = source[m.end() - 1]
        start = m.end()
        depth, i = 0, start + 1
        while i < len(source):
            ch = source[i]
            if ch == "\\":
                i += 2
                continue
            if quote == "`" and ch == "$" and source[i + 1:i + 2] == "{":
                depth += 1
                i += 1
            elif quote == "`" and ch == "}":
                depth -= 1
            elif quote == "`" and ch == "`" and depth == 0:
                break
            elif quote != "`" and ch == quote:
                break
            i += 1
        yield source[m.start():start], source[start + 1:i]


def _fragment_names(source: str):
    """Identifiers whose value is HTML assembled from esc()'d pieces."""
    trusted = set()
    for m in re.finditer(r"(?:const|let)\s+(\w+)\s*=\s*", source):
        rest = source[m.end():m.end() + 400]
        if not re.match(r"[`\"']", rest.lstrip()[0] if rest else ""):
            continue
        if "<" not in rest.split("\n")[0] and "`<" not in rest and "<" not in rest[:120]:
            continue
        trusted.add(m.group(1))
    return trusted


def test_attribute_interpolations_are_escaped():
    """Any ${...} inside an HTML attribute must pass through esc(): no XSS sinks."""
    attr_re = re.compile(
        r'(aria-label|title|data-\w+|href|src)="([^"]*\$\{[^"]*)"'
    )
    # `key` is the loop variable over the literal plan list [free, pro, studio].
    static_idents = {"key"}
    offenders = []
    for attr, value in attr_re.findall(JS):
        for expr in re.findall(r"\$\{([^{}]*)\}", value):
            expr = expr.strip()
            if "esc(" in expr or "CSS.escape(" in expr:
                continue
            if expr.split("(")[0].split(".")[0].strip() in static_idents:
                continue
            offenders.append(f"{attr}=...${{{expr}}}")
    assert not offenders, f"unescaped attribute interpolation: {offenders}"


def test_inner_html_interpolations_are_escaped():
    """Text position is where the attribute-only check went wrong: an artifact key
    or a plan price reaching `${}` un-escaped is a stored-XSS sink."""
    trusted = _fragment_names(JS) | {
        "progressHtml", "links", "preview", "audioPlay", "share", "retry",
        "cancel", "bars", "body",
        # The engine report: every piece of it is assembled in renderSteps()/the card
        # from esc()'d labels and tf() templates over server numbers — no raw field
        # reaches the markup. Same contract as `links` above, one entry per name.
        "stepsBtn", "stepsHtml",
        # The demo-transcript chip: a translated label plus the server's reason, both
        # behind esc(). A reason string is provider text, so it must stay escaped.
        "demoAsr",
    }
    offenders = []
    for label, template in _html_templates(JS):
        for expr in re.findall(r"\$\{([^{}]*)\}", template):
            expr = expr.strip()
            if not expr or _SAFE_INTERP.match(expr):
                continue
            head = expr.split("(")[0].split(".")[0].strip()
            if head in trusted or "esc(" in expr:
                continue
            offenders.append(f"{label} -> ${{{expr}}}")
    assert not offenders, f"unescaped innerHTML interpolation: {offenders}"


def test_server_errors_never_surface_status_text():
    """statusText is empty over HTTP/2 and English over HTTP/1: both are wrong."""
    assert "resp.statusText" not in JS and "xhr.statusText" not in JS, \
        "raw status text is back in the user's toast"
    for handler in ("downloadArtifact", "playAudio"):
        assert "httpError(resp.status)" in _fn_body(handler)


def test_unknown_server_detail_cannot_throw():
    """FastAPI 422 sends detail as a list; the matcher must not call startsWith()."""
    assert "String(msg ?? " in _fn_body("localizeServerError")


def test_status_and_type_labels_fall_back_on_key_echo():
    """t() echoes the key, so `t(key) || fallback` can never reach the fallback."""
    for name in ("statusLabel", "typeLabel"):
        body = _fn_body(name)
        assert "=== key" in body, f"{name}() cannot detect a missing translation"


def test_localized_state_owns_its_label():
    """applyI18n() must not overwrite a filename or an upload percentage."""
    i18n = (STATIC / "i18n.js").read_text(encoding="utf-8")
    assert "i18nLocked" in i18n, "applyI18n overwrites JS-owned text"
    assert 'dataset.i18nLocked = "1"' in JS and "delete $(\"#drop-label\").dataset.i18nLocked" in JS


# ─── boot ordering: temporal dead zone, duplicated passes ────────────────

BOOT_CALL = 'if (window.Telegram && window.Telegram.WebApp) tgTryLogin();'
HASH_ROUTE = 'if (location.hash === "#studio") showStudio();'
_MODULE_DECL = re.compile(r"^(let|const|var)\s+([A-Za-z_$][\w$]*)")


def test_no_module_state_is_declared_below_the_boot_call():
    """A `let` below a call site is a ReferenceError at boot, not hoisted undefined.

    telegram-web-app.js loads async; when it wins the race, app.js runs with
    Telegram.WebApp already present, so the boot chain reached showStudio() →
    refreshAll() → refreshJobs() mid-file. The first version of this gate listed
    three names by hand and browser QA still found a fourth: `_fetchJobs` wrote
    `_nextCursor` before line 459 was evaluated, the ReferenceError died in the
    async chain behind an empty catch, and the studio came up blind after F5.
    So the rule is positional and total, and it is anchored on the *earliest*
    boot-capable statement: the deep link runs showStudio() straight from the hash
    too, so state below either line is unreachable at that moment.
    """
    assert BOOT_CALL in JS, "the Telegram boot call moved; update this test"
    assert HASH_ROUTE in JS, "the #studio deep link moved; update this test"
    boot_at = min(JS.index(BOOT_CALL), JS.index(HASH_ROUTE))
    below = [ln for ln in JS[boot_at:].split("\n") if _MODULE_DECL.match(ln)]
    assert not below, (
        "module state declared below the boot path is a live temporal dead zone: "
        + "; ".join(x.strip()[:48] for x in below)
    )
    for name in ("_allJobs", "_nextCursor", "jobFilter", "prevStatuses", "_jobsFetch"):
        assert f"let {name}" in JS, f"{name} vanished from app.js"
    assert JS.index("function tgTryLogin") < JS.index(BOOT_CALL)
    assert JS.index("function showStudio") < boot_at, "the deep link calls it first"


def _catch_clauses(body: str) -> list[str]:
    """Text of every `catch { ... }` block in a function body.

    Comments are stripped first: the word "catch" inside a comment is not a
    clause, and walking into one leaves no `{` to open, which sent this loop back
    to the same offset for ever.
    """
    body = _code(body)
    out, i = [], 0
    while True:
        j = body.find("catch", i)
        if j < 0:
            return out
        k = body.find("{", j)
        if k < 0:  # prose, not a clause — nothing left to walk
            return out
        depth, n = 1, k + 1
        while n < len(body) and depth:
            depth += body[n] == "{"
            depth -= body[n] == "}"
            n += 1
        out.append(body[k + 1:n - 1])
        i = n


def test_job_refresh_never_swallows_a_failure():
    """`catch {}` is what made the boot ReferenceError invisible: zero console
    messages while the list silently failed to load. Every background refresh in
    the boot chain must log the failure it decided not to show the user — logging
    something else in the function is not enough, so the check is per catch."""
    for fn in ("_fetchJobs", "loadMoreJobs", "refreshGlossary", "refreshNotif"):
        clauses = _catch_clauses(_fn_body(fn))
        assert clauses, f"{fn}() has no catch to inspect; update this gate"
        for c in clauses:
            assert re.search(r"console\.error\([^)]*\be\b", c), (
                f"{fn}() catches a failure and reports nothing: {c.strip()[:60]!r}"
            )


def test_a_stale_response_cannot_paint_the_previous_account():
    """Clearing the caches in logout() is worthless while a GET issued moments
    earlier is still running: its continuation re-rendered the departed account's
    rows into the signed-out panel. The response must be tied to the session that
    asked for it."""
    assert "let _sessionGen = 0;" in JS
    body = _fn_body("_fetchJobs")
    assert "const gen = _sessionGen;" in body, "_fetchJobs does not stamp its request"
    stamp = body.index("const gen = _sessionGen;")
    check = re.search(r"if \(gen !== _sessionGen \|\| !token\) return;", body)
    assert check, "_fetchJobs paints whatever the server returned, whenever it returns"
    assert stamp < check.start() < body.index("renderJobs("), (
        "the session check must sit between the request and the repaint"
    )
    for fn in ("adopt", "logout"):
        assert "_sessionGen++" in _fn_body(fn), f"{fn}() does not retire in-flight reads"


def test_adopted_job_respects_the_active_filter():
    """The keepPages merge replaces rows but never removes them, so an adopted row
    outside the active filter hung on screen for the whole life of the job — but
    dropping it silently was worse: the panel stayed blank and the completion toast
    lost the previous status it compares against. Order and side effects matter, so
    the guard is located, not just detected."""
    body = _code(_fn_body("adoptJob"))
    guard = re.search(r"if \(jobFilter && job\.status !== jobFilter\) \{", body)
    assert guard, "adoptJob has no status-filter guard (or changed shape)"
    assert body.index("_allJobs = [job") > guard.start(), (
        "the row is inserted before the filter is consulted"
    )
    block = body[guard.end():body.index("return;", guard.end())]
    assert "prevStatuses[job.id] = job.status;" in block, (
        "an out-of-filter job is never seeded, so its completion is never announced"
    )
    assert "renderJobs(_allJobs)" in block, "the blank panel is never repainted"
    adopt = _fn_body("adopt")
    for name in ("_allFetch", "_jobsFetch", "_jobsQueued"):
        assert f"{name} = null" in adopt, f"adopt() leaves {name} owned by the old session"


def test_a_settled_job_does_not_keep_a_frozen_progress_bar():
    """_progressCache exists to survive a re-render mid-flight, not to outrank
    server truth: a done job frozen at 40% claimed the pipeline stopped there."""
    body = _fn_body("pruneProgress")
    assert "isActiveJob(j)" in body and "delete _progressCache" in body
    # Every path that paints the list has to prune, polling included.
    for fn in ("_fetchJobs", "loadMoreJobs", "ensurePolling"):
        assert "pruneProgress(" in _fn_body(fn), f"{fn}() paints stale progress"
    prog = re.search(r"const prog = (.*?);\n", _code(_fn_body("renderJobs")))
    assert prog, "renderJobs no longer resolves progress in one expression"
    expr = prog.group(1)
    assert expr.startswith("isActiveJob(j) ?") and "_progressCache" not in expr.split(":")[-1], (
        f"a terminal job can still draw a bar: {expr}"
    )


def test_a_failed_login_does_not_end_the_session():
    """api() used to logout() on any 401, so a wrong password printed "session
    expired" and reset the form the visitor was still typing in. The endpoints are
    asserted as a set: they also appear as call sites elsewhere in the file, so a
    substring check passes even after one is removed from the whitelist."""
    body = _fn_body("api")
    assert "resp.status === 401 && !isAuthCall(path)" in body, (
        "a 401 from the login/register endpoints must not be read as an expired session"
    )
    m = re.search(r"const AUTH_ENDPOINTS = \[(.*?)\];", JS, re.S)
    assert m, "AUTH_ENDPOINTS changed shape; update this test"
    listed = {x.strip().strip('"\'') for x in m.group(1).split(",") if x.strip()}
    assert listed == {"/api/auth/login", "/api/auth/register", "/api/auth/telegram"}, (
        f"session-creating endpoints drifted: {sorted(listed)}"
    )


def test_boot_refresh_pass_runs_once():
    """An authorized #studio load reached refreshAll() twice — hash route and
    tryLogin — costing eight requests where five belonged, with the list rendering
    only from the second pass."""
    body = _code(_fn_body("refreshAll"))
    assert "if (_allFetch) return _allFetch;" in body, "refreshAll does not dedupe passes"
    for call in ("refreshMe()", "refreshJobs()", "refreshGlossary()", "refreshNotif()"):
        assert body.count(call) == 1, f"refreshAll pulls {call} more than once"
    assert "_allFetch = null" in _fn_body("adopt"), (
        "a pass started under the previous session would be reused after login"
    )
    # The in-flight guard alone is not enough: the second entry arrives after the
    # first pass already resolved, so the transition itself must be the trigger.
    nav = _code(_fn_body("showStudio"))
    assert 'const entering = $("#studio").classList.contains("hidden");' in nav
    assert "if (entering) refreshAll();" in nav, (
        "re-entering an already visible studio refetches everything"
    )


def test_a_filtered_empty_list_does_not_deny_the_account_has_jobs():
    """With a status filter on and nothing matching, the panel said "No jobs yet.
    Drop a file above" — browser QA called it misleading copy, because the account
    is full of jobs and the selection is empty. The list also flashes a skeleton
    the filter would immediately drop, and the filter survived sign-out."""
    body = _code(_fn_body("renderJobs"))
    assert "!jobs.length && !!jobFilter" in body, "renderJobs ignores the active filter"
    assert "empty.children[0].hidden" in body and "empty.children[1].hidden" in body, (
        "the two empty states are not switched separately"
    )
    assert '$("#jobs-clear-filter").hidden' in body
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'data-i18n="jobs_empty_filter" hidden' in html, "no filter-specific empty state"
    assert 'id="jobs-clear-filter"' in html
    # The escape hatch must go through the pill, not reimplement filter state.
    clear = re.search(r'\$\("#jobs-clear-filter"\)\.addEventListener\("click",(.*?)\);\n',
                      JS, re.S)
    assert clear and '.filter-pill[data-filter=""]' in clear.group(1), (
        "the clear-filter handler sets jobFilter itself and can drift from the pills"
    )
    lo = _code(_fn_body("logout"))
    assert 'jobFilter = ""' in lo, "sign-out leaves the next visitor inside a filter"
    start = JS.index('$("#job-form").addEventListener("submit"')
    tail = JS[start:JS.index("function uploadJob")]
    assert re.search(r"if \(!jobFilter\) \{\n\s+\$\(\"#jobs-list\"\)\.prepend\(skel\)", tail), (
        "the submission skeleton is prepended into a filtered list"
    )


def _classes_setting_display(css: str) -> set[str]:
    """Every class name whose author rules force a display value other than none.

    A hard-coded display on a class beats the user-agent [hidden]{display:none},
    which is how a "hidden" button stayed visible and clickable (browser QA
    DEFECT-1). The demand is derived from the markup, so a new hidden control in a
    styled component requires this gate to keep working, not just to keep passing.
    """
    out = set()
    for sel, block in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if re.search(r"display:\s*(?!none\b)[\w-]+", block):
            out.update(re.findall(r"\.([A-Za-z][\w-]*)", sel))
    return out


def test_the_hidden_attribute_really_hides_the_controls_that_use_it():
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    forced = _classes_setting_display(css)
    at_risk = []
    for attrs in re.findall(r"<[a-zA-Z][^>]*?\bhidden\b[^>]*>", html):
        classes = re.findall(r"\.?([A-Za-z][\w-]*)",
                             ("." + re.search(r'class="([^"]+)"', attrs).group(1))
                             if 'class="' in attrs else "")
        if forced.intersection(classes):
            at_risk.append(attrs.strip())
    # JS also flips the property, which only reflects the same attribute.
    for elem in re.findall(r'\$\("([^"]+)"\)\.hidden =', JS):
        tag = re.search(r'id="%s"[^>]*>' % re.escape(elem.lstrip("#")), html)
        if tag and forced.intersection(
                re.findall(r"\.([A-Za-z][\w-]*)",
                           "".join(re.search(r'class="([^"]+)"', tag.group(0)).groups())
                           if 'class="' in tag.group(0) else "")):
            at_risk.append(elem)
    if at_risk:
        assert re.search(r"\[hidden\][^{]*\{[^}]*display:\s*none\s*!important", css), (
            "these controls carry a hard-coded display, so the hidden attribute "
            f"does not hide them and [hidden]{{display:none!important}} is required: {at_risk}"
        )


def test_a_hidden_job_still_gets_named_when_it_finishes():
    """Under a status filter the finished job is absent from _allJobs, and the
    aria-live announcement degraded to a bare "\u2014 Job completed!" (browser QA
    DEFECT-2). The type must therefore be remembered on sight, before the filter
    gets a chance to drop the row."""
    getter = _code(_fn_body("_jobTypeOf"))
    assert "_typeCache" in getter, "_jobTypeOf only knows about rows that are on screen"
    assert getter.index("_allJobs") < getter.index("_typeCache"), (
        "the cache must be the fallback, not the authority: a live row outranks memory"
    )
    adopt = _code(_fn_body("adoptJob"))
    guard = adopt.find("if (jobFilter")
    record = adopt.find("_typeCache[job.id]")
    assert 0 <= record < guard, (
        "the type is recorded after the filter returns, so a filtered job is never remembered"
    )
    assert "_typeCache[j.id] = j.type" in _code(_fn_body("renderJobs")), (
        "painted rows do not refresh the memory, so a job only seen in the list stays nameless"
    )


def test_signing_out_repaints_the_list_through_the_painter():
    """logout() emptied #jobs-list by hand and reset jobFilter, but the empty-state
    bookkeeping lives in renderJobs, so a signed-out visitor kept staring at "No job
    matches this filter" and a clickable "Show all" that could not show anything."""
    body = _code(_fn_body("logout"))
    assert 'jobFilter = ""' in body and "renderJobs(" in body, (
        "logout must reset the filter and repaint, not hand-clear the DOM"
    )
    assert body.index('jobFilter = ""') < body.index("renderJobs("), (
        "repainting before the filter is reset draws the filtered empty state again"
    )
    assert '$("#jobs-list").innerHTML = ""' not in body, (
        "logout clears the list outside the painter, so the empty states can drift"
    )


def test_the_visible_toast_and_the_live_region_carry_one_sentence():
    """The completion sentence was built twice with different content: the toast said
    "Job completed!" while aria-live said "Document — Job completed!", which is
    ambiguous with two jobs in flight and useless to a screen reader with one."""
    helper = _code(_fn_body("announceTerminal"))
    assert 'toast(' in helper and 'aria-announce' in helper, (
        "announceTerminal must own both live regions, not one"
    )
    assert re.search(r"label \? label \+ \" — \" \+ sentence : sentence", helper), (
        "an unknown job type reintroduces the dangling dash in the announcement"
    )
    for fn in ("_handleWSEvent", "ensurePolling"):
        body = _code(_fn_body(fn))
        assert "announceTerminal(" in body, f"{fn} builds its own completion sentence"
        assert 't("job_done_toast")' not in body, f"{fn} still writes the toast text inline"


def test_pipeline_failure_reasons_are_all_localized():
    """job.error is machine prose shown inside a customer-facing row.

    Every string the backend can write there needs a prefix in the map, and a
    duplicated key would silently shadow the first one — JavaScript object literals
    keep the last value, so the defect is invisible at runtime."""
    m = re.search(r"const SERVER_ERROR_MAP = \{(.*?)\n\};", JS, re.S)
    assert m, "SERVER_ERROR_MAP changed shape; update this test"
    keys = re.findall(r"^\s*'([^']+)':", m.group(1), re.M)
    assert len(keys) == len(set(keys)), (
        f"duplicate prefix shadows a mapping: {sorted(k for k in keys if keys.count(k) > 1)}"
    )
    # What the pipeline and the restart-recovery actually store in job.error.
    for prefix in ("job exceeded", "worker restart"):
        assert prefix in keys, f"{prefix!r} can reach the UI untranslated"
    assert 'error="timeout"' not in PIPELINE, (
        "the wall-clock branch must write the same sentence as the checkpoint"
    )
    for value in set(re.findall(r"^\s*'[^']+':\s*'([a-z_]+)'", m.group(1), re.M)):
        assert f"{value}:" in I18N, f"SERVER_ERROR_MAP points at a missing key: {value}"


def test_submitted_job_is_drawn_from_the_response():
    """The POST already says what the row is.

    Refetching the list to learn it cost a GET and, worse, left the live channel
    with no `data-jid` to patch: every queued/start/asr frame missed, triggered
    another refetch, and the user ended up seeing only the final state.
    """
    start = JS.index('$("#job-form").addEventListener("submit"')
    tail = JS[start:]
    ok_block = tail[tail.index("try {"):tail.index("} catch (e) {")]
    assert "adoptJob(" in ok_block, "the created job is never drawn from the response"
    assert "refreshJobs()" not in _code(ok_block), "submit refetches what it just created"
    body = _fn_body("adoptJob")
    for needed in ("_allJobs = [job", "prevStatuses[job.id]", "renderJobs(", "renderLoadMore("):
        assert needed in body, f"adoptJob lost {needed}"


def test_money_facing_numbers_are_not_raw_floats():
    """`Balans: 4.430000000000001 min` is binary float noise printed next to the
    customer's money — browser QA filed it as a money-trust defect."""
    assert "fmtMinutes(_me.balance_minutes)" in _fn_body("paintMe")
    meta = re.search(r'class="job-meta"[^\n]*', JS)
    assert meta and "fmtMinutes(j.minutes)" in meta.group(0), "job rows print raw minutes"
    m = re.search(r"const fmtMinutes = \(n\) => \{(.*?)\n\};", JS, re.S)
    assert m, "fmtMinutes changed shape; update this test"
    assert "Number.isFinite" in m.group(1), "fmtMinutes must survive null/NaN from the API"


# ─── PWA ─────────────────────────────────────────────────────────────────────

def test_service_worker_cache_matches_release_version():
    """A stale shell cache makes QA (and users) see yesterday's UI."""
    m = re.search(r"const BUILD = '([\d.]+)';", SW)
    assert m, "sw.js build stamp not found"
    assert "const VERSION = 'ovoz-v' + BUILD;" in SW, (
        "sw.js cache name must be derived from BUILD, or the two can drift apart"
    )
    assert m.group(1) == app_pkg.__version__, (
        f"bump sw.js BUILD with the app version: sw={m.group(1)} app={app_pkg.__version__}"
    )


def test_code_assets_are_build_stamped_in_the_document():
    """A deploy must change the cache key, not only the caching policy.

    After sw.js went network-first, browser QA's first hard-reload still served
    the previous release's i18n.js next to a current app.js: the service worker
    that was active at that moment had code cached under the bare path, and it is
    the *old* worker that answers the *transition* navigation. ?v=BUILD makes
    yesterday's entries unaddressable instead of merely disfavoured.
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    ver = re.escape(app_pkg.__version__)
    for asset in ("styles.css", "i18n.js", "app.js", "tg-boot.js", "sw-boot.js"):
        assert re.search(rf"""{asset}\?v={ver}["']""", html), (
            f"{asset} is not build-stamped in index.html"
        )
    m = re.search(r"const BUILD = '([\d.]+)';", SW)
    assert m and m.group(1) == app_pkg.__version__, "sw.js BUILD must equal app version"


def test_service_worker_answers_a_build_question_and_the_page_asks_it():
    """Announcing on activate is only half a protocol.

    A tab that boots on top of an already-active worker of another build never sees
    an activate event, and a tab that is never navigated never asks the network
    either — browser QA lived through exactly that: the server had shipped a new
    release, the tab still executed the old one, and nothing fixed it until a human
    reloaded. So the page must ask, and the worker must answer; a one-way "we notify
    you" is the bug, not the mitigation."""
    boot = (STATIC / "sw-boot.js").read_text(encoding="utf-8")
    assert '"whatBuild"' in boot, "the page never asks which build owns it"
    assert "reg.update" in boot, "a background tab is never told to re-check"
    assert "adoptBuild" in boot, "the reload decision is not shared by both paths"
    assert "function adoptBuild" in boot, "the reload decision is not shared by both paths"
    assert "sessionStorage" in boot, "a mismatching build must not reload in a loop"
    assert "'whatBuild'" in SW, "the worker does not answer the page's question"
    assert "'build:' + BUILD" in SW, "the answer must carry the worker's own build"


def test_landing_deep_links_do_not_land_in_a_hidden_page():
    """`#ling` shared while the studio is open is a real URL a person will use.

    Every target section lives on the landing page, which is `display:none` at that
    moment: with no branch for it the visitor got a studio and a changed address bar,
    which reads as a broken link on a marketing page."""
    body = JS[JS.index('addEventListener("hashchange"'):]
    body = body[:body.index("\n", body.index("});"))]
    assert 'h !== "#studio"' in body, "a landing deep link is ignored in the studio"
    assert "showLanding()" in body and "scrollIntoView" in body


def test_every_artifact_kind_keeps_its_real_file_extension():
    """A chip that downloads `align-<id>.txt` for a JSON report is a file a person
    cannot open in a player or tell from a transcript.

    The server names the file (`Content-Disposition`), and the client map is the
    fallback — derived from the server's own tuple, so a kind added there without an
    extension fails here instead of shipping as a `.txt`."""
    from app.main import _JOB_ARTIFACT_KINDS
    m = re.search(r"const DL_EXT = \{(.*?)\n\};", JS, re.S)
    assert m, "the artifact extension map is gone"
    table = dict(re.findall(r"(\w+):\s*" + '"([.][a-z0-9]+)"', m.group(1)))
    missing = set(_JOB_ARTIFACT_KINDS) - set(table)
    assert not missing, f"artifact kinds with no extension: {sorted(missing)}"
    for kind in ("align", "words", "diarization", "layout"):
        assert table.get(kind) == ".json", f"{kind} is JSON, mapped as {table.get(kind)}"
    assert "dlName(" in JS and 'resp.headers.get("Content-Disposition")' in JS, \
        "the server's own filename is being ignored"


def test_the_preview_is_built_from_the_engines_not_a_second_parser():
    """One interpretation of a subtitle file.

    The player used to re-parse SRT timecodes in JavaScript while the engine parsed
    the same file in Python; two parsers of one format drift by a frame, and the
    drift shows up as a highlight that leads the voice — the exact thing Ovoz So'z
    is sold to prevent. Word timings now come from `/caption`, assembled by the
    same module that measured them."""
    assert "-->" not in JS, "a second SRT parser is back in the browser"
    assert "parseSrtClient" not in JS, "the client-side parser is still wired"
    body = JS[JS.index("async function openPlayer"):JS.index("function showMedia")]
    assert "/caption" in body and "/media" in body, "the preview asks for neither"
    assert '_pstats' in body, "word and voice counts are recomputed per frame"
    stats = JS[JS.index("function paintStats"):JS.index("function paintWord")]
    assert "_pstats.total" in stats, "the stats line counts a card without a total"


def test_the_player_marks_up_both_media_kinds_and_repaints_on_language():
    """A subtitle job is usually an audio job: a black `<video>` box for a voice
    recording is the product telling the user it does not know what it just
    processed. And a preview whose generated lines stay in yesterday's language is
    the same bug as an unpainted log line."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    block = html[html.index('<dialog id="player-dlg"'):html.index("</dialog>",
                                                                    html.index('id="player-dlg"'))]
    for ident in ('id="p-video"', 'id="p-audio"', 'id="p-speaker"', 'id="p-meta"',
                  'id="p-demo"'):
        assert ident in block, f"the player markup lost {ident}"
    assert block.count('hidden') >= 3, "elements are visible at once by default"
    assert 'accept="video/*,audio/*"' in block, "a tape cannot be picked by hand"
    # the language switch has to be inside the dialog: the app-level one sits under
    # the modal layer, so a visitor who wants the preview in their language cannot
    # reach it without closing what they were looking at
    assert 'class="lang-switch"' in block, "no language switch inside the preview"
    assert ' on' not in block and 'style=' not in block, "inline handler or style"
    assert 'window.__playerRepaint' in JS, "the preview outlives a language switch"
    assert "__playerRepaint?.()" in JS, "the repaint is not wired into the switch"
    assert 'classList.toggle("on"' in JS, "word highlight is not driven by the engine"
    assert '"S" + c.speaker' in JS and 'c.speaker + 1' not in JS, \
        "the speaker chip invents a speaker: diarization counts from 1"
    stop = JS[JS.index("function stopPlayer"):JS.index("function stopPlayer") + 300]
    assert "hideMedia()" in stop, "closing the preview does not stop the tape"
    wiring = JS[JS.index('$("#player-dlg").addEventListener'):
                JS.index("function stopPlayer")]
    assert '"close", stopPlayer' in wiring, "the tape outlives the close event"
    assert '"cancel", stopPlayer' in wiring, "Esc leaves the tape playing"
    assert "stopPlayer();" in JS[JS.index('$("#player-close")'):-1] or \
        "stopPlayer();" in wiring, "the close button does not stop the tape"


def test_the_preview_routes_are_owner_only_and_never_name_a_path():
    """`/media` is the customer's raw upload: the ownership check is the whole
    privacy story of this feature, and an error that echoes a server path turns a
    404 into a map of the filesystem."""
    src = (STATIC.parent / "app" / "main.py").read_text(encoding="utf-8")
    for fn in ("def job_caption(", "def job_media("):
        body = src[src.index(fn):src.index("\n@app.", src.index(fn))]
        assert 'job["user_id"] != user["id"]' in body, f"{fn} has no owner check"
        assert 'raise HTTPException(404, "Job not found")' in body
    media = src[src.index("def job_media("):]
    media = media[:media.index("\n\n# ")]
    assert "filename=" not in media, "media is served as an attachment download"
    assert '410' in media, "a purged source must not look like a missing job"


def test_the_live_probes_survive_being_told_no():
    """A probe that crashes on a refusal reports the product as broken.

    The listening routes answer 429 *before* reading the tape, which is the whole
    point of the early refusal; on Windows the client then sees the connection reset
    while it is still reading the error body, and `e.read()` raised out of a probe
    made a green server look red — twice from the same batch, once from an
    identical rerun seconds later. A refusal is an answer, so reading it is
    best-effort everywhere a probe handles HTTPError.
    """
    scripts = (STATIC.parent / "scripts").glob("probe_*_live.py")
    bad = []
    for path in scripts:
        src = path.read_text(encoding="utf-8")
        for block in re.findall(r"except urllib\.error\.HTTPError as \w+:\n((?:[ \t]+.*\n)+)",
                                src):
            if re.search(r"= e\.read\(\)|return e\.code, e\.read\(\)", block) \
                    and "try:" not in block:
                bad.append("%s: %s" % (path.name, block.strip().splitlines()[0][:60]))
    assert not bad, f"a probe reads a refusal body without surviving a reset: {bad}"


def test_document_carries_no_executable_inline_script():
    """The CSP is only as strong as the document that lets it stay strict.

    script-src drops 'unsafe-inline' in this release, which pays off only while
    nobody adds `<script>...</script>` or an on* attribute back: the moment one
    appears, either the page breaks in production (a silent boot failure QA sees
    too late) or someone re-loosens the header. This gate makes the first option
    the loud one, in the developer's own terminal. JSON-LD is data, not script,
    and is exempt for that reason.
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for tag in re.findall(r"<script\b[^>]*>", html):
        assert "src=" in tag or 'type="application/ld+json"' in tag, (
            f"inline <script> in the document defeats the strict CSP: {tag}"
        )
    assert not re.search(r"\son\w+\s*=\s*[\"']", html), \
        "an on* handler attribute needs 'unsafe-inline' and silently kills the XSS guard"


def test_service_worker_offers_every_asset_the_document_loads():
    """A code file the page asks for must be in the offline shell too.

    tg-boot.js and sw-boot.js were new files: index.html referenced them, the
    document cached itself, and an offline visitor would have got the shell with
    two 503s where the Telegram handshake and the worker registration live.
    Deriving the list from the document means the next extracted script cannot
    be forgotten the way a hand-written list gets forgotten.
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    wanted = {"/" + m + ".js" for m in re.findall(r'<script src="([\w.-]+?)\.js', html)}
    wanted.add("/styles.css")
    code_paths = set(re.findall(r"'(\/[\w.-]+)'",
                                re.search(r"CODE_PATHS = \[(.*?)\]", SW, re.S).group(1)))
    precached = set(re.findall(r"`(/[\w.-]+\.(?:js|css))\?v=", SW)) | {"/manifest.webmanifest"}
    for asset in sorted(wanted):
        assert asset in code_paths, (
            f"{asset} is loaded by the page but not in CODE_PATHS, so the shell "
            f"policy never covers it")
        assert asset in precached, f"{asset} is loaded by the page but never precached"


def test_a_new_build_adopts_a_page_running_the_old_one():
    """sw-boot listened for a reload message nothing ever sent.

    The listener promising "the worker announces a new build" sat dead for two
    releases: no postMessage existed anywhere in sw.js. The protocol is now real
    and one-sided-safe — the worker announces BUILD on activate, the page compares
    it with the ?v= stamp it was actually served with, and only reloads on a real
    mismatch, once per session. A first visit must never be reloaded, and a stale
    document beside a fresh worker must never be reloaded twice.
    """
    boot = (STATIC / "sw-boot.js").read_text(encoding="utf-8")
    assert "postMessage('build:' + BUILD)" in SW, \
        "the worker never announces the build, so the page's reload listener is dead code"
    assert ".postMessage('build:' + BUILD).catch" not in SW, \
        "postMessage returns undefined: chaining .catch on it throws on activate"
    assert "matchAll({ type: 'window' })" in SW, "the announcement must reach open pages"
    assert "build:" in boot and "SERVED_BUILD" in boot, \
        "the page must compare the announced build with the one it was served"
    assert "live === SERVED_BUILD" in boot, \
        "reloading on every activation would reload a first-time visitor too"
    assert "sessionStorage" in boot, \
        "without a one-shot guard a stale document plus a fresh worker is a reload loop"
    assert 'if (e.data === "reload")' not in boot, \
        "the bare broadcast is replaced by the build protocol"


def test_every_server_step_has_a_localised_label():
    """The live progress line composes its text from `ps_<step>`, and `stepLabel()`
    falls back to the raw token. `align`, `polish` and `words` shipped as pipeline
    steps without ever being added to the dictionaries, so a UZ customer watched a
    job say "align" in an English-only alphabet of one."""
    from test_i18n_completeness import dictionaries
    main_src = (STATIC.parent / "app" / "main.py").read_text(encoding="utf-8")
    table = re.search(r"_STEP_PCT = \{(.*?)\}", main_src, re.S).group(1)
    steps = set(re.findall(r'"(\w+)":\s*\d+', table))
    assert {"align", "words", "polish", "diarize"} <= steps, steps
    for lang, keys in dictionaries().items():
        missing = {"ps_" + s for s in steps} - set(keys)
        assert not missing, f"{lang} has no label for the server's steps: {sorted(missing)}"


def test_a_refused_step_can_be_explained_in_every_language():
    """`skipped` is only half an answer: the client maps `reason` to a sentence.
    Every reason the pipeline writes by hand must have a key, and the reasons it
    forwards from an engine (codes it cannot enumerate here) must land on a
    generic template instead of an empty row."""
    from test_i18n_completeness import dictionaries
    pipe = (STATIC.parent / "app" / "pipeline.py").read_text(encoding="utf-8")
    reasons = set(re.findall(r'"reason":\s*"([a-z_]+)"', pipe))
    assert {"no_audio", "not_subtitles"} <= reasons, reasons
    for lang, keys in dictionaries().items():
        missing = {"st_skip_" + r for r in reasons} - set(keys)
        assert not missing, f"{lang} cannot explain these refusals: {sorted(missing)}"
        assert "st_skip_generic" in keys, f"{lang} has no fallback for an engine code"


def test_every_artifact_kind_has_a_customer_facing_label():
    """The chip row on a job card is the customer's file list, and `align`, `words`
    and `ass_karaoke` were rendering their database names verbatim: internal
    vocabulary on a paid screen (the same class as an unpainted log line).

    Derived from the server's own tuple, so a kind added there without a label
    fails here instead of shipping."""
    from app.main import _JOB_ARTIFACT_KINDS
    body = JS[JS.index("function kindLabel(k)"):
              JS.index("function kindLabel(k)") + 900]
    labelled = set(re.findall(r"(\w+):\s*\"", body))
    missing = set(_JOB_ARTIFACT_KINDS) - labelled
    assert not missing, f"artifact kinds with no label: {sorted(missing)}"


def test_the_engine_report_is_rendered_from_data_and_not_from_the_log_line():
    """`message` is written for a developer reading logs in English. Painting it is
    the bug class this app already fixed for errors, and the disclosure would be
    worthless on a finished card — which is where the customer decides whether the
    option they paid for did anything."""
    body = JS[JS.index("function stepLines(ev)"):JS.index("function renderSteps(")]
    assert "ev.data" in body and "d.code" in body
    assert "message" not in body, "the log line is being localised by accident"
    card = JS[JS.index("function renderJobs(jobs)"):JS.index("function kindLabel(")]
    assert 'data-steps=' in card and 'aria-expanded=' in card
    assert "j.options" in card, "the toggle appears where nothing was asked for"
    assert "_stepsOpen.has(j.id)" in card, "open state must survive the next repaint"
    assert "again.focus()" in JS, "repainting the card drops keyboard focus"
    main_src = (STATIC.parent / "app" / "main.py").read_text(encoding="utf-8")
    assert main_src.count("_job_options(") >= 3, \
        "list and detail must offer the same options as one helper"


def test_every_engine_code_the_pipeline_writes_is_rendered_and_translated():
    """`skipped` has a fallback template; a success code has nothing.

    `stepLines` returns null for a code it does not know, which renders no row at
    all — so the day the pipeline starts writing a number without a client branch,
    the paid option silently disappears from the report instead of failing. Derived
    from both sides' own text: the codes the server writes, the branches the client
    has, the keys all three locales carry."""
    from test_i18n_completeness import dictionaries
    pipe = (STATIC.parent / "app" / "pipeline.py").read_text(encoding="utf-8")
    codes = set(re.findall(r'"code":\s*"([a-z_]+)"', pipe)) - {"skipped"}
    # A code picked by a conditional is two codes, and a scan that sees only the
    # first half would let the other half ship without a row.
    for other in re.findall(r'"code":\s*"[a-z_]+"[^,}]*else "([a-z_]+)"', pipe):
        codes.add(other)
    assert {"aligned", "timed", "turns", "turns_text"} <= codes, codes
    body = JS[JS.index("function stepLines(ev)"):JS.index("function renderSteps(")]
    unrendered = {c for c in codes if f'd.code === "{c}"' not in body}
    assert not unrendered, f"stepLines silently drops these engine codes: {sorted(unrendered)}"
    used = set(re.findall(r'tf\("(st_[a-z_]+)"', body)) - {"st_skip_generic"}
    for lang, keys in dictionaries().items():
        missing = used - set(keys)
        assert not missing, f"{lang} cannot paint these report lines: {sorted(missing)}"


def test_the_job_listens_to_the_whole_tape_it_paid_for():
    """The window that refuses a tape is the one an anonymous caller may not push
    past. A job has already paid, been throttled and been size-checked, so routing
    it through the single-shot entry would let it hit `too_long` — the refusal that
    quietly drops both paid listening options.

    Textual because the case cannot be raised in a test double: it needs a real
    tape longer than fifteen minutes, and the bug it prevents is a job that finishes
    `done` with two steps skipped for a limit the customer never agreed to."""
    pipe = (STATIC.parent / "app" / "pipeline.py").read_text(encoding="utf-8")
    assert "align_mod.align_curve(" in pipe and "word_mod.words_curve(" in pipe
    for single_shot in ("align_mod.align(", "word_mod.words("):
        assert single_shot not in pipe, f"the job re-meets the anonymous window: {single_shot}"
    assert "check_listen_window" not in pipe, \
        "the pipeline is refusing its own customers"


def test_the_cut_ruler_is_wired_to_the_sixth_engine():
    """The widget is the only place this law becomes visible to a person who will
    never read the API. Everything the ruler paints must come from codes and
    numbers; a server sentence on this surface is the bug class v0.17 removed."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for node in ('id="nafis"', 'id="nafis-run"', 'id="nf-ruler"', 'id="nf-list"',
                 'id="nafis-metrics"', 'id="nf-hint"', 'data-i18n-aria="nf_aria"',
                 'aria-live="polite" aria-atomic="true"'):
        assert node in html, f"the cut ruler markup lost {node}"
    body = JS[JS.index('const btn = $("#nafis-run")'):JS.index("// ─── auth ───")]
    assert 'window.__nafisRepaint' in body, "a locale switch would freeze the ruler"
    assert "() => window.__nafisRepaint?.()," in JS, "the repaint is not in the painter list"
    assert '"/api/v1/ling/nafis"' in body
    assert body.count("await api(") == 1, "one request per click, not a poll"
    assert "answer.ruler" in body and "legal_share" in body
    # `e.message` (the client's own failure) is fine; a sentence from the engine is not.
    for forbidden in (".detail", "answer.message", "answer.score", "r[\"detail\"]"):
        assert forbidden not in body, f"the ruler is painting server prose: {forbidden}"
    assert "esc(" in body, "the demo text reaches innerHTML unescaped"


def test_every_engine_law_has_a_name_in_three_languages():
    """A code without a translation shows the customer `compound_verb` in Latin
    script. The list is read from the engine, so a sixth law cannot ship nameless."""
    from app.ling import nafis
    from app.ling.srt import Cue
    from test_i18n_completeness import dictionaries
    laws = set(nafis.analyze([Cue(1, 0.0, 1.0, "jamoaga ishlash uchun kelib berdi")])["laws"])
    assert laws == {l.__name__.split("_", 2)[2] for l in nafis.LAWS}
    for lang, keys in dictionaries().items():
        missing = {"nf_law_" + c for c in laws} - set(keys)
        assert not missing, f"{lang} cannot name these laws: {sorted(missing)}"


def test_the_demo_transcript_chip_comes_from_the_engine_not_the_string():
    """Spotting invented text by grepping a `[демо]` prefix out of a subtitle file
    breaks the moment a real transcript contains that word, and it is still a guess
    about somebody's content. The disclosure is a fact the pipeline recorded — and
    the reason with it, in the tooltip."""
    card = JS[JS.index("function renderJobs(jobs)"):JS.index("function kindLabel(")]
    assert "j.engines" in card and "asr_demo" in card
    assert 'class="mode-chip"' in card and 'title="${esc(j.engines.asr_reason' in card
    # the marker may be discussed in a comment; it may not be matched against text
    code = "\n".join(ln for ln in card.splitlines()
                     if not ln.strip().startswith("//"))
    assert "[\u0434\u0435\u043c\u043e]" not in code, "the UI is grepping a demo marker out of file text"
    quotes = "\"'`"
    assert not re.search(r"(includes|startsWith|indexOf|match|search)\(\s*[" + quotes
                         + r"]\[", code), \
        "detection of invented text must not read the customer's content"
    # and the chip's own label is translated, like every other word on the card
    assert 't("asr_demo_chip")' in card


def test_telegram_says_ready_before_anything_can_skip_it():
    """A Mini App stays in the host's loading state until `ready()`. It used to live
    under the token early-return, so the most common visitor — already signed in —
    was the one the host never heard from."""
    body = JS[JS.index("function tgTryLogin()"):JS.index("// ─── wallet + balance")]
    assert body.index("tg.ready()") < body.index("if (token) {"), "ready() is after a return path"
    assert body.index("tg.ready()") < body.index("tg.initData"), "ready() gated on initData"
    assert "tgGoBack" in body, "the back button is claimed only in some branches"
    # a host that is not Telegram, or an older SDK, must not break the boot
    assert body.count("try {") >= 3


def test_the_back_button_sees_every_layer_the_user_can_be_on():
    """Dialog, studio, landing: system back must unwind one layer at a time, and the
    stack must be the dialog element's own state — not a second bookkeeping that can
    drift when somebody adds a dialog and forgets to announce it."""
    at = JS.index('document.addEventListener("toggle"')
    handler = JS[at:JS.index("}, true);", at)]
    assert '"DIALOG"' in handler and "_openDialogs" in handler and "tgSyncBack()" in handler
    assert "splice" in handler and "push" in handler
    # Mini App APIs are claimed only inside a Mini App: the vendor SDK loads in a
    # plain browser tab too and answers every unsupported call with a console warn.
    guard = JS[JS.index("function tgInsideApp()"):JS.index("function tgSyncBack()")]
    assert 'tg.platform !== "web"' in guard and "tg.initData" in guard
    sync = JS[JS.index("function tgSyncBack()"):JS.index("function tgGoBack()")]
    assert "tgInsideApp()" in sync, "the button is claimed outside Telegram too"
    hb = JS[JS.index("function haptic(style)"):JS.index("\n}\n", JS.index("function haptic(style)"))]
    assert "tgInsideApp()" in hb, "haptics fire in a browser that has no motor"
    go_back = JS[JS.index("function tgGoBack()"):at]
    assert ".close()" in go_back and "splice" not in go_back, "the stack is owned by toggle"
    assert "showLanding()" in go_back
    for fn in ("function showStudio()", "function showLanding()"):
        end = JS.index("\n}", JS.index(fn))
        assert "tgSyncBack()" in JS[JS.index(fn):end], f"{fn} moves without the button"


def test_feedback_haptics_hang_off_the_single_toast_funnel():
    body = JS[JS.index("function toast(msg"):JS.index("\n}", JS.index("function toast(msg"))]
    assert "haptic(isErr ? \"error\" : \"success\")" in body
    hb = JS[JS.index("function haptic(style)"):JS.index("\n}\n", JS.index("function haptic(style)"))]
    assert "HapticFeedback" in hb and "catch" in hb, "no SDK, no exception on the phone"
    assert JS.count("notificationOccurred") == 2


def test_a_returning_tab_refreshes_instead_of_showing_a_stale_job():
    """A hidden tab throttles its timers to about one a minute, and a Telegram Mini
    App is hidden every time the user opens the chat list — so "the poller will fix
    it" is not a plan for the surface where this product actually lives.

    Verified live: a job that finished while the tab was backgrounded still read
    "queued" on the card while the API already answered `done`.
    """
    at = JS.index('document.addEventListener("visibilitychange"')
    # `\n});` and not `});`: the first closing of the callback is inside
    # `refreshAll().catch(() => {})`, and a gate that reads half a handler agrees
    # with whatever the other half happens to contain.
    handler = JS[at:JS.index("\n});", at)]
    assert "if (document.hidden || !token) return;" in handler, "must not fire when leaving"
    assert "refreshAll()" in handler, "the coalesced pass, not a pile of fetches"
    assert "_connectWS()" in handler, "a socket that died with the tab must be redialed"
    assert "fetch(" not in handler
    assert 'addEventListener("pageshow"' in JS, "restore from bfcache is the same stale state"


def test_service_worker_never_answers_code_from_cache():
    """Cache is the offline escape hatch, not a response strategy for code.

    Browser QA caught a returning visitor served the previous build: the old
    app.js did not understand the current progress steps, so a finished job sat
    on "Ishlayapti" forever and printed raw provider prose. Bumping VERSION
    alone does not help while the SW answers from the old cache first.
    """
    strategies = set(re.findall(r"event\.respondWith\((\w+)\(", SW))
    assert strategies == {"networkFirst"}, (
        f"a request served cache-first can ship a stale client: {sorted(strategies)}"
    )
    assert "staleWhileRevalidate" not in SW
    # The build stamp changes the cache key every release, so something has to
    # delete the old ones or the shell cache grows a copy per deploy.
    assert "pruneAndPut" in SW and "cache.keys()" in SW, \
        "nothing prunes superseded build keys from the shell cache"


# ─── request coalescing ─────────────────────────────────────────────────────

def test_live_events_coalesce_into_one_jobs_refresh():
    """Six GETs for one job was the finding; the guard is the shape.

    Each of the five live frames a job emits called refreshJobs() on arrival, and
    none of them looked at whether a request was already running. Polling is off
    while the channel works, so nothing else would have merged them.
    """
    body = _code(_fn_body("refreshJobs"))
    assert "if (_jobsFetch)" in body, "refreshJobs does not dedupe a request in flight"
    assert "if (queued) refreshJobs(queued);" in body, \
        "a request that arrives mid-flight is dropped instead of deferred"
    assert "_fetchJobs(opts)" in body, "refreshJobs does the network work itself"
    inner = _fn_body("_fetchJobs")
    for needed in ("renderJobs(", "renderLoadMore()", "startPolling()"):
        assert needed in inner, f"the real job-list work lost {needed}"


def test_the_public_qa_studio_scroll_bug_stays_fixed():
    """29.09.2026 public browser QA over the real tunnel: pressing «open studio»
    left ~3200px of live-demo sections on screen — the hide-list predated the
    demos and nothing reset the scroll. Both navigation functions must now name
    the same section set, and entering the studio starts at the top."""
    ss = _code(_fn_body("showStudio"))
    sl = _fn_body("showLanding")
    for cls in ("turn-demo", "qator-demo", "jimlik-demo", "soz-demo", "nafis-demo"):
        assert cls in ss, f"showStudio leaves .{cls} stacked above the studio"
        assert cls in sl, f"showLanding never gives .{cls} back"
    assert "window.scrollTo(0, 0);" in ss, "the studio opens out of view again"


def test_the_header_wraps_instead_of_overflowing_on_phones():
    """QA measured the header's min-content at ~400px against a 390px viewport
    (brand + lang switch + 44px CTA). Without wrap the landing gains a horizontal
    scrollbar — the one layout sin the mobile rules had sworn off since Round 17."""
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    start = css.index("@media (max-width: 680px)")
    mobile = css[start:start + 3000]   # the block itself, comments included
    assert "flex-wrap: wrap" in mobile, ".nav must wrap at phone width"


def test_uz_is_the_default_language_for_a_fresh_visitor():
    """UZ-first product: an EN-browser visitor landing on EN is landing on the
    wrong market. Language still sticks after a manual switch."""
    assert 'localStorage.getItem("ovoz_lang") || "uz"' in I18N


def test_telegram_theme_maps_every_panel_variable():
    """User screenshot 29.09: Telegram LIGHT theme, and --surface-2 — the most
    used panel variable in the app (segments, selects, toasts, chips) — was
    never mapped, so dark #171b25 pills sat on a white page reading dark-on-dark.
    The theme pass must derive surface-2, declare color-scheme to native
    controls, and reflect the header color into theme-color for the host."""
    body = _fn_body("applyTgTheme")
    assert "'--surface-2'" in body, "surface-2 unmapped again"
    assert "colorScheme" in body, "native selects stay dark on a light page"
    assert "theme-color" in body
    assert "_tgLum" in body and "_tgMix" in body


def test_the_phone_segmented_control_is_two_by_two():
    """Four 92px-min segments overflow 342px: the 3rd tab clipped and the 4th
    fell alone (screenshot). A 2x2 grid is the readable phone shape."""
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    mobile = css[css.index("@media (max-width: 680px)"):
                 css.index("@media (max-width: 680px)") + 3000]
    assert ".seg { display: grid; grid-template-columns: 1fr 1fr; }" in mobile
    assert ".seg button { min-width: 0; }" in mobile


def test_nav_and_cta_read_the_theme_variables_with_the_old_dark_as_fallback():
    """--nav-bg/--mint-ink were dead in both directions: JS set them, CSS never
    read them. The fallback must stay the original dark, so the web build (no
    Telegram) is pixel-identical to before the theming pass."""
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert "--nav-bg: #0b0d12;" in css and "--mint-ink: #06210f;" in css
    assert "var(--nav-bg" in css, "the nav still hardcodes its dark"
    assert "color: var(--mint-ink)" in css, "the CTA text ignores button_text_color"


def test_the_mobile_tab_bar_follows_the_theme_too():
    """QA sweep caught the last dark island: .nav-links hardcoded rgba(11,13,18,.88)
    + #1c212c border — a black bar under a light page. It now mixes from
    --nav-bg/--ink with the old literals kept as pre-color-mix fallbacks."""
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    mobile = css.index("@media (max-width: 680px)")   # the desktop .nav-links has no bar
    start = css.index(".nav-links {", mobile)
    block = css[start:start + 700]
    assert "color-mix(in srgb, var(--nav-bg) 90%, transparent)" in block
    assert "color-mix(in srgb, var(--ink) 12%, transparent)" in block
    assert "rgba(11, 13, 18, .88)" in block, "fallback for engines without color-mix"


def test_hint_color_is_clamped_to_a_readable_contrast():
    """Telegram's hint #8e8e93 measured 2.89–3.26:1 under readable text in light
    theme (13 elements). The theme pass must walk an unreadable hint toward the
    text color until WCAG 4.5:1, and leave a passing dark hint untouched."""
    body = _fn_body("applyTgTheme")
    assert "_tgContrast(hint, bg) < 4.5" in body
    assert "_tgMix(hint" in body
    assert "_tgLum(tp.text_color) > _tgLum(hint)" in body, "must mix toward the text color, not away"
