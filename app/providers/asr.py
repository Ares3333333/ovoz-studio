"""ASR-провайдеры: офлайн-демо и реальный whisper.

Два реальных диалекта, потому что на практике ставят и то и другое:
`openai/whisper` (Python CLI) и `whisper.cpp` (`whisper-cli`, без Python вообще).
У них разные аргументы И разные JSON-ы, так что «whisper установлен» — это не один
факт, а два: найден бинарник и понятен его диалект.

И третье, для чего этот файл написан честно: `status()` отделеляет *заявленное*
от *доказуемого*. Если админ включил реальный ASR, а бинарника нет, молча
возвращать симуляцию — значит выдать клиенту выдуманный текст за его деньги.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from ..config import settings
from ..ling import srt as srt_mod
from .base import Segment

TEXT_SUFFIXES = {".txt", ".srt", ".vtt", ".md"}
DIALECTS = ("auto", "openai", "whispercpp")
# Binaries that speak the whisper.cpp dialect; `main` is its historical name.
_CPP_NAMES = ("whisper-cli", "whisper.cpp", "whisper-cpp", "main")

# Uzbek is not in openai-whisper's shorthand list, and passing a language it does
# not know makes it transcribe in Russian instead of refusing.
_CPP_LANG = {"uz": "uz", "kk": "kk", "ky": "ky", "tg": "tg"}


class SimASR:
    """Офлайн-режим: принимает транскрипт (txt/srt) или генерирует демо-сегменты.

    Это позволяет прогонять весь пайплайн end-to-end без моделей и без ключей —
    критично для MVP-демо и тестов.
    """

    name = "sim-asr"

    def transcribe(self, audio_path: Path, lang: str) -> list[Segment]:
        p = Path(audio_path)
        if p.suffix.lower() in TEXT_SUFFIXES:
            return self._from_text(p)
        sidecar = p.with_suffix(".txt")
        if sidecar.exists():
            return self._from_text(sidecar)
        # чистое аудио без транскрипта — честная демо-заглушка
        return [
            Segment(0.0, 4.0, "[демо] Assalomu alaykum, bugun bizning mahsulot haqida gapiramiz."),
            Segment(4.0, 8.0, "[демо] Buyurtma berish uchun Telegram botimizga yozing."),
        ]

    def invents_text(self, audio_path: Path) -> bool:
        """True when this provider will hand back words nobody spoke.

        A text upload is not invention — the customer's own transcript is read.
        Silence on audio is: the job still completes, and the UI has to say so.
        """
        p = Path(audio_path)
        if p.suffix.lower() in TEXT_SUFFIXES:
            return False
        return not p.with_suffix(".txt").exists()

    def _from_text(self, p: Path) -> list[Segment]:
        content = p.read_text(encoding="utf-8", errors="replace")
        if p.suffix.lower() in {".srt", ".vtt"}:
            cues = srt_mod.parse_srt(content)
            if cues:
                return [Segment(c.start, c.end, c.text.replace("\n", " ")) for c in cues]
        segs: list[Segment] = []
        t = 0.0
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            # поддержка формата "start-end|текст"
            if "|" in line:
                head, _, body = line.partition("|")
                try:
                    a, b = head.replace("--", "-").split("-")
                    segs.append(Segment(float(a), float(b), body.strip()))
                    t = float(b)
                    continue
                except ValueError:
                    pass
            dur = max(2.0, min(6.0, len(line) / 12.0))
            segs.append(Segment(round(t, 2), round(t + dur, 2), line))
            t += dur
        return segs


class WhisperASR:
    """Реальный ASR через CLI — в одном из двух диалектов."""

    name = "whisper"

    def __init__(self, binary: str, dialect: str = "auto", model: str = "") -> None:
        self.binary = binary
        self.model = model
        self.dialect = self._resolve(dialect)

    def _resolve(self, dialect: str) -> str:
        if dialect in ("openai", "whispercpp"):
            return dialect
        # `auto` decides on evidence, not on hope: an explicit model file is the
        # signature of whisper.cpp, as is the binary's own name.
        base = os.path.basename(self.binary or "").lower()
        if base in _CPP_NAMES or (self.model or "").lower().endswith(".bin"):
            return "whispercpp"
        return "openai"

    def command(self, audio_path: Path, out_dir: Path, lang: str) -> list[str]:
        if self.dialect == "whispercpp":
            stem = out_dir / (Path(audio_path).stem + ".out")
            cmd = [self.binary, "-m", self.model, "-f", str(audio_path),
                   "-oj", "-of", str(stem)]
            if lang in _CPP_LANG:
                cmd += ["--language", _CPP_LANG[lang]]
            return cmd
        cmd = [self.binary, str(audio_path), "--output_format", "json",
               "--output_dir", str(out_dir)]
        if lang in {"uz", "kk", "ky", "tg"}:
            cmd += ["--language", "uzbek"]
        return cmd

    def parse(self, data: dict) -> list[Segment]:
        """Both schemas, one contract: float seconds, stripped text.

        whisper.cpp counts milliseconds in `offsets`; openai-whisper counts float
        seconds in `start`/`end`. Mixing them up turns a 90-second tape into a
        90-hour one without any error, so the units are named here instead of being
        guessed downstream.
        """
        rows = data.get("transcription")
        if rows is not None:                      # whisper.cpp
            return [Segment(round(float(o["offsets"]["from"]) / 1000.0, 3),
                            round(float(o["offsets"]["to"]) / 1000.0, 3),
                            str(o.get("text", "")).strip())
                    for o in rows if str(o.get("text", "")).strip()]
        return [Segment(float(s["start"]), float(s["end"]), str(s["text"]).strip())
                for s in data.get("segments", []) if str(s.get("text", "")).strip()]

    def transcribe(self, audio_path: Path, lang: str) -> list[Segment]:
        # изолированный выходной каталог на job: не global glob на весь uploads/
        out_dir = audio_path.parent / f".asr_tmp_{audio_path.stem}"
        out_dir.mkdir(exist_ok=True)
        try:
            cmd = self.command(audio_path, out_dir, lang)
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if proc.returncode != 0:
                raise RuntimeError(f"whisper failed: {proc.stderr[:500]}")
            # ищем JSON именно в нашем изолированном dir
            candidates = sorted(out_dir.glob("*.json"))
            if not candidates:
                raise RuntimeError("whisper produced no json output")
            data = json.loads(candidates[0].read_text(encoding="utf-8"))
            # Empty output is a real outcome (a silent tape), not a crash: the
            # pipeline reports zero segments and the customer sees that.
            return self.parse(data)
        finally:
            # убираем временные файлы
            shutil.rmtree(out_dir, ignore_errors=True)


def status() -> dict:
    """What the deployment claims about ASR against what it can prove right now.

    Two audiences, two halves of the answer. `code`/`reason` are safe to show a
    customer and an anonymous SDK: a *variable name* is documentation, a *value* is
    topology. `operator` holds the binary, the model path and what was found — it
    belongs to the admin endpoint and the log, never to `/api/v1/info`: a stranger
    learning which whisper build and which model path this host uses has just been
    handed the shape of the machine.
    """
    provider = settings.asr_provider
    binary = settings.whisper_bin
    model = settings.whisper_model
    dialect = settings.asr_dialect if settings.asr_dialect in DIALECTS else "auto"
    found = bool(binary) and bool(shutil.which(binary))
    out = {"provider": provider, "mode": "sim", "code": "", "reason": "",
           "operator": {"binary": binary or None, "binary_found": found,
                        "dialect": dialect, "model": model or None,
                        "model_found": bool(model) and Path(model).exists()}}
    if provider != "real":
        out["code"], out["reason"] = "no_provider", "demo transcription (no provider configured)"
        return out
    if not binary:
        out["code"] = "no_binary_configured"
        out["reason"] = "OVOZ_ASR_PROVIDER=real without OVOZ_WHISPER_BIN"
        return out
    if not found:
        out["code"] = "binary_missing"
        out["reason"] = "the configured OVOZ_WHISPER_BIN is not installed on this host"
        return out
    asr = WhisperASR(binary, dialect, model)
    out["operator"]["dialect"] = asr.dialect
    if asr.dialect == "whispercpp":
        if not model:
            out["code"] = "no_model_configured"
            out["reason"] = "whisper.cpp needs OVOZ_WHISPER_MODEL"
            return out
        if not Path(model).exists():
            out["code"], out["reason"] = "model_missing", "the configured OVOZ_WHISPER_MODEL is not readable"
            return out
    out["mode"], out["code"], out["reason"] = "real", "ok", ""
    return out


def get_asr() -> SimASR | WhisperASR:
    """The provider to use now — see `status()` for why it is the one."""
    st = status()
    if st["mode"] == "real":
        op = st["operator"]
        return WhisperASR(op["binary"], op["dialect"], op["model"] or "")
    return SimASR()
