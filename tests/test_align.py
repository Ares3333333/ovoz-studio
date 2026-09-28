"""Round 17 — Ovoz Jimlik: silence-gap boundary aligner.

The buyer's claim here is one sentence: *the cut lands where the speaker
stopped*. Everything below is measured on audio we generated ourselves, so the
truth of that sentence does not depend on a fixture someone recorded once and an
engine that quietly learned its shape.

Four properties are checked on every case, plus the refusals:

  1. words are untouched (this engine has no authority over text);
  2. a moved boundary ends up inside measured silence, clear of the speech edge;
  3. nothing travels further than the declared `max_shift`, and no input timing
     is made worse;
  4. the same tape and the same cues produce the same report twice.

Audio is built in-test with `math` and `array` only: no numpy, no sample files,
no network, deterministic on any machine that runs the suite.
"""
import io
import json
import math
import re
import time
from array import array
from pathlib import Path

import pytest

from app.ling import align as A
from app.ling.srt import Cue, format_srt

RATE = 8_000                     # the rate the pipeline decodes to
FRAME_S = A.FRAME_MS / 1000.0    # 20 ms


# ─── the studio: synthetic speech-shaped audio ────────────────────────────────
# Shared with the Round-18 word timer (tests/audio_studio.py): two engines listen
# to the same invented tapes, so they must be handed the same truth about them.
from audio_studio import (BLOCK_SEC, FRAME_S, RATE, block, block_cues, interview,
                          long_tape, nonstop, rewrite_header, room, tape, voice,
                          wav_bytes)  # noqa: E402

# ─── measuring the tape ───────────────────────────────────────────────────────

def test_frame_levels_are_full_scale_rms():
    """The ruler before the ruling: a full-scale tone reads near 0.7 (sine RMS is
    amp/√2, dipped by the syllable modulation), silence reads exactly 0."""
    levels = A.frame_levels(tape(voice(0.2, amp=1.0)), 160)
    assert levels and all(0.4 < lv < 0.8 for lv in levels)
    assert A.frame_levels(tape(room(0.2)), 160) == [0.0] * 10   # 0.2 s / 20 ms


def test_detect_gaps_finds_the_two_pauses_we_left_in():
    read = A.detect_gaps(interview(), RATE)
    assert read["speech_runs"] == 3
    assert len(read["gaps"]) == 2
    g1, g2 = read["gaps"]
    # The true edges are 1.0→2.0 and 3.0→3.5; a frame that straddles an edge is
    # loud enough to count as speech, so the tolerance is one frame, not sloppiness.
    assert abs(g1.start - 1.0) <= 2 * FRAME_S and g1.end <= 2.0 + FRAME_S
    assert abs(g2.start - 3.0) <= 2 * FRAME_S and abs(g2.end - 3.5) <= FRAME_S
    assert read["dead_air"] is False
    assert abs(read["duration"] - 5.0) < 0.01
    assert 3.4 < read["speech_sec"] < 3.7
    assert read["speech_sec"] + read["silence_sec"] <= read["duration"] + 0.001


def test_noise_floor_is_measured_from_the_tape_not_assumed():
    """Two tapes, one with digital silence, one with room tone: the floor differs
    by an order of magnitude, yet the same three utterances are heard. A fixed
    threshold would have to be re-tuned for every recording; a percentile does not."""
    quiet = A.detect_gaps(interview(tone=0), RATE)
    hissy = A.detect_gaps(interview(tone=200), RATE)
    assert hissy["noise_floor"] > quiet["noise_floor"] * 5
    assert [round(g.start, 1) for g in hissy["gaps"]] == \
           [round(g.start, 1) for g in quiet["gaps"]]
    assert quiet["speech_level"] < 0.2 and hissy["speech_level"] < 0.25


def test_a_very_quiet_speaker_is_still_a_speaker():
    """amp 0.05 is ≈ -26 dBFS — a lapel mic set low, or a phone across the table.
    Refusing to hear that is how an aligner earns the reputation of being useless."""
    read = A.detect_gaps(interview(amp=0.05), RATE)
    assert read["speech_runs"] == 3 and len(read["gaps"]) == 2


def test_dead_air_says_so_instead_of_inventing_speech():
    """A silent tape has no level above its own floor: `span` is 0, so a naive
    `floor * 1.5` threshold would be 0 and every frame would read as speech."""
    read = A.detect_gaps(tape(room(3.0)), RATE)
    assert read["dead_air"] is True and read["speech_runs"] == 0
    assert read["gaps"] == []           # no pause worth reporting either: no edges


def test_continuous_audio_yields_no_pause_to_align_to():
    read = A.detect_gaps(nonstop(), RATE)
    assert read["gaps"] == [] and read["speech_runs"] == 1


# ─── the aligner, on the buyer's claims ───────────────────────────────────────

CRUSHED = [
    # The classic raw-ASR damage: cuts stamped inside the syllables, 10 ms apart.
    Cue(1, 0.05, 0.95, "Birinchi qator."),
    Cue(2, 1.05, 2.05, "Ikkinchi qator."),
    Cue(3, 2.90, 3.60, "Uchinchi qator."),
]


def cues_of(report):
    return [(c["i"], c["start"], c["end"]) for c in report["cues"]]


def test_cuts_move_into_the_pause_they_were_stamped_over():
    report = A.align(interview(), RATE, CRUSHED)
    g1, g2 = report["gaps"][0], report["gaps"][1]
    got = dict((c["i"], c) for c in report["cues"])
    # cue 1 ended at 0.95 — inside the first utterance — and now sits just after
    # its true end, before the next one starts.
    assert got[1]["end"] > 0.95
    assert g1["start"] + A.BOUNDARY_PAD - 0.001 <= got[1]["end"] <= g1["end"] - A.BOUNDARY_PAD + 0.001
    assert got[1]["start"] == pytest.approx(0.05, abs=0.001)   # no silence before it
    # cue 2 straddles the pause: its start is already in silence, and pulling its
    # end back to 1.97 would leave the card displayed *only* during silence. The
    # retreat is therefore refused, and the cue is returned as it was sent.
    assert got[2]["start"] == pytest.approx(1.05, abs=0.001)
    assert got[2]["end"] == pytest.approx(2.05, abs=0.001)
    # cue 3 was stamped 0.1 s before its line began; the nearest silence is the
    # pause it is standing in, so the cut slides *later* into it. Its end stays at
    # 3.60, inside the last utterance, because that utterance runs to the end of
    # the tape and there is no silence after it to move to.
    assert got[3]["start"] > 2.90
    assert g2["start"] + A.BOUNDARY_PAD - 0.001 <= got[3]["start"] <= g2["end"]
    assert got[3]["end"] == pytest.approx(3.60, abs=0.001)
    assert report["summary"]["moved"] == 2
    assert [m["cue"] for m in report["moves"]] == [1, 3]


# The shape every real export arrives in: cue N ends exactly where cue N+1 starts.
BACK_TO_BACK = [
    Cue(1, 0.0, 0.6, "Birinchi qator."),
    Cue(2, 0.6, 2.8, "Ikkinchi qator."),
    Cue(3, 2.8, 5.0, "Uchinchi qator."),
]


def test_a_back_to_back_cut_moves_as_one_cut():
    """`cue.end == next.start` is one moment with two names.

    Walling the left name by the right name's unmoved position — which is what a
    strictly local pass does — lets only the later cue slide into the pause. The
    visible result is the opposite of the product: the first line still disappears
    mid-word, and a blank flashes where the text used to be continuous."""
    report = A.align(interview(), RATE, BACK_TO_BACK)
    got = dict((c["i"], c) for c in report["cues"])
    for left, right, after in ((1, 2, 0.6), (2, 3, 2.8)):
        assert got[left]["end"] == got[right]["start"], \
            "the pair separated: a blank appeared where the export was continuous"
        assert got[left]["end"] > after, "the cut never left the word it was on"
    assert abs(got[1]["end"] - 1.03) < 0.02 and abs(got[2]["end"] - 3.03) < 0.02
    # Four edges travelled because one cut is two names — and no pause was invented.
    assert report["summary"]["moved"] == 4
    assert report["summary"]["beyond_audio"] == 0
    assert report["summary"]["never_worse"] is True
    assert report["summary"]["overlap_sec_after"] == 0.0
    # The outer ends stay where they were: there is no silence before 0.0 s and none
    # after the last utterance, and a cut cannot be moved by wishful thinking.
    assert got[1]["start"] == pytest.approx(0.0, abs=0.001)
    assert got[3]["end"] == pytest.approx(5.0, abs=0.001)


def test_a_shared_cut_never_either_line():
    """Sharing a cut means the pause is divided between two cards, and neither may
    pay for it: the pair is only allowed to travel as far as each cue keeps its own
    dwell. A cue squeezed to a blink is a worse subtitle than a late one."""
    report = A.align(interview(), RATE, BACK_TO_BACK)
    for c in report["cues"]:
        assert c["end"] - c["start"] >= A.MIN_SPAN
    original = {q.index: q.end - q.start for q in BACK_TO_BACK}
    for c in report["cues"]:
        assert c["end"] - c["start"] >= original[c["i"]] * (1.0 - A.MAX_SPAN_LOSS)


def test_the_report_separates_the_work_from_the_no_op():
    """Three facts, three numbers.

    One counter used to answer for all of them, so a client that had four cuts moved
    successfully read `moved: 4, in_silence: 0` — "you moved four boundaries and none
    of them landed in silence" — and had every reason to refund the job."""
    crushed = A.align(interview(), RATE, BACK_TO_BACK)["summary"]
    assert crushed["already_in_silence"] == 0        # nothing arrived correct
    assert crushed["in_silence"] == crushed["moved"] == 4   # and all four landed
    good = A.align(interview(), RATE, [
        Cue(1, 1.10, 1.30, "Birinchi."), Cue(2, 1.50, 1.70, "Ikkinchi."),
        Cue(3, 3.10, 3.20, "Uchinchi."), Cue(4, 3.30, 3.40, "To'rtinchi.")])
    assert good["summary"]["moved"] == 0
    assert good["summary"]["in_silence"] == good["summary"]["already_in_silence"] == 8


def test_words_are_untouched_and_order_is_kept():
    report = A.align(interview(), RATE, CRUSHED)
    assert [c["text"] for c in report["cues"]] == [c.text for c in CRUSHED]
    assert [c["i"] for c in report["cues"]] == [1, 2, 3]
    assert report["summary"]["words_preserved"] is True
    assert report["summary"]["order_preserved"] is True
    # The SRT body is the same text in the same order, only the timecodes differ.
    body = [ln for ln in report["srt"].splitlines() if not ln.strip().isdigit()
            and "-->" not in ln and ln.strip()]
    assert body == [c.text for c in CRUSHED]


def test_no_boundary_travels_further_than_declared():
    for shift in (0.05, 0.2, 0.6, 2.0):
        report = A.align(interview(), RATE, CRUSHED, max_shift=shift)
        for m in report["moves"]:
            assert m["shift"] <= shift + 0.001
        assert report["summary"]["max_applied"] <= shift + 0.001
        assert report["tuning"]["max_shift"] == pytest.approx(shift, abs=0.001)


def test_already_correct_timing_is_left_alone_to_the_millisecond():
    """The worst thing an "improver" can do is move a cut that was right. Every
    boundary below sits deep inside one of the two pauses of `interview()`."""
    good = [Cue(1, 1.10, 1.30, "Birinchi."),
            Cue(2, 1.50, 1.70, "Ikkinchi."),
            Cue(3, 3.10, 3.20, "Uchinchi."),
            Cue(4, 3.30, 3.40, "To'rtinchi.")]
    report = A.align(interview(), RATE, good)
    assert report["summary"]["moved"] == 0
    assert report["moves"] == []
    assert cues_of(report) == [(1, 1.10, 1.30), (2, 1.50, 1.70),
                               (3, 3.10, 3.20), (4, 3.30, 3.40)]
    # Every boundary of that input was already inside silence: the engine knows
    # the difference between "nothing to do" and "nothing found".
    assert report["summary"]["in_silence"] == 8


def test_a_tape_without_silence_snaps_nothing():
    report = A.align(nonstop(), RATE, [Cue(1, 0.4, 1.6, "Söz"),
                                       Cue(2, 1.7, 3.9, "qator")])
    assert report["summary"]["moved"] == 0
    assert report["audio"]["gaps"] == 0
    assert cues_of(report) == [(1, 0.4, 1.6), (2, 1.7, 3.9)]


def test_the_result_is_never_worse_than_the_input():
    """Cues 2 and 3 overlap by 0.35 s in the source and `max_shift` is too small
    to repair it. The engine may not add a millisecond to that overlap, and it may
    not pretend otherwise: the report carries the measurement from both sides."""
    messy = [Cue(1, 0.0, 0.9, "Birinchi."),
             Cue(2, 1.2, 2.35, "Ikkinchi."),
             Cue(3, 2.0, 3.3, "Uchinchi.")]
    before = [max(0.0, p - s) for p, s in
              zip((c.end for c in messy[:-1]), (c.start for c in messy[1:]))]
    report = A.align(interview(), RATE, messy, max_shift=0.2)
    pairs = report["cues"]
    after = [max(0.0, p - s) for p, s in
             zip((c["end"] for c in pairs[:-1]), (c["start"] for c in pairs[1:]))]
    assert all(a <= b + 0.001 for a, b in zip(after, before))
    assert report["summary"]["overlap_sec_after"] <= sum(before) + 1e-6
    assert report["summary"]["never_worse"] is True
    assert report["summary"]["overlap_sec_before"] == pytest.approx(sum(before),
                                                                    abs=0.001)


def test_a_short_cue_is_not_squeezed_out_of_existence():
    """A 0.22 s cue has no readable dwell, but the aligner is not the layout
    engine: it must not make the span shorter by stealing both edges into pauses."""
    short = [Cue(1, 0.90, 1.12, "X"), Cue(2, 1.20, 2.90, "Uzun qator")]
    report = A.align(interview(), RATE, short)
    for c in report["cues"]:
        assert c["end"] - c["start"] >= A.MIN_SPAN - 0.001


def test_two_boundaries_may_not_share_the_pause_they_both_love():
    """End of cue 1 and start of cue 2 are both pulled toward 1.03–1.97. If the
    engine let them meet, the pair would collide and the report would hide it."""
    tight = [Cue(1, 0.1, 0.98, "Birinchi."), Cue(2, 1.02, 2.6, "Ikkinchi.")]
    report = A.align(interview(), RATE, tight, max_shift=0.6)
    a, b = report["cues"]
    assert b["start"] >= a["end"] + A.MIN_SEP - 0.002


def test_a_card_may_not_be_swallowed_by_one_pause():
    """A boundary can be "perfectly placed" twice and still produce a caption that
    shows while nobody talks and hides for the whole line it carries. The engine may
    never create that state; if the client sent it, it is left as it was."""
    doomed = [Cue(1, 0.0, 0.4, "Kirish."),             # over the first utterance
              Cue(2, 0.85, 1.95, "Qisqa."),            # its end is already in silence
              Cue(3, 2.2, 4.4, "Uzun qator")]
    report = A.align(interview(), RATE, doomed, max_shift=0.6)
    for new, old in zip(report["cues"], doomed):
        for g in report["gaps"]:
            now = g["start"] + A.BOUNDARY_PAD <= new["start"] and \
                new["end"] <= g["end"] - A.BOUNDARY_PAD
            was = g["start"] + A.BOUNDARY_PAD <= old.start and \
                old.end <= g["end"] - A.BOUNDARY_PAD
            assert not now or was, (new, g)
    # Cue 2 is the sharp case: its start would slide 0.18 s into the pause its end
    # already occupies, and the pair would then bracket nothing but silence.
    assert report["cues"][1]["start"] == pytest.approx(0.85, abs=0.001)


def test_dwell_is_not_traded_for_silence_beyond_a_reasonable_share():
    """A 1.0 s cue may slide into silence, but not end up as a 0.25 s flash: past
    MAX_SPAN_LOSS the move costs more readability than the sync it buys."""
    squeeze = [Cue(1, 0.3, 1.3, "Birinchi qator."), Cue(2, 2.1, 2.9, "Ikkinchi")]
    report = A.align(interview(), RATE, squeeze, max_shift=0.6)
    for new, old in zip(report["cues"], squeeze):
        span = new["end"] - new["start"]
        assert span >= (old.end - old.start) * (1.0 - A.MAX_SPAN_LOSS) - 0.002


def test_a_boundary_outside_the_tape_is_reported_not_invented():
    """Cues past the end of the audio have no silence to be measured against;
    they stay put and the summary says how many are hanging."""
    late = [Cue(1, 0.2, 0.9, "Birinchi."), Cue(2, 9.0, 11.0, "Keyingi.")]
    report = A.align(interview(), RATE, late)
    assert report["summary"]["beyond_audio"] == 1
    assert cues_of(report)[1] == (2, 9.0, 11.0)


def test_the_first_start_can_never_go_negative():
    report = A.align(tape(room(0.6), voice(1.0), room(0.6), voice(1.0)), RATE,
                     [Cue(1, 0.75, 1.9, "Salom"), Cue(2, 2.1, 3.0, "Qalaysiz")])
    assert all(c["start"] >= 0.0 for c in report["cues"])


def test_gaps_narrower_than_the_pad_are_not_offered_as_boundaries():
    """A 0.04 s dip between two plosives is not a pause: `MIN_GAP_SEC` is what
    keeps the engine from cutting a word in half and calling it prosody."""
    read = A.detect_gaps(tape(voice(1.0), room(0.04), voice(1.0)), RATE)
    assert read["gaps"] == []


def test_gap_list_in_the_report_is_bounded():
    """A 400-cue audiobook read on a 3-minute tape must not return a report the
    client cannot render; the count is total, the list is capped and flagged."""
    pieces = []
    for _ in range(240):
        pieces += [voice(0.15), room(0.1)]
    report = A.align(tape(*pieces), RATE, [Cue(1, 0.1, 0.2, "salom")],
                     max_shift=0.2)
    assert report["audio"]["gaps"] > len(report["gaps"])
    assert len(report["gaps"]) == A.MAX_REPORT_GAPS
    assert report["gaps_truncated"] is True


def test_engine_is_deterministic_to_the_millisecond():
    pcm, cues = interview(), CRUSHED
    one = json.dumps(A.align(pcm, RATE, cues), ensure_ascii=False, sort_keys=True)
    two = json.dumps(A.align(pcm, RATE, list(cues)), ensure_ascii=False,
                     sort_keys=True)
    three = json.dumps(A.align(pcm, RATE, list(cues)), ensure_ascii=False,
                       sort_keys=True)
    assert one == two == three


def test_report_carries_only_numbers_json_can_hold():
    """`json.dumps(allow_nan=False)` is the whole test: an `Infinity` in this
    report would kill `response.json()` on the share page, where the artifact is
    served raw. Zero-length spans and empty move lists are the paths that make it."""
    report = A.align(tape(room(2.0), voice(1.0), room(2.0)), RATE,
                     [Cue(1, 0.0, 0.0, "nul"), Cue(2, 2.9, 3.1, "norm")])
    blob = json.dumps(report, allow_nan=False, ensure_ascii=False)
    assert "Infinity" not in blob and "NaN" not in blob
    assert report["cues"][0]["start"] == 0.0
    assert math.isfinite(report["summary"]["mean_applied"])


def test_empty_and_absurd_inputs_are_refused_with_a_code():
    for cues, code in (
        ([], "no_cues"),
        ([Cue(i + 1, i, i + 0.5, "x") for i in range(A.MAX_CUES + 1)], "too_many_cues"),
    ):
        with pytest.raises(A.AlignError) as exc:
            A.align(interview(), RATE, cues)
        assert exc.value.code == code
    for bad in (0.0, 1e9, -1.0, float("nan"), "fast"):
        with pytest.raises(A.AlignError) as exc:
            A.align(interview(), RATE, CRUSHED, max_shift=bad)
        assert exc.value.code == "bad_shift"
    with pytest.raises(A.AlignError) as exc:
        A.align(interview(), 3_000, CRUSHED)
    assert exc.value.code == "bad_rate"
    with pytest.raises(A.AlignError) as exc:
        A.align(tape(room(0.01)), RATE, CRUSHED)
    assert exc.value.code == "too_short"
    with pytest.raises(A.AlignError) as exc:
        A.align(array("h", bytes(2 * (A.MAX_LISTEN_SAMPLES + 1))), RATE, CRUSHED)
    assert exc.value.code == "too_long"


# ─── the listening window (Round 19) ───────────────────────────────────────────

def test_a_fifteen_minute_tape_is_heard_all_the_way_to_minute_fifteen():
    """The bug this case exists for: the window used to be a *sample* count, so
    every job past 200 s — most real jobs — came back `align: skipped: too_long`
    while the pipeline happily decoded fifteen minutes of tape.

    The tape repeats one six-second phrase, so its pauses are identical by
    construction: an answer that is right at second two and absent at minute
    fourteen cannot survive the assertions below."""
    pcm = long_tape(900)
    assert len(pcm) == 7_200_000, "the fixture stopped being the window it claims"
    assert len(pcm) > 1_600_000, "the old ceiling: a tape this long was refused"
    report = A.align(pcm, RATE, block_cues(900))
    s = report["summary"]
    assert s["cues"] == 150 and s["boundaries"] == 300
    # Every cut had exactly one reachable pause 0.18 s away, in every block.
    assert s["moved"] == 300 and s["in_silence"] == 300
    assert s["already_in_silence"] == 0 and s["beyond_audio"] == 0
    assert s["max_applied"] == 0.18 == s["mean_applied"]
    assert s["never_worse"] and s["words_preserved"] and s["order_preserved"]
    assert report["audio"]["duration"] == 900.0
    assert report["audio"]["frames"] == 45_000
    assert report["audio"]["window_sec"] == A.MAX_LISTEN_SEC
    # The last block is the interesting one: it is where the old code stopped.
    assert report["moves"][-2]["cue"] == 150
    assert report["cues"][-1]["start"] == 896.03
    # 300 pauses do not fit the report, and the report says so instead of lying
    # about how many it listed.
    assert report["gaps_truncated"] is True and len(report["gaps"]) == A.MAX_REPORT_GAPS
    assert report["moves_truncated"] is False


def test_the_window_is_time_and_a_memory_cap_is_samples():
    """Two ceilings, two messages, because they tell a caller two different things
    to do: shorten the tape, or resample it. At the pipeline's 8 kHz the time bound
    binds (900 s = 7.2 M samples); the sample bound only bites at rates whose
    fifteen minutes nobody could hold in memory anyway."""
    with pytest.raises(A.AlignError) as exc:                      # 901 s of tape
        A.align(long_tape(906), RATE, block_cues(906))
    assert exc.value.code == "too_long"
    assert "900" in str(exc.value) and "samples" not in str(exc.value)

    huge = array("h", bytes(2 * (A.MAX_LISTEN_SAMPLES + 1)))
    with pytest.raises(A.AlignError) as exc:                       # 1200 s at 8 kHz
        A.align(huge, RATE, CRUSHED)
    assert exc.value.code == "too_long"
    assert str(A.MAX_LISTEN_SAMPLES) in str(exc.value)
    assert "Hz" in str(exc.value), "the caller needs to know resampling is the fix"

    # The same law, read from the container: refused by name, not by crash.
    with pytest.raises(A.AlignError) as exc:
        A.pcm_from_wav(wav_bytes(long_tape(906)))
    assert exc.value.code == "too_long"


def test_an_over_long_tape_is_refused_before_it_is_decoded():
    """The header may claim more tape than the file holds — a stranger's upload
    does that by accident more often than on purpose. `wave` reports the claimed
    frame count, and the window must be checked against *that* number before a
    single byte is read into an array, or the ceiling protects nothing."""
    data = bytearray(wav_bytes(tape(room(0.5))))
    at = data.find(b"data")
    claimed = int(A.MAX_LISTEN_SEC + 10) * RATE * 2       # bytes in the data chunk
    data[at + 4:at + 8] = bytes(claimed.to_bytes(4, "little"))
    started = time.perf_counter()
    with pytest.raises(A.AlignError) as exc:
        A.pcm_from_wav(bytes(data))
    took = time.perf_counter() - started
    assert exc.value.code == "too_long"
    assert took < 0.5, f"refused after decoding {claimed} bytes, not before"


def test_the_envelope_reads_every_sample_of_every_frame():
    """Decimation is the obvious next optimisation and the one that must not be
    slipped in quietly: a thinned envelope estimates a frame's loudness, and an
    estimate that misses an inter-word pause moves a cut inside a word — the exact
    failure this engine was built to prevent. The pass costs 0.6 s over fifteen
    minutes, so there is nothing for thinning to buy here.

    A source gate, not a numeric one: numbers can be reproduced by a lucky
    estimate, a third slice index cannot."""
    src = Path(A.__file__).read_text(encoding="utf-8")
    body = src[src.index("def frame_levels"):src.index("def _percentile")]
    assert body.count("pcm[") == 1 and "pcm[a:a + frame_len]" in body, \
        "the frame is no longer read whole"
    assert "stride" not in body.lower(), "a decimation knob appeared"
    assert "def frame_levels(pcm, frame_len: int)" in body, \
        "frame_curve grew a way to hear less"
    assert "def frame_curve(pcm, rate: int)" in src
    assert A.MAX_LISTEN_SEC == 900.0 and A.MAX_LISTEN_SAMPLES > A.MAX_LISTEN_SEC * 10_000


def test_both_listeners_hear_the_same_window():
    """One law, one function: if `align` and `words` carried separate ceilings,
    a job could get cue edges for a tape its word timings were refused for."""
    from app import pipeline
    from app.ling import word as W
    assert W.check_listen_window is A.check_listen_window
    with pytest.raises(W.WordError) as exc:
        W.words(long_tape(906), RATE, block_cues(906))
    assert exc.value.code == "too_long"


def test_the_pipeline_listens_to_the_money_and_not_to_a_window():
    """Round 19 moved the ceiling from 200 s to 900 s and called it the engine's
    window; Round 25 removed the ceiling as a limit at all: what bounds the
    listening is what the job paid for, and only an hour of tape stays as a stop.

    The margin law survives in a different unit. `ffmpeg` writes whole blocks, so
    decoding exactly to the boundary can hand the listener a frame more than it
    agreed to read — harmless here, because nothing is refused any more; what must
    not drift is the *promise*: paid seconds, not internal slack, decide whether the
    job says "not all of it was heard"."""
    from app import pipeline
    want, paid = pipeline._heard_budget(20.0)
    assert (paid, want) == (1200.0, 1260.0), "paid timeline plus the slack, not the tape"
    assert pipeline._heard_budget(0.0) == (66.0, 6.0)
    # An hour bought: the stop is the job ceiling, and the ceiling is a memory
    # decision, not the engine refusing to listen.
    assert pipeline._heard_budget(120.0) == (pipeline.MAX_JOB_TAPE_SEC, 7200.0)
    assert pipeline.MAX_JOB_TAPE_SEC > A.MAX_LISTEN_SEC, \
        "the ceiling is still the engine's window wearing a new number"
    assert pipeline.LISTEN_BLOCK_SEC <= A.BLOCK_SEC_MAX, \
        "a block the envelope would refuse is a job that dies mid-tape"
    # The honesty note keys off the ceiling, and only when paid time was lost.
    trunc, note = pipeline._heard_report(3600.0, 3600.0, 7200.0)
    assert trunc == {"heard_sec": 3600, "paid_sec": 7200, "window_sec": 3600}, trunc
    assert "heard 3600s of 7200s" in note, note
    assert pipeline._heard_report(600.0, 660.0, 1200.0) == ({}, ""), \
        "a tape that simply ran out is not a lost promise"


def test_a_frame_is_measured_from_every_sample_of_it():
    """The source gate below forbids a second index in `frame_levels`. This is the
    check the gate cannot see: reading the frame through a strided *view* of the
    same slice keeps the slice, keeps the variable name and keeps the word
    "stride" out of the file — and loses every pause that falls on the samples
    nobody looked at. So: compare the number against an independent full sum.
    """
    pcm = tape(voice(0.4, amp=1.0))
    want = math.sqrt(sum(x * x for x in pcm[:160]) / 160) / A.FULL_SCALE
    got = A.frame_levels(pcm, 160)[0]
    assert abs(got - want) < 1e-12, (got, want)

    # Loud on even samples only: every 2nd sample alone reads this frame as
    # silence, and a silent frame is a gap the aligner would happily cut into.
    even = array("h", [0, 30_000] * 80)
    assert A.frame_levels(even, 160) == [pytest.approx(30_000 / math.sqrt(2) / A.FULL_SCALE,
                                                       rel=1e-6)], \
        "the envelope is thinner than the frame it measures"


def test_an_8_bit_tape_is_scaled_sample_for_sample():
    """The 8-bit branch was rewritten to a generator to stop materialising a list
    of five million pointers; the arithmetic must not move by one unit."""
    src = interview()
    data = wav_bytes(src, width=1)
    pcm, rate = A.pcm_from_wav(data)
    body = data[data.index(b"data") + 8:]
    assert rate == RATE and len(pcm) == len(src)
    assert list(pcm) == [(b - 128) * 256 for b in body]
    assert all(v % 256 == 0 for v in pcm), "not an 8-bit scale"


def test_a_job_event_says_what_was_heard_when_the_ceiling_ended_first(monkeypatch):
    """`_heard_report` returns the sentence; only the steps can *say* it. A test on
    the helper alone stays green when someone drops `{note}` from both f-strings,
    which is precisely the silent loss this release was written against.

    The ceiling is shrunk for the test rather than the tape grown to an hour: the
    numbers the event carries are produced by the same code either way, and a
    58 MB fixture would test memory, not honesty."""
    from app import db, pipeline
    from audio_studio import listening
    monkeypatch.setattr(pipeline, "MAX_JOB_TAPE_SEC", 120.0)
    uid = db.create_user("N", "+99890n00001")["id"]
    job = db.create_job(uid, "subtitles", "uz", "ru", 20.0, "x.srt", {})
    segs = [pipeline.Segment(c.start, c.end, c.text) for c in block_cues(120)]
    tape = listening(long_tape(900), segs, 20.0)
    assert tape["heard_sec"] == 120.0 and tape["blocks"] == 2, tape["heard_sec"]
    out = pipeline._align_step(job["id"], tape, segs)
    line = [e for e in db.job_timeline(job["id"]) if e["step"] == "align"][-1]
    assert "heard 120s of 1200s" in line["message"], line
    assert len(out) == len(segs)
    # The same facts as data: the card renders these, not the English sentence.
    data = line["data"]
    assert data["code"] == "aligned" and data["moved"] == data["boundaries"] == 40, data
    assert data["max_applied"] == 0.18 and data["duration"] == 120.0, data
    assert data["truncation"] == {"heard_sec": 120, "paid_sec": 1200,
                                  "window_sec": 120}, data
    pipeline._words_step(job["id"], tape, block_cues(120))
    wline = [e for e in db.job_timeline(job["id"]) if e["step"] == "words"][-1]
    assert "heard 120s of 1200s" in wline["message"], wline
    assert wline["data"]["code"] == "timed" and wline["data"]["words"] == 40, wline["data"]
    assert wline["data"]["cues"] == wline["data"]["cues_measured"] == 20, wline["data"]
    assert wline["data"]["valley_share"] == 1.0, wline["data"]
    assert "truncation" in wline["data"], wline["data"]


def test_a_refused_step_says_why_in_a_token_not_in_prose(client, auth):
    """`reason` is the stable half of a refusal: the client maps it to a localised
    sentence, so `exc.code` must arrive untouched and `skipped` must always be the
    code — a UI that guesses from the message breaks on the next rewording."""
    from app import db
    job = _submit(client, auth, align="1")
    line = [e for e in db.job_timeline(job["id"]) if e["step"] == "align"][-1]
    assert line["data"] == {"code": "skipped", "reason": "no_audio"}, line


def test_the_listening_ceiling_sits_above_the_upload_ceiling():
    """Two numbers in two files promise a client one thing, and the promise is only
    true while the order holds: a job may never be refused material it paid for.

    If `MAX_JOB_TAPE_SEC` ever drops below the upload's own timeline cap, the
    truncation sentence stops being a corner case for lying containers and becomes a
    ordinary tax on paid minutes — which is the defect this release removed. This
    gate is the difference between documenting that and noticing it."""
    from app import main, pipeline
    assert pipeline.MAX_JOB_TAPE_SEC >= main.MAX_JOB_TIMELINE_SEC, (
        f"jobs can be bought past the listening ceiling: "
        f"{pipeline.MAX_JOB_TAPE_SEC} < {main.MAX_JOB_TIMELINE_SEC}")
    # And the sentence itself: it fires only when the ceiling ate paid time, never
    # when the tape simply ended before the credit did.
    assert pipeline._heard_report(3600.0, 3600.0, 7200.0)[0] != {}, "ceiling bound"
    assert pipeline._heard_report(1800.0, 1860.0, 1800.0) == ({}, ""), \
        "a tape that ends inside its own paid timeline is not a loss"


def test_a_short_job_promises_nothing_it_did_not_run_out_of(client, auth, monkeypatch):
    """The note is a claim about a limit, so it must be absent whenever the paid
    timeline — not the ceiling — is what stopped the listening."""
    from app import db, pipeline
    from audio_studio import listening
    uid = db.create_user("S", "+99890s00002")["id"]
    job = db.create_job(uid, "subtitles", "uz", "ru", 1.0, "x.srt", {})
    tape = listening(long_tape(60), block_cues(60), 1.0)
    assert tape["truncation"] == {} and tape["note"] == "", tape
    pipeline._align_step(job["id"], tape, block_cues(60))
    line = [e for e in db.job_timeline(job["id"]) if e["step"] == "align"][-1]
    assert "heard" not in line["message"], line


def test_the_listeners_compute_off_the_event_loop():
    """Both routes are `async def` and both engines are pure Python: called
    directly, one five-megabyte tape freezes /healthz, the WebSocket hub and every
    API route for as long as it reads frames. The guard is textual because the
    failure is invisible to a functional test — TestClient serialises everything,
    so a blocked event loop cannot be observed from inside the loop."""
    src = (Path(A.__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    # No trailing "(": the offload passes the engine as a callable, not a call.
    engines = ("align_mod.pcm_from_wav", "word_mod.pcm_from_wav",
               "align_mod.align", "word_mod.words")
    for name in ("async def ling_align(", "async def ling_words("):
        rest = src[src.index(name) + len(name):]
        body = rest[:rest.index("\n@app.")]
        calls = [line.strip() for line in body.splitlines()
                 if any(c in line for c in engines)]
        assert calls, f"{name} no longer calls an engine: update this gate"
        for line in calls:
            assert "run_in_threadpool(" in line, \
                f"{name} computes on the event loop: {line}"
    assert "_ling_listener()" in src and "_LING_LISTENERS.release()" in src, \
        "the listeners need a bound as well as a threadpool"


def test_a_third_tape_is_asked_to_wait_instead_of_queued(client):
    """Allowed is not the same as affordable: both engines are pure Python, so an
    unbounded number of permitted requests is an unbounded number of busy
    threads. The answer to the third is a 429 that names the retry, not a queue
    the client can only experience as a hang."""
    from app import main as m
    held = [m._LING_LISTENERS.acquire(blocking=False) for _ in range(2)]
    assert all(held), "the test needs both listener slots free"
    try:
        rep = _align(client, wav_bytes(interview()))
    finally:
        for _ in held:
            m._LING_LISTENERS.release()
    assert rep.status_code == 429, rep.text
    assert rep.headers.get("retry-after") == "5", rep.headers
    assert "two tapes" in rep.json()["detail"], rep.text
    # the slots came back: the very next request is served
    assert _align(client, wav_bytes(interview())).status_code == 200


def test_the_listening_routes_are_a_tighter_cost_class_than_the_text_ones():
    """12 reads a minute, 60 keystrokes a minute: first match wins in the table,
    so the listening lines above `/api/v1/ling` are load-bearing."""
    from app.main import _endpoint_limit
    assert _endpoint_limit("/api/v1/ling/align") == 12
    assert _endpoint_limit("/api/v1/ling/words") == 12
    assert _endpoint_limit("/api/v1/ling/diarize") == 60
    assert _endpoint_limit("/api/v1/ling/convert") == 60


# ─── reading a container ──────────────────────────────────────────────────────

def test_a_plain_16_bit_wav_round_trips_into_pcm():
    src = interview()
    pcm, rate = A.pcm_from_wav(wav_bytes(src))
    assert rate == RATE and len(pcm) == len(src)
    assert list(pcm[:8]) == list(src[:8])


def test_stereo_is_folded_to_mono_without_losing_the_pauses():
    src = interview()
    pcm, rate = A.pcm_from_wav(wav_bytes(src, channels=2))
    assert rate == RATE and len(pcm) == len(src)
    mono = A.detect_gaps(src, RATE)
    folded = A.detect_gaps(pcm, rate)
    assert [round(g.start, 1) for g in folded["gaps"]] == \
           [round(g.start, 1) for g in mono["gaps"]]


def test_8_bit_tape_is_read_at_the_same_scale_as_16_bit():
    src = interview()
    pcm, rate = A.pcm_from_wav(wav_bytes(src, width=1))
    read = A.detect_gaps(pcm, rate)
    assert read["speech_runs"] == 3 and len(read["gaps"]) == 2


def test_a_file_we_cannot_honestly_decode_is_refused_by_name():
    """No fallback to "assume PCM": a compressed or float stream read as integers
    produces an energy curve that looks plausible and is meaningless. Every case
    names the reason, because "bad file" is not something a client can act on."""
    with pytest.raises(A.AlignError) as exc:
        A.pcm_from_wav(b"not a wav at all, just noise bytes" * 4)
    assert exc.value.code == "not_wav"
    with pytest.raises(A.AlignError) as exc:
        A.pcm_from_wav(b"RIFF")
    assert exc.value.code == "not_wav"
    good = wav_bytes(interview())
    for fmt, width, code, needle in (
            (6, 8, "compressed", "format tag"),        # ITU G.711 a-law
            (3, 32, "compressed", "format tag"),       # IEEE float
            (0xFFFE, 16, "compressed", "format tag"),  # WAVE_FORMAT_EXTENSIBLE
            (1, 32, "bad_depth", "32-bit"),
            (1, 24, "bad_depth", "24-bit")):
        with pytest.raises(A.AlignError) as exc:
            A.pcm_from_wav(rewrite_header(good, fmt=fmt, width=width))
        assert exc.value.code == code, (fmt, width, exc.value.code)
        assert needle in str(exc.value)
    with pytest.raises(A.AlignError) as exc:                # header lies about itself
        A.pcm_from_wav(rewrite_header(good, channels=2))
    assert exc.value.code == "bad_header"
    with pytest.raises(A.AlignError) as exc:                # honest byte cap
        A.pcm_from_wav(good, max_bytes=100)
    assert exc.value.code == "too_long"


def test_a_header_that_disagrees_with_the_decoder_is_refused():
    """The bug this test exists for: `wave` derives the sample width from fields it
    picks itself, and a 32-bit PCM header was reported to us as `sampwidth=1`. A
    reader that trusts one source of truth reads someone else's data as 8-bit and
    produces a confident, meaningless pause map. Both are read, and a disagreement
    is a refusal."""
    good = wav_bytes(interview())
    lied = rewrite_header(good, width=32)
    with pytest.raises(A.AlignError) as exc:
        A.pcm_from_wav(lied)
    assert exc.value.code == "bad_depth"
    # …and the file that *is* what it says still reads, so this is not a blanket no.
    pcm, rate = A.pcm_from_wav(good)
    assert rate == RATE and len(pcm) == len(interview())


def test_the_end_to_end_answer_is_a_usable_srt_document():
    """The deliverable: valid SRT, same words, and the timecodes are the aligned
    ones — a customer should be able to drop it in a player without reading our
    JSON at all."""
    report = A.align(interview(), RATE, CRUSHED)
    from app.ling.srt import parse_srt
    parsed = parse_srt(report["srt"])
    assert [p.text for p in parsed] == [c.text for c in CRUSHED]
    assert [(p.start, p.end) for p in parsed] == \
           [(c["start"], c["end"]) for c in report["cues"]]
    assert report["srt"] == format_srt(
        [Cue(c["i"], c["start"], c["end"], c["text"]) for c in report["cues"]])


# ─── the public endpoint ──────────────────────────────────────────────────────

LINES = [{"start": c.start, "end": c.end, "text": c.text} for c in CRUSHED]


def long_take(repeats: int = 4) -> bytes:
    """A recording whose *body* is bigger than the JSON budget the other language
    endpoints live on: 20 s at 8 kHz/16-bit is ~320 KB. It exists to prove the
    ceiling is chosen per path — an aligner that refuses its own legitimate request
    is worse than one that never runs."""
    return wav_bytes(tape(*[interview() for _ in range(repeats)]))


def _multipart(boundary: str, fields: dict, blob: bytes, filename: str = "take.wav") -> bytes:
    """A real multipart body, written by hand: needed to control the framing headers
    that `files=` in the test client always sets for us."""
    out = bytearray()
    for key, val in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
                f"{val}\r\n").encode("utf-8")
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; "
            f"filename=\"{filename}\"\r\nContent-Type: audio/wav\r\n\r\n").encode("utf-8")
    out += blob + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return bytes(out)


def _align(client, audio, lines=LINES, srt=None, max_shift=None):
    data = {}
    if lines is not None:
        data["lines"] = json.dumps(lines)
    if srt is not None:
        data["srt"] = srt
    if max_shift is not None:
        data["max_shift"] = max_shift
    return client.post("/api/v1/ling/align",
                       files={"audio": ("take.wav", audio, "audio/wav")},
                       data=data)


def test_the_endpoint_answers_with_the_report_the_engine_produces(client):
    """The public route must not be a softer copy of the engine: same bytes, same
    cues, same numbers — otherwise the demo sells an answer the product gives."""
    audio = wav_bytes(interview())
    rep = _align(client, audio)
    assert rep.status_code == 200, rep.text
    body = rep.json()
    direct = A.align(*A.pcm_from_wav(audio), CRUSHED)
    assert body["engine"] == "ovoz-jimlik"
    assert body["cues"] == direct["cues"]
    assert body["summary"] == direct["summary"]
    assert body["summary"]["words_preserved"] is True
    assert body["summary"]["never_worse"] is True


def test_an_srt_document_is_accepted_as_well_as_a_line_array(client):
    audio = wav_bytes(interview())
    by_doc = _align(client, audio, lines=None, srt=format_srt(CRUSHED))
    assert by_doc.status_code == 200, by_doc.text
    assert by_doc.json()["cues"] == _align(client, audio).json()["cues"]


def test_a_recording_bigger_than_the_json_budget_still_gets_an_answer(client):
    """Path-conditional ceilings are the whole point of the split: the language
    endpoints share a 130 KB body budget because they carry text, and this one
    carries a tape. Same bytes, two different verdicts."""
    audio = long_take()
    assert len(audio) > 130_000
    ok = _align(client, audio)
    assert ok.status_code == 200, ok.text
    assert ok.json()["audio"]["duration"] > 15.0
    same_size = client.post("/api/v1/ling/analyze",
                            json={"text": "a" * (len(audio) + 10)})
    assert same_size.status_code == 413, same_size.status_code


def test_a_tape_past_the_old_sample_ceiling_is_answered_over_http(client):
    """Four minutes of recording through the public route. This used to be a 413
    from the engine (`1.6 M samples`), even though the transport happily accepted
    the upload — a ceiling in the wrong unit, discovered by comparing the two
    numbers instead of reasoning about them."""
    pcm = long_tape(246)
    audio = wav_bytes(pcm)
    assert len(pcm) > 1_600_000 and 1_900_000 < len(audio) < 5_000_000
    lines = [{"start": c.start, "end": c.end, "text": c.text}
             for c in block_cues(246)]
    rep = _align(client, audio, lines=lines)
    assert rep.status_code == 200, rep.text
    body = rep.json()
    assert body["audio"]["duration"] == 246.0
    assert body["summary"]["moved"] == 2 * len(lines)


def test_an_upload_past_the_byte_cap_is_told_what_it_can_send(client):
    """The cap protects the server, the message has to help the caller: bytes, the
    seconds those bytes buy at the rate the engines ask for, and the direction to
    go with a longer tape."""
    audio = wav_bytes(long_tape(324))
    assert len(audio) > 5_000_000
    rep = _align(client, audio)
    assert rep.status_code == 413, rep.text
    detail = rep.json()["detail"]
    assert "5000000" in detail and "312s" in detail, detail
    assert "8 kHz" in detail and "job" in detail, detail


def test_tuning_the_move_is_a_request_not_an_override(client):
    audio = wav_bytes(interview())
    tight = _align(client, audio, max_shift="0.05").json()
    wide = _align(client, audio, max_shift="2.0").json()
    assert tight["tuning"]["max_shift"] == 0.05
    assert wide["tuning"]["max_shift"] == 2.0
    # A shorter leash cannot move more boundaries than a longer one.
    assert tight["summary"]["max_applied"] <= wide["summary"]["max_applied"] + 1e-9


def _unhearable():
    """Files a real camera/phone/DSP hands you that this engine must not decode.
    Named cases, because a parametrise over raw bytes turns the WAV itself into the
    test id and floods every failure report."""
    good = wav_bytes(interview())
    return {
        "garbage": (b"not a wav at all", "RIFF"),
        "alaw": (rewrite_header(good, fmt=6), "format tag"),      # ITU G.711
        "float": (rewrite_header(good, fmt=3), "format tag"),      # IEEE float
        "32bit": (rewrite_header(good, width=32), "32-bit"),
        "lying-channels": (rewrite_header(good, channels=2), "block alignment"),
    }


@pytest.mark.parametrize("case", ["garbage", "alaw", "float", "32bit",
                                  "lying-channels"])
def test_audio_that_cannot_be_honest_input_is_refused_by_media_type(client, case):
    bad, needle = _unhearable()[case]
    r = _align(client, bad)
    assert r.status_code == 415, case
    detail = r.json()["detail"]
    assert needle in detail, (case, detail[:200])
    # a refusal that names the format is the product: the caller must know what
    # to send back, not that our decoder had a bad afternoon
    assert "Traceback" not in detail and "wave" not in detail
    # …and the machine-readable line agrees: `internal_error` on a 415 tells an
    # SDK "our fault, retry" about the caller's own file.
    assert r.json()["error_code"] == "unsupported_media", r.json()


def test_every_aligner_refusal_leaves_the_error_model_a_client_can_match():
    """The aligner answers with four statuses; the unified error model maps status
    to code in one table. A code that falls through to `internal_error` is marked
    retryable in spirit — the worst possible advice for "send 16-bit PCM"."""
    from app.errors import ErrorCode
    from app.main import _ALIGN_REFUSAL_STATUS

    named = {400, 413, 415, 422}      # statuses the model can blame on the caller
    unmapped = sorted(set(_ALIGN_REFUSAL_STATUS.values()) - named - {500})
    assert not unmapped, f"statuses the aligner can emit but the model cannot name: {unmapped}"
    assert ErrorCode.UNSUPPORTED_MEDIA.value == "unsupported_media"


def test_max_shift_outside_the_contract_is_refused(client):
    """Unparseable is one bug, out-of-range is another, and both arrive before the
    envelope is measured: a leash of 99 s is not alignment, it is rewriting sync."""
    audio = wav_bytes(interview())
    assert _align(client, audio, max_shift="soon").status_code == 422
    assert _align(client, audio, max_shift="0").status_code == 422
    too_big = _align(client, audio, max_shift="99")
    assert too_big.status_code == 422
    assert "max_shift" in too_big.json()["detail"]


def test_the_cue_side_is_validated_before_the_upload_is_buffered(client):
    """Order matters on a multipart call: five megabytes must not be paid for to
    discover the subtitles were missing."""
    audio = wav_bytes(interview())
    assert _align(client, audio, lines=None, srt=None).status_code == 422
    both = _align(client, audio, lines=LINES, srt=format_srt(CRUSHED))
    assert both.status_code == 422 and "exactly one" in both.json()["detail"]
    r = client.post("/api/v1/ling/align",
                    files={"audio": ("take.wav", audio, "audio/wav")},
                    data={"lines": "not json"})
    assert r.status_code == 422 and "JSON" in r.json()["detail"]


def test_a_body_over_the_aligner_ceiling_is_refused_structurally(client):
    """The middleware answers this one, before FastAPI touches the multipart
    envelope — so the reply must still carry the error_code every refusal here
    promises, and close the connection instead of keep-aliving a body on the wire.
    The extra field is the cheap way to cross a six-megabyte line: the ceiling is
    about bytes on the wire, not about what the bytes claim to be."""
    from app.main import LING_ALIGN_MAX_BODY_BYTES as CAP
    r = client.post("/api/v1/ling/align",
                    files={"audio": ("take.wav", wav_bytes(interview()), "audio/wav")},
                    data={"lines": json.dumps(LINES), "pad": "x" * (CAP + 10_000)})
    assert r.status_code == 413, r.text
    assert r.json()["error_code"], r.text
    assert str(CAP) in r.json()["detail"]


def test_an_upload_that_declares_no_length_is_refused_before_it_lands(client):
    """Every byte ceiling on these routes is keyed on `Content-Length`, and a
    chunked request has none — so without this rule Starlette finishes spooling the
    body to the temp directory before anything can say "too large". An iterator body
    is how httpx emits `Transfer-Encoding: chunked`, which is what makes this test
    about the header and not about the size."""
    boundary = "----ovozqachunk"
    fields = {"lines": json.dumps(LINES)}
    body = _multipart(boundary, fields, wav_bytes(interview()))
    parts = iter([body[i:i + 2048] for i in range(0, len(body), 2048)])
    r = client.post("/api/v1/ling/align", content=parts,
                    headers={"content-type": f"multipart/form-data; boundary={boundary}"})
    assert r.status_code == 411, r.text
    assert r.json()["error_code"] == "length_required", r.text
    assert "Content-Length" in r.json()["detail"], r.text
    # the paid upload path is under the same rule, and the refusal precedes auth
    j = client.post("/api/jobs", content=iter([b"x" * 64]),
                    headers={"content-type": f"multipart/form-data; boundary={boundary}"})
    assert j.status_code == 411, j.text
    # an honest length on the same body is served normally
    assert _align(client, wav_bytes(interview())).status_code == 200


def test_the_aligner_shares_the_language_kill_switch(client, monkeypatch):
    from app import main as m
    monkeypatch.setattr(m, "_flags_all",
                        lambda: {"uzbek_language_engine": {"enabled": False}})
    assert _align(client, wav_bytes(interview())).status_code == 404


def test_the_report_survives_json_with_no_infinities(client):
    """A duration of 0.0 or a shift divided by nothing would make json emit
    `Infinity`, which no client parser accepts and no test that only reads Python
    objects would ever notice."""
    raw = _align(client, wav_bytes(interview())).text
    assert "Infinity" not in raw and "NaN" not in raw


def test_the_aligner_is_advertised_where_sdks_look(client):
    body = client.get("/api/v1/info").json()
    assert "silence_gap_alignment" in body["features"], body["features"]


# ─── the job pipeline ─────────────────────────────────────────────────────────

def test_the_listening_stops_where_the_money_stops_and_not_a_frame_later():
    """`_drain` reads the tape in blocks and never past the ceiling it declared.

    An unbounded read is how a ten-hour upload turns one paid job into a
    machine-wide outage; a ceiling that also cuts paid material is the silent loss
    Round 19 named. Both directions get asserted, because a test that only checks
    one of them passes when someone "fixes" the other."""
    from app import pipeline
    from audio_studio import listening
    raw = long_tape(900).tobytes()
    got: list[int] = []
    pos = 0

    def read(nbytes: int) -> bytes:
        nonlocal pos
        out = raw[pos:pos + nbytes]
        got.append(len(out))
        pos += len(out)
        return out

    tape = pipeline._drain(read, 5.0)                       # five minutes bought
    assert tape["heard_sec"] == 360.0, tape["heard_sec"]    # paid plus the slack
    assert tape["blocks"] == 360 / pipeline.LISTEN_BLOCK_SEC, tape["blocks"]
    assert sum(got) / 2 / RATE <= 360.0 + 1e-9, "the pass read past its ceiling"
    assert tape["truncation"] == {}, "a tape longer than the paid timeline is not a loss"
    # A ceiling that is not a multiple of the block is where the last read used to
    # overshoot: the request must be cut to the remaining budget, or `heard_sec`
    # contradicts `reach_sec` by up to a whole block (59 s) — and the truncation
    # sentence below rests on that number.
    got.clear()
    pos = 0
    edge = pipeline._drain(read, 3.0)                       # 180 + 60 slack = 240 s
    assert edge["heard_sec"] == edge["reach_sec"] == 240.0, edge
    assert sum(got) / 2 / RATE == 240.0, "the tail read went past the budget"
    # A tape shorter than the credit was heard whole: silence is honest here.
    short = listening(long_tape(120), block_cues(120), 20.0)
    assert short["heard_sec"] == 120.0 and short["truncation"] == {}, short


def _submit(client, auth, align=None, jtype="subtitles"):
    data = {"jtype": jtype, "src": "uz", "tgt": "ru"}
    if align is not None:
        data["align"] = align
    r = client.post("/api/jobs", headers=auth,
                    files={"file": ("interview.srt", format_srt(CRUSHED).encode(),
                                    "text/plain")},
                    data=data)
    assert r.status_code == 201, r.text
    return r.json()["job"]


@pytest.mark.parametrize("flag", ["1", "true", "on", "yes", "TRUE"])
def test_the_align_form_flag_reaches_the_job_and_asks_for_hearing(client, auth, flag):
    from app import db
    job = _submit(client, auth, align=flag)
    assert db.get_job(job["id"])["meta"]["align"] is True
    steps = {e["step"] for e in db.job_timeline(job["id"])}
    assert "align" in steps, steps


@pytest.mark.parametrize("flag", ["", "0", "off", "no", "false"])
def test_a_falsy_align_flag_stays_off(client, auth, flag):
    from app import db
    job = _submit(client, auth, align=flag)
    assert db.get_job(job["id"])["meta"].get("align") is None
    assert "align" not in job["artifacts"]
    steps = {e["step"] for e in db.job_timeline(job["id"])}
    assert "align" not in steps, steps


def test_a_tape_that_is_not_there_is_reported_instead_of_guessed(client, auth):
    """A pasted document has no audio. The step cannot run, and the one thing it
    must never do is stay quiet: the user asked for alignment and is owed the
    reason it did not happen — and the subtitles must still arrive."""
    from app import db
    job = _submit(client, auth, align="1")
    line = [e for e in db.job_timeline(job["id"]) if e["step"] == "align"][-1]
    assert "no audio track" in line["message"], line
    assert job["status"] == "done", job
    assert "align" not in job["artifacts"]
    assert client.get(job["artifacts"]["srt"], headers=auth).status_code == 200


def test_the_align_step_sits_between_recognition_and_everything_timed(client):
    """Ordering is the feature: diarization windows, translated cues, the layout
    audit and the dub all inherit the corrected timeline. Put it after them and
    each one describes timings that were about to change."""
    from app.main import _STEP_PCT
    assert _STEP_PCT["asr"] < _STEP_PCT["align"] < _STEP_PCT["diarize"] \
        < _STEP_PCT["translate"] < _STEP_PCT["polish"] < _STEP_PCT["tts"]


def test_a_text_job_without_the_flag_is_never_retimed(client, auth):
    """Silently moving a customer's timecodes is the one change a setting cannot
    undo."""
    from app import db
    job = _submit(client, auth)
    assert {e["step"] for e in db.job_timeline(job["id"])}.isdisjoint({"align"})


def test_one_curve_per_job_and_profiles_after_the_aligner(client, auth, monkeypatch):
    """Align, So'z and the diarizer all hear the same audio; the curve is measured
    once, and the voice profiles are collected from the timeline the aligner
    finished writing.

    Decoding per feature is not merely slower, it is two windows over the same job
    that can disagree — and the second disagreement is real: a profile window taken
    from the ASR timeline sits partly outside the cue Jimlik has already moved,
    which blends two speakers into one fingerprint and lets the diarizer argue from
    evidence the aligner cancelled. So the pass is bounded, not rewound: one decode
    for the curve, one for the profiles, and never a buffer a step keeps for itself.
    """
    from app import db, pipeline as pl
    from audio_studio import listening

    heard = listening(long_tape(60), block_cues(60), 1.0)
    assert heard is not None and "curve" in heard, heard
    curves, profiles = [], []

    # A text upload has no audio, so the real pass would answer None and the
    # diarizer would correctly take the text-only branch. The curve is therefore
    # supplied — measured by the production `_drain`, just not through ffmpeg.
    def fake_curve(source, billed, deadline=0.0, **kw):
        curves.append(Path(source).name)
        return dict(heard)

    def counting_profiles(source, segments, billed, deadline=0.0):
        profiles.append([round(s.start, 3) for s in segments])
        return {"voices": [None] * len(segments), "off_tape": 0,
                "heard_sec": heard["heard_sec"], "note": "", "truncation": {}}

    monkeypatch.setattr(pl, "_tape_pass", fake_curve)
    monkeypatch.setattr(pl, "_profiles_pass", counting_profiles)
    r = client.post("/api/jobs", headers=auth,
                    files={"file": ("interview.srt", format_srt(CRUSHED).encode(),
                                    "text/plain")},
                    data={"jtype": "subtitles", "src": "uz", "tgt": "ru",
                          "align": "1", "diarize": "1", "words": "1"})
    assert r.status_code == 201, r.text
    job = r.json()["job"]
    steps = {e["step"] for e in db.job_timeline(job["id"])}
    assert {"align", "diarize", "words"} <= steps, steps
    assert len(curves) == 1, f"the curve was measured {len(curves)} times"
    assert len(profiles) == 1, f"profiles collected {len(profiles)} times"
    # The profiles were asked for the ALIGNED timeline, not the raw ASR one.
    timeline = db.job_timeline(job["id"])
    aligned = [e for e in timeline if e["step"] == "align"][-1]
    assert aligned["data"]["code"] in ("aligned", "skipped"), aligned["data"]
    assert profiles[0], "the diarizer got no cue windows at all"


def test_the_tape_is_never_held_by_a_step():
    """The rule for every step that has not been written yet: the tape reaches the
    engines as the pass `_execute` already ran, never as a fresh subprocess."""
    import inspect

    from app import pipeline as pl

    body = inspect.getsource(pl._execute)
    assert body.count("_tape_pass(") == 1, \
        "_execute must measure one curve, through _tape_pass, and pass it on"
    assert "_profiles_pass(" in body, "diarization must hear the aligned timeline"
    for fn in (pl._execute, pl._align_step, pl._words_step):
        step = inspect.getsource(fn)
        for side in ("_pcm(", "Popen", "subprocess."):
            assert side not in step, f"{fn.__name__} decodes the tape on the side: {side}"


# ─── static gates: there is no JS test runner, so these are the tests ─────────

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def _html():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _js():
    return (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def test_the_jimlik_section_is_rendered_into_a_live_region():
    html = _html()
    assert 'id="jimlik-metrics" aria-live="polite" aria-atomic="true"' in html
    assert 'id="jimlik"' in html and 'id="jimlik-rows"' in html
    assert 'id="align-row"' in html and 'id="j-align"' in html


def test_the_new_markup_carries_no_inline_style_or_handler():
    """CSP has no 'unsafe-inline' any more: one on* attribute in the bundle is a
    404-shaped bug that only appears when the page is actually opened."""
    html = _html()
    section = html[html.index('id="jimlik"'):html.index('<section class="caps"')]
    assert "style=" not in section and "onclick=" not in section
    assert "jimlik_aria" in section


def test_the_widget_sends_a_real_recording_and_relocalises_without_a_refetch():
    js = _js()
    assert '"/api/v1/ling/align"' in js
    assert "window.__jimlikRepaint" in js
    assert "() => window.__jimlikRepaint?.()," in js, \
        "the Jimlik widget is not in the locale-switch painter list"
    assert 'fd.append("align", "1")' in js
    assert '$("#align-row").classList.toggle("hidden", docMode)' in js
    # The tape is synthesised in the browser: no embedded recording, no data URI.
    assert "function wavMono16" in js
    widget = js[js.index("// ─── Ovoz Jimlik demo"):js.index("// ─── auth ───")]
    assert "atob" not in widget and "data:audio" not in widget


def test_a_pasted_document_unchecks_align_instead_of_ignoring_it():
    js = _js()
    toggle = js[js.index('$("#align-row").classList.toggle'):]
    assert '$("#j-align").checked = false;' in toggle[:200]


def test_the_classes_the_widget_writes_are_the_classes_the_stylesheet_styles():
    """Two files written apart: a class the JS emits and the CSS never mentions
    ships unreadable at exactly one screen width, and no test that renders nothing
    will ever see it. Both directions are checked."""
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    js = _js()
    widget = js[js.index("(function jimlikDemo"):js.index("// ─── auth ───")]
    emitted = set(re.findall(r'class=\\?"([a-z][a-z0-9- ]*)', widget))
    assert "j-new" in " ".join(emitted), "the aligned timing lost its emphasis class"
    for cls in sorted({c for group in emitted for c in group.split()}):
        assert f".{cls}" in css, f"widget writes .{cls}, stylesheet never styles it"
    for cls in ("jimlik-rows", "jimlik-demo", "j-new"):
        assert f".{cls}" in css, f".{cls} is styled for nothing"
        # A class may be introduced by the widget at runtime rather than sit in the
        # static shell — `j-new` marks an answer that only exists after a POST, so
        # demanding it in index.html would ask for markup the page cannot have yet.
        assert cls in _html() or cls in js, \
            f".{cls} is styled but nothing ever writes it"


def test_the_widget_widens_its_columns_only_where_widening_cannot_overflow():
    """The stylesheet declares a page-wide collapse (`@media (max-width: 840px)` →
    one column). A demo that asks for two columns with a plain `.a .b { … }` rule
    out-specifies that collapse — same media, two class names beat one — and the
    side column then hangs past the viewport on a phone. Live QA measured exactly
    this: `scrollWidth 661 > innerWidth 643`. A multi-column rule for a selector
    that the collapse also names is therefore only legal inside a `min-width` query
    that starts where the collapse stops."""
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    # A comment is not a selector: its periods would be counted as class names and
    # its braces would be read as blocks.
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    collapse = re.search(r"@media\s*\(max-width:\s*(\d+)px\)[^{]*\{[^}]*"
                        r"\.qator-wrap\s*\{[^}]*grid-template-columns:\s*1fr\s*;",
                        css, re.S)
    assert collapse, "the page-wide one-column collapse of .qator-wrap disappeared"
    stop = int(collapse.group(1))

    # Walk the sheet keeping the @media conditions actually in force: the last
    # `@media` seen before a rule is not necessarily its wrapper, and a gate that
    # guesses would pass the very bug it exists to catch.
    stack = []
    i, pending = 0, ""
    while i < len(css):
        ch = css[i]
        if ch == "{":
            if pending.strip().startswith("@media"):
                stack.append(pending.strip())
            else:
                sel, decl, j = pending, "", i + 1
                depth = 1
                while depth:
                    if css[j] == "{":
                        depth += 1
                    elif css[j] == "}":
                        depth -= 1
                        if not depth:
                            break
                    decl += css[j]
                    j += 1
                cols = re.search(r"grid-template-columns\s*:\s*([^;}]+)", decl)
                if cols and ".qator-wrap" in sel and sel.count(".") >= 2 \
                        and len(cols.group(1).split()) >= 2:
                    floors = [int(m.group(1)) for frame in stack for m in
                              re.finditer(r"min-width:\s*(\d+)px", frame)]
                    assert floors and max(floors) >= stop + 1, (
                        "%r widens .qator-wrap with no min-width guard "
                        "(collapse ends at %dpx)" % (sel.strip(), stop))
                i, pending = j, ""
                continue
            pending = ""
        elif ch == "}":
            if stack:
                stack.pop()
            pending = ""
        elif ch == ";":
            pending = ""
        else:
            pending += ch
        i += 1


def test_the_browser_encoder_writes_a_header_the_engine_accepts():
    """The two ends of the demo were written separately: if the browser's WAV is
    not the format the engine reads, the widget shows an error the product cannot
    reproduce — and a visitor decides the moat is broken."""
    js = _js()
    body = js[js.index("function wavMono16"):js.index("(function jimlikDemo")]
    assert 'text(0, "RIFF")' in body and 'text(12, "fmt ")' in body
    assert 'view.setUint16(20, 1, true)' in body      # format tag 1: uncompressed
    assert "setUint16(34, 16" in body                 # 16-bit, the only depth accepted
    # …and a file built by that code really does parse and align.
    rate = 8000
    samples = [int(0.3 * 32767 * math.sin(2 * math.pi * 145 * i / rate))
               for i in range(int(1.5 * rate))] + [0] * int(1.0 * rate)
    pcm, got_rate = A.pcm_from_wav(wav_bytes(array("h", samples), rate=rate))
    assert got_rate == rate
    rep = A.align(pcm, rate, [Cue(1, 0.0, 1.2, "Salom."),
                              Cue(2, 1.6, 2.4, "Qalaysiz.")])
    assert rep["audio"]["gaps"] >= 1


# ─── Round 25: «Ovoz Qayta» — слушать ленту длиннее одного окна ───────────────
# English is the language of these files, so the header above is the only Russian
# line here on purpose: it names the release the reader is standing in.

def blocks_of(pcm, size_samples: int):
    """The same bytes, arriving as a pipe would: fixed-size blocks, last one short."""
    raw = pcm.tobytes() if isinstance(pcm, array) else bytes(pcm)
    step = max(2, size_samples * 2)
    for off in range(0, len(raw), step):
        yield raw[off:off + step]


def _env_of(pcm, size_samples: int, block_sec: float) -> A.Envelope:
    env = A.Envelope(RATE, block_sec)
    for b in blocks_of(pcm, size_samples):
        env.feed(b)
    return env


@pytest.mark.parametrize("size", [1_000, 3_999, 8_000, 8_001, 16_000, 60 * RATE])
def test_a_tape_measured_in_blocks_is_the_same_tape(size):
    """The frame sequence is a property of the recording, not of the reader.

    Blocks are a memory shape. If the envelope depended on where a block ended, the
    gate would move between two runs of the same job on two different machines and
    every claim about determinism in this repository would be decoration."""
    src = long_tape(60)
    one = A.frame_curve(src, RATE)
    got = _env_of(src, size, min(A.BLOCK_SEC_MAX, max(1.0, size / RATE))).curve()
    assert got["levels"] == one["levels"], f"block {size} measured different frames"
    assert got["gate_db"] == one["gate_db"] and got["duration"] == one["duration"]
    assert got["noise_floor"] == one["noise_floor"]


def test_a_half_sample_byte_is_neither_dropped_nor_invented():
    """A pipe hands over what it has: an odd byte count is normal, and the sample it
    belongs to is not a sample until its partner arrives. Carry it, or the next
    block reads every frame out of alignment by one byte."""
    src = array("h", [0] * 7 + [12000] * 400 + [0] * 3)
    raw = src.tobytes() + b"\x7f"                # a trailing half sample
    env = A.Envelope(RATE, 1.0)
    env.feed(raw[:501])                          # odd split: mid-sample
    env.feed(raw[501:])
    whole = A.frame_curve(src, RATE)
    assert env.samples == len(src), "a half sample was counted as a whole one"
    assert env.curve()["levels"] == whole["levels"]


def test_aligning_a_tape_in_blocks_equals_aligning_it_at_all():
    """Not the curve but the ANSWER: same cues, same pauses, byte-identical report,
    with `blocks` the only field allowed to say how the tape was heard.

    This is the test that makes "we listened to all of it" more than a second code
    path wearing the first one's name."""
    src, cues = long_tape(60), block_cues(60)
    whole = A.align(src, RATE, cues)
    streamed = A.align_blocks(blocks_of(src, 5_000), RATE, cues)
    assert {k: v for k, v in streamed.items() if k != "audio"} == \
           {k: v for k, v in whole.items() if k != "audio"}
    assert whole["audio"]["streamed"] is False and streamed["audio"]["streamed"] is True
    assert streamed["audio"]["blocks"] > 1 and streamed["audio"]["heard_sec"] == 60.0
    assert streamed["srt"] == whole["srt"]


def test_a_cut_at_minute_nineteen_lands_on_a_real_pause():
    """The customer-visible claim of the release. A twenty-minute tape used to get
    either a refusal or fifteen minutes of work and a checkbox; here a cue planted
    in the last block is moved onto the pause that is really under it. Nothing is
    stubbed — the engine listens to the whole tape."""
    src = long_tape(1200)
    cues = block_cues(1200)[-3:]
    rep = A.align_blocks(blocks_of(src, 60 * RATE), RATE, cues)
    assert rep["audio"]["heard_sec"] == 1200.0, rep["audio"]
    assert rep["summary"]["moved"] == 2 * len(cues), rep["summary"]
    assert all(m["shift"] <= 0.6 for m in rep["moves"]), rep["moves"]
    assert rep["summary"]["never_worse"] and rep["summary"]["words_preserved"]
    assert rep["summary"]["beyond_audio"] == 0


def test_a_block_bigger_than_the_promised_window_is_refused():
    """`block_sec` is the memory bound, so it has to be enforced and not documented:
    a caller that promises one minute and hands over an hour is the reason a job
    stops being a queue item and becomes an outage."""
    env = A.Envelope(RATE, 1.0)
    with pytest.raises(A.AlignError) as exc:
        env.feed(array("h", [0] * (5 * RATE)).tobytes())
    assert exc.value.code == "bad_block"
    for bad in (0.0, 0.2, A.BLOCK_SEC_MAX + 1, float("nan"), "sixty"):
        with pytest.raises(A.AlignError):
            A.Envelope(RATE, bad)
    with pytest.raises(A.AlignError) as rate_exc:
        A.Envelope(400, 60.0)
    assert rate_exc.value.code == "bad_rate"


def test_the_pass_never_holds_more_tape_than_it_promised():
    """`peak_samples` is not a statistic nobody reads: it is the number the hour-long
    ceiling is priced against, measured on the pass itself."""
    src = long_tape(120)
    env = _env_of(src, 10 * RATE, 10.0)
    assert env.peak_samples <= 10 * RATE + env.frame_len, env.peak_samples
    assert env.samples == len(src) and env.blocks == 12


def test_the_index_answers_exactly_what_the_sweep_answers():
    """The pause index is what makes an hour affordable — 1500 cues sweeping 20 000
    pauses is the cost that was refused on. It must not move a cut by one
    millisecond, so both search strategies get asked the same questions on the same
    gaps, and any difference is a bug rather than a trade-off."""
    rnd = __import__("random").Random(20260928)
    for _ in range(30):
        pos, gaps = 0.0, []
        while pos < 300.0:
            start = pos + rnd.uniform(0.0, 0.4)
            gaps.append(A.Gap(round(start, 3),
                              round(start + rnd.choice([A.MIN_GAP_SEC,
                                                       rnd.uniform(0.06, 0.9),
                                                       rnd.uniform(0.9, 3.0)]), 3)))
            pos = gaps[-1].end + rnd.uniform(0.1, 3.0)
        idx = A._GapIndex.build(gaps)
        assert idx is not None, "a legal pause list lost its index"
        for _ in range(60):
            t = rnd.uniform(0.0, 300.0)
            low = max(0.0, t - rnd.uniform(0.0, 1.2))
            high = t + rnd.uniform(0.0, 1.2)
            shift = rnd.choice([0.05, 0.6, 2.0])
            assert A.snap_one(t, gaps, shift, low, high, idx) == \
                   A.snap_one(t, gaps, shift, low, high), (t, low, high, shift)


def test_an_unsorted_pause_list_keeps_the_honest_sweep():
    """The index assumes ascending, disjoint pauses. Given anything else it must not
    exist at all: a wrong answer delivered fast is the one failure mode this
    optimisation is not allowed to add."""
    crossed = [A.Gap(5.0, 6.0), A.Gap(1.0, 2.0)]
    assert A._GapIndex.build(crossed) is None
    touching = [A.Gap(1.0, 4.0), A.Gap(3.0, 6.0)]
    assert A._GapIndex.build(touching) is None
    assert A._GapIndex.build([]) is None


def test_the_report_is_identical_with_the_index_switched_off(monkeypatch):
    """End to end, on a real interview tape: force the sweep and compare whole
    reports, so an equivalence proven per-boundary cannot hide a cue-level
    interaction (the collapse undo reads pauses too)."""
    src, cues = interview(tone=120), [Cue(1, 0.20, 0.85, "bir"), Cue(2, 1.10, 1.90, "ikki"),
                                      Cue(3, 2.05, 3.10, "uch")]
    fast = A.align(src, RATE, cues)
    monkeypatch.setattr(A._GapIndex, "build", classmethod(lambda cls, gaps: None))
    slow = A.align(src, RATE, cues)
    assert fast == slow


def test_a_profile_window_is_taken_from_the_timeline_the_aligner_wrote(client, auth,
                                                                       monkeypatch):
    """The order is the claim. Round 25 moved voice profiles into the same stream as
    the curve, and that silently put them on the ASR timeline — while `analyze_turns`
    pairs each profile with the cue Jimlik has already moved by up to
    `ALIGN_MAX_SHIFT_SEC`. A window can sit 60% outside its own cue and still print
    three confident numbers, so this test does not check numbers: it asks which
    segments the profile pass was handed.

    The tape here is a real curve built without ffmpeg, and the profile answer is
    synthetic: the only thing under test is the handover between the two steps."""
    from app import db, pipeline as pl
    from audio_studio import listening

    heard = listening(long_tape(60), block_cues(60), 1.0)
    assert heard is not None and "curve" in heard, heard
    monkeypatch.setattr(pl, "_tape_pass",
                        lambda source, billed, deadline=0.0, **kw: dict(heard))

    asked: list[list[float]] = []

    def watching(source, segments, billed, deadline=0.0):
        asked.append([round(s.start, 3) for s in segments])
        return {"voices": [None] * len(segments), "off_tape": 0,
                "heard_sec": 60.0, "note": "", "truncation": {}}

    shifted: list[list[float]] = []
    real_align = pl._align_step

    def lying_align(jid, tape, segments):
        out = real_align(jid, tape, segments)
        # Every cut moves: the diarizer must now hear the moved timeline, not this one.
        moved = [pl.Segment(s.start + 0.4, s.end + 0.4, s.text) for s in out]
        shifted.append([round(s.start, 3) for s in moved])
        return moved

    monkeypatch.setattr(pl, "_profiles_pass", watching)
    monkeypatch.setattr(pl, "_align_step", lying_align)
    r = client.post("/api/jobs", headers=auth,
                    files={"file": ("interview.srt", format_srt(CRUSHED).encode(),
                                    "text/plain")},
                    data={"jtype": "subtitles", "src": "uz", "tgt": "ru",
                          "align": "1", "diarize": "1"})
    assert r.status_code == 201, r.text
    job = r.json()["job"]
    steps = {e["step"] for e in db.job_timeline(job["id"])}
    assert {"align", "diarize"} <= steps, steps
    assert asked and shifted, "the profile pass never ran"
    assert asked[0] == shifted[0], (
        "the diarizer was handed a timeline that is not the one the aligner wrote: "
        f"{asked[0][:4]} vs {shifted[0][:4]}")
    # …and the diarize row says where its profiles came from, in numbers.
    line = [e for e in db.job_timeline(job["id"]) if e["step"] == "diarize"][-1]
    assert line["data"]["code"] == "turns" and line["data"]["heard_sec"] == 60.0, line


def test_a_deadline_between_blocks_wakes_the_job_instead_of_losing_it():
    """`_drain` checks the wall clock where a hung ffmpeg would otherwise sit: a
    cooperative timeout that never regains control is not a timeout."""
    from app import pipeline as pl

    def endless(nbytes: int) -> bytes:
        return b"\x00" * nbytes

    with pytest.raises(TimeoutError):
        pl._drain(endless, 10.0, deadline=time.monotonic() - 1.0)


def test_a_hung_pipe_is_killed_before_it_is_closed(monkeypatch):
    """`close()` on a pipe a worker is still reading waits for that worker, and the
    worker waits for ffmpeg: the promised read timeout then returns nothing at all.
    Measured on the old order — 98,8 s of cleanup for a 1 s timeout — which leaves
    the job `running` with no refund, keeps the child decoding, and hangs server
    shutdown on the pool's atexit join.

    Asserted by the order of calls, not by a stopwatch: a timing test is a flake
    that happens to pass on a fast machine."""
    import threading

    from app import pipeline as pl

    calls: list[str] = []
    released = threading.Event()

    class FakePipe:
        def read(self, nbytes: int) -> bytes:
            released.wait(30)         # настоящий read ждёт, пока процесс не умрёт
            return b""

        def close(self) -> None:
            calls.append("close")

    class FakeProc:
        stdout = FakePipe()

        def poll(self):
            return None

        def kill(self) -> None:
            calls.append("kill")
            released.set()

        def wait(self, timeout=None) -> int:
            calls.append("wait")
            return 0

    monkeypatch.setattr(pl.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(pl, "LISTEN_READ_TIMEOUT_SEC", 0.2)
    with pytest.raises(TimeoutError):
        pl._tape_pass(Path("silent-never-answers.mp3"), 5.0)
    assert calls and calls[0] == "kill", f"cleanup did not start by killing: {calls}"
    assert "close" in calls[1:], f"the pipe was closed before the kill: {calls}"


def test_a_refused_block_leaves_the_pass_exactly_as_it_was():
    """A pass that says "I will not hold that" must not have already counted it:
    `peak_samples` is the memory number the job's ceiling is priced against, and a
    refusal that grows it makes the artifact lie about the promise it just kept."""
    env = A.Envelope(RATE, 1.0)
    with pytest.raises(A.AlignError) as exc:
        env.feed(array("h", [0] * (3 * RATE)).tobytes())
    assert exc.value.code == "bad_block"
    assert (env.samples, env.peak_samples, env.blocks) == (0, 0, 0)
    assert not env._levels, "a refused block was measured anyway"


def test_a_pause_exactly_min_gap_long_holds_no_cut_for_either_search():
    """`MIN_GAP_SEC == 2 * BOUNDARY_PAD`: such a pause is legal as a pause and empty
    as a place to put a cut. The sweep asks `_target` and says "not in silence";
    an index that answers from the raw bounds says the opposite, and the customer
    report then counts a cut as already-correct because of an optimisation."""
    gaps = [A.Gap(1.0, 1.0 + A.MIN_GAP_SEC), A.Gap(3.0, 4.0)]
    idx = A._GapIndex.build(gaps)
    assert idx is not None
    mid = 1.0 + A.MIN_GAP_SEC / 2
    assert idx.at(mid) == -1, "the index put a cut inside a pause with no room"
    assert A.snap_one(mid, gaps, 0.6, 0.0, 5.0, idx) == \
           A.snap_one(mid, gaps, 0.6, 0.0, 5.0)


def test_a_tape_shorter_than_a_frame_is_too_short_and_not_missing_audio(client, auth):
    """Four hundred real samples are an audio track the engine cannot measure — they
    are not "no audio track". The client reads these reasons in three languages, so
    the difference is a user hunting for a file they already uploaded."""
    from app import db, pipeline as pl
    from audio_studio import listening
    uid = db.create_user("TS", "+99890ts00001")["id"]
    job = db.create_job(uid, "subtitles", "uz", "ru", 1.0, "x.srt", {})
    tiny = long_tape(60)[:400]                  # 0,05 с: лента есть, кадр не выходит
    tape = listening(tiny, block_cues(6), 10.0)
    assert tape is not None, "a short tape was reported as no tape at all"
    assert "curve" not in tape and tape["refusal"] == "too_short", tape
    pl._align_step(job["id"], tape, block_cues(6))
    line = [e for e in db.job_timeline(job["id"]) if e["step"] == "align"][-1]
    assert line["data"] == {"code": "skipped", "reason": "too_short"}, line["data"]
    assert "too_short" in line["message"], line["message"]

