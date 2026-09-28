r"""Ovoz So'z — word-level timing inside an existing cue (the fifth moat layer).

Jimlik moves a cue's *edges* into the pauses the speaker took. That fixes the
blink, but the reader still meets six words at once. Word timings are what turn a
subtitle into something a machine can narrate, highlight, or karaoke: they are the
input to text-to-speech alignment, to "follow along" readers, to `.ass` `\k`
karaoke for editors, and to clipping a highlight reel to a spoken phrase.

Every product that sells this does it with an acoustic model — a forced aligner
trained on labelled speech, which is why none of them has it for Uzbek. This
engine has no model and needs no corpus. It already knows two things that no
transcript contains: which frames of the tape are speech (the Schmitt gate shared
with Jimlik, so the two listeners can never disagree about the same recording),
and how much air an Uzbek word needs (vowel clusters, both alphabets). From those
it derives a skeleton of plausible cut points and lets *the tape* decide each one:
a cut goes to a real valley of silence inside the line, matched so that word order
and cut order can never cross; where the speaker left no valley to spare, the cut
goes to the quietest frame nearby, and the report says which of the two happened.

The contract, held by tests:

  * **the words are not ours to change** — every token comes back byte-for-byte,
    in order, none added or dropped; the text is copied, never re-split. Tokens
    that carry no letters (`[S1]`, `[демо]`, a lone «—») are markup, not speech: a
    label on a line has no duration on the tape and gets no highlight,
  * **the cue is partitioned, not approximated** — the first word starts exactly
    where the cue starts and the last ends exactly where the cue ends, cuts are
    strictly increasing and contiguous, so the highlight never stalls or overlaps;
  * **a word boundary never sits inside speech if the tape offered anywhere else
    within reach** — the failure mode this engine exists to avoid is the
    inter-syllable dip that reads as silence to a naive gate;
  * **a cut that cannot be measured is refused, not invented** — a cue shorter
    than one frame per word, or one that lies outside the tape, is reported with a
    reason and carries no words; the rest of the file still gets its timings;
  * **the method is part of the answer** — `valley` / `quiet` / `speech` per cut,
    so a client can tell a measured boundary from a geometric one;
  * **every number is finite** — this report is served raw as an artifact;
  * **determinism** — the same tape and the same cues give a byte-identical
    report. Nothing here reads a clock or a random number.
  * **length is not a reason to refuse** — a tape longer than one block is timed
    through `words_blocks`, and the word boundaries come out where `words` would
    have put them: one curve, one gate, one algorithm, whether the samples arrived
    in one piece or in a hundred.
"""
from __future__ import annotations

import math
import re

from .align import (BLOCK_SEC_MAX, MAX_CUES, MAX_LISTEN_SEC, MIN_SAMPLES,
                    RATE_MAX, RATE_MIN, AlignError, Envelope, check_listen_window,
                    frame_curve, pcm_from_wav, speech_runs)
from .srt import Cue

# ─── the law, as numbers ───────────────────────────────────────────────────────
MAX_WORDS = 8_000          # CPU budget: every cut searches a window of frames
MIN_WORD_SEC = 0.06        # below this a word cannot be read, let alone highlighted
SEARCH_MS = 240.0          # how far from the geometric cut a sub-gate dip may be sought

_VOWELS = frozenset("aeiouəòóôàâ" + "AEIOUƏ" + "аеиоуэыәёюя" + "АЕИОУЭЫӘЁЮЯ")
# Ё, ю, я each carry a glide *and* a nucleus (йо, ю, ја): after another vowel they
# still open a new syllable, so they cannot be folded into a running cluster.
# "қиёма" is ki-yo-ma — three syllables, not two.
_IOTIZED = frozenset("ёюяЁЮЯ")
# oʻ / gʻ carry their own modifier: the apostrophe is part of the letter, so it
# must neither count as a vowel of its own nor break the vowel it follows.
_APOSTROPHE = "ʻʼ’'‘`"

_WORD_RE = re.compile(r"\S+")
# A caption line carries markup that is nobody's speech: `[S1]` (who is talking),
# `[демо]` (how the text was obtained) and `[Музыка]` (a sound, not a line) are
# typed labels the reader sees and no mouth pronounces. Timing them would give the
# preview a highlight that sits on a bracket, and would bill the line a "word" that
# has no length in the tape — which is exactly what a bracket means in a subtitle.
# A bare number, on the other hand, is spoken — «qirq ikki» is a word the mouth
# says — so the test is "any letter or digit", not "any letter".
_TAG_RE = re.compile(r"^\[[^\]]*\]$")


def tokens(text: str | None) -> list[str]:
    """The spoken words of a line: whitespace-split, tags and bare punctuation out.

    "Every token comes back byte-for-byte" still holds for what this returns — it is
    the *set* of timed tokens that excludes markup, and the exclusion is what makes
    `words` mean words to a person counting them on screen.
    """
    return [t for t in _WORD_RE.findall(text or "")
            if not _TAG_RE.match(t) and any(ch.isalnum() for ch in t)]

__all__ = ["WordError", "words", "words_curve", "words_blocks", "weights",
           "tokens", "to_vtt", "to_ass", "pcm_from_wav"]


class WordError(AlignError):
    """So'z says no. A subclass of the family's refusal type so that one endpoint
    handler and one status map serve both listeners: a bad WAV is the same bad WAV
    whichever question was being asked of it."""

    def __init__(self, message: str, code: str = "invalid_audio"):
        super().__init__(message, code)


# ─── how much air a word needs ─────────────────────────────────────────────────

def weights(words: list[str]) -> list[float]:
    """Relative speaking time of each word, from its own shape.

    Uzbek is vowel-harmonic enough that vowel *clusters* count syllables well:
    "bog'ladim" has three, "stol" one. Length alone would give a seven-letter
    consonant run the same room as three open syllables, and a cluster count alone
    would forget that a long consonant is literally longer to say — hence the small
    per-character term. No lookup table, no language model: one pass over letters.
    """
    out = []
    for w in words:
        clusters = 0
        prev_vowel = False
        for ch in w:
            v = ch in _VOWELS
            if v and (not prev_vowel or ch in _IOTIZED):
                clusters += 1
            prev_vowel = v or (prev_vowel and ch in _APOSTROPHE)
        out.append(max(1.0, clusters) + 0.04 * max(0, len(w) - 1))
    return out


# ─── the cue's window on the tape ──────────────────────────────────────────────

def _valleys(runs: list[tuple[int, int]], f0: int, f1: int) -> list[tuple[int, int]]:
    """Frame ranges inside [f0, f1] that are not speech, in tape order.

    These are the pauses *within a line* — the ones Jimlik cannot use because no
    cue edge reaches them, and the ones a word boundary was made for.
    """
    out = []
    prev = f0
    for s, e in runs:
        if e < f0 or s > f1:
            continue
        lo = max(s, f0)
        if lo > prev:
            out.append((prev, lo - 1))
        prev = max(prev, min(e, f1) + 1)
    if prev <= f1:
        out.append((prev, f1))
    return [(a, b) for a, b in out if b - a + 1 >= 2]


def _skeleton(f0: int, f1: int, w: list[float], min_frames: int) -> list[int] | None:
    """Cut points the text alone would ask for: monotone, each word holding at
    least `min_frames`, weighted by how much air each word needs.

    Works on cue *edges* — word k occupies frames [edges[k], edges[k+1]) — because
    spacing a word is what has to be guaranteed, and an off-by-one here is a word
    that loses a frame to its neighbour. Returns None when the window cannot hold
    that many frames: geometry, not judgement.
    """
    need = len(w) - 1
    if need <= 0:
        return []
    span = (f1 + 1) - f0
    if span < (need + 1) * min_frames:
        return None
    total = sum(w)
    edges: list[int] = []
    last = f0
    run = 0.0
    for k in range(need):
        run += w[k]
        e = (int(round(f0 + span * run / total)) if total
             else f0 + (k + 1) * min_frames)
        e = max(e, last + min_frames)
        e = min(e, (f1 + 1) - (need - k) * min_frames)
        if e <= last:
            return None
        edges.append(e)
        last = e
    return edges


def _assign_valleys(edges: list[int], valleys: list[tuple[int, int]], f0: int,
                    f1: int, min_frames: int) -> dict[int, int]:
    """Match the tape's pauses to the cut points, in order.

    Nearest-first per cut would be a greedy that breaks: two words sharing one
    valley would both claim it and the highlight would freeze. So a valley is used
    once, order is preserved on both sides, and a cut may only take a valley that
    still leaves one for every cut after it.
    """
    centers = [((a + b) // 2, (a, b)) for a, b in valleys]
    taken: dict[int, int] = {}
    used = set()
    for k, e in enumerate(edges):
        remaining = len(edges) - k - 1
        floor_at = max(taken.values()) if taken else f0
        best = None
        for j, (c, _range) in enumerate(centers):
            if j in used:
                continue
            # a valley at the very head of a cue is the pause *before* the line:
            # cutting there would hand the first word nothing at all
            if c < floor_at + min_frames:
                continue
            if c > (f1 + 1) - (remaining + 1) * min_frames:
                continue
            free_after = sum(1 for m, (cm, _r) in enumerate(centers)
                             if m not in used and m != j and cm > c)
            if free_after < remaining:
                continue
            key = (abs(c - e), c)
            if best is None or key < best[0]:
                best = (key, j, c)
        if best is None:
            break
        used.add(best[1])
        taken[k] = best[2]
    return taken


def _dip(lo: int, hi: int, target: int, levels: list[float],
         gate: float) -> tuple[int, str]:
    """The best moment for a cut the tape gave no pause to.

    Below the gate first: a frame nobody was shouting through is a fair place to
    change words. Only when the whole window is speech does the engine cut inside
    speech — at its quietest frame — and label the cut `speech`, because a client
    has to be able to tell a measured boundary from the least bad guess.
    """
    hi = min(hi, len(levels) - 1)
    lo = max(lo, 0)
    if lo > hi:
        return target, "speech"
    window = range(lo, hi + 1)
    quiet = [f for f in window if levels[f] < gate]
    pool = quiet or list(window)
    pick = min(pool, key=lambda f: (round(levels[f], 9), abs(f - target)))
    return pick, ("quiet" if quiet else "speech")


# ─── the engine ────────────────────────────────────────────────────────────────

def _check_cues(cues, rate, n: int | None) -> None:
    """Everything So'z refuses before it measures: the request's own shape, then
    the tape's size. Refusing after the envelope pass would charge a customer for
    a listening nobody asked for and no answer ever used.

    `n` is the length of the tape in samples, or None when the tape is still
    arriving in blocks — then the length check belongs at the end of the pass, not
    here, and pretending otherwise would refuse every long recording as empty."""
    if not cues:
        raise WordError("no cues to time", "no_cues")
    if len(cues) > MAX_CUES:
        raise WordError(f"too many cues ({len(cues)}, max {MAX_CUES})",
                        "too_many_cues")
    counted = sum(len(tokens(c.text)) for c in cues)
    if counted > MAX_WORDS:
        # CPU budget: every cut searches a window of frames, so the number of
        # words — not the length of the tape — bounds what an anonymous call costs.
        raise WordError(f"too many words ({counted}, max {MAX_WORDS})",
                        "too_many_words")
    if rate is None or not isinstance(rate, int) or not RATE_MIN <= rate <= RATE_MAX:
        raise WordError(f"sample rate must be an integer {RATE_MIN}-{RATE_MAX} Hz",
                        "bad_rate")
    if n is not None and n < MIN_SAMPLES:
        raise WordError(f"audio shorter than {MIN_SAMPLES} samples cannot be "
                        f"measured", "too_short")


def words(pcm, rate: int, cues: list[Cue]) -> dict:
    """Time every word of every cue against the tape that carries it.

    The one-call shape of `words_curve`, for a recording held whole in memory."""
    n = len(pcm)
    _check_cues(cues, rate, n)
    # The same window Jimlik reads, from the same function: two listeners with two
    # ceilings would mean a job gets cue edges for a tape its word timings were
    # refused for, and nobody downstream could explain the difference. Refused as a
    # WordError, so a caller that only catches this engine's error still hears about
    # the window.
    check_listen_window(n, rate, err=WordError)
    return words_curve(cues, frame_curve(pcm, rate))


def words_blocks(blocks, rate: int, cues: list[Cue],
                 block_sec: float = BLOCK_SEC_MAX) -> dict:
    """The same word timings over a tape that arrives in blocks — an hour of audio,
    one block in hand at a time.

    No second algorithm: the blocks build one curve and `words_curve` cannot tell
    the difference, which is the only reason a word boundary past the old window is
    trustworthy. The gate still comes from the whole tape, so the first act and the
    last act are measured against the same room tone."""
    _check_cues(cues, rate, None)
    env = Envelope(rate, block_sec)
    for b in blocks:
        env.feed(b)
    if env.samples < MIN_SAMPLES:
        raise WordError(f"audio shorter than {MIN_SAMPLES} samples cannot be "
                        f"measured", "too_short")
    return words_curve(cues, env.curve())


def words_curve(cues: list[Cue], curve: dict) -> dict:
    """Time every word against a curve the caller has already measured.

    Deliberately uncapped by `MAX_WORDS`: that ceiling exists so an anonymous
    caller cannot buy CPU, and a job has already paid, been throttled and been
    size-checked. Re-asserting it here would refuse a Studio job for a number that
    costs nothing extra — the same quiet bug the listen window used to be. A job's
    real bound is `MAX_SEGMENTS_PER_JOB` times the words per cue, and every cut
    searches a fixed window of frames, so the work is linear in the tape's own
    text."""
    runs = speech_runs(curve)
    if curve["silent_tape"] or not runs:
        # Nothing to hear: timing words off geometry alone would sell the customer
        # a karaoke track whose highlights drift a syllable per line.
        raise WordError("the tape carries no speech to place words in", "silent_tape")

    levels = curve["levels"]
    frame_s = curve["frame_s"]
    duration = curve["duration"]
    gate = curve["hold_level"]          # below this, nobody is speaking
    min_frames = max(1, int(math.ceil(MIN_WORD_SEC / frame_s)))
    search = max(1, int(round(SEARCH_MS / 1000.0 / frame_s)))

    out: list[dict] = []
    tally = {"valley": 0, "quiet": 0, "speech": 0}
    total_words = measured = 0
    longest = 0.0

    for c in cues:
        start, end = float(c.start), float(c.end)
        toks = tokens(c.text)
        base = {"i": c.index, "start": round(start, 3), "end": round(end, 3)}
        if not toks:
            out.append({**base, "words": [], "reason": "no_text"})
            continue
        if start >= duration or end <= 0.0:
            out.append({**base, "words": [], "reason": "outside_audio",
                        "n": len(toks)})
            continue
        if (end - start) < MIN_WORD_SEC * len(toks):
            out.append({**base, "words": [], "reason": "too_fast", "n": len(toks),
                        "cps_needed": round(len(toks) / max(end - start, 1e-6), 3)})
            continue

        f0 = max(0, min(len(levels) - 1, int(math.floor(start / frame_s))))
        f1 = max(f0, min(len(levels) - 1, int(math.ceil(end / frame_s)) - 1))
        skeleton = _skeleton(f0, f1, weights(toks), min_frames)
        if skeleton is None:
            out.append({**base, "words": [], "reason": "too_short",
                        "n": len(toks), "frames": (f1 + 1) - f0,
                        "needs": (len(toks)) * min_frames})
            continue

        valleys = _valleys(runs, f0, f1)
        wanted = _assign_valleys(skeleton, valleys, f0, f1, min_frames)
        edges: list[int] = []
        methods: list[str] = []
        for k, guess in enumerate(skeleton):
            if k in wanted:
                edges.append(wanted[k])
                methods.append("valley")
                tally["valley"] += 1
                continue
            lo = (edges[-1] + min_frames) if edges else f0 + 1
            # the reach of a dip is short on purpose: a cut two words away from the
            # place the text asked for is a different cut, not a better one
            hi = guess + search
            limit = (f1 + 1) - (len(skeleton) - k) * min_frames
            cut, how = _dip(max(lo, guess - search), min(hi, limit), guess,
                            levels, gate)
            floor_at = edges[-1] if edges else f0 - 1
            if cut <= floor_at:
                cut = floor_at + min_frames
                how = "speech"
            edges.append(cut)
            methods.append(how)
            tally[how] += 1

        bounds = [f0] + edges + [f1 + 1]
        items = []
        for k, tok in enumerate(toks):
            s = round(bounds[k] * frame_s, 3)
            e = round(bounds[k + 1] * frame_s, 3)
            items.append({"w": tok, "s": s, "e": max(e, s)})
        # the cue owns its own ends: the frames are the ruler, not the promise
        if items:
            items[0]["s"] = round(min(start, duration), 3)
            items[-1]["e"] = round(min(end, duration), 3)
            for a, b in zip(items, items[1:]):
                if b["s"] < a["e"]:
                    b["s"] = a["e"]
        longest = max([longest] + [it["e"] - it["s"] for it in items])
        total_words += len(items)
        measured += 1
        out.append({**base, "words": items, "valleys": len(valleys),
                    "methods": methods})

    cuts = tally["valley"] + tally["quiet"] + tally["speech"]
    return {
        "engine": "ovoz-soz",
        "audio": {"duration": round(duration, 3), "frames": len(levels),
                  "frame_ms": round(frame_s * 1000.0, 3),
                  # The same honesty Jimlik reports: how much of the tape this
                  # answer actually heard, in how many pieces, and what one piece
                  # was allowed to be. For a streamed pass the block IS the window;
                  # quoting the single-call ceiling beside `heard_sec` of an hour
                  # would make the artifact deny its own measurement.
                  "window_sec": curve.get("block_sec", MAX_LISTEN_SEC),
                  "heard_sec": round(duration, 3),
                  "blocks": curve.get("blocks", 1),
                  "block_sec": round(curve.get("block_sec", MAX_LISTEN_SEC), 3),
                  "streamed": "blocks" in curve,
                  "speech_runs": len(runs),
                  "speech_sec": round(sum((e - s + 1) * frame_s for s, e in runs), 3)},
        "tuning": {"min_word_sec": MIN_WORD_SEC, "search_ms": SEARCH_MS,
                   "min_frames": min_frames, "search_frames": search,
                   "gate_db": curve["gate_db"]},
        "cues": out,
        "summary": {
            "cues": len(cues),
            "cues_measured": measured,
            "cues_refused": len(cues) - measured,
            "words": total_words,
            "cut_valley": tally["valley"],
            "cut_quiet": tally["quiet"],
            "cut_speech": tally["speech"],
            "cuts": cuts,
            "longest_word_sec": round(longest, 3),
            # A share the client can compare across jobs: how much of this answer
            # came from the tape rather than from geometry.
            "valley_share": round(tally["valley"] / cuts, 3) if cuts else 1.0,
        },
    }


# ─── exports a player can actually use ─────────────────────────────────────────

def _vtt_time(sec: float) -> str:
    ms = max(0, int(round(sec * 1000)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def to_vtt(report: dict) -> str:
    """WebVTT with per-word inline timestamps.

    `<hh:mm:ss.mmm>` inside a cue is the timing syntax browsers already parse, so a
    follow-along reader needs no player of its own: each word turns on at the frame
    the tape said, not at a guess. A cue that was refused contributes nothing —
    half a karaoke line is worse than no karaoke line.
    """
    lines = ["WEBVTT", ""]
    for cue in report["cues"]:
        items = cue.get("words") or []
        if not items:
            continue
        lines.append(f"{_vtt_time(cue['start'])} --> {_vtt_time(cue['end'])}")
        lines.append(" ".join(f"<{_vtt_time(it['s'])}>{it['w']}" for it in items))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_ass(report: dict, title: str = "Ovoz Studio") -> str:
    """ASS karaoke: `\\kf` per word, centiseconds, the format Aegisub and ffmpeg
    burn in natively. The colour sweep itself is left to the style — the timings are
    the product here."""
    header = (
        "[Script Info]\n"
        f"Title: {title}\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1280\nPlayResY: 720\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, Alignment\n"
        "Style: Default,Arial,42,&H00FFFFFF,&H00000000,2\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Text\n"
    )

    def ass_time(sec: float) -> str:
        cs = max(0, int(round(sec * 100)))
        return "%d:%02d:%02d.%02d" % (cs // 360000, cs % 360000 // 6000,
                                      cs % 6000 // 100, cs % 100)

    events = []
    for cue in report["cues"]:
        items = cue.get("words") or []
        if not items:
            continue
        parts = []
        for k, it in enumerate(items):
            cs = max(1, int(round((it["e"] - it["s"]) * 100)))
            word = (it["w"].replace("\n", " ").replace("\r", " ")
                    .replace("{", "(").replace("}", ")"))
            # a space belongs between words in the rendered line, and ASS keeps
            # whatever is inside the tag — so it goes outside the timing block
            parts.append((" " if k else "") + "{\\kf%d}%s" % (cs, word))
        events.append(f"Dialogue: 0,{ass_time(cue['start'])},{ass_time(cue['end'])},"
                      f"Default,,0,0,0,,{''.join(parts)}")
    return header + "\n".join(events) + "\n"
