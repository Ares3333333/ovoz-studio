# -*- coding: utf-8 -*-
"""Round 21 — Ovoz Nafis: the right to cut an Uzbek sentence.

Each law is asserted twice: once that it fires on the defect it names, and once
that it stays silent where the same surface form is legitimate. A language engine
that only has positive tests is a machine that finds faults everywhere and is
believed nowhere.
"""
import json
import re
from pathlib import Path

import pytest

from app.ling import nafis
from app.ling.srt import Cue


def W(text):
    return nafis.words_of(text)


def codes(text, k):
    return {code for code, _w, _d in nafis.cut_faults(W(text), k)}


# ─── the laws ──────────────────────────────────────────────────────────────────

def test_a_postposition_never_opens_the_card_after_the_cut():
    words = W("rejani hozir uchun yozdik")
    assert words == ["rejani", "hozir", "uchun", "yozdik"]
    assert "postposition" in {c for c, _w, _d in nafis.cut_faults(words, 2)}


def test_every_unambiguous_postposition_is_blamed_where_it_starts_a_card():
    for pp in ("uchun", "bilan", "haqida", "gacha", "tufayli", "orqali"):
        words = W(f"jamoaga ish {pp} kelishdik")
        k = words.index(pp)
        assert "postposition" in codes(f"jamoaga ish {pp} kelishdik", k), (pp, words)


def test_the_ambiguous_adverb_reading_is_not_blamed():
    """`keyin` after a comma is "then", not "after". Blaming it would accuse the
    engine of a defect it does not have, and the measured number would lie."""
    assert codes("matnni tekshirib, keyin esa tezlikni o'lchaymiz", 3) == set()
    # the same word after a noun phrase IS the postposition reading
    assert "postposition" in codes("uch soat keyin uddalandik", 2)


def test_a_compound_verb_is_one_predicate():
    # each case names the exact cut that tears the predicate in two
    for text, k in (("jamoa tayyorlab berdi", 2),
                    ("uni ko'rib chiqamiz keyin", 2),
                    ("buni bo'lib bo'lmaydi", 2),
                    ("ishni davom ettira olaman", 3),
                    ("kitobni o'qib bo'ldim", 2),
                    ("kelgan edi kecha", 1)):
        assert "compound_verb" in codes(text, k), (text, k)


def test_the_converb_ending_alone_is_not_guilty():
    """`-ib` is a real converb, but a light verb has to be the word being stranded.
    `borib ketdi` is a compound; `borib maktabga kirdi` cuts after an unrelated
    noun, and no law applies there."""
    assert "compound_verb" not in codes("u borib maktabga kirdi", 2)
    assert "compound_verb" in codes("u borib ketdi", 2)


def test_a_conjunction_never_closes_a_card():
    for text, k in (("jamoamiz va ushbu hamkorlik", 2),
                    ("bu qiyin edi chunki muddat qisqa", 4),
                    ("uni sizga berdi lekin keyin qaytarib oldi", 4)):
        assert "bind_right" in codes(text, k), (text, k, W(text))


def test_a_fixed_junction_split_across_cards_is_its_own_fault():
    assert "pair" in codes("shuning uchun rejani oldindan yozdik", 1)
    assert "pair" in codes("garchi bo'lsa ham biz bordik", 1)


def test_a_numeral_is_not_cut_from_its_unit():
    assert "numeral_unit" in codes("yigirma kishidan iborat jamoa", 1)
    assert "numeral_unit" in codes("ikki oy ichida tayyor", 1)
    assert "numeral_unit" in codes("uch yilda tug'ilgan", 1) or \
        "postposition" in codes("uch yilda tug'ilgan", 1)
    # a numeral with no unit after it is not a fault
    assert "numeral_unit" not in codes("birinchi bo'lim nihoya", 1)
    assert "numeral_unit" not in codes("bu bir edi", 2)


# ─── the machinery the laws are trusted by ─────────────────────────────────────

def test_a_dead_stem_cannot_hide_behind_an_apostrophe():
    """The bug this gate exists for: `fold` maps U+02BB to ASCII ',` so a stem
    written as `bol` never matches `bo'ldi`. A silently dead law reports a clean
    timeline, which is worse than no law at all."""
    for stem in nafis.LIGHT_VERBS:
        assert nafis.fold(stem) == stem, f"light-verb stem not in folded form: {stem!r}"
    for stem in nafis.UNIT_STEMS + tuple(nafis.NUMERALS):
        assert nafis.fold(stem) == stem or stem == "tort", f"not folded: {stem!r}"
    for left, right in nafis.PAIRS:
        assert nafis.fold(left) in ("shuning", "shu", "garchi", "qarab", "nisbatan", "bir")
        assert nafis.fold(right) == right.lower().replace("\u02bb", "'"), (left, right)


def test_the_fold_covers_every_apostrophe_marker_in_use():
    """A missed codepoint is not cosmetic: the laws are keyed on folded text, so an
    unmapped apostrophe disables whichever rule mentions it — and the engine keeps
    reporting a clean timeline. Every entry of the table is walked, not sampled."""
    assert nafis.fold("O\u02bbZBEK") == nafis.fold("o'zbek") == nafis.fold("o\u2018zbek")
    assert nafis.fold("o\u2019z") == "o'z" and nafis.fold("o`z") == "o'z"
    for cp in nafis._APOS:
        assert nafis.fold("a%sb" % cp) == "a'b", hex(ord(cp))


def test_fold_strips_the_punctuation_a_card_leaves_on_words():
    assert nafis.fold("(bilan,)") == "bilan"


def test_the_engine_refuses_to_judge_a_cut_that_does_not_exist():
    words = W("ikki soat")
    assert nafis.cut_faults(words, 0) == []          # nothing is before the cut
    assert nafis.cut_faults(words, len(words)) == []  # nothing is after it
    assert nafis.cut_faults(W("bitta"), 1) == []      # one word has no cuts
    assert nafis.cut_faults([], 0) == []


def test_legal_and_illegal_positions_partition_every_cut():
    words = W("jamoaga ishlash uchun kelib berdi va biz ham bordik chunki kech")
    every = set(range(1, len(words)))
    legal = set(nafis.legal_positions(words))
    illegal = {f["k"] for f in nafis.illegal_cuts(words)}
    assert legal | illegal == every
    assert not (legal & illegal)
    assert illegal, "this corpus is built to contain at least one forbidden cut"


def test_penalty_is_zero_only_where_the_laws_are_silent():
    words = W("uni tayyorlab berdi")
    for k in range(1, len(words)):
        if nafis.cut_faults(words, k):
            assert nafis.penalty(words, k) == nafis.HARD_PENALTY * len(
                nafis.cut_faults(words, k))
        else:
            assert nafis.penalty(words, k) == 0.0


# ─── the report ────────────────────────────────────────────────────────────────

CUES = [
    Cue(1, 0.0, 3.0, "Bu loyiha Farg'ona vodiyida ikki oy ichida ishga\n"
                     "tushirildi uni esa yigirma"),
    Cue(2, 3.2, 6.0, "kishidan iborat jamoa o'z vaqtida tayyorlab berdi"),
    Cue(3, 6.2, 9.0, "Rejani hozirdanoq yozdik shuning"),
    Cue(4, 9.2, 11.0, "uchun kechikmadik"),
]


def test_the_report_is_json_finite_and_carries_no_prose():
    rep = nafis.analyze(CUES)
    again = json.loads(json.dumps(rep, ensure_ascii=False))
    assert again["engine"] == "ovoz-nafis"
    assert len(again["boundary_list"]) <= nafis.MAX_CUTS_REPORT
    assert all(isinstance(v, (int, float, list, dict, str, type(None)))
               for v in again.values())
    assert "clean_share" in again["boundaries"]


def test_the_report_measures_the_cut_between_two_cards_not_inside_them():
    """Cue 1 ends `yigirma`, cue 2 starts `kishidan`: the boundary is the defect, and
    only a boundary-aware engine can see it — line metrics say both cards fit."""
    rep = nafis.analyze(CUES)
    hits = [b for b in rep["boundary_list"] if b["between"] == 2]
    assert hits and hits[0]["hard"] and "numeral_unit" in hits[0]["codes"], hits
    assert hits[0]["left"] == "yigirma" and hits[0]["right"] == "kishidan"


def test_a_pair_torn_across_two_cards_is_reported_as_the_same_defect():
    """`shuning uchun` is one junction. Line metrics inside each card are fine;
    only the boundary view can see that the thought was cut in half."""
    rep = nafis.analyze(CUES)
    hits = [b for b in rep["boundary_list"] if b["between"] == 4]
    assert hits and "pair" in hits[0]["codes"], hits
    assert hits[0]["left"] == "shuning" and hits[0]["right"] == "uchun"


def test_the_engine_never_touches_the_text_it_is_given():
    before = [(c.index, c.start, c.end, c.text) for c in CUES]
    nafis.analyze(CUES)
    assert [(c.index, c.start, c.end, c.text) for c in CUES] == before


def test_tokens_survive_the_analysis():
    rep = nafis.analyze(CUES)
    flat = sum((nafis.words_of(c.text) for c in CUES), [])
    assert rep["words"] == len(flat)


def test_the_same_words_always_give_the_same_answer():
    first = nafis.analyze(CUES)
    for _ in range(3):
        assert nafis.analyze(CUES)["faults"] == first["faults"]


def test_a_cue_list_nobody_can_judge_is_refused_not_reported_as_clean():
    with pytest.raises(ValueError):
        nafis.analyze([])


def test_the_engine_names_its_own_laws():
    """The UI lists these; a law added without a name here ships unexplained."""
    assert set(nafis.analyze(CUES)["laws"]) == {
        "postposition", "compound_verb", "bind_right", "pair", "numeral_unit"}
    assert {l.__name__.split("_", 2)[2] for l in nafis.LAWS} == set(nafis.analyze(CUES)["laws"])


# ─── the law has teeth where the layout engine decides ─────────────────────────

# Six sentences of real Uzbek interview prose, timed ~19 chars/sec so Qator MUST
# split them: the boundary between the resulting cards is what a viewer cannot go
# back to, and it is the place a width-and-balance engine gets to choose badly.
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
    "Rahmat, juda foydali suhbat bo'ldi, va sizning har bir javobingiz uchun "
    "alomirda minndordorman.",
]


def prose_cues():
    cues, t = [], 0.0
    for i, text in enumerate(PROSE):
        dur = max(3.0, len(text) / 19.0)
        cues.append(Cue(i + 1, round(t, 2), round(t + dur, 2), text))
        t += dur + 0.12
    return cues


def test_qator_never_tears_a_word_group_across_two_cards():
    """The regression gate for the integration, measured on prose rather than on a
    minimal pair: Ovoz Qator chooses its own boundaries, and the answer must be that
    none of them is a hard fault.

    Honest about scope: the share of cuts that fall *inside* a clause (no punctuation
    at the edge) is unchanged — that is a taste axis Qator prices as a discount, not
    a law. What the law forbids is tearing a group of words in two, and it is 0/13
    here where it was 1/13 before the engine was wired in."""
    from app.ling import layout
    rep = layout.polish(prose_cues())
    cards = rep["cards"]
    assert len(cards) > len(PROSE), "the prose must be tight enough to force real splits"
    assert rep["words_preserved"] is True
    hard = []
    for a, b in zip(cards, cards[1:]):
        lw, rw = nafis.words_of(a["text"]), nafis.words_of(b["text"])
        faults = nafis.cut_faults(lw + rw, len(lw))
        if faults:
            hard.append((lw[-1], rw[0], [f[0] for f in faults]))
    assert not hard, f"forbidden card boundaries shipped: {hard}"


def test_the_layout_still_obeys_its_own_house_norms():
    """The grammar law may not buy readability by breaking the measure.

    What Qator promises and what it does not: in sync mode a card may legitimately
    stay above `MAX_CPS` (this corpus is built at 19 chars/sec on purpose, and
    stretching the dwell further would desync it from the tape), so the assertion
    is the one that matters — no MAJOR finding appears because of the law, and the
    peak reading speed never gets worse than the engine's own starting measurement.
    Asserting "every card ≤ 17" would be asserting a promise the engine never made.
    """
    from app.ling import layout
    cues = prose_cues()
    rep = layout.polish(cues)
    majors_before = [f for f in rep["before"]["findings"] if f["severity"] == "major"]
    majors = [f for f in rep["after"]["findings"] if f["severity"] == "major"]
    assert not majors, f"grammar law broke the house measure: {majors}"
    assert rep["after"]["peak_cps"] <= rep["before"]["peak_cps"] + 1e-9, \
        (rep["before"]["peak_cps"], rep["after"]["peak_cps"])
    assert rep["after"]["score"] >= rep["before"]["score"], \
        (rep["before"]["score"], rep["after"]["score"], majors_before)


def test_the_law_is_consulted_where_a_boundary_is_chosen():
    """A gate, not a joke: every boundary decision in Qator funnels through
    `_break_cost`, so the law lives in one place and cannot be forgotten by the
    next splitter someone adds."""
    src = (Path(layout_path()) / "layout.py").read_text(encoding="utf-8")
    assert "nafis.penalty(words, k)" in src
    for fn in ("def rewrap(", "def _split_card(", "def _soften_break("):
        body = src[src.index(fn):]
        body = body[:body.index("\ndef ")]
        assert "_break_cost(" in body, f"{fn} picks a break without the oracle"


def layout_path():
    from app.ling import layout
    return Path(layout.__file__).resolve().parent


# ─── the public endpoint ───────────────────────────────────────────────────────

def _nafis(client, **payload):
    return client.post("/api/v1/ling/nafis", json=payload)


def test_the_endpoint_judges_the_boundaries_a_client_will_ship(client):
    rep = _nafis(client, lines=[{"start": i * 3.0, "end": i * 3.0 + 2.9,
                                 "text": t} for i, t in enumerate(PROSE)])
    assert rep.status_code == 200, rep.text
    j = rep.json()
    assert j["engine"] == "ovoz-nafis" and j["mode"] == "cues"
    assert j["boundaries"]["measured"] >= len(PROSE) - 1
    assert j["forbidden"] > 0 and j["ruler"], "a law with nothing to say is not deployed"
    assert all({"k", "left", "right", "codes"} <= set(r) for r in j["ruler"])
    assert j["score"] is not None and j["legal_share"] is not None


def test_the_endpoint_judges_a_paragraph_without_inventing_timings(client):
    """An editor asks 'may I cut here' before any file exists. The answer must come
    without fake timestamps, and must not carry a boundary score it never measured.
    """
    rep = _nafis(client, text="Jamoaga ishlash uchun kelib berdi va biz ham bordik")
    assert rep.status_code == 200, rep.text
    j = rep.json()
    assert j["mode"] == "prose" and j["boundaries"]["measured"] == 0
    assert j["score"] is None, "a 100 for measuring nothing is the lie this engine forbids"
    assert 0.0 < j["legal_share"] < 1.0, j["legal_share"]
    assert {c for r in j["ruler"] for c in r["codes"]} & {"postposition", "bind_right",
                                                          "compound_verb", "pair"}


def test_the_particle_that_closes_a_phrase_is_not_accused_of_opening_one():
    """`siz ham` = "you too": `ham` binds LEFT, so a cut after it is a normal clause
    edge. The live ruler marked it as a torn conjunction; guessing on an ambiguous
    particle costs the client trust in a file that was already correct."""
    assert codes("Agar siz ham o'zbek tilida subtitr qilas", 3) == set()   # after `ham`
    # ... while the conjunctions that really do promise the next line still fire
    assert "bind_right" in codes("jamoamiz va ushbu hamkorlik", 2)
    assert "ham" not in nafis.BIND_RIGHT and "ham" not in nafis.POSTPOSITIONS


def test_a_cut_that_breaks_two_laws_is_still_one_cut():
    """`shuning || uchun` is a broken junction AND a stranded postposition. The
    position counts once, the laws count twice — collapse those two counters into
    one field and the report adds up to more cuts than the sentence contains.

    A live check, not a theory: this is exactly what the first deployed ruler did.
    """
    text = "rejeni yozdik shuning uchun kechiktik"
    words = W(text)
    k = words.index("uchun")
    codes = {c for c, _w, _d in nafis.cut_faults(words, k)}
    assert {"pair", "postposition"} <= codes, codes
    rep = nafis.analyze([], source_text=text)
    assert rep["legal_positions"] + rep["forbidden"] == rep["cut_positions"], rep
    assert rep["fault_count"] > rep["forbidden"], (rep["fault_count"], rep["forbidden"])
    assert sum(rep["by_code"].values()) == rep["fault_count"]
    row = [r for r in rep["ruler"] if r["k"] == k]
    assert row and row[0]["codes"] == sorted(codes), rep["ruler"]


def test_the_endpoint_counts_delivered_boundaries_once(client):
    """`boundaries.measured` is the number of seams in the delivered file, and
    `hard` is a subset of it — the widget prints them as `clean/total`, so the
    arithmetic has to hold even when a seam breaks two laws."""
    rep = _nafis(client, text="Rejani yozdik shuning uchun kechiktik, jamoa tayyorlab berdi")
    j = rep.json()
    assert j["boundaries"]["measured"] == 0 and j["score"] is None   # prose: no seams
    assert j["legal_positions"] + j["forbidden"] == j["cut_positions"]


def test_a_request_with_nothing_to_judge_is_refused_not_flattered(client):
    assert _nafis(client).status_code == 422
    blank = _nafis(client, text="   \n  ")
    assert blank.status_code == 422, blank.text


def test_the_endpoint_never_trims_the_text_it_is_asked_about(client):
    long_cue = " ".join(["so'z"] * 70)          # > _LING_MAX_CUE_CHARS
    rep = _nafis(client, lines=[{"start": 0.0, "end": 2.0, "text": long_cue}])
    assert rep.status_code == 422, rep.text[:120]
    assert "never cuts" in rep.json()["detail"], rep.json()


def test_an_oversized_paragraph_is_refused_by_name_of_the_cap(client):
    rep = _nafis(client, text="a " * 4000)
    assert rep.status_code == 413, rep.text[:120]
    assert "5000" in rep.json()["detail"]


def test_every_language_engine_runs_off_the_event_loop():
    """Round 23's review caught the half-fix: the audio routes were moved to a
    threadpool with slots, while `diarize`, `layout` and `nafis` kept computing in
    the event loop — so the cheaper class, at 60/min, was the one that could freeze
    `/healthz` for everyone. Named per route, because a list someone must remember
    is the reason the bug existed."""
    src = (Path(nafis.__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    routes = {"ling_diarize": "diar_mod.diarize", "ling_layout": "lay_mod.polish",
              "ling_nafis": "nafis_mod.analyze", "ling_align": "align_mod.align",
              "ling_words": "word_mod.words"}
    for name, call in routes.items():
        at = src.index("async def %s(" % name)
        body = " ".join(src[at:src.index("\n@app.", at)].split())
        assert call in body, "%s no longer calls %s" % (name, call)
        # every call site is wrapped: the offload always appears immediately before
        # the engine name inside the same expression
        assert re.search(r"(run_in_threadpool|_ling_run)\([^)]*?" + re.escape(call), body), \
            f"{name} computes on the event loop: {call}"
    assert "_LING_TEXT = threading.BoundedSemaphore(3)" in src
    assert src.count("_ling_run(_LING_TEXT") >= 4          # diarize, layout, nafis ×2 modes


def test_text_engine_slots_refuse_instead_of_queuing(client):
    """The fourth request must not sit behind three in a threadpool it then steals
    from another route: 429 with a wait, which is also what a browser can obey."""
    from app import main as m
    held = [m._LING_TEXT.acquire(blocking=False) for _ in range(3)]
    assert all(held), "needs all three text slots free"
    try:
        r = _nafis(client, text="jamoaga ishlash uchun kelib berdi")
        assert r.status_code == 429, r.text
        assert r.headers.get("retry-after") == "5", r.headers
        assert "busy" in r.json()["detail"]
    finally:
        for _ in held:
            m._LING_TEXT.release()
    assert _nafis(client, text="jamoaga ishlash uchun kelib berdi").status_code == 200


def test_a_refusal_also_costs_a_rate_token(client, monkeypatch):
    """The budget used to be spent only on requests the server agreed to serve, so
    the free path was the one that still cost a drain and a parse.

    Asserted as a property, not as sixty real requests: the window is 60 s, and a
    test that stamps sixty of them measures how fast this machine drains 130 KB
    bodies rather than what the middleware orders.
    """
    import time

    from app import main as m
    from app.main import LING_MAX_BODY_BYTES
    # The limiter is off for the whole suite (one shared 'testclient' identity) —
    # this test is about the limiter, so it opts back in, the same way
    # test_round12.py does.
    monkeypatch.setenv("OVOZ_ENDPOINT_RATE_LIMIT", "1")
    oversize = {"lines": "x" * (LING_MAX_BODY_BYTES + 100)}
    m._ENDPOINT_BUCKETS.clear()
    for _ in range(5):
        assert client.post("/api/v1/ling/diarize", json=oversize).status_code == 413
    key = "testclient|/api/v1/ling"
    assert len(m._ENDPOINT_BUCKETS.get(key, [])) >= 5, \
        "refusals that spend no budget are the free path"
    # and with the budget spent, the refusal answer is 429 BEFORE the body is drained
    monkeypatch.setitem(m._ENDPOINT_BUCKETS, key, [time.monotonic()] * 60)
    r = client.post("/api/v1/ling/diarize", json=oversize)
    assert r.status_code == 429, r.text
    assert r.headers.get("x-ratelimit-limit") == "60", r.headers


def test_the_reply_is_json_clean_and_the_ruler_is_bounded(client):
    rep = _nafis(client, text=("Jamoaga ishlash uchun kelib berdi va biz ham bordik "
                               "chunki rejani yigirma kishidan iborat jamoa qildi " * 30))
    assert rep.status_code == 200, rep.text[:120]
    body = rep.text
    for banned in ("Infinity", "NaN"):
        assert banned not in body, banned
    j = rep.json()
    assert len(j["ruler"]) <= nafis.MAX_CUTS_REPORT
    assert j["ruler_truncated"] == (j["forbidden"] > nafis.MAX_CUTS_REPORT)
    # counts describe the whole text even though the list stops early, and they
    # count the same kind of thing: positions, not law hits
    assert j["legal_positions"] + j["forbidden"] == j["cut_positions"]
    assert j["fault_count"] >= j["forbidden"]


def test_two_identical_requests_return_byte_identical_answers(client):
    scene = {"lines": [{"start": 0.0, "end": 2.0, "text": "Uzun javob " + "so'z " * 30},
                       {"start": 2.2, "end": 4.0, "text": "shuning uchun kechiktik"}]}
    a, b = _nafis(client, **scene), _nafis(client, **scene)
    assert a.status_code == b.status_code == 200
    assert a.text == b.text


def test_the_engine_is_advertised_where_sdks_look(client):
    feats = client.get("/api/v1/info").json()["features"]
    assert "subtitle_cut_law" in feats, feats


def test_the_route_shares_the_language_kill_switch(client, monkeypatch):
    from app import main as m
    monkeypatch.setattr(m, "_flags_all",
                        lambda: {"uzbek_language_engine": {"enabled": False}})
    assert _nafis(client, text="salom dunyo").status_code == 404


def test_the_route_is_a_text_engine_for_rate_limits(client):
    """Not the listening class (12/min): this engine never reads a tape, so it must
    not be priced like one that does."""
    from app.main import _endpoint_limit
    assert _endpoint_limit("/api/v1/ling/nafis") == 60


# ─── the source gates this project trusts for its invariants ───────────────────

def test_the_laws_never_edit_text():
    """A language engine that rewrites tokens is a different product. The laws may
    only read the list they are handed."""
    src = Path(nafis.__file__).read_text(encoding="utf-8")
    body = src[src.index("def _law_postposition"):src.index("def cut_faults")]
    assert "words[k] =" not in src and "words[k - 1] =" not in src
    assert not re.search(r"words\.(append|insert|pop|sort|reverse|extend)\(", body), \
        "a law is mutating the word list it was handed"
