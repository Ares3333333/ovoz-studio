"""Round 29 — the dubbing voice must be a voice Microsoft actually ships.

For every prior round the product's `edge-tts` path named an Uzbek voice that does
not exist (`uz-MM-AtiyeNeural` — locale `MM` is invented, `Atiye` is not a voice),
so the moment dubbing went to the real provider it died on `NoAudioReceived`. The
stub never noticed. A test that only checks the dict is non-empty would not catch a
wrong-but-well-formed name either — so the gate asserts the *locale prefix* is the
one real Uzbek locale, and a live (network-gated) test proves the voice answers.
"""
import os
import re
import wave

import pytest

from app.providers.tts import EdgeTTS

# Microsoft voice ids look like `uz-UZ-MadinaNeural`: lang-REGION-Name + "Neural".
VOICE_ID = re.compile(r"^[a-z]{2}-[A-Z]{2}-[A-Za-z]+Neural$")


def test_every_configured_voice_is_a_microsoft_voice_shape():
    for lang, voice in EdgeTTS.VOICES.items():
        assert VOICE_ID.match(voice), f"{lang!r} voice {voice!r} is not a valid id"


def test_the_uzbek_voice_uses_the_only_real_uzbek_locale():
    """The exact bug this round fixed: `uz-MM-...`. Microsoft publishes exactly one
    Uzbek locale (`uz-UZ`); any other region code cannot exist, so the guard is on
    the prefix, not on the (unknowable-offline) voice name."""
    assert EdgeTTS.VOICES["uz"].startswith("uz-UZ-"), EdgeTTS.VOICES["uz"]
    assert EdgeTTS.VOICES["ru"].startswith("ru-RU-")
    assert EdgeTTS.VOICES["en"].startswith("en-US-")


def test_every_sellable_target_language_has_its_own_voice():
    """The invariant this round exists to protect: any language the product will sell
    a dub in must have a first-class voice, or a future LANGS entry would dub in
    Russian and still reach `done` silently. Also pins the reserved uz_male locale."""
    from app.main import LANGS
    assert LANGS <= set(EdgeTTS.VOICES), LANGS - set(EdgeTTS.VOICES)
    assert EdgeTTS.VOICES["uz_male"].startswith("uz-UZ-")


def test_synthesize_selects_the_real_uzbek_voice(tmp_path, monkeypatch):
    """The wiring, not the network: the voice handed to edge-tts for lang 'uz' must
    be the Madina id, and an unknown language must fall back to Russian rather than
    invent a fourth voice."""
    import edge_tts

    captured = {}

    class FakeCom:
        def __init__(self, text, voice):
            captured["voice"] = voice
            captured["text"] = text

        async def save(self, path):
            # a canonical 22050 mono 16-bit WAV so no ffmpeg is needed here
            with wave.open(str(path), "w") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(22050)
                w.writeframes(b"\x00\x20" * 2205)   # 0.1 s

    monkeypatch.setattr(edge_tts, "Communicate", FakeCom)
    tts = EdgeTTS()
    tts.synthesize("Salom", "uz", tmp_path / "a.wav", dur_sec=1.0)
    assert captured["voice"] == EdgeTTS.VOICES["uz"] == "uz-UZ-MadinaNeural"
    tts.synthesize("Salom", "kk", tmp_path / "b.wav", dur_sec=1.0)
    assert captured["voice"] == EdgeTTS.VOICES["ru"], "unknown lang must fall back, not guess"


def test_status_reports_edge_real_only_when_the_package_is_present(monkeypatch):
    """`OVOZ_TTS_PROVIDER=edge` without the package installed used to fall back to
    stub tones silently — a paid dub delivered as a beep track. status() separates
    the claim from the proof."""
    from app import config
    from app.providers import tts as ttsmod
    monkeypatch.setattr(config.settings, "tts_provider", "edge", raising=False)
    st = ttsmod.status()
    assert st["mode"] == "real" and st["code"] == "ok", st   # edge_tts is installed here
    assert st["operator"]["package_found"] is True


@pytest.mark.skipif(
    os.environ.get("OVOZ_TEST_NETWORK") != "1",
    reason="live voice check needs network; opt in with OVOZ_TEST_NETWORK=1",
)
def test_the_uzbek_voice_really_speaks(tmp_path):
    """The proof the shipped name was dead: over the network, the configured Uzbek
    voice returns non-trivial audio. Skipped by default so CI (offline) stays green."""
    out = tmp_path / "uz.wav"
    EdgeTTS().synthesize("Assalomu alaykum, bu Ovoz AI Studio.", "uz", out, dur_sec=4.0)
    assert out.stat().st_size > 1000, "the voice returned nothing — it may not exist"
