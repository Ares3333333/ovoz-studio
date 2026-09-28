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

    def synthesize(self, text: str, lang: str, out_path: Path, dur_sec: float) -> None:
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
    `--list-voices`: у Microsoft единственный узбекский локат — `uz-UZ` (Madina
    женский, Sardor мужской). Прежнее `uz-MM-AtiyeNeural` не существовало: локат
    `MM` выдуман, имени `Atiye` нет, и каждый реальный узбекский даббинг падал на
    `NoAudioReceived`. Слоты времени речи не выдумываем — берём то, что отдаёт API."""

    VOICES = {
        "uz": "uz-UZ-MadinaNeural",
        "uz_male": "uz-UZ-SardorNeural",
        "ru": "ru-RU-SvetlanaNeural",
        "en": "en-US-JennyNeural",
    }
    # Only the three shipped UI languages get a first-class key; anything else
    # falls back to Russian rather than inventing a voice id that 404s.
    name = "edge-tts"

    def synthesize(self, text: str, lang: str, out_path: Path, dur_sec: float) -> None:
        import asyncio

        import edge_tts  # опциональная зависимость: pip install edge-tts

        voice = self.VOICES.get(lang, self.VOICES["ru"])

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
