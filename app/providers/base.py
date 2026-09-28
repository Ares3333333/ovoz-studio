"""Базовые типы и контракты провайдеров."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class Segment:
    start: float
    end: float
    text: str


class ASRProvider(Protocol):
    name: str

    def transcribe(self, audio_path: Path, lang: str) -> list[Segment]: ...


class TranslateProvider(Protocol):
    name: str

    def translate(self, text: str, src: str, tgt: str) -> str: ...


class TTSProvider(Protocol):
    name: str

    def synthesize(self, text: str, lang: str, out_path: Path, dur_sec: float,
                   speaker: int = 0) -> None: ...
