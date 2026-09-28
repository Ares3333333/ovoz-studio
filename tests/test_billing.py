from app import billing, db


def _mk_user(tag: str) -> str:
    u = db.create_user("Tester", f"test-{tag}@ovoz.dev")
    return u["id"]


def test_credit_debit_balance(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "bind_db", lambda *a: None)
    uid = _mk_user("cd")
    assert billing.balance(uid) == 0
    billing.credit(uid, 10, "signup:bonus")
    assert billing.balance(uid) == 10
    assert billing.debit(uid, 4, "job:1") is True
    assert billing.balance(uid) == 6
    assert billing.debit(uid, 100, "job:2") is False  # не хватает — списания нет
    assert billing.balance(uid) == 6


def test_webhook_idempotent(tmp_path, monkeypatch):
    uid = _mk_user("wh")
    r1 = billing.apply_payment_webhook("payme", "ext-001", uid, 12000000, "UZS", 120)
    r2 = billing.apply_payment_webhook("payme", "ext-001", uid, 12000000, "UZS", 120)
    assert r1["status"] == "ok"
    assert r2["status"] == "duplicate"
    assert billing.balance(uid) == 120


def test_refund(tmp_path):
    uid = _mk_user("rf")
    job = db.create_job(uid, "subtitles", "uz", "ru", 3.0, "x.srt", {})
    billing.credit(uid, 10, "signup")
    assert billing.charge_for_job(job) is True
    assert billing.balance(uid) == 7
    billing.refund_job(job)
    assert billing.balance(uid) == 10
