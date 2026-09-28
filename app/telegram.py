"""Верификация initData Telegram Mini App (официальная схема HMAC-SHA256).

Спека: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
secret_key = HMAC256(key="WebAppData", msg=BOT_TOKEN)
hash       = HMAC256(secret_key, data_check_string).hexdigest()

Меры против replay: max_age 6ч, reject auth_date в будущем, single-use
по (tg_id, auth_date).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

from .config import settings

MAX_AGE_SEC = 6 * 3600        # окно replay — 6 часов
FUTURE_TOLERANCE_SEC = 90     # допуск на часы клиента
_CONSUMED: set[tuple[str, int]] = set()  # (tg_id, auth_date) — single-use guard


def bot_token() -> str:
    # читаем env в рантайме: токен может быть выдан после старта сервера
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or settings.telegram_bot_token


def verify_init_data(init_data: str, token: str | None = None,
                     max_age: int = MAX_AGE_SEC) -> dict | None:
    """Возвращает словарь с данными пользователя либо None.
    None = подпись не сошлась / протухла / из будущего / уже использована."""
    token = token or bot_token()
    if not token or not init_data:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        return None
    try:
        auth_ts = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    now = time.time()
    if auth_ts - now > FUTURE_TOLERANCE_SEC:
        return None
    if now - auth_ts > max_age:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        return None
    try:
        user = json.loads(pairs.get("user", "{}"))
    except ValueError:
        return None
    if not isinstance(user, dict) or "id" not in user:
        return None
    # single-use: запоминаем пару (tg_id, auth_date)
    key = (str(user["id"]), auth_ts)
    if key in _CONSUMED:
        return None
    if len(_CONSUMED) > 10_000:  # грубый LRU, чтобы не разрасталось
        _CONSUMED.clear()
    _CONSUMED.add(key)
    first_last = " ".join(filter(None, [user.get("first_name", ""), user.get("last_name", "")])).strip()
    return {"tg_id": str(user["id"]),
            "name": first_last or user.get("username", "Telegram user"),
            "username": user.get("username", "")}
