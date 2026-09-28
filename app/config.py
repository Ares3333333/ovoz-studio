"""Ovoz Studio — конфигурация окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def webhook_secret() -> str:
    """HMAC-секрет подписи платёжных webhook'ов (читается в рантайме)."""
    return _env("OVOZ_WEBHOOK_SECRET")


def admin_secret() -> str:
    """Ключ админ/демо-эндпоинтов (начисление демо-кредитов). Рантайм."""
    return _env("OVOZ_ADMIN_SECRET")


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("OVOZ_DATA_DIR", "./data")).resolve())

    asr_provider: str = field(default_factory=lambda: _env("OVOZ_ASR_PROVIDER", "sim"))
    whisper_bin: str = field(default_factory=lambda: _env("OVOZ_WHISPER_BIN"))
    # Which CLI the binary speaks: openai/whisper (`--output_format json`) or
    # whisper.cpp (`-m model -f in -oj -of stem`). Their arguments and their JSON
    # are both different, so "whisper installed" is not one fact, it is two.
    asr_dialect: str = field(default_factory=lambda: _env("OVOZ_ASR_DIALECT", "auto"))
    whisper_model: str = field(default_factory=lambda: _env("OVOZ_WHISPER_MODEL"))
    # faster-whisper (Python, CPU, no torch): the model *size* is fetched from the
    # local HF cache on first use and then works fully offline. Only consulted when
    # asr_provider == "faster"; an empty value means "not configured for faster".
    asr_model: str = field(default_factory=lambda: _env("OVOZ_ASR_MODEL"))
    asr_compute: str = field(default_factory=lambda: _env("OVOZ_ASR_COMPUTE", "int8"))

    translate_provider: str = field(default_factory=lambda: _env("OVOZ_TRANSLATE_PROVIDER", "sim"))
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openai_model: str = field(default_factory=lambda: _env("OVOZ_OPENAI_MODEL", "gpt-4o-mini"))

    tts_provider: str = field(default_factory=lambda: _env("OVOZ_TTS_PROVIDER", "sim"))

    ffprobe_bin: str = field(default_factory=lambda: _env("OVOZ_FFPROBE_BIN", "ffprobe"))
    ffmpeg_bin: str = field(default_factory=lambda: _env("OVOZ_FFMPEG_BIN", "ffmpeg"))
    # Токен Telegram-бота для Mini App; читается и в рантайме (тесты подменяют env)
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))

    sync_pipeline: bool = field(default_factory=lambda: _env("OVOZ_SYNC_PIPELINE", "0") == "1")
    signup_bonus_minutes: float = field(
        default_factory=lambda: float(_env("OVOZ_SIGNUP_BONUS_MINUTES", "10"))
    )
    # сколько дней хранить файлы завершённых jobs (0 = не чистить)
    retention_days: int = field(
        default_factory=lambda: int(_env("OVOZ_RETENTION_DAYS", "7"))
    )

    # --- производные пути ---
    @property
    def db_path(self) -> Path:
        return self.data_dir / "ovoz.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.uploads_dir, self.artifacts_dir):
            p.mkdir(parents=True, exist_ok=True)


settings = Settings()
