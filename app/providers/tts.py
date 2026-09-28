"""TTS-провайдеры: офлайн-заглушка (тон) и edge-tts (реальная узбекская/русская озвучка)."""
from __future__ import annotations

import math
import wave
from pathlib import Path

from ..config import settings

SAMPLE_RATE = 22050


class StubTTS:
    """Генерирует слышимую заглушку: мелодичный тон нужной длительности.

    Позволяет проверить весь даббинг-пайплайн (склейку дорожек по таймингам)
    полностью офлайн. В проде заменяется на EdgeTTS/GPT-SoVITS/собственную модель.
    """

    name = "stub-tone"

    def synthesize(self, text: str, lang: str, out_path: Path, dur_sec: float,
                   speaker: int = 0) -> None:
        dur = max(0.3, min(dur_sec, 30.0))
        n = int(SAMPLE_RATE * dur)
        base = 220.0 if lang == "uz" else 180.0  # разная «интонация» по языкам
        with wave.open(str(out_path), "w") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            frames = bytearray()
            for i in range(n):
                t = i / SAMPLE_RATE
                env = math.sin(math.pi * min(1.0, t / dur))  # плавная огибающая
                f = base + 40 * math.sin(2 * math.pi * 1.7 * t)
                val = int(12000 * env * math.sin(2 * math.pi * f * t))
                frames += val.to_bytes(2, "little", signed=True)
            wav.writeframes(bytes(frames))


class EdgeTTS:
    """Реальный TTS через Microsoft Edge neural voices. Голоса подобраны живым
    `--list-voices` (Round 29/31), не по памяти: у Microsoft единственный узбекский
    локат — `uz-UZ`. Каждый язык — пара [основной(женский), запасной(мужской)],
    чтобы диалог двух голосов озвучивали два разных реальных голоса, а не один."""

    VOICES = {
        "uz": ["uz-UZ-MadinaNeural", "uz-UZ-SardorNeural"],
        "ru": ["ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural"],
        "en": ["en-US-JennyNeural", "en-US-GuyNeural"],
    }
    # `uz`/`ru`/`en` are exactly the dubbing targets the job LANGS allow, and each
    # carries two verified voices so a diarized speaker map can be cast. An unknown
    # language falls back to Russian rather than inventing a voice id that 404s.
    name = "edge-tts"

    def _voice(self, lang: str, speaker: int) -> str:
        seq = self.VOICES.get(lang) or self.VOICES["ru"]
        if speaker and speaker > 0:
            return seq[(speaker - 1) % len(seq)]   # 1-based diarization -> 0-based
        return seq[0]                              # no speaker map -> primary voice

    def synthesize(self, text: str, lang: str, out_path: Path, dur_sec: float,
                   speaker: int = 0) -> None:
        import asyncio

        import edge_tts  # опциональная зависимость: pip install edge-tts

        voice = self._voice(lang, speaker)

        async def _run() -> None:
            com = edge_tts.Communicate(text, voice)
            await com.save(str(out_path))

        asyncio.run(_run())


def status() -> dict:
    """Does the deployment really speak the lines, or does it emit stub tones?

    The answer needs the package, not just the setting: `OVOZ_TTS_PROVIDER=edge`
    without `edge_tts` installed has been falling back silently, which turns a
    paid dubbing job into a beep track nobody told the customer about.
    """
    provider = settings.tts_provider
    out = {"provider": provider, "mode": "sim", "code": "", "reason": "",
           "operator": {"package_found": False}}
    if provider != "edge":
        out["code"], out["reason"] = "no_provider", "stub tones (no provider configured)"
        return out
    try:
        import edge_tts  # noqa: F401
        out["operator"]["package_found"] = True
    except ImportError:
        out["code"] = "package_missing"
        out["reason"] = "OVOZ_TTS_PROVIDER=edge but edge_tts is not installed"
        return out
    out["mode"], out["code"] = "real", "ok"
    return out


def get_tts() -> StubTTS | EdgeTTS:
    """The provider to use now — see `status()` for why it is the one."""
    if status()["mode"] == "real":
        return EdgeTTS()
    return StubTTS()
