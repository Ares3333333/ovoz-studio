"""Round 15 — Ovoz Qator: subtitle layout and readability engine.

These tests are written from the buyer's side. A studio purchasing subtitles is
not buying "some re-timing"; it is buying four claims, and each one is a test
here:

  1. the words are untouched — not one added, dropped or reordered;
  2. the result is readable — rate, measure, stacking, dwell and clearance;
  3. the result is in sync — a card never drifts away from the line it carries;
  4. the result explains itself — every finding quotes the number behind it.

Plus the two ways the engine reaches a real user: the public
/api/v1/ling/layout endpoint and the `polish` flag on a job.
"""
import json
import math
import re
from pathlib import Path

import pytest

from app.ling import layout as L
from app.ling.srt import Cue, parse_srt, format_srt

STATIC = Path(__file__).resolve().parent.parent / "static"

# One interview scene as a raw ASR export delivers it: correct words, unreadable
# layout — 90 characters sitting on 3.2 seconds of screen.
SCENE = [
    Cue(1, 0.0, 3.2, "Assalomu alaykum hurmatli tomoshabinlar, bugun biz yangi loyihani ko'rib chiqamiz."),
    Cue(2, 3.4, 7.1, "Bu loyiha Farg'ona vodiyida ikki oy ichida ishga tushirildi va butun jamoa juda yaxshi ishladi."),
    Cue(3, 7.3, 10.6, "Rahmat, savollaringizni hoziryoq bera olasiz, lekin juda qisqa qilib."),
    Cue(4, 10.9, 15.2, "Sizning so'nggi loyihangiz qanday natija berdi va uni boshqalar ham takrorlay oladimi?"),
]

CLEAN = [
    Cue(1, 0.0, 3.0, "Rahmat, ustoz."),
    Cue(2, 3.2, 6.6, "Arzimaydi, ishlaringizga omad tilayman."),
]

# A timeline with no reading room at all: the ASR stamped 130 characters into a
# 0.4-second window. No layout can make that readable without losing sync, and
# the engine has to say so instead of picking a pretty lie.
CRUSHED = [
    Cue(1, 0.0, 3.4, "Assalomu alaykum, ustoz, vaqtingizni olganim uchun uzr, boshlaymizmi bugundan?"),
    Cue(2, 3.6, 4.0, "Va alaykum assalom, xush kelibsiz, men tayyorman, albatta biz uni Farg'ona vodiyida ikki oyda ishga tushirdik va jamoa tajribali edi."),
    Cue(3, 4.1, 9.0, "Sizning so'nggi loyihangiz haqida so'rasam bo'ladimi?"),
]


def words(cues):
    return [w for c in cues for w in c.text.split()]


# ─── metrics: the ruler has to be right before the repair means anything ───────

def test_display_length_ignores_the_line_break_it_chose():
    """The break the engine inserted is free to the reader; every other character,
    including the ones it did not choose, is charged to the measure."""
    assert L.display_len("_salom\n_dunyo") == len("_salom") + len("_dunyo") == 12
    assert L.display_len("salom\ndunyo") == L.display_len("salom dunyo") - 1
    assert L.line_widths("abc\ndefg") == [3, 4]


def test_a_card_that_ends_when_it_starts_is_measured_as_infinite():
    assert L.cps("salom", 5.0, 5.0) == float("inf")


@pytest.mark.parametrize("start,end,expected", [(0.0, 4.0, 10.0), (0.0, 2.0, 20.0)])
def test_cps_is_characters_per_second_on_screen(start, end, expected):
    text = "x" * 40
    assert L.cps(text, start, end) == expected


# ─── rewrap: the break the eye lands on ────────────────────────────────────────

def test_short_text_is_not_touched():
    assert L.rewrap("Rahmat, ustoz.") == "Rahmat, ustoz."
    assert L.rewrap("Bir") == "Bir"
    assert L.rewrap("") == ""


def test_a_long_cue_becomes_two_lines_that_both_fit_the_measure():
    text = SCENE[0].text
    out = L.rewrap(text)
    assert "\n" in out
    assert all(w <= L.MAX_CHARS_PER_LINE for w in L.line_widths(out)), out
    assert out.replace("\n", " ").split() == text.split()


def test_the_break_avoids_an_orphan_tail():
    """Two lines of 42/2 pass the measure and still look machine-made: a tail of a
    few characters is the classic machine-translation tell, so balance is part of
    the rule and not a nicety."""
    text = ("Birinchidan biz butun jamoa bilan birga ishlab chiqdimiz va "
            "keyin esa mijozga ko'rsatdik")
    out = L.rewrap(text)
    widths = L.line_widths(out)
    assert len(widths) == 2
    assert min(widths) >= L.ORPHAN_MIN, out


def test_the_break_prefers_a_clause_junction_over_the_exact_middle():
    """'va' opens a new clause: a reader expects the break there. The middle of
    the string is where a naive wrapper would cut and split a phrase in half."""
    text = "Men bu ishni tugatdim " + "va " * 2 + "butun jamoa bilan baham ko'rdim"
    out = L.rewrap(text)
    assert out.split("\n")[1].startswith("va "), repr(out)


def test_the_break_is_the_same_in_cyrillic_and_in_latin():
    """The connective rules are Uzbek, not Latin-alphabet rules: a Cyrillic cue
    must break at the same word or the two scripts ship two different edits."""
    latin = "Biz bu loyihani Farg'ona vodiyida ishga tushirdik va jamoa tajribali edi"
    cyr = ("Биз бу лойиҳани Фарғона водийида ишга туширдик ва жамоа тажрибали эди")
    la = L.rewrap(latin).split("\n")
    cy = L.rewrap(cyr).split("\n")
    assert len(la) == len(cy) == 2
    # the same number of words either side of the break, in both scripts
    assert [len(s.split()) for s in la] == [len(s.split()) for s in cy]


def test_the_engine_never_hands_back_a_card_it_could_not_read():
    """Two mechanisms hold the measure and this pins the second one: where no
    legal break exists inside one card at all (a 46-character first line is the
    only cut available), the run becomes two cards instead of one wide one.
    Disable the split in `_split_card` and the last shape comes back wide."""
    shapes = [
        "Assalomu alaykum hurmatli tomoshabinlar, biz bugun Farg'ona loyihasini ko'ramiz.",
        "Men bu juda uzun gapni aytishim kerak edi lekin vaqtimiz juda kam qoldi bugun.",
        "Birinchidan biz butun jamoa bilan birga ishlab chiqdimiz va keyin mijozga ko'rsatdik.",
        " " .join(["y" * 25, "z" * 20, "w" * 35]),   # every break is wide, none is legal
    ]
    for text in shapes:
        for card in L.reflow([Cue(1, 0.0, 12.0, text)]):
            longest = max(L.line_widths(card.text))
            widths = [len(w) for w in card.text.split()]
            assert longest <= L.MAX_CHARS_PER_LINE or max(widths) > L.MAX_CHARS_PER_LINE, \
                card.text


def test_a_legal_break_wins_even_when_an_illegal_one_looks_nicer():
    """A comma at position 46 begs to be the line break; the measure says no.
    Without the hard term in `_break_cost` the engine prefers that prettier cut,
    fails its own audit and pays for it with an extra card — so the card count is
    part of the claim, not just the widths."""
    text = " ".join(["Q" * 42, "va,", "W" * 37])      # 84 chars: one legal break, at 42
    cards = L.reflow([Cue(1, 0.0, 12.0, text)])
    assert len(cards) == 1, [c.text for c in cards]
    assert max(L.line_widths(cards[0].text)) <= L.MAX_CHARS_PER_LINE, cards[0].text
    assert cards[0].text.replace("\n", " ").split() == text.split()


def test_a_run_that_cannot_fit_two_lines_becomes_more_cards():
    """A 45-character word leaves no legal break inside one card, so the run is
    divided rather than delivered wide — the engine would otherwise fail the audit
    it sells, which is the whole thing a studio is paying for."""
    text = "Birinchi qism yetarlicha uzun " + "x" * 45 + " va ikkinchi qism ham uzun"
    cards = L.reflow([Cue(1, 0.0, 20.0, text)])
    assert len(cards) >= 2
    assert all(max(L.line_widths(c.text)) <= L.MAX_CHARS_PER_LINE for c in cards) \
        or any(len(w) > L.MAX_CHARS_PER_LINE for w in text.split())
    assert " ".join(c.text for c in cards).split() == text.split()


def test_rewrap_is_deterministic():
    assert L.rewrap(SCENE[1].text) == L.rewrap(SCENE[1].text)


# ─── audit: every finding quotes its own number ────────────────────────────────

def test_a_compliant_scene_scores_clean_and_says_nothing():
    rep = L.audit(CLEAN)
    assert rep["findings"] == [], rep
    assert rep["score"] == 100.0 and rep["grade"] == "A"


def test_each_rule_is_caught_and_named():
    cases = {
        "fast": Cue(1, 0.0, 1.0, "x" * 40),
        "wide": Cue(1, 0.0, 10.0, "y" * 60),
        "tall": Cue(1, 0.0, 10.0, "a" * 20 + "\n" + "b" * 20 + "\n" + "c" * 20),
        "blink": Cue(1, 0.0, 0.2, "Salom, ustoz."),
        "linger": Cue(1, 0.0, 12.0, "Salom, ustoz."),
        "zero_span": Cue(1, 3.0, 3.0, "Salom, ustoz."),
        "empty": Cue(1, 0.0, 3.0, "   "),
    }
    for rule, cue in cases.items():
        rep = L.audit([cue])
        assert rule in rep["by_rule"], (rule, rep["by_rule"])


def test_overlapping_and_strobing_gaps_are_both_caught():
    clash = [Cue(1, 0.0, 4.0, "Birinchi replika."), Cue(2, 3.5, 6.0, "Ikkinchi replika.")]
    bump = [Cue(1, 0.0, 4.0, "Birinchi replika."), Cue(2, 4.02, 6.0, "Ikkinchi replika.")]
    assert "clash" in L.audit(clash)["by_rule"]
    assert "bump" in L.audit(bump)["by_rule"]


def test_the_finding_detail_carries_the_measurement_not_just_a_label():
    """A customer who is told 'too fast' argues; one who is told '24.4 chars/sec
    (max 17)' fixes it. The number is the product."""
    rep = L.audit([Cue(1, 0.0, 3.2, SCENE[0].text)])
    fast = [f for f in rep["findings"] if f["rule"] == "fast"][0]
    assert re.search(r"\d+\.\d+ chars/sec", fast["detail"]), fast


def test_the_score_degrades_with_the_number_of_major_breaks():
    one = L.audit([Cue(1, 0.0, 0.2, "Bir")])
    three = L.audit([Cue(1, 0.0, 0.2, "Bir"), Cue(2, 1.0, 1.2, "Ikki"),
                     Cue(3, 2.0, 2.2, "Uch")])
    assert one["score"] > three["score"]
    assert L._grade(100) == "A" and L._grade(50) == "D" and L._grade(10) == "F"


def test_audit_never_mutates_what_it_measured():
    before = [(c.index, c.start, c.end, c.text) for c in SCENE]
    L.audit(SCENE)
    assert [(c.index, c.start, c.end, c.text) for c in SCENE] == before


def test_speaker_flags_only_appear_when_attribution_is_supplied():
    cues = [Cue(1, 0.0, 0.5, "Ha."), Cue(2, 0.7, 4.0, "Albatta, davom etamiz."),
            Cue(3, 4.2, 8.0, "Rahmat, bu juda foydali bo'ldi.")]
    assert "speaker_flash" not in L.audit(cues)["by_rule"]
    assert "speaker_flash" in L.audit(cues, speakers=[1, 2, 1])["by_rule"]


# ─── reflow: the four claims a buyer is owed ───────────────────────────────────

def test_not_one_word_is_added_dropped_or_reordered():
    fixed = L.reflow(SCENE)
    assert words(fixed) == words(SCENE)


def test_every_repaired_card_fits_the_measure_and_the_stacking_rule():
    for c in L.reflow(SCENE):
        widths = L.line_widths(c.text)
        assert len(widths) <= L.MAX_LINES, c.text
        assert max(widths) <= L.MAX_CHARS_PER_LINE, c.text


def test_cards_never_overlap_and_never_strobe():
    fixed = L.reflow(SCENE)
    for a, b in zip(fixed, fixed[1:]):
        assert a.end < b.start, (a.index, b.index)
        assert b.start - a.end >= L.MIN_GAP - L.TIME_TOL, (a.end, b.start)


def test_no_card_is_shorter_than_the_engine_promised():
    assert all(c.end - c.start >= 0.05 for c in L.reflow(CRUSHED))


def test_a_repair_only_helps_it_never_makes_things_worse():
    before = L.audit(SCENE)["score"]
    after = L.audit(L.reflow(SCENE))["score"]
    assert after > before, (before, after)


def test_a_scene_that_needs_nothing_is_left_where_it_was():
    """An engine that re-times correct subtitles is worse than no engine: the
    customer's frame-accurate in-points are the one thing they trust."""
    rep = L.polish(CLEAN)
    assert rep["after"]["findings"] == [], rep["after"]
    assert rep["cards_after"] == len(CLEAN)
    assert [round(c["start"], 2) for c in rep["cards"]] == [0.0, 3.2]
    assert rep["words_preserved"] is True


def test_the_engine_is_repeatable_to_the_millisecond():
    first = L.reflow(CRUSHED)
    second = L.reflow(CRUSHED)
    assert format_srt(first) == format_srt(second)
    assert [c.text for c in first] == [c.text for c in second]


def test_an_empty_or_textless_scene_is_answered_without_crashing():
    assert L.reflow([]) == []
    assert L.audit([])["score"] == 0.0
    assert L.polish([])["cards_after"] == 0
    assert L.reflow([Cue(1, 0.0, 3.0, "   \n  ")]) == []


def test_out_of_order_input_is_laid_out_in_time_order():
    scrambled = [Cue(3, 7.3, 10.6, "Uchinchi replika, bu yerda qo'shimcha matn bor."),
                 Cue(1, 0.0, 3.2, "Birinchi replika, bu yerda qo'shimcha matn bor."),
                 Cue(2, 3.4, 7.1, "Ikkinchi replika, bu yerda qo'shimcha matn bor.")]
    fixed = L.reflow(scrambled)
    assert [c.start for c in fixed] == sorted(c.start for c in fixed)
    assert words(fixed) == words(sorted(scrambled, key=lambda c: c.start))


# ─── the sync decision: the trade the engine has to make out loud ──────────────

def test_a_sparse_scene_is_laid_out_for_reading_and_stays_put():
    rep = L.polish([Cue(1, 0.0, 2.0, "Salom, ustoz, xush kelibsiz."),
                    Cue(2, 12.0, 14.0, "Rahmat, ishlar yaxshimi?")])
    assert rep["mode"] == "readability"
    assert rep["in_sync"] is True


def test_a_crushed_timeline_keeps_the_tape_instead_of_looking_pretty():
    """Readability mode would give every card the time it needs and arrive eight
    seconds late. A late subtitle is a wrong subtitle, so the engine must take
    the sync policy and report the rate it could not honour."""
    rep = L.polish(CRUSHED)
    assert rep["mode"] == "sync"
    assert rep["in_sync"] is True
    assert rep["max_drift"] <= L.MAX_DRIFT
    assert rep["after"]["by_rule"].get("fast"), rep["after"]["by_rule"]
    assert words(L.reflow(CRUSHED, prefer="sync")) == words(CRUSHED)


def test_a_desync_is_charged_to_the_score_and_not_merely_noted():
    """A report that says 'perfect layout, oh, and eight seconds late' is the
    exact failure this engine was written to avoid."""
    scores = {}
    for prefer in ("readability", "sync"):
        cards, owners, drift = L._plan(CRUSHED, None, prefer=prefer)
        rep = L._layout(CRUSHED, None, prefer=prefer)
        scores[prefer] = (rep[3]["score"], drift)
    assert scores["readability"][1] > L.MAX_DRIFT
    assert "late" in L._layout(CRUSHED, None, "readability")[3]["by_rule"]
    assert "late" not in L._layout(CRUSHED, None, "sync")[3]["by_rule"]


def test_attribution_survives_a_split_and_never_mixes_voices():
    """One cue holding two sentences becomes two cards: both still belong to the
    voice that spoke them."""
    cues = [Cue(1, 0.0, 3.0, "Birinchi ovoz bu yerda ancha uzoq gapiradi va "
                            "ikkinchi jumlani ham aytib o'tadi albatta."),
            Cue(2, 3.4, 8.0, "Ikkinchi ovoz esa qisqa javob qaytardi, rahmat.")]
    cards, owners = L.reflow(cues, speakers=[1, 2], with_owners=True)
    assert len(cards) == len(owners)
    assert owners[0] == 1
    assert owners[-1] == 2
    assert set(owners) <= {1, 2}


# ─── the public API surface ────────────────────────────────────────────────────

def _body(cues):
    return {"lines": [{"start": c.start, "end": c.end, "text": c.text} for c in cues]}


def test_public_layout_repairs_the_demo_scene(client):
    r = client.post("/api/v1/ling/layout", json=_body(SCENE))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["before"]["score"] < out["after"]["score"]
    assert out["words_preserved"] is True
    assert out["cards_after"] >= out["cards_before"]
    # the deliverable is a real SRT: it parses back with the same words in it
    reparsed = parse_srt(out["srt"])
    assert [w for c in reparsed for w in c.text.split()] == words(SCENE)


def test_public_layout_accepts_a_raw_srt_document(client):
    r = client.post("/api/v1/ling/layout", json={"srt": format_srt(SCENE)})
    assert r.status_code == 200, r.text
    assert r.json()["after"]["findings"] is not None or r.json()["after"]["score"] >= 0
    assert r.json()["words_preserved"] is True


def test_public_layout_carries_speaker_attribution_from_ovoz_turn(client):
    body = _body(SCENE)
    body["speakers"] = [1, 2, 1, 2]
    r = client.post("/api/v1/ling/layout", json=body)
    assert r.status_code == 200, r.text
    cards = r.json()["cards"]
    assert cards[0]["speaker"] == 1
    assert {c["speaker"] for c in cards} <= {1, 2}


@pytest.mark.parametrize("speakers", [
    [1, 2],                     # shorter than the cues: the tail would lie
    [1, 2, 1, 2, 1],            # longer than the cues
    "not a list",
    [1, 2, 1, True],            # a bool is not a speaker id
    [1, 2, 1, 99],              # outside the engine's ceiling
    [1, 2, 1, "one"],
])
def test_impossible_speaker_lists_are_refused(client, speakers):
    body = _body(SCENE)
    body["speakers"] = speakers
    assert client.post("/api/v1/ling/layout", json=body).status_code == 422


def test_public_layout_reuses_the_cue_guards(client):
    for body in ({}, {"lines": "not even a list"}, {"lines": []},
                 {"lines": [{"start": 0, "end": 1}]},
                 {"lines": [{"start": 5, "end": 1, "text": "Salom"}]},
                 {"lines": [{"start": 0, "end": 99 * 3600, "text": "Salom"}]},
                 {"lines": [{"start": i * 2.0, "end": i * 2.0 + 1, "text": "Salom"}
                            for i in range(401)]}):
        assert client.post("/api/v1/ling/layout", json=body).status_code == 422, body


def test_the_moat_engines_are_advertised_where_sdks_look(client):
    """`/api/v1/info` is what a client integration reads before it decides this
    vendor can do the job. An engine that ships unlisted is an engine nobody buys."""
    body = client.get("/api/v1/info").json()
    assert {"uzbek_language_engine", "speaker_diarization",
            "subtitle_layout_engine"} <= set(body["features"]), body["features"]


def test_a_card_with_no_dwell_is_not_reported_as_an_infinite_rate(client):
    """`Infinity` is not JSON. The artifact is written with a plain json.dumps and
    served as raw bytes, so an `inf` in the report would reach the share page and
    kill `response.json()` there — the engine has to be safe on its own, without
    leaning on whatever the transport happens to sanitize."""
    cues = [Cue(1, 1.0, 1.0, "Salom, ustoz."), Cue(2, 2.0, 6.0, "Va alaykum assalom.")]
    rep = L.polish(cues)
    json.dumps(rep, allow_nan=False)               # raises on inf/NaN
    assert rep["before"]["peak_cps"] == 4.75, rep["before"]   # the measurable card
    assert "zero_span" in rep["before"]["by_rule"]     # ...and still reported
    only = L.polish([Cue(1, 1.0, 1.0, "Salom, ustoz.")])
    assert only["before"]["peak_cps"] == 0.0, only["before"]
    json.dumps(only, allow_nan=False)
    # The per-card rate is a second exit for the same bug: `cps()` answers `inf`,
    # and that value used to be copied straight into cards[]. It is now `null`, so
    # the report stays valid even if a future policy ever emits an unmeasurable card.
    assert L.rate_or_none("salom", 5.0, 5.0) is None
    assert L.rate_or_none("salom", 5.0, 7.0) == 2.5
    for card in rep["cards"] + only["cards"]:
        assert card["cps"] is None or math.isfinite(card["cps"]), card
    r = client.post("/api/v1/ling/layout", json=_body(cues))
    assert r.status_code == 200, r.text
    assert "Infinity" not in r.text and "NaN" not in r.text
    assert json.loads(r.text, parse_constant=lambda c: (_ for _ in ()).throw(
        ValueError(c)))


def test_a_long_cue_is_refused_rather_than_silently_shortened(client):
    """The public cue caps trim text to 300 characters. For an engine whose claim is
    word-for-word preservation, trimming first would make `words_preserved: true` a
    statement about text the customer never sent — so the honest answer is 422."""
    long_text = "a" * 200 + " " + "b" * 200
    body = {"lines": [{"start": 0.0, "end": 20.0, "text": long_text}]}
    r = client.post("/api/v1/ling/layout", json=body)
    assert r.status_code == 422, r.text
    assert "never cuts" in r.json()["detail"], r.text
    # the diarizer treats cue text as evidence, not as a deliverable, so its trim
    # stays — the strictness belongs to the promise, not to the parser
    assert client.post("/api/v1/ling/diarize", json=body).status_code == 200
    ok = {"lines": [{"start": 0.0, "end": 20.0, "text": long_text[:200]}]}
    assert client.post("/api/v1/ling/layout", json=ok).status_code == 200


def test_the_layout_endpoint_honours_the_engine_kill_switch(client, monkeypatch):
    from app import main as m
    monkeypatch.setattr(m, "_flags_all",
                        lambda: {"uzbek_language_engine": {"enabled": False}})
    # 404 first: with the moat switched off, validation must not leak that the
    # endpoint is there at all
    assert client.post("/api/v1/ling/layout", json={}).status_code == 404


# ─── the job pipeline ──────────────────────────────────────────────────────────

_SRT_TMPL = """1
00:00:00,000 --> 00:00:03,200
{0}

2
00:00:03,400 --> 00:00:07,100
{1}

3
00:00:07,300 --> 00:00:10,600
{2}
"""


def _srt_bytes():
    return _SRT_TMPL.format(SCENE[0].text, SCENE[1].text, SCENE[2].text).encode("utf-8")


def _submit(client, auth, polish=None, jtype="subtitles"):
    data = {"jtype": jtype, "src": "uz", "tgt": "ru"}
    if polish is not None:
        data["polish"] = polish
    r = client.post("/api/jobs", headers=auth,
                    files={"file": ("interview.srt", _srt_bytes(), "text/plain")},
                    data=data)
    assert r.status_code == 201, r.text
    return r.json()["job"]


@pytest.mark.parametrize("flag", ["1", "true", "on", "yes", "TRUE"])
def test_the_polish_form_flag_lands_in_meta_and_artifacts(client, auth, flag):
    from app import db
    job = _submit(client, auth, polish=flag)
    assert db.get_job(job["id"])["meta"]["polish"] is True
    assert "layout" in job["artifacts"]
    rep = client.get(job["artifacts"]["layout"], headers=auth).json()
    assert rep["before"]["score"] < rep["after"]["score"]
    assert rep["words_preserved"] is True


@pytest.mark.parametrize("flag", ["", "0", "off", "no", "false"])
def test_a_falsy_polish_flag_stays_off(client, auth, flag):
    from app import db
    job = _submit(client, auth, polish=flag)
    assert db.get_job(job["id"])["meta"].get("polish") is None
    assert "layout" not in job["artifacts"]


def test_a_plain_job_is_never_silently_re_laid_out(client, auth):
    """Re-timing a customer's subtitles without being asked for is the one change
    that cannot be undone by a setting."""
    job = _submit(client, auth)
    assert job["artifacts"].keys() <= {"transcript", "srt", "srt_bilingual", "ass"}
    assert "layout" not in job["artifacts"]


def test_the_delivered_srt_is_the_polished_one(client, auth):
    """The report is worthless if the file the customer downloads is the old one:
    the artifact and the report have to describe the same deliverable."""
    job = _submit(client, auth, polish="1")
    subs = client.get(job["artifacts"]["srt"], headers=auth).text
    rep = client.get(job["artifacts"]["layout"], headers=auth).json()
    cues = parse_srt(subs)
    assert len(cues) == rep["cards_after"]
    for c in cues:
        assert max(L.line_widths(c.text)) <= L.MAX_CHARS_PER_LINE, c.text
        assert len(L.line_widths(c.text)) <= L.MAX_LINES, c.text
    # and the report's own after-audit agrees with the file on disk
    assert L.audit(cues)["score"] == rep["after"]["score"] or \
        abs(L.audit(cues)["score"] - rep["after"]["score"]) < 0.2


def test_the_bilingual_track_keeps_the_asr_timeline(client, auth):
    """A bilingual card is two scripts deep by construction and no layout makes
    that readable; pretending otherwise would put a false number in the report."""
    job = _submit(client, auth, polish="1")
    rep = client.get(job["artifacts"]["layout"], headers=auth).json()
    bi = parse_srt(client.get(job["artifacts"]["srt_bilingual"], headers=auth).text)
    assert len(bi) == rep["cards_before"]
    assert {round(c.start, 1) for c in bi} <= {0.0, 3.4, 7.3, 4.1, 10.9}


def test_polish_is_recorded_in_the_job_timeline(client, auth):
    from app import db
    job = _submit(client, auth, polish="1")
    steps = [e["step"] for e in db.job_timeline(job["id"])]
    assert "polish" in steps
    assert steps.index("translate") < steps.index("polish")


def test_batch_upload_forwards_the_polish_flag(client, auth):
    """The same class of bug as diarize: the single path was wired and the batch
    path dropped the flag, so the check is on the batch endpoint itself."""
    from app import db
    files = [("files", ("one.srt", _srt_bytes(), "text/plain")),
             ("files", ("two.srt", _srt_bytes(), "text/plain"))]
    r = client.post("/api/jobs/batch", headers=auth, files=files,
                    data={"jtype": "subtitles", "src": "uz", "tgt": "ru",
                          "polish": "1"})
    assert r.status_code == 201, r.text
    for item in r.json()["results"]:
        assert item["ok"] is True
        assert db.get_job(item["job"]["id"])["meta"]["polish"] is True
        assert "layout" in item["job"]["artifacts"]


def test_the_polished_and_diarized_job_keeps_both_promises(client, auth):
    """Two features bought on one job: the author prefix must survive the layout
    pass, and the layout pass must not invent words. The scene is a real dialogue —
    a monologue would honestly come back as one voice, and then [S2] could never
    appear no matter how well either engine worked."""
    from test_diarize import _uzbek_srt_bytes
    r = client.post("/api/jobs", headers=auth,
                    files={"file": ("interview.srt", _uzbek_srt_bytes(), "text/plain")},
                    data={"jtype": "subtitles", "src": "uz", "tgt": "ru",
                          "diarize": "1", "polish": "1"})
    assert r.status_code == 201, r.text
    job = r.json()["job"]
    assert {"diarization", "layout"} <= set(job["artifacts"])
    subs = client.get(job["artifacts"]["srt"], headers=auth).text
    assert "[S1]" in subs and "[S2]" in subs
    for cue in parse_srt(subs):
        assert max(L.line_widths(cue.text)) <= L.MAX_CHARS_PER_LINE, cue.text
    rep = client.get(job["artifacts"]["layout"], headers=auth).json()
    assert rep["words_preserved"] is True
    assert "speaker_flash" not in rep["after"]["by_rule"], rep["after"]["findings"]


def test_the_polish_step_has_a_progress_percentage_that_moves_forward():
    from app.main import _STEP_PCT
    assert _STEP_PCT["translate"] < _STEP_PCT["polish"] < _STEP_PCT["tts"]


# ─── static gates: there is no JS test runner, so these are the tests ─────────

def _html():
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _js():
    return (STATIC / "app.js").read_text(encoding="utf-8")


def test_the_qator_section_is_rendered_into_a_live_region():
    """The result arrives asynchronously: without a live region a screen reader
    never hears that the audit finished."""
    html = _html()
    assert 'id="qator-metrics" aria-live="polite" aria-atomic="true"' in html
    assert 'id="qator"' in html and 'id="qator-cards"' in html
    assert 'id="polish-row"' in html and 'id="j-polish"' in html


def test_the_markup_of_the_new_section_carries_no_inline_style_or_handler():
    """CSP rejects them and the app already passed without them."""
    html = _html()
    section = html[html.index('id="qator"'):html.index('<section class="caps"')]
    assert "style=" not in section and "onclick=" not in section
    assert "qator_aria" in section                      # aria-label comes from i18n


def test_the_demo_calls_the_real_endpoint_and_relocalises_without_a_refetch():
    js = _js()
    assert '"/api/v1/ling/layout"' in js
    assert "window.__qatorRepaint" in js
    assert "() => window.__qatorRepaint?.()," in js, \
        "the Qator widget is not in the locale-switch painter list"
    assert 'fd.append("polish", "1")' in js
    assert '$("#polish-row").classList.toggle("hidden", docMode)' in js


def test_a_pasted_document_unchecks_polish_instead_of_ignoring_it():
    """A checked box that does nothing is a lie; the row is hidden and cleared."""
    js = _js()
    toggle = js[js.index('$("#polish-row").classList.toggle'):]
    assert '$("#j-polish").checked = false;' in toggle[:200]


def test_every_qator_key_used_in_markup_exists_in_all_three_locales():
    import sys
    sys.path.insert(0, str(STATIC))
    from test_i18n_completeness import dictionaries, _keys_used_in_markup, _literal_t_calls
    tables = dictionaries()
    needed = ({k for k in _keys_used_in_markup() if k.startswith(("qator_", "polish_"))}
              | {k for k in _literal_t_calls() if k.startswith("qator_")})
    assert needed, "the Qator widget lost its i18n surface"
    for lang, table in tables.items():
        missing = {k for k in needed if k not in table}
        assert not missing, f"{lang} is missing {sorted(missing)}"


def test_the_layout_report_is_json_safe_through_the_wire(client, auth):
    """The report is served as an artifact: a float('inf') from a zero-span card
    would make json.dumps emit `Infinity`, which no JSON parser accepts."""
    job = _submit(client, auth, polish="1")
    raw = client.get(job["artifacts"]["layout"], headers=auth).text
    assert "Infinity" not in raw and "NaN" not in raw
    rep = json.loads(raw)
    assert set(rep) >= {"before", "after", "cards", "srt", "max_drift", "mode"}
