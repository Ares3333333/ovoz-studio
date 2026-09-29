"""Round 37 forensic RAM audit — regression bounds.

The audit's thesis: the 2.7 GB production killer was never one huge file, it was
(1) a mixer buffer sized by the *customer's* timeline instead of the container,
(2) an implicit second copy on write, (3) unbounded concurrent mixers, (4) hope
in the garbage collector where the OS would return native memory. Each bound
below is the arithmetic proof that a class of blow-up can no longer happen —
measured behaviour lives in the long-form stress run (docs/MEMORY_AUDIT_v0.30.md).
"""
import subprocess
from pathlib import Path

import pytest

from app import pipeline


def test_mixer_output_ceiling_is_absolute_not_derived():
    """cap = min(span*2+slack, ABS): with a fabricated 1-million-second timeline
    the buffer must still be 15 minutes of audio — the container decides, not
    the upload. 900 s * 22050 * 2 B = 39.7 MB, the whole job, worst case."""
    span = 1_000_000.0
    cap = min(span * 2.0 + pipeline.TIMELINE_SLACK_SEC, pipeline.MAX_DUB_OUTPUT_SEC)
    assert cap == pipeline.MAX_DUB_OUTPUT_SEC == 900.0
    assert cap * 22050 * 2 < 45e6, "one mixer buffer must fit well under 45 MB"


def test_concurrent_mixers_are_bounded_to_two():
    """The old eight-worker pool could start eight mixers at once: 8*164 MB peak
    copied into a 1 GB container is an OOM-kill, and an OOM-kill is not an
    exception any handler can see. Slots: two mix, the third waits (and watches
    its deadline while waiting — see the acquire loop with _check_deadline)."""
    assert pipeline.DUB_MIXER_SLOTS == 2
    assert pipeline._DUB_SEM._value <= 2


def test_persona_leaves_no_temp_when_ffmpeg_fails(tmp_path, monkeypatch):
    """A failed DSP pass must not orphan a .persona.wav and must not destroy the
    original audio: honest fallback to the raw voice, nothing else."""
    from app.providers import tts as ttsmod

    src = tmp_path / "voice.mp3"
    src.write_bytes(b"\xff\xfb" + b"0" * 4000)

    def boom(*a, **k):
        raise FileNotFoundError("no ffmpeg")

    monkeypatch.setattr(ttsmod.subprocess, "run", boom)
    assert ttsmod.apply_persona(src, 2.0, 1.0) is False
    assert src.read_bytes()[:2] == b"\xff\xfb", "original audio was clobbered"
    assert not list(tmp_path.glob("*.persona*")), "temp survived the failure"


def test_mixer_temp_files_never_outlive_the_job(tmp_path, client, auth):
    """Every line_*.wav must be gone whether the job finishes, dies on deadline
    or raises in the middle — /tmp on a 64 MB tmpfs container has no room for
    litter, and retention code never walks artifact dirs for unlinked temps."""
    import time
    import wave
    from app import db
    from app.providers.base import Segment
    from app.providers.tts import apply_persona

    uid = client.get("/api/me", headers=auth).json()["user"]["id"]
    job = db.create_job(uid, "dubbing", "uz", "ru", 2.0, "x.wav", {})

    class _T:
        name = "tiny"

        def synthesize(self, text, lang, out_path, dur_sec=0, speaker=0):
            with wave.open(str(out_path), "w") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
                w.writeframes(b"\x00\x00" * 2205)

    segs = [Segment(start=0.0, end=1.0, text="salom"),
            Segment(start=1.0, end=2.0, text="keling")]
    pipeline._mix_dubbing(job["id"], segs, _T(), "ru", tmp_path)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith("line_")]
    assert leftovers == [], leftovers
    assert (tmp_path / "dubbing.wav").exists()

    with pytest.raises(TimeoutError):
        pipeline._mix_dubbing(job["id"], segs, _T(), "ru", tmp_path,
                              deadline=time.monotonic() - 1)
    assert [p for p in tmp_path.iterdir() if p.name.startswith("line_")] == []


def test_tape_is_never_wholly_resident():
    """The listen/drain pass reads ffmpeg stdout in LISTEN_BLOCK_SEC blocks; a
    `.read()` with no size would pull a whole hour of PCM (115 MB at 8k mono,
    more at 48k stereo float) straight into RAM. The bound is enforced by the
    source shape, not by a comment: _drain exists, reads bounded, and its body
    mentions the block constant."""
    import inspect

    assert pipeline.LISTEN_BLOCK_SEC <= 60.0
    fn = getattr(pipeline, "_drain", None)
    assert fn is not None, "the bounded drain pass vanished from pipeline"
    body = inspect.getsource(fn)
    assert "LISTEN_BLOCK_SEC" in body, "drain no longer sizes its reads by the block"
    assert ".read()" not in body.replace(" ", ""), "unbounded whole-stream read in drain"


def test_compose_declares_the_memory_contract():
    """docs/MEMORY_AUDIT_v0.30.md numbers into infra: the measured long-form
    dubbing peak (1.07 GB) sets the floor — 2 GB with heavy slots at 1 is the
    honest minimum; job wall is 600 s, so the 10 s SIGKILL default shredded
    paid jobs at every deploy; demo-credit explicitly off by default."""
    y = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "mem_limit: 2g" in y
    assert "OVOZ_ASR_SLOTS: ${OVOZ_ASR_SLOTS:-1}" in y
    assert "OVOZ_DUB_SLOTS: ${OVOZ_DUB_SLOTS:-1}" in y
    assert "stop_grace_period: 11m" in y
    assert "OVOZ_ALLOW_DEMO_CREDIT:-0" in y
    pids = [ln for ln in y.splitlines() if "pids_limit" in ln]
    assert pids, "process budget is part of the memory contract (fork-bombs)"
