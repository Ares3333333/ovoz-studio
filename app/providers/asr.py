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

import importlib.util
import json
import os
import shutil
import subprocess
import threading
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


class FasterWhisperASR:
    """Реальный офлайн-ASR через faster-whisper (CTranslate2, CPU, без torch).

    В отличие от CLI-диалекта, не требует внешнего бинарника: тянет модель из
    локального HF-кэша при первом запуске и после — работает полностью офлайн.
    Модель грузится лениво и переиспользуется между задачами одного процесса
    (синглтон ниже), иначе каждый job платил бы ~10 с за загрузку весов."""

    name = "faster-whisper"

    def __init__(self, model_size: str, compute: str = "int8", device: str = "cpu") -> None:
        self.model_size = model_size
        self.compute = compute
        self.device = device
        self._model = None

    def _load(self):
        # Double-checked under a module lock: up to GLOBAL_WORKER_SLOTS jobs start
        # concurrently in the worker pool, and an unguarded lazy load let 8 cold jobs
        # each build a full WhisperModel (hundreds of MB ~ GB each) -> duplicated the
        # very ~10 s load this cache exists to avoid, or an OOM that kills the process
        # and every job in it. The lock makes the cold path load exactly once.
        if self._model is None:
            with _FASTER_LOCK:
                if self._model is None:
                    try:
                        from faster_whisper import WhisperModel
                        self._model = WhisperModel(self.model_size, device=self.device,
                                                   compute_type=self.compute)
                    except Exception as exc:  # noqa: BLE001
                        # The raw CTranslate2/HF error embeds the cache path and model
                        # id (operator topology). Normalize to a variable-name sentence
                        # so a failed load on a customer's job cannot leak the machine.
                        raise RuntimeError(
                            "faster-whisper could not load the configured "
                            "OVOZ_ASR_MODEL on CPU") from exc
        return self._model

    def transcribe(self, audio_path: Path, lang: str) -> list[Segment]:
        # Text is not speech to decode: a .txt/.srt/.vtt upload (and a .txt sidecar)
        # is the customer's own transcript and must go through the same path SimASR
        # uses, or the recommended no-key provider would crash every document/SRT job
        # the moment ASR is switched to faster-whisper. Only real audio hits the model.
        p = Path(audio_path)
        if p.suffix.lower() in TEXT_SUFFIXES or p.with_suffix(".txt").exists():
            return SimASR()._from_text(p)
        model = self._load()
        segments, _info = model.transcribe(str(audio_path),
                                           language=lang or None, beam_size=5)
        out: list[Segment] = []
        for s in segments:
            text = str(getattr(s, "text", "")).strip()
            if text:
                out.append(Segment(round(float(s.start), 3),
                                   round(float(s.end), 3), text))
        return out


# One loaded model per (size, compute, device): the weights are big and slow to
# load, and a worker process runs many jobs, so the instance is cached deliberately.
_FASTER_SINGLETON: dict[tuple, FasterWhisperASR] = {}
_FASTER_LOCK = threading.Lock()


def _package_present(name: str) -> bool:
    """A seam so status()/tests can ask 'is the package there' without mutating the
    stdlib importlib (which would leak into every other import during a test)."""
    return importlib.util.find_spec(name) is not None


def _faster_provider(model_size: str, compute: str, device: str) -> FasterWhisperASR:
    key = (model_size, compute, device)
    prov = _FASTER_SINGLETON.get(key)
    if prov is None:
        with _FASTER_LOCK:
            # setdefault keeps it to one instance even if another thread just made one.
            prov = _FASTER_SINGLETON.setdefault(
                key, FasterWhisperASR(model_size, compute, device))
    return prov


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
    pkg = _package_present("faster_whisper")
    out = {"provider": provider, "mode": "sim", "code": "", "reason": "",
           "operator": {"binary": binary or None, "binary_found": found,
                        "dialect": dialect, "model": model or None,
                        "model_found": bool(model) and Path(model).exists(),
                        "faster_package": pkg, "faster_model": settings.asr_model or None}}
    # faster-whisper is a Python provider judged by the package + a chosen model
    # size, not by a CLI binary. The weights download once, then serve offline.
    if provider == "faster":
        if not pkg:
            out["code"] = "package_missing"
            out["reason"] = "OVOZ_ASR_PROVIDER=faster but faster-whisper is not installed"
            return out
        if not settings.asr_model:
            out["code"] = "no_model_configured"
            out["reason"] = ("OVOZ_ASR_PROVIDER=faster needs OVOZ_ASR_MODEL "
                             "(tiny/base/small/medium/large-v3)")
            return out
        out["mode"], out["code"], out["reason"] = "real", "ok", ""
        return out
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


def get_asr() -> SimASR | WhisperASR | FasterWhisperASR:
    """The provider to use now — see `status()` for why it is the one."""
    st = status()
    if st["mode"] == "real":
        if st["provider"] == "faster":
            return _faster_provider(st["operator"]["faster_model"],
                                    settings.asr_compute, "cpu")
        op = st["operator"]
        return WhisperASR(op["binary"], op["dialect"], op["model"] or "")
    return SimASR()
