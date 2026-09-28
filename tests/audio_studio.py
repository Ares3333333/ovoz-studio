"""The studio: synthetic speech-shaped audio, shared by the engines that listen.

Round 17 built it for Ovoz Jimlik, Round 18 for Ovoz So'z, and the point is the
same for both: the claims these engines make are about *pauses* — where one ends,
how long it is, whether a cut landed inside it. A recorded fixture can only ever
demonstrate the pauses that recording happens to contain, and a fixture bought or
downloaded for the suite is a fixture some engine was quietly tuned on. Built from
`math` and `array`, every case states the truth it will be judged against.

No numpy, no files, no network: deterministic on any machine that runs the suite.
"""
import io
import math
import wave
from array import array

from app.ling import align as A

RATE = 8_000                     # the rate the pipeline decodes to
FRAME_S = A.FRAME_MS / 1000.0    # 20 ms


def voice(seconds: float, amp: float = 0.35, freq: float = 140.0) -> list[int]:
    """A voiced frame: a sine at voice pitch with syllable-rate amplitude
    modulation. Amplitude modulation matters because a pure steady tone is the
    easiest possible case, and real speech dips toward its own floor."""
    n = int(seconds * RATE)
    out = []
    for i in range(n):
        am = 0.75 + 0.25 * math.sin(2 * math.pi * 3.0 * i / RATE)
        out.append(int(amp * am * 32767 * math.sin(2 * math.pi * freq * i / RATE)))
    return out


def room(seconds: float, tone: int = 0) -> list[int]:
    """Silence, optionally with deterministic room tone added. `tone=0` is a
    digital-silence best case; `tone=200` is the tape a real interview has."""
    n = int(seconds * RATE)
    if not tone:
        return [0] * n
    return [int(tone * math.sin(2 * math.pi * 997.0 * i / RATE)) for i in range(n)]


def tape(*pieces) -> array:
    flat = []
    for p in pieces:
        flat.extend(p)
    return array("h", [max(-32768, min(32767, v)) for v in flat])


# 5 s of an interview: three utterances, two pauses.
def interview(tone: int = 0, amp: float = 0.35) -> array:
    return tape(voice(1.0, amp), room(1.0, tone), voice(1.0, amp),
                room(0.5, tone), voice(1.5, amp))


# Nobody stops speaking for 4.5 s: a wall of sound with no pause to align to.
def nonstop(amp: float = 0.35) -> array:
    return tape(voice(4.5, amp))


# One six-second phrase of a long interview: two utterances and two real pauses.
BLOCK_SEC = 6.0


def block() -> array:
    return tape(voice(2.0, 0.35), room(0.6, 120),
                voice(2.0, 0.35), room(1.4, 120))


def long_tape(seconds: float) -> array:
    """A tape of any length, built cheaply: the six-second phrase is repeated by
    `array` multiplication rather than by a million-call Python loop, so a
    fifteen-minute case costs a memcpy and still contains a measured pause every
    three seconds.

    The repetition is the point, not a shortcut around it: the truth every block
    states is identical, so an engine that heard minute one but stopped hearing at
    minute fourteen cannot pass a test written against this tape."""
    unit = block()
    reps = int(round(seconds / BLOCK_SEC))
    return unit * max(1, reps)


def block_cues(seconds: float, per_block: float = BLOCK_SEC) -> list:
    """One cue per block, both edges planted inside speech, 0.15 s short of the
    pause that follows. `max_shift` is 0.6 s by default, so a cut may only reach
    its silence by travelling 0.18 s — the expected answer is arithmetic, not
    opinion, at every minute of the tape."""
    from app.ling.srt import Cue
    n = int(round(seconds / per_block))
    return [Cue(i + 1, i * per_block + 1.85, i * per_block + 4.45, f"slovo {i + 1}")
            for i in range(n)]


def wav_bytes(pcm, rate: int = RATE, channels: int = 1, width: int = 2,
              framerate: int | None = None) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(framerate or rate)
        if width == 2:
            data = array("h", pcm).tobytes()
        else:
            data = bytes((max(-128, min(127, v // 256)) + 128) for v in pcm)
        if channels > 1:
            interleaved = array("h", bytes(len(data) * channels))
            for i in range(0, len(pcm)):
                for c in range(channels):
                    interleaved[i * channels + c] = pcm[i]
            data = interleaved.tobytes() if width == 2 else bytes(
                (max(-128, min(127, v // 256)) + 128) for v in interleaved)
        w.writeframes(data)
    return buf.getvalue()


def rewrite_header(data: bytes, fmt: int = None, width: int = None,
                   channels: int = None) -> bytes:
    """Patch a real WAV's format tag / bit depth / channel count: the honest way to
    ask whether the reader refuses a file it cannot decode, without shipping one."""
    b = bytearray(data)
    if fmt is not None:
        b[20:22] = bytes([fmt & 0xFF, (fmt >> 8) & 0xFF])
    if channels is not None:
        b[22:24] = bytes([channels & 0xFF, (channels >> 8) & 0xFF])
    if width is not None:
        b[34:36] = bytes([width & 0xFF, (width >> 8) & 0xFF])
    return bytes(b)
