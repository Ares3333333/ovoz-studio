"""Ovoz Qator — subtitle layout and readability engine (the second moat layer).

Diarization answers *who* spoke. This answers whether a human can actually read
the answer in the time it is on screen, and fixes it without touching a single
word. It is the part every streaming vendor does by hand with an expensive
operator, and the part no generic ASR API ships:

  * CPS (characters per second) — the broadcast reading-rate law. A cue that
    holds 90 characters for 2 seconds is unreadable no matter how correct it is.
  * Line measure and stacking — 42 characters, at most 2 lines, and a balanced
    break: a lone two-word tail line is a professional tell even when the
    character count passes.
  * Dwell and clearance — a cue must live long enough to be read twice and must
    not collide with its neighbour (a clash flickers, a sub-frame gap strobes).
  * Speaker integrity — a card never carries two voices, so a rewrap may not
    group words across a turn boundary found by `diarize`.

Every rule is auditable: `audit` reports the measured number behind each
finding, `reflow` returns the cards it produced, and the invariant *word for
word* is asserted by the tests — the engine may move a break and borrow silence,
but it may not add, drop or reorder one word of what was said.

Pure Python, no model, no network, deterministic: the same cue list always
produces the same layout, which is what makes it sellable to a studio that runs
the same file twice and expects the same deliverable.
"""
from __future__ import annotations

import math
import re

from .romanizer import normalize_uzbek
from . import nafis
from .srt import Cue, format_srt

# ─── the house style, as numbers ───────────────────────────────────────────────
MAX_CPS = 17.0          # characters (incl. spaces) per second on screen
MAX_CHARS_PER_LINE = 42
MAX_LINES = 2
MIN_DWELL = 0.8         # s: below this the cue cannot be read at all
MAX_DWELL = 7.0         # s: above this the cue outstays the speech
MIN_GAP = 0.08          # s: ~2 frames — below this the cut strobes
ORPHAN_MIN = 12         # chars: a shorter tail line than this is a bad break

# Tolerances for the audit, not for the repair. Times leave the engine rounded to
# milliseconds, so 0.800 - 0.001 is 0.7999999999999989 in binary float: without a
# hair of slack the engine flags its own output as a 1 ms overlap and a 0.7999 s
# blink, and a checker that cries over rounding noise is a checker nobody reruns.
TIME_TOL = 0.002        # s
CPS_TOL = 0.05          # chars/sec

_WORD_RE = re.compile(r"\S+")


def card_budget() -> int:
    """Display characters one card may hold at the house measure."""
    return MAX_CHARS_PER_LINE * MAX_LINES


# ─── text metrics ──────────────────────────────────────────────────────────────

def display_len(text: str) -> int:
    """Characters a reader's eye actually crosses: everything but the newline."""
    return len(text.replace("\n", ""))


def line_widths(text: str) -> list[int]:
    return [len(ln) for ln in text.split("\n")]


def cps(text: str, start: float, end: float) -> float:
    span = end - start
    if span <= 0:
        return float("inf")
    return display_len(text) / span


def rate_or_none(text: str, start: float, end: float) -> float | None:
    """`cps` for anything that leaves as JSON.

    `Infinity` is not a number JSON can carry, and this report is written to an
    artifact and served raw to the share page, where `response.json()` would die.
    A card nobody could dwell on has no reading rate to state — the audit files a
    `zero_span` finding for it — so `null` reports the honest absence instead of
    inventing a number or corrupting the document. The engine's own timing rules
    make repaired cards measurable; this is why the guarantee does not depend on
    that."""
    span = end - start
    return None if span <= 0 else round(display_len(text) / span, 2)


def _fold(text: str) -> str:
    """Script-neutral form, so the Uzbek connective rules fire on Cyrillic too."""
    return normalize_uzbek(text or "").lower()


# Words a line break may fall *before*: Uzbek clause junctions, both scripts.
_CONNECTIVES = frozenset(map(_fold, (
    "va", "hamda", "lekin", "ammo", "biroq", "yoki", "chunki", "sababli",
    "deb", "ki", "uchun", "bilan", "keyin", "ya'ni", "agar", "garchi",
    "shunda", "shuningdek", "qolaversa", "vasofiy",
)))
# Words that open an address turn — a soft break point before them.
_VOCATIVES = frozenset(map(_fold, ("ustoz", "aka", "opa", "do'stim", "hurmatli")))

_PUNCT_END = (",", ";", ":", "?", "!", "—", ")", "ʼ", "'")


def _break_cost(words: list[str], k: int, line_max: int | None = None) -> float:
    """Cost of breaking between words[k-1] and words[k]: lower is a nicer cut.

    Balance is the primary term (a lopsided break is the classic machine tell);
    punctuation and connectives discount it, because cutting *there* costs the
    reader less than the raw lengths suggest. `line_max` makes the measure a rule
    rather than a preference: a cut that puts more characters on screen than the
    house norm is not a nicer-looking version of a good one.

    The grammar law is Ovoz Nafis's, not this file's: `_CONNECTIVES` above is a
    *preference* (a discount), `nafis.penalty` is a *fault* (a forbidden cut that
    tears a postpositional phrase, a compound verb or a numeral from its unit).
    They are deliberately two lists — one is taste, the other is the language.
    The penalty is priced between the house measure (+400 for going wide) and
    ordinary balance noise, so a grammatical cut never wins by breaking the norm:
    width still outranks grammar, because a wide card cannot be read at all."""
    left = len(" ".join(words[:k]))
    right = len(" ".join(words[k:]))
    cost = abs(left - right) + abs(right - (left + right) / 2.0)
    cost += nafis.penalty(words, k)
    if words[k - 1].endswith(_PUNCT_END):
        cost -= 14
    if _fold(words[k]) in _CONNECTIVES:
        cost -= 12
    if _fold(words[k]) in _VOCATIVES:
        cost -= 8
    if words[k][:1].isupper():
        cost -= 4                      # a capital usually starts a new thought
    if right < ORPHAN_MIN:
        cost += (ORPHAN_MIN - right) * 3
    if line_max is not None and max(left, right) > line_max:
        cost += 400                    # wide on purpose is still wide
    if max(left, right) > card_budget():
        cost += 1000                   # a break that cannot hold the text is not one
    return cost


def rewrap(text: str, max_chars: int = MAX_CHARS_PER_LINE) -> str:
    """Stack one run of text into at most two balanced visual lines.

    Only whitespace changes: the token sequence is untouched, so a rewrap can
    never alter what was said — only where the eye gets to rest."""
    words = _WORD_RE.findall(text or "")
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    if len(" ".join(words)) <= max_chars:
        return " ".join(words)
    best_k, best_cost = 1, float("inf")
    for k in range(1, len(words)):
        cost = _break_cost(words, k, max_chars)
        if cost < best_cost - 1e-9:    # strict win; equal costs keep the earlier cut
            best_cost, best_k = cost, k
    return " ".join(words[:best_k]) + "\n" + " ".join(words[best_k:])


def _fit_words(words: list[str], budget: int) -> int:
    """How many leading words fit `budget` display characters."""
    used = 0
    for k, w in enumerate(words, start=1):
        used += len(w) + (1 if k > 1 else 0)
        if used > budget:
            return max(1, k - 1)
    return len(words)


def _cards_for(cue: Cue) -> list[tuple[str, int]]:
    """Split one cue into cards that fit the measure: (text, weight) pairs.

    A card is only created for real text, weights are display characters so the
    time split below is proportional to reading effort, and the break point is
    softened to the best junction nearby so a phrase is never cut in half."""
    words = _WORD_RE.findall(cue.text)
    if not words:
        return []
    budget = card_budget()
    groups: list[list[str]] = []
    rest = words
    while rest:
        take = _fit_words(rest, budget)
        if take < len(rest):
            take = _soften_break(rest, take)
        groups.append(rest[:take])
        rest = rest[take:]
    out: list[tuple[str, int]] = []
    for g in groups:
        out.extend(_split_card(g))
    return out


def _split_card(words: list[str]) -> list[tuple[str, int]]:
    """Cards for one run that already fits the two-line budget.

    When the measure still cannot be honoured — a 45-character word drags the
    break past 42 — the run becomes two cards instead of one wide one, because an
    engine whose own output trips the audit it sells has nothing left to report.
    Recursion ends on a single word, which is the only case the engine admits it
    cannot break."""
    text = rewrap(" ".join(words))
    if not text:
        return []
    if len(words) < 2 or max(line_widths(text)) <= MAX_CHARS_PER_LINE:
        return [(text, max(1, display_len(text)))]
    best_k, best_cost = 1, float("inf")
    for k in range(1, len(words)):
        cost = _break_cost(words, k)
        if cost < best_cost - 1e-9:
            best_cost, best_k = cost, k
    return _split_card(words[:best_k]) + _split_card(words[best_k:])


def _soften_break(words: list[str], take: int) -> int:
    """Move a hard fit boundary to the nicest junction within a few words.

    The window is bounded so a card can never balloon past the measure: only
    breaks that keep both halves inside the budget compete."""
    lo, hi = max(1, take - 4), min(len(words) - 1, take + 2)
    best, best_cost = take, _break_cost(words, take)
    for k in range(lo, hi + 1):
        cost = _break_cost(words, k)
        if cost < best_cost - 1e-9:
            best_cost, best = cost, k
    return best


# ─── audit ─────────────────────────────────────────────────────────────────────

def audit(cues: list[Cue], speakers: list[int] | None = None) -> dict:
    """Measure a cue list against the house style.

    Returns findings with the measurement behind each one, a 0-100 score and a
    per-rule histogram. Never mutates its input."""
    ordered = list(cues)
    findings: list[dict] = []

    def add(card: int, rule: str, detail: str, severity: str = "minor") -> None:
        findings.append({"card": card, "rule": rule, "detail": detail,
                         "severity": severity})

    for i, c in enumerate(ordered):
        n = i + 1
        if not display_len(c.text.strip()):
            add(n, "empty", "card holds no text", "major")
            continue
        widths = line_widths(c.text)
        rate = cps(c.text, c.start, c.end)
        dwell = c.end - c.start
        if rate == float("inf"):
            add(n, "zero_span", "end equals start", "major")
        elif rate > MAX_CPS + CPS_TOL:
            add(n, "fast", f"{rate:.1f} chars/sec (max {MAX_CPS:g})",
                "major" if rate > MAX_CPS * 1.4 + CPS_TOL else "minor")
        if max(widths) > MAX_CHARS_PER_LINE:
            add(n, "wide", f"line of {max(widths)} chars (max {MAX_CHARS_PER_LINE})",
                "major")
        if len(widths) > MAX_LINES:
            add(n, "tall", f"{len(widths)} lines (max {MAX_LINES})", "major")
        elif len(widths) == 2 and min(widths) < ORPHAN_MIN:
            add(n, "orphan", f"tail line of {min(widths)} chars", "minor")
        if dwell < MIN_DWELL - TIME_TOL:
            add(n, "blink", f"{dwell:.2f}s on screen (min {MIN_DWELL:g}s)", "major")
        elif dwell > MAX_DWELL + TIME_TOL:
            add(n, "linger", f"{dwell:.1f}s on screen", "minor")
        if i + 1 < len(ordered):
            nxt = ordered[i + 1]
            if c.end - nxt.start > TIME_TOL:
                add(n, "clash",
                    f"overlaps card {i + 2} by {c.end - nxt.start:.2f}s", "major")
            elif nxt.start - c.end < MIN_GAP - TIME_TOL:
                add(n, "bump", f"{nxt.start - c.end:.3f}s between cards", "minor")
        if speakers is not None and i + 1 < len(speakers) and i < len(speakers):
            if dwell < 1.0 and speakers[i] and speakers[i + 1] != speakers[i]:
                add(n, "speaker_flash",
                    f"voice {speakers[i]} shown {dwell:.2f}s before a change",
                    "minor")

    major = sum(1 for f in findings if f["severity"] == "major")
    minor = len(findings) - major
    score = 0.0 if not ordered else max(0.0, round(100.0 - 9.0 * major - 2.0 * minor, 1))
    live = [c for c in ordered if display_len(c.text.strip())]
    # A card with no measurable dwell has no reading rate to report — its own
    # `zero_span` finding is the honest statement. Folding `inf` in here would put
    # a bare `Infinity` into layout.json: not valid JSON, served raw as an artifact,
    # and it kills the share page's response.json() on the client.
    measurable = [c for c in live if c.end > c.start]
    return {
        "score": score,
        "grade": _grade(score),
        "cards": len(ordered),
        "findings": findings,
        "by_rule": _hist(findings),
        "peak_cps": round(max((cps(c.text, c.start, c.end) for c in measurable),
                              default=0.0), 2),
        "ms_per_char": _ms_per_char(ordered),
    }


def _grade(score: float) -> str:
    return ("A" if score >= 95 else "B" if score >= 85
            else "C" if score >= 70 else "D" if score >= 40 else "F")


def _hist(findings: list[dict]) -> dict:
    out: dict[str, int] = {}
    for f in findings:
        out[f["rule"]] = out.get(f["rule"], 0) + 1
    return dict(sorted(out.items()))


def _ms_per_char(cues: list[Cue]) -> float:
    """Milliseconds of screen time per displayed character — the number a subtitle
    buyer checks intuitively (250-400 ms reads comfortable)."""
    live = [c for c in cues if c.end > c.start and display_len(c.text.strip())]
    if not live:
        return 0.0
    chars = sum(display_len(c.text) for c in live)
    return round(sum(c.end - c.start for c in live) * 1000.0 / chars, 1)


# ─── repair ────────────────────────────────────────────────────────────────────

MAX_DRIFT = 1.0         # s: how far a card may slide from where it was said


def _ms_up(value: float) -> float:
    """Round a time outwards, to the next millisecond.

    Durations are only ever rounded *up*: rounding a 4.0006s dwell down to 4.000
    puts a card a hair over the rate law it was just sized to, and an engine that
    flags its own output is not an engine a studio can trust."""
    return math.ceil(value * 1000.0 - 1e-9) / 1000.0


def _plan(cues: list[Cue], speakers: list[int] | None, prefer: str = "readability"):
    """The whole repair in one deterministic pass, under one of two policies.

    `readability` gives every card the time the reading-rate law demands and lets
    the tail slide: correct for sparse speech, and the right first attempt.
    `sync` keeps each card inside the window its own line owns, scaling the dwell
    down when the window is too small: the result may break the rate law (and
    says so), but it never loses the tape. A studio can ship the second; nobody
    ships the first.

    Returns (cards, owners, max_drift). The drift is what lets `polish` choose."""
    order = sorted(range(len(cues)), key=lambda i: (cues[i].start, cues[i].end))
    ordered = [cues[i] for i in order]

    # The window a line may occupy: up to the clearance before the next voice
    # takes over. This is the hard budget the timeline gives the subtitle.
    windows: list[tuple[float, float]] = []
    for pos, c in enumerate(ordered):
        lo = c.start
        hi = (ordered[pos + 1].start - MIN_GAP if pos + 1 < len(ordered)
              else c.end + MIN_DWELL)
        windows.append((lo, max(hi, lo + 0.2)))

    # (text, ideal start, dwell, owner) per card, before the policy is applied
    raw: list[tuple[str, float, float, int, int]] = []   # + parent position
    for pos, c in enumerate(ordered):
        cards = _cards_for(c)
        if not cards:
            continue
        owner = 0 if speakers is None or order[pos] >= len(speakers) \
            else speakers[order[pos]]
        span = max(c.end - c.start, 0.0)
        total_w = sum(w for _, w in cards) or 1.0
        t = c.start
        for text, w in cards:
            share = (span * w / total_w) if span > 0 else MIN_DWELL
            # The dwell a card is *owed*: the reading-rate law is the binding
            # constraint and MIN_DWELL only catches short lines. A cue crammed
            # with 130 characters in 0.4s is physically unreadable, so the card
            # demands w/MAX_CPS seconds and the policy decides who pays for them.
            owed = min(MAX_DWELL, max(MIN_DWELL, share, w / MAX_CPS))
            raw.append((text, t, owed, owner, pos))
            t += share
    if not raw:
        return [], [], 0.0

    if prefer == "sync":
        raw = _fit_to_windows(raw, windows)

    out: list[Cue] = []
    owners: list[int] = []
    drift = 0.0
    cursor = raw[0][1]
    for text, ideal, dwell, owner, _pos in raw:
        # One clearance rule for both policies. `sync` sizes its windows to leave
        # the gap, but a card held up by the 50 ms floor can still overrun into
        # the next line's in-point by a few milliseconds, and "at least MIN_GAP
        # between cards" is either a guarantee or it is not.
        start = max(cursor, ideal)
        end = _ms_up(start + dwell)
        drift = max(drift, start - ideal, end - _parent_end(windows, _pos))
        out.append(Cue(len(out) + 1, round(start, 3), end, rewrap(text)))
        owners.append(owner)
        cursor = end + MIN_GAP + 0.002
    return out, owners, round(max(drift, 0.0), 3)


def _parent_end(windows: list[tuple[float, float]], pos: int) -> float:
    """The latest a card may still be on screen without stepping on the next line.
    Sliding *into* the following silence is allowed and normal; overrunning it is
    the desync the drift metric measures."""
    return windows[pos][1]


def _fit_to_windows(raw, windows):
    """Scale each line's cards into its own window, proportionally to reading need.

    Every card keeps a strictly positive dwell, the order never changes and no
    card crosses its window, so the result is in sync by construction. The
    clearance between *sibling* cards of one split line is paid for out of the
    window as well: two cards butted at 0.000 s apart read as one card that
    flashes once and again, which is the exact artefact this engine exists to
    remove."""
    by_parent: dict[int, list[int]] = {}
    for i, (_t, _s, _d, _o, pos) in enumerate(raw):
        by_parent.setdefault(pos, []).append(i)
    fixed: list[tuple[str, float, float, int, int]] = list(raw)
    for pos, idxs in by_parent.items():
        lo, hi = windows[pos]
        room = max(hi - lo - MIN_GAP * max(0, len(idxs) - 1), 0.01)
        owed = [raw[i][2] for i in idxs]
        total = sum(owed)
        scale = min(1.0, room / total) if total > 0 else 1.0
        t = lo
        for n, (i, d) in enumerate(zip(idxs, owed)):
            dwell = max(0.05, d * scale)
            fixed[i] = (raw[i][0], t, dwell, raw[i][3], pos)
            t = _ms_up(t + dwell + (MIN_GAP + 0.002 if n + 1 < len(idxs) else 0.0))
    return fixed


def reflow(cues: list[Cue], speakers: list[int] | None = None,
           with_owners: bool = False, prefer: str = "readability"):
    """Rebuild a cue list into readable cards.

    Guarantees, each one a test:
      * word for word — the token stream is preserved exactly, in order;
      * monotonic — start < end and no card overlaps the next;
      * clearance — at least MIN_GAP between neighbours;
      * dwell — no card is shorter than 50 ms, so nothing flickers;
      * attribution — when `speakers` is passed, `with_owners=True` returns the
        owner of every output card (a split card keeps its parent's voice).
    """
    out, owners, _ = _plan(cues, speakers, prefer=prefer)
    if speakers is not None or with_owners:
        return out, owners
    return out


def tokens(cues: list[Cue]) -> list[str]:
    """The word stream a cue list carries — the invariant everything is checked
    against: reflow may move breaks, never words."""
    out: list[str] = []
    for c in cues:
        out.extend(_WORD_RE.findall(c.text))
    return out


# ─── one-call surface ──────────────────────────────────────────────────────────

def _layout(cues: list[Cue], speakers: list[int] | None, prefer: str):
    """One policy's deliverable: cards, owners, drift and the scored audit.

    The desync penalty lives *here*, not in the caller, so two policies are
    compared on the same ruler. Scoring it outside was the first bug of this
    engine: the readable-but-late variant always looked perfect (100) next to a
    variant with honest `fast` findings, so the deliverable nobody can ship won
    every single time."""
    fixed, owners, drift = _plan(cues, speakers, prefer=prefer)
    report = audit(fixed, owners)
    if drift > MAX_DRIFT:
        report["findings"].append({
            "card": len(fixed) or 1, "rule": "late",
            "detail": f"cards run up to {drift:.1f}s behind the tape "
                      f"(max {MAX_DRIFT:g}s)", "severity": "major"})
        report["by_rule"] = _hist(report["findings"])
        report["score"] = max(0.0, round(report["score"] - 9.0, 1))
        report["grade"] = _grade(report["score"])
    return fixed, owners, drift, report


def polish(cues: list[Cue], speakers: list[int] | None = None) -> dict:
    """The public answer: the audit, the repaired cards, the audit of the result
    and the SRT a customer can drop straight into a player.

    The policy rule is decisive, not a score beauty contest. Readability is
    attempted first because a subtitle exists to be read. If it would leave the
    tail more than MAX_DRIFT behind the tape, sync wins outright — a subtitle
    that appears three seconds after the line was spoken is a wrong subtitle no
    matter how comfortably it reads. Only when *both* policies lose sync does the
    score decide, and the losing trade-off is reported either way."""
    before = audit(cues, speakers)
    fixed, owners, drift, after = _layout(cues, speakers, "readability")
    mode = "readability"
    if drift > MAX_DRIFT:
        s_fixed, s_owners, s_drift, s_report = _layout(cues, speakers, "sync")
        if s_drift <= MAX_DRIFT or s_report["score"] > after["score"]:
            fixed, owners, drift, after = s_fixed, s_owners, s_drift, s_report
            mode = "sync"
    doc = format_srt(fixed)
    return {
        "before": before,
        "after": after,
        "cards": [{"i": c.index, "start": c.start, "end": c.end,
                   "lines": line_widths(c.text),
                   "cps": rate_or_none(c.text, c.start, c.end),
                   "speaker": owners[c.index - 1] if speakers is not None else 0,
                   "text": c.text} for c in fixed],
        "srt": doc,
        "cards_before": len(cues),
        "cards_after": len(fixed),
        # Drift is reported, not hidden: a card that had to slide further than
        # MAX_DRIFT means the source timeline itself has no reading room, and the
        # honest answer is "this needs real speech timestamps", not a green tick.
        "max_drift": drift,
        "in_sync": drift <= MAX_DRIFT,
        "mode": mode,
        "words_preserved": tokens(fixed) == tokens(cues),
        "changed": doc != format_srt(list(cues)),
        "delta": round(after["score"] - before["score"], 1),
    }
