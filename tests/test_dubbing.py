"""Round 28 — the dubbing mix must survive a real voice, not just the stub.

`StubTTS` emits exactly `dur_sec` of audio at 22050 Hz, so for its whole life the
dubbing mixer looked correct: lines landed on their cue, the buffer was sized from
`max(seg.end)`, and nothing ever overran. A neural voice breaks all three at once —
it speaks for however long it speaks, sometimes at 24 kHz — and the old code turned
that into overlapping voices summed on top of each other plus a silently clipped
tail. This is the flagship "даббинг" feature shipping fake behaviour the moment it
went real, which no test caught because no test ever handed the mixer a voice
longer than its window.

The fix splits into a pure scheduler (unit-testable, no audio) and honest re-timing
in the mix; the API-level test drives the real pipeline with a fake provider.
"""
import io
import math
import subprocess
import wave
from pathlib import Path

import pytest

from app import pipeline
from app.providers.base import Segment

RATE = 22050


def _sine_wav(path: Path, seconds: float, rate: int = RATE) -> None:
    """A real 16-bit mono PCM WAV of exactly `seconds`, at `rate` Hz."""
    n = max(1, int(seconds * rate))
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            v = int(6000 * math.sin(2 * math.pi * 220.0 * i / rate))
            frames += v.to_bytes(2, "little", signed=True)
        w.writeframes(bytes(frames))


def _dur(path: Path) -> tuple[float, int, int]:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate(), w.getframerate(), w.getnchannels()


# ─── the pure scheduler: where does each voice stand ─────────────────────────

BIG = 1e9    # a cap that never binds, for the scheduling tests


def test_a_voice_that_fits_its_window_stays_on_its_cue():
    """Backward-compat: well-fitted audio (the stub, or a disciplined real voice)
    must reproduce the cue timeline exactly — the fix is invisible to what already
    worked, or we would have quietly shifted every existing dub."""
    segs = [Segment(0.0, 2.0, "a"), Segment(2.0, 4.0, "b"), Segment(5.0, 6.0, "c")]
    starts, total, clipped = pipeline._dub_schedule(segs, [2.0, 2.0, 1.0], BIG)
    assert starts == [0.0, 2.0, 5.0]
    assert total == pytest.approx(6.0)
    assert not clipped


def test_a_long_voice_pushes_the_next_one_back_instead_of_stacking_on_it():
    """The core defect: two cues one second apart, but the first sentence takes
    three to speak. Old code summed voice 2 at t=1 on top of voice 1 — a jumble.
    Now voice 2 waits for voice 1 to finish."""
    segs = [Segment(0.0, 1.0, "long one"), Segment(1.0, 2.0, "next")]
    starts, total, clipped = pipeline._dub_schedule(segs, [3.0, 1.0], BIG)
    assert starts == [0.0, 3.0]
    assert starts[1] >= starts[0] + 3.0            # never overlap
    assert total >= 4.0                             # and nothing was clipped away
    assert not clipped


def test_schedule_is_monotonic_and_never_overlaps_for_any_durations():
    segs = [Segment(i, i + 1.0, f"l{i}") for i in range(6)]
    durations = [2.5, 0.4, 3.0, 1.0, 0.2, 4.0]
    starts, total, clipped = pipeline._dub_schedule(segs, durations, BIG)
    assert not clipped
    for k in range(1, len(starts)):
        assert starts[k] >= starts[k - 1] + durations[k - 1] - 1e-9, "voices overlapped"
        assert starts[k] >= segs[k].start            # never rewind before its cue
    assert total >= starts[-1] + durations[-1]


def test_an_empty_script_yields_a_positive_silent_track():
    starts, total, clipped = pipeline._dub_schedule([], [], BIG)
    assert starts == []
    assert total >= 1.0    # a zero-length buffer would make wave.open choke
    assert not clipped


def test_a_negative_measured_duration_cannot_move_the_head_backwards():
    """The mixer trusts `actual` from the file header; a broken/short read must not
    make the schedule run backwards and overwrite earlier audio."""
    segs = [Segment(0.0, 1.0, "a"), Segment(1.0, 2.0, "b")]
    starts, total, _ = pipeline._dub_schedule(segs, [1.0, -5.0], BIG)
    assert starts == [0.0, 1.0]


def test_a_runaway_schedule_is_capped_and_says_so_rather_than_eating_ram():
    """A provider whose every voice is many times its window would, under the naive
    fix, grow the mix buffer without bound (the old code was implicitly capped by
    billing via max(seg.end)). The schedule clamps the output to cap_sec and reports
    the truncation honestly instead of allocating a giant bytearray."""
    segs = [Segment(float(i), float(i) + 1.0, f"l{i}") for i in range(100)]
    durations = [10.0] * 100                       # 1000 s of voice for a 100 s tape
    starts, total, clipped = pipeline._dub_schedule(segs, durations, cap_sec=250.0)
    assert clipped is True
    assert total <= 250.0
    assert len(starts) == 100                       # plan stays aligned with voices


# ─── _ensure_wav: sample rate is a time scale, not a quality knob ────────────

def test_a_canonical_pcm_wav_is_left_byte_for_byte_untouched(tmp_path):
    """The stub path must not start needing ffmpeg: if the header is already
    22050/mono/16 the mixer is right to trust it, and re-encoding would change the
    samples. This proves the fix did not turn every dub into a subprocess call."""
    p = tmp_path / "ok.wav"
    _sine_wav(p, 0.5)
    before = p.read_bytes()
    pipeline._ensure_wav(p)
    assert p.read_bytes() == before


_HAS_FFMPEG = subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
def test_a_real_wav_at_the_wrong_rate_is_normalized_not_mistimed(tmp_path):
    """A 24 kHz neural voice used to pass the RIFF sniff untouched, then its frames
    landed on a 22050 grid: the dub played ~7% fast and every later line slid out of
    its pause. `_ensure_wav` must normalise a legit-but-wrong-rate WAV too."""
    p = tmp_path / "edge.wav"
    _sine_wav(p, 1.0, rate=24000)
    assert _dur(p)[1] == 24000
    pipeline._ensure_wav(p)
    dur, rate, chans = _dur(p)
    assert rate == RATE and chans == 1
    assert dur == pytest.approx(1.0, abs=0.05)   # real seconds preserved


# ─── the mix, on the real pipeline, with a voice that overruns its window ────

class _OverrunVoice:
    """A fake 'neural' provider: always speaks three times its cue window. No stub
    ever did this, which is exactly why the bug survived every prior round."""
    name = "fake-neural"

    def synthesize(self, text, lang, out_path, dur_sec):
        _sine_wav(Path(out_path), dur_sec * 3.0)


def _upload_srt(client, auth, text):
    return client.post("/api/jobs", headers=auth,
                       files={"file": ("d.srt", text.encode("utf-8"),
                                       "application/octet-stream")},
                       data={"jtype": "dubbing", "src": "ru", "tgt": "uz"})


SRT_3_LINES = (
    "1\n00:00:00,000 --> 00:00:01,000\nPervaya\n\n"
    "2\n00:00:01,000 --> 00:00:02,000\nVtoraya\n\n"
    "3\n00:00:02,000 --> 00:00:03,000\nTretya\n\n"
)


def test_a_long_voice_extends_the_dub_instead_of_clipping_its_tail(client, auth,
                                                                   monkeypatch):
    monkeypatch.setattr(pipeline, "get_tts", lambda: _OverrunVoice())
    r = _upload_srt(client, auth, SRT_3_LINES)
    assert r.status_code == 201, r.text
    job = r.json()["job"]
    assert job["status"] == "done", job.get("error")
    wav = client.get(job["artifacts"]["dubbing"], headers=auth).content
    with wave.open(io.BytesIO(wav), "rb") as w:
        dur = w.getnframes() / w.getframerate()
    # 3 voices × 3 s each, scheduled back-to-back ≈ 9 s. The old code sized the
    # buffer from max(seg.end)=3 s and clipped — a dub that simply stopped.
    assert dur > 8.0, f"tail was clipped: dub is only {dur:.2f}s"
    assert dur < 10.5, f"schedule ran away: {dur:.2f}s"


def test_the_customer_is_told_the_dub_ran_past_the_timings(client, auth, monkeypatch):
    """Non-overlapping is the right fix, but it does mean the voices no longer sit on
    their original cue times — the customer deserves to know, not a silent 'done'."""
    monkeypatch.setattr(pipeline, "get_tts", lambda: _OverrunVoice())
    r = _upload_srt(client, auth, SRT_3_LINES)
    jid = r.json()["job"]["id"]
    timeline = client.get(f"/api/jobs/{jid}", headers=auth).json()["timeline"]
    events = " | ".join(e.get("message", "") for e in timeline)
    assert "retimed" in events and "overlap" in events, events


def test_a_disciplined_voice_is_never_reported_as_retimed(client, auth, monkeypatch):
    class _FittingVoice:
        name = "fake-fit"

        def synthesize(self, text, lang, out_path, dur_sec):
            _sine_wav(Path(out_path), dur_sec)     # exactly on budget

    monkeypatch.setattr(pipeline, "get_tts", lambda: _FittingVoice())
    r = _upload_srt(client, auth, SRT_3_LINES)
    jid = r.json()["job"]["id"]
    timeline = client.get(f"/api/jobs/{jid}", headers=auth).json()["timeline"]
    assert not any("retimed" in e.get("message", "") for e in timeline)


def test_the_mix_is_a_valid_mono_pcm_wav_the_share_page_can_play(client, auth,
                                                                 monkeypatch):
    monkeypatch.setattr(pipeline, "get_tts", lambda: _OverrunVoice())
    r = _upload_srt(client, auth, SRT_3_LINES)
    wav = client.get(r.json()["job"]["artifacts"]["dubbing"], headers=auth).content
    assert wav[:4] == b"RIFF"
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        assert w.getframerate() == RATE


def test_dubbing_artifact_reports_no_filesystem_path(client, auth, monkeypatch):
    """The job payload is customer-visible; the mixer writes real temp files, and none
    of those paths may leak into the API."""
    monkeypatch.setattr(pipeline, "get_tts", lambda: _OverrunVoice())
    r = _upload_srt(client, auth, SRT_3_LINES)
    jid = r.json()["job"]["id"]
    blob = client.get(f"/api/jobs/{jid}", headers=auth).text
    # The download URL carries the kind (`.../download/dubbing`) and the job id, but
    # never the file the mixer actually wrote to disk.
    assert str(jid) in blob                   # the id legitimately appears
    assert "dubbing.wav" not in blob          # the on-disk filename does not
    assert "line_" not in blob                # nor a per-line temp name
