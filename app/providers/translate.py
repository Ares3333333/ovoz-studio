"""Переводчики: офлайн-демо (словарь + нормализация) и LLM-адаптер (OpenAI-совместимый)."""
from __future__ import annotations

import httpx

from ..config import settings
from ..ling.romanizer import cyr2lat, normalize_uzbek

# Мини-словарь demo-режима (ru → uz / uz → ru). Реальное качество обеспечивает LLM-адаптер.
RU_UZ = {
    "здравствуйте": "assalomu alaykum",
    "спасибо": "rahmat",
    "сегодня": "bugun",
    "завтра": "erta",
    "магазин": "do'kon",
    "цена": "narx",
    "скидка": "chegirma",
    "заказ": "buyurtma",
    "доставка": "yetkazib berish",
    "телефон": "telefon",
    "время": "vaqt",
    "деньги": "pul",
    "друг": "do'st",
    "дом": "uy",
    "вода": "suv",
    "хлеб": "non",
    "новый": "yangi",
    "хороший": "yaxshi",
    "большой": "katta",
    "клиент": "mijoz",
    "услуга": "xizmat",
    "оплата": "to'lov",
}
UZ_RU = {v: k for k, v in RU_UZ.items()}


class DemoTranslate:
    """Словарный перевод + нормализация узбекской кириллицы/апострофов.

    Демо-режим для офлайна и тестов: показывает весь контракт пайплайна,
    не выдавая себя за продакшн-качество (честно маркируется провайдером).
    """

    name = "demo-dict"

    def translate(self, text: str, src: str, tgt: str) -> str:
        if src == "uz":
            text = normalize_uzbek(cyr2lat(text)) if _has_cyr(text) else normalize_uzbek(text)
        out_words = []
        for word in text.split(" "):
            core = word.strip(".,!?;:«»()")
            low = core.lower()
            table = RU_UZ if (src == "ru" and tgt == "uz") else UZ_RU if (src == "uz" and tgt == "ru") else {}
            rep = table.get(low)
            if rep:
                if core[:1].isupper():
                    rep = rep[:1].upper() + rep[1:]
                out_words.append(word.replace(core, rep))
            else:
                out_words.append(word)
        return " ".join(out_words)


class OpenAITranslate:
    """Боевой режим: любой OpenAI-совместимый endpoint с узбекскими инструкциями."""

    name = "openai"

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def translate(self, text: str, src: str, tgt: str) -> str:
        lang_names = {"uz": "Uzbek (modern Latin script, o'/g' apostrophes)",
                      "ru": "Russian", "en": "English", "kk": "Kazakh", "ky": "Kyrgyz"}
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": (
                        f"You are a professional subtitle translator. Translate from "
                        f"{lang_names.get(src, src)} to {lang_names.get(tgt, tgt)}. "
                        "Keep numbers, names and the meaning of slang. Return ONLY the translation."
                    )},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.2,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


def _has_cyr(text: str) -> bool:
    return any("Ѐ" <= ch.lower() <= "ӿ" for ch in text)


def status() -> dict:
    """Claimed against provable, like the ASR self-check: a configured provider with
    no key is a demo dictionary, and the customer's translation is worse for it.

    `code`/`reason` are public-safe (a variable name is documentation); which model
    is named and whether a key exists is operator detail — the key itself never
    leaves this process in any form.
    """
    provider = settings.translate_provider
    out = {"provider": provider, "mode": "sim", "code": "", "reason": "",
           "operator": {"model": settings.openai_model,
                        "key_present": bool(settings.openai_api_key)}}
    if provider != "openai":
        out["code"], out["reason"] = "no_provider", "demo dictionary (no provider configured)"
        return out
    if not out["operator"]["key_present"]:
        out["code"] = "no_key"
        out["reason"] = "OVOZ_TRANSLATE_PROVIDER=openai without OPENAI_API_KEY"
        return out
    out["mode"], out["code"] = "real", "ok"
    return out


def get_translate() -> DemoTranslate | OpenAITranslate:
    """The provider to use now — see `status()` for why it is the one."""
    if status()["mode"] == "real":
        return OpenAITranslate(settings.openai_api_key, settings.openai_model)
    return DemoTranslate()
