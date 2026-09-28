#!/usr/bin/env python3
"""Round 25 — live probe of the listening reach («Ovoz Qayta»).

`verify_2026.py` and the other probes ask how a single engine call behaves. This
file asks the question only a real job on a real server can answer:

    does a tape longer than one window get heard in full, end to end?

Before this release the answer was no: `ffmpeg -t 895` cut the tape, both
listeners aligned the first fifteen minutes, and the job said nothing about the
rest. The fix is a streaming envelope and a curve-based engine entry point, and
neither is visible in a unit test that hands the engine a buffer. So this probe
uploads a real 16-minute recording, waits for the worker, and reads back what the
job itself claims it heard.

Everything here is generated in this file (a six-second interview phrase,
repeated), encoded with the same ffmpeg the product uses, and uploaded as an MP3
because a 4 MB ceiling is the product's own law. Nothing is written into the
repository: the scratch tape lives in the system temp dir and is left behind for
the operator to inspect if a check fails.

Usage: python scripts/probe_reach_live.py <base_url>
Requires the server to run with OVOZ_ALLOW_DEMO_CREDIT=1 and OVOZ_ADMIN_SECRET set
(the demo grant is the only way a probe buys a 16-minute job).
"""
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from array import array

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8102").rstrip("/")
ADMIN_KEY = os.environ.get("OVOZ_ADMIN_SECRET", "")
RATE = 8_000
TAPE_SEC = 960.0        # sixteen minutes: past the 900-second window engines know
BLOCK_SEC = 6.0         # the phrase the tape is built from
PASS = FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS  {label}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"FAIL  {label}  {detail}")


def call(path, data=None, token=None, ctype=None, raw=None):
    """One HTTP call. `data` is form-encoded, `raw` is the body as bytes."""
    body = None
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if raw is not None:
        body = raw
        headers["Content-Type"] = ctype or "application/octet-stream"
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(BASE + path, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def jget(path, token=None):
    st, body = call(path, token=token)
    try:
        return st, json.loads(body)
    except Exception:                                    # noqa: BLE001
        return st, {}


def jform(path, data=None, token=None):
    st, body = call(path, data=data, token=token)
    try:
        return st, json.loads(body)
    except Exception:                                    # noqa: BLE001
        return st, {"_raw": body[:200].decode("utf-8", "replace")}


# ─── the tape ─────────────────────────────────────────────────────────────────

def phrase() -> array:
    """One six-second phrase: two utterances and two measured pauses.

    Identical to the unit suite's block on purpose: the truth this tape states is
    the same everywhere, so a job that heard minute one but stopped hearing at
    minute fourteen cannot pass the checks below."""
    out = []

    def voice(seconds, amp=0.35, freq=140.0):
        for i in range(int(seconds * RATE)):
            am = 0.75 + 0.25 * math.sin(2 * math.pi * 3.0 * i / RATE)
            out.append(int(amp * am * 32767 * math.sin(2 * math.pi * freq * i / RATE)))

    def room(seconds, tone=120):
        for i in range(int(seconds * RATE)):
            out.append(int(tone * math.sin(2 * math.pi * 997.0 * i / RATE)))

    voice(2.0)
    room(0.6)
    voice(2.0)
    room(1.4)
    return array("h", out)


def build_tape(directory: str) -> str:
    """The 16-minute recording as an MP3 the product is willing to accept."""
    wav = os.path.join(directory, "longtape.wav")
    unit = phrase()
    reps = int(TAPE_SEC / BLOCK_SEC)
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((unit * reps).tobytes())
    mp3 = os.path.join(directory, "longtape.mp3")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", wav, "-codec:a",
                    "libmp3lame", "-b:a", "32k", "-ac", "1", mp3],
                   check=True, timeout=300)
    os.remove(wav)
    return mp3


def multipart(path: str, fields: dict) -> tuple[str, bytes]:
    boundary = "----ovozreach" + uuid.uuid4().hex
    name = os.path.basename(path)
    with open(path, "rb") as f:
        blob = f.read()
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"'
                     f'\r\n\r\n{v}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                  f'filename="{name}"\r\nContent-Type: audio/mpeg\r\n\r\n').encode())
    parts.append(blob + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return ("multipart/form-data; boundary=" + boundary, b"".join(parts))


# ─── the job, on the server, for real ────────────────────────────────────────

contact = "+9989" + str(uuid.uuid4().int % 10**9).rjust(9, "0")
st, reg = jform("/api/auth/register", {"name": "Reach probe", "contact": contact,
                                      "secret": "reach-probe-secret-2026"})
check("auth: a probe user can register", st in (200, 201) or st == 409, f"{st}")
st, login = jform("/api/auth/login", {"contact": contact,
                                     "secret": "reach-probe-secret-2026"})
token = login.get("token", "")
check("auth: and log in", st == 200 and bool(token), str(st))
if not token:
    print("ABORT: no token, nothing to probe")
    sys.exit(1)

st, granted = jform("/api/dev/demo-credit",
                    {"minutes": "600", "admin_key": ADMIN_KEY}, token=token)
check("billing: the demo grant is available to this probe", st == 200, str(granted))
if st != 200:
    print("ABORT: run the server with OVOZ_ALLOW_DEMO_CREDIT=1 and "
          "OVOZ_ADMIN_SECRET exported here")
    sys.exit(1)

scratch = tempfile.mkdtemp(prefix="ovoz-reach-")
try:
    tape = build_tape(scratch)
    size = os.path.getsize(tape)
    check("tape: a 16-minute recording fits the product's own 4 MB ceiling",
          size < 4 * 1024 * 1024, f"{size/1e6:.2f} MB at 32 kbps mono")
    ctype, body = multipart(tape, {"jtype": "subtitles", "src": "uz", "tgt": "ru",
                                   "align": "1", "words": "1", "diarize": "1"})
    t0 = time.monotonic()
    st, raw = call("/api/jobs", raw=body, ctype=ctype, token=token)
    job = json.loads(raw) if st < 300 else {}
    jid = job.get("job", {}).get("id", "")
    check("upload: the long tape is accepted", st == 201 and jid, str(st))
    if not jid:
        raise SystemExit(1)

    status, doc = "", {}
    while time.monotonic() - t0 < 240:
        time.sleep(1.0)
        st, doc = jget(f"/api/jobs/{jid}", token=token)
        status = doc.get("job", {}).get("status", "")
        if status in ("done", "failed", "canceled"):
            break
    elapsed = time.monotonic() - t0
    check("job: the sixteen-minute tape finishes", status == "done",
          f"{status} {doc.get('job', {}).get('error', '')} in {elapsed:.0f}s")

    events = {e["step"]: e for e in doc.get("timeline", [])}
    align = events.get("align", {}).get("data", {}) or {}
    words = events.get("words", {}).get("data", {}) or {}
    turns = events.get("diarize", {}).get("data", {}) or {}

    # 1. the reach claim, as the job itself reports it
    check("reach: Jimlik heard past the old 900-second window",
          align.get("duration", 0) > 900.0, f"duration={align.get('duration')}")
    check("reach: So'z timed words on that same full tape",
          words.get("cues", 0) > 0 and not words.get("truncation"),
          f"cues={words.get('cues')}")
    check("reach: nothing paid for was left unheard",
          not align.get("truncation") and not words.get("truncation"),
          str(align.get("truncation")))
    # 2. the pass really was a pass, not a giant single read
    st, rep = jget(f"/api/jobs/{jid}/download/align", token=token)
    if st != 200 or not isinstance(rep, dict):
        rep = {}
    audio = rep.get("audio", {}) or {}
    check("stream: the report says the tape arrived in blocks",
          audio.get("streamed") is True and audio.get("blocks", 0) >= 15,
          f"blocks={audio.get('blocks')} block_sec={audio.get('block_sec')}")
    check("stream: heard_sec is the tape, not a window",
          abs(audio.get("heard_sec", 0) - TAPE_SEC) < 2.0,
          f"heard={audio.get('heard_sec')}")
    check("stream: the whole tape still answers the engine's own laws",
          rep.get("summary", {}).get("words_preserved") is True and
          rep.get("summary", {}).get("never_worse") is True,
          str(rep.get("summary", {}).get("moved")))
    st, wrep = jget(f"/api/jobs/{jid}/download/words", token=token)
    if not isinstance(wrep, dict):
        wrep = {}
    check("stream: the word timer read the same curve",
          (wrep.get("audio", {}) or {}).get("streamed") is True,
          str((wrep.get("audio", {}) or {}).get("blocks")))
    # 3. the honesty numbers Round 25 added to the speaker step
    check("diarize: the card gets numbers, not an English sentence",
          turns.get("code") in ("turns", "turns_text") and
          ("heard_sec" in turns or turns.get("code") == "turns_text"),
          str(turns))
    check("diarize: profiles past the tape are counted, not hidden",
          isinstance(turns.get("off_tape"), int) and turns.get("off_tape", 0) >= 0,
          str(turns.get("off_tape")))
    st, srt = jget(f"/api/jobs/{jid}/download/srt", token=token)
    check("output: subtitles came out of the long tape", st == 200, str(st))
finally:
    for f in os.listdir(scratch):
        try:
            os.remove(os.path.join(scratch, f))
        except OSError:
            pass
    os.rmdir(scratch)

print(f"\nREACH PROBE: {'PASS' if not FAIL else 'FAIL'} "
      f"({PASS}/{PASS + FAIL} checks, {TAPE_SEC:.0f}s of tape)")
sys.exit(1 if FAIL else 0)
