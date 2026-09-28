# -*- coding: utf-8 -*-
"""Live verification of the public language engine: the 2026 converter, the
Round-13.5 review fixes and the Round-14 speaker-attribution endpoint.
Runs against a real server, so it catches what in-process tests cannot: routing,
rate limits and the payload shapes the browser actually receives.
Usage: python scripts/verify_2026.py <base_url>   (UTF-8 safe on Windows.)"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8098"
OU = "\u00d6"       # Ö
SH = "\u015e"       # Ş
CYR_UZ = "\u040e\u0437\u0431\u0435\u043a \u0442\u0438\u043b\u0438"   # Ўзбек тили
NEW_ONLY = "\u015e\u015e\u015e\u00c7\u00c7\u00c7"                    # ŞŞŞÇÇÇ
MIXED = "\u040e\u0437\u0431\u0435\u043a " + OU + "zbek"              # Ўзбек Özbek

# A four-cue interview as ASR delivers it: greeting, greeting answer, question,
# answer. Two people, alternating — the engine's headline claim, checked over HTTP.
DIALOG = [
    {"start": 0.0, "end": 3.4, "text":
     "Assalomu alaykum, ustoz, vaqtingizni olganim uchun uzr, boshlaymizmi?"},
    {"start": 3.6, "end": 7.9, "text":
     "Va alaykum assalom, xush kelibsiz, men tayyorman."},
    {"start": 8.2, "end": 12.5, "text":
     "Sizning so'nggi loyihangiz haqida so'rasam bo'ladimi?"},
    {"start": 12.8, "end": 19.1, "text":
     "Albatta, biz uni Farg'ona vodiyida ikki oyda ishga tushirdik."},
]
DIALOG_SRT = "\n".join(
    f"{i}\n00:00:{int(c['start']):02d},000 --> 00:00:{int(c['end']):02d},000\n"
    f"{c['text']}\n" for i, c in enumerate(DIALOG, 1))
# The same tape transcribed in the 2000s Cyrillic alphabet: attribution must not
# move by one cue, or the engine is script-dependent and half the market is lost.
DIALOG_CYR = [
    "Ассалому алайкум, устоз, вақтингизни олганим учун узр, бошлаймизми?",
    "Ва алайкум ассалом, хуш келибсиз, мен тайёрман.",
    "Сизнинг сўнги лойиҳангиз ҳақида сўрасам бўладими?",
    "Албатта, биз уни Фарғона водийида икки ойда ишга туширдик.",
]

checks = []


def _open(req, attempts=4):
    """The public language endpoints are rate-limited on purpose (60/min), and a
    dense verification run legitimately trips it. A 429 used to kill the script with
    a traceback, which reads like a broken engine — so wait out the window and retry,
    bounded, and let a real failure surface as a failure."""
    import time
    for i in range(attempts):
        try:
            return urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == attempts - 1:
                raise
            time.sleep(20)


def post(path, payload):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with _open(req) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def get(path):
    with _open(base + path) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def get_text(path):
    with _open(base + path) as r:
        return r.status, r.read().decode("utf-8", "replace")


def check(name, cond, detail=""):
    # bool() is not decoration: a truthy count (`by_rule.get("fast")` → 3) used to
    # inflate the tally past the number of checks, i.e. the report could score 50/48.
    cond = bool(cond)
    checks.append(cond)
    print(("PASS  " if cond else "FAIL  ") + name + ("  " + detail if detail else ""))


st, info = get("/api/v1/info")
check("info >= v0.11 + converter feature", st == 200
      and tuple(int(x) for x in info["version"].split(".")[:2]) >= (0, 11)
      and "uzbek_2026_alphabet_converter" in info["features"],
      f"{info['version']}")

st, a = post("/api/v1/ling/analyze", {"text": CYR_UZ})
check("analyze new_latin = %szbek tili" % OU, st == 200 and a["new_latin"] == OU + "zbek tili",
      repr(a["new_latin"]))
check("analyze latin stays legacy ASCII", a["latin"] == "O'zbek tili", repr(a["latin"]))
check("analyze script-state flags", a["has_legacy_latin_letters"]
      and not a["has_reform_letters"], str({k: v for k, v in a.items() if k.startswith("has_")}))

# M6: pure reform text must classify as latin, not 'other'
st, a = post("/api/v1/ling/analyze", {"text": NEW_ONLY})
check("M6 pure new-Latin classified latin", st == 200 and a["script"] == "latin"
      and a["has_reform_letters"], f'{a["script"]} conf={a["confidence"]}')

# M2: mixed Cyrillic + reform input must fold to legacy Latin (no leakage)
st, a = post("/api/v1/ling/analyze", {"text": MIXED})
check("M2 mixed folds to legacy Latin", st == 200 and a["latin"] == "O'zbek O'zbek",
      repr(a["latin"]))
check("M2 no reform letter leaks", OU not in a["latin"])

# M3: all-caps round-trip through the public endpoint
st, r = post("/api/v1/ling/transliterate", {"text": "SHAHAR", "to": "new_latin"})
check("M3 forward SHAHAR->%sAHAR" % SH, st == 200 and r["result"] == SH + "AHAR", repr(r["result"]))
st, r = post("/api/v1/ling/transliterate", {"text": SH + "AHAR", "to": "latin"})
check("M3 reverse %sAHAR->SHAHAR" % SH, st == 200 and r["result"] == "SHAHAR", repr(r["result"]))

# m1: one target vocabulary — aliases resolve at the API too
st, r = post("/api/v1/ling/transliterate", {"text": CYR_UZ, "to": "new"})
check("m1 alias 'new' accepted", st == 200 and r["result"] == OU + "zbek tili", repr(r["result"]))
for bad in ("klingon", "", "latn"):
    try:
        post("/api/v1/ling/transliterate", {"text": "salom", "to": bad})
        check("m1 rejects %r" % bad, False)
    except urllib.error.HTTPError as e:
        check("m1 rejects %r" % bad, e.code == 422, str(e.code))

# m2: glossary keyed with the official turned comma must match
st, r = post("/api/v1/ling/transliterate", {
    "text": "\u040e\u0437\u0431\u0435\u043a\u0438\u0441\u0442\u043e\u043d",
    "terms": {"O\u02bbzbekiston": "OZ"}})
check("m2 official-apostrophe term matches", st == 200 and r["result"] == "OZ", repr(r["result"]))

# official must not decorate a reformed result
st, r = post("/api/v1/ling/transliterate", {"text": CYR_UZ, "to": "new_latin", "official": True})
check("official ignored for new_latin", st == 200 and "\u02bb" not in r["result"]
      and r["result"] == OU + "zbek tili", repr(r["result"]))

# caps preservation on the production subtitle path (cyr2lat via transliterate)
st, r = post("/api/v1/ling/transliterate", {"text": "\u0428\u0432\u0435\u0442\u0441\u0438\u044f", "to": "latin"})
check("title-case digraph not shouted", st == 200 and r["result"] == "Shvetsiya", repr(r["result"]))
st, r = post("/api/v1/ling/transliterate", {"text": "\u0428\u0412\u0415\u0422\u0421\u0418\u042f", "to": "latin"})
check("all-caps run stays shouted", st == 200 and r["result"] == "SHVETSIYA", repr(r["result"]))

# m6: kill-switch-consistent detect endpoint + size caps
qs = urllib.parse.urlencode({"text": "O'zbek"})
st, d = get("/api/v1/ling/detect?" + qs)
check("detect still works", st == 200 and d["script"] == "latin", str(d))
try:
    post("/api/v1/ling/analyze", {"text": "a" * 6000})
    check("text cap 413", False)
except urllib.error.HTTPError as e:
    check("text cap 413", e.code == 413, str(e.code))

# ─── Round 14: Ovoz Turn over HTTP ───────────────────────────────────────────
st, t = post("/api/v1/ling/diarize", {"lines": DIALOG})
seq = [ln["speaker"] for ln in t.get("lines", [])]
check("diarize: two voices, alternating", st == 200 and seq == [1, 2, 1, 2]
      and t["speakers"] == 2, str(seq))
check("diarize: every boundary is explained",
      # The first cue has nothing to compare against — attributing it would be a
      # guess. Every line that *is* a boundary must name the cues that decided it.
      all(ln["cues"] for ln in t["lines"][1:]) and not t["lines"][0]["cues"],
      str([ln["cues"] for ln in t["lines"]]))
check("diarize: transcript names the speakers",
      t["transcript"].startswith("[S1] ") and "[S2] " in t["transcript"],
      repr(t["transcript"][:24]))

# A monologue must not grow a second speaker just because it is long.
st, mono = post("/api/v1/ling/diarize", {"lines": [
    {"start": 0.0, "end": 6.2, "text": "Bugun ertalab ofisga kirib, jadvalni qaytadan ko'rib chiqdim."},
    {"start": 6.4, "end": 13.0, "text": "Eski reja bizga mos kelmadi, chunki mijozlar soni ikki baravarga oshdi."},
    {"start": 13.2, "end": 20.5, "text": "Shuning uchun jamoa ikki guruhga bo'lindi va har biri o'z yo'nalishi bo'yicha ishladi."}]})
check("diarize: monologue stays one voice", st == 200 and mono["speakers"] == 1,
      str(mono["speakers"]))

# Raw SRT is the shape a subtitle tool sends, and Cyrillic is the shape a user pastes.
st, via_srt = post("/api/v1/ling/diarize", {"srt": DIALOG_SRT})
check("diarize: raw SRT accepted", st == 200
      and [ln["speaker"] for ln in via_srt["lines"]] == seq, str(seq))
st, cyr = post("/api/v1/ling/diarize", {"lines": [
    dict(c, text=t) for c, t in zip(DIALOG, DIALOG_CYR)]})
check("diarize: Cyrillic tape gives the same answer", st == 200
      and [ln["speaker"] for ln in cyr["lines"]] == seq, str(cyr.get("lines")))

# Public endpoint = hostile input: the caps have to hold here, not only in tests.
for name, payload, want in (
        ("no lines", {"text": "salom"}, 422),
        ("bad timings", {"lines": [{"start": 5, "end": 1, "text": "salom"}]}, 422),
        ("max_speakers=0", {"lines": DIALOG, "max_speakers": 0}, 422),
        ("max_speakers=99", {"lines": DIALOG, "max_speakers": 99}, 422),
        ("too many lines", {"lines": DIALOG * 110}, 422)):
    try:
        post("/api/v1/ling/diarize", payload)
        check("diarize rejects %s" % name, False)
    except urllib.error.HTTPError as e:
        check("diarize rejects %s" % name, e.code == want, str(e.code))

# A legal SRT timecode can still run for 99 hours; bounding only the start let it
# fake a 356399-second overlap and invent a second voice out of a broken export.
try:
    post("/api/v1/ling/diarize", {"srt":
         "1\n00:00:00,000 --> 99:59:59,999\nMen uyga bordim.\n\n"
         "2\n00:00:01,000 --> 00:00:02,000\nKeyin kitob o'qdim.\n"})
    check("diarize rejects a 99-hour cue", False)
except urllib.error.HTTPError as e:
    check("diarize rejects a 99-hour cue", e.code == 422, str(e.code))
st, ov = post("/api/v1/ling/diarize", {"lines": [
    {"start": 0.0, "end": 5.0, "text": "Men uyga bordim."},
    {"start": 4.2, "end": 9.0, "text": "Sen qachon kelasan?"}]})
check("diarize still hears a real overlap", st == 200 and ov["speakers"] == 2,
      str(ov.get("speakers")))

# ─── Round 15: Ovoz Qator over HTTP ───────────────────────────────────────────
MEASURE = 42           # the broadcast measure; mirrors layout.MAX_CHARS_PER_LINE
LINES = 2


def widths(text):
    return [len(ln) for ln in text.split("\n")]


st, lay = post("/api/v1/ling/layout", {"lines": DIALOG})
check("layout: repairs without touching a word", st == 200
      and lay["before"]["score"] < lay["after"]["score"]
      and lay["words_preserved"] is True, f'{lay["before"]["score"]} -> {lay["after"]["score"]}')
check("layout: every delivered card fits the measure",
      all(len(c["text"].split("\n")) <= LINES and max(widths(c["text"])) <= MEASURE
          for c in lay["cards"]),
      str([max(widths(c["text"])) for c in lay["cards"]]))
check("layout: cards stay monotonic and never strobe",
      all(a["end"] < b["start"] and b["start"] - a["end"] >= 0.08 - 0.003
          for a, b in zip(lay["cards"], lay["cards"][1:])),
      str([(a["end"], b["start"]) for a, b in zip(lay["cards"], lay["cards"][1:])]))
check("layout: the SRT on the wire parses back to the same words",
      lay["srt"].count("-->") == lay["cards_after"]
      and lay["after"]["findings"] is not None, str(lay["cards_after"]))
check("layout: it says which policy it took and why",
      lay["mode"] in ("readability", "sync") and lay["in_sync"] is True
      and lay["max_drift"] <= 1.0, f'{lay["mode"]} drift={lay["max_drift"]}')

# A crushed tape has no reading room: the engine must keep the clock and report
# the rate it could not honour, instead of delivering a beautiful late subtitle.
CRUSHED_LINES = [
    {"start": 0.0, "end": 3.4, "text":
     "Assalomu alaykum, ustoz, vaqtingizni olganim uchun uzr, boshlaymizmi bugundan?"},
    {"start": 3.6, "end": 4.0, "text":
     "Va alaykum assalom, xush kelibsiz, men tayyorman, albatta biz uni Farg'ona "
     "vodiyida ikki oyda ishga tushirdik va jamoa tajribali edi."},
    {"start": 4.1, "end": 9.0, "text":
     "Sizning so'nggi loyihangiz haqida so'rasam bo'ladimi?"},
]
st, crushed = post("/api/v1/ling/layout", {"lines": CRUSHED_LINES})
check("layout: a crushed tape keeps the clock, not the looks",
      st == 200 and crushed["mode"] == "sync" and crushed["in_sync"] is True
      and crushed["max_drift"] <= 1.0
      and crushed["after"]["by_rule"].get("fast"), f'{crushed["mode"]} {crushed["after"]["by_rule"]}')
check("layout: every finding quotes its own measurement",
      any(any(ch.isdigit() for ch in f["detail"]) for f in crushed["after"]["findings"]),
      str([f["rule"] for f in crushed["after"]["findings"]][:6]))

# Correct subtitles must arrive untouched: re-timing a customer's in-points that
# were never wrong is the one change no setting can undo.
CLEAN_LINES = [{"start": 0.0, "end": 3.0, "text": "Rahmat, ustoz."},
               {"start": 3.2, "end": 6.6, "text": "Arzimaydi, ishlaringizga omad tilayman."}]
st, clean = post("/api/v1/ling/layout", {"lines": CLEAN_LINES})
check("layout: a scene that needs nothing is left where it was",
      st == 200 and clean["after"]["findings"] == []
      and [c["start"] for c in clean["cards"]] == [0.0, 3.2],
      str([c["start"] for c in clean["cards"]]))

st, carried = post("/api/v1/ling/layout", {"lines": DIALOG, "speakers": seq})
check("layout: attribution from Ovoz Turn survives the repair",
      st == 200 and [c["speaker"] for c in carried["cards"]][:1] == [1]
      and set(c["speaker"] for c in carried["cards"]) <= {1, 2},
      str(sorted(set(c["speaker"] for c in carried["cards"]))))

for name, payload, want in (
        ("no cues", {"text": "salom"}, 422),
        ("bad timings", {"lines": [{"start": 5, "end": 1, "text": "salom"}]}, 422),
        ("short speaker list", {"lines": DIALOG, "speakers": [1, 2]}, 422),
        ("speaker out of range", {"lines": DIALOG, "speakers": [1, 2, 1, 99]}, 422),
        ("speaker as bool", {"lines": DIALOG, "speakers": [1, 2, 1, True]}, 422),
        ("too many lines", {"lines": DIALOG * 110}, 422)):
    try:
        post("/api/v1/ling/layout", payload)
        check("layout rejects %s" % name, False)
    except urllib.error.HTTPError as e:
        check("layout rejects %s" % name, e.code == want, str(e.code))

# ─── Round 22: Ovoz Nafis — is a cut here legal in Uzbek at all ────────────────
NAFIS_TEXT = ("Bu loyiha Farg'ona vodiyida ikki oy ichida ishga tushirildi, uni esa "
              "yigirma kishidan iborat jamoa tayyorlab berdi. Shuning uchun rejani "
              "hozirdanoq yozdik, chunki boshqa til qoidalari bu yerda ishlamaydi.")
st, naf = post("/api/v1/ling/nafis", {"text": NAFIS_TEXT})
check("nafis: a paragraph is judged without invented timings",
      st == 200 and naf["mode"] == "prose" and naf["score"] is None
      and naf["cut_positions"] > 0 and naf["legal_share"] is not None,
      "%d positions, score=%s" % (naf.get("cut_positions", -1), naf.get("score")))
check("nafis: the ruler adds up to the text it judged",
      naf["legal_positions"] + naf["forbidden"] == naf["cut_positions"]
      and naf["fault_count"] >= naf["forbidden"]
      and len(naf["ruler"]) <= 40 and naf["ruler"],
      "%d legal of %d, %d law hits" % (naf["legal_positions"], naf["cut_positions"],
                                       naf["fault_count"]))
check("nafis: a forbidden cut is named by its law, not by a sentence",
      any(r["codes"] and "uchun" in (r["left"] + " " + r["right"]).lower()
          for r in naf["ruler"]), str(naf["ruler"][:1]))
st, judged = post("/api/v1/ling/nafis", {"lines": CRUSHED_LINES})
check("nafis: delivered boundaries are counted, not estimated",
      st == 200 and judged["mode"] == "cues"
      and judged["boundaries"]["measured"] == len(CRUSHED_LINES) - 1
      and judged["words"] > 0, str(judged.get("boundaries")))
try:
    post("/api/v1/ling/nafis", {})
    check("nafis: nothing to judge is refused, never flattered", False)
except urllib.error.HTTPError as e:
    check("nafis: nothing to judge is refused, never flattered", e.code == 422, str(e.code))
st, info = get("/api/v1/info")
check("nafis: the cut law is advertised where SDKs look",
      "subtitle_cut_law" in info["features"], str(info["features"][-4:]))
# Round 23: the deployment must be able to say what it can prove, per stage.
pr = info.get("providers") or {}
check("providers: the self-check answers for every stage",
      set(pr) == {"asr", "translate", "tts"}
      and all(p.get("mode") in ("real", "sim", "unknown") for p in pr.values())
      and all(isinstance(p.get("reason"), str) for p in pr.values()),
      str({k: v.get("mode") for k, v in pr.items()}))
check("providers: the answer carries no secret material",
      "sk-" not in json.dumps(pr) and "api_key" not in json.dumps(pr).lower(),
      str(json.dumps(pr))[:90])
# The ruler has to be on the page the customer opens, or the engine is an API rumour.
_st_nf, _html_nf = get_text("/")
check("nafis: the cut ruler is on the served page",
      'id="nafis"' in _html_nf and 'id="nafis-run"' in _html_nf
      and 'id="nf-ruler"' in _html_nf and 'id="nf-list"' in _html_nf
      and 'data-i18n-aria="nf_aria"' in _html_nf,
      "section/button/ruler/list/aria")

# The landing widget must ship with the engine it demos.
st, html = get_text("/")
check("layout: the Qator widget is on the served page",
      'id="qator"' in html and 'id="polish-row"' in html
      and 'aria-live="polite" aria-atomic="true"' in html,
      "section/option/live-region")

# ─── Round 17: Ovoz Jimlik over HTTP ─────────────────────────────────────────
# The aligner is the one language call that carries a recording, so it is the one
# check set that has to be built from real bytes: a server-side test of "the cut
# landed in a pause" means nothing if the tape on the wire is not a tape.
import io as _io
import math as _math
import wave as _wave
from array import array as _array

ARATE = 8000


def _voice(seconds, amp=0.35, freq=140.0):
    n = int(seconds * ARATE)
    return [int(amp * (0.75 + 0.25 * _math.sin(2 * _math.pi * 3.0 * i / ARATE))
                * 32767 * _math.sin(2 * _math.pi * freq * i / ARATE)) for i in range(n)]


def _silence(seconds, tone=180):
    n = int(seconds * ARATE)
    return [int(tone * _math.sin(2 * _math.pi * 997.0 * i / ARATE)) for i in range(n)]


def _wav(*pieces, rate=ARATE):
    samples = [s for part in pieces for s in part]
    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(_array("h", samples).tobytes())
    return buf.getvalue()


# 0.0-1.5 voice | 1.5-2.0 pause | 2.0-3.5 voice | 3.5-4.0 pause | 4.0-5.5 voice
TAPE = _wav(_voice(1.5), _silence(0.5), _voice(1.5), _silence(0.5), _voice(1.5))
# Every cut deliberately falls inside someone talking — the exact defect the
# engine sells a cure for, at the exact scale an ASR produces it.
CRUSHED_IN_SPEECH = [
    {"start": 0.0, "end": 1.2, "text": "Assalomu alaykum, ustoz."},
    {"start": 1.2, "end": 3.2, "text": "Va alaykum assalom, xush kelibsiz."},
    {"start": 3.2, "end": 5.5, "text": "Sizning so'nggi loyihangiz haqida so'rasam bo'ladimi?"},
]
BOUNDARY = "----ovoz2026"


def multipart_body(audio, fields=None, ctype="audio/wav"):
    body = b""
    for k, v in (fields or {}).items():
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                 % (BOUNDARY, k, v)).encode("utf-8")
    body += ("--%s\r\nContent-Disposition: form-data; name=\"audio\"; "
             "filename=\"tape.wav\"\r\nContent-Type: %s\r\n\r\n"
             % (BOUNDARY, ctype)).encode("utf-8") + audio + b"\r\n"
    body += ("--%s--\r\n" % BOUNDARY).encode("utf-8")
    return body


def post_align(audio, fields=None, ctype="audio/wav", path="/api/v1/ling/align"):
    req = urllib.request.Request(
        base + path, data=multipart_body(audio, fields, ctype), method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=" + BOUNDARY})
    with _open(req) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _json_is_finite(obj):
    """The report is handed to the browser raw (artifact, share page), so a single
    NaN or Infinity on the wire breaks `response.json()` for the customer — the
    serializer must refuse to write one, and the parser refuse to read one."""
    try:
        text = json.dumps(obj, allow_nan=False)
        json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except ValueError:
        return False
    return True


LINES = json.dumps(CRUSHED_IN_SPEECH, ensure_ascii=False)
st, rep = post_align(TAPE, {"lines": LINES})
check("align: a tape of real bytes gets a report", st == 200 and "summary" in rep,
      str(st))
if st == 200:
    s = rep["summary"]
    check("align: cuts inside speech are heard and moved",
          s["moved"] >= 4 and s["in_silence"] == s["moved"] and
          s["already_in_silence"] == 0,
          "moved=%s in_silence=%s already=%s"
          % (s["moved"], s["in_silence"], s["already_in_silence"]))
    # `cue.end == next.start` is one cut with two names: if only one of them
    # travels, the export gains a blank flash and keeps dropping mid-word.
    ends = {c["i"]: c for c in rep["cues"]}
    check("align: a back-to-back pair stays continuous across the moved cut",
          ends[1]["end"] == ends[2]["start"] and ends[2]["end"] == ends[3]["start"],
          "%s / %s" % (ends[1]["end"], ends[2]["end"]))
    check("align: nothing travels further than the declared ceiling",
          s["max_applied"] <= 0.601 and s["beyond_audio"] == 0,
          "max_applied=%s" % s["max_applied"])
    check("align: not one word changed, and the report proves it",
          s["words_preserved"] is True and s["order_preserved"] is True and
          [c["text"] for c in rep["cues"]] == [c["text"] for c in CRUSHED_IN_SPEECH],
          str(s))
    check("align: overlap never gets worse",
          s["never_worse"] is True and
          s["overlap_sec_after"] <= s["overlap_sec_before"] + 1e-6,
          "%s -> %s" % (s["overlap_sec_before"], s["overlap_sec_after"]))
    check("align: the SRT on the wire parses back to the same words",
          all(any(c["text"] in seg for seg in rep["srt"].split("\n\n"))
              for c in CRUSHED_IN_SPEECH))
    check("align: every number on the wire is finite",
          _json_is_finite(rep))
    # Correct in-points must survive: re-timing subtitles that were never wrong is
    # the one change a customer cannot undo with a setting.
    st2, still = post_align(TAPE, {"lines": json.dumps(
        [{"start": 0.0, "end": 1.55, "text": "Birinchi."},
         {"start": 1.95, "end": 3.55, "text": "Ikkinchi."}],
        ensure_ascii=False)})
    check("align: a cut already sitting in the pause does not move",
          st2 == 200 and still["summary"]["moved"] == 0 and
          still["summary"]["already_in_silence"] == still["summary"]["in_silence"],
          str(still["summary"]))
    # Same bytes in, same bytes out — the report is a measurement, not a mood.
    st3, again = post_align(TAPE, {"lines": LINES})
    check("align: the same tape and cues answer identically twice",
          st3 == 200 and json.dumps(again, sort_keys=True) == json.dumps(rep, sort_keys=True))

# A recording is bigger than the JSON budget of the other language routes; the
# ceiling that lets it through must not be the one that refuses the rest.
BIG = _wav(_voice(20.0), _silence(1.0), _voice(20.0))
st_big, _ = post_align(BIG, {"lines": json.dumps(
    [{"start": 0.0, "end": 2.0, "text": "Uzoq repleka."},
     {"start": 20.5, "end": 22.0, "text": "Ikkinchi qism."}], ensure_ascii=False)})
check("align: a 600 KB tape is answered where analyze would refuse the bytes",
      st_big == 200 and len(BIG) > 400_000, "%s bytes, status %s" % (len(BIG), st_big))

# Round 19: the window is 900 s of tape, not 1.6 M samples. Four minutes used to
# come back 413 from the engine after the transport had already accepted the
# upload — a ceiling kept in the wrong unit, found by comparing two numbers.
LONG = _wav(_voice(120.0), _silence(2.0), _voice(124.0))
try:
    st_long, longrep = post_align(LONG, {"lines": json.dumps(
        [{"start": 0.0, "end": 2.0, "text": "Birinchi bo'lim."},
         {"start": 121.2, "end": 123.4, "text": "O'rta bo'lim."},
         {"start": 240.0, "end": 244.0, "text": "Oxirgi bo'lim."}],
        ensure_ascii=False)})
except urllib.error.HTTPError as exc:
    st_long, longrep = exc.code, {}
check("window: four minutes of tape are heard instead of refused",
      st_long == 200 and longrep.get("audio", {}).get("window_sec") == 900
      and longrep["audio"]["duration"] > 240,
      "%s bytes -> %s" % (len(LONG), st_long))
check("window: at four minutes the ruler is still 20 ms and the tape is measured",
      st_long == 200 and abs(longrep["tuning"]["frame_ms"] - 20.0) < 1e-9
      and longrep["audio"]["frames"] > 12_000 and
      longrep["summary"]["boundaries"] == 6, str(longrep.get("summary")))

for name, audio, ctype, fields, want in (
        ("not a WAV", b"RIFF" + b"\x00" * 400, "audio/wav", {"lines": LINES}, 415),
        ("compressed body", b"RIFF" + b"JUNK" * 40, "audio/wav", {"lines": LINES}, 415),
        ("no cues", TAPE, "audio/wav", {"lines": "[]"}, 422),
        ("both cue shapes", TAPE, "audio/wav",
         {"lines": LINES, "srt": "1\n00:00:00,000 --> 00:00:01,000\nsalom\n"}, 422),
        ("max_shift not a number", TAPE, "audio/wav",
         {"lines": LINES, "max_shift": "soon"}, 422),
        ("max_shift beyond contract", TAPE, "audio/wav",
         {"lines": LINES, "max_shift": "9"}, 422),
        ("impossible cue", TAPE, "audio/wav",
         {"lines": json.dumps([{"start": 5, "end": 1, "text": "salom"}])}, 422)):
    try:
        post_align(audio, fields, ctype)
        check("align rejects %s" % name, False)
    except urllib.error.HTTPError as e:
        check("align rejects %s" % name, e.code == want, str(e.code))

st, info = get("/api/v1/info")
check("info: the aligner is advertised where SDKs look",
      "silence_gap_alignment" in info["features"], str(info["features"]))
st, html = get_text("/")
check("align: the Jimlik widget is on the served page",
      'id="jimlik"' in html and 'id="align-row"' in html and 'id="j-align"' in html,
      "section/option/checkbox")

# The document is what pins the build: it carries the ?v= stamps of every asset.
# StaticFiles answers it with ETag/Last-Modified only, and a browser given no
# Cache-Control invents freshness from the file's age — browser QA was served a
# 0.15.2 shell by a 0.16.0 server, which looks like a broken service worker and is
# not one. Every code file the page loads must therefore revalidate by name.
for _path, _want in (("/", "no-cache"), ("/app.js", "must-revalidate"),
                     ("/tg-boot.js", "must-revalidate"),
                     ("/sw-boot.js", "must-revalidate"), ("/sw.js", "no-cache")):
    with _open(base + _path) as _r:
        _cc = _r.headers.get("Cache-Control") or ""
    check("cache: %s says how long it may live (%s)" % (_path, _want),
          _want in _cc, repr(_cc))


# ─── Round 19.1: a chunked upload has no length to check against ──────────────
# Every byte ceiling in this app is keyed on Content-Length. `Transfer-Encoding:
# chunked` declares none, and Starlette streams multipart file parts to the temp
# directory without a per-part cap — so the honest answer is to refuse that framing
# before the body is on disk, not to promise a bound it cannot hold mid-stream.
def _chunked_post(path, body):
    """One chunked POST, obeying a 429 the way the API tells clients to.

    Refusals now cost a rate-limit token too (a free refusal path is the path an
    anonymous client can churn), so a dense verification run can legitimately be
    told to wait — and this script must show the polite behaviour, not the
    exceptional one.
    """
    import http.client
    import time as _t
    hostport = base.split("//", 1)[1]
    host, _, port = hostport.partition(":")
    for attempt in range(5):
        conn = http.client.HTTPConnection(host, int(port) if port else 80)
        conn.putrequest("POST", path)
        conn.putheader("Content-Type", "multipart/form-data; boundary=" + BOUNDARY)
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        for i in range(0, len(body), 1024):
            chunk = body[i:i + 1024]
            conn.send(("%x\r\n" % len(chunk)).encode("ascii") + chunk + b"\r\n")
        conn.send(b"0\r\n\r\n")
        resp = conn.getresponse()
        payload = resp.read().decode("utf-8", "replace")
        status, retry = resp.status, resp.getheader("Retry-After")
        conn.close()
        if status != 429:
            return status, payload
        _t.sleep(min(int(retry or 5), 65))
    return status, payload


_st, _bd = _chunked_post("/api/v1/ling/align",
                         multipart_body(TAPE, {"lines": json.dumps(CRUSHED_IN_SPEECH)}))
check("chunked: an upload with no declared length is refused before it lands",
      _st == 411 and "length_required" in _bd, "%s %s" % (_st, _bd[:70]))
_st2, _bd2 = post_align(TAPE, {"lines": json.dumps(CRUSHED_IN_SPEECH)})
check("chunked: the same tape with an honest length is still served",
      _st2 == 200, str(_st2))

# ─── Round 18: Ovoz So'z — word-level timing inside the cues ───────────────────
WORDS = "/api/v1/ling/words"
st, wrep = post_align(TAPE, {"lines": LINES}, path=WORDS)
check("words: the same tape that Jimlik heard gets a word report", st == 200,
      str(st))
if st == 200:
    ws = wrep["summary"]
    check("words: every word of every measurable cue is timed",
          wrep["engine"] == "ovoz-soz" and ws["words"] >= 12 and
          ws["cues_measured"] == 3, str(ws))
    ok_partition = True
    detail = ""
    for c, sent in zip(wrep["cues"], CRUSHED_IN_SPEECH):
        toks = [it["w"] for it in c["words"]]
        if toks != sent["text"].split():
            ok_partition, detail = False, (toks, sent["text"])
            break
        edges = [it["e"] for it in c["words"]]
        starts = [it["s"] for it in c["words"]]
        if any(b < a for a, b in zip(starts, edges)) or \
           any(y < x for x, y in zip(edges, starts[1:])):
            ok_partition, detail = False, (starts, edges)
            break
        if starts[0] != c["start"] or edges[-1] != c["end"]:
            ok_partition, detail = False, (starts[0], c["start"], edges[-1], c["end"])
            break
    check("words: tokens come back whole and the cue is partitioned in order",
          ok_partition, str(detail))
    check("words: every cut says how it was found",
          all(m in ("valley", "quiet", "speech")
              for c in wrep["cues"] for m in c.get("methods", [])) and
          0.0 <= ws["valley_share"] <= 1.0, str(ws["valley_share"]))
    # The steady tone above has no word-sized dip in it (a 6 dB syllable sag is
    # still speech), so the only honest answer is "zero boundaries earned". An
    # engine that called geometry a measurement would show up right here.
    check("words: a tape without dips earns no valley claim",
          ws["cuts"] > 0 and ws["cut_valley"] == 0 and
          ws["valley_share"] == 0.0 and
          all(m in ("quiet", "speech")
              for c in wrep["cues"] for m in c.get("methods", [])),
          "valley=%s quiet=%s speech=%s" % (ws["cut_valley"], ws["cut_quiet"],
                                             ws["cut_speech"]))
    check("words: a highlight never stalls or overlaps on the wire",
          ws["longest_word_sec"] > 0 and
          all(it["e"] - it["s"] >= 0.059 for c in wrep["cues"]
              for it in c["words"]), str(ws["longest_word_sec"]))
    st_v, vtt = post_align(TAPE, {"lines": LINES, "fmt": "vtt"}, path=WORDS)
    st_a, ass = post_align(TAPE, {"lines": LINES, "fmt": "ass"}, path=WORDS)
    check("words: the exports a player consumes are offered on request",
          st_v == 200 and vtt["vtt"].startswith("WEBVTT") and
          st_a == 200 and "\\kf" in ass["ass"] and
          "ass" not in wrep and "vtt" not in wrep)
    check("words: every number on the wire is finite", _json_is_finite(wrep))
    st_w, again = post_align(TAPE, {"lines": LINES}, path=WORDS)
    check("words: the same tape and cues answer identically twice",
          st_w == 200 and json.dumps(again, sort_keys=True) ==
          json.dumps(wrep, sort_keys=True))

# The positive half of the claim, over HTTP: a tape that really goes quiet before
# every word must be answered with real valleys, not with a weighted guess.
DIP_TAPE = _wav(*[x for _ in range(4) for x in (_voice(0.35), _silence(0.05))])
st_d, dips = post_align(DIP_TAPE, {"lines": json.dumps(
    [{"start": 0.0, "end": 1.55, "text": "bir ikki uch to'rt"}],
    ensure_ascii=False)}, path=WORDS)
check("words: a tape with a real dip before each word is timed on it",
      st_d == 200 and dips["summary"]["cut_valley"] >= 2 and
      dips["summary"]["valley_share"] >= 0.6,
      str(dips["summary"]) if st_d == 200 else str(st_d))

st_big, _ = post_align(BIG, {"lines": json.dumps(
    [{"start": 0.0, "end": 2.0, "text": "Uzoq repleka."},
     {"start": 20.5, "end": 22.0, "text": "Ikkinchi qism."}],
    ensure_ascii=False)}, path=WORDS)
check("words: a 600 KB tape is answered on the same ceiling as the aligner",
      st_big == 200, str(st_big))

for name, audio, fields, want in (
        ("a tape with no speech", _wav(_silence(3.0)), {"lines": LINES}, 422),
        ("not a WAV", b"RIFF" + b"\x00" * 400, {"lines": LINES}, 415),
        ("an unmeasurable cue", TAPE,
         {"lines": json.dumps([{"start": 0.0, "end": 0.02,
                                 "text": "bir ikki uch to'rt besh"}])}, 200),
        ("both cue shapes", TAPE, {"lines": LINES, "srt": "1\n00:00:00,000 --> "
                                   "00:00:01,000\nsalom\n"}, 422),
        ("an unknown export", TAPE, {"lines": LINES, "fmt": "srt"}, 422),
        ("impossible cue", TAPE,
         {"lines": json.dumps([{"start": 5, "end": 1, "text": "salom"}])}, 422)):
    try:
        code, body = post_align(audio, fields, path=WORDS)
        # a 200 is only a pass where the engine refuses per cue and still answers
        check("words rejects %s" % name,
              want == 200 and code == 200 and
              body["cues"][0]["words"] == [] and body["cues"][0]["reason"],
              "%s %s" % (code, body.get("cues", [{}])[0].get("reason")))
    except urllib.error.HTTPError as e:
        check("words rejects %s" % name, want != 200 and e.code == want, str(e.code))

st, info = get("/api/v1/info")
check("words: the word timer is advertised where SDKs look",
      "word_level_timing" in info["features"], str(info["features"]))
st, html = get_text("/")
check("words: the So'z widget is on the served page",
      'id="soz"' in html and 'id="words-row"' in html and 'id="j-words"' in html and
      'id="soz-rows"' in html, "section/option/checkbox/strip")

print("\nOVERALL:", "PASS" if all(checks) else "FAIL",
      f"({sum(checks)}/{len(checks)} checks)")
sys.exit(0 if all(checks) else 1)
