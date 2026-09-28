"""Round 14 — Ovoz Turn: the proprietary speaker-attribution engine (the moat #2).

These tests describe what a customer is allowed to be lied about, not internals:
an interview must come out as two voices, a monologue must never grow a second
one, the same tape in Cyrillic and in Latin must give the *same* answer, a voice
must never be invented out of punctuation, and every boundary must carry the cue
that produced it (an unexplainable decision cannot be debugged, let alone sold).

Plus the two ways the engine reaches a real user: the public /api/v1/ling/diarize
endpoint and the `diarize` flag on a job.
"""
import time

import pytest

from app.ling import diarize as d
from app.ling.srt import Cue

# ─── fixtures: real Uzbek speech as an ASR delivers it ────────────────────────

INTERVIEW_LATIN = [
    (0.0, 3.4, "Assalomu alaykum, ustoz, vaqtingizni olganim uchun uzr, boshlaymizmi?"),
    (3.6, 7.9, "Va alaykum assalom, xush kelibsiz, men tayyorman."),
    (8.2, 12.5, "Sizning so'nggi loyihangiz haqida so'rasam bo'ladimi?"),
    (12.8, 19.1, "Albatta, biz uni Farg'ona vodiyida ikki oyda ishga tushirdik."),
    (19.4, 22.0, "Qiyin bo'lmadi mi?"),
    (22.3, 28.8, "Qiyin edi, lekin jamoa tajribali edi, shuning uchun hammasi o'z vaqtida bo'ldi."),
    (29.2, 32.5, "Oxirgi savol: endi nimani rejalashtirmoqdasiz?"),
    (32.8, 37.4, "Kelasi yil uchun ikkinchi vodiy loyihasini rejalashtirganmiz."),
]

# The same tape transcribed in the 2000s Cyrillic alphabet: every lexical cue is
# folded to Latin inside the engine, so the attribution must not move by one cue.
INTERVIEW_CYRILLIC = [
    (0.0, 3.4, "Ассалому алайкум, устоз, вақтингизни олганим учун узр, бошлаймизми?"),
    (3.6, 7.9, "Ва алайкум ассалом, хуш келибсиз, мен тайёрман."),
    (8.2, 12.5, "Сизнинг сўнги лойиҳангиз ҳақида сўрасам бўладими?"),
    (12.8, 19.1, "Албатта, биз уни Фарғона водийида икки ойда ишга туширдик."),
    (19.4, 22.0, "Қийин бўлмади ми?"),
    (22.3, 28.8, "Қийин эди, лекин жамоа тажрибали эди, шунинг учун ҳаммаси ўз вақтида бўлди."),
    (29.2, 32.5, "Охирги савол: энди нимани режалаштирмоқдасиз?"),
    (32.8, 37.4, "Келаси йил учун иккинчи водий лойиҳасини режалаштирганмиз."),
]

MONOLOGUE = [
    (0.0, 6.2, "Bugun ertalab ofisga kirib, jadvalni qaytadan ko'rib chiqdim."),
    (6.4, 13.0, "Eski reja bizga mos kelmadi, chunki mijozlar soni ikki baravarga oshdi."),
    (13.2, 20.5, "Shuning uchun jamoa ikki guruhga bo'lindi va har biri o'z yo'nalishi bo'yicha ishladi."),
    (20.7, 27.1, "Natijada birinchi partiyani mijozga ikki oy muddatda yetkazdik."),
]


def segs(cues):
    return [Cue(i + 1, s, e, t) for i, (s, e, t) in enumerate(cues)]


def speakers_of(cues, **kw):
    return [t.speaker for t in d.analyze_turns(segs(cues), **kw)]


# ─── behaviour 1: a dialogue really splits, and only into the voices present ───

def test_a_two_voice_interview_comes_out_as_two_alternating_voices():
    got = speakers_of(INTERVIEW_LATIN)
    assert got == [1, 2, 1, 2, 1, 2, 1, 2], got


def test_the_same_tape_in_cyrillic_and_in_latin_gives_the_same_answer():
    """Script invariance is a contract, not a nicety: the same interview recorded
    once in Tashkent Cyrillic and once in Latin must not produce two transcripts
    with different authors."""
    assert (speakers_of(INTERVIEW_CYRILLIC) == speakers_of(INTERVIEW_LATIN))


def test_a_monologue_never_grows_a_second_voice():
    assert set(speakers_of(MONOLOGUE)) == {1}


def test_an_asr_split_in_the_middle_of_a_sentence_is_not_a_turn():
    """The single most common diarizer bug: one utterance delivered as two cues.
    No closing punctuation, a near-silent gap and a lowercase start mean the same
    person is still talking."""
    halves = [
        (0.0, 4.0, "Men bugun bozorga borib"),
        (4.02, 7.5, "kitob oldim, keyin esa to'g'ridan-to'g'ri ishga keldim."),
    ]
    assert speakers_of(halves) == [1, 1]


def test_identical_wording_without_audio_invents_nobody():
    """Four cues that read exactly alike carry no evidence at all. A text-only job
    must answer 'one voice' rather than guess, because a wrong author in subtitles
    is worse than no author."""
    same = [(i * 5.0, i * 5.0 + 3.0, "Yaxshi, davom etamiz.") for i in range(4)]
    assert set(speakers_of(same)) == {1}


def test_acoustics_split_two_cues_that_read_exactly_alike():
    """The other half of the same rule: identical words from two different voices
    are two people, and only the tape can say so."""
    same = [(0.0, 3.0, "Yaxshi, davom etamiz."), (3.4, 6.6, "Yaxshi, davom etamiz.")]
    voices = [(0.21, 0.11, 0.30), (0.74, 0.48, 1.62)]
    got = [t.speaker for t in d.analyze_turns(segs(same), voices=voices)]
    assert got == [1, 2], got


def test_a_third_genuinely_different_voice_adds_a_third_speaker():
    cues = [
        (0.0, 3.0, "Assalomu alaykum, hozir navbatma-navbat gapiramiz."),
        (3.2, 6.4, "Va alaykum assalom, men tayyorman, boshlaymiz."),
        (6.8, 10.2, "Rahmat, endi men ham o'z fikrimni aytaman."),
        (10.6, 14.0, "Albatta, sizning gapingiz biz uchun muhim."),
        (14.4, 17.8, "Men esa boshqa masalani ko'taraman, bu juda zarur."),
    ]
    voices = [
        (0.20, 0.10, 0.28), (0.21, 0.11, 0.29),   # speaker A
        (0.66, 0.42, 1.40), (0.68, 0.44, 1.45),   # speaker B
        (0.05, 0.90, 1.90),                       # speaker C: fits neither
    ]
    # The fixture has to earn its name: a "third voice" that sits inside the split
    # threshold of an established profile is not a third person, and the engine is
    # right to fold it back. Assert the tape really carries three distances.
    for speaker in (voices[0], voices[2]):
        assert d.voice_distance(voices[4], speaker) > d.VOICE_SPLIT_DIST
    got = [t.speaker for t in d.analyze_turns(segs(cues), voices=voices)]
    assert len(set(got)) == 3, got


def test_speaker_count_is_a_hard_ceiling():
    """A twelve-cue panel cannot mint more than three ids when the caller asked for
    three: the extra voices fold back into the people already on the tape."""
    loud = [(i * 4.0, i * 4.0 + 3.0, "Assalomu alaykum, men ham fikr bildirmoqchiman?")
            for i in range(12)]
    voices = [(0.10 + 0.07 * i, 0.05 * i, 0.2 + 0.6 * i) for i in range(12)]
    got = [t.speaker for t in d.analyze_turns(segs(loud), max_speakers=3, voices=voices)]
    assert set(got) <= {1, 2, 3}, got
    assert speakers_of(MONOLOGUE, max_speakers=1) == [1] * 4


# ─── behaviour 2: every decision is explainable ───────────────────────────────

def test_every_speaker_change_names_the_cue_that_made_it():
    turns = d.analyze_turns(segs(INTERVIEW_LATIN))
    for prev, cur in zip(turns, turns[1:]):
        if cur.speaker != prev.speaker:
            assert cur.cues, f"cue {cur.index} changed the floor with no stated reason"


def test_the_engine_is_deterministic():
    first = [(t.speaker, t.score, t.cues) for t in d.analyze_turns(segs(INTERVIEW_LATIN))]
    second = [(t.speaker, t.score, t.cues) for t in d.analyze_turns(segs(INTERVIEW_LATIN))]
    assert first == second


def test_summary_counts_the_same_truth_the_lines_show():
    turns = d.analyze_turns(segs(INTERVIEW_LATIN))
    read = d.summary(turns)
    assert read["speakers"] == 2 and read["turns"] == len(INTERVIEW_LATIN)
    assert sum(r["turns"] for r in read["per_speaker"]) == read["turns"]
    assert read["per_speaker"] == sorted(read["per_speaker"], key=lambda r: -r["seconds"])
    assert 0.0 < read["longest_share"] <= 1.0


def test_transcript_tags_every_line_once():
    turns = d.analyze_turns(segs(INTERVIEW_LATIN))
    lines = d.plain(turns).split("\n")
    assert len(lines) == len(INTERVIEW_LATIN)
    assert all(ln.startswith("[S1] ") or ln.startswith("[S2] ") for ln in lines)


# ─── behaviour 3: degenerate input is data, not a crash ───────────────────────

@pytest.mark.parametrize("cues", [
    [],
    [(0.0, 0.0, "")],
    [(0.0, 5.0, None)],                      # type is str|None upstream: tolerate
    [(0.0, 1.0, "?")],                      # no words at all
    [(9.0, 2.0, "Teskari vaqt")],           # end before start
    [(0.0, 1e9, "Uzun qural")],             # absurd duration
])
def test_the_engine_survives_input_it_should_not_have_to(cues):
    turns = d.analyze_turns(segs(cues))
    assert len(turns) == len(cues)
    assert all(isinstance(t.speaker, int) and t.speaker >= 1 for t in turns)


def test_attribution_scales_linearly_with_the_cue_count():
    """The pipeline caps a job at 1500 segments; attribution must not be the thing
    that makes a ten-minute job wall-clock out.

    Measured as a ratio, not as a wall: an absolute seconds-bound on a shared or
    loaded machine is a flake with a deadline, and it fails for reasons that have
    nothing to do with the engine. Five times the cues may cost up to twelve times
    as long here — quadratic would cost twenty-five, so an accidental nested loop
    still fails loudly, and a busy CI box does not."""
    def cost(n):
        cues = [(i * 3.0, i * 3.0 + 2.6, "Rahmat, davom etamizmi?") for i in range(n)]
        started = time.perf_counter()
        d.analyze_turns(segs(cues))
        return time.perf_counter() - started

    small = cost(250)
    big = cost(1250)
    assert big < max(0.5, small * 12.0), f"250 cues in {small:.3f}s, 1250 in {big:.3f}s"
    # the absolute guard stays, loose enough to be about the order of magnitude only
    assert cost(1500) < 10.0


# ─── behaviour 4: the linguistic reads themselves ─────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("Men tayyorman.", "1st"),
    ("Сиз келасизми?", "2nd"),           # Cyrillic: folded before it is read
    ("Siz kelasizmi?", "2nd"),           # -mi sits *outside* the person suffix
    ("Kelibsizmi?", "2nd"),
    ("Ishga tushirdik.", "1st"),
    ("U bog'ga bordi.", None),           # 3rd person says nothing about the floor
])
def test_person_of_reads_both_scripts_and_the_question_particle(text, expected):
    assert d.person_of(text) == expected


def test_a_capital_at_the_start_of_a_cue_is_not_someone_being_called():
    """ASR capitalizes the first word of every cue, so 'Albatta,' must not count as
    a vocative — that single false tell invented speakers on real tapes."""
    assert d.greets_or_addresses("Albatta, biz uni ikki oyda ishga tushirdik.") is False
    assert d.greets_or_addresses("Rahmat, Akmal, aniq javob bo'ldi.") is True
    assert d.greets_or_addresses("Раҳмат, Ақмал, аниқ жавоб бў'лди.") is True
    assert d.greets_or_addresses("Rahmat, ustoz, aniq javob bo'ldi.") is True


def test_reply_to_greeting_is_recognised_in_both_scripts():
    assert d.replies_to_greeting(INTERVIEW_LATIN[0][2], INTERVIEW_LATIN[1][2]) is True
    assert d.replies_to_greeting(INTERVIEW_CYRILLIC[0][2], INTERVIEW_CYRILLIC[1][2]) is True
    assert d.replies_to_greeting("Salom.", "Bugun ish yo'q.") is False


def test_is_backchannel_only_answers_and_never_opens_a_topic():
    assert d.is_backchannel("Ha, tushundim.") is True
    assert d.is_backchannel("Ha, albatta, lekin bu loyihaga ikki oy kerak bo'ladi.") is False


def test_continuation_needs_a_real_gap_and_no_closing_punctuation():
    assert d.is_continuation("Men bugun bozorga borib", "kitob oldim", 0.02) is True
    assert d.is_continuation("Men bugun bozorga borib.", "kitob oldim", 0.02) is False
    assert d.is_continuation("Men bugun bozorga borib", "Kitob oldim", 0.02) is False
    assert d.is_continuation("Men bugun bozorga borib", "kitob oldim", 3.0) is False


# ─── the public API surface ────────────────────────────────────────────────────

def test_public_diarize_splits_the_demo_sample(client):
    body = {"lines": [{"start": s, "end": e, "text": t} for s, e, t in INTERVIEW_LATIN]}
    r = client.post("/api/v1/ling/diarize", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["speakers"] == 2
    assert [ln["speaker"] for ln in out["lines"]] == [1, 2, 1, 2, 1, 2, 1, 2]
    assert out["transcript"].count("[S1] ") == 4 and out["transcript"].count("[S2] ") == 4


def test_public_diarize_accepts_a_raw_srt_document(client):
    doc = ("1\n00:00:00,000 --> 00:00:03,400\nAssalomu alaykum, ustoz, boshlaymizmi?\n\n"
           "2\n00:00:03,600 --> 00:00:07,900\nVa alaykum assalom, men tayyorman.\n\n"
           "3\n00:00:08,200 --> 00:00:12,500\nSizning loyihangiz haqida so'rasam bo'ladimi?\n")
    r = client.post("/api/v1/ling/diarize", json={"srt": doc})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["turns"] == 3
    assert [ln["speaker"] for ln in out["lines"]] == [1, 2, 1]


@pytest.mark.parametrize("body,code", [
    ({}, 422),                                              # nothing to attribute
    ({"lines": "not even a list"}, 422),
    ({"lines": [{"start": 0, "end": 1}]}, 422),             # cue without text
    ({"lines": [{"start": "x", "end": 1, "text": "Salom"}]}, 422),
    ({"lines": [{"start": 5, "end": 1, "text": "Salom"}]}, 422),   # ends before it starts
    ({"lines": [{"start": -3, "end": 1, "text": "Salom"}]}, 422),  # a negative time
    ({"lines": [{"start": i * 2.0, "end": i * 2.0 + 1, "text": "Salom"}
                for i in range(401)]}, 422),                      # over the cue cap
    ({"lines": [{"start": 0, "end": 1, "text": "Salom"}], "max_speakers": 0}, 422),
    ({"lines": [{"start": 0, "end": 1, "text": "Salom"}], "max_speakers": 9}, 422),
    ({"lines": [{"start": 0, "end": 1, "text": "Salom"}], "max_speakers": "two"}, 422),
    ({"lines": [{"start": 0, "end": 1, "text": "Salom"}], "max_speakers": True}, 422),
])
def test_public_diarize_rejects_impossible_input_before_it_costs_cpu(client, body, code):
    assert client.post("/api/v1/ling/diarize", json=body).status_code == code


def test_public_diarize_honours_the_engine_kill_switch(client, monkeypatch):
    from app import main as m
    monkeypatch.setattr(m, "_flags_all",
                        lambda: {"uzbek_language_engine": {"enabled": False}})
    body = {"lines": [{"start": 0, "end": 1, "text": "Salom"}]}
    assert client.post("/api/v1/ling/diarize", json=body).status_code == 404


def test_the_diarize_step_has_a_progress_percentage():
    """The live channel drives the bar from this table: a step missing there shows
    up as the 30% default and the bar walks backwards mid-job."""
    from app.main import _STEP_PCT
    assert _STEP_PCT["diarize"] > _STEP_PCT["asr"], "diarization runs after ASR"
    assert _STEP_PCT["diarize"] < _STEP_PCT["translate"]


# ─── the job pipeline ──────────────────────────────────────────────────────────

_SRT_TMPL = """1
00:00:00,000 --> 00:00:03,400
{0}

2
00:00:03,600 --> 00:00:07,900
{1}

3
00:00:08,200 --> 00:00:12,500
{2}
"""


def _uzbek_srt_bytes():
    return _SRT_TMPL.format(INTERVIEW_LATIN[0][2], INTERVIEW_LATIN[1][2],
                            INTERVIEW_LATIN[2][2]).encode("utf-8")


def _submit(client, auth, diarize=None, jtype="subtitles"):
    data = {"jtype": jtype, "src": "uz", "tgt": "ru"}
    if diarize is not None:
        data["diarize"] = diarize
    r = client.post("/api/jobs", headers=auth,
                    files={"file": ("interview.srt", _uzbek_srt_bytes(), "text/plain")},
                    data=data)
    assert r.status_code == 201, r.text
    return r.json()["job"]


def test_a_plain_job_does_not_pay_for_speakers_it_never_asked_for(client, auth):
    from app import db
    job = _submit(client, auth)
    assert job["artifacts"].keys() <= {"transcript", "srt", "srt_bilingual", "ass"}
    assert "diarization" not in job["artifacts"]
    assert db.get_job(job["id"])["meta"].get("diarize") is None
    text = client.get(job["artifacts"]["transcript"], headers=auth).text
    assert "[S" not in text


@pytest.mark.parametrize("flag", ["1", "true", "on", "yes", "TRUE"])
def test_the_diarize_form_flag_lands_in_meta_and_artifacts(client, auth, flag):
    """A Form flag arrives as a string. '0'/'off'/'' must not light up — the test
    below checks the off side, this one the whole truthy vocabulary."""
    from app import db
    job = _submit(client, auth, diarize=flag)
    assert db.get_job(job["id"])["meta"]["diarize"] is True
    assert "diarization" in job["artifacts"]
    read = client.get(job["artifacts"]["diarization"], headers=auth).json()
    assert read["speakers"] == 2 and read["turns"] == 3
    text = client.get(job["artifacts"]["transcript"], headers=auth).text
    assert text.startswith("[S1] ")
    assert "[S2] " in text
    subs = client.get(job["artifacts"]["srt"], headers=auth).text
    assert "[S1]" in subs and "[S2]" in subs


@pytest.mark.parametrize("flag", ["", "0", "off", "no", "false"])
def test_a_falsy_diarize_flag_stays_off(client, auth, flag):
    from app import db
    job = _submit(client, auth, diarize=flag)
    assert db.get_job(job["id"])["meta"].get("diarize") is None
    assert "diarization" not in job["artifacts"]


def test_diarization_is_recorded_in_the_job_timeline(client, auth):
    """The user watches steps arrive live; a silent expensive stage looks like a
    hang, and the event is also the only audit of what the engine was fed."""
    from app import db
    job = _submit(client, auth, diarize="1")
    steps = [e["step"] for e in db.job_timeline(job["id"])]
    assert "diarize" in steps
    assert steps.index("asr") < steps.index("diarize")


def test_batch_upload_forwards_the_diarize_flag(client, auth):
    """The single-upload path was wired first and the batch path silently dropped
    the flag — so the check is on the batch endpoint itself, not on the helper."""
    from app import db
    files = [("files", ("one.srt", _uzbek_srt_bytes(), "text/plain")),
             ("files", ("two.srt", _uzbek_srt_bytes(), "text/plain"))]
    r = client.post("/api/jobs/batch", headers=auth, files=files,
                    data={"jtype": "transcribe", "src": "uz", "tgt": "ru",
                          "diarize": "1"})
    assert r.status_code == 201, r.text
    created = r.json()["results"]
    assert len(created) == 2
    for item in created:
        assert item["ok"] is True
        assert db.get_job(item["job"]["id"])["meta"]["diarize"] is True
        assert "diarization" in item["job"]["artifacts"]


def test_the_share_view_offers_exactly_what_the_private_view_offers(client, auth):
    """Both views list artifacts by hand. If they drift, a shared link either hides
    a file the owner can see or leaks one the owner never had."""
    from app.main import _JOB_ARTIFACT_KINDS
    job = _submit(client, auth, diarize="1")
    r = client.post(f"/api/jobs/{job['id']}/share", headers=auth, data={})
    assert r.status_code == 200, r.text
    share = client.get(r.json()["share_url"])
    assert share.status_code == 200, share.text
    assert set(share.json()["artifacts"]) == set(job["artifacts"])
    assert set(job["artifacts"]) <= set(_JOB_ARTIFACT_KINDS)
    assert "diarization" in job["artifacts"], job["artifacts"]


# ─── Round 14.1: what the adversarial review and browser QA caught ────────────

def test_a_voice_profile_is_read_from_the_window_it_claims():
    """A cue at 16 s must be measured at 16 s.

    Dividing the sample offset by the decimation stride *and* stepping by it reads
    every profile from twice as early: the loud guest gets the host's quiet room,
    and the voice family then argues from evidence that never existed."""
    from array import array

    from app import pipeline as pl
    from app.providers.base import Segment

    rate = pl.DIAR_PCM_RATE
    pcm = array("h", [400 if i < rate * 10 else 20000 for i in range(rate * 20)])
    # The buffer, not the file: since Round 17 one decode serves the aligner and
    # the diarizer alike, so `_voices` is handed PCM and no longer a path.
    quiet, loud = pl._voices(pcm, [Segment(1.0, 3.0, "birinchi"),
                                   Segment(16.0, 18.0, "ikkinchi")])
    assert quiet[0] < 0.05 < 0.4 < loud[0], (quiet, loud)
    # A cue that straddles the boundary must be described by the whole utterance.
    # Truncating every profile to its first second turned an onset — a breath, a
    # stressed vowel — into a voice, which is exactly the false split the engine is
    # not allowed to make.
    straddle = pl._voices(pcm, [Segment(8.0, 12.0, "aralash")])[0]
    assert straddle[0] > 0.15, straddle


@pytest.mark.parametrize("text,expected", [
    ("U kecha kelgan.", None),              # -gan is a participle, not a person
    ("Hammasi yaxshi bo'lgan.", None),
    ("Men kecha kelganman.", "1st"),        # the real compound still ends in -man
    ("Siz kelgan ekansiz.", "2nd"),
])
def test_a_past_participle_does_not_make_someone_speak(text, expected):
    assert d.person_of(text) == expected


def test_a_past_tense_narrative_never_invents_a_second_voice():
    """The bug this closes was loud: every -gan tail read as "men", so a witness
    describing the past flipped person on each cue and the engine handed the floor
    to a speaker nobody in the room ever heard."""
    cues = [(0.0, 5.0, "U dastlab shu yerga kelgan."),
            (5.3, 10.0, "Keyin u ishni boshlab yuborgan."),
            (10.4, 15.0, "Va u hammasini o'zi qilgan.")]
    assert speakers_of(cues) == [1, 1, 1]


@pytest.mark.parametrize("text,expected", [
    ("Вы можете это повторить?", "2nd"),
    ("Мы это вместе сделали.", "1st"),
    ("Vy mozhete eto povtorit?", "2nd"),    # Russian typed in Latin: same answer
])
def test_russian_code_switch_carries_person_like_the_script_it_is_written_in(text, expected):
    """Half of Tashkent answers an Uzbek interview in Russian. The needles used to
    be Cyrillic literals compared against folded text, where 'вы' has become 'vы' —
    so the whole morphology family was silently dead on exactly those cues."""
    assert d.person_of(text) == expected


def test_the_srt_shape_applies_the_same_timing_rule_as_the_lines_shape(client):
    """The `lines` path rejected end-before-start; the SRT path handed it straight
    to the engine, where a large negative gap reads as an overlap cue."""
    doc = ("1\n00:00:10,000 --> 00:00:05,000\nAssalomu alaykum.\n\n"
           "2\n00:00:06,000 --> 00:00:09,000\nVa alaykum assalom.\n")
    assert client.post("/api/v1/ling/diarize", json={"srt": doc}).status_code == 422


def test_a_timecode_that_runs_for_days_is_refused_on_both_shapes(client):
    """`99:59:59,999` is legal SRT syntax and an impossible cue: bounding only the
    start left the end free to fabricate a 356399-second overlap."""
    doc = ("1\n00:00:00,000 --> 99:59:59,999\nMen uyga bordim.\n\n"
           "2\n00:00:01,000 --> 00:00:02,000\nKeyin kitob o'qdim.\n")
    assert client.post("/api/v1/ling/diarize", json={"srt": doc}).status_code == 422
    body = {"lines": [{"start": 0, "end": 1e18, "text": "Men uyga bordim."},
                      {"start": 1, "end": 2, "text": "Keyin kitob o'qdim."}]}
    assert client.post("/api/v1/ling/diarize", json=body).status_code == 422


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_timecode_never_reaches_the_aggregate(client, literal):
    """json.loads accepts the NaN/Infinity literals and `end < start` is False for
    all of them, so the value used to sail into summary() and out through a JSON
    encoder that cannot represent it. Sent as raw bytes because a real client's
    serializer is allowed to refuse them — the server's is not."""
    body = ('{"lines":[{"start":0,"end":%s,"text":"Salom."}]}' % literal).encode()
    r = client.post("/api/v1/ling/diarize", content=body,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 422, r.text


def test_a_cue_swallowed_by_the_previous_one_is_a_broken_export_not_two_voices():
    """Only a PARTIAL overlap means two people talked at once. Even if a monster
    timecode slipped past a future validator, the engine must not read it as a
    second voice — over-attribution is the one error this product will not make."""
    cues = [(0.0, 356399.999, "Men uyga bordim."), (1.0, 2.0, "Keyin kitob o'qdim.")]
    assert speakers_of(cues) == [1, 1]
    # A document in the wrong order is the same shape: the second line ends before
    # the first one started.
    shuffled = [(10.0, 11.0, "Men uyga bordim."), (0.0, 1.0, "Keyin kitob o'qdim.")]
    assert speakers_of(shuffled) == [1, 1]
    # And a real overlap still counts, or the rule would be a refusal to ever
    # hear two voices at once.
    real = [(0.0, 5.0, "Men uyga bordim."), (4.2, 9.0, "Sen qachon kelasan?")]
    assert len(set(speakers_of(real))) == 2


@pytest.mark.parametrize("text,expected", [
    ("U dushman haqida gapirdi.", None),      # -man on a noun: nobody is speaking
    ("Bu juda qiziq roman edi.", None),
    ("Bu odamga qasam ichdim.", "1st"),       # the verb still reads, the noun not
    ("Это Таджикистан.", None),               # -stan was buying a country as a person
    ("Ko'rdingizmi?", "2nd"),                 # the formal address the family missed
    ("Сиз кўрингизми?", "2nd"),
])
def test_a_person_suffix_is_only_read_where_a_verb_could_be(text, expected):
    assert d.person_of(text) == expected


def test_a_terminal_step_never_walks_the_progress_bar_backwards():
    """Every step that ends a worker's stream must sit at the top of the table:
    'orphan' once reported 60, so a dubbing job visibly fell back from 85%."""
    from app.main import _STEP_PCT
    top = max(_STEP_PCT.values())
    for step in ("done", "failed", "canceled", "orphan"):
        assert _STEP_PCT[step] == top, step


def test_the_demo_result_is_rendered_into_a_live_region():
    """Attribution lands asynchronously. If the node app.js writes the outcome to
    is not an aria-live region, a screen-reader user clicks and hears nothing."""
    import re
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    ids = set(re.findall(r'\$\("#([\w-]+)"\)', js))
    for id_attr in ids & {"turn-metrics"}:
        tag = re.search(rf'<[^>]*id="{id_attr}"[^>]*>', html)
        assert tag, f"#{id_attr} is written by app.js but missing from the page"
        assert 'aria-live="polite"' in tag.group(0), tag.group(0)
    assert "turn-metrics" in ids, "the demo no longer paints a result region"
