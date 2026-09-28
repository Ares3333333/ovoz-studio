"""Ovoz Nafis — право реза узбекского предложения (the sixth engine).

Qator answers *how much* text may sit on one card: 17 chars/sec, 42 per line, two
lines, min dwell. Jimlik answers *when* a cut may happen: at a real pause in the
tape. Neither answers the question this file is about — **where a cut is allowed at
all** in a language that puts the verb at the end and glues meaning with
postpositions.

That question cannot be answered by a general punctuation-and-balance heuristic,
which is what every subtitle tool on the market ships: a cut that looks tidy in
English is unreadable in Uzbek.

    ... ikki oy ichida ishga tushirildi, uni esa yigirma [kishidan iborat jamoa] ...
                                                        ↑ a cut here is legal by width,
                                                          illegal by grammar

The laws below are all *left-binding* groups: a word that needs the words before
it, or a group that must not be torn apart. Each law reports a fault only when the
group is actually split by THIS cut, and each law carries the ambiguity of the
language instead of pretending it away:

  postposition   `uchun`/`bilan`/`haqida` cannot start a card — they govern the
                 phrase before them. `keyin`/`so'ng`/`ichida` are ambiguous (also
                 adverbs), so they are only blamed when the word before the cut is
                 not a clause end: "tekshirib, || keyin esa" is a new sentence
                 member, "uch soat || keyin" is a torn phrase.
  compound_verb  `tayyorlab berdi`, `ko'rib chiqamiz`, `bo'lib bo'lmaydi` — the
                 converb and its light verb are one predicate; splitting them
                 leaves the viewer holding a half action.
  bind_right     `va`, `yoki`, `chunki`, `agar` cannot END a card: they promise
                 what follows.
  pair           fixed connective pairs (`shuning uchun`, `garchi bo'lsa`) are one
                 junction; one half on each card reads as two.
  numeral_unit   `yigirma || kishidan`, `ikki || oy` — a numeral stranded from its
                 unit is the classic machine cut.

Design rules shared by the whole of `app/ling/`:

  * **text is never edited**. This module reads words and reports positions; it
    does not reorder, add, drop or rewrite one token. Deciding where to cut is the
    layout engine's job, and doing it here would let a language engine silently
    change what was said;
  * **pure Python, no model, no network, deterministic**: the same word list
    always yields the same fault set, which is what makes it sellable to a studio
    that runs one file twice;
  * **refuse rather than guess**: an empty or single-word list has no cuts to
    judge, and the answer is an empty set, never a fabricated "legal position".
"""
from __future__ import annotations

import re
from functools import lru_cache

ENGINE = "ovoz-nafis"
VERSION = "1.0"

# ─── word classes ──────────────────────────────────────────────────────────────
# Folded for comparison: `OʻZBEK`, "o'zbek" and "O'ZBEK" are the same word here,
# because a law that depends on which apostrophe a transliterator chose is a law
# that fires randomly.
_APOS = ("\u02bb", "\u2018", "\u2019", "\u02bc", "`")

# Postpositions that only ever govern left. Ambiguous ones live in AMBIGUOUS_PP.
POSTPOSITIONS = frozenset("""
    uchun bilan haqida gacha tufayli orqali karshi oldidan orqasidan ichidan
    tashqari nisbatan markazida atroflida chekkacha degan deb qarab bog'liq
""".split())

# Same form as an adverb ("afterwards") — blamed only when the previous word does
# not end a clause.
AMBIGUOUS_PP = frozenset("""
    keyin so'ng shundan oldin ortidan yakunida natijasida
""".split())

# Conjunctions and particles that must not close a card.
# `ham` is deliberately NOT here. The live ruler accused "siz ham || o'zbek
# tilida" of tearing a conjunction, and that is wrong: in `siz ham` the particle
# closes the noun phrase ("you too"), so a cut after it is an ordinary clause edge.
# Its other reading (`ham X ham Y`, "both ... and") does bind right, which is why
# it is not listed as a left-governing postposition either. Leaving the ambiguity
# alone costs one rare defect; guessing at it costs the client's trust in a correct
# subtitle file — the ruler marks are the loudest surface this engine has.
BIND_RIGHT = frozenset("""
    va yoki lekin ammo biroq chunki garchi agar toki bass unda shuning ya
""".split())

# Light verbs and modal words: the second half of a compound predicate. Matched as
# a stem prefix, because Uzbek inflects them (berdi/beradi/bergan/berib yubordi).
# Every stem here is written the way `fold` leaves it — apostrophe included — or it
# silently never matches, which is the worst kind of dead rule: the engine reports
# a clean timeline because its own regex could not read the language.
LIGHT_VERBS = (
    "ber", "qil", "ol", "bo'", "bo'lm", "qol", "chiq", "kir", "ket", "kel",
    "yot", "tur", "ot", "boshla", "davom", "yoz", "ura", "qayt", "qo'", "ko'ra",
)
MODALS = frozenset("""
    mumkin kerak shart lozim zarur majbur imkoniyat edi emas edik edi-xolos
    bo'ladi bo'lish bo'lishi bo'lmaydi bo'ldi
""".split())

# Converb / verbal-noun endings that signal "a light verb is coming". Two forms
# the shortlist must not forget: `-lab` (consonant and Persian-derived stems:
# `tayyorlab berdi`, `moliyalashtirib berdi`) and the potential `-a/-ay` series
# (`ko'ra olaman`, `ayta olmaydi`). A bare `-a` is deliberately NOT in here: it
# would match `jamoa` and half the noun vocabulary, and a law with that many false
# positives is worse than no law — hence the consonant+bond forms spelled out.
_CONVERB_RE = re.compile(
    r"(?:ib|ilib|yib|ub|quv|lab|lay|lib|masdan|maslik|may|gani|gan|adigan|moqchi"
    r"|ishi|ish|ra|ta|ay)\Z",
    re.IGNORECASE)

# Numerals and the units they measure.
NUMERALS = frozenset("""
    bir ikki uch tort besh olti yetti sakkiz toqqiz on yigirma qirq ellik oltmish
    yetmish sakson toqson yuz ming million milliard yarim necha
""".replace("'", "") .split())
# Unit STEMS, matched by prefix: `yil` also appears as `yilda`, `yilgi`, `yiliga`,
# and a numeral cut from any of them is the same defect.
UNIT_STEMS = (
    "kishi", "odam", "inson", "nafar", "yil", "oy", "kun", "soat", "minut", "sekund",
    "metr", "kilogramm", "tonna", "dona", "marta", "martaba", "foiz", "million",
    "milliard", "yuz", "ming",
)

# Fixed multi-word junctions: half of each, on each side of a cut, reads as two.
PAIRS = (
    ("shuning", "uchun"), ("shu", "sababli"), ("garchi", "bo'lsa"),
    ("qarab", "turib"), ("nisbatan", "olganda"), ("bir", "necha"),
)

# Clause-end punctuation, used by the ambiguous-postposition rule only.
_CLAUSE_END = (".", "?", "!", ":", ";", ",", "\u2014", "\u2026")

_WORD_RE = re.compile(r"\S+")

# How the layout engine should weigh a fault. A hard fault is not "less nice":
# it is a cut a studio would send back. `line_max` violations in Qator use the
# same idea — a rule, not a preference.
HARD_PENALTY = 260.0
MAX_CUTS_REPORT = 40          # finite report bound, same law as the other engines


@lru_cache(maxsize=8192)
def fold(word: str) -> str:
    """Compare-ready form of a token: lowercase, one apostrophe, punctuation off.

    The official 2026 apostrophe `ʻ` (U+02BB) is the character Uzbek typography
    actually uses, so it is the one this function may not skip: a fold that leaves
    it alone makes `oʻl` and `o'l` different words and silently disables every rule
    that mentions an apostrophe. The gate in `tests/test_nafis.py` asserts that
    against the whole apostrophe table."""
    w = (word or "").lower()
    for a in _APOS:
        w = w.replace(a, "'")
    return re.sub(r"^[^\w']+", "", re.sub(r"[^\w']+$", "", w, flags=re.UNICODE),
                  flags=re.UNICODE)


def words_of(text: str) -> list[str]:
    return _WORD_RE.findall(text or "")


# ─── the laws ──────────────────────────────────────────────────────────────────

def _law_postposition(words, k):
    """`uchun`/`bilan` may not open the card after this cut."""
    right = fold(words[k])
    if right in POSTPOSITIONS:
        return ("postposition", words[k], "governs the phrase before the cut")
    if right in AMBIGUOUS_PP:
        left = words[k - 1]
        if left.rstrip().endswith(_CLAUSE_END):
            return None            # "tekshirib, || keyin esa" — a new member
        return ("postposition", words[k], "adverb reading; here it follows its noun")
    return None


def _law_compound_verb(words, k):
    """`tayyorlab berdi`, `ko'rib chiqamiz` — one predicate, two cards is a tear."""
    left, right = fold(words[k - 1]), fold(words[k])
    if not left or not right:
        return None
    if not _CONVERB_RE.search(left):
        return None
    if right in MODALS or any(right.startswith(stem) for stem in LIGHT_VERBS):
        return ("compound_verb", words[k - 1] + " " + words[k],
                "converb separated from its light verb")
    return None


def _law_bind_right(words, k):
    """`va`, `chunki`, `agar` may not close the card before this cut."""
    left = fold(words[k - 1])
    if left in BIND_RIGHT:
        return ("bind_right", words[k - 1], "conjunction promises what follows")
    return None


def _law_pair(words, k):
    a = fold(words[k - 1])
    b = fold(words[k])
    for left, right in _FOLDED_PAIRS:
        if a == left and b == right:
            return ("pair", words[k - 1] + " " + words[k], "fixed junction split in two")
    return None


def _law_numeral_unit(words, k):
    left, right = fold(words[k - 1]), fold(words[k])
    if left.isdigit() or left in NUMERALS:
        if any(right.startswith(stem) for stem in UNIT_STEMS):
            return ("numeral_unit", words[k - 1] + " " + words[k],
                    "numeral separated from its unit")
    return None


LAWS = (_law_postposition, _law_compound_verb, _law_bind_right, _law_pair,
        _law_numeral_unit)

# Folded once at import, not once per candidate position: this loop runs over every
# junction of every client text, and re-folding the constant side of a comparison
# made the engine pay for words it already knew.
_FOLDED_PAIRS = tuple((fold(a), fold(b)) for a, b in PAIRS)


def cut_faults(words: list[str], k: int) -> list[tuple[str, str, str]]:
    """Faults caused by cutting between words[k-1] and words[k].

    Empty when the cut is legal. Positions outside the text are not faults but
    non-decisions: there is nothing there to cut, and inventing a verdict about it
    is how an engine starts to lie."""
    n = len(words)
    if n < 2 or not 0 < k < n:
        return []
    out = []
    for law in LAWS:
        hit = law(words, k)
        if hit:
            out.append(hit)
    return out


def illegal_cuts(words: list[str]) -> list[dict]:
    """Every position in this run of text that must not become a card boundary."""
    return [{"k": k, "code": code, "word": word, "detail": detail}
            for k in range(1, len(words))
            for code, word, detail in cut_faults(words, k)]


def legal_positions(words: list[str]) -> list[int]:
    """Cut positions this text would allow. A run with no legal cut is reported as
    empty, not as "anywhere": the layout engine must then widen the card, borrow
    silence, or split upstream — not pick a random tear."""
    return [k for k in range(1, len(words)) if not cut_faults(words, k)]


def penalty(words: list[str], k: int) -> float:
    """Cost for a layout engine that is choosing among positions anyway."""
    return HARD_PENALTY * len(cut_faults(words, k))


# ─── the report ────────────────────────────────────────────────────────────────

def boundaries(cues) -> list[dict]:
    """Each boundary between consecutive cues, judged as the viewer will read it.

    Cue text arrives with its own line breaks; those are the *look* of a card and
    are flattened here, because this report is about where one card stops and the
    next begins.
    """
    out = []
    rows = list(cues or [])
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        left, right = words_of(getattr(prev, "text", "")), words_of(getattr(cur, "text", ""))
        if not left or not right:
            continue
        faults = cut_faults(left + right, len(left))
        out.append({
            "between": i + 1,                          # cue i and cue i+1
            "after": getattr(prev, "index", i),
            "left": left[-1],
            "right": right[0],
            "hard": bool(faults),
            "codes": sorted({code for code, _w, _d in faults}),
        })
    return out


def ruler(words: list[str], limit: int = MAX_CUTS_REPORT) -> dict:
    """Every cut position in this text, legal or not — the data a cut ruler paints.

    This is the ONE full scan of the text: `analyze` reads its fault counts, its
    per-code histogram and its list from here. Scanning twice (once for the ruler,
    once for the totals) made the engine pay double for the same junctions, which a
    review measured on the worst legal input the endpoint accepts.

    Truncation is reported, never hidden: `ruler` stops at `limit` and says so, and
    the counts above it stay true for the WHOLE text. A widget that silently drops
    the tail would show a clean ruler on a dirty paragraph.
    """
    rows, legal, codes = [], 0, {}
    fault_count = 0
    for k in range(1, len(words)):
        faults = cut_faults(words, k)
        if not faults:
            legal += 1
            continue
        for code in {f[0] for f in faults}:
            codes[code] = codes.get(code, 0) + 1
        fault_count += len(faults)
        if len(rows) < limit:
            rows.append({"k": k, "left": words[k - 1], "right": words[k],
                         "codes": sorted({code for code, _w, _d in faults})})
    total = max(0, len(words) - 1)
    forbidden = total - legal
    return {"positions": total, "legal": legal, "forbidden": forbidden,
            "fault_count": fault_count, "by_code": dict(sorted(codes.items())),
            "forbidden_truncated": forbidden > limit,
            "truncated": fault_count > limit,
            "ruler": rows}


def analyze(cues, source_text: str | None = None) -> dict:
    """The engine's answer for a cue list: what it forbids, where, and how much of
    the delivered timeline is clean. Numbers only, no prose — the client localises
    the verdicts the same way it does every other engine report."""
    rows = list(cues or [])
    prose = (source_text or "").strip()
    if not rows and not prose:
        # Nothing to judge. An empty cue list with no text is refused rather than
        # reported as a perfect 100 — a score for work never done is the exact lie
        # this engine exists to make impossible.
        raise ValueError("nafis needs cues or a text to judge")
    if source_text is not None:
        flat = words_of(source_text)
    else:
        flat = [w for c in rows for w in words_of(getattr(c, "text", ""))]
    rule = ruler(flat)
    faults = [{"k": r["k"], "code": c, "word": r["left"] + " " + r["right"]}
              for r in rule["ruler"] for c in r["codes"]]
    b = boundaries(rows)
    hard = [x for x in b if x["hard"]]
    total = len(b)
    shown = faults[:MAX_CUTS_REPORT]
    # `forbidden` counts POSITIONS, not law hits: one cut can break two laws at once
    # (`shuning || uchun` is a broken junction and a stranded postposition), and a
    # client that adds `legal_positions + forbidden` to get the text length gets a
    # number bigger than the sentence. `fault_count` is the law-hit total, and
    # `by_code` breaks it down — a live check caught the two meanings living in one
    # field, which is exactly how a report starts to disagree with itself.
    return {
        "engine": ENGINE,
        "version": VERSION,
        "laws": ["postposition", "compound_verb", "bind_right", "pair",
                 "numeral_unit"],
        "words": len(flat),
        "cut_positions": max(0, len(flat) - 1),
        "legal_positions": rule["legal"],
        # The ruler's own verdict, meaningful for a paragraph as well as for a
        # delivered file: how many of the cuts available in this text are legal.
        "legal_share": round(rule["legal"] / rule["positions"], 4) if rule["positions"] else None,
        "ruler": rule["ruler"],
        "ruler_truncated": rule["forbidden_truncated"],
        "forbidden": rule["forbidden"],
        "fault_count": rule["fault_count"],
        "forbidden_truncated": rule["truncated"],
        "faults": shown,
        "by_code": rule["by_code"],
        "boundaries": {"measured": total, "hard": len(hard),
                       "clean_share": round(1.0 - len(hard) / total, 4) if total else None},
        "boundary_list": b[:MAX_CUTS_REPORT],
        # `score` is about BOUNDARIES. A paragraph with no cue boundaries has no
        # boundary score — it gets `null` and reads its quality from `legal_share`.
        # Reporting 100.0 here would award a prize for measuring nothing.
        "score": round(100.0 * (1.0 - len(hard) / total), 1) if total else None,
    }
