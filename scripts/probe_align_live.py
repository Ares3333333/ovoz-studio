# -*- coding: utf-8 -*-
"""Round 17 — hostile live probe of Ovoz Jimlik (`/api/v1/ling/align`).

`verify_2026.py` asks whether the endpoint answers correctly. This file asks the
questions a browser and an SDK actually ask, and that only a live socket can
answer: what happens to a body over the ceiling, whether a refusal is still a
refusal when the client keeps writing, and whether the numbers on the wire are
the numbers the engine measured.

Every check here is built from bytes generated in this file: no fixture recording
is shipped, so nothing can be tuned to one tape.

Usage: python scripts/probe_align_live.py <base_url>
"""
import io
import json
import math
import sys
import time
import urllib.error
import urllib.request
import wave
from array import array

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8102"
RATE = 8000
PATH = "/api/v1/ling/align"
# Mirrors main.py: LING_ALIGN_MAX_BODY_BYTES / _LING_ALIGN_MAX_AUDIO_BYTES.
CEILING = 6_000_000
AUDIO_CEILING = 5_000_000

results = []


def check(name, cond, detail=""):
    cond = bool(cond)
    results.append(cond)
    print(("PASS  " if cond else "FAIL  ") + name + ("  " + detail if detail else ""))


def voice(seconds, amp=0.35, freq=140.0):
    n = int(seconds * RATE)
    return [int(amp * (0.75 + 0.25 * math.sin(2 * math.pi * 3.0 * i / RATE))
                * 32767 * math.sin(2 * math.pi * freq * i / RATE)) for i in range(n)]


def room(seconds, tone=180):
    n = int(seconds * RATE)
    return [int(tone * math.sin(2 * math.pi * 997.0 * i / RATE)) for i in range(n)]


def wav(samples, rate=RATE, fmt=1, bits=16, channels=1, block_align=None,
        bytes_per_sec=None):
    """A hand-written WAV, so a bad file can be described exactly rather than by
    hoping some encoder produced it."""
    width = bits // 8
    data = array("h", samples).tobytes() if bits == 16 else bytes(
        (max(-128, min(127, v // 256)) + 128) for v in samples)
    ba = block_align or channels * width
    bps = bytes_per_sec or ba * rate
    body = (b"RIFF" + struct_le(4 + 24 + 8 + len(data)) + b"WAVE"
            + b"fmt " + struct_le(16) + struct_u16(fmt) + struct_u16(channels)
            + struct_le(rate) + struct_le(bps) + struct_u16(ba) + struct_u16(bits)
            + b"data" + struct_le(len(data)))
    return body + data


def struct_le(n):
    return bytes([n & 255, (n >> 8) & 255, (n >> 16) & 255, (n >> 24) & 255])


def struct_u16(n):
    return bytes([n & 255, (n >> 8) & 255])


BOUND = "----probealign"


def multipart(fields, blob, filename="tape.wav", ctype="audio/wav"):
    body = b""
    for k, v in fields.items():
        body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                 % (BOUND, k, v)).encode("utf-8")
    body += ('--%s\r\nContent-Disposition: form-data; name="audio"; '
             'filename="%s"\r\nContent-Type: %s\r\n\r\n'
             % (BOUND, filename, ctype)).encode("utf-8") + blob + b"\r\n"
    body += ("--%s--\r\n" % BOUND).encode("utf-8")
    return body


def send(fields, blob, ctype="audio/wav", wait=True):
    """POST and hand back (status, headers, json) — an HTTPError still carries all
    three, because a refusal is an answer too.

    A 429 is obeyed, not argued with: the listening routes cost 12/min on purpose
    (seconds of pure Python each), and a probe that ignores `Retry-After` proves
    only that our own client cannot follow the contract we ship to SDKs. This probe
    makes ~15 calls at that class, so ten waits of one window is the budget — a
    failure here must mean the server is unreachable, not that the probe was rude.
    """
    for attempt in range(10):
        st, hdr, payload = _send_once(fields, blob, ctype)
        if st != 429 or not wait:
            return st, hdr, payload
        time.sleep(min(int(hdr.get("Retry-After") or 5), 65))
    return st, hdr, payload


def _send_once(fields, blob, ctype):
    body = multipart(fields, blob, ctype=ctype)
    req = urllib.request.Request(BASE + PATH, data=body, method="POST",
                                 headers={"Content-Type":
                                          "multipart/form-data; boundary=" + BOUND})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, dict(r.headers), json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {"_raw": raw[:200].decode("utf-8", "replace")}
        return e.code, dict(e.headers), payload


LINES = json.dumps([{"start": 0.0, "end": 1.2, "text": "Birinchi qator."},
                    {"start": 1.2, "end": 3.3, "text": "Ikkinchi qator."},
                    {"start": 3.3, "end": 5.0, "text": "Uchinchi qator."}],
                   ensure_ascii=False)
TAPE = wav(voice(1.5) + room(0.5) + voice(1.5) + room(0.5) + voice(1.5))


def timecode(sec):
    """A moment as the delivered SRT document must write it."""
    return "%02d:%02d:%02d,%03d" % (sec // 3600 % 24, sec // 60 % 60, sec % 60,
                                    round((sec - int(sec)) * 1000))


# ─── 1. the answer is the measurement, not a summary of it ────────────────────
st, hdr, rep = send({"lines": LINES}, TAPE)
check("probe: a real tape answers 200 with a report", st == 200 and "moves" in rep, str(st))
if st == 200:
    s = rep["summary"]
    check("probe: the move list agrees with the counters edge by edge",
          len(rep["moves"]) == s["moved"] and
          all(m["to"] != m["from"] for m in rep["moves"]),
          "moves=%s moved=%s" % (len(rep["moves"]), s["moved"]))
    gaps = [(g["start"], g["end"]) for g in rep["gaps"]]
    landed = all(any(a - 0.031 <= m["to"] <= b + 0.031 for a, b in gaps)
                 for m in rep["moves"])
    check("probe: every reported move really lands inside a reported pause", landed,
          "%d moves against %d pauses" % (len(rep["moves"]), len(gaps)))
    # The document a customer downloads is the report's own claim in another shape:
    # if a cue's timing is not the timecode in the SRT, one of the two is a lie.
    doc = rep["srt"]
    missing = [c["i"] for c in rep["cues"]
               if timecode(c["start"]) not in doc or timecode(c["end"]) not in doc]
    check("probe: the served SRT carries the served cue timings", not missing,
          "cues %s are not in the document" % missing)
    check("probe: a content-type is set and the body is JSON, not a stream",
          hdr.get("content-type", "").startswith("application/json"),
          hdr.get("content-type", ""))


# ─── 2. the ceilings are the ones the product claims ──────────────────────────
big = wav(voice(1.5) + room(0.5) + voice(1.5))          # ~54 s of tape, over 5 MB
st2, hdr2, over = send({"lines": LINES}, big + b"\x00" * (AUDIO_CEILING - len(big) + 10))
check("probe: an audio part over its own ceiling is refused 413, not decoded",
      st2 == 413 and "audio too large" in json.dumps(over), "%s %s" % (st2, over))

# Round 19: the listening window counts *time*, not samples. Four minutes of tape is
# a third of the window and used to be a 413 from the engine while the transport had
# already accepted the upload — a ceiling measured in the wrong unit.
st2b, _, long2 = send({"lines": LINES},
                      wav(voice(120.0) + room(2.0) + voice(124.0)))
check("probe: a four-minute tape is heard instead of refused",
      st2b == 200 and long2.get("audio", {}).get("duration", 0) > 240
      and long2["audio"]["window_sec"] == 900, "%s %s" % (st2b, str(long2)[:90]))
check("probe: and it is heard with the same 20 ms ruler as a five-second one",
      st2b == 200 and long2["audio"]["frames"] > 12_000
      and abs(long2["tuning"]["frame_ms"] - 20.0) < 1e-9,
      str(long2.get("audio", {}).get("frames")))

# The over-ceiling POST is written to the socket on purpose: a 413 that tears the
# connection while the client is still sending looks like a network bug to the SDK,
# not like a refusal, so the answer must arrive as a complete HTTP response.
try:
    st4, hdr4, payload4 = send({"lines": LINES, "pad": "x" * (CEILING + 1000)}, TAPE)
    check("probe: a body over the aligner ceiling is refused structurally",
          st4 == 413 and "error_code" in json.dumps(payload4),
          "%s %s" % (st4, str(payload4)[:120]))
    check("probe: the refusal closes the connection instead of resetting it",
          hdr4.get("connection", "").lower() == "close",
          repr(hdr4.get("connection")))
except Exception as exc:                                    # noqa: BLE001
    check("probe: a body over the aligner ceiling is refused structurally", False,
          repr(exc)[:160])
    check("probe: the refusal closes the connection instead of resetting it", False)

# ─── 3. refusals name the format, so the client can fix it ───────────────────
cases = {
    "float32": wav(voice(1.5), fmt=3, bits=32),
    "mu-law": wav(voice(1.5), fmt=7, bits=8),
    "32-bit int": wav(voice(1.5), bits=32),
    "lying block-align": wav(voice(1.5), channels=2, block_align=2),
    "absurd rate": wav(voice(0.4), rate=441_000 // 2),
}
for name, blob in cases.items():
    st5, _, payload5 = send({"lines": LINES}, blob)
    text = json.dumps(payload5)
    check("probe: %s is refused with a readable reason" % name,
          st5 in (413, 415, 422) and "detail" in text and
          '"internal_error"' not in text,
          "%s %s" % (st5, str(payload5)[:110]))

# The cue side is validated before a five-megabyte upload is buffered: an
# anonymous caller must not be able to make the server read bytes to discover a
# typo it could have seen in the first kilobyte.
st6, _, payload6 = send({"lines": "not json"}, wav(voice(30.0) + room(1.0) + voice(30.0)))
check("probe: bad cues are refused without buffering the tape",
      st6 == 422 and "lines" in json.dumps(payload6), "%s %s" % (st6, str(payload6)[:90]))

# ─── 4. the other language routes keep their own (small) ceiling ─────────────
try:
    req = urllib.request.Request(
        BASE + "/api/v1/ling/analyze",
        data=json.dumps({"text": "a" * 200_000}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        check("probe: the JSON ceiling still guards the JSON routes", False,
              "status %s" % r.status)
except urllib.error.HTTPError as e:
    check("probe: the JSON ceiling still guards the JSON routes", e.code == 413,
          str(e.code))
except Exception as exc:                                    # noqa: BLE001
    check("probe: the JSON ceiling still guards the JSON routes", False, repr(exc))

# ─── 5. determinism over the wire, twice, on the same bytes ──────────────────
# A determinism claim needs TWO answers under the same conditions. Retrying each
# call on its own was wrong here: the anonymous listening class is 12/min, so one
# of the pair could wait its way to success while the other gave up, and the check
# then reported "reports differ" about a 429 — a throttle dressed up as a
# nondeterminism bug (which is exactly how a flaky check teaches nobody anything).
# So the PAIR is retried: either both are 200, or we wait and try the pair again.
a = b = None
for pair_try in range(6):
    a = send({"lines": LINES}, TAPE)
    b = send({"lines": LINES}, TAPE)
    if a[0] == b[0] == 200:
        break
    time.sleep(5)
keys = set(a[2]) | set(b[2]) if isinstance(a[2], dict) and isinstance(b[2], dict) else set()
diff = sorted(k for k in keys if a[2].get(k) != b[2].get(k)) if keys else ["<not json>"]
check("probe: the same tape answers byte-identically (no clock, no randomness)",
      a[0] == b[0] == 200 and a[2] == b[2],
      "statuses %s/%s after %d pair attempts" % (a[0], b[0], pair_try + 1)
      if a[0] != 200 or b[0] != 200 else "differ in %s" % diff[:4])

print("\nPROBE:", "PASS" if all(results) else "FAIL",
      f"({sum(results)}/{len(results)} checks)")
sys.exit(0 if all(results) else 1)
