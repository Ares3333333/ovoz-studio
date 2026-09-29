"""Round 29->37 — every voice the product casts must be real for its language.

Round 29 caught a fabricated id (`uz-MM-AtiyeNeural`). Round 31 needed a pair.
Round 37 raised the bar with a live TTS->ASR oracle (synthesis judged by local
faster-whisper, not by trust): the Edge catalog carries exactly 2 Uzbek voices,
reads Russian natively through only 6 short-names (the non-multilingual en-US
voices answer `NoAudioReceived` for Cyrillic text — a gate that once forbade
them from the ru roster would have been forbidding the WRONG thing: correct
multilingual voices), and ships 17 English voices. The product now sells 10
distinct-sounding slots per language (5 female + 5 male): real voices first,
then honest DSP personas of proven voices (pitch/tempo-shifted, pronunciation
untouched) — and never a voice the oracle saw refuse.
"""
import os
import shutil
import wave

import pytest

from app.providers.tts import EDGE_RATE, EdgeTTS, apply_persona

FEMALE = {
    "Madina", "Svetlana", "Ava", "Emma", "Jenny", "Aria", "Michelle",
}
MALE = {"Sardor", "Dmitry", "Andrew", "Brian", "Guy", "Christopher", "Steffan", "Roger"}


def _name(short: str) -> str:
    # ru-RU-SvetlanaNeural -> Svetlana; en-US-AvaMultilingualNeural -> Ava
    return short.split("-")[-1][: -len("Neural")].replace("Multilingual", "")


def test_rosters_are_ten_strong_and_gendered_five_plus_five():
    for lang, rows in EdgeTTS.ROSTERS.items():
        assert len(rows) == 10, f"{lang} must sell 10 voice slots, has {len(rows)}"
        assert {_name(r[0]) for r in rows[:5]} <= FEMALE, f"{lang} slots 0-4 not female"
        assert {_name(r[0]) for r in rows[5:]} <= MALE, f"{lang} slots 5-9 not male"


def test_roster_entries_are_distinct_people_or_distinct_personas():
    """A diarized 10-speaker tape cast onto ten identical (voice,0,0) rows would
    claim variety it does not have. Every slot is a different source voice or a
    genuinely shifted persona."""
    for lang, rows in EdgeTTS.ROSTERS.items():
        assert len(set(rows)) == len(rows), f"{lang} roster has duplicate slots"
        for row in rows:
            assert abs(row[1]) <= 4.0 and 0.9 <= row[2] <= 1.1, \
                f"{lang} persona {row} beyond honest DSP range"


def test_the_uzbek_roster_uses_only_the_real_uzbek_locale():
    """Microsoft publishes exactly one Uzbek locale (`uz-UZ`). Any other region
    code cannot exist — the Round-29 bug class, guarded forever."""
    for short, _p, _t in EdgeTTS.ROSTERS["uz"]:
        assert short.startswith("uz-UZ-"), short


def test_the_russian_roster_carries_no_voice_the_oracle_saw_refuse():
    """The live oracle answered NoAudioReceived for these ids on Cyrillic text;
    a Russian dub cast onto one of them would ship silence-in-place of a voice."""
    ru = {short for short, _p, _t in EdgeTTS.ROSTERS["ru"]}
    assert not (ru & set(EdgeTTS.RU_REFUSED)), ru & set(EdgeTTS.RU_REFUSED)
    for short in ru:
        ok = short.startswith(("ru-RU-", "en-US-")) and (
            short.startswith("ru-RU-") or "Multilingual" in short)
        assert ok, f"{short} is not proven to read Russian"


def test_every_sellable_target_language_has_ten_slots():
    from app.main import LANGS
    assert LANGS <= set(EdgeTTS.ROSTERS), LANGS - set(EdgeTTS.ROSTERS)


def test_entry_selection_cycles_ten_by_speaker():
    """Diarization is 1-based; speaker 1 -> slot 0 (primary female), 6 -> slot 5
    (primary male), 11 wraps to slot 0. speaker 0 (no map) -> slot 0."""
    tts = EdgeTTS()
    uz = EdgeTTS.ROSTERS["uz"]
    assert tts._entry("uz", 0) == uz[0]
    assert tts._entry("uz", 1) == uz[0]
    assert tts._entry("uz", 2) == uz[1]
    assert tts._entry("uz", 11) == uz[0]
    assert tts._entry("de", 7) == EdgeTTS.ROSTERS["ru"][6]  # unknown lang -> ru roster


def test_synthesize_wires_voice_and_persona(tmp_path, monkeypatch):
    import edge_tts

    captured = []

    class FakeCom:
        def __init__(self, text, voice):
            captured.append(voice)

        async def save(self, path):
            with wave.open(str(path), "w") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(22050)
                w.writeframes(b"\x00\x20" * 2205)

    monkeypatch.setattr(edge_tts, "Communicate", FakeCom)
    persona_calls = []
    monkeypatch.setattr("app.providers.tts.apply_persona",
                        lambda p, pitch, tempo: persona_calls.append((pitch, tempo)) or True)
    tts = EdgeTTS()
    tts.synthesize("Salom", "uz", tmp_path / "a.wav", dur_sec=1.0)          # slot 0: raw
    assert captured[-1] == "uz-UZ-MadinaNeural"
    assert persona_calls == [], "an authentic voice slot must not touch DSP"
    tts.synthesize("Salom", "uz", tmp_path / "b.wav", dur_sec=1.0, speaker=2)  # persona
    assert captured[-1] == "uz-UZ-MadinaNeural"
    assert persona_calls and persona_calls[-1][0] == pytest.approx(2.4)
    tts.synthesize("Salom", "kk", tmp_path / "c.wav", dur_sec=1.0)
    assert captured[-1] == "ru-RU-SvetlanaNeural", "unknown lang must fall back, not guess"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg on this box")
def test_apply_persona_shifts_pitch_and_keeps_duration(tmp_path):
    """The DSP promise, measured: +3 semitones raises f0 by 2^(3/12) while the
    atempo correction keeps duration — the customer hears a different timbre,
    not a different person reading faster or chopped words."""
    src = tmp_path / "sine.wav"
    f0, dur = 440.0, 1.0
    with wave.open(str(src), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(EDGE_RATE)
        import math
        w.writeframes(b"".join(
            int(9000 * math.sin(2 * math.pi * f0 * i / EDGE_RATE)).to_bytes(2, "little", signed=True)
            for i in range(EDGE_RATE * int(dur))))
    assert apply_persona(src, 3.0, 1.0) is True
    with wave.open(str(src), "rb") as w:
        n = w.getnframes()
        data = w.readframes(n)
    samples = [int.from_bytes(data[i:i + 2], "little", signed=True) for i in range(0, len(data) - 1, 2)]
    # zero-crossing frequency estimate over the stable middle
    mid = samples[len(samples) // 4: len(samples) // 4 * 3]
    crosses = sum(1 for a, b in zip(mid, mid[1:]) if (a < 0 <= b))
    span_sec = len(mid) / EDGE_RATE
    est = crosses / span_sec
    want = f0 * 2 ** (3.0 / 12.0)
    assert abs(est - want) / want < 0.12, f"pitch {est:.0f} Hz, want {want:.0f}"
    assert abs(n / EDGE_RATE - dur) < 0.12, "duration drifted: atempo correction failed"


def test_status_reports_edge_real_only_when_the_package_is_present(monkeypatch):
    from app import config
    from app.providers import tts as ttsmod
    monkeypatch.setattr(config.settings, "tts_provider", "edge", raising=False)
    st = ttsmod.status()
    assert st["mode"] == "real" and st["code"] == "ok", st
    assert st["operator"]["package_found"] is True
    assert st["operator"]["voice_slots"] == {"uz": 10, "ru": 10, "en": 10}


@pytest.mark.skipif(
    os.environ.get("OVOZ_TEST_NETWORK") != "1",
    reason="set OVOZ_TEST_NETWORK=1 to synthesize against the live Edge service",
)
def test_live_uzbek_persona_actually_returns_audio(tmp_path):
    """Nightly truth: slot 2 is Madina +2.4 semis. If Microsoft ever retires the
    voice or ffmpeg dies, the product must be told by a red test, not by a quiet
    fallback to a single timbre."""
    tts = EdgeTTS()
    out = tmp_path / "persona.mp3"
    tts.synthesize("Assalomu alaykum, bugun oba havoa, biz Toshkentdamiz.",
                   "uz", out, dur_sec=6.0, speaker=2)
    assert out.stat().st_size > 2000
