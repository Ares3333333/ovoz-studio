"""TTS-провайдеры: офлайн-заглушка (тон) и edge-tts (реальная узбекская/русская озвучка).

Round 37: живой полигон TTS→ASR (probe-data/voice_oracle.py) установил правду о
каталоге Edge: узбекский локат — ровно 2 голоса, русский нативно читают 6
(Svetlana, Dmitry, Ava/Emma/Andrew/Brian Multilingual; Aria/Jenny/Guy/Steffan и
прочие NON-multilingual на русском отдают NoAudioReceived — их в ростере быть
не может), английский — 8f+9m нативно. Поэтому ростер на язык = 10 слотов
(5 женских + 5 мужских): все реальные проверенные голоса + DSP-персоны (сдвиг
высоты темпа через ffmpeg): произношение остаётся нейронным и верным, тембр —
различимым. Это честно: персона не выдаётся за нового человека, label живёт в
ростере, а голос-полигон судит, кто реально умеет читать язык.
"""
from __future__ import annotations

import math
import subprocess
import wave
from pathlib import Path

from ..config import settings

SAMPLE_RATE = 22050
EDGE_RATE = 24000  # поток edge-tts отдаёт именно столько; asetrate привязан к нему


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


def apply_persona(path: Path, pitch_semis: float, tempo: float) -> bool:
    """Сдвинуть тембр готовой дорожки, сохранив длительность и словоизменение.

    asetrate меняет высоту И темп вместе (как замедленная плёнка); atempo=1/k
    возвращает темп, aresample приводит частоту обратно. Итог: тот же нейронный
    голос говорит ровно столько же, другим тембром. Возвращает False, если ffmpeg
    не смог — зовущий честно откатится на исходный голос вместо падения.
    """
    if abs(pitch_semis) < 1e-6 and abs(tempo - 1.0) < 1e-6:
        return True
    k = 2 ** (pitch_semis / 12.0)
    af = f"asetrate={EDGE_RATE}*{k:.6f},aresample={EDGE_RATE},atempo={1 / k:.6f}"
    if abs(tempo - 1.0) >= 1e-6:
        af += f",atempo={tempo:.4f}"
    # явный pcm_s16le в имя с .wav: ffmpeg не должен угадывать контейнер по
    # служебному суффиксу — аmono PCM на выходе ровно то, что ожидает _ensure_wav
    tmp = path.parent / (path.name + ".persona.wav")
    try:
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(path), "-af", af, "-vn", "-acodec", "pcm_s16le",
                        str(tmp)],
                       check=True, capture_output=True, timeout=60)
        tmp.replace(path)
        return True
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


class EdgeTTS:
    """Реальный TTS через Microsoft Edge neural voices.

    Ростер на язык — ровно 10 слотов: 5 женских + 5 мужских. Запись ростера:
    (short_name, pitch_semis, tempo). Ноль-ноль = подлинный голос без обработки;
    смещённые строки — DSP-персоны поверх проверенных полигоном голосов (в uz
    иных реальных узбекских голосов у Microsoft нет — врать про «10 разных людей»
    мы не имеем права, но слушаться 10 разных тембров они обязаны).
    """

    ROSTERS: dict[str, list[tuple[str, float, float]]] = {
        "uz": [  # 2 подлинных + 8 персон: иных uz-голосов каталог не содержит
            ("uz-UZ-MadinaNeural", 0.0, 1.0),
            ("uz-UZ-MadinaNeural", 2.4, 1.0),
            ("uz-UZ-MadinaNeural", -1.8, 1.0),
            ("uz-UZ-MadinaNeural", 1.2, 1.03),
            ("uz-UZ-MadinaNeural", -3.0, 0.98),
            ("uz-UZ-SardorNeural", 0.0, 1.0),
            ("uz-UZ-SardorNeural", -2.2, 1.0),
            ("uz-UZ-SardorNeural", 1.7, 1.0),
            ("uz-UZ-SardorNeural", -3.2, 0.97),
            ("uz-UZ-SardorNeural", 3.0, 1.04),
        ],
        "ru": [  # Svetlana/Dmitry нативны; Ava/Emma/Andrew/Brian Multilingual
                   # прочитаны полигоном не хуже нативных; + персоны до пятёрок
            ("ru-RU-SvetlanaNeural", 0.0, 1.0),
            ("en-US-AvaMultilingualNeural", 0.0, 1.0),
            ("en-US-EmmaMultilingualNeural", 0.0, 1.0),
            ("ru-RU-SvetlanaNeural", 2.2, 1.0),
            ("en-US-AvaMultilingualNeural", -1.9, 1.02),
            ("ru-RU-DmitryNeural", 0.0, 1.0),
            ("en-US-AndrewMultilingualNeural", 0.0, 1.0),
            ("en-US-BrianMultilingualNeural", 0.0, 1.0),
            ("ru-RU-DmitryNeural", -2.4, 1.0),
            ("en-US-AndrewMultilingualNeural", 1.9, 0.98),
        ],
        "en": [  # здесь каталог щедр: десять настоящих разных людей
            ("en-US-JennyNeural", 0.0, 1.0),
            ("en-US-AriaNeural", 0.0, 1.0),
            ("en-US-AvaNeural", 0.0, 1.0),
            ("en-US-EmmaNeural", 0.0, 1.0),
            ("en-US-MichelleNeural", 0.0, 1.0),
            ("en-US-GuyNeural", 0.0, 1.0),
            ("en-US-ChristopherNeural", 0.0, 1.0),
            ("en-US-BrianNeural", 0.0, 1.0),
            ("en-US-SteffanNeural", 0.0, 1.0),
            ("en-US-RogerNeural", 0.0, 1.0),
        ],
    }
    # Голоса, отказавшие живому полигону на русском (NoAudioReceived), никогда
    # не должны вернуться в ростер — гейт тестов сторожит этот список.
    RU_REFUSED = frozenset({
        "en-US-AriaNeural", "en-US-JennyNeural", "en-US-GuyNeural",
        "en-US-SteffanNeural", "en-US-RogerNeural", "en-US-ChristopherNeural",
        "en-US-MichelleNeural", "en-US-EmmaNeural", "en-US-AvaNeural",
        "en-US-AnaNeural",
    })
    name = "edge-tts"

    # обратная совместимость старого гейта: плоские имена по языку
    @property
    def VOICES(self) -> dict[str, list[str]]:  # noqa: N802 (наследованное имя гейта)
        return {lang: [s for s, _p, _t in rows] for lang, rows in self.ROSTERS.items()}

    def _entry(self, lang: str, speaker: int) -> tuple[str, float, float]:
        seq = self.ROSTERS.get(lang) or self.ROSTERS["ru"]
        if speaker and speaker > 0:
            return seq[(speaker - 1) % len(seq)]   # 1-based diarization -> 0-based
        return seq[0]                              # no speaker map -> primary voice

    def synthesize(self, text: str, lang: str, out_path: Path, dur_sec: float,
                   speaker: int = 0) -> None:
        import asyncio

        import edge_tts  # опциональная зависимость: pip install edge-tts

        voice, pitch, tempo = self._entry(lang, speaker)

        async def _run() -> None:
            com = edge_tts.Communicate(text, voice)
            await com.save(str(out_path))

        asyncio.run(_run())
        if (pitch or tempo != 1.0) and not apply_persona(Path(out_path), pitch, tempo):
            # ffmpeg пропал — голос останется подлинным, не выдумкой и не падением;
            # mixer честно отчитается числом persona_fallback.
            self._persona_failures = getattr(self, "_persona_failures", 0) + 1


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
    out["operator"]["voice_slots"] = {lang: len(rows) for lang, rows in EdgeTTS.ROSTERS.items()}
    return out


def get_tts() -> StubTTS | EdgeTTS:
    """The provider to use now — see `status()` for why it is the one."""
    if status()["mode"] == "real":
        return EdgeTTS()
    return StubTTS()
