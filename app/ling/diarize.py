"""Ovoz Turn — proprietary speaker-attribution engine (the second moat).

Nobody can buy this for Uzbek. Off-the-shelf diarizers (pyannote, Speechmatics,
AssemblyAI) cluster on room acoustics and ignore what is being *said*, so they
break on the two things real Uzbek interviews are full of: ASR cues that split one
utterance in half, and speaker changes that are marked morphologically rather than
acoustically (a phone call sounds like one voice).

This engine decides turn boundaries from four independent evidence families and
fuses them with an explicit, explainable score:

1. Structure   — gaps, overlaps, and whether a cue continues the previous sentence
                 (unclosed punctuation + lowercase start = the same person talking;
                 this single rule removes most of the ASR over-segmentation noise).
2. Morphology  — person marking on verbs and pronouns, in *both* script states:
                 every cue is folded to legacy Latin by romanizer.cyr2lat first, so
                 Cyrillic Uzbek gets exactly the same analysis as Latin Uzbek.
3. Dialogue    — adjacency pairs: a greeting takes the floor, an acknowledgment-only
                 cue answers someone, a question expects a *different* voice next.
4. Voice       — optional acoustic profile per cue (loudness, zero-crossing rate,
                 energy-weighted brightness proxy) computed by the caller from PCM.
                 Pure numbers, no model, no download.

Output is deterministic: the same segments always produce the same speakers, and
every decision carries the cues that produced it so the UI can explain itself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .romanizer import cyr2lat, normalize_uzbek

MAX_SPEAKERS_DEFAULT = 8

class Cue(Protocol):
    """Anything with a timing and text — providers.base.Segment satisfies it, so
    app/ling stays free of the provider layer."""
    start: float
    end: float
    text: str


# ─── cue weights ────────────────────────────────────────────────────────────────
# Thresholds, not a classifier: auditable, tunable per language without retraining.
W_NEW_SPEAKER = 0.85     # score at or above which the floor changes hands
W_KEEP_SAME = -1.60      # at or below this the previous speaker is held no matter what
W_CONTINUATION = -2.20   # an ASR split mid-sentence is not a turn at all
W_OVERLAP = 0.90         # two voices literally at once
W_LONG_GAP = 0.45        # a beat of silence before a fresh start
W_PERSON_FLIP = 0.70     # 1st person <-> 2nd person disagreement between adjacent cues
W_FLOOR_TAKER = 0.55     # explicit turn-openers ("keling", "to'xtang", greetings)
W_BACKCHANNEL = 0.85     # acknowledgment-only cue: it answers, so it is a new voice
W_ADJACENCY = 0.55       # role exchange: question<->statement across a boundary
W_ADDRESS = 0.45         # a name or "aka"/"ustoz" called out inside the cue
W_REPLY_GREET = 0.90     # answering half of a greeting pair: near-categorical
W_VOICE_FAR = 1.00       # acoustic profile outside every known speaker
W_VOICE_NEAR = -0.35     # acoustic profile is a strong match
# A cue that carries some turn evidence but not enough to invent a person is read
# as the *other* voice already on the tape, never as the current one continuing.
# This threshold is what keeps a soft Q->A exchange from piling up new speakers.
W_ALTERNATE = 0.55
VOICE_SPLIT_DIST = 0.45  # relative L1 distance that counts as a different voice
VOICE_SAME_DIST = 0.18   # below this the two cues are certainly the same voice
MAX_GAP_SAME = 0.35      # below this the cues are effectively continuous speech


@dataclass
class Turn:
    """One attributed utterance: a segment plus the decision made about it."""
    index: int
    start: float
    end: float
    text: str
    speaker: int = 1
    score: float = 0.0
    cues: tuple[str, ...] = ()
    confidence: float = 0.0
    voice: tuple[float, float, float] | None = None


# ─── morphology: person marking ─────────────────────────────────────────────────
# Suffixes are the productive 2nd/1st-person verbal tails, matched on a folded word
# tail: "berayapsiz", "kelibsizmi", "qilsangiz" all read as 2nd person. The tails are
# deliberately short — "-siz" can also be the derivational 'without' suffix — so a
# person flip is strong evidence but never decides a boundary alone.
_P2_SUFF = ("siz", "san", "sang", "ding", "ngiz")
# "-gan" is deliberately absent: it is the person-neutral past participle
# ("u kelgan" = he came), so reading it as 1st person turns every past-tense
# narration into "men" and fabricates speaker changes. The real 1st-person
# compound still lands on "-man" ("kelganman"), which also covers "-yapman"/"-ayman".
_P1_SUFF = ("man", "miz", "dim", "dik", "sam")
_P2_WORDS = ("siz", "sen", "size", "senga", "seni", "sizni")
_P1_WORDS = ("men", "biz", "menga", "mening", "bizning", "mendan")
# A suffix test cannot see the stem, so these ordinary words would read as a person
# marking: "roman"/"dushman" would say "I am", and every oath ("qasam") would put a
# speaker on the tape. The honest fix is a verbal-stem check; this is the cheap one.
_TAIL_EXCEPTIONS = frozenset(("roman", "dushman", "german", "oman", "qasam",
                              "insan", "somon"))
# Russian pronouns are the person signal in the code-switched register half of
# Tashkent speaks. They are kept here in the script people type them in and folded
# into the text's own shape below — a Cyrillic needle compared against folded text
# never matches, which silently blinds the whole morphology family.
_RU_P2_WORDS = ("вы", "ты", "вас", "вам", "тебя", "тобой")
_RU_P1_WORDS = ("я", "мы", "меня", "мне", "наш", "мой", "моя")

# Turn openers: someone explicitly taking or asking for the floor.
_FLOOR_TAKERS = (
    "assalomu", "salom", "hayrli", "rahmat", "rahmat sizga", "kiring", "keling",
    "to'xtang", "toxtang", "ayting", "aytib bering", "tushuntiraman", "javob",
    "savol", "menimcha", "menda", "bu yerdaman", "xullas", "yo'q," "yoq,",
    "ha,", "ha, albatta", "tushundim", "qanday yordam", "eslatib o'taman",
)
# Acknowledgment-only cues: too short to open a topic, so they must be a reply.
_BACKCHANNELS = (
    "ha", "aha", "oh", "ooh", "uvvoh", "aan", "anj anch", "tushundim", "tushunarli",
    "yaxmi", "mayli", "joyida", "to'g'ri", "tougri", "ro'stmi", "rostmi", "ha ha",
    "rahmat", "jaxshimi", "qaerda", "keldim", "eshityapman",
)
_ADDRESS_WORDS = ("aka", "opa", "ustoz", "ustozim", "do'stim", "dostim",
                  "azizim", "janob", "hurmatli", "jonim", "farzandim", "rahbar")
# The greeting pair is the cleanest floor exchange in the language: whoever opens
# with "assalomu alaykum" is addressed, and the answering half can only come from
# the other side of the line. Matched on folded text, so both scripts read alike.
_GREET_OPEN = ("assalomu", "salom", "hello", "dobriy", "privet", "assalom")
_GREET_REPLY = ("va alaykum", "valaykum", "w alaykum", "alaykum assalom",
               "alaykum assalom", "va alaikum")

_VOCATIVE = re.compile(r"[,;]\s*([A-Z\u0410-\u042f][\w'ʻʼ`]{1,24})\b")
_ENDS_DONE = re.compile(r"[.!?…]$")
# -mi/-maymi question particle glued to a word tail (both scripts fold to Latin).
_INTERROGATIVE = re.compile(r"(?:maymi|dimi|mi)\b\s*[?.!]?\s*$")
# Words keep their internal apostrophe: "bo'lmadi" is one verb, and splitting it
# would make every suffix test read a fragment of the word instead of its tail.
_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*", re.UNICODE)


def _separate(text: str) -> str:
    """Folded text with every punctuation mark turned into a single space.
    Words keep their internal apostrophe, so "to'g'ri" survives as one token."""
    return re.sub(r"[^\w'ʻʼ`]+", " ", _fold(text), flags=re.UNICODE).strip()


def _fold(text: str) -> str:
    """Cyrillic or 2026-reform Latin -> legacy ASCII Latin, lowercase, spaced."""
    lat = cyr2lat(normalize_uzbek(text or "")).lower()
    return " " + re.sub(r"[ʻʼ`´’]", "'", lat) + " "


def _ru_tokens(words):
    """Russian pronouns in the shape the folded text can actually contain.

    cyr2lat maps в->v and т->t but leaves ы alone, so "вы" folds to the mixed-form
    token "vы"; a client that typed Russian in Latin sends "vy". Both spellings are
    registered, and the comparison is whole-token, so "vam" cannot match inside a
    longer word."""
    out = set()
    for w in words:
        folded = _fold(w).strip()
        # A needle that folds away to nothing would leave a Russian pronoun that can
        # never be matched by anything — the family would look alive and be blind.
        assert folded, f"Russian needle {w!r} folded to nothing"
        out.add(folded)
        out.add(folded.replace("ы", "y"))
    return frozenset(out)


_RU_P2 = _ru_tokens(_RU_P2_WORDS)
_RU_P1 = _ru_tokens(_RU_P1_WORDS)


def _tail_person(word: str) -> str | None:
    """Person read from the tail of one folded word.

    Uzbek carries the person in a verbal suffix and the interrogative -mi attaches
    *after* it, so "kelibsizmi" must be tested as "kelibsiz" — otherwise every
    question loses its person and the whole morphology family goes blind."""
    if word in _TAIL_EXCEPTIONS:
        return None
    w = word
    if w.endswith("mi") and len(w) > 4:
        w = w[:-2]
    if w.endswith(_P2_SUFF):
        return "2nd"
    if w.endswith(_P1_SUFF):
        return "1st"
    return None


def person_of(text: str) -> str | None:
    """'1st' / '2nd' / None from Uzbek (both scripts) and Russian person cues."""
    b = _fold(text)
    words = _WORD.findall(b)
    if not words:
        return None
    # Same token set as the verb scan: "men, ..." is the most natural way a person
    # pronoun appears in a real cue, and a whitespace split would drop it.
    toks = set(words)
    p2 = bool(toks & set(_P2_WORDS)) or bool(toks & _RU_P2)
    p1 = bool(toks & set(_P1_WORDS)) or bool(toks & _RU_P1)
    # The final person-marked verb governs who is talking: "sizga aytaman" names
    # the listener and is still 1st person, and "men kelaman" stays 1st however
    # many 2nd-person pronouns the rest of the cue carries.
    for w in reversed(words):
        tail = _tail_person(w)
        if tail:
            return tail
    if p2 and not p1:
        return "2nd"
    if p1 and not p2:
        return "1st"
    return None


def is_floor_taker(text: str) -> bool:
    b = _fold(text)
    return any(b.lstrip().startswith(m) for m in _FLOOR_TAKERS)


def is_backchannel(text: str) -> bool:
    # ASR hands back "Ha, tushundim." — the comma sits inside the two markers, so a
    # prefix test against the raw cue misses every backchannel that is punctuated
    # the way real subtitles are. Punctuation becomes a separator first, and the
    # marker list is then compared on clean word boundaries.
    b = _separate(text)
    if not b or len(_WORD.findall(b)) > 3:
        return False
    return any(b == m or b.startswith(m + " ")
               for m in (x.strip(" ,.") for x in _BACKCHANNELS) if m)


def asks(text: str) -> bool:
    b = _fold(text).rstrip()
    return b.endswith("?") or bool(_INTERROGATIVE.search(b))


def greets_or_addresses(text: str) -> bool:
    b = _fold(text)
    # Tokens, not whitespace splits: ASR glues the comma to the name ("rahmat,
    # ustoz,"), and a set membership test on raw words never sees the address.
    if set(_WORD.findall(b)) & set(_ADDRESS_WORDS):
        return True
    # A vocative tell is a Capitalized word right after a comma. Sentence-initial
    # capitals are stripped by ASR on every cue, so they prove nothing; mid-sentence
    # capitals in Uzbek only mark proper names. Raw text is tested because folding
    # to lowercase erases the signal — and Cyrillic capitals must count too, or the
    # same tape in the two scripts gives two answers.
    raw = (text or "").strip()
    return bool(_VOCATIVE.search(raw))


def replies_to_greeting(prev_text: str, cur_text: str) -> bool:
    """True when cur opens the answering half of a greeting pair started by prev."""
    p = _fold(prev_text).lstrip()
    c = _fold(cur_text).lstrip()
    if not any(p.startswith(g) for g in _GREET_OPEN):
        return False
    return any(c.startswith(r) for r in _GREET_REPLY)


# ─── structure ──────────────────────────────────────────────────────────────────
def gap_of(prev: Cue, cur: Cue) -> float:
    return float(cur.start) - float(prev.end)


def is_continuation(prev_text: str, cur_text: str, gap: float) -> bool:
    """True when `cur` is the second half of the sentence `prev` started.

    ASR hands us one utterance as two cues; a diarizer that calls that a speaker
    change invents people. The tell is typographic, not acoustic: no closing
    punctuation, an almost silent gap, and a lowercase start."""
    p = (prev_text or "").rstrip()
    c = (cur_text or "").lstrip()
    if not c:
        return True
    if gap > MAX_GAP_SAME:
        return False
    if _ENDS_DONE.search(p):
        return False
    first = c[0]
    return first.islower() or first in "—,-…"


# ─── voice (optional acoustic evidence) ─────────────────────────────────────────
def voice_distance(a, b) -> float:
    """Normalised L1 distance in [0,1] over (loudness, ZCR, brightness).

    Missing on either side -> None, which the fusion reads as 'no evidence', never
    as 'different' — a text-only job must not be split by an imaginary signal."""
    if not a or not b:
        return None
    diffs = []
    for x, y in zip(a, b):
        if x is None or y is None:
            return None
        peak = max(abs(x), abs(y), 1e-9)
        diffs.append(abs(x - y) / peak)
    return sum(diffs) / len(diffs)


# ─── fusion ─────────────────────────────────────────────────────────────────────
# Evidence is grouped into families. Two cues from one family describe the same
# fact twice (a greeting is both a floor-taker and a vocative), so they must not
# add up: the strongest counts fully, the runner-up 30%, the rest nothing. Left
# unchecked, three soft dialogue cues invented a new person on every "rahmat".
SECOND_FAMILY_WEIGHT = 0.30
STRONG_NEW = 1.30       # with no alternate in memory, this score creates a speaker
ALTERNATE_WINDOW = 12   # cues of memory for "who held the floor, but not just now"


def _evidence(prev: Turn, cur: Turn, voice_dist, profile_dist) -> dict[str, list[tuple[str, float]]]:
    """Per-family list of (cue, weight) explaining this boundary."""
    fam: dict[str, list[tuple[str, float]]] = {}

    def add(family: str, cue: str, weight: float) -> None:
        fam.setdefault(family, []).append((cue, weight))

    gap = cur.start - prev.end
    if gap < -0.05:
        # Only a PARTIAL overlap is "two voices at once". When the previous cue runs
        # past the end of this one, the timing is broken or duplicated, and reading
        # it as an overlap minted a speaker out of a bad export.
        if -gap < max(cur.end - cur.start, 0.0):
            add("structure", f"overlap:{-gap:.2f}s", W_OVERLAP)
    elif gap >= 2.5:
        add("structure", f"gap:{gap:.1f}s", W_LONG_GAP)
    if is_continuation(prev.text, cur.text, gap):
        add("structure", "continuation", W_CONTINUATION)
    if replies_to_greeting(prev.text, cur.text):
        add("dialogue", "reply-greeting", W_REPLY_GREET)
    if is_backchannel(cur.text) and not is_backchannel(prev.text):
        add("dialogue", "backchannel", W_BACKCHANNEL)
    if is_floor_taker(cur.text) and not is_floor_taker(prev.text):
        add("dialogue", "floor-taker", W_FLOOR_TAKER)
    # The exchange goes both ways: a question after a statement is the interviewer
    # taking the floor, a statement after a question is the answer. Only a change
    # of role across the boundary counts — two questions in a row are one person
    # pressing, not an exchange.
    if asks(prev.text) != asks(cur.text):
        add("dialogue", "adjacency:role-flip", W_ADJACENCY)
    if greets_or_addresses(cur.text) and not greets_or_addresses(prev.text):
        add("dialogue", "vocative", W_ADDRESS)
    pp, cp = person_of(prev.text), person_of(cur.text)
    if pp and cp and pp != cp:
        add("morphology", f"person:{pp[0]}>{cp[0]}", W_PERSON_FLIP)
    if voice_dist is not None:
        # Speaker identity lives in the profile, not in the neighbouring cue: a new
        # voice that happens to be quiet is still a new voice, and comparing only
        # against the previous cue would read it as "same".
        d = profile_dist if profile_dist is not None else voice_dist
        if d >= VOICE_SPLIT_DIST:
            add("voice", f"voice:{d:.2f}", W_VOICE_FAR)
        elif d <= VOICE_SAME_DIST:
            add("voice", f"same-voice:{d:.2f}", W_VOICE_NEAR)
    return fam


def _fuse(fam: dict) -> tuple[float, list[str]]:
    """One number plus the cues that made it, capped per family."""
    total = 0.0
    cues: list[str] = []
    for items in fam.values():
        # A veto (continuation) is the strongest thing a family can say.
        items.sort(key=lambda kv: -abs(kv[1]))
        for rank, (cue, weight) in enumerate(items):
            cues.append(cue)
            if rank == 0:
                total += weight
            elif rank == 1:
                total += weight * SECOND_FAMILY_WEIGHT
    return total, cues


def _profile_of(profiles: dict, voice) -> tuple[int | None, float | None]:
    """Nearest known speaker for a voice vector, and how far it is."""
    best: int | None = None
    best_d: float | None = None
    for sp, mean in profiles.items():
        d = voice_distance(mean, voice)
        if d is None:
            continue
        if best_d is None or d < best_d:
            best, best_d = sp, d
    return best, best_d


def _alternate_speaker(turns: list[Turn], current: int) -> int | None:
    """The most recent voice that is not the one speaking now.

    In an interview the floor alternates, so a boundary that the words cannot
    attribute belongs to the other person — not to a new one. This is what keeps a
    two-voice tape from becoming a five-voice tape."""
    for t in reversed(turns[-ALTERNATE_WINDOW:]):
        if t.speaker != current:
            return t.speaker
    return None


def _mean_voice(samples: list[tuple]) -> tuple:
    n = len(samples)
    dims = len(samples[0])
    return tuple(round(sum(s[k] for s in samples) / n, 4) for k in range(dims))


def _attribute(turns, prev: Turn, cur: Turn, profiles: dict, score: float,
               max_speakers: int) -> int:
    """Who owns a cue that clearly opens a new turn.

    Acoustic evidence wins when it exists — the nearest profile, or a genuinely new
    voice when the tape is far from every known one. Without audio the only honest
    answer is the dialogue structure: hand the floor to the other recent speaker
    unless the case for a third person is strong."""
    known = {t.speaker for t in turns}
    if cur.voice:
        means = {sp: _mean_voice(v) for sp, v in profiles.items()}
        nearest, dist = _profile_of(means, cur.voice)
        if nearest is not None and dist is not None and dist < VOICE_SPLIT_DIST:
            return nearest
        if len(known) < max_speakers:
            return max(known) + 1
        return _alternate_speaker(turns, prev.speaker) or prev.speaker
    alt = _alternate_speaker(turns, prev.speaker)
    if alt is not None and score < STRONG_NEW:
        return alt
    if len(known) < max_speakers:
        return max(known) + 1
    return alt if alt is not None else prev.speaker


def _relabel(turns: list[Turn]) -> list[Turn]:
    """Speaker ids are renumbered by order of first appearance: 1, 2, 3…"""
    seen: dict[int, int] = {}
    for t in turns:
        seen.setdefault(t.speaker, len(seen) + 1)
        t.speaker = seen[t.speaker]
    return turns


def analyze_turns(segments, max_speakers: int = MAX_SPEAKERS_DEFAULT,
                  voices=None) -> list[Turn]:
    """Attribute every cue to a speaker. Deterministic and side-effect free.

    `voices` is an optional per-segment list of (loudness, zcr, brightness) triples;
    when absent the engine decides on structure, morphology and dialogue only."""
    segs = list(segments or [])
    if not segs:
        return []
    if max_speakers < 1:
        max_speakers = 1
    turns: list[Turn] = []
    profiles: dict[int, list[tuple[float, float, float]]] = {}
    for i, s in enumerate(segs):
        voice = voices[i] if voices and i < len(voices) else None
        base = Turn(index=i, start=float(s.start), end=float(s.end), text=s.text,
                    speaker=1, voice=voice)
        if not turns:
            turns.append(base)
            if voice:
                profiles[1] = [voice]
            continue
        prev = turns[-1]
        dist = voice_distance(prev.voice, voice)
        if voice and profiles:
            prof_dist = _profile_of(
                {sp: _mean_voice(v) for sp, v in profiles.items()}, voice)[1]
        else:
            prof_dist = None
        score, cues = _fuse(_evidence(prev, base, dist, prof_dist))
        # A voice that fits no known profile is on the tape: that alone hands the
        # floor over. The one thing that still outranks it is the proof that this
        # cue is the second half of the previous sentence.
        voice_far = prof_dist is not None and prof_dist >= VOICE_SPLIT_DIST
        # The tape already carries two voices and this cue says "the floor moves"
        # without saying whose: give it to the other known voice instead of holding.
        # That is what reads an interview correctly when both speakers sit in the
        # same register, and it still cannot invent anybody — it only fires once a
        # second voice is already on the tape. An acoustic match vetoes it: two
        # identical-sounding cues are one person, whatever the words look like.
        alternates = (score >= W_ALTERNATE
                      and len({t.speaker for t in turns}) > 1
                      and not (prof_dist is not None
                               and prof_dist <= VOICE_SAME_DIST))
        if score <= W_KEEP_SAME:
            speaker = prev.speaker                 # forced hold: never invent people
        elif score >= W_NEW_SPEAKER or voice_far:
            speaker = _attribute(turns, prev, base, profiles, score, max_speakers)
        elif dist is not None and dist >= VOICE_SPLIT_DIST:
            # The words are silent but two voices are audible: trust the tape.
            speaker = _attribute(turns, prev, base, profiles, score, max_speakers)
        elif alternates:
            speaker = _alternate_speaker(turns, prev.speaker) or prev.speaker
        else:
            speaker = prev.speaker
        if voice:
            profiles.setdefault(speaker, []).append(voice)
        base.speaker = speaker
        base.score = round(score, 3)
        base.cues = tuple(cues)
        # Confidence: how far the decision sits from the boundary it made.
        edge = W_NEW_SPEAKER if speaker != prev.speaker else 0.0
        base.confidence = round(max(0.0, min(1.0, 0.5 + abs(score - edge) / 2.5)), 2)
        turns.append(base)
    return _relabel(turns)


def label_segments(segments, **kw) -> list[int]:
    """The compact answer: one 1-based speaker id per input cue."""
    return [t.speaker for t in analyze_turns(segments, **kw)]


def plain(turns) -> str:
    """Transcript with speaker tags — the shape a human actually reads.

    `text` is defended because ASR providers hand back None for a silent cue, and
    one None must not lose the whole transcript at the very end of a job."""
    return "\n".join(f"[S{t.speaker}] {(t.text or '').strip()}" for t in turns)


def srt_text(turns) -> str:
    """Cue text with the tag inline, ready to be embedded in SRT/ASS."""
    return plain(turns)


def summary(turns) -> dict:
    """Interview metrics the UI and the CSV export both read."""
    total = len(turns)
    speakers: dict[int, dict] = {}
    for t in turns:
        row = speakers.setdefault(t.speaker, {"speaker": t.speaker, "turns": 0,
                                              "seconds": 0.0, "words": 0})
        row["turns"] += 1
        row["seconds"] = round(row["seconds"] + max(0.0, t.end - t.start), 2)
        row["words"] += len(_WORD.findall(t.text or ""))
    rows = sorted(speakers.values(), key=lambda r: -r["seconds"])
    conf = [t.confidence for t in turns if t.cues]
    return {
        "speakers": len(rows),
        "turns": total,
        "per_speaker": rows,
        "longest_share": round(rows[0]["seconds"] / sum(r["seconds"] for r in rows), 2)
        if rows and sum(r["seconds"] for r in rows) > 0 else 0.0,
        "avg_confidence": round(sum(conf) / len(conf), 2) if conf else 1.0,
    }


def diarize(segments, **kw) -> dict:
    """One-call answer for the public API: turns + aggregate read-out."""
    turns = analyze_turns(segments, **kw)
    out = summary(turns)
    out["lines"] = [{"i": t.index, "start": round(t.start, 3), "end": round(t.end, 3),
                     "speaker": t.speaker, "text": t.text, "cues": list(t.cues),
                     "confidence": t.confidence} for t in turns]
    out["transcript"] = plain(turns)
    return out
