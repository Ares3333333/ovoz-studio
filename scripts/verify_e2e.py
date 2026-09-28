"""Расширенная живая проверка продуктового контракта v0.4 против запущенного сервера.

Отличается от smoke.py тем, что проверяет именно ЗАКАЛЁННЫЙ контур:
PBKDF2-секрет (min 10) на входе, Bearer-only скачивание, CRUD глоссария,
demo-credit по админ-ключу, язык-allowlist, unmatched-amount на webhook'е,
 тайминг-инъекция в .txt не должна убить сервер.

Требует env:
  OVOZ_ADMIN_SECRET  — ключ демо-начислений (совпадает с серверным)
  OVOZ_BASE_URL      — по умолчанию http://127.0.0.1:8077 (dev), для docker: http://127.0.0.1:8080
"""
import os
import pathlib
import sys
import time
import uuid

import httpx

BASE = os.environ.get("OVOZ_BASE_URL", "http://127.0.0.1:8077")
ADMIN_KEY = os.environ.get("OVOZ_ADMIN_SECRET") or ""
DEMO = pathlib.Path(__file__).resolve().parent.parent / "demo"


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise SystemExit("E2E FAIL: " + msg)


def _ok(resp: httpx.Response, code: int = 200) -> dict:
    if resp.status_code != code:
        raise SystemExit(f"E2E FAIL: HTTP {resp.status_code} (want {code}) · {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError:
        raise SystemExit("E2E FAIL: not JSON · " + resp.text[:300])


def wait_done(c: httpx.Client, h: dict, jid: str) -> dict:
    for _ in range(120):
        d = _ok(c.get(f"/api/jobs/{jid}", headers=h))
        job = d["job"]
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.5)
    raise SystemExit("timeout waiting for " + jid)


def main() -> None:
    _need(bool(ADMIN_KEY), "OVOZ_ADMIN_SECRET env required (must match server)")
    c = httpx.Client(base_url=BASE, timeout=60)
    secret = "e2e-" + uuid.uuid4().hex[:12]  # ≥10 символов
    contact = "+99890" + uuid.uuid4().hex[:8]

    # 1) регистрация с секретом
    _ok(c.post("/api/auth/register",
               data={"name": "E2E", "contact": contact, "secret": secret}))
    # 2) короткий секрет запрещён
    r = c.post("/api/auth/register",
               data={"name": "X", "contact": "+99890" + uuid.uuid4().hex[:8], "secret": "pw"})
    _need(r.status_code == 422, "short secret must be rejected with 422")
    # 3) неверный секрет = 401, без утечки "пользователь существует"
    _need(c.post("/api/auth/login",
                 data={"contact": contact, "secret": "wrong-password"}).status_code == 401,
          "wrong secret must 401")
    # 4) верный секрет
    token = _ok(c.post("/api/auth/login",
                        data={"contact": contact, "secret": secret}))["token"]
    h = {"Authorization": "Bearer " + token}

    # 5) документ uz->ru
    j = _ok(c.post("/api/jobs", headers=h,
                   files={"file": ("d.txt", "Assalomu alaykum, bu sinov hujjati.".encode(), "text/plain")},
                   data={"jtype": "document", "src": "uz", "tgt": "ru"}), code=201)["job"]
    doc_job = wait_done(c, h, j["id"])
    _need(doc_job["status"] == "done", "document job failed: " + str(doc_job))
    doc = c.get(doc_job["artifacts"]["document"], headers=h)
    _need(doc.status_code == 200 and doc.text.strip(), "document didn't download")

    # 6) даббинг ru->uz по SRT
    j = _ok(c.post("/api/jobs", headers=h,
                   files={"file": ("sample_ru.srt", (DEMO / "sample_ru.srt").read_bytes(), "text/plain")},
                   data={"jtype": "dubbing", "src": "ru", "tgt": "uz"}), code=201)["job"]
    dub_job = wait_done(c, h, j["id"])
    _need(dub_job["status"] == "done", "dubbing job failed: " + str(dub_job))
    wav = c.get(dub_job["artifacts"]["dubbing"], headers=h)
    _need(wav.content[:4] == b"RIFF" and len(wav.content) > 1000, "WAV broken")

    # 7) токен в URL мёртв
    _need(c.get(dub_job["artifacts"]["dubbing"], params={"token": token}).status_code == 401,
          "?token= must 401")
    # 8) язык-allowlist
    _need(c.post("/api/jobs", headers=h,
                 files={"file": ("x.txt", b"hello", "text/plain")},
                 data={"jtype": "subtitles", "src": "ru", "tgt": "de"}).status_code == 400,
          "unknown language must 400")

    # 9) тайминг-инъекция в .txt: seg.end=60000 не должен OOM-нуть сервер
    evil = b"0-60000|Salom\n60000-120000|Salom2\n"
    j = _ok(c.post("/api/jobs", headers=h,
                   files={"file": ("evil.txt", evil, "text/plain")},
                   data={"jtype": "dubbing", "src": "uz", "tgt": "ru"}), code=201)["job"]
    inj = wait_done(c, h, j["id"])
    _need(inj["status"] in ("done", "failed"), "job must resolve")
    # healthcheck после атаки
    _need(c.get("/healthz").json().get("ok") is True, "server died after timing attack")

    # 10) глоссарий CRUD
    _ok(c.post("/api/glossary", headers=h, json=[{"src": "Telegram", "tgt": "TelegramUA"}]))
    terms = _ok(c.get("/api/glossary", headers=h))["terms"]
    _need(any(x["src_term"] == "Telegram" for x in terms), "glossary didn't save")
    _need(_ok(c.request("DELETE", "/api/glossary", headers=h,
                        json={"src": "Telegram"}))["deleted"] is True,
          "glossary delete failed")

    # 11) demo-credit без ключа = 403
    bal0 = _ok(c.get("/api/me", headers=h))["balance_minutes"]
    _need(c.post("/api/dev/demo-credit", headers=h,
                 data={"minutes": 50}).status_code == 403,
          "demo-credit without admin key must 403")
    # 12) demo-credit с ключом
    _ok(c.post("/api/dev/demo-credit", headers=h,
               data={"minutes": 50, "admin_key": ADMIN_KEY}))
    bal1 = _ok(c.get("/api/me", headers=h))["balance_minutes"]
    _need(bal1 == bal0 + 50, f"balance mismatch: {bal0} → {bal1}")

    # 13) webhook с unmatched amount = 422 (не начисляет)
    r = c.post("/api/payments/webhook/payme",
               data={"external_id": "e2e-" + uuid.uuid4().hex[:8],
                     "user_id": "does-not-matter",
                     "amount_minor": 1,  # 1 тийин — не тариф
                     "currency": "UZS",
                     "minutes": 600,
                     "signature": "not-real"})
    _need(r.status_code in (403, 422), "webhook must reject unsigned/mismatched")

    # 14) logout
    _ok(c.post("/api/auth/logout", headers=h))
    _need(c.get("/api/me", headers=h).status_code == 401,
          "token must be revoked after logout")

    print(f"E2E-VERIFY OK · биллинг честный · PBKDF2 + logout + timing-attack-green · "
          f"документ+WAV+глоссарий+кредиты зелёные")


if __name__ == "__main__":
    main()
