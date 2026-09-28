"""Round 18 — Ovoz So'z: word-level timing inside a cue.

The claim being sold is one sentence: *each word lights up when the tape says it
was said*. A claim about frames is only testable against frames we made ourselves,
so every case here states the truth it will be judged against — where the syllables
are, where the speaker stopped, and where nothing stops at all.

Five properties are checked on every case:

  1. the tokens come back exactly as they went in (this engine has no authority
     over text, and a dropped apostrophe in `oʻ` is a changed word);
  2. the cue is partitioned: the first word starts where the cue starts, the last
     ends where the cue ends, and cuts strictly increase — so a highlight can
     never stall, overlap, or run past its line;
  3. a cut called `valley` really sits in measured silence;
  4. a cue that cannot be measured is refused with a reason, and the rest of the
     file still gets its timings;
  5. the same tape and the same cues give a byte-identical report twice.

Audio comes from the studio the aligner shares (tests/audio_studio.py): no numpy,
no files, no network.
"""
import json
import math
import re
from pathlib import Path

import pytest

from app.ling import align as A
from app.ling import word as W
from app.ling.srt import Cue, format_srt

from audio_studio import (BLOCK_SEC, FRAME_S, RATE, block_cues, long_tape, nonstop,
                          room, tape, voice, wav_bytes)


# ─── the tapes, stated as facts ───────────────────────────────────────────────

def phrase() -> list:
    """3.89 s, stated exactly: speech 0–0.60, pause to 0.75, speech to 1.25, pause to
    1.37, speech to 2.07, pause to 2.67, speech to 3.27, pause to 3.39, speech to
    3.89. Two lines of three and two words, with the breaks already in the tape."""
    return [voice(0.6), room(0.15), voice(0.5), room(0.12), voice(0.7),
            room(0.6), voice(0.6), room(0.12), voice(0.5)]


PHRASE = tape(*phrase())

# The breaks a human editor would cut at, in seconds, per cue.
BREAKS = {0: [(0.58, 0.77), (1.23, 1.41)], 1: [(3.25, 3.41)]}

ONE_LINE = [Cue(1, 0.0, 2.07, "Bir ikkinchi uchinchi"),
            Cue(2, 2.07, 3.89, "To'rtinchi beshinchi")]


def timed(pcm, cues):
    return W.words(pcm, RATE, cues)


def long_cues(seconds: float) -> list:
    """Two words per block, one valley between them: every cue the tape owns gets
    exactly one measured cut, so `cut_valley == number of cues` is a checkable
    claim at minute fifteen as well as at second two."""
    return [Cue(c.index, c.start, c.end, "bir ikki") for c in block_cues(seconds)]


def silent_frames(pcm):
    """Frame indices that are not speech, by the same gate both listeners share."""
    curve = A.frame_curve(pcm, RATE)
    out, prev = set(), 0
    for s, e in A.speech_runs(curve):
        out.update(range(prev, s))
        prev = e + 1
    out.update(range(prev, len(curve["levels"])))
    return out, curve


def cut_frames(rep, cue_pos):
    """The internal boundaries of one cue, as (frame index, method)."""
    cue = rep["cues"][cue_pos]
    return [(int(round(it["e"] / FRAME_S)), m)
            for it, m in zip(cue["words"][:-1], cue["methods"])]


# ─── the syllable ruler ────────────────────────────────────────────────────────

@pytest.mark.parametrize("w,expect", [
    ("bog'ladim", 3),      # o-a-i, and the apostrophe belongs to the o
    ("stol", 1),
    ("ish", 1),
    ("o'qish", 2),
    ("keladigan", 4),
    ("OVOZ", 2),           # uppercase latin counts: a shout is still two syllables
    ("бўғла", 1),          # cyrillic ў and ғ are consonants
    ("қиёма", 3),          # cyrillic Uzbek vowels
    ("100", 1),            # a numeral still occupies the mouth
    ("—", 1),              # a bare dash still occupies the screen
])
def test_a_word_is_measured_by_the_air_it_needs(w, expect):
    got = W.weights([w])[0]
    assert math.isfinite(got) and got >= expect, (w, got)
    assert got < expect + 1.0, (w, got)     # the length term must stay small


def test_the_modifier_letter_is_not_a_second_vowel():
    """`oʻ` is one letter of the alphabet, not a vowel plus punctuation. Counting
    it twice would give every `oʻ`-word a longer share than it is spoken for."""
    plain = W.weights(["tor"])[0]
    marked = W.weights(["toʻr"])[0]
    assert marked - plain == pytest.approx(0.04), (plain, marked)


def test_an_iotized_vowel_opens_a_syllable_of_its_own():
    """After another vowel, ё/ю/я are two sounds (glide + nucleus), not one sustained
    syllable. Folding them into the preceding cluster makes the engine time
    "киёма" as three beats instead of four — and a highlight that runs fast on
    borrowed words is exactly the drift a forced aligner is bought to avoid."""
    plain = W.weights(["киома"])[0]
    iotized = W.weights(["киёма"])[0]
    assert iotized - plain >= 1.0, (plain, iotized)
    assert W.weights(["сюжет"])[0] >= 2.0


# ─── the partition, on every shape of tape ─────────────────────────────────────

def test_the_words_are_the_words_that_went_in():
    rep = timed(PHRASE, ONE_LINE)
    for cue, out in zip(ONE_LINE, rep["cues"]):
        assert " ".join(it["w"] for it in out["words"]) == cue.text.strip()


def test_a_cue_is_partitioned_not_approximated():
    rep = timed(PHRASE, ONE_LINE)
    for cue, out in zip(ONE_LINE, rep["cues"]):
        items = out["words"]
        assert items, out
        assert items[0]["s"] == pytest.approx(cue.start, abs=0.001), items[0]
        assert items[-1]["e"] == pytest.approx(cue.end, abs=0.001), items[-1]
        for a, b in zip(items, items[1:]):
            assert b["s"] == a["e"], (a, b)          # contiguous: no stall, no gap
            assert b["e"] > b["s"], b                # never zero-length
            assert b["s"] >= a["e"], (a, b)          # never overlaps


def test_every_word_has_room_to_be_read():
    rep = timed(PHRASE, ONE_LINE)
    for out in rep["cues"]:
        for it in out["words"]:
            assert it["e"] - it["s"] >= W.MIN_WORD_SEC - 0.001, it


def test_the_cue_order_and_indices_survive_the_pass():
    cues = [Cue(7, 2.97, 4.47, "Ikkinchi"), Cue(3, 0.0, 1.97, "Birinchi")]
    rep = timed(PHRASE, cues)
    assert [c["i"] for c in rep["cues"]] == [7, 3]


def test_two_listeners_never_disagree_about_the_same_tape():
    """A cut So'z calls silence must be silence for Jimlik too — the gate is shared,
    not reimplemented, or the two reports describe two different recordings. Only
    the `speech` method is allowed inside a run, and it says so."""
    rep = timed(PHRASE, ONE_LINE)
    quiet, curve = silent_frames(PHRASE)
    assert rep["audio"]["frame_ms"] == pytest.approx(curve["frame_s"] * 1000.0)
    for pos in range(len(ONE_LINE)):
        for f, how in cut_frames(rep, pos):
            if how == "speech":
                continue
            assert f in quiet, (pos, f, how)


def test_the_breaks_in_the_tape_are_the_breaks_in_the_words():
    """The headline case, checked against how the tape was built: every word boundary
    of the first line must land in one of the two pauses that are really there, not
    where the letter count alone would have put it."""
    rep = timed(PHRASE, ONE_LINE)
    for pos, spans in BREAKS.items():
        cuts = cut_frames(rep, pos)
        assert len(cuts) == len(spans), (pos, cuts, rep["cues"][pos]["words"])
        for (f, how), (lo, hi) in zip(cuts, spans):
            assert how == "valley", (pos, f, how)
            assert lo <= f * FRAME_S <= hi, (pos, f * FRAME_S, (lo, hi))
    assert rep["summary"]["cut_valley"] == 3, rep["summary"]
    assert rep["summary"]["valley_share"] == 1.0, rep["summary"]


def test_a_wall_of_sound_is_still_timed_and_says_it_guessed():
    """No pauses at all: the engine still has to deliver a partition (a karaoke file
    with missing lines is worse than an approximate one), but it must not present
    geometric cuts as measured ones. `valley_share: 0` is the honesty check."""
    pcm = nonstop()
    cues = [Cue(1, 0.0, 4.5, "Bir ikki uch to'rt besh olti")]
    rep = timed(pcm, cues)
    items = rep["cues"][0]["words"]
    assert len(items) == 6
    assert rep["summary"]["cut_valley"] == 0, rep["summary"]
    assert rep["summary"]["valley_share"] == 0.0, rep["summary"]
    assert rep["summary"]["cut_speech"] == 5, rep["summary"]
    assert rep["cues"][0]["methods"] == ["speech"] * 5
    for a, b in zip(items, items[1:]):
        assert b["s"] == a["e"]


def test_a_room_tone_tape_is_measured_and_a_dead_tape_is_refused():
    """Digital silence everywhere is not quiet speech, it is no speech: the engine
    has nothing to place, and saying so beats emitting a uniform staircase."""
    with pytest.raises(W.WordError) as err:
        W.words(tape(room(5.0)), RATE, ONE_LINE)
    assert err.value.code == "silent_tape"


# ─── what gets refused, per cue ────────────────────────────────────────────────

def test_a_line_past_the_end_of_the_tape_is_refused_not_guessed():
    """Half a cue can hang off the recording (the client's timings came from an ASR
    that ran past the file). The frames simply are not there, and a word with no
    frames does not get a timing invented for it."""
    cues = [Cue(1, 0.0, 2.07, "Birinchi"), Cue(2, 3.85, 4.20, "Ikki so'z")]
    rep = timed(PHRASE, cues)
    assert rep["cues"][0]["words"], rep["cues"][0]
    assert rep["cues"][1]["reason"] == "too_short", rep["cues"][1]
    assert rep["cues"][1]["frames"] < rep["cues"][1]["needs"]
    assert rep["cues"][1]["words"] == []
    assert rep["summary"]["cues_measured"] == 1
    assert rep["summary"]["cues_refused"] == 1


def test_a_cue_outside_the_recording_says_so():
    cues = [Cue(1, 9.0, 11.0, "Hech qachon eshitilmagan")]
    rep = timed(PHRASE, cues)
    assert rep["cues"][0]["reason"] == "outside_audio"


def test_six_words_in_a_tenth_of_a_second_are_refused_with_their_rate():
    cues = [Cue(1, 0.0, 0.1, "bir ikki uch to'rt besh olti")]
    rep = timed(PHRASE, cues)
    assert rep["cues"][0]["reason"] == "too_fast", rep["cues"][0]
    assert rep["cues"][0]["cps_needed"] > 17.0     # layout's own readability ceiling
    assert rep["cues"][0]["words"] == []


def test_blank_lines_carry_no_words_but_do_not_break_the_file():
    cues = [Cue(1, 0.0, 1.97, "Birinchi"), Cue(2, 2.97, 4.47, "   \n  \t ")]
    rep = timed(PHRASE, cues)
    assert rep["cues"][0]["words"]
    assert rep["cues"][1]["reason"] == "no_text"
    assert rep["summary"]["words"] == 1


def test_one_word_needs_no_cut_at_all():
    rep = timed(PHRASE, [Cue(1, 0.0, 1.97, "Salom")])
    assert rep["summary"]["cuts"] == 0
    assert rep["cues"][0]["words"][0] == {"w": "Salom", "s": 0.0, "e": 1.97}


# ─── the refusals that belong to the whole request ─────────────────────────────

def test_no_cues_is_refused_rather_than_answered_with_empty():
    with pytest.raises(W.WordError) as err:
        W.words(PHRASE, RATE, [])
    assert err.value.code == "no_cues"


def test_the_word_budget_is_spent_before_a_single_frame_is_read():
    """Order of guards is a security property: the CPU cost of this engine is per
    cut, so an over-long transcript must be refused before the envelope pass."""
    text = " ".join("so'z" for _ in range(W.MAX_WORDS + 2))
    with pytest.raises(W.WordError) as err:
        W.words(tape(room(0.2)), RATE, [Cue(1, 0.0, 0.2, text)])   # dead, tiny tape
    assert err.value.code == "too_many_words"


def test_an_absurd_sample_rate_is_refused_by_number_not_by_crash():
    with pytest.raises(W.WordError) as err:
        W.words(PHRASE, 220_500, ONE_LINE)
    assert err.value.code == "bad_rate"


def test_a_tape_shorter_than_one_frame_is_refused():
    with pytest.raises(W.WordError) as err:
        W.words(tape(room(0.005)), RATE, ONE_LINE)
    assert err.value.code == "too_short"


def test_the_cue_budget_is_refused_too():
    cues = [Cue(i, 0.0, 0.1, "bir") for i in range(A.MAX_CUES + 1)]
    with pytest.raises(W.WordError) as err:
        W.words(PHRASE, RATE, cues)
    assert err.value.code == "too_many_cues"


# ─── the listening window (Round 19) ──────────────────────────────────────────

def test_fifteen_minutes_of_tape_still_gets_its_words_measured():
    """Same hole as the aligner's, same proof: the window used to count samples,
    so any job past 200 s came back `words: skipped: too_long` and the paid
    checkbox quietly bought nothing.

    Two words per block, one pause between them: the answer is forced by the tape,
    and `valley_share 1.0` at minute fourteen means what it says."""
    pcm = long_tape(900)
    assert len(pcm) > 1_600_000, "the old ceiling: this tape was refused outright"
    rep = W.words(pcm, RATE, long_cues(900))
    s = rep["summary"]
    assert s["cues"] == 150 == s["cues_measured"] and s["cues_refused"] == 0
    assert s["words"] == 300 and s["cuts"] == 150
    assert s["cut_valley"] == 150 and s["cut_speech"] == 0
    assert s["valley_share"] == 1.0
    assert rep["audio"]["window_sec"] == A.MAX_LISTEN_SEC
    first, last = rep["cues"][0], rep["cues"][-1]
    assert first["methods"] == last["methods"] == ["valley"]
    # The pause of block n sits at n*6 + 2.0 … 2.6, and the cut must land in it.
    for cue, base in ((first, 0.0), (last, 149 * BLOCK_SEC)):
        cut = cue["words"][0]["e"]
        assert base + 2.0 <= cut <= base + 2.6, (base, cut)
        assert cue["words"][1]["s"] == cut


def test_the_window_refusal_arrives_as_the_word_engine_own_error():
    """The ceiling is shared with Jimlik; the exception type is not. A caller that
    catches only WordError must still be able to catch this, or one law would leak
    two different classes depending on which ceiling happened to bind."""
    with pytest.raises(W.WordError) as exc:
        W.words(long_tape(906), RATE, long_cues(906))
    assert exc.value.code == "too_long"
    assert isinstance(exc.value, A.AlignError), "the family refused a subclass"


# ─── the report as a wire format ───────────────────────────────────────────────

def test_the_same_tape_answers_byte_identically():
    a = json.dumps(timed(PHRASE, ONE_LINE), sort_keys=True, ensure_ascii=False)
    b = json.dumps(timed(PHRASE, ONE_LINE), sort_keys=True, ensure_ascii=False)
    assert a == b


def test_every_number_in_the_report_is_finite():
    """`response.json()` on the client is strict about `Infinity`, and a cue of
    duration 0 divided into three words is exactly how a float gets there."""
    raw = json.dumps(timed(PHRASE, ONE_LINE + [Cue(3, 1.0, 1.0, "a b c")]),
                     allow_nan=False)
    assert "Infinity" not in raw and "NaN" not in raw


def test_a_zero_length_cue_cannot_produce_a_negative_word():
    rep = timed(PHRASE, ONE_LINE + [Cue(3, 1.0, 1.0, "a b c")])
    last = rep["cues"][-1]
    assert last["words"] == [] or all(it["e"] >= it["s"] for it in last["words"]), last


# ─── the exports a player consumes ─────────────────────────────────────────────

def test_the_vtt_carries_one_timestamp_per_word():
    rep = timed(PHRASE, ONE_LINE)
    vtt = W.to_vtt(rep)
    assert vtt.startswith("WEBVTT\n")
    stamps = re.findall(r"<(\d{2}):(\d{2}):(\d{2})\.(\d{3})>", vtt)
    assert len(stamps) == rep["summary"]["words"], (len(stamps), rep["summary"])
    assert vtt.count(" --> ") == rep["summary"]["cues_measured"]
    # and the timings round-trip: the file says what the report said
    assert stamps[0] == ("00", "00", "00", "000")


def test_the_ass_file_is_karaoke_not_a_plain_dump():
    rep = timed(PHRASE, ONE_LINE)
    ass = W.to_ass(rep)
    assert ass.startswith("[Script Info]")
    k = re.findall(r"\{\\kf(\d+)\}(\S+)", ass)
    assert len(k) == rep["summary"]["words"]
    assert all(int(cs) >= 1 for cs, _ in k), k[:4]
    assert "[Events]" in ass
    assert ass.count("Dialogue:") == rep["summary"]["cues_measured"]


def test_a_text_brace_cannot_escape_into_the_ass_stream():
    """A transcript is untrusted input to a format that reads braces as commands:
    an editor that swallows `{\an8}` from a customer's text is a broken editor."""
    rep = timed(PHRASE, [Cue(1, 0.0, 1.97, "{\\an8}bir {uchinchi}")])
    ass = W.to_ass(rep)
    assert re.findall(r"\{\\(?!kf)\w", ass) == [], ass
    assert ass.count("\\kf") == 2


def test_the_refused_cues_are_absent_from_the_exports_not_silently_wrong():
    rep = timed(PHRASE, [Cue(1, 0.0, 1.97, "Birinchi"), Cue(2, 9.0, 11.0, "Yo'q")])
    assert "Yo'q" not in W.to_vtt(rep)
    assert "Yo'q" not in W.to_ass(rep)


# ─── the public endpoint ───────────────────────────────────────────────────────
# The transport contract is the aligner's, on purpose: same envelope, same
# ceilings, same refusal table, same drain. Two listening endpoints that disagree
# about cost or about error codes are two engines a client cannot trust.

LINES = [{"start": c.start, "end": c.end, "text": c.text} for c in ONE_LINE]
STATIC = Path(__file__).resolve().parents[1] / "static"


def _words(client, audio, lines=LINES, srt=None, fmt=None):
    data = {}
    if lines is not None:
        data["lines"] = json.dumps(lines)
    if srt is not None:
        data["srt"] = srt
    if fmt is not None:
        data["fmt"] = fmt
    return client.post("/api/v1/ling/words",
                       files={"audio": ("take.wav", audio, "audio/wav")},
                       data=data)


def test_the_endpoint_answers_with_what_the_engine_says(client):
    """The public route may not be a softer copy of the engine: the demo and the
    paid job have to be the same arithmetic on the same bytes."""
    audio = wav_bytes(PHRASE)
    got = _words(client, audio)
    assert got.status_code == 200, got.text
    body = got.json()
    direct = W.words(*A.pcm_from_wav(audio), ONE_LINE)
    assert body["engine"] == "ovoz-soz"
    assert body["cues"] == direct["cues"]
    assert body["summary"] == direct["summary"]
    assert body["summary"]["words"] == 5


def test_an_srt_document_is_timed_exactly_like_a_line_array(client):
    audio = wav_bytes(PHRASE)
    by_doc = _words(client, audio, lines=None, srt=format_srt(ONE_LINE))
    assert by_doc.status_code == 200, by_doc.text
    assert by_doc.json()["cues"] == _words(client, audio).json()["cues"]


def test_a_tape_bigger_than_the_json_budget_still_gets_an_answer(client):
    """Path-conditional ceilings: the text endpoints live on 130 KB because they
    carry text; this one carries a recording. Same bytes, two verdicts."""
    from app.main import LING_MAX_BODY_BYTES
    long_tape = wav_bytes(tape(*(phrase() + phrase())))   # ~7.8 s ≈ 124 KB of body
    assert len(long_tape) * 2 > LING_MAX_BODY_BYTES       # the multipart envelope
    ok = _words(client, long_tape, lines=[{"start": 0.0, "end": 2.07,
                                           "text": "Bir ikkinchi uchinchi"}])
    assert ok.status_code == 200, ok.text
    assert ok.json()["audio"]["duration"] > 7.0
    same_size = client.post("/api/v1/ling/analyze",
                             json={"text": "a" * (len(long_tape) * 2 + 10)})
    assert same_size.status_code == 413, same_size.status_code


def test_the_export_is_offered_when_asked_and_the_wrong_one_refused(client):
    audio = wav_bytes(PHRASE)
    ass = _words(client, audio, fmt="ass")
    assert ass.status_code == 200, ass.text
    assert "\\kf" in ass.json()["ass"]
    vtt = _words(client, audio, fmt="vtt")
    assert vtt.json()["vtt"].startswith("WEBVTT")
    # The default answer must not carry a megabyte of text the caller did not ask
    # for: the report is the product, the export is an extra.
    plain = _words(client, audio).json()
    assert "ass" not in plain and "vtt" not in plain
    bad = _words(client, audio, fmt="srt")
    assert bad.status_code == 422, bad.text


def test_every_code_the_word_engine_can_raise_has_a_mapped_status():
    """An unmapped code answers 500 on purpose — but then the engine would be
    inventing codes nobody reviewed. The codes are read out of the syntax tree, so
    a new refusal cannot ship without its verdict, and no unrelated string in the
    file can be mistaken for one."""
    import ast
    from app.main import _ALIGN_REFUSAL_STATUS
    src = (Path(__file__).resolve().parents[1] / "app" / "ling" /
           "word.py").read_text(encoding="utf-8")
    codes = set()
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") in ("WordError", "AlignError")
                and len(node.args) == 2
                and isinstance(node.args[1], ast.Constant)):
            codes.add(str(node.args[1].value))
    assert codes, "no refusals found: this gate would pass on nothing"
    missing = sorted(codes - set(_ALIGN_REFUSAL_STATUS))
    assert not missing, f"WordError codes with no status mapping: {missing}"


@pytest.mark.parametrize("lines,code,needle", [
    ([{"start": 900.0, "end": 2.0, "text": "Bir ikkinchi"}], 422, "impossible"),
    ([{"start": 0.0, "end": 0.05, "text": "Bir ikkinchi uchinchi"}], 200, "refused"),
])
def test_a_hostile_request_is_answered_with_a_verdict(client, lines, code, needle):
    """Impossible timings are refused by the shared cue validator before a single
    frame is read; a cue that cannot be measured is refused per-cue, with the
    reason inside the report rather than in the HTTP status."""
    got = _words(client, wav_bytes(PHRASE), lines=lines)
    assert got.status_code == code, got.text
    body = got.json()
    if code == 200:
        assert body["cues"][0]["words"] == []
        assert body["cues"][0]["reason"], body["cues"][0]
    else:
        assert needle in body["detail"], body
        assert body.get("error_code"), body


def test_a_container_we_will_not_decode_is_refused_by_media_type(client):
    got = _words(client, b"not a wav at all, just noise bytes" * 4)
    assert got.status_code == 415, got.text
    assert got.json().get("error_code"), got.text


def test_a_tape_with_no_speech_is_refused_rather_than_guessed(client):
    """Geometry alone can place five words on five equal slices — and a karaoke
    highlight that drifts a syllable per line is worse than no highlight."""
    got = _words(client, wav_bytes(tape(room(3.0))))
    assert got.status_code == 422, got.text
    assert "no speech" in got.json()["detail"], got.text


def test_the_word_budget_answers_with_a_verdict_not_a_timeout(client):
    """A cue is capped at 300 characters by the shared validator, so the word
    budget is reached across cues — and it must still answer 422. Every cue is
    refused before a frame is read, because it is the number of cuts, not the
    length of the tape, that bounds what an anonymous call costs."""
    from app.ling.word import MAX_WORDS
    crowd = [{"start": 0.0, "end": 0.3, "text": "ab " * 100}
             for _ in range(1 + MAX_WORDS // 100)]
    got = _words(client, wav_bytes(PHRASE), lines=crowd)
    assert got.status_code == 422, got.text
    assert "too many words" in got.json()["detail"], got.text


def test_sending_both_cue_shapes_or_neither_is_a_422(client):
    audio = wav_bytes(PHRASE)
    both = client.post("/api/v1/ling/words",
                       files={"audio": ("t.wav", audio, "audio/wav")},
                       data={"lines": json.dumps(LINES),
                             "srt": format_srt(ONE_LINE)})
    assert both.status_code == 422, both.text
    neither = client.post("/api/v1/ling/words",
                          files={"audio": ("t.wav", audio, "audio/wav")},
                          data={})
    assert neither.status_code == 422, neither.text


def test_the_word_timer_is_advertised_where_sdks_look(client):
    body = client.get("/api/v1/info").json()
    assert "word_level_timing" in body["features"], body["features"]


def test_the_second_listening_endpoint_shares_the_kill_switch(client, monkeypatch):
    from app import main as m
    monkeypatch.setattr(m, "_flags_all",
                        lambda: {"uzbek_language_engine": {"enabled": False}})
    assert _words(client, wav_bytes(PHRASE)).status_code == 404


def test_the_report_survives_json_with_no_infinities(client):
    raw = _words(client, wav_bytes(PHRASE)).text
    assert "Infinity" not in raw and "NaN" not in raw


# ─── the job pipeline ─────────────────────────────────────────────────────────

def _submit(client, auth, words=None, jtype="subtitles"):
    data = {"jtype": jtype, "src": "uz", "tgt": "ru"}
    if words is not None:
        data["words"] = words
    r = client.post("/api/jobs", headers=auth,
                    files={"file": ("phrase.srt",
                                    format_srt(ONE_LINE).encode(), "text/plain")},
                    data=data)
    assert r.status_code == 201, r.text
    return r.json()["job"]


def test_the_words_step_sits_after_the_layout_that_moved_the_windows():
    """Ordering is the feature: Ovoz Qator changes cue windows, and words timed to
    a pre-layout card would highlight a text the screen no longer shows."""
    from app.main import _STEP_PCT
    assert _STEP_PCT["polish"] < _STEP_PCT["words"] < _STEP_PCT["tts"]


@pytest.mark.parametrize("flag", ["1", "true", "on", "yes", "TRUE"])
def test_the_words_form_flag_reaches_the_job(client, auth, flag):
    from app import db
    job = _submit(client, auth, words=flag)
    assert db.get_job(job["id"])["meta"]["words"] is True
    assert "words" in {e["step"] for e in db.job_timeline(job["id"])}


@pytest.mark.parametrize("flag", ["", "0", "off", "no", "false"])
def test_a_falsy_words_flag_stays_off(client, auth, flag):
    from app import db
    job = _submit(client, auth, words=flag)
    assert db.get_job(job["id"])["meta"].get("words") is None
    steps = {e["step"] for e in db.job_timeline(job["id"])}
    assert "words" not in steps, steps
    assert "words" not in job["artifacts"]


def test_a_pasted_document_reports_why_the_words_never_arrived(client, auth):
    """A text job has no cue windows on a tape. The step cannot run — the one
    thing it must never do is stay quiet about it."""
    from app import db
    job = _submit(client, auth, words="1")
    line = [e for e in db.job_timeline(job["id"]) if e["step"] == "words"][-1]
    assert "no audio track" in line["message"], line
    assert job["status"] == "done", job
    assert "words" not in job["artifacts"]


def test_a_transcribe_job_says_the_option_does_not_apply(client, auth):
    from app import db
    job = _submit(client, auth, words="1", jtype="transcribe")
    line = [e for e in db.job_timeline(job["id"]) if e["step"] == "words"][-1]
    assert "subtitle job" in line["message"], line


def test_the_step_writes_a_report_and_a_karaoke_track(client, auth, tmp_path):
    r"""The artifact pair is the paid deliverable: the JSON says how every boundary
    was found, the ASS is what a player burns in. The plain subtitle track must
    stay plain — \kf tags in a player that ignores them are still tags."""
    from app import db, pipeline
    from audio_studio import listening
    uid = db.create_user("W", "+99890w0000")["id"]
    job = db.create_job(uid, "subtitles", "uz", "ru", 1.0, "x.srt", {})
    cues = [Cue(i + 1, c.start, c.end, c.text) for i, c in enumerate(ONE_LINE)]
    pipeline._words_step(job["id"], listening(PHRASE, cues, 1.0), cues)
    arts = {k: Path(p) for k, p in
            ((k, db.get_artifact(job["id"], k)) for k in ("words", "ass_karaoke"))}
    assert all(p and p.exists() for p in arts.values()), arts
    rep = json.loads(arts["words"].read_text(encoding="utf-8"))
    assert rep["summary"]["words"] == 5
    assert "\\kf" in arts["ass_karaoke"].read_text(encoding="utf-8")
    events = [e for e in db.job_timeline(job["id"]) if e["step"] == "words"]
    assert "5 words" in events[-1]["message"], events[-1]


def test_both_new_artifacts_are_offered_to_job_and_share_views():
    """One list serves the private job view and the public share page: a kind
    added to one and not the other either hides a file or leaks it."""
    from app.main import _JOB_ARTIFACT_KINDS
    assert "words" in _JOB_ARTIFACT_KINDS
    assert "ass_karaoke" in _JOB_ARTIFACT_KINDS


# ─── the widget: static invariants the suite can check without a browser ───────

def _html():
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _js():
    return (STATIC / "app.js").read_text(encoding="utf-8")


def _css():
    return (STATIC / "styles.css").read_text(encoding="utf-8")


def test_the_markup_of_the_new_section_carries_no_inline_style_or_handler():
    html = _html()
    section = html[html.index('id="soz"'):html.index('<section class="caps"')]
    assert "style=" not in section and "onclick=" not in section
    assert "soz_aria" in section
    # every control in the landing form area must be type="button": a bare <button>
    # inside the page's form would submit it on the first tap on a word
    assert "<button " not in section.replace('<button type="button"', "")


def test_the_studio_offers_the_option_and_hides_it_where_there_is_no_tape():
    html = _html()
    assert 'id="words-row"' in html and 'id="j-words"' in html
    js = _js()
    assert 'fd.append("words", "1")' in js
    assert '$("#words-row").classList.toggle("hidden", docMode)' in js
    toggle = js[js.index('$("#words-row").classList.toggle'):]
    assert '$("#j-words").checked = false;' in toggle[:200]


def test_the_widget_sends_a_real_recording_and_relocalises_without_a_refetch():
    js = _js()
    assert '"/api/v1/ling/words"' in js
    assert "window.__sozRepaint" in js
    assert "() => window.__sozRepaint?.()," in js, \
        "the So'z widget is not in the locale-switch painter list"
    widget = js[js.index("// ─── Ovoz So'z demo"):js.index("// ─── auth ───")]
    assert "atob" not in widget and "data:audio" not in widget
    assert "wavMono16(" in widget            # synthesised, not embedded
    assert "requestAnimationFrame" in widget


def test_the_demo_tape_is_written_from_the_cue_list_and_not_from_a_tone():
    """A showcase that scores 0% measured boundaries showcases the worst mode.
    The tape this widget used to send was three steady bursts — honest, but with
    no air between words, so the engine could only ever answer `speech` and the
    page was advertising its own fallback. The schedule is now derived from the
    cues, and the gap has to be at least the two frames the engine needs in order
    to see a gap at all."""
    js = _js()
    widget = js[js.index("// ─── Ovoz So'z demo"):js.index("// ─── auth ───")]
    assert "function schedule()" in widget
    assert "for (const [n, amp] of cells)" in widget
    assert "syllables(" in widget, \
        "words stopped carrying their own air: the strip would go flat again"
    assert r"c.text.split(/\s+/)" in widget, "the tape is no longer built from the words"
    assert "TAPE" not in widget, "a hand-written tape came back"
    gap = re.search(r"const WORD_GAP = ([0-9.]+);", widget)
    assert gap, "the word gap disappeared"
    assert float(gap.group(1)) >= 0.04, \
        "below two 20 ms frames the engine cannot see a pause: the demo would lie"


def test_a_language_switch_keeps_the_word_the_visitor_chose():
    """Live QA found it: switching UZ → EN rebuilt the whole strip and with the
    labels wiped the highlight and the readout too, so the word a visitor had just
    asked about vanished mid-reading. A repaint re-locates the choice by
    coordinates, and a word with no measured boundary is never credited with a
    method the engine did not use."""
    js = _js()
    widget = js[js.index("// ─── Ovoz So'z demo"):js.index("// ─── auth ───")]
    paint = widget[widget.index("function paint(answer)"):]
    assert "const keep = selAt;" in paint, "the repaint no longer remembers the choice"
    assert "if (keep) {" in paint
    assert "announce(describe(selAt))" in widget
    assert 'm ? t(METHOD[m]) : "—"' in widget
    assert '|| "soz_m_speech"' not in widget, \
        "an unmeasured border is being labelled as a measured one"


def test_every_word_state_the_script_emits_has_a_rule():
    """The strip is built from class names assembled at runtime (`d${d}`,
    `data-m=`); a bucket with no CSS rule is a word that silently loses its
    width or its provenance, and no HTTP test would notice."""
    css = _css()
    js = _js()
    assert "d${d}" in js
    for n in (2, 3, 4):
        assert f".wz-slot.d{n} {{" in css, f"bucket d{n} has no rule"
    for meth in ("valley", "quiet", "speech"):
        assert f'.wz[data-m="{meth}"]' in css, f"method {meth} has no rule"
    for state in (".wz.on", ".wz.now"):
        assert state in css, f"{state} is never styled"
    # and the buckets the script can emit are exactly the buckets styled above
    assert "bucket(dur, longest)" in js
    assert re.search(r"r > 0\.75 \? 4 : r > 0\.5 \? 3 : r > 0\.28 \? 2 : 1", js), \
        "the bucket ladder no longer matches the four CSS steps"


def test_the_proportional_strip_cannot_overflow_a_phone():
    """The word widths are the answer, so the collapse rule has to keep them on
    the screen: live QA on a 643 px viewport measured 18 px of horizontal scroll
    once a two-class rule out-specified a media collapse. Below 561 px the strip
    wraps and each word keeps its own measure."""
    css = _css()
    block = css[css.index("A proportional timeline"):] if "A proportional timeline" in css else css
    mobile = block[:block.index(".cap-big")] if ".cap-big" in block else block
    assert "@media (max-width: 560px)" in mobile
    assert ".soz-strip { flex-wrap: wrap; }" in mobile
    assert "overflow-wrap: anywhere" in css
