"""Billing-движок: кредиты = минуты обработки, ledger-first, идемпотентные платежи.

Единица измерения совпадает с продуктом («минуты видео/аудио») — так прозрачнее
для пользователя и легче считать юнит-экономику.
"""
from __future__ import annotations

from . import db

PLANS = {
    "free":  {"name": "Boshlang'ich",  "price_usd": 0,  "minutes": 10},
    "pro":   {"name": "Pro",           "price_usd": 9,  "minutes": 120},
    "studio": {"name": "Studio",       "price_usd": 25, "minutes": 500},
    # локальные валюты: сумма в минорных единицах PSP
    # Payme/Click: UZS в тийинах (×100), RUB в копейках (×100)
    "pro_uzs": {"price_minor": 12_000_000, "minutes": 120, "currency": "UZS", "plan_id": "pro"},  # 120 000 UZS
    "pro_rub": {"price_minor": 90_000,  "minutes": 120, "currency": "RUB", "plan_id": "pro"},     # 900 RUB
    "studio_uzs": {"price_minor": 30_000_000, "minutes": 500, "currency": "UZS", "plan_id": "studio"},
    "studio_rub": {"price_minor": 250_000, "minutes": 500, "currency": "RUB", "plan_id": "studio"},
}


def balance(uid: str) -> float:
    row = db.get_conn().execute(
        "SELECT COALESCE(SUM(delta), 0) AS b FROM ledger WHERE user_id = ?", (uid,)
    ).fetchone()
    return float(row["b"] or 0)


def credit(uid: str, minutes: float, reason: str, ref: str | None = None) -> None:
    cur = db.get_conn()
    cur.execute(
        "INSERT INTO ledger (user_id, delta, reason, ref, created_at) VALUES (?,?,?,?,?)",
        (uid, minutes, reason, ref, db.now_iso()),
    )
    cur.commit()


def debit(uid: str, minutes: float, reason: str, ref: str | None = None) -> bool:
    """Списать минуты атомарно; False, если баланс недостаточен."""
    return db.debit_atomic(uid, minutes, reason, ref)


def charge_for_job(job: dict) -> bool:
    """Оплата job по факту: списание минут при создании, возврат при ошибке."""
    return debit(job["user_id"], job["minutes"], f"job:{job['id']}", ref=job["id"])


def refund_job(job: dict, reason: str = "refund:failed") -> bool:
    """Возврат за job. Двойной возврат исключён CAS-переходами статусов
    (cancel/retry/fail выигрывает только один), поэтому broad-guard здесь
    вредил бы: после retry нужен НОВЫЙ возврат с тем же ref."""
    credit(job["user_id"], job["minutes"], reason, ref=job["id"])
    return True


def apply_payment_webhook(provider: str, external_id: str, uid: str,
                          amount_minor: int, currency: str, minutes: float) -> dict:
    """Идемпотентная обработка уведомления ПС (Payme/Click/Telegram Stars).

    Возвращает error='unmatched_amount', если сумма не совпала ни с одним
    тарифом. HTTP-слой превращает это в 422 + ручной разбор.
    Повторный webhook с тем же external_id не начисляет кредиты второй раз.
    При успешной оплате апгрейдит план пользователя.
    """
    existing = db.find_payment(external_id)
    if existing:
        return {"status": "duplicate", "payment": existing}
    plan_key, plan = _plan_for_price(provider, amount_minor, currency)
    if plan is None:
        return {"status": "error", "error": "unmatched_amount",
                "amount_minor": amount_minor, "currency": currency}
    credited_minutes = float(plan["minutes"])
    # атомарная вставка: concurrent PSP retries won't double-credit
    try:
        payment = db.create_payment(uid, provider, external_id, amount_minor, currency,
                                     credited_minutes)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            # lost race: another thread inserted it first
            dup = db.find_payment(external_id)
            return {"status": "duplicate", "payment": dup or {}}
        raise
    credit(uid, credited_minutes, f"payment:{provider}", ref=external_id)
    # Upgrade user plan if mapped (never downgrade)
    upgrade_to = plan.get("plan_id") or plan_key
    if upgrade_to and upgrade_to != "free":
        current = db.get_user(uid) or {}
        current_plan = current.get("plan", "free")
        if PLAN_PRIORITY.get(upgrade_to, 0) >= PLAN_PRIORITY.get(current_plan, 0):
            db.set_user_plan(uid, upgrade_to)
    return {"status": "ok", "payment": payment}


MAX_WEBHOOK_MINUTES = 600.0  # страхующий потолок на одно начисление

# Plan priority: never downgrade on payment
PLAN_PRIORITY = {"free": 0, "pro": 1, "studio": 2}


def _plan_for_price(provider: str, amount_minor: int, currency: str) -> tuple[str | None, dict | None]:
    """Сопоставление суммы платежа с тарифом (суммы в минорах: тийин/копейка).
    Возвращает (plan_key, plan_dict) или (None, None).
    Нулевые/отрицательные суммы не матчатся никогда — иначе free-план (0$)
    превращается в бесконечный генератор кредитов."""
    if amount_minor <= 0:
        return None, None
    currency = currency.upper()
    for key, plan in PLANS.items():
        if plan.get("currency") == currency and plan.get("price_minor") == amount_minor:
            return key, plan
        price_usd = plan.get("price_usd")
        if currency == "USD" and price_usd and price_usd > 0 \
                and abs(price_usd * 100 - amount_minor) < 1e-9:
            return key, plan
    return None, None
