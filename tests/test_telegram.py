"""Telegram Mini App auth: криптографическая верификация initData."""
import hashlib
import hmac
import json
import time
from urllib.parse import quote

FAKE_TOKEN = "123456:ABC-DEF-fake-token-for-tests"


def make_init_data(user: dict, token: str = FAKE_TOKEN, auth_ts: int | None = None) -> str:
    params = {
        "auth_date": str(auth_ts if auth_ts is not None else int(time.time())),
        "query_id": "AAH6lMAZ",
        "user": json.dumps(user, ensure_ascii=False),
    }
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return "&".join(f"{k}={quote(str(v))}" for k, v in params.items()) + f"&hash={h}"


TG_USER = {"id": 777001, "first_name": "Aziza", "last_name": "Karimova",
           "username": "aziza_k", "language_code": "uz"}


def test_telegram_auth_501_without_token(client, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    import app.telegram as tg
    monkeypatch.setattr(tg.settings, "telegram_bot_token", "", raising=False)
    r = client.post("/api/auth/telegram", data={"init_data": make_init_data(TG_USER)})
    assert r.status_code == 501


def test_telegram_auth_flow_and_repeat(client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    init = make_init_data(TG_USER)
    r = client.post("/api/auth/telegram", data={"init_data": init})
    assert r.status_code == 200
    body = r.json()
    assert body["is_new"] is True
    assert body["user"]["contact"] == "tg:777001"
    assert body["user"]["name"] == "Aziza Karimova"
    H = {"Authorization": f"Bearer {body['token']}"}
    bal = client.get("/api/me", headers=H).json()["balance_minutes"]
    assert bal == 10  # бонус начислен один раз

    # повторный вход — но с ДРУГИМ auth_date (как будто пользователь вышел и зашёл снова)
    # (single-use guard ловит буквально тот же initData — это отдельный тест)
    init2 = make_init_data(TG_USER, auth_ts=int(time.time()) + 1)
    r2 = client.post("/api/auth/telegram", data={"init_data": init2})
    assert r2.status_code == 200
    assert r2.json()["is_new"] is False
    H2 = {"Authorization": f"Bearer {r2.json()['token']}"}
    assert client.get("/api/me", headers=H2).json()["balance_minutes"] == 10  # без дабла


def test_telegram_init_data_single_use(client, monkeypatch):
    """M11: буквально тот же initData со вторым запросом не должен давать токен."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    init = make_init_data({"id": 777099, "first_name": "Single"})
    r1 = client.post("/api/auth/telegram", data={"init_data": init})
    assert r1.status_code == 200
    r2 = client.post("/api/auth/telegram", data={"init_data": init})
    assert r2.status_code == 403  # replay


def test_telegram_auth_rejects_bad_hash(client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    bad = make_init_data(TG_USER) + "0000"  # ломаем подпись hash
    r = client.post("/api/auth/telegram", data={"init_data": bad})
    assert r.status_code == 403


def test_telegram_auth_rejects_stale_auth_date(client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    stale = {
        "auth_date": str(int(time.time()) - 999999),
        "user": json.dumps(TG_USER),
    }
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(stale.items()))
    secret = hmac.new(b"WebAppData", FAKE_TOKEN.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    init = f"auth_date={stale['auth_date']}&user={quote(stale['user'])}&hash={h}"
    r = client.post("/api/auth/telegram", data={"init_data": init})
    assert r.status_code == 403


def test_verify_unit_level():
    from app.telegram import verify_init_data
    assert verify_init_data(make_init_data(TG_USER), FAKE_TOKEN) is not None
    assert verify_init_data("user=%7B%7D&hash=deadbeef", FAKE_TOKEN) is None
    assert verify_init_data("", FAKE_TOKEN) is None
    assert verify_init_data(make_init_data(TG_USER), "wrong:token") is None
