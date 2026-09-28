"""i18n completeness: kills the "button renders the raw key / renders empty" bug class.

applyI18n() silently skips missing keys (`if (v) el.textContent = v`) and window.t()
falls back to echoing the key itself, so a typo'd or half-added translation is
invisible until a human opens the page in the wrong locale. These tests make the
static reference (index.html + app.js) and the dictionaries agree at CI time.
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "static"
LOCALES = ("uz", "ru", "en")

_KEY_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"((?:[^"\\]|\\.)*)"')
_START_RE = re.compile(r"(?m)^\s*(" + "|".join(LOCALES) + r")\s*:\s*\{")


def _object_body(text, brace_idx):
    """Substring inside the object literal that opens at text[brace_idx] == '{'."""
    depth, i, in_str, escaped, quote = 0, brace_idx, False, False, ""
    while i < len(text):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_str = False
        elif ch in "\"'":
            in_str, quote = True, ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[brace_idx + 1:i]
        i += 1
    raise AssertionError("unbalanced object literal in i18n.js")


def _parsed():
    """lang -> (key table, raw body text); body text lets us subtract the
    dictionaries themselves from the 'where is this key referenced' scan."""
    src = (STATIC / "i18n.js").read_text(encoding="utf-8")
    out = {}
    for m in _START_RE.finditer(src):
        body = _object_body(src, m.end() - 1)
        out[m.group(1)] = (dict(_KEY_RE.findall(body)), body)
    return out


def dictionaries():
    return {lang: table for lang, (table, _body) in _parsed().items()}


def _keys_used_in_markup():
    """data-i18n / -ph / -aria keys referenced from HTML, incl. JS-generated markup."""
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    return set(re.findall(r'data-i18n(?:-ph|-aria|-title)?="([A-Za-z0-9_]+)"', markup + app))


def _literal_t_calls():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    return set(re.findall(r'\bt\(\s*"([A-Za-z0-9_]+)"\s*\)', app))


def _dynamic_t_prefixes():
    """t("prefix_" + variable) — the literal part must still resolve to real keys."""
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    return set(re.findall(r'\bt\(\s*"([A-Za-z0-9_]*_)"\s*\+', app))


def _literal_tf_calls():
    """tf("key", {...}) — the helper that fills {tokens}.

    Every scan above knows only `t("key")`, so a key reachable only through tf()
    is invisible to the whole completeness suite until a customer meets the missing
    translation as a literal brace on their screen."""
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    return set(re.findall(r'\btf\(\s*"([A-Za-z0-9_]+)"', app))


def test_the_tf_helper_is_covered_by_a_scan():
    assert _literal_tf_calls(), "app.js has no tf() calls, or the scan regex broke"


def test_all_three_locales_are_parsed():
    dicts = dictionaries()
    assert set(dicts) == set(LOCALES), "i18n.js layout changed; update this test's parser"
    for lang, table in dicts.items():
        assert len(table) > 100, f"{lang} dictionary looks truncated"


def test_locales_have_identical_key_sets():
    dicts = dictionaries()
    reference = set(dicts["uz"])
    for lang in LOCALES[1:]:
        other = set(dicts[lang])
        assert other == reference, (
            f"{lang} drift: missing {sorted(reference - other)}, extra {sorted(other - reference)}"
        )


def test_no_empty_or_whitespace_only_values():
    blanks = [
        f"{lang}.{key}"
        for lang, table in dictionaries().items()
        for key, value in table.items()
        if not value.strip()
    ]
    assert not blanks, f"empty translations: {blanks}"


@pytest.mark.parametrize("lang", LOCALES)
def test_markup_keys_exist(lang):
    missing = sorted(_keys_used_in_markup() - set(dictionaries()[lang]))
    assert not missing, f"markup references keys absent from {lang}: {missing}"


@pytest.mark.parametrize("lang", LOCALES)
def test_js_literal_keys_exist(lang):
    missing = sorted(_literal_t_calls() - set(dictionaries()[lang]))
    assert not missing, f'app.js t("key") calls absent from {lang}: {missing}'


@pytest.mark.parametrize("lang", LOCALES)
def test_js_template_keys_exist(lang):
    missing = sorted(_literal_tf_calls() - set(dictionaries()[lang]))
    assert not missing, f'app.js tf("key") calls absent from {lang}: {missing}'


def test_a_template_placeholder_set_survives_translation():
    """A translation may reorder a sentence; it may not rename, drop or invent a
    token. `tf` fills {a} {b} {c} by name, so the locale that writes {n} instead
    shows the customer a brace — and the locale that drops one shows a sentence
    about nothing."""
    dicts = dictionaries()
    offenders = []
    for key in sorted(dicts["uz"]):
        if key not in dicts["ru"] or key not in dicts["en"]:
            continue
        tokens = {lang: set(re.findall(r"\{([a-z0-9]+)\}", dicts[lang][key]))
                  for lang in LOCALES}
        if not any(tokens.values()):
            continue
        if len({frozenset(v) for v in tokens.values()}) != 1:
            offenders.append((key, tokens))
    assert not offenders, f"placeholder drift: {offenders}"


def test_dynamic_translation_prefixes_resolve():
    prefixes = _dynamic_t_prefixes()
    assert prefixes, "expected concatenating t() calls to be covered"
    for lang, table in dictionaries().items():
        for prefix in prefixes:
            assert any(k.startswith(prefix) for k in table), (
                f"{lang} has no key starting with {prefix!r}"
            )


def _reference_text():
    """Everything that can name a translation key, minus the dictionaries themselves."""
    parsed = _parsed()
    text = (STATIC / "index.html").read_text(encoding="utf-8")
    text += (STATIC / "app.js").read_text(encoding="utf-8")
    text += (STATIC / "i18n.js").read_text(encoding="utf-8")
    for _table, body in parsed.values():
        text = text.replace(body, "")
    return text


_STATUS_FILTER_RE = re.compile(r'if status and status not in \(([^)]*)\)')
# db.add_job_event(jid, "step", ...) — the vocabulary the live channel actually pushes.
_JOB_EVENT_STEP_RE = re.compile(r'add_job_event\(\s*[A-Za-z_]\w*\s*,\s*"([a-z_]+)"')


def _emitted_progress_steps():
    """Step names the backend emits, collected from the call sites themselves.

    Deriving ps_ from _STEP_PCT alone is circular: the percentage table is a
    second list someone has to remember to update. A step emitted without an
    entry there ships as pct=30 (the progress bar walks backwards) and the label
    test would still pass, because both lists were written from the same guess.
    """
    app = STATIC.parent / "app"
    steps = set()
    for name in ("pipeline.py", "main.py"):
        steps |= set(_JOB_EVENT_STEP_RE.findall((app / name).read_text(encoding="utf-8")))
    assert steps, "no add_job_event call sites found; update this test"
    return steps


def _refusal_reasons():
    """Every `reason` value a client can be handed for a step.

    Two sources, both mechanical: the reasons the pipeline writes itself, and the
    engine codes the transport already knows how to refuse (the same table that
    picks the HTTP status). Deriving it here means a new refusal code cannot ship
    as a bare word in front of a paying customer."""
    from app.main import _ALIGN_REFUSAL_STATUS
    app = STATIC.parent / "app"
    static = set(re.findall(r'"reason":\s*"([a-z_]+)"',
                            (app / "pipeline.py").read_text(encoding="utf-8")))
    return static | set(_ALIGN_REFUSAL_STATUS)


def _composed_families():
    """Key families the UI builds at runtime: t("st_" + status), t("ps_" + pipeline
    step), t("notif_" + kind), t("t_" + job type).

    The suffixes come from the backend itself, so adding a job status, a live
    progress step or a notification kind without a translation fails here instead
    of printing "ps_hypothetical" in the studio.
    """
    from app.main import _STEP_PCT
    from app.notify import NotifKind
    from app.pipeline import JOB_TYPES

    main_src = (STATIC.parent / "app" / "main.py").read_text(encoding="utf-8")
    m = _STATUS_FILTER_RE.search(main_src)
    assert m, "job status filter vanished from main.py; update this test"
    statuses = {s.strip().strip('"\'') for s in m.group(1).split(",")}
    steps = _emitted_progress_steps() | set(_STEP_PCT)
    # A step that is never emitted is dead weight in the percentage table; a step
    # that is emitted but missing there gets a made-up pct on the wire.
    unused = set(_STEP_PCT) - _emitted_progress_steps()
    assert not unused, f"_STEP_PCT covers steps nothing emits (dead or renamed): {sorted(unused)}"
    return {
        "st_": statuses | {"all"},
        "ps_": steps,
        "notif_": {k.value for k in NotifKind},
        "t_": set(JOB_TYPES),
        # A paid option that refuses owes the customer a sentence, not a token:
        # the UI composes t("st_skip_" + reason), so every possible reason must be
        # translated in every locale or the studio prints developer prose.
        "st_skip_": _refusal_reasons(),
    }


def test_composed_keys_are_translated_in_every_locale():
    dicts = dictionaries()
    missing = [
        f"{lang}.{prefix}{suffix}"
        for prefix, suffixes in _composed_families().items()
        for lang, table in dicts.items()
        for suffix in sorted(suffixes)
        if prefix + suffix not in table
    ]
    assert not missing, f"runtime-composed keys without a translation: {missing}"


def test_interpolated_keys_keep_their_placeholders():
    """ago_* carry {n}; a translator dropping it silently hides the number."""
    dicts = dictionaries()
    broken = [
        f"{lang}.{key}={value!r}"
        for lang, table in dicts.items()
        for key, value in table.items()
        if key.startswith("ago_") and key != "ago_now" and "{n}" not in value
    ]
    assert not broken, "placeholder {{n}} missing from plural-time keys: " + ", ".join(broken)


_UI_LITERAL_RES = (
    re.compile(r'\b(?:toast|confirm|alert)\(\s*"([^"]{4,})"'),
    re.compile(r"\b(?:toast|confirm|alert)\(\s*'([^']{4,})'"),
    re.compile(r'\btextContent\s*=\s*"([^"]{4,})"'),
    re.compile(r"\btextContent\s*=\s*'([^']{4,})'"),
)
_SENTENCE_RE = re.compile(r"[A-Za-z\u0400-\u04FF]{2,}[\s,]+[A-Za-z\u0400-\u04FF]{2,}")


def test_js_does_not_hardcode_ui_messages():
    """Every toast/confirm/label must go through t().

    Inline copy in JS is invisible to the locale switcher and to the markup
    scans above, so it is where untranslated UI always creeps back in.
    """
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    hardcoded = sorted({
        lit
        for rx in _UI_LITERAL_RES
        for lit in rx.findall(app)
        if _SENTENCE_RE.search(lit)
    })
    assert not hardcoded, f"user-facing literals in app.js (use t(key)): {hardcoded}"


def test_no_dead_translation_keys():
    """Every dictionary key must be reachable from markup or JS.

    Keys are referenced in three ways: data-i18n attributes, t("key") calls and
    key-name string literals (SERVER_ERROR_MAP values, notification type maps,
    onboarding arrays). A `.` before the token is a property access, not a
    reference — `a.download =` must not keep the `download` translation alive.
    """
    ref = _reference_text()
    live = _keys_used_in_markup() | _literal_t_calls()
    prefixes = _dynamic_t_prefixes()
    composed = {
        prefix + suffix
        for prefix, suffixes in _composed_families().items() for suffix in suffixes
    }
    dead = sorted(
        k for k in dictionaries()["uz"]
        if k not in live
        and k not in composed
        and not any(k.startswith(p) for p in prefixes)
        and not re.search(r"(?<![A-Za-z0-9_.])" + re.escape(k) + r"(?![A-Za-z0-9_])", ref)
    )
    assert not dead, f"unused i18n keys (remove them or wire them up): {dead}"


# Tokens that are legitimately identical in uz/ru/en: brands, file formats, tech words.
UNTRANSLATABLE = {
    "SRT", "ASS", "PDF", "API", "JSON", "Ovoz", "Telegram", "YouTube", "Reels",
    "TikTok", "WAV", "Studio", "PDF/A", "OCR", "AI", "GPT", "URL",
}


def test_locales_are_actually_translated():
    dicts = dictionaries()
    unaccounted = sorted(
        key for key, value in dicts["uz"].items()
        if value == dicts["ru"].get(key) == dicts["en"].get(key)
        and value.strip() not in UNTRANSLATABLE and len(value.strip()) > 3
    )
    assert not unaccounted, f"same string in uz/ru/en, probably untranslated: {unaccounted}"


_ATTR_RE = re.compile(r'(aria-label|placeholder|title)\s*=\s*"([^"]*)"', re.I)
_TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9-]*\b[^>]*)>")
_TITLE_TWINS = {"aria-label": "data-i18n-aria", "placeholder": "data-i18n-ph", "title": "data-i18n-title"}
# Brand names are the same string in uz/ru/en; labelling them would be noise.
BRAND_LABELS = {"Ovoz"}


def test_localizable_attributes_are_driven_by_i18n():
    """An aria-label/placeholder/title written directly in HTML is frozen English.

    applyI18n only rewrites attributes that carry a data-i18n* twin, so a literal
    aria-label survives every locale switch and screen readers announce English in
    an Uzbek/Russian product.
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    offenders = []
    for tag in _TAG_RE.finditer(html):
        body = tag.group(1)
        for attr in _ATTR_RE.finditer(body):
            name = attr.group(1).lower()
            value = attr.group(2).strip()
            if not value or value in BRAND_LABELS:
                continue
            if _TITLE_TWINS[name] not in body:
                offenders.append(f"<{tag.group(0)[:70]}")
    assert not offenders, "hardcoded localizable attributes: " + "; ".join(offenders[:12])


# English UI words that must never be hardcoded as visible text.
ENGLISH_TEXT_BLOCKLIST = {
    "Loading", "Settings", "Close", "Cancel", "OK", "Notifications", "Submit",
    "Search", "Save", "Delete", "Back", "Next", "Previous", "Download", "Retry",
    "Error", "Success", "Failed", "Pending", "Language",
}
_WORD_RE = re.compile(r"[A-Za-z]+")


def test_no_hardcoded_english_visible_text():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = re.sub(r"(?s)<(script|style)\b.*?</\1>", "", html)
    html = re.sub(r"<!--.*?-->", "", html)
    # an element tagged data-i18n gets its text overwritten at runtime anyway
    html = re.sub(r"<[a-zA-Z][^>]*data-i18n[^>]*>[^<]*</[^>]+>", "", html)
    text = re.sub(r"<[^>]+>", " ", html)
    hits = sorted({w for w in _WORD_RE.findall(text) if w in ENGLISH_TEXT_BLOCKLIST})
    assert not hits, f"hardcoded English UI words in markup: {hits}"
