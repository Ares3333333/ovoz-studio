# -*- coding: utf-8 -*-
"""Round 18 — hostile live probe of Ovoz So'z (`/api/v1/ling/words`).

`verify_2026.py` asks whether the endpoint answers correctly. This file asks what
an SDK, a browser and an attacker ask: does the ceiling still hold when the client
keeps writing, is a 200 still honest when the tape is unmeasurable, does an
eight-thousand-word request cost the CPU it claims to cost, and is every refusal a
refusal rather than a stack trace.

Every byte here is generated in this file — no fixture recording is shipped, so
nothing can have been tuned against one.

Usage: python scripts/probe_words_live.py <base_url>
"""
import io
import json
import math
import socket
import sys
import time
import urllib.error
import urllib.request
import wave
from array import array

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8102"
RATE = 8000
PATH = "/api/v1/ling/words"
# Mirrors main.py: LING_ALIGN_MAX_BODY_BYTES / _LING_ALIGN_MAX_AUDIO_BYTES and the
# byte rate its 413 quotes, plus align.MAX_LISTEN_SEC — the engine's own window.
# At 8 kHz the byte cap (~312 s of tape) binds long before a 900 s window, so this
# probe cannot reach the window ceiling over HTTP at all; what it can and must prove
# is that a four-minute tape is now measured instead of refused.
CEILING = 6_000_000
AUDIO_CEILING = 5_000_000
TAPE_BYTE_RATE = 16_000       # 8 kHz mono s16le: the unit the 413 answers in
WINDOW_SEC = 900              # align.MAX_LISTEN_SEC
OLD_WINDOW_SEC = 200          # the ceiling Round 19 removed, kept as a regression bar
MAX_WORDS = 8_000
MAX_CUE_CHARS = 300          # _LING_MAX_CUE_CHARS, enforced strict on this route

results = []
POLITE = []          # counts the 429s this probe waited out, and asserts the first


def check(name, cond, detail=""):
    cond = bool(cond)
    results.append((cond, name))
    print(("PASS  " if cond else "FAIL  ") + name +
          ("  " + str(detail)[:200] if detail else ""))


def voice(seconds, amp=0.35, freq=140.0):
    n = int(seconds * RATE)
    return [int(amp * (0.75 + 0.25 * math.sin(2 * math.pi * 3.0 * i / RATE))
                * 32767 * math.sin(2 * math.pi * freq * i / RATE)) for i in range(n)]


def room(seconds, tone=180):
    n = int(seconds * RATE)
    return [int(tone * math.sin(2 * math.pi * 997.0 * i / RATE)) for i in range(n)]


def wav(*pieces, rate=RATE, fmt=1, bits=16, channels=1):
    samples = array("h")
    for part in pieces:
        samples.extend(part if isinstance(part, array) else array("h", part))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bits // 8)
        w.setframerate(rate)
        w.writeframes(samples.tobytes() if bits == 16 else
                      bytes((max(-128, min(127, v // 256)) + 128) for v in samples))
    raw = bytearray(buf.getvalue())
    if fmt != 1:                                  # rewrite the format tag
        raw[20] = fmt & 0xFF
        raw[21] = fmt >> 8
    return bytes(raw)


def tape_of(seconds, cell=0.25):
    """`seconds` of speech-shaped samples, by repeating one short cell.

    The ceiling tests below are about volume on the wire, not about content: a
    per-sample Python loop over 2.5M frames would make the probe slower than the
    server it is timing, which would make every number in this file worthless.
    """
    cell_samples = array("h", voice(cell))
    n = int(seconds * RATE)
    out = cell_samples * (n // len(cell_samples) + 1)
    return out[:n]


def dip_tape(words, per=0.35, gap=0.05):
    """Speech that really goes quiet before every word — the tape on which a
    `valley` claim is owed, and the one a steady tone never proves anything on."""
    out = []
    for _ in range(words):
        out += [voice(per), room(gap)]
    return wav(*out)


def steady_tape(seconds):
    """One unbroken vowel: full of syllable sags, short of a single real pause."""
    return wav(voice(seconds))


BOUNDARY = "----probewords"


def body(audio, fields=None, ctype="audio/wav"):
    out = b""
    for k, v in (fields or {}).items():
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                % (BOUNDARY, k, v)).encode("utf-8")
    out += ("--%s\r\nContent-Disposition: form-data; name=\"audio\"; "
            "filename=\"take.wav\"\r\nContent-Type: %s\r\n\r\n"
            % (BOUNDARY, ctype)).encode("utf-8") + audio + b"\r\n"
    out += ("--%s--\r\n" % BOUNDARY).encode("utf-8")
    return out


def post(audio, fields=None, ctype="audio/wav"):
    """Ask, and take a 429 for the polite signal it is.

    This probe deliberately makes more requests per minute than an anonymous
    visitor should, so the endpoint throttle is expected to bite: the interesting
    fact is not that it stops us, it is whether it says how long to wait. Every
    refusal below is therefore retried after the advertised pause.
    """
    for attempt in range(6):
        req = urllib.request.Request(
            BASE + PATH, data=body(audio, fields, ctype), method="POST",
            headers={"Content-Type": "multipart/form-data; boundary=" + BOUNDARY})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            wait = int(e.headers.get("Retry-After") or 1)
            if not POLITE:
                # The number itself belongs to a test: `tests/test_align.py` pins the
                # listening class at 12/min against 60/min for the text routes. What
                # the live socket must prove is that a refusal is obeyable: a wait the
                # client can act on, and a window whenever the answer is a rate limit
                # (a busy-listener 429 legitimately carries no window at all).
                lim = e.headers.get("X-RateLimit-Limit") or ""
                ok = 0 < wait <= 60 and (not lim or (lim.isdigit() and 0 < int(lim) <= 60))
                check("throttle: a 429 carries an obeyable wait",
                      ok, "Retry-After=%s limit=%s" % (wait, lim or "none"))
            POLITE.append(wait)
            time.sleep(min(wait, 70))
    raise AssertionError("still rate-limited after six polite waits")


def refuse(audio, fields=None, ctype="audio/wav"):
    """POST and report the verdict as (status, detail) — HTTPError is the answer
    we want here, so an unexpected 200 has to be visible as a failure."""
    try:
        st, rep = post(audio, fields, ctype)
        return st, json.dumps(rep)[:200]
    except urllib.error.HTTPError as e:
        # A refusal body can arrive on a connection the server already gave up on
        # (it answered before reading the tape): reading it must not be the reason
        # the probe dies. Status is the answer; the body is commentary.
        try:
            raw = e.read()
        except OSError:
            raw = b"{\"_reset\": true}"
        return e.code, raw.decode("utf-8", "replace")[:200]


# Cues written against the cell structure of dip_tape(): one real pause in front
# of every word that needs a cut, so "the tape decided" is a claim that can be
# checked rather than argued about.
LINES = json.dumps([{"start": 0.0, "end": 1.55, "text": "bir ikki uch to'rt"},
                    {"start": 1.6, "end": 2.35, "text": "besh olti"}],
                   ensure_ascii=False)
TAPE = dip_tape(6)

# ─── the happy path, judged on bytes ──────────────────────────────────────────
st, rep = post(TAPE, {"lines": LINES})
check("answer: an anonymous request gets a word report (no key, no cookie)",
      st == 200 and rep["engine"] == "ovoz-soz", st)
words = [it for c in rep["cues"] for it in c["words"]]
check("answer: every token comes back whole and in order",
      [it["w"] for it in words] ==
      ["bir", "ikki", "uch", "to'rt", "besh", "olti"], words[:2])
check("answer: each cue is partitioned, not merely listed",
      all(c["words"] and c["words"][0]["s"] == c["start"] and
          c["words"][-1]["e"] == c["end"] and
          all(a["e"] <= b["s"] for a, b in zip(c["words"], c["words"][1:]))
          for c in rep["cues"]))
check("answer: the dips were heard, not guessed",
      rep["summary"]["cut_valley"] >= 4 and rep["summary"]["valley_share"] >= 0.8,
      rep["summary"])
st2, again = post(TAPE, {"lines": LINES})
check("answer: the same bytes answer identically twice",
      json.dumps(again, sort_keys=True) == json.dumps(rep, sort_keys=True))
st3, steady = post(steady_tape(2.0), {"lines": LINES})
check("answer: a tape with no dips is not allowed to claim valley cuts",
      st3 == 200 and steady["summary"]["cuts"] > 0 and
      steady["summary"]["cut_valley"] == 0 and
      steady["summary"]["valley_share"] == 0.0, steady["summary"])
try:
    json.dumps(rep, allow_nan=False)
    finite = True
except ValueError:
    finite = False
check("answer: no NaN or Infinity reaches the client", finite)

# ─── the exports a player consumes ────────────────────────────────────────────
st, ass = post(TAPE, {"lines": LINES, "fmt": "ASS"})       # case-insensitive
check("export: fmt is matched case-insensitively and yields \\kf",
      st == 200 and "\\kf" in ass.get("ass", ""), st)
st, vtt = post(TAPE, {"lines": LINES, "fmt": "vtt"})
check("export: vtt carries one inline stamp per word",
      st == 200 and vtt["vtt"].count("<00:") == rep["summary"]["words"],
      vtt["vtt"].count("<00:") if st == 200 else st)
st, detail = refuse(TAPE, {"lines": LINES, "fmt": "../../etc/passwd"})
check("export: an unknown fmt is a 422, never a file", st == 422, detail)

# ─── ceilings, on the wire ────────────────────────────────────────────────────
huge = wav(voice(1.0), room(0.05), voice(1.0)) + b"\x00" * (CEILING + 50_000)
st, detail = refuse(huge, {"lines": LINES})
check("ceiling: a body over the cap is refused with the number in it",
      st == 413 and str(CEILING) in detail, detail)

st, detail = refuse(wav(tape_of(AUDIO_CEILING / 2 / RATE + 2)), {"lines": LINES})
check("ceiling: an audio part over its own cap is refused before it is decoded",
      st == 413 and str(AUDIO_CEILING) in detail, detail)
check("ceiling: that refusal answers in seconds, the unit the caller holds",
      str(AUDIO_CEILING // TAPE_BYTE_RATE) + "s" in detail, detail)

# Round 19: the window counts time, not samples. Four minutes is a third of the
# window and twice the old ceiling — the tape that used to come back 413.
st, longrep = post(wav(tape_of(246)), {"lines": LINES})
check("window: a tape past the old 200 s cap is measured, not refused",
      st == 200 and longrep.get("audio", {}).get("duration") == 246.0
      and longrep["audio"]["window_sec"] == WINDOW_SEC, st)
check("window: the answer still carries the same ruler at four minutes",
      st == 200 and longrep["audio"]["frame_ms"] == 20.0
      and longrep["summary"]["words"] == 6, longrep.get("summary"))

# The one that used to hang: the client keeps writing after the server refused.
host, _, port = urllib.parse.urlparse(BASE).netloc.partition(":")
port = int(port or 80)
payload = body(b"\x00" * (CEILING + 2_000_000), {"lines": LINES})
try:
    s = socket.create_connection((host, port), timeout=30)
    s.sendall(("POST %s HTTP/1.1\r\nHost: %s\r\nContent-Type: multipart/form-data; "
               "boundary=%s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
               % (PATH, host, BOUNDARY, len(payload) + 4_000_000)).encode())
    t0 = time.time()
    try:
        s.sendall(payload)             # more than the drain is willing to swallow
    except OSError:
        pass                           # closed while we wrote: that is the answer
    head = s.recv(65536)
    s.close()
    drained = time.time() - t0
    status = head.split(b" ")[1].decode() if b" " in head else "?"
    check("socket: an over-cap body is answered, not stalled",
          status == "413", "%s in %.1fs" % (status, drained))
    check("socket: the refusal closes the connection (no keep-alive smuggling)",
          b"connection: close" in head.lower(), head.split(b"\r\n\r\n")[0][:120])
    check("socket: the drain stops at its own ceiling instead of reading forever",
          drained < 3.5, "%.1fs" % drained)
except OSError as exc:
    check("socket: an over-cap body is answered, not stalled", False, exc)

# ─── containers the engine will not guess at ──────────────────────────────────
st, detail = refuse(b"RIFF" + b"\x00" * 600, {"lines": LINES})
check("container: bytes that are not a WAV are a 415", st == 415, detail)
st, detail = refuse(dip_tape(6), {"lines": LINES}, ctype="audio/mpeg")
check("container: the sniffed bytes win over the declared type",
      st == 200, detail)
st, detail = refuse(wav(voice(1.0), fmt=6), {"lines": LINES})
check("container: a header claiming compression is refused by name",
      st == 415 and "compressed" in detail, detail)
st, detail = refuse(wav(*([voice(0.35) + room(0.05)] * 5), bits=8),
                    {"lines": LINES})
check("container: 8-bit PCM is accepted, because the contract says so",
      st == 200, detail)
st, detail = refuse(wav(voice(1.0), rate=4000), {"lines": LINES})
check("container: an out-of-contract rate is refused, not upsampled",
      st == 422 and "rate" in detail, detail)
st, detail = refuse(wav(*([room(1.0)] * 2)), {"lines": LINES})
check("container: a silent tape is refused rather than drawn from geometry",
      st == 422 and "speech" in detail, detail)

# ─── cue validation, before any frame is read ─────────────────────────────────
st, detail = refuse(TAPE, {"lines": "not json at all"})
check("cues: unparseable lines are a 422 naming the shape",
      st == 422 and "JSON" in detail, detail)
st, detail = refuse(TAPE, {"lines": json.dumps(
    [{"start": float("inf"), "end": 2.0, "text": "salom"}])})
check("cues: an infinite start cannot ride on the wire", st == 422, detail)
st, detail = refuse(TAPE, {"lines": json.dumps(
    [{"start": 0.0, "end": 1.0, "text": "a" * (MAX_CUE_CHARS + 1)}])})
check("cues: an over-long cue is refused, never trimmed into a false claim",
      st == 422 and str(MAX_CUE_CHARS) in detail, detail)
st, detail = refuse(TAPE, {"lines": json.dumps(
    [{"start": 0.0, "end": 1.0, "text": "x"}]),
    "srt": "1\n00:00:00,000 --> 00:00:01,000\nsalom\n"})
check("cues: sending both shapes is a 422, not a coin flip", st == 422, detail)
st, detail = refuse(TAPE, {})
check("cues: no cue shape at all is a 422, not a 500", st == 422, detail)

crowd = json.dumps([{"start": 0.0, "end": 0.9, "text": "ab " * 100}
                    for _ in range(1 + MAX_WORDS // 100)])
t0 = time.time()
st, detail = refuse(dip_tape(6), {"lines": crowd})
check("budget: the word cap answers 422 before the tape is listened to",
      st == 422 and "too many words" in detail,
      "%s in %.1fs" % (detail, time.time() - t0))

# Just inside the cap, on a tape long enough to hold it: the refusal must be the
# budget, never the geometry, or the 8 000 number is decorative.
fits = json.dumps([{"start": 0.0, "end": 6.9, "text": "ab " * 100}
                   for _ in range(MAX_WORDS // 100 - 1)])
t0 = time.time()
st, rep2 = post(dip_tape(40), {"lines": fits})
took = time.time() - t0
check("budget: a request just inside the cap is served, not refused",
      st == 200 and rep2["summary"]["cues_refused"] == 0 and
      rep2["summary"]["words"] == (MAX_WORDS // 100 - 1) * 100,
      "%s in %.1fs" % (st, took))
check("budget: the worst legal request costs seconds, not minutes",
      took < 20.0, "%.1fs for %d words" % (took, MAX_WORDS - 100))

# ─── per-cue honesty inside a 200 ─────────────────────────────────────────────
st, rep3 = post(TAPE, {"lines": json.dumps(
    [{"start": 0.0, "end": 0.4, "text": "bir ikki uch to'rt besh ololti yetti"},
     {"start": 9.0, "end": 9.5, "text": "tashqarida"}], ensure_ascii=False)})
check("refusal inside a 200: an impossible line rate is named per cue",
      st == 200 and rep3["cues"][0]["reason"] == "too_fast" and
      "cps_needed" in rep3["cues"][0], rep3["cues"][0])
check("refusal inside a 200: a cue off the end of the tape says so",
      rep3["cues"][1]["reason"] == "outside_audio", rep3["cues"][1])
check("refusal inside a 200: the summary counts what was not measured",
      rep3["summary"]["cues_refused"] == 2 and rep3["summary"]["words"] == 0,
      rep3["summary"])
st4, only_bad = post(TAPE, {"lines": json.dumps(
    [{"start": 9.0, "end": 9.5, "text": "tashqarida"}], ensure_ascii=False),
    "fmt": "vtt"})
check("export: a cue the engine refused contributes no line to the VTT",
      st4 == 200 and only_bad["summary"]["cues_refused"] == 1 and
      "-->" not in only_bad["vtt"], only_bad.get("vtt", "")[:40])

print()
if POLITE:
    print(f"(waited out {len(POLITE)} rate-limit pauses: {sum(POLITE)}s total)")
bad = [n for ok, n in results if not ok]
print("PROBE:", "PASS" if not bad else "FAIL",
      f"({len(results) - len(bad)}/{len(results)} checks)")
for n in bad:
    print("  failed:", n)
sys.exit(1 if bad else 0)
