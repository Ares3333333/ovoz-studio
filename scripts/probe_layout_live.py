# -*- coding: utf-8 -*-
"""Adversarial live probe of the public /api/v1/ling/layout endpoint.

The unit tests exercise the engine; this one exercises the *product* — routing,
transport caps, what the JSON encoder is handed, and the shapes that would turn a
deterministic layout engine into a latency or memory incident. Every expectation
here is the answer we chose on purpose, including the refusals: 400x300 is a
413 because the streamed body cap is the real ceiling, and a 301-character cue is
a 422 because this endpoint promises it never cuts your text.

Usage: python scripts/probe_layout_live.py <base_url>   (UTF-8 safe on Windows.)"""
import json
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8102"
PATH = "/api/v1/ling/layout"


def post(payload, raw=None):
    data = raw.encode("utf-8") if raw is not None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + PATH, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    t0 = time.time()

    def mark():                                 # elapsed seconds, recorded once
        times.append(time.time() - t0)
        return times[-1]
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return mark(), r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return mark(), e.code, e.read().decode("utf-8", "replace")[:120]
    except Exception as e:                                    # noqa: BLE001
        return mark(), -1, repr(e)[:160]


results = []
times = []


def report(name, dt, code, body, want=200):
    ok = code == want
    note = ""
    if ok and want == 200:
        try:
            # parse_constant makes a bare Infinity/NaN in the reply a hard failure:
            # the artifact path writes with a plain json.dumps and the share page
            # calls response.json() on the other end.
            j = json.loads(body, parse_constant=lambda c: (_ for _ in ()).throw(
                ValueError("non-finite " + c)))
            note = (f" cards={j['cards_after']} mode={j['mode']}"
                    f" words={j['words_preserved']} score={j['after']['score']}")
        except Exception as e:                                # noqa: BLE001
            ok = False
            note = " " + repr(e)[:70]
    elif ok:
        # A refusal is part of the product. If the socket died on the way in, the
        # status code survives but the body does not, and the customer's SDK has
        # nothing to show — so an unreadable or unstructured refusal is a FAIL here,
        # not a cosmetic one.
        if "error_code" not in body:
            ok = False
            note = " refusal carried no structured error body"
        else:
            note = " " + body[:60].replace("\n", " ")
    else:
        note = " " + body[:70].replace("\n", " ")
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + f"{name:34s} {dt:6.2f}s {code}{note}")


W = " "
# 1. the biggest legal scene, all crushed into 1-second windows: worst case for
#    splitting, placement and the sync policy.
big = [{"start": float(i), "end": float(i) + 1.0,
        "text": (W.join(["ab" * 7] * 40))[:299]} for i in range(400)]
report("400x300 (over body cap)", *post({"lines": big})[:3], want=413)
# A body past the cap but inside the drain ceiling is the realistic SDK accident
# (someone pastes a whole script). It must still get the structured refusal, and
# it must get it quickly: the drain is bounded in bytes *and* seconds, so refusing
# cannot become a slow-loris amplifier for the server.
mid = '{"lines": [{"start": 0.0, "end": 40.0, "text": "' + "q" * 480_000 + '"}]}'
dt, code, body = post(None, raw=mid)[:3]
report("480KB body (over cap, under ceiling)", dt, code, body, want=413)
results.append(dt < 5.0)
print(("PASS " if dt < 5.0 else "FAIL ") + f"refusal is prompt                 {dt:6.2f}s")
half = big[:120]
report("120x299 crushed", *post({"lines": half})[:3])

# 2. one unbreakable word per cue: the only input where a wide card is honest, so
#    the engine must report it instead of looping on it.
wide = [{"start": float(i), "end": float(i) + 3.0, "text": "q" * 300}
        for i in range(200)]
report("200 unbreakable words", *post({"lines": wide})[:3])

# 3. recursion bait: 43-char and 1-char words alternating, so every level of a
#    card split is forced.
bait = [{"start": 0.0, "end": 60.0, "text": W.join(["z" * 43, "a"] * 60)[:299]}]
report("split recursion bait", *post({"lines": bait})[:3])

# 4. a cue whose text is only invisible characters: the validator refuses it, and
#    that is the answer we chose (an empty card is not a card).
report("reject: whitespace-only cue", *post({"lines": [
    {"start": 0.0, "end": 5.0, "text": "\n \t \u00a0 \u2028 " * 30}]})[:3], want=422)

# 5. a cue longer than the cap: refused, not trimmed — the preservation promise is
#    worthless if the transport can quietly delete the tail.
report("reject: 301-char cue", *post({"lines": [
    {"start": 0.0, "end": 20.0, "text": "q" * 301}]})[:3], want=422)

# 6. timing edges. A zero span is legal input (the audit names it); the rest are
#    input bugs and must never reach the engine.
report("zero span is audited, not refused", *post({"lines": [
    {"start": 1.0, "end": 1.0, "text": "Salom, ustoz."}]})[:3])
for name, payload in (
        ("reversed", {"lines": [{"start": 9.0, "end": 2.0, "text": "Salom, ustoz."}]}),
        ("negative start", {"lines": [{"start": -5.0, "end": 2.0, "text": "Salom"}]}),
        ("25-hour cue", {"lines": [{"start": 0.0, "end": 90000.0, "text": "Salom"}]}),
        ("401 cues", {"lines": [{"start": float(i), "end": float(i) + 1, "text": "Salom"}
                                for i in range(401)]}),
        ("speakers shorter", {"lines": [{"start": 0.0, "end": 5.0, "text": "Salom"}],
                              "speakers": []}),
        ("speaker 99", {"lines": [{"start": 0.0, "end": 5.0, "text": "Salom"}],
                        "speakers": [99]}),
        ("speaker bool", {"lines": [{"start": 0.0, "end": 5.0, "text": "Salom"}],
                          "speakers": [True]}),
):
    report("reject: " + name, *post(payload)[:3], want=422)

report("speaker 8 is the ceiling", *post(
    {"lines": [{"start": 0.0, "end": 5.0, "text": "Salom, ustoz."}], "speakers": [8]})[:3])

# A hand-written body can carry the JSON constants no encoder produces. The
# chained timing comparison in the validator drops them, because an `end` of NaN
# survives `end < start` and would otherwise land in the reply as `Infinity`.
report("reject: literal NaN", *post(None, raw='{"lines": [{"start": 0.0, "end": NaN,'
      ' "text": "Salom"}]}')[:3], want=422)
report("reject: literal Infinity", *post(None, raw='{"lines": [{"start": 0.0,'
      ' "end": Infinity, "text": "Salom"}]}')[:3], want=422)

# 7. determinism over the wire, twice, on a hostile scene.
scene = {"lines": [{"start": 0.0, "end": 0.9, "text": (W.join(["word"] * 40))[:299]},
                   {"start": 1.0, "end": 1.1, "text": "Qisqa."},
                   {"start": 1.15, "end": 9.0,
                    "text": "Va uzun javob " + W.join(["so'z"] * 40)[:250]}]}
_, c1, b1 = post(scene)[:3]
_, c2, b2 = post(scene)[:3]
same = c1 == c2 == 200 and json.loads(b1)["srt"] == json.loads(b2)["srt"]
results.append(same)
# a bare FAIL here is worthless at 2 a.m., so say which half broke
why = ("both 200" if c1 == c2 == 200 else f"codes {c1}/{c2}")
if c1 == c2 == 200 and not same:
    why = "SRT differs"
print(("PASS " if same else "FAIL ") + f"byte-identical repeat over the wire  {why}")

# 8. the language law, measured on the answer the server actually sent. Every
#    boundary decision in Qator funnels through one cost function, and Ovoz Nafis
#    forbids tearing a word group in two across that boundary: a postposition that
#    opens the next card, a compound verb split as `tayyorlayotgan || bo'lsangiz`, a
#    numeral away from its unit. Unit tests prove the engine; this proves the deploy.
from pathlib import Path                                        # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ling import nafis                                      # noqa: E402

PROSE = [
    "Assalomu alaykum hurmatli tomoshbinlar, bugun biz yangi loyihaning natijalarini "
    "birga ko'rib chiqamiz va har bir bosqichni alohida tushuntirib o'tamiz.",
    "Bu loyiha Farg'ona vodiyida ikki oy ichida ishga tushirildi, uni esa yigirma "
    "kishidan iborat jamoa o'z vaqtida va to'liq tayyorlab berdi.",
    "Agar siz ham o'zbek tilida subtitr tayyorlayotgan bo'lsangiz, unda avval matnni "
    "tekshirib, keyin esa tezlikni o'lchab ko'rishingiz shart, chunki boshqa til "
    "qoidalari bu yerda ishlamaydi.",
    "Menimcha, eng qiyin qismi \u2014 bu so'zning oxirini kesmasdan satrga sig'dirish, "
    "chunki o'zbek tilida ko'pgina so'zlar uzun va ularni bo'lib bo'lmaydi.",
    "Kelasi yilda biz ikkinchi vodiy loyihasini ham boshlaymiz, uni esa xalqaro "
    "hamkorlar moliyalashtiradi, shuning uchun rejani hozirdanoq tayyorlab qo'ydik.",
]
lines, _t = [], 0.0
for _s in PROSE:
    _d = max(3.0, len(_s) / 19.0)          # deliberately too fast: forces real splits
    lines.append({"start": round(_t, 2), "end": round(_t + _d, 2), "text": _s})
    _t += _d + 0.12
_, cl, bl = post({"lines": lines})[:3]
hard = []
if cl == 200:
    cards = json.loads(bl)["cards"]
    for a, z in zip(cards, cards[1:]):
        lw, rw = nafis.words_of(a["text"]), nafis.words_of(z["text"])
        faults = nafis.cut_faults(lw + rw, len(lw))
        if faults:
            hard.append((lw[-1], rw[0], [f[0] for f in faults]))
    law_note = f" cards={len(cards)}"
else:
    law_note = " " + bl[:60].replace("\n", " ")
law_ok = cl == 200 and not hard
results.append(law_ok)
print(("PASS " if law_ok else "FAIL ") + f"law: no boundary tears a word group   "
      f"{len(hard)} forbidden{law_note}{' ' + str(hard[:2]) if hard else ''}")

print("\nPROBE:", "PASS" if all(results) else "FAIL",
      f"({sum(results)}/{len(results)})",
      f"slowest={max(times) if times else 0.0:.2f}s")
sys.exit(0 if all(results) else 1)
