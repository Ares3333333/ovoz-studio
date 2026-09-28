"""Общие фикстуры: изолированный data-каталог и синхронный пайплайн для тестов."""
from __future__ import annotations

import itertools
import os
import sys
import tempfile
from pathlib import Path

TEST_DATA = Path(tempfile.mkdtemp(prefix="ovoz-test-"))
os.environ["OVOZ_DATA_DIR"] = str(TEST_DATA)
os.environ["OVOZ_SYNC_PIPELINE"] = "1"
os.environ["OVOZ_ASR_PROVIDER"] = "sim"
os.environ["OVOZ_TRANSLATE_PROVIDER"] = "sim"
os.environ["OVOZ_TTS_PROVIDER"] = "sim"
os.environ["OVOZ_WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["OVOZ_ADMIN_SECRET"] = "test-admin-key"
os.environ["OVOZ_ALLOW_DEMO_CREDIT"] = "1"  # enable for test suite
os.environ["OVOZ_ENDPOINT_RATE_LIMIT"] = "0"  # off: IP is shared 'testclient' across the session

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


_CONTACT_SEQ = itertools.count(1)


def unique_digits() -> str:
    """Digits that stay unique after the server normalizes a contact.

    `uuid4().hex[:8]` looked like plenty of entropy, but _normalize_contact keeps
    digits only: every letter vanishes, ~200 registrations fall into a space of
    roughly 10^5, and the suite started failing at random with 409 "Contact
    already registered". A counter has no such tail.
    """
    return f"{next(_CONTACT_SEQ):06d}"


def unique_contact(prefix: str = "+99890") -> str:
    return prefix + unique_digits()


@pytest.fixture(scope="session")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_telegram_replay_guard():
    """Single-use initData guard глобален; чистим между тестами."""
    from app import telegram as tg
    tg._CONSUMED.clear()
    yield
    tg._CONSUMED.clear()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """in-memory _FAILS живёт между тестами — чистим, иначе register-throttle
    с TestClient IP='testclient' убьёт весь suite после 8-й регистрации."""
    from app import main as m
    with m._FAIL_LOCK:
        m._FAILS.clear()
    with m._EP_LOCK:
        m._ENDPOINT_BUCKETS.clear()
    yield
    with m._FAIL_LOCK:
        m._FAILS.clear()
    with m._EP_LOCK:
        m._ENDPOINT_BUCKETS.clear()


@pytest.fixture()
def auth(client):
    """Новый пользователь с балансом 10 минут (signup bonus) и валидным секретом."""
    resp = client.post("/api/auth/register",
                       data={"name": "Test", "contact": unique_contact(), "secret": "test-secret-XX"})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}
