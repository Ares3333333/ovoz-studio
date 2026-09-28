"""Ovoz Jimlik — silence-gap boundary aligner (the fourth moat layer).

`diarize` answers *who* spoke, `layout` answers *can it be read*. Neither one
listens to the tape, and that is the hole this engine fills: a cut placed at
00:12.418 because the ASR happened to emit a segment there is a cut in the middle
of a syllable. The reader experiences it as a blink; an operator calls it "not in
sync" and redoes the job by hand — which is exactly the labour this product is
supposed to remove.

Ovoz Jimlik finds the pauses the speaker actually took and moves cue boundaries
*into* them:

  * energy envelope, own code: 20 ms RMS frames over honest PCM, no model, no
    library, no network — the same determinism the rest of `app/ling` sells;
  * a Schmitt trigger, not a threshold: a level halfway between floor and voice
    cannot make a frame flip back and forth, so pauses come out as whole regions
    instead of confetti;
  * the floor is measured from the tape (10th percentile), so a quiet room and a
    loud room are both read correctly, and an *empty* tape is recognised as empty
    rather than invented as speech;
  * a boundary may travel a bounded distance (`max_shift`) and no further: a cut
    3 s away from the text it belongs to is a wrong subtitle, however silent.

The contract, held by tests:

  * **text is never touched** — cue count, order and words come back identical;
  * **never worse than in** — total overlap between cards after alignment is not
    greater than before; a pause nobody asked about is left alone;
  * **already-correct input is a no-op** — a boundary sitting inside its pause
    does not move by one millisecond;
  * **a back-to-back cut moves as one cut** — where `cue.end == next.start` (the
    shape every ASR exports), both names land on the same millisecond, so the text
    stays continuous and the line does not drop in the middle of a word;
  * **silence-free signal snaps nothing** — continuous audio yields `moved: 0`
    and says so, instead of hallucinating a pause;
  * **refuse, don't guess** — compressed, float, absurd-rate or over-long audio
    comes back as an `AlignError` with a code, never as a confident lie;
  * **every number leaving here is finite** — this report is served raw as an
    artifact, so `NaN`/`Infinity` would break `response.json()` on the client.
"""
from __future__ import annotations

import io
import math
import wave
from array import array
from dataclasses import dataclass

from .srt import Cue, format_srt

# ─── analysis law, as numbers ──────────────────────────────────────────────────
FRAME_MS = 20.0            # one analysis frame: 3 frames ≈ one blink
MIN_GAP_SEC = 0.060        # a shorter pause cannot hold a cut
BOUNDARY_PAD = 0.030       # stay off the speech edge: a cut *on* the edge flashes
MIN_SHIFT = 0.020          # below this the boundary already sits in the pause
MIN_SPAN = 0.200           # never squeeze a cue below two frames of dwell
MIN_SEP = 0.020            # two cards may not share a frame (also beats ms-rounding)
MAX_SHIFT_DEFAULT = 0.60   # how far a cut may travel to reach real silence
MAX_SHIFT_LIMIT = 2.00     # beyond this we are rewriting sync, not fixing it
MAX_SHIFT_MIN = 0.050

NOISE_FLOOR_PCT = 0.10     # percentile of the tape that counts as "room tone"
SPEECH_LEVEL_PCT = 0.90    # percentile that counts as "someone is talking"
# The gate is placed in decibels, never as a ratio between two percentiles: the
# loudness of a *word* swings 6-10 dB syllable to syllable, while a real pause on
# a tape sits 20-40 dB under the voice. A gate computed from the spread of the level
# distribution alone landed inside the syllable dip and filed a consonant as a
# pause — which is worse than hearing nothing, because it moves a cut into a word.
# So the gate is the midpoint of the tape's own quiet band, clamped between two
# hard limits: never closer than 6 dB to the room tone, never less than 12 dB under
# the speaking level. A tape whose whole dynamic range is 6 dB (a steady tone) then
# gets a gate below everything on it — one run, no pauses. That is the right answer.
GATE_ABOVE_FLOOR_DB = 6.0    # never so low that room tone reads as speech
GATE_BELOW_SPEECH_DB = 12.0  # never so high that a syllable dip reads as a pause
HYST_DB = 3.0                # the hold band between opening and closing a run
DYN_CAP_DB = 40.0            # beyond this, extra range is digital silence
MIN_SPEECH_DB = -46.0        # ≈ a disconnected mic: nothing here is speech
MAX_SPAN_LOSS = 0.40         # a cue may not lose more than 40% of its dwell

# ─── ceilings for a public, unauthenticated call ──────────────────────────────
RATE_MIN, RATE_MAX = 6_000, 192_000
CHANNELS_MAX = 8
# The window is bounded in TIME and, separately, in SAMPLES, because those answer
# different questions. Both listeners read *every* sample of every 20 ms frame:
# thinning the tape was the obvious next move and was rejected after measuring it —
# the envelope pass costs 0.13 s at 200 s and 0.60 s at 900 s of 8 kHz mono, so a
# 1.6 M-sample cap was refusing every job over three and a half minutes to save
# about half a second. A ceiling that buys nothing is not a ceiling, it is a bug.
MAX_LISTEN_SEC = 900.0          # fifteen minutes of tape: what a listener reads
MAX_LISTEN_SAMPLES = 9_600_000  # memory: the same window up to 16 kHz
# The library's own ceiling, sized for the pipeline's longest legal job. The public
# endpoint is far stricter (`_LING_MAX_LINES`) because there the caller is anonymous;
# a job already paid for must not be refused for a number that costs nothing extra.
MAX_CUES = 2_000
MAX_REPORT_GAPS = 200
MAX_REPORT_MOVES = 400
MIN_SAMPLES = 800          # below ~0.1 s there is no pause to measure
FULL_SCALE = 32768.0


class AlignError(ValueError):
    """The aligner's way of saying no. `code` is what the API turns into a
    machine-readable refusal: a client that sent MP3 needs to hear "send PCM",
    not read a stack trace about the `wave` module."""

    def __init__(self, message: str, code: str = "invalid_audio"):
        super().__init__(message)
        self.code = code


def check_listen_window(n: int, rate: int, err: type = AlignError) -> None:
    """Refuse a tape nobody will listen to, in the unit the caller can act on.

    One law for both listeners and for the WAV reader, so `align` and `words` can
    never disagree about how much of the same recording they heard. Which ceiling
    binds depends on the rate: at the pipeline's 8 kHz the *time* window does
    (900 s = 7.2 M samples), while 15 minutes at 48 kHz is 172 MB of integers and
    hits the *sample* cap at 200 s. The message names whichever it was, because
    "send a shorter tape" and "resample" are different instructions.

    `err` lets the caller refuse in its own class: a client catching the word
    engine's error must still catch this one, or the shared law would leak a
    different exception type depending on which ceiling bound."""
    if n > MAX_LISTEN_SAMPLES:
        raise err(
            f"audio too long: {n} samples, max {MAX_LISTEN_SAMPLES} "
            f"({MAX_LISTEN_SEC:.0f} s of tape only fits below "
            f"{MAX_LISTEN_SAMPLES // int(MAX_LISTEN_SEC)} Hz)", "too_long")
    if n / rate > MAX_LISTEN_SEC:
        raise err(f"audio window too long ({n / rate:.1f} s, max "
                  f"{MAX_LISTEN_SEC:.0f} s)", "too_long")


@dataclass
class Gap:
    """One region of the tape where nobody is speaking, in seconds."""
    start: float
    end: float

    @property
    def dur(self) -> float:
        return self.end - self.start


# ─── reading the tape ─────────────────────────────────────────────────────────

def _read_fmt(data: bytes) -> dict:
    """The `fmt ` chunk, read by hand.

    `wave` derives the sample width it reports from the file's own fields, and a
    real-world WAV disagrees with itself often enough to matter: a 32-bit file
    came back as `sampwidth=1`, i.e. 8-bit, and the energy curve built from it
    looked perfectly plausible while being garbage. So the declared bit depth and
    block alignment are checked here, against each other and against `wave`,
    before a single sample is trusted."""
    if len(data) < 44 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AlignError("not a WAV file: RIFF/WAVE header missing", "not_wav")
    if data[12:16] != b"fmt ":
        raise AlignError("WAV has no 'fmt ' chunk where it belongs", "not_wav")
    fmt = int.from_bytes(data[20:22], "little")
    ch = int.from_bytes(data[22:24], "little")
    rate = int.from_bytes(data[24:28], "little")
    align_ = int.from_bytes(data[32:34], "little")
    bits = int.from_bytes(data[34:36], "little")
    if fmt != 1:
        # 3 = IEEE float, 6/7 = G.711, 0x11 = ADPCM, 0xFFFE = extensible (which
        # carries its real format 8 bytes later, in a chunk `wave` may not even
        # read). None of them is a stream of integers we can measure.
        raise AlignError(f"WAV format tag {fmt:#06x} is not integer PCM: "
                         f"send uncompressed 16-bit PCM WAV", "compressed")
    if bits not in (8, 16):
        raise AlignError(f"{bits}-bit WAV is not supported: send 16-bit PCM",
                         "bad_depth")
    if not 1 <= ch <= CHANNELS_MAX:
        raise AlignError(f"{ch} channels is not supported", "bad_channels")
    if align_ != ch * bits // 8:
        raise AlignError("WAV block alignment contradicts its channels and depth",
                         "bad_header")
    if not RATE_MIN <= rate <= RATE_MAX:
        raise AlignError(f"sample rate {rate} Hz is outside "
                         f"{RATE_MIN}-{RATE_MAX} Hz", "bad_rate")
    return {"channels": ch, "rate": rate, "width": bits // 8, "bits": bits}


def pcm_from_wav(data: bytes, max_bytes: int = 0) -> tuple[array, int]:
    """Uncompressed PCM out of a WAV container, `wave` and nothing else.

    8- and 16-bit integer PCM are accepted and mixed down to mono at the 16-bit
    scale; everything else is refused by name. 24-bit and 32-bit files are
    rejected on purpose: a 32-bit WAV is IEEE float far more often than integer,
    and reading a float stream as integers yields an energy curve that is
    *plausible-looking garbage* — the worst possible failure for an engine whose
    whole claim is that it heard the pause."""
    if not isinstance(data, (bytes, bytearray)):
        raise AlignError("audio must be bytes", "bad_input")
    if max_bytes and len(data) > max_bytes:
        raise AlignError(f"audio larger than {max_bytes} bytes", "too_long")
    head = _read_fmt(data)
    try:
        wf = wave.open(io.BytesIO(bytes(data)), "rb")
    except (wave.Error, EOFError):
        raise AlignError("not a readable WAV file: send PCM WAV", "not_wav")
    with wf:
        sw, ch, rate, n = (wf.getsampwidth(), wf.getnchannels(),
                           wf.getframerate(), wf.getnframes())
        if (sw, ch, rate) != (head["width"], head["channels"], head["rate"]):
            # The header and the library disagree: believe neither.
            raise AlignError("WAV header and audio parameters disagree",
                             "bad_header")
        if wf.getcomptype() != "NONE":
            raise AlignError(f"compressed audio ({wf.getcomptype()}): "
                             f"send uncompressed PCM WAV", "compressed")
        if n <= 0:
            raise AlignError("WAV carries no samples", "empty")
        # Before a single frame is read into memory: the ceiling exists to keep an
        # anonymous call from making us decode a tape we will refuse anyway.
        check_listen_window(n, rate)
        raw = wf.readframes(n)
    if sw == 2:
        flat = array("h")
        flat.frombytes(raw[:len(raw) - len(raw) % 2])
    else:                                    # unsigned 8-bit → 16-bit scale
        # Генератор, а не list-comprehension: `array()` принимает итератор, и
        # промежуточный список из 5 000 000 указателей (плюс столько же отдельных
        # PyLong вне кэша −5…256) не материализуется. На ленте в 5 МБ это разница
        # между ~5 МБ и ~190 МБ временного RSS на анонимный запрос.
        flat = array("h", ((b - 128) * 256 for b in raw))
    if ch == 1:
        return flat, rate
    mono = array("h", bytes(2 * (len(flat) // ch)))
    for i in range(len(mono)):
        acc = 0
        for c in range(ch):
            acc += flat[i * ch + c]
        mono[i] = acc // ch
    return mono, rate


def frame_levels(pcm, frame_len: int) -> list[float]:
    """RMS of every full frame, 0..1 of full scale. One pass, no FFT: the pause
    question is a loudness question, and paying for a spectrum would buy noise.

    Every sample is read, at every rate, for any tape inside the window. This is
    deliberate: a thinned envelope is an *estimate* of a frame's loudness, and an
    estimate that misses an inter-word pause moves a cut inside a word — the one
    failure this engine exists to prevent. The pass is a C-level slice per frame
    and a 160-element sum, which measured 0.6 s over fifteen minutes: exactness is
    affordable here, so `tests/test_align.py` forbids a third slice index rather
    than trusting nobody will add one later."""
    out: list[float] = []
    n = len(pcm)
    for a in range(0, n - frame_len + 1, frame_len):
        chunk = pcm[a:a + frame_len]
        energy = sum(x * x for x in chunk) / len(chunk)
        out.append(math.sqrt(energy) / FULL_SCALE)
    return out


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    return sorted_vals[round(p * (len(sorted_vals) - 1))]


def _db(x: float) -> float:
    """RMS → dBFS. The clamp is the floor of the measurable world, not a fudge:
    digital silence is 0.0, and `log10(0)` would take the gate to -∞ and hand the
    next comparison a NaN-shaped answer."""
    return 20.0 * math.log10(max(x, 1e-9))


def frame_curve(pcm, rate: int) -> dict:
    """The tape as one listener hears it: an RMS pass and the gate derived from
    *this* recording alone.

    Jimlik asks where speech stops; So'z asks where each word sits inside a line.
    Two engines reading two different envelopes would produce two incompatible
    stories about the same file — a word boundary landing in a frame the aligner
    called speech is a bug nobody can reproduce. So the gate is computed once,
    here, and shared. The clamping order is load-bearing: on a tape with no
    dynamics `floor + 6 dB` sits *above* the speaking level, and a gate up there
    cuts speech into confetti."""
    frame_len = max(1, round(rate * FRAME_MS / 1000.0))
    levels = frame_levels(pcm, frame_len)
    if not levels:
        raise AlignError("audio shorter than one analysis frame", "too_short")
    ordered = sorted(levels)
    floor = _percentile(ordered, NOISE_FLOOR_PCT)
    top = _percentile(ordered, SPEECH_LEVEL_PCT)
    n_db, s_db = _db(floor), _db(top)
    mid = (max(n_db, s_db - DYN_CAP_DB) + s_db) / 2.0
    gate_db = min(max(mid, n_db + GATE_ABOVE_FLOOR_DB),
                  s_db - GATE_BELOW_SPEECH_DB)
    hi = 10.0 ** (gate_db / 20.0)
    lo = 10.0 ** ((gate_db - HYST_DB) / 20.0)
    return {
        "levels": levels,
        "frame_len": frame_len,
        "frame_s": frame_len / rate,
        "duration": len(pcm) / rate,
        "noise_floor": floor,
        "speech_level": hi,      # a frame at or above this is being spoken
        "hold_level": lo,        # and it stays speech until it falls below this
        "gate_db": round(gate_db, 3),
        "tape_db": round(s_db, 3),
        # A tape whose loudest frames are still mic hiss carries no speech to
        # place words in. Saying so is the difference between an empty answer
        # and a fabricated one.
        "silent_tape": s_db < MIN_SPEECH_DB,
    }


def speech_runs(curve: dict) -> list[tuple[int, int]]:
    """Frame indices where somebody is talking, as whole runs.

    The Schmitt pair (`speech_level` / `hold_level`) is what makes a run a region
    instead of a frame: a vowel decaying through the gate would otherwise be read
    as speech-silence-speech-silence, and both listeners would then be placing
    cuts in the tail of a sound that never stopped."""
    if curve["silent_tape"]:
        return []
    hi, lo = curve["speech_level"], curve["hold_level"]
    runs: list[tuple[int, int]] = []
    open_at = -1
    live = False
    for i, lv in enumerate(curve["levels"]):
        if not live and lv >= hi:
            live, open_at = True, i
        elif live and lv < lo:
            live = False
            runs.append((open_at, i - 1))
    if live:
        runs.append((open_at, len(curve["levels"]) - 1))
    return runs


def detect_gaps(pcm, rate: int) -> dict:
    """Where the tape is quiet, measured from the tape itself.

    Two thresholds, not one: frames at or above `hi` open speech, frames below
    `lo` close it, and anything in between keeps the previous state. A single
    threshold turns the tail of a decaying vowel into a dozen one-frame "pauses"
    and the aligner then reports silence it never heard."""
    curve = frame_curve(pcm, rate)
    levels, frame_s = curve["levels"], curve["frame_s"]
    hi, lo, floor = curve["speech_level"], curve["hold_level"], curve["noise_floor"]
    duration = curve["duration"]

    if curve["silent_tape"]:
        return {"gaps": [], "duration": round(duration, 3), "frames": len(levels),
                "frame_len": curve["frame_len"], "speech_sec": 0.0,
                "silence_sec": round(duration, 3), "noise_floor": round(floor, 5),
                "speech_level": round(hi, 5), "hold_level": round(lo, 5),
                "speech_runs": 0, "dead_air": True}

    runs = speech_runs(curve)

    gaps: list[Gap] = []
    prev = 0
    for s, e in runs:
        gaps.append(Gap(round(prev * frame_s, 3), round(s * frame_s, 3)))
        prev = e + 1
    gaps.append(Gap(round(prev * frame_s, 3), round(duration, 3)))
    usable = [g for g in gaps if g.dur >= MIN_GAP_SEC]
    speech_sec = round(sum((e - s + 1) * frame_s for s, e in runs), 3)
    return {
        "gaps": usable,
        "duration": round(duration, 3),
        "frames": len(levels),
        "frame_len": curve["frame_len"],
        "speech_sec": speech_sec,
        "silence_sec": round(max(0.0, duration - speech_sec), 3),
        "noise_floor": round(floor, 5),
        "speech_level": round(hi, 5),
        "hold_level": round(lo, 5),
        "speech_runs": len(runs),
        # A tape with no run above `hi` carries no speech to align to. Saying so
        # is the difference between "nothing moved" and "I found nothing".
        "dead_air": not runs,
    }


# ─── the snap ─────────────────────────────────────────────────────────────────

def _target(g: Gap, t: float, pad: float = BOUNDARY_PAD) -> tuple[float, float] | None:
    """The stretch inside a pause where a cut may legally sit."""
    a, b = g.start + pad, g.end - pad
    return (a, b) if b > a else None


def _in_gap(g: Gap, start: float, end: float) -> bool:
    """Is a whole card inside one pause? Its edges may each be "perfectly placed"
    and still produce a caption that shows while nobody speaks and hides for the
    entire line it translates — the failure mode a per-boundary score cannot see."""
    a, b = _target(g, start) or (0.0, 0.0)
    return a <= start <= b and a <= end <= b


def snap_one(t: float, gaps: list[Gap], max_shift: float,
             low: float, high: float) -> tuple[float, Gap | None, bool]:
    """Nearest legal point of real silence for one boundary.

    `low`/`high` are the neighbours' claims on this moment, applied *before* the
    distance test: a pause that is reachable in 0.4 s but walled off by the
    previous card is not reachable, and reporting the move anyway would be the
    engine praising itself for an overlap it just created. The in-silence answer is
    still given when the window is empty — "this cut already sits in a pause, but
    its neighbour owns that moment" is a different fact from "this cut is on top of
    a word", and the report must not flatten them."""
    lo_w, hi_w = max(t - max_shift, low), min(t + max_shift, high)
    room = hi_w >= lo_w
    best_t, best_g, best_d = t, None, float("inf")
    in_place = False
    for g in gaps:
        iv = _target(g, t)
        if iv is None:
            continue
        a, b = iv
        if a <= t <= b:
            in_place = True
        if not room:
            continue
        a, b = max(a, lo_w), min(b, hi_w)
        if b < a:
            continue
        cand = min(max(t, a), b)
        d = abs(cand - t)
        if d < MIN_SHIFT:
            continue            # the cut is already where the speaker paused
        if d < best_d - 1e-9 or (abs(d - best_d) <= 1e-9 and cand < best_t):
            best_t, best_g, best_d = cand, g, d
    return (t, None, in_place) if best_g is None else (best_t, best_g, in_place)


def _overlap_sec(spans: list[tuple[float, float]]) -> float:
    """Total time two cards are on screen at once. Reported before and after so
    "we never made it worse" is a measurement, not a promise."""
    return round(sum(max(0.0, prev - s)
                     for prev, s in zip((e for _, e in spans[:-1]),
                                        (st for st, _ in spans[1:]))), 6)


def _ms(x: float) -> float:
    """Time as it leaves the engine: milliseconds, always finite.

    `round()` on a float is enough here because every value was produced by
    arithmetic on finite inputs and clamped into a window; unlike a rate, no
    division by a possibly-zero span exists on this path."""
    return round(x + 0.0, 3)


# ─── public entry ─────────────────────────────────────────────────────────────

def align(pcm, rate: int, cues: list[Cue],
          max_shift: float = MAX_SHIFT_DEFAULT) -> dict:
    """Move cue boundaries into the pauses the speaker actually took.

    Single left-to-right pass, so a decision is made once against facts already
    fixed to its left: the result cannot depend on look-ahead heuristics and
    therefore cannot drift between runs. Text is copied verbatim into the output
    cues — this function has no authority over words."""
    if not cues:
        raise AlignError("no cues to align", "no_cues")
    if len(cues) > MAX_CUES:
        raise AlignError(f"too many cues ({len(cues)}, max {MAX_CUES})",
                         "too_many_cues")
    try:
        shift = float(max_shift)
    except (TypeError, ValueError):
        raise AlignError("max_shift must be a number of seconds", "bad_shift")
    if not math.isfinite(shift) or not MAX_SHIFT_MIN <= shift <= MAX_SHIFT_LIMIT:
        raise AlignError(f"max_shift must be between {MAX_SHIFT_MIN} and "
                         f"{MAX_SHIFT_LIMIT} seconds", "bad_shift")
    if rate is None or not isinstance(rate, int) or not RATE_MIN <= rate <= RATE_MAX:
        raise AlignError(f"sample rate must be an integer {RATE_MIN}-{RATE_MAX} Hz",
                         "bad_rate")
    n = len(pcm)
    if n < MIN_SAMPLES:
        raise AlignError(f"audio shorter than {MIN_SAMPLES} samples cannot be "
                         f"measured", "too_short")
    check_listen_window(n, rate)

    read = detect_gaps(pcm, rate)
    gaps, duration = read["gaps"], read["duration"]

    out: list[Cue] = []
    moves: list[dict] = []
    moved = in_silence = already = beyond = 0
    applied: list[float] = []
    prev_end = 0.0

    def fallback(start: float, ne: float) -> float:
        """The moment a cue may be handed back to without stepping onto the
        previous cue's new end. A shared cut moves the left edge of one cue past the
        start of the next, so a naive "undo" to the original start would invent the
        very overlap the report promises never to create."""
        return max(start, min(prev_end, ne - MIN_SEP))

    for i, c in enumerate(cues):
        nxt = cues[i + 1] if i + 1 < len(cues) else None
        start, end = float(c.start), float(c.end)
        ns, gs, sil_s = snap_one(start, gaps, shift, prev_end, end - MIN_SPAN)
        # `cue.end == next.start` is one cut with two names, and that is exactly
        # what every ASR and every editor exports. Walling the left name by the
        # right one's *unmoved* position means the cut can never travel: only the
        # later cue's start slides into the pause, the first line still drops in the
        # middle of a word, and a blank flashes where the text used to be
        # continuous. So a shared cut is measured against the pair — it may reach
        # into the next cue as far as that cue's own survival allows (a full
        # `MIN_SPAN` of dwell and at most `MAX_SPAN_LOSS` of its time) — and the
        # next cue's start is then walled from below by where this cut landed, which
        # is why both edges finish on the same millisecond.
        if nxt is not None and abs(float(nxt.start) - end) <= 1e-6:
            nxt_end = float(nxt.end)
            edge_hi = min(nxt_end - MIN_SPAN, end + (nxt_end - end) * MAX_SPAN_LOSS)
            edge_lo = max(ns + MIN_SPAN,
                          ns + (end - start) * (1.0 - MAX_SPAN_LOSS))
        else:
            edge_hi = float(nxt.start) if nxt is not None else duration
            edge_lo = ns + MIN_SPAN
        ne, ge, sil_e = snap_one(end, gaps, shift, edge_lo, edge_hi)
        # A card whose first and last frame both fall in the *same* pause has been
        # swallowed by it: the text would be on screen only while nobody speaks and
        # hidden for the whole line it translates. Alignment never creates that
        # state — the move responsible is undone, and if both are, the cue stays put
        # exactly as the client wrote it.
        for g in gaps:
            if not (_in_gap(g, ns, ne) and not _in_gap(g, start, end)):
                continue
            end_free = not _in_gap(g, ns, end)     # new start + old end: no collapse
            start_free = not _in_gap(g, start, ne)
            if gs is not None and end_free:
                ne, ge = end, None
            elif ge is not None and start_free:
                ns, gs = fallback(start, ne), None
            else:
                ns, ne, gs, ge = fallback(start, end), end, None, None
            break
        # The same argument in degrees: silence is not a reason to destroy dwell.
        # A cue may lose at most MAX_SPAN_LOSS of the time it had; past that the
        # move is worth less than the readability it costs, and `layout` downstream
        # would have to invent time to put back.
        if ne - ns < (end - start) * (1.0 - MAX_SPAN_LOSS):
            if end - ns >= (end - start) * (1.0 - MAX_SPAN_LOSS):
                ne, ge = end, None
            elif ne - start >= (end - start) * (1.0 - MAX_SPAN_LOSS):
                ns, gs = fallback(start, ne), None
            else:
                ns, ne, gs, ge = fallback(start, end), end, None, None
        # Three different facts, and flattening them would lie in the report:
        # `already_in_silence` is the no-op proof (the cut arrived correct), `moved`
        # is the work done, and `in_silence` is the state the customer leaves with —
        # a cut that travelled into a pause and one that was born there each count
        # once, and a cut still sitting on a word counts never.
        if sil_s:
            already += 1
        if sil_e:
            already += 1
        if gs is not None or (sil_s and ns == start):
            in_silence += 1
        if ge is not None or (sil_e and ne == end):
            in_silence += 1
        if gs is not None:
            moved += 1
            applied.append(abs(ns - start))
            moves.append({"cue": c.index, "edge": "start", "from": _ms(start),
                          "to": _ms(ns), "shift": _ms(abs(ns - start)),
                          "gap": [_ms(gs.start), _ms(gs.end)]})
        if ge is not None:
            moved += 1
            applied.append(abs(ne - end))
            moves.append({"cue": c.index, "edge": "end", "from": _ms(end),
                          "to": _ms(ne), "shift": _ms(abs(ne - end)),
                          "gap": [_ms(ge.start), _ms(ge.end)]})
        if start > duration + 0.001 or end > duration + 0.001:
            beyond += 1
        prev_end = ne
        out.append(Cue(c.index, _ms(ns), _ms(ne), c.text))

    doc_out = _overlap_sec([(c.start, c.end) for c in out])
    doc_in = _overlap_sec([(float(c.start), float(c.end)) for c in cues])
    return {
        "engine": "ovoz-jimlik",
        "audio": {"sample_rate": rate, "duration": duration,
                  "frames": read["frames"], "speech_sec": read["speech_sec"],
                  "silence_sec": read["silence_sec"], "gaps": len(gaps),
                  "window_sec": MAX_LISTEN_SEC,
                  "speech_runs": read["speech_runs"],
                  "noise_floor": read["noise_floor"],
                  "speech_level": read["speech_level"],
                  "hold_level": read["hold_level"],
                  "dead_air": read["dead_air"]},
        "tuning": {"frame_ms": FRAME_MS, "max_shift": _ms(shift),
                   "pad": BOUNDARY_PAD, "min_gap": MIN_GAP_SEC,
                   "min_span": MIN_SPAN, "min_sep": MIN_SEP},
        "gaps": [{"start": _ms(g.start), "end": _ms(g.end),
                  "dur": _ms(g.dur)} for g in gaps[:MAX_REPORT_GAPS]],
        "gaps_truncated": len(gaps) > MAX_REPORT_GAPS,
        "moves": moves[:MAX_REPORT_MOVES],
        "moves_truncated": len(moves) > MAX_REPORT_MOVES,
        "cues": [{"i": c.index, "start": c.start, "end": c.end, "text": c.text}
                 for c in out],
        "srt": format_srt(out),
        "summary": {
            "cues": len(out),
            "boundaries": 2 * len(out),
            "moved": moved,
            "in_silence": in_silence,
            "already_in_silence": already,
            "untouched": 2 * len(out) - moved,
            "max_applied": _ms(max(applied, default=0.0)),
            "mean_applied": _ms(sum(applied) / len(applied)) if applied else 0.0,
            "beyond_audio": beyond,
            "no_pause_found": not gaps,
            # The two proof-lines a customer is owed: words and sync.
            "words_preserved": [c.text for c in out] == [c.text for c in cues],
            "order_preserved": [c.index for c in out] == [c.index for c in cues],
            "overlap_sec_before": doc_in,
            "overlap_sec_after": doc_out,
            "never_worse": doc_out <= doc_in + 1e-6,
        },
    }
