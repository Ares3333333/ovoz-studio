"""Закалка v0.3: атомарный debit, идемпотентный refund, CAS-статусы, recovery."""
import uuid

from app import billing, db


def _uid() -> str:
    u = db.create_user("H", "+99890h" + uuid.uuid4().hex[:8])
    billing.credit(u["id"], 10, "signup:bonus")
    return u["id"]


def test_debit_atomic_rejects_overdraft():
    uid = _uid()
    assert db.debit_atomic(uid, 10.5, "job:x", ref="x1") is False
    assert billing.balance(uid) == 10  # ничего не списалось
    assert db.debit_atomic(uid, 10, "job:x", ref="x2") is True
    assert billing.balance(uid) == 0
    assert db.debit_atomic(uid, 0.1, "job:x", ref="x3") is False


def test_refund_uses_job_minutes_and_survives_retry_cycle():
    """Возврат не блокируется broad-guard'ом: после retry нужен новый возврат."""
    uid = _uid()
    job = db.create_job(uid, "subtitles", "ru", "uz", 2.0, "/dev/null", {})
    assert billing.debit(uid, job["minutes"], f"job:{job['id']}", ref=job["id"])
    assert billing.balance(uid) == 8
    assert billing.refund_job(job, reason="refund:failed") is True
    assert billing.balance(uid) == 10
    # цикл retry: снова списали, снова вернули — легитимно
    assert billing.debit(uid, job["minutes"], f"job:{job['id']}", ref=job["id"])
    assert billing.refund_job(job, reason="refund:failed") is True
    assert billing.balance(uid) == 10


def test_set_status_if_cas():
    uid = _uid()
    job = db.create_job(uid, "subtitles", "ru", "uz", 0.5, "/dev/null", {})
    assert db.set_status_if(job["id"], "running", ("queued",)) is True
    assert db.set_status_if(job["id"], "running", ("queued",)) is False  # уже не queued
    assert db.set_status_if(job["id"], "canceled", ("queued",)) is False  # не из running
    assert db.set_status_if(job["id"], "failed", ("running",), error="boom") is True
    assert db.get_job(job["id"])["error"] == "boom"


def test_cancel_and_claim_are_atomic():
    uid = _uid()
    job = db.create_job(uid, "subtitles", "ru", "uz", 0.5, "/dev/null", {})
    assert db.cancel_job(job["id"]) is True
    assert db.cancel_job(job["id"]) is False
    assert db.claim_for_retry(job["id"]) is True   # canceled → queued
    assert db.claim_for_retry(job["id"]) is False  # уже queued


def test_failed_job_refunds_exactly_once_via_cas():
    """Двойной возврат невозможен: failed-переход CAS'ом выигрывает только один."""
    uid = _uid()
    job = db.create_job(uid, "subtitles", "ru", "uz", 1.5, "/dev/null", {})
    billing.debit(uid, job["minutes"], f"job:{job['id']}", ref=job["id"])
    db.set_status_if(job["id"], "running", ("queued",))
    first = db.set_status_if(job["id"], "failed", ("running",), error="boom")
    second = db.set_status_if(job["id"], "failed", ("running",), error="boom")
    assert first is True and second is False  # проигравший не имеет права на возврат


def test_recover_stale_jobs_marks_failed():
    uid = _uid()
    job = db.create_job(uid, "dubbing", "ru", "uz", 1.0, "/dev/null", {})
    stale = db.recover_stale_jobs()
    assert any(j["id"] == job["id"] for j in stale)
    after = db.get_job(job["id"])
    assert after["status"] == "failed"
    assert after["error"] == "worker restart"
