"""Ovoz Lingua — proprietary Uzbek language engine (the moat).

A single high-level facade that composes script detection, reversible
Cyrillic↔Latin transliteration, official-standard apostrophe rendering
(U+02BB turned comma, O'zbekiston 2019 rule), brand-glossary protection and
lightweight code-switch analysis into one call.

No off-the-shelf product (Descript, Veed, DeepL, Google) offers this for
Uzbek: the digraph-aware round-trip plus the official apostrophe handling is
what makes the pipeline lossless for real Karakalpak/Uzbek subtitles.
"""
from __future__ import annotations

from .romanizer import (detect_script, is_new_latin, looks_cyrillic_uz,
                        normalize_uzbek, normalize_target, to_new_latin,
                        to_official_apostrophe, transliterate)
from .glossary import apply_terms

# Common Russian function-word fragments that leak into spoken Uzbek
# (code-switching). Counted in both scripts because real input arrives as
# Cyrillic *and* Latin. Kept intentionally small — used only as a soft signal.
_RU_MARKERS_CYR = ("и ", "в ", "не ", "что ", "но ", "или ", "как ", "это ")
_RU_MARKERS_LAT = ("i ", "v ", "ne ", "no ", "ili ", "kak ", "eto ", "cto ")


def count_words(text: str) -> int:
    return sum(1 for tok in text.split() if any(c.isalpha() for c in tok))


def detect(text: str) -> dict:
    """Script classification only (cheap path for the /detect endpoint)."""
    return detect_script(text or "")


def _code_switch_hints(text: str, latin: str) -> int:
    """Soft count of inline Russian markers, checked in the raw text (Cyrillic
    markers) and in the transliterated Latin form (Latin markers)."""
    cyr_blob = " " + normalize_uzbek(text).lower() + " "
    lat_blob = " " + latin.lower() + " "
    return (sum(cyr_blob.count(m) for m in _RU_MARKERS_CYR)
            + sum(lat_blob.count(m) for m in _RU_MARKERS_LAT))


def analyze(text: str, terms: dict[str, str] | None = None) -> dict:
    """Full linguistic read of a Uzbek text blob. Public, side-effect free.

    Returns a JSON-ready dict with script, confidence, transliterations in
    both directions, the official-apostrophe form, word/char metrics, a
    code-switch signal and any glossary term hits."""
    text = text or ""
    info = detect_script(text)
    lat = transliterate(text, "latin")
    cyr = transliterate(text, "cyrillic")
    official = to_official_apostrophe(lat)
    new_latin = to_new_latin(lat)
    hits = []
    if terms:
        low = text.lower()
        for src in terms:
            if src and src.lower() in low:
                hits.append(src)
    return {
        "script": info["script"],
        "confidence": info["confidence"],
        "cyrillic_count": info["cyrillic"],
        "latin_count": info["latin"],
        "words": count_words(text),
        "chars": len(text),
        "latin": lat,
        "cyrillic": cyr,
        "official": official,
        "new_latin": new_latin,
        # Which script states are actually present, reported separately from the
        # dominant 'script' label: transitional 2027-2031 text mixes states and
        # consumers (UI badges, corpus filters) need both answers at once.
        "has_reform_letters": is_new_latin(text),
        "has_cyrillic_letters": info["cyrillic"] > 0,
        "has_legacy_latin_letters": any("a" <= c.lower() <= "z" for c in lat),
        "code_switch_hints": _code_switch_hints(text, lat),
        "glossary_hits": hits,
        "is_cyrillic": looks_cyrillic_uz(text),
    }


def convert(text: str, to: str = "latin", official: bool = False,
            terms: dict[str, str] | None = None) -> dict:
    """Transliterate + optionally render official apostrophes + protect terms.

    Glossary is applied *after* transliteration so brand names survive the
    script flip in both directions."""
    out = transliterate(text or "", to)
    if terms:
        out = apply_terms(out, terms)
    # Official turned commas exist only in the legacy-Latin target: decorating a
    # Cyrillic or 2026-reform result would re-introduce oʻ/gʻ the target forbids.
    if official and normalize_target(to or "latin") == "latin":
        out = to_official_apostrophe(out)
    return {"result": out, "script": detect_script(text or "")["script"],
            "official": official}
