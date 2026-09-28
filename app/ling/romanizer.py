"""Транслитерация узбекского: кириллица ↔ латиница (официальный алфавит, правило 2019 — o'/g').

Это один из «рвов» продукта: носители старше ~35 лет пишут кириллицей,
молодёжь и госсектор — латиницей; любой языковой AI без нормализации теряет точность.
"""
from __future__ import annotations

import re
import unicodedata

# Узбекская кириллица → современная латиница (с диграфами и апострофом)
CYR_TO_LAT: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k", "қ": "q", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ў": "o'", "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ъ": "", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "ғ": "g'", "ҳ": "h", "ң": "ng", "ә": "a", "ө": "o", "ү": "u", "ӣ": "i", "ӯ": "u",
}

LAT_TO_CYR_PAIRS: list[tuple[str, str]] = [
    ("g'", "ғ"), ("o'", "ў"), ("ch", "ч"), ("shch", "щ"), ("sh", "ш"),
    ("yo", "ё"), ("yu", "ю"), ("ya", "я"), ("ts", "ц"), ("q", "қ"),
    ("h", "ҳ"), ("a", "а"), ("b", "б"), ("v", "в"), ("g", "г"), ("d", "д"),
    ("e", "е"), ("j", "ж"), ("z", "з"), ("i", "и"), ("y", "й"), ("k", "к"),
    ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("r", "р"),
    ("s", "с"), ("t", "т"), ("u", "у"), ("f", "ф"), ("x", "х"),
]
# Guarantee longest-first matching regardless of the literal order above
# (Python's sort is stable, so same-length pairs keep their deliberate order).
LAT_TO_CYR_PAIRS.sort(key=lambda p: -len(p[0]))


def _cap(template: str, target: str) -> str:
    """Сохраняем регистр источника при переходе лат→кир: 'Aliya'→'Алия', 'G'''→'Ғ'."""
    if not template or not target:
        return target
    if template.isupper():
        return target.upper()
    if template[:1].isupper():
        return target[:1].upper() + target[1:]
    return target


def word_shout_flags(text: str) -> list[bool]:
    """For every index: True when its whole word-run is ALL CAPS.

    The discriminator is word-level casing, not neighbour letters: a lone
    capital word "Я" in "Я ЛЮБЛЮ" stays shout ("YA"), while title-case
    "Шветсия" maps to "Shvetsiya" (not "SHV"). Both cyr2lat (live subtitle
    path, where ALL-CAPS lines are common) and from_new_latin need exactly
    this rule, so it is stated once."""
    n = len(text)
    shout = [False] * n
    i = 0
    while i < n:
        if text[i].isalpha():
            j = i
            while j < n and text[j].isalpha():
                j += 1
            cased = [c for c in text[i:j] if c.isupper() or c.islower()]
            if cased and all(c.isupper() for c in cased):
                for k in range(i, j):
                    shout[k] = True
            i = j
        else:
            i += 1
    return shout


def cyr2lat(text: str) -> str:
    out: list[str] = []
    shout = word_shout_flags(text)
    for idx, ch in enumerate(text):
        low = ch.lower()
        if low in CYR_TO_LAT:
            mapped = CYR_TO_LAT[low]
            if mapped:
                if ch.isupper():
                    if shout[idx]:
                        out.append(mapped.upper())
                    else:
                        out.append(mapped[:1].upper() + mapped[1:])
                else:
                    out.append(mapped)
        else:
            out.append(ch)
    return "".join(out)


def lat2cyr(text: str) -> str:
    out: list[str] = []
    i = 0
    lowered = text.lower()
    while i < len(text):
        matched = False
        for lat, cyr in LAT_TO_CYR_PAIRS:  # список отсортирован по убыванию длины диграфа
            n = len(lat)
            if lowered[i:i + n] == lat:
                out.append(_cap(text[i:i + n], cyr))
                i += n
                matched = True
                break
        if not matched:
            out.append(text[i])
            i += 1
    return "".join(out)


def normalize_uzbek(text: str) -> str:
    """Приводим узбекский латинский текст к единому стандарту апострофов: ʻ ’ ` ´ → '."""
    for variant in ("ʻ", "’", "`", "´", "ʼ"):
        text = text.replace(variant, "'")
    return unicodedata.normalize("NFC", text)


def looks_cyrillic_uz(text: str) -> bool:
    cyr = sum(1 for ch in text if "\u0400" <= ch.lower() <= "\u04ff")
    latin = sum(1 for ch in text if _count_latin(ch))
    return cyr > latin


# ─── Единый языковой движок (the moat) ───
# Ни один мировой продукт (Descript / Veed / DeepL / Google) не поддерживает
# узбекскую кириллицу↔латиницу с официальным апострофом. Это ядро — то, что
# нельзя скопировать «из коробки»: детект скрипта, обратимая транслитерация,
# идемпотентная нормализация и вывод по стандарту O'zbekiston 2019 (U+02BB).

_OFFICIAL_APOS = "\u02bb"  # ʻ — official Uzbek turned comma (oʻ, gʻ)


# ─── 2026 alphabet reform (O'zbekiston Senati, 10 sentabr 2026) ───
# The Senate approved a reformed Latin alphabet — Oʻ→Ö, Gʻ→Ğ, Sh→Ş, Ch→Ç —
# phased in from 2027 (full textbook transition by ~2031). For years THREE
# script states (Cyrillic, legacy Latin, new Latin) coexist at national scale.
# A lossless three-way converter is genuine language infrastructure nobody
# else in the world ships — it turns a compliance nightmare into the moat.
# (Defined early: script counting below needs the reform letters.)

_NEW_LATIN_MAP = {"o'": "\u00f6", "g'": "\u011f", "sh": "\u015f", "ch": "\u00e7"}
# Reverse derived from the forward map so the two tables can never drift.
_NEW_LATIN_REVERSE = {v: k for k, v in _NEW_LATIN_MAP.items()}
_NEW_LATIN_LETTERS = set("\u00f6\u011f\u015f\u00e7") | {c.upper() for c in "\u00f6\u011f\u015f\u00e7"}
_NEW_LATIN_RE = re.compile(r"(?i)(o'|g'|sh|ch)")


def _count_latin(ch: str) -> bool:
    """A letter that belongs to Uzbek Latin script counting: plain ASCII
    (case-insensitive) or one of the 2026 reform letters."""
    low = ch.lower()
    return ("a" <= low <= "z") or low in _NEW_LATIN_LETTERS


def detect_script(text: str) -> dict:
    """Classify the dominant script of Uzbek text with confidence.
    Returns {script: 'latin'|'cyrillic'|'mixed'|'other', cyrillic, latin, confidence}."""
    cyr = sum(1 for ch in text if "\u0400" <= ch.lower() <= "\u04ff")
    latin = sum(1 for ch in text if _count_latin(ch))
    total = cyr + latin
    if total == 0:
        return {"script": "other", "cyrillic": 0, "latin": 0, "confidence": 0.0}
    cyr_ratio, lat_ratio = cyr / total, latin / total
    dominant = "cyrillic" if cyr_ratio > lat_ratio else "latin"
    confidence = max(cyr_ratio, lat_ratio)
    # mixed when neither script reaches 80% and both are substantial
    script = "mixed" if confidence < 0.8 and min(cyr_ratio, lat_ratio) >= 0.2 else dominant
    return {"script": script, "cyrillic": cyr, "latin": latin,
            "confidence": round(confidence, 3)}


# Single source of truth for target names: the public API validates through
# normalize_target(), so no alias can be accepted by one layer and rejected
# by the other (a previous review found 'newlatin' dead at the boundary).
_TARGET_ALIASES = {
    "latin": "latin", "lat": "latin",
    "cyrillic": "cyrillic", "cyr": "cyrillic",
    "new_latin": "new_latin", "newlatin": "new_latin", "new": "new_latin",
}


def normalize_target(to: object) -> str | None:
    """Canonical script target, or None when the caller asked for something we
    do not implement. Case and surrounding space are tolerated."""
    if not isinstance(to, str):
        return None
    return _TARGET_ALIASES.get(to.strip().lower())


def transliterate(text: str, to: str = "latin") -> str:
    """Idempotent, script-aware conversion across the three co-existing Uzbek
    script states (Cyrillic / legacy Latin / 2026-reform Latin).
    - to='latin': canonical official Latin (apostrophe kept as ASCII ');
      Cyrillic or new-Latin input is folded to legacy Latin.
    - to='cyrillic': → Cyrillic; already-Cyrillic text passes through NFC.
    - to='new_latin': the 2026 Senate alphabet (Ö/Ğ/Ş/Ç).
    Never mangles a string that is already in the target script."""
    target = normalize_target(to) or "latin"
    has_cyr = any("\u0400" <= ch.lower() <= "\u04ff" for ch in text)
    if target == "cyrillic":
        if looks_cyrillic_uz(text):
            return unicodedata.normalize("NFC", text)
        return unicodedata.normalize("NFC", lat2cyr(from_new_latin(normalize_uzbek(text))))
    # a Latin target: first canonicalise to legacy Latin
    if has_cyr:
        # cyr2lat leaves any reform letters embedded in the text untouched, so
        # fold them too: mixed Cyrillic+new-Latin input (the 2027–2031 reality)
        # must still canonicalise to legacy Latin (contract of to='latin').
        legacy = from_new_latin(normalize_uzbek(cyr2lat(text)))
    else:
        legacy = from_new_latin(normalize_uzbek(text))
    if target == "new_latin":
        return unicodedata.normalize("NFC", to_new_latin(legacy))
    return unicodedata.normalize("NFC", legacy)


def to_official_apostrophe(text: str) -> str:
    """Render ASCII o'/g' with the official turned comma oʻ/gʻ (U+02BB).
    Applied only to the letters that take the mark in Uzbek, so Russian loan
    apostrophes and quotes stay untouched. Matching is on the two-character
    pairs, so it also works inside ALL-CAPS runs (O'ZBEK → OʻZBEK)."""
    out = text.replace("o'", "o" + _OFFICIAL_APOS).replace("g'", "g" + _OFFICIAL_APOS)
    out = out.replace("O'", "O" + _OFFICIAL_APOS).replace("G'", "G" + _OFFICIAL_APOS)
    return out


# ─── three-way converter helpers live above; see _NEW_LATIN_MAP ───


def is_new_latin(text: str) -> bool:
    return any(ch in _NEW_LATIN_LETTERS for ch in text)


def to_new_latin(text: str) -> str:
    """Legacy/official Latin → 2026 reform Latin (\u00f6 \u011f \u015f \u00e7). Case maps onto the
    single replacement letter. Idempotent: letters already reformed are left."""
    legacy = from_new_latin(normalize_uzbek(text))  # normalise to ASCII-apostrophe base

    def _rep(m: "re.Match[str]") -> str:
        src = m.group(0)
        tgt = _NEW_LATIN_MAP[src.lower()]
        return tgt.upper() if src[:1].isupper() else tgt

    return _NEW_LATIN_RE.sub(_rep, legacy)


def from_new_latin(text: str) -> str:
    """2026 reform Latin → legacy official Latin (ö→o', ş→sh, …). Title-case
    on a cased digraph start (Şahar→Shahar); a whole-word ALL-CAPS run expands
    in full (ŞAHAR→SHAHAR, IŞ→ISH) using the same word-level rule as cyr2lat.
    The single irrecoverable case is a capital digraph standing alone ("Ş"),
    where no cased neighbour exists — it renders title ("Sh"), a one-bit limit."""
    out: list[str] = []
    shout = word_shout_flags(text)
    for i, ch in enumerate(text):
        low = ch.lower()
        if low in _NEW_LATIN_REVERSE:
            mapped = _NEW_LATIN_REVERSE[low]
            if ch.isupper():
                out.append(mapped.upper() if shout[i]
                           else mapped[:1].upper() + mapped[1:])
            else:
                out.append(mapped)
        else:
            out.append(ch)
    return "".join(out)
