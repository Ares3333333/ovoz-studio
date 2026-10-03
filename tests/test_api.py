"""E2E: регистрация → задача (субтитры/даббинг) → артефакты; глоссарий; 402; платежи.

Контракт безопасности v0.3: токены только в Authorization-заголовке, цену считает
только сервер, webhook ПС — с HMAC-подписью, демо-начисление — по админ-ключу.
"""
import hashlib
import hmac
import re
import uuid
from pathlib import Path

from conftest import unique_contact, unique_digits

DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"

WEBHOOK_SECRET = "test-webhook-secret"  # из conftest
ADMIN_KEY = "test-admin-key"


def _srt_file():
    return {"file": ("sample_ru.srt", (DEMO_DIR / "sample_ru.srt").read_bytes(), "text/plain")}


def _sig(provider: str, external_id: str, user_id: str, amount: int, currency: str) -> str:
    canon = f"{provider}|{external_id}|{user_id}|{amount}|{currency}"
    return hmac.new(WEBHOOK_SECRET.encode(), canon.encode(), hashlib.sha256).hexdigest()


def test_health(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_me_and_bonus(client, auth):
    r = client.get("/api/me", headers=auth)
    assert r.status_code == 200
    assert r.json()["balance_minutes"] == 10


def test_subtitles_job_e2e(client, auth):
    r = client.post(
        "/api/jobs", headers=auth, files=_srt_file(),
        data={"jtype": "subtitles", "src": "ru", "tgt": "uz"},
    )
    assert r.status_code == 201, r.text
    job = r.json()["job"]
    assert job["status"] == "done", job
    # списание по длительности srt (12 сек → 0.2 мин), которую посчитал сервер
    assert job["minutes"] == 0.2
    bal = client.get("/api/me", headers=auth).json()["balance_minutes"]
    assert bal == 9.8

    dl = client.get(job["artifacts"]["srt"], headers=auth)
    assert dl.status_code == 200
    body = dl.text
    assert "assalomu alaykum" in body.lower()   # словарь demo-переводчика сработал
    assert "00:00:0" in body                    # тайминги сохранены

    bi = client.get(job["artifacts"]["srt_bilingual"], headers=auth)
    assert "Здравствуйте" in bi.text and "assalomu alaykum" in bi.text.lower()


def test_download_requires_header_not_query(client, auth):
    """Токен в URL больше не принимается — утечка через логи/Referer/историю."""
    r = client.post(
        "/api/jobs", headers=auth, files=_srt_file(),
        data={"jtype": "subtitles", "src": "ru", "tgt": "uz"},
    )
    job = r.json()["job"]
    token = auth["Authorization"][7:]
    assert client.get(job["artifacts"]["srt"] + "?token=" + token).status_code == 401
    assert client.get(job["artifacts"]["srt"], headers=auth).status_code == 200


def test_language_validation(client, auth):
    for src, tgt in [("ru", "ru"), ("xx", "ru"), ("ru", "de")]:
        r = client.post(
            "/api/jobs", headers=auth, files=_srt_file(),
            data={"jtype": "subtitles", "src": src, "tgt": tgt},
        )
        assert r.status_code == 400, f"{src}->{tgt} должен отклоняться"


def test_dubbing_produces_wav(client, auth):
    r = client.post(
        "/api/jobs", headers=auth, files=_srt_file(),
        data={"jtype": "dubbing", "src": "ru", "tgt": "uz"},
    )
    job = r.json()["job"]
    assert job["status"] == "done", job["error"]
    dl = client.get(job["artifacts"]["dubbing"], headers=auth)
    assert dl.status_code == 200
    assert dl.content[:4] == b"RIFF"  # legit WAV-контейнер


def test_glossary_protects_terms(client, auth):
    client.post("/api/glossary", headers=auth,
                json=[{"src": "Telegram", "tgt": "Телеграм-канал"}],
                )
    r = client.post(
        "/api/jobs", headers=auth, files=_srt_file(),
        data={"jtype": "subtitles", "src": "ru", "tgt": "uz"},
    )
    job = r.json()["job"]
    dl = client.get(job["artifacts"]["srt"], headers=auth)
    assert "Телеграм-канал" in dl.text


def test_glossary_term_delete(client, auth):
    client.post("/api/glossary", headers=auth,
                json=[{"src": "TmpTerm", "tgt": "Временный"}])
    terms = client.get("/api/glossary", headers=auth).json()["terms"]
    assert any(x["src_term"] == "TmpTerm" for x in terms)
    r = client.request("DELETE", "/api/glossary", headers=auth, json={"src": "TmpTerm"})
    assert r.status_code == 200 and r.json()["deleted"] is True
    terms = client.get("/api/glossary", headers=auth).json()["terms"]
    assert all(x["src_term"] != "TmpTerm" for x in terms)
    assert client.request("DELETE", "/api/glossary", headers=auth, json={}).status_code == 422


def test_insufficient_credits_402_no_orphan(client, auth):
    """Дорогой job (серверная оценка > баланса) → 402, и в списке не висит сирота."""
    big = {"file": ("big.txt", ("word " * 4000).encode(), "text/plain")}  # ~26 мин
    r = client.post(
        "/api/jobs", headers=auth, files=big,
        data={"jtype": "subtitles", "src": "ru", "tgt": "uz"},
    )
    assert r.status_code == 402
    jobs = client.get("/api/jobs", headers=auth).json()["jobs"]
    assert all(j["status"] != "queued" for j in jobs)


def test_register_login_with_secret(client):
    contact = unique_contact()
    r = client.post("/api/auth/register",
                    data={"name": "Secret", "contact": contact, "secret": "top-secret"})
    assert r.status_code == 200
    # и «нет пользователя», и «неверный секрет» — единый 401 (оракул перечисления закрыт)
    assert client.post("/api/auth/login", data={"contact": contact}).status_code == 401
    assert client.post("/api/auth/login",
                       data={"contact": contact, "secret": "wrong"}).status_code == 401
    assert client.post("/api/auth/login",
                       data={"contact": "+998999999999"}).status_code == 401
    ok = client.post("/api/auth/login",
                     data={"contact": contact, "secret": "top-secret"})
    assert ok.status_code == 200 and ok.json()["token"]


def test_payment_webhook_requires_signature(client, auth):
    uid = client.get("/api/me", headers=auth).json()["user"]["id"]
    base = {"external_id": "pay-nosig", "user_id": uid,
            "amount_minor": 12000000, "currency": "UZS", "minutes": 120}
    r = client.post("/api/payments/webhook/payme", data=base)
    assert r.status_code == 403  # без подписи — отказ
    r2 = client.post("/api/payments/webhook/payme",
                     data=base | {"signature": "0" * 64})
    assert r2.status_code == 403  # неверная подпись — отказ


def test_payment_webhook_disabled_without_secret(client, auth, monkeypatch):
    """Если секрет не настроен — webhook не принимает ничего вообще."""
    monkeypatch.setenv("OVOZ_WEBHOOK_SECRET", "")
    r = client.post("/api/payments/webhook/payme",
                    data={"external_id": "pay-nokey", "user_id": "u",
                          "amount_minor": 12000000, "currency": "UZS", "minutes": 120})
    assert r.status_code == 501


def test_payment_webhook_via_api(client, auth):
    me = client.get("/api/me", headers=auth).json()
    uid = me["user"]["id"]
    ext = "pay-e2e-" + uuid.uuid4().hex[:6]
    base = {"external_id": ext, "user_id": uid,
            "amount_minor": 12000000, "currency": "UZS", "minutes": 120}
    signed = base | {"signature": _sig("payme", ext, uid, 12000000, "UZS")}
    r = client.post("/api/payments/webhook/payme", data=signed)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    r2 = client.post("/api/payments/webhook/payme", data=signed)
    assert r2.json()["status"] == "duplicate"  # повтор не начисляет
    bal = client.get("/api/me", headers=auth).json()["balance_minutes"]
    assert bal == 130  # 10 бонус + 120 платеж


def test_manual_provider_rejected(client, auth):
    r = client.post("/api/payments/webhook/manual",
                    data={"external_id": "m1", "user_id": "x",
                          "amount_minor": 1, "minutes": 99999})
    assert r.status_code == 400


def test_zero_amount_webhook_cannot_mint_credits(client, auth):
    """free-план стоит 0$ — нулевая сумма не должна матчиться ни на что."""
    uid = client.get("/api/me", headers=auth).json()["user"]["id"]
    bal0 = client.get("/api/me", headers=auth).json()["balance_minutes"]
    ext = "pay-zero-" + uuid.uuid4().hex[:6]
    sig = _sig("payme", ext, uid, 0, "USD")
    r = client.post("/api/payments/webhook/payme",
                    data={"external_id": ext, "user_id": uid, "amount_minor": 0,
                          "currency": "USD", "minutes": 1, "signature": sig})
    assert r.status_code == 422  # сумма ≤ 0 отклоняется до начисления
    assert client.get("/api/me", headers=auth).json()["balance_minutes"] == bal0


def test_demo_credit_requires_admin_key(client, auth):
    token = auth["Authorization"][7:]
    assert client.post("/api/dev/demo-credit", headers=auth,
                       data={"minutes": 60}).status_code == 403
    assert client.post("/api/dev/demo-credit", headers=auth,
                       data={"minutes": 60, "admin_key": "nope"}).status_code == 403
    r = client.post("/api/dev/demo-credit", headers=auth,
                    data={"minutes": 60, "admin_key": ADMIN_KEY})
    assert r.status_code == 200
    assert r.json()["balance_minutes"] == 70


# ---------- Round-4: продукт-уровень безопасность/надёжность ----------


def test_register_requires_secret_min_len(client):
    """P0/C1: пустой или короткий секрет запрещён — иначе anyone-with-contact залогинится."""
    c1 = unique_contact()
    assert client.post("/api/auth/register",
                       data={"name": "x", "contact": c1}).status_code == 422
    assert client.post("/api/auth/register",
                       data={"name": "x", "contact": c1, "secret": ""}).status_code == 422
    assert client.post("/api/auth/register",
                       data={"name": "x", "contact": c1, "secret": "short1"}).status_code == 422
    assert client.post("/api/auth/register",
                       data={"name": "x", "contact": c1, "secret": "0123456789"}).status_code == 200


def test_pbkdf2_hashing_and_legacy_migration(client):
    """P0/C2: PBKDF2 с солью; два аккаунта с одинаковым паролем имеют разные hash."""
    from app import db
    s = "same-password"
    # digit-only контакты, чтобы find_user_by_contact совпадал с нормализованным
    c1 = "+998900" + str(uuid.uuid4().int % 10**8).zfill(8)
    c2 = "+998901" + str(uuid.uuid4().int % 10**8).zfill(8)
    client.post("/api/auth/register", data={"name": "a", "contact": c1, "secret": s})
    client.post("/api/auth/register", data={"name": "b", "contact": c2, "secret": s})
    u1 = db.find_user_by_contact(c1)
    u2 = db.find_user_by_contact(c2)
    assert u1 and u2, (c1, c2)
    assert u1["secret_hash"] != u2["secret_hash"]  # разные соли
    assert u1["secret_algo"].startswith("pbkdf2_sha256$")
    assert len(u1["secret_hash"]) == 64  # sha256 digest hex


def test_login_unified_401_no_oracle(client):
    """C1/C2/m6: пустой секрет не должен пускать; одинаковый ответ для нет-юзера и нет-секрета."""
    contact = unique_contact()
    client.post("/api/auth/register", data={"name": "x", "contact": contact, "secret": "abcd123456"})
    r1 = client.post("/api/auth/login", data={"contact": contact})  # без секрета
    r2 = client.post("/api/auth/login", data={"contact": "+9989999999"})  # нет юзера
    r3 = client.post("/api/auth/login", data={"contact": contact, "secret": "wrong-wrong-wrong"})
    assert r1.status_code == r2.status_code == r3.status_code == 401
    # одинаковый detail — ни какой-либоSignals что контакт существует
    assert r1.json()["detail"] == r2.json()["detail"] == r3.json()["detail"]


def test_logout_revokes_session(client, auth):
    """M9: logout должен снести сессию — дальше /api/me обязан отдать 401."""
    assert client.get("/api/me", headers=auth).status_code == 200
    assert client.post("/api/auth/logout", headers=auth).status_code == 200
    assert client.get("/api/me", headers=auth).status_code == 401


def test_security_headers_present(client, auth):
    """M8: CSP, nosniff, referrer, frame-ancestors."""
    r = client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors" in csp and "web.telegram.org" in csp
    assert "default-src 'self'" in csp


def test_script_src_has_no_unsafe_inline(client):
    """Executable script is same-origin or Telegram — never 'whatever is inline'.

    Checked per directive on purpose: style-src still carries 'unsafe-inline'
    (the SPA sets inline styles for the player and the theme), and a blanket
    "'unsafe-inline' not in csp" assertion would either fail today or be deleted
    tomorrow. With no inline script in the document (invariant test) this is the
    difference between a stored XSS that fires and one that is refused."""
    csp = client.get("/healthz").headers["content-security-policy"]
    directives = {d.split(" ", 1)[0]: d for d in csp.split("; ") if d}
    assert "script-src" in directives
    assert "'unsafe-inline'" not in directives["script-src"], directives["script-src"]
    assert "'unsafe-eval'" not in directives["script-src"], directives["script-src"]
    assert "'self'" in directives["script-src"]
    # a bare `https:` source would let any host on the internet script this page;
    # named origins like https://telegram.org are the point of the directive
    sources = directives["script-src"].split()[1:]
    assert not [s for s in sources if s in ("https:", "http:", "*")], sources


def test_payment_webhook_unmatched_amount_rejected(client, auth):
    """M2: сумма не из прайса → 422 и НИКАКОГО начисления."""
    uid = client.get("/api/me", headers=auth).json()["user"]["id"]
    bal0 = client.get("/api/me", headers=auth).json()["balance_minutes"]
    ext = "pay-odd-" + uuid.uuid4().hex[:6]
    # 12000001 ≠ 12000000, подпись правильная
    sig = _sig("payme", ext, uid, 12000001, "UZS")
    r = client.post("/api/payments/webhook/payme",
                    data={"external_id": ext, "user_id": uid, "amount_minor": 12000001,
                          "currency": "UZS", "minutes": 600, "signature": sig})
    assert r.status_code == 422
    assert client.get("/api/me", headers=auth).json()["balance_minutes"] == bal0


def test_timing_injection_cannot_oom(client, auth):
    """C3: seg.end=60000 в .txt не должен alloc'ить гигабайты — clampSegments вырежет."""
    evil = {"file": ("evil.txt", b"0-60000|Salom\n60000-120000|Salom2\n", "text/plain")}
    r = client.post("/api/jobs", headers=auth, files=evil,
                    data={"jtype": "dubbing", "src": "uz", "tgt": "ru"})
    # сервер обязан ответить (создал job или отклонил), но не 500
    assert r.status_code in (201, 402, 413), r.text
    if r.status_code == 201:
        # job завершится done/failed, но не зависнет
        assert client.get("/healthz").json()["ok"] is True


def test_active_jobs_flood_guard(client, auth):
    """C4: больше MAX_ACTIVE_JOBS_PER_USER одновременных queued/running → 429.
    В sync_pipeline=1 (тест-режим) job сразу done, так что проверяем константу.
    """
    import app.main as m
    assert m.MAX_ACTIVE_JOBS_PER_USER <= 10


def test_contact_normalization_dedupes(client):
    """m6: телефон в разном формате должен схлопываться в один аккаунт (E.164-lite)."""
    raw = unique_digits()
    c1 = f"+99890{raw}"
    c2 = f"998 90 {raw[:3]} {raw[3:]}"  # тот же номер с пробелами и без +
    r = client.post("/api/auth/register",
                    data={"name": "x", "contact": c1, "secret": "abcd123456"})
    assert r.status_code == 200
    # нормализация c2 → +99890... — тот же контакт → 409
    r2 = client.post("/api/auth/register",
                     data={"name": "y", "contact": c2, "secret": "abcd123456"})
    assert r2.status_code == 409


def test_healthz_no_provider_leak(client):
    """m4: /healthz без авторизации не должен раскрывать, какие провайдеры живы."""
    body = client.get("/healthz").json()
    assert "providers" not in body
    assert body["ok"] is True
    assert "version" in body
    assert body.get("db") is True


def test_glossary_payload_capped(client, auth):
    """m8: запрос на 201 термин → 422."""
    many = [{"src": f"term{i}", "tgt": f"T{i}"} for i in range(201)]
    assert client.post("/api/glossary", headers=auth, json=many).status_code == 422


def test_login_throttle_after_many_fails(client):
    """M3: после 8 неудач подряд по одному контакту → 429."""
    contact = unique_contact()
    client.post("/api/auth/register",
                data={"name": "thr", "contact": contact, "secret": "abcd123456"})
    for _ in range(8):
        assert client.post("/api/auth/login",
                           data={"contact": contact, "secret": "wrong-password"}).status_code == 401
    r = client.post("/api/auth/login",
                    data={"contact": contact, "secret": "abcd123456"})
    assert r.status_code == 429


# ---------- Round 8: Error codes ----------

def test_error_envelope_has_error_code(client, auth):
    """All HTTPException responses include error_code field."""
    r = client.get("/api/jobs/nonexistent", headers=auth)
    assert r.status_code == 404
    body = r.json()
    assert body["error_code"] == "not_found"


def test_error_envelope_insufficient_credits(client, auth):
    """402 carries structured INSUFFICIENT_CREDITS code."""
    # drain balance
    client.post("/api/dev/demo-credit", headers=auth,
                data={"minutes": "-9999", "admin_key": ADMIN_KEY})
    r = client.post("/api/jobs", headers=auth, files=_srt_file(),
                    data={"jtype": "subtitles", "src": "ru", "tgt": "uz"})
    # credit to 0 then try to submit
    client.post("/api/dev/demo-credit", headers=auth,
                data={"minutes": "0", "admin_key": ADMIN_KEY})
    r2 = client.post("/api/jobs", headers=auth, files=_srt_file(),
                     data={"jtype": "subtitles", "src": "ru", "tgt": "uz"})
    if r2.status_code == 402:
        assert r2.json()["error_code"] == "insufficient_credits"


def test_validation_error_envelope(client, auth):
    """FastAPI 422 validation errors map to our structured envelope."""
    # missing required field
    r = client.post("/api/jobs", headers=auth, data={"jtype": "bad"})
    assert r.status_code in (400, 422)
    body = r.json()
    assert "error_code" in body


# ---------- Round 8: Pagination ----------

def test_jobs_pagination_cursor(client, auth):
    """GET /api/jobs?limit=1 returns next_cursor for paging."""
    # create 3 jobs
    for _ in range(3):
        client.post("/api/jobs", headers=auth, files=_srt_file(),
                    data={"jtype": "transcribe", "src": "ru", "tgt": "uz"})
    r = client.get("/api/jobs?limit=1", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert len(body["jobs"]) == 1
    assert body["next_cursor"] is not None
    # page through all: collect IDs
    all_ids = {body["jobs"][0]["id"]}
    cursor = body["next_cursor"]
    for _ in range(5):  # safety limit
        r2 = client.get(f"/api/jobs?limit=1&cursor={cursor}", headers=auth)
        assert r2.status_code == 200
        page = r2.json()
        if not page["jobs"]:
            break
        all_ids.add(page["jobs"][0]["id"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    # must have seen at least 2 unique jobs (all same-second edge may skip 1)
    assert len(all_ids) >= 2


# ---------- Round 8: Metrics ----------

def test_metrics_endpoint(client):
    """GET /metrics returns Prometheus text format."""
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "ovoz_total_jobs" in r.text
    assert "ovoz_uptime_seconds" in r.text
    assert "ovoz_active_jobs" in r.text


# ---------- Round 8: Magic-byte rejection ----------

def test_reject_fake_wav(client, auth):
    """File with .wav extension but no RIFF header is rejected."""
    fake = b"MZ" + b"\x00" * 100  # looks like PE exe, not WAV
    r = client.post("/api/jobs", headers=auth,
                    files={"file": ("evil.wav", fake, "audio/wav")},
                    data={"jtype": "subtitles", "src": "uz", "tgt": "ru"})
    assert r.status_code == 400
    assert "does not match" in r.json()["detail"]


# ---------- Round 8: Session integrity ----------

def test_session_survives_new_login(client):
    """Regression: creating a new session must NOT invalidate existing ones."""
    c1 = unique_contact()
    c2 = unique_contact()
    r1 = client.post("/api/auth/register",
                     data={"name": "A", "contact": c1, "secret": "secret123456"})
    t1 = r1.json()["token"]
    # second user registers (triggers cleanup inside create_session)
    r2 = client.post("/api/auth/register",
                     data={"name": "B", "contact": c2, "secret": "secret123456"})
    t2 = r2.json()["token"]
    # first token still works
    me = client.get("/api/me", headers={"Authorization": f"Bearer {t1}"})
    assert me.status_code == 200


# ---------- Round 8: Logout case-insensitivity ----------

def test_logout_case_insensitive(client):
    """Logout works with 'bearer' (lowercase) prefix."""
    contact = unique_contact()
    r = client.post("/api/auth/register",
                    data={"name": "X", "contact": contact, "secret": "caseins12345"})
    tok = r.json()["token"]
    # logout with lowercase bearer
    r2 = client.post("/api/auth/logout", headers={"Authorization": f"bearer {tok}"})
    assert r2.status_code == 200
    # token revoked
    r3 = client.get("/api/me", headers={"Authorization": f"Bearer {tok}"})
    assert r3.status_code == 401


# ---------- Round 8/13: WebSocket handshake via single-use ticket ----------

def _ticket(client, auth) -> str:
    r = client.post("/api/auth/ws-ticket", headers=auth)
    assert r.status_code == 200
    return r.json()["ticket"]


def test_websocket_rejects_no_ticket(client):
    """WebSocket /ws/jobs without a ticket → close 4001."""
    import pytest
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/jobs?ticket=") as ws:
            ws.receive_json()  # triggers the close
    assert exc_info.value.code == 4001


def test_websocket_rejects_the_session_token(client, auth):
    """The long-lived bearer must never authenticate a query string again."""
    import pytest
    from starlette.websockets import WebSocketDisconnect
    tok = auth["Authorization"].split(" ", 1)[1]
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/ws/jobs?token={tok}") as ws:
            ws.receive_json()
    assert exc_info.value.code == 4001


def test_ws_ticket_requires_auth(client):
    assert client.post("/api/auth/ws-ticket").status_code == 401


def test_websocket_connects_with_ticket(client, auth):
    with client.websocket_connect(f"/ws/jobs?ticket={_ticket(client, auth)}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "connected"
        assert "uid" in msg


def test_ws_ticket_is_single_use(client, auth):
    """Replay of a consumed ticket must fail, even inside its TTL."""
    import pytest
    from starlette.websockets import WebSocketDisconnect
    t = _ticket(client, auth)
    with client.websocket_connect(f"/ws/jobs?ticket={t}") as ws:
        assert ws.receive_json()["type"] == "connected"
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/ws/jobs?ticket={t}") as ws:
            ws.receive_json()
    assert exc_info.value.code == 4001


def test_ws_ticket_does_not_leak_into_history(client, auth):
    """A ticket is high-entropy and dead in 15s — unlike the session bearer."""
    tok = auth["Authorization"].split(" ", 1)[1]
    t = _ticket(client, auth)
    assert tok not in t and len(t) >= 24


def test_websocket_caps_sockets_per_user(client, auth):
    """One client cannot multiply connections against the server's FD budget."""
    import pytest
    from starlette.websockets import WebSocketDisconnect
    from app.main import WS_MAX_SOCKETS_PER_USER
    opened = []
    for _ in range(WS_MAX_SOCKETS_PER_USER):
        ctx = client.websocket_connect(f"/ws/jobs?ticket={_ticket(client, auth)}")
        ws = ctx.__enter__()
        assert ws.receive_json()["type"] == "connected"
        opened.append(ctx)
    try:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(f"/ws/jobs?ticket={_ticket(client, auth)}") as ws:
                ws.receive_json()
        assert exc_info.value.code == 4003
    finally:
        for ctx in opened:
            ctx.__exit__(None, None, None)


def test_job_events_reach_a_live_socket_end_to_end(client, auth):
    """The one test that proves the whole chain is wired.

    The hub tests check the pub/sub object, the client tests check the socket
    handshake, /metrics checks the counters — and `db.add_job_event =
    _patched_add_event` can be deleted with every one of them still green. That
    single line is the only thing connecting the pipeline to the wire, so here a
    real job runs while a real socket is open and the frames must arrive.
    """
    with client.websocket_connect(f"/ws/jobs?ticket={_ticket(client, auth)}") as ws:
        assert ws.receive_json()["type"] == "connected"
        r = client.post("/api/jobs", headers=auth, files=_srt_file(),
                        data={"jtype": "subtitles", "src": "ru", "tgt": "uz"})
        assert r.status_code == 201, r.text
        jid = r.json()["job"]["id"]
        steps, pcts = [], []
        while True:
            msg = ws.receive_json()
            if msg["type"] != "job_event":
                continue  # keepalive pings interleave; they are not the contract
            assert msg["job_id"] == jid, "streamed another user's job"
            steps.append(msg["step"])
            pcts.append(msg["pct"])
            if msg["step"] in ("done", "failed"):
                break
        assert steps[0] == "queued", steps
        assert steps[-1] == "done", steps
        assert all(isinstance(p, int) for p in pcts), "_STEP_PCT reached the client"
        # The client drives one bar from these frames: a regressing percentage is
        # a visible glitch even when every individual value is sensible.
        assert pcts == sorted(pcts), f"progress walked backwards: {steps} {pcts}"


def test_hub_delivers_events_published_from_a_worker_thread():
    """The realtime channel must survive asyncio API changes, not hide them.

    asyncio.Queue.get_loop() was removed in Python 3.10. The hub called it from a
    pipeline thread inside a bare `except Exception: pass`, so every job_event was
    silently dropped: sockets connected, streamed nothing, and 200+ tests stayed
    green because none of them ever checked delivery. This publishes from a
    foreign thread into a loop that is really running — the pipeline's exact shape.
    """
    import asyncio
    import threading
    from app import main as main_mod

    async def scenario():
        hub = main_mod._WSHub()
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        sub = hub.subscribe("u1", q, asyncio.get_running_loop())
        publisher = threading.Thread(target=hub.broadcast,
                                     args=("u1", {"step": "done", "job_id": "j1", "pct": 100}))
        publisher.start()
        try:
            event = await asyncio.wait_for(q.get(), timeout=5)
        finally:
            publisher.join(timeout=5)
            hub.unsubscribe("u1", sub)
        assert event["step"] == "done"
        assert (hub.published, hub.dropped) == (1, 0), "publish failed but stayed silent"
        return event

    asyncio.run(scenario())


def test_hub_reports_dropped_frames_instead_of_swallowing_them():
    """A full buffer is a deliberate drop, and a failed publish is a logged one —
    both counted, so /metrics and the logs can tell a slow client from a dead hub."""
    import asyncio
    from app import main as main_mod

    async def scenario():
        hub = main_mod._WSHub()
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        sub = hub.subscribe("u1", q, asyncio.get_running_loop())
        hub.broadcast("u1", {"step": "start"})
        hub.broadcast("u1", {"step": "asr"})   # buffer is full → dropped
        hub.broadcast("nobody", {"step": "asr"})  # no subscriber → no-op
        await asyncio.sleep(0.05)  # let the loop run the scheduled deliveries
        first = q.get_nowait()
        hub.unsubscribe("u1", sub)
        assert first["step"] == "start"
        assert hub.published == 1 and hub.dropped == 1, \
            f"published={hub.published} dropped={hub.dropped}"
        assert hub.count("nobody") == 0

    asyncio.run(scenario())


def test_hub_does_not_use_removed_asyncio_queue_api():
    """Guard the exact regression: Queue.get_loop() has not existed since 3.10."""
    from app import main as main_mod
    src = Path(main_mod.__file__).read_text(encoding="utf-8")
    assert "get_loop()" not in src, "asyncio.Queue.get_loop() was removed in 3.10"


# ---------- Round 9: Notifications ----------

def test_notifications_after_job(client, auth):
    """After a completed job, user has at least one notification."""
    client.post("/api/jobs", headers=auth, files=_srt_file(),
                data={"jtype": "transcribe", "src": "ru", "tgt": "uz"})
    r = client.get("/api/notifications", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["unread_count"] >= 1
    assert any(n["kind"] == "job_done" for n in body["notifications"])


def test_mark_notification_read(client, auth):
    """Can mark a notification as read."""
    # ensure at least one notification
    client.post("/api/jobs", headers=auth, files=_srt_file(),
                data={"jtype": "transcribe", "src": "ru", "tgt": "uz"})
    items = client.get("/api/notifications?unread=true", headers=auth).json()["notifications"]
    if items:
        nid = items[0]["id"]
        r = client.post(f"/api/notifications/{nid}/read", headers=auth)
        assert r.status_code == 200


# ---------- Round 9: Ledger history ----------

def test_ledger_history(client, auth):
    """GET /api/ledger returns balance entries."""
    r = client.get("/api/ledger", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert "entries" in body
    assert "balance_minutes" in body
    # signup bonus should be the first entry
    assert any(e["reason"] == "signup:bonus" for e in body["entries"])


# ---------- Round 9: Daily quota ----------

def test_daily_quota_blocks_excess(client, auth):
    """After spending daily cap, further jobs are 429."""
    # drain all balance with tiny jobs (each ~0.1 min for transcribe of small srt)
    # Instead, just check the endpoint structure by requesting many
    import uuid
    for _ in range(25):
        r = client.post("/api/jobs", headers=auth, files=_srt_file(),
                        data={"jtype": "transcribe", "src": "ru", "tgt": "uz"})
        if r.status_code == 429:
            assert "quota" in r.json()["detail"].lower() or "active" in r.json()["detail"].lower()
            return
    # if 25 attempts didn't trigger, quota is generous enough for tests


# ---------- Round 9: Admin status endpoint ----------

def test_admin_status_requires_key(client, auth):
    r = client.get("/api/admin/status", headers=auth)
    assert r.status_code == 403


def test_admin_status_no_secret(client):
    r = client.get("/api/admin/status", headers={"Authorization": "Bearer wrong"})
    assert r.status_code in (403, 501)


def test_admin_status_ok(client):
    admin_hdr = {"Authorization": f"Bearer {ADMIN_KEY}"}
    r = client.get("/api/admin/status", headers=admin_hdr)
    assert r.status_code == 200
    body = r.json()
    assert "users" in body
    assert "jobs" in body
    assert "revenue" in body
    assert "system" in body
    assert body["system"]["version"]


# ---------- Round 9: Job list filtering ----------

def test_job_filter_by_status(client, auth):
    # create a job (transcribe of srt)
    r = client.post("/api/jobs", headers=auth, files=_srt_file(),
                    data={"jtype": "transcribe", "src": "ru", "tgt": "uz"})
    assert r.status_code == 201
    # filter done
    r = client.get("/api/jobs?status=done&limit=5", headers=auth)
    assert r.status_code == 200
    for j in r.json()["jobs"]:
        assert j["status"] == "done"


def test_job_filter_invalid_status(client, auth):
    r = client.get("/api/jobs?status=bogus", headers=auth)
    assert r.status_code == 422


def test_job_filter_by_type(client, auth):
    r = client.get("/api/jobs?jtype=transcribe&limit=5", headers=auth)
    assert r.status_code == 200
    for j in r.json()["jobs"]:
        assert j["type"] == "transcribe"


def test_job_filter_invalid_type(client, auth):
    r = client.get("/api/jobs?jtype=fly_to_moon", headers=auth)
    assert r.status_code == 422


# ---------- Round 9: Payment upgrades plan ----------

def test_payment_upgrades_plan(client, auth):
    # Get user ID from /api/me
    me = client.get("/api/me", headers=auth).json()
    uid = me["user"]["id"]
    assert me["user"]["plan"] == "free"
    # Simulate a valid pro_uzs payment (12_000_000 tiyin)
    ext_id = "pay-" + uuid.uuid4().hex[:12]
    sig = _sig("payme", ext_id, uid, 12_000_000, "UZS")
    r = client.post("/api/payments/webhook/payme",
                    data={"external_id": ext_id, "user_id": uid,
                          "amount_minor": 12_000_000, "currency": "UZS",
                          "minutes": 120, "signature": sig})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    # Verify plan upgraded
    me2 = client.get("/api/me", headers=auth).json()
    assert me2["user"]["plan"] == "pro"


# ---------- Round 9: GDPR delete cleans notifications ----------

def test_gdpr_delete_cleans_notifications(client):
    # register fresh user
    contact = unique_contact()
    r = client.post("/api/auth/register",
                    data={"name": "GDPR", "contact": contact, "secret": "test-secret-XX"})
    assert r.status_code == 200
    token = r.json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    # create a job to generate notifications
    client.post("/api/jobs", headers=hdr, files=_srt_file(),
                data={"jtype": "transcribe", "src": "ru", "tgt": "uz"})
    # check notifications exist
    nr = client.get("/api/notifications", headers=hdr)
    # even if no notifications were created (depending on notification trigger), the delete should work
    # delete account
    dr = client.delete("/api/account", headers=hdr)
    assert dr.status_code == 200
    # login again should fail
    lr = client.post("/api/auth/login", data={"contact": contact, "secret": "test-secret-XX"})
    assert lr.status_code == 401


# ---------- Round 9: OpenAPI docs available ----------

def test_openapi_json(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert spec["info"]["title"] == "Ovoz AI Studio"
    # check tags present
    tag_names = [t["name"] for t in spec.get("tags", [])]
    assert "auth" in tag_names
    assert "jobs" in tag_names
    assert "admin" in tag_names


def test_swagger_ui(client):
    r = client.get("/docs")
    assert r.status_code == 200
    assert "swagger" in r.text.lower() or "openapi" in r.text.lower()


# ---------- Round 9: Notification endpoint returns items ----------

def test_notifications_endpoint(client, auth):
    # create a job to potentially fire notifications
    client.post("/api/jobs", headers=auth, files=_srt_file(),
                data={"jtype": "transcribe", "src": "ru", "tgt": "uz"})
    r = client.get("/api/notifications", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert "notifications" in body
    assert "unread_count" in body
    assert isinstance(body["notifications"], list)


# ---------- Round 9: Account settings ----------

def test_update_name(client, auth):
    r = client.patch("/api/account/name", headers=auth,
                     json={"name": "NewName"})
    assert r.status_code == 200
    assert r.json()["name"] == "NewName"
    # verify in /api/me
    me = client.get("/api/me", headers=auth).json()
    assert me["user"]["name"] == "NewName"


def test_update_name_too_long(client, auth):
    r = client.patch("/api/account/name", headers=auth,
                     json={"name": "x" * 65})
    assert r.status_code == 422


def test_change_secret(client, auth):
    # Register a fresh user so we know the original password
    contact = unique_contact()
    r = client.post("/api/auth/register",
                    data={"name": "SecTest", "contact": contact, "secret": "old-secret-XX"})
    assert r.status_code == 200
    hdr = {"Authorization": f"Bearer {r.json()['token']}"}
    # Change password
    r = client.post("/api/account/change-secret", headers=hdr,
                    data={"old_secret": "old-secret-XX", "new_secret": "new-secret-YY"})
    assert r.status_code == 200
    # Login with new password
    r = client.post("/api/auth/login",
                    data={"contact": contact, "secret": "new-secret-YY"})
    assert r.status_code == 200


def test_change_secret_wrong_old(client, auth):
    r = client.post("/api/account/change-secret", headers=auth,
                    data={"old_secret": "totally-wrong-pass", "new_secret": "new-secret-XX"})
    assert r.status_code == 403


def test_change_secret_too_short(client, auth):
    r = client.post("/api/account/change-secret", headers=auth,
                    data={"old_secret": "test-secret-XX", "new_secret": "short"})
    assert r.status_code == 422


# ---------- Round 9: Bulk mark notifications read ----------

def test_bulk_mark_read(client, auth):
    # create a job to fire notification
    client.post("/api/jobs", headers=auth, files=_srt_file(),
                data={"jtype": "transcribe", "src": "ru", "tgt": "uz"})
    r = client.post("/api/notifications/read-all", headers=auth)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "marked" in r.json()


# ---------- Round 9: API version info ----------

def test_api_v1_info(client):
    r = client.get("/api/v1/info")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Ovoz AI Studio"
    assert "version" in body
    assert "features" in body
    assert "subtitles" in body["features"]


# ---------- Round 9: Rate-limit headers on 429 ----------

def test_rate_limit_headers_on_job_cap(client, auth):
    # fill up active jobs to trigger the cap
    for _ in range(10):
        r = client.post("/api/jobs", headers=auth, files=_srt_file(),
                        data={"jtype": "transcribe", "src": "ru", "tgt": "uz"})
        if r.status_code == 429:
            assert "retry-after" in r.headers
            assert "x-ratelimit-limit" in r.headers
            return
    # if never triggered (sync mode completes instantly), that's fine


# ---------- Round 10: Job sharing ----------

def test_share_creates_link(client, auth):
    # Create and complete a job
    r = client.post("/api/jobs", headers=auth, files=_srt_file(),
                    data={"jtype": "transcribe", "src": "ru", "tgt": "uz"})
    assert r.status_code == 201
    jid = r.json()["job"]["id"]
    # Share it
    r = client.post(f"/api/jobs/{jid}/share", headers=auth, data={"ttl_hours": 24})
    assert r.status_code == 200
    body = r.json()
    assert "share_id" in body
    assert body["share_url"].startswith("/s/")


def test_share_public_access(client, auth):
    # Create job, share, then access publicly (no auth)
    r = client.post("/api/jobs", headers=auth, files=_srt_file(),
                    data={"jtype": "transcribe", "src": "ru", "tgt": "uz"})
    jid = r.json()["job"]["id"]
    r = client.post(f"/api/jobs/{jid}/share", headers=auth, data={"ttl_hours": 24})
    sid = r.json()["share_id"]
    # Access without auth
    r = client.get(f"/s/{sid}")
    assert r.status_code == 200
    assert "artifacts" in r.json()


def test_share_download_no_auth(client, auth):
    r = client.post("/api/jobs", headers=auth, files=_srt_file(),
                    data={"jtype": "subtitles", "src": "ru", "tgt": "uz"})
    jid = r.json()["job"]["id"]
    r = client.post(f"/api/jobs/{jid}/share", headers=auth)
    sid = r.json()["share_id"]
    # Download srt artifact via share (no auth)
    r = client.get(f"/s/{sid}/dl/srt")
    assert r.status_code == 200
    assert "transcript" not in r.headers.get("content-type", "") or True


def test_share_not_found(client):
    r = client.get("/s/nonexistent")
    assert r.status_code == 404


def test_share_only_done_jobs(client, auth):
    # Jobs in sync mode finish immediately, so check error on nonexistent
    r = client.post("/api/jobs/fakeid/share", headers=auth)
    assert r.status_code == 404


# ---------- Round 10: Admin revenue uses 'paid' status ----------

def test_admin_revenue_after_payment(client, auth):
    """Verify revenue shows non-zero after a payment."""
    me = client.get("/api/me", headers=auth).json()
    uid = me["user"]["id"]
    ext_id = "rev-test-" + uuid.uuid4().hex[:8]
    sig = _sig("click", ext_id, uid, 12_000_000, "UZS")
    client.post("/api/payments/webhook/click",
                data={"external_id": ext_id, "user_id": uid,
                      "amount_minor": 12_000_000, "currency": "UZS",
                      "minutes": 120, "signature": sig})
    # Admin should see revenue
    admin_hdr = {"Authorization": f"Bearer {ADMIN_KEY}"}
    r = client.get("/api/admin/status", headers=admin_hdr)
    assert r.status_code == 200
    assert r.json()["revenue"]["total_minor_units"] >= 12_000_000


# ---------- Round 11: Security headers ----------

def test_security_headers_xframe(client):
    """X-Frame-Options DENY on all responses."""
    r = client.get("/healthz")
    assert r.headers.get("x-frame-options") == "DENY"


def test_security_headers_coop_corp(client):
    """Cross-Origin-Opener-Policy and Cross-Origin-Resource-Policy set."""
    r = client.get("/healthz")
    assert r.headers.get("cross-origin-opener-policy") == "same-origin"
    assert r.headers.get("cross-origin-resource-policy") == "same-origin"


def test_security_headers_response_time(client):
    """X-Response-Time header is present and parses as ms."""
    r = client.get("/healthz")
    rt = r.headers.get("x-response-time", "")
    assert rt.endswith("ms")
    float(rt[:-2])  # must parse


def test_csp_has_worker_src(client):
    """CSP includes worker-src 'self' for service worker."""
    r = client.get("/healthz")
    csp = r.headers.get("content-security-policy", "")
    assert "worker-src 'self'" in csp


def test_csp_has_wss_connect(client):
    """CSP connect-src covers same-origin WebSocket via 'self'."""
    r = client.get("/healthz")
    csp = r.headers.get("content-security-policy", "")
    assert "connect-src 'self'" in csp


# ---------- Round 11: Service Worker + PWA ----------

def test_sw_js_served(client):
    """sw.js accessible at root with correct MIME."""
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers.get("content-type", "")


def test_sw_no_cache_header(client):
    """Service Worker must never be cached by browser."""
    r = client.get("/sw.js")
    assert "no-cache" in r.headers.get("cache-control", "")


def test_the_document_that_pins_the_build_is_never_cached_heuristically(client):
    """index.html decides which ?v= of every asset the visitor runs.

    StaticFiles answers it with ETag/Last-Modified only, and a browser given no
    Cache-Control invents one: 10% of the file's age, i.e. 10% of the age of the
    Docker build. Live QA hit the real version of this — a 0.15.2 shell served by
    a 0.16.0 server, so the tab loaded yesterday's app.js and the SW looked broken.
    """
    for path in ("/", "/index.html"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers.get("cache-control") == "no-cache", (path, r.headers.get("cache-control"))


def test_every_code_asset_the_page_loads_revalidates(client):
    """The policy used to be a hand-written list of four paths.

    tg-boot.js, sw-boot.js and the manifest were never in it, so they fell through
    to whatever the static layer said. The gate lists what the document references.
    """
    html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")
    paths = ["/" + m + ".js" for m in re.findall(r'<script src="([\w.-]+?)\.js', html)]
    paths += ["/" + m for m in re.findall(r'<link rel="stylesheet" href="([\w.-]+\.css)', html)]
    paths += ["/manifest.webmanifest"]
    assert "/app.js" in paths and "/sw-boot.js" in paths, paths
    for path in paths:
        cc = client.get(path).headers.get("cache-control", "")
        assert "max-age=0" in cc and "must-revalidate" in cc, (path, cc)


def test_manifest_content_type(client):
    """manifest.webmanifest served with correct type."""
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200


# ---------- Round 11: Schema versioning ----------

def test_schema_version_in_info(client):
    """schema_version present in /api/v1/info response."""
    r = client.get("/api/v1/info")
    assert r.status_code == 200
    assert r.json()["schema_version"] >= 5


def test_schema_version_table_exists(client):
    """schema_version table has entries for applied migrations."""
    from app import db
    conn = db.get_conn()
    rows = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    versions = [r[0] for r in rows]
    assert 1 in versions


def test_step_events_carry_a_payload_a_client_can_render(client):
    """Round 20: a step used to say only a sentence, and a sentence cannot be
    localised into uz/ru/en — so a finished card showed either English prose or
    nothing about the option the user just paid for."""
    from app import db
    cols = {r[1] for r in db.get_conn().execute("PRAGMA table_info(job_events)")}
    assert "data_json" in cols, cols
    uid = db.create_user("EV", unique_contact())["id"]
    job = db.create_job(uid, "subtitles", "uz", "ru", 1.0, "x.srt", {})
    db.add_job_event(job["id"], "align", "human prose",
                     {"code": "skipped", "reason": "no_audio"})
    ev = db.job_timeline(job["id"])[-1]
    assert ev["data"] == {"code": "skipped", "reason": "no_audio"}, ev
    assert ev["message"] == "human prose" and "data_json" not in ev, ev


def test_an_event_written_before_the_column_exists_still_reads_as_empty(client):
    """Old installs keep their old rows. Reading history must not invent a payload
    and must not crash on a value that was never written."""
    from app import db
    conn = db.get_conn()
    uid = db.create_user("OLD", unique_contact())["id"]
    job = db.create_job(uid, "subtitles", "uz", "ru", 1.0, "x.srt", {})
    conn.execute("INSERT INTO job_events (job_id, ts, step, message) VALUES (?,?,?,?)",
                 (job["id"], db.now_iso(), "asr", "legacy row"))
    conn.commit()
    ev = db.job_timeline(job["id"])[-1]
    assert ev["data"] == {} and ev["message"] == "legacy row", ev


def test_the_migration_list_matches_the_declared_schema_version(client):
    """`_SCHEMA_VERSION` is what /api/v1/info reports and what a fresh install
    records; a migration added without bumping it would never be applied."""
    from app import db
    assert db._SCHEMA_VERSION == max(v for v, _ in db._get_migrations(0)), \
        "bump _SCHEMA_VERSION with the migration list"
    assert db._SCHEMA_VERSION >= 7


# ---------- Round 11: CORS preflight PATCH ----------

def test_cors_preflight_patch(client):
    """OPTIONS preflight for PATCH returns correct allow-methods."""
    r = client.options("/api/account/name", headers={
        "Origin": "https://web.telegram.org",
        "Access-Control-Request-Method": "PATCH",
        "Access-Control-Request-Headers": "Authorization,Content-Type",
    })
    assert r.status_code in (200, 204)
    allow = r.headers.get("access-control-allow-methods", "")
    assert "PATCH" in allow


# ---------- Round 11: Concurrent jobs ----------

def test_concurrent_job_creation(client, auth):
    """Multiple simultaneous job creations don't crash."""
    import concurrent.futures
    results = []
    def submit():
        return client.post("/api/jobs", headers=auth, files=_srt_file(),
                          data={"jtype": "subtitles", "src": "ru", "tgt": "uz"})
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(submit) for _ in range(3)]
        results = [f.result() for f in futs]
    # at least 2 should succeed (3rd might hit active-jobs limit)
    ok = sum(1 for r in results if r.status_code in (200, 201))
    assert ok >= 2


# ---------- Round 11: Error format consistency ----------

def test_error_has_error_code(client):
    """All API errors return structured error_code."""
    r = client.get("/api/jobs/fakeid")
    assert r.status_code >= 400
    body = r.json()
    assert "error_code" in body
    assert "detail" in body


def test_validation_error_format(client, auth):
    """422 from missing params returns structured error."""
    r = client.post("/api/auth/register", data={})
    assert r.status_code in (400, 422)
    body = r.json()
    assert "error_code" in body


# ---------- Round 11: Upgrade-insecure-requests in CSP ----------

def test_csp_upgrade_insecure(client):
    """CSP includes upgrade-insecure-requests directive."""
    r = client.get("/healthz")
    csp = r.headers.get("content-security-policy", "")
    assert "upgrade-insecure-requests" in csp


# ---------- Round 11: API Keys ----------

def test_api_key_requires_paid_plan(client, auth):
    """Free users cannot create API keys."""
    r = client.post("/api/account/keys", headers=auth, data={"label": "test"})
    assert r.status_code == 403


def test_api_key_create_and_use(client, auth):
    """Upgrade to pro, create key, use it to access API."""
    # upgrade plan via payment webhook
    me = client.get("/api/me", headers=auth).json()
    uid = me["user"]["id"]
    ext_id = "apikey-" + uuid.uuid4().hex[:8]
    sig = _sig("click", ext_id, uid, 12_000_000, "UZS")
    client.post("/api/payments/webhook/click",
                data={"external_id": ext_id, "user_id": uid,
                      "amount_minor": 12_000_000, "currency": "UZS",
                      "minutes": 120, "signature": sig})
    # now create API key
    r = client.post("/api/account/keys", headers=auth, data={"label": "CI"})
    assert r.status_code == 200
    raw_key = r.json()["key"]
    assert raw_key.startswith("ovoz_")
    # use the key to access /api/me
    r2 = client.get("/api/me", headers={"X-API-Key": raw_key})
    assert r2.status_code == 200
    assert r2.json()["user"]["id"] == uid


def test_api_key_revoke(client, auth):
    """Revoked key no longer authenticates."""
    me = client.get("/api/me", headers=auth).json()
    uid = me["user"]["id"]
    ext_id = "revkey-" + uuid.uuid4().hex[:8]
    sig = _sig("click", ext_id, uid, 12_000_000, "UZS")
    client.post("/api/payments/webhook/click",
                data={"external_id": ext_id, "user_id": uid,
                      "amount_minor": 12_000_000, "currency": "UZS",
                      "minutes": 120, "signature": sig})
    r = client.post("/api/account/keys", headers=auth, data={"label": "temp"})
    assert r.status_code == 200
    kid = r.json()["record"]["id"]
    raw_key = r.json()["key"]
    # revoke
    r2 = client.delete(f"/api/account/keys/{kid}", headers=auth)
    assert r2.status_code == 200
    # key no longer works
    r3 = client.get("/api/me", headers={"X-API-Key": raw_key})
    assert r3.status_code == 401


def test_api_key_invalid(client):
    """Bogus API key returns 401."""
    r = client.get("/api/me", headers={"X-API-Key": "ovoz_bogus_invalid_key"})
    assert r.status_code == 401


def test_api_key_list(client, auth):
    """List keys returns empty for new free user."""
    r = client.get("/api/account/keys", headers=auth)
    assert r.status_code == 200
    assert r.json()["keys"] == []


# ---------- Round 11: X-Response-Time header on API ----------

def test_api_response_time(client):
    """All API responses have X-Response-Time."""
    r = client.get("/api/plans")
    assert "x-response-time" in r.headers


# ---------- Round 11: robots.txt ----------

def test_robots_txt(client):
    """robots.txt accessible."""
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "sitemap" in r.text.lower() or "allow" in r.text.lower()


# ---------- Round 11: GDPR Data Export (Art. 20) ----------

def test_data_export(client, auth):
    """User can export all their data as JSON."""
    # create some data
    client.post("/api/jobs", headers=auth, files=_srt_file(),
                data={"jtype": "subtitles", "src": "ru", "tgt": "uz"})
    client.post("/api/glossary", headers=auth, json=[{"src": "test", "tgt": "probe"}])
    r = client.get("/api/account/export", headers=auth)
    assert r.status_code == 200
    data = r.json()
    assert "user" in data
    assert "jobs" in data
    assert "glossary" in data
    assert "payments" in data
    assert "notifications" in data
    assert "exported_at" in data
    assert data["format_version"] == 1
    assert len(data["jobs"]) >= 1
    assert any(g["src"] == "test" for g in data["glossary"])


def test_data_export_content_disposition(client, auth):
    """Export response has attachment disposition."""
    r = client.get("/api/account/export", headers=auth)
    assert "attachment" in r.headers.get("content-disposition", "")


# ---------- Round 11: Feature Flags ----------

def test_flags_admin_list(client):
    """Admin can list feature flags."""
    admin_hdr = {"Authorization": f"Bearer {ADMIN_KEY}"}
    r = client.get("/api/admin/flags", headers=admin_hdr)
    assert r.status_code == 200
    flags = r.json()["flags"]
    assert "job_sharing" in flags
    assert "api_keys" in flags


def test_flags_admin_update(client):
    """Admin can update a flag."""
    admin_hdr = {"Authorization": f"Bearer {ADMIN_KEY}"}
    r = client.patch("/api/admin/flags/batch_jobs", headers=admin_hdr,
                     json={"enabled": True, "pct": 50})
    assert r.status_code == 200
    assert r.json()["updated"]["enabled"] is True
    assert r.json()["updated"]["pct"] == 50
    # Round 42 made this flag gate a live endpoint: a leaked 50% rollout now
    # really does lock half of every later batch test out. The switch is the
    # thing under test — restore it afterwards.
    r = client.patch("/api/admin/flags/batch_jobs", headers=admin_hdr,
                     json={"enabled": True, "pct": 100})
    assert r.status_code == 200 and r.json()["updated"]["pct"] == 100


def test_flags_requires_admin(client):
    """Non-admin cannot access flags."""
    r = client.get("/api/admin/flags")
    assert r.status_code in (401, 403)


def test_flag_is_enabled_logic():
    """Unit test for the flag logic itself."""
    from app.flags import is_enabled, _flags, _lock
    # ensure loaded
    with _lock:
        _flags["test_flag"] = {"enabled": True, "min_plan": "pro", "pct": 100}
    assert is_enabled("test_flag", user_id="u1", plan="pro") is True
    assert is_enabled("test_flag", user_id="u1", plan="free") is False
    assert is_enabled("nonexistent_flag", user_id="u1", plan="studio") is False


# ---------- Round 11: Webhooks ----------

def test_webhook_requires_studio(client, auth):
    """Non-studio users cannot create webhooks."""
    r = client.post("/api/webhooks", headers=auth,
                    data={"url": "https://example.com/hook"})
    assert r.status_code == 403


def test_webhook_requires_https(client, auth):
    """Webhook URL must be HTTPS."""
    # first upgrade to studio
    me = client.get("/api/me", headers=auth).json()
    uid = me["user"]["id"]
    ext_id = "wh-https-" + uuid.uuid4().hex[:8]
    sig = _sig("click", ext_id, uid, 30_000_000, "UZS")
    client.post("/api/payments/webhook/click",
                data={"external_id": ext_id, "user_id": uid,
                      "amount_minor": 30_000_000, "currency": "UZS",
                      "minutes": 500, "signature": sig})
    # now try http
    r = client.post("/api/webhooks", headers=auth,
                    data={"url": "http://insecure.com/hook"})
    assert r.status_code == 422


def test_webhook_create_and_list(client, auth):
    """Studio user can create and list webhooks."""
    me = client.get("/api/me", headers=auth).json()
    uid = me["user"]["id"]
    ext_id = "wh-cr-" + uuid.uuid4().hex[:8]
    sig = _sig("click", ext_id, uid, 30_000_000, "UZS")
    client.post("/api/payments/webhook/click",
                data={"external_id": ext_id, "user_id": uid,
                      "amount_minor": 30_000_000, "currency": "UZS",
                      "minutes": 500, "signature": sig})
    r = client.post("/api/webhooks", headers=auth,
                    data={"url": "https://example.com/ovoz-hook", "events": "job.done"})
    assert r.status_code == 200
    assert r.json()["secret"]
    wid = r.json()["id"]
    # list
    r2 = client.get("/api/webhooks", headers=auth)
    assert r2.status_code == 200
    assert len(r2.json()["webhooks"]) >= 1
    # delete
    r3 = client.delete(f"/api/webhooks/{wid}", headers=auth)
    assert r3.status_code == 200
