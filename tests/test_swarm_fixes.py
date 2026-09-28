"""Regressions the organized review swarm caught that no prior test did.

Three defects that shipped silently for many rounds:
- Outbound webhooks (a paid Studio feature) never fired: `get_active_webhooks`
  read a column it never selected, raised on every call, and the caller swallowed
  it -- so the product delivered zero webhooks while claiming the feature existed.
- The worker pool discarded every Future it submitted: if run_job itself died
  (a DB fault before its first CAS), the exception sat unread in the Future, the
  job stayed queued forever, and the customer's minutes were spent on nothing.
- Toasts were fixed-position z-60 while <dialog>.showModal() lives in the top
  layer above every z-index: feedback given while a modal was open was invisible.

(The romanizer's Cyrillic `ы` gap is a real mixed-script leak the swarm also found,
but fixing it globally regresses Russian code-switch romanization -- it needs a
language-aware ы->i (Uzbek) vs ы->y (Russian) mapping, tracked as its own moat task,
not a one-line map.)
"""
import time
from pathlib import Path

import pytest

from app import db


def test_active_webhooks_actually_filter_on_their_events(client, auth):
    """The bug: `SELECT url, secret` then `r["events"]` -> IndexError -> nothing fires."""
    uid = client.get("/api/me", headers=auth).json()["user"]["id"]
    db.create_webhook(uid, "https://example.invalid/hook", "sekret", "job.done")
    done = db.get_active_webhooks(uid, "job.done")
    assert any(w["url"].endswith("/hook") for w in done), done
    # only url/secret are returned to the firing path; subscription is honored
    assert set(done[0]) >= {"url", "secret"}, done[0]
    # a different event the hook never subscribed to must NOT receive it
    assert db.get_active_webhooks(uid, "job.failed") == []


def test_future_guard_rescues_a_job_whose_worker_died_before_any_cas(client, auth,
                                                                     monkeypatch):
    """The bug: _POOL.submit(pipeline.run_job, jid) dropped the Future; an exception
    raised before run_job's try-block was swallowed whole -- job stuck queued, credit
    spent, nothing ever failed or refunded it."""
    from app import billing, main as main_mod

    uid = client.get("/api/me", headers=auth).json()["user"]["id"]
    bal0 = billing.balance(uid)
    job = db.create_job(uid, "subtitles", "ru", "uz", 1.0, "x.srt", {})
    assert billing.charge_for_job(job) is True
    assert billing.balance(uid) == bal0 - 1

    def boom(jid):
        raise RuntimeError("db fell over before any CAS")

    monkeypatch.setattr(main_mod.pipeline, "run_job", boom)
    main_mod._dispatch_job(job["id"])
    # done-callback runs on the pool thread: poll briefly, never sleep blindly
    for _ in range(200):
        if db.get_job(job["id"])["status"] == "failed":
            break
        time.sleep(0.05)
    j = db.get_job(job["id"])
    assert j["status"] == "failed", j
    assert j["error"] == "internal worker fault"
    assert billing.balance(uid) == bal0, "the guard must refund the spent minute"


def test_future_guard_never_double_refunds_a_finished_job(client, auth):
    """The guard shares run_job's CAS: a job that already reached a terminal state
    must keep its ledger untouched (retry cycles legitimately reuse the same ref,
    so a broad refund_exists guard would be wrong -- the CAS is the guard)."""
    from app import billing, main as main_mod

    uid = client.get("/api/me", headers=auth).json()["user"]["id"]
    job = db.create_job(uid, "subtitles", "ru", "uz", 2.0, "x.srt", {})
    billing.charge_for_job(job)
    db.set_status_if(job["id"], "failed", ("queued",), error="honest failure")

    class _FakeFuture:
        @staticmethod
        def exception():
            return RuntimeError("late worker fault")

    bal = billing.balance(uid)
    main_mod._job_future_guard(_FakeFuture(), job["id"])
    assert billing.balance(uid) == bal, "CAS lost means no second credit"


def test_mix_dubbing_refuses_to_outrun_the_deadline(tmp_path):
    """The mixer used to have no deadline: thousands of cues or an hour-long tape
    could synthesize/mix far past the paid wall-clock, invisible to the timeout
    checks that only ran between pipeline steps."""
    from app.pipeline import _mix_dubbing
    from app.providers.base import Segment

    class _NoTTS:
        name = "fake"

        def synthesize(self, *a, **k):
            raise AssertionError("must not synthesize past the deadline")

    segs = [Segment(start=0.0, end=1.0, text="salom")]
    with pytest.raises(TimeoutError):
        _mix_dubbing("j1", segs, _NoTTS(), "uz", tmp_path,
                     deadline=time.monotonic() - 1)


def test_toasts_live_in_the_top_layer_next_to_the_dialogs():
    """showModal() paints above every z-index; a toast that is not in the top
    layer is silence exactly when the user needs to see the result of an action
    taken inside a dialog (delete account, pay, export)."""
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    css = (root / "styles.css").read_text(encoding="utf-8")
    assert 'id="toasts" aria-live="polite" popover="manual"' in html
    # re-elevation, not merely opening: a popover opened before the modal sits UNDER
    # it (top-layer order = append order), so every toast must hide-then-show to be
    # re-appended on top -- otherwise the fix only works for the first toast.
    assert "if (wrap.isOpen) wrap.hidePopover();" in js, \
        "a toast fired before a modal would stay beneath it"
    assert "wrap.showPopover();" in js, "nothing ever opens the popover container"
    # UA paints popovers with a white "bubblenorm" sheet + border + padding; the grid is transparent
    block = css[css.index("#toasts"):css.index("#toasts") + 400]
    assert "background: transparent" in block
    assert "border: 0; padding: 0" in block, "UA popover border/padding would frame the toasts"
    assert "bottom: 22px" in block, "the build-stamped mobile placement must survive"


# --- Round 35: the seven-agent full re-audit's money/security haul -----------

def test_retry_without_credits_leaves_the_canceled_job_untouched(client, auth, tmp_path):
    """The bug: claim->debit meant a debit failure rolled status back with a BLIND
    write, and a cancel landing between them refunded minutes never charged --
    credit minting. Debit-first: an underfunded retry must change nothing."""
    from app import billing, db

    uid = client.get("/api/me", headers=auth).json()["user"]["id"]
    src = tmp_path / "x.srt"
    src.write_text("1\n00:00:00,000 --> 00:00:01,000\nsalom\n", encoding="utf-8")
    job = db.create_job(uid, "subtitles", "ru", "uz", 50.0, str(src), {})
    db.update_job(job["id"], status="canceled")
    bal = billing.balance(uid)  # nowhere near 50
    r = client.post(f"/api/jobs/{job['id']}/retry", headers=auth)
    assert r.status_code == 402
    assert db.get_job(job["id"])["status"] == "canceled", "blind rollback stomped the state machine"
    assert billing.balance(uid) == bal, "a refused retry must move no money"


def test_recover_stale_jobs_refunds_each_row_at_most_once(client, auth):
    """Two overlapping boots used to SELECT all, UPDATE all, and refund all --
    twice. Per-row CAS means the second recovery sees nothing."""
    from app import db

    uid = client.get("/api/me", headers=auth).json()["user"]["id"]
    job = db.create_job(uid, "subtitles", "ru", "uz", 1.0, "x.srt", {})
    first = db.recover_stale_jobs()
    assert any(j["id"] == job["id"] for j in first)
    assert db.recover_stale_jobs() == [], "a row may be recovered by exactly one boot"


def test_delete_job_survives_share_rows(client, auth):
    """shares.job_id is an enforced FK: delete_job without share cleanup raised
    IntegrityError, so GDPR erasure 500-ed for every customer who ever used the
    paid share link -- sessions killed, data left, no self-service recovery."""
    from app import db

    uid = client.get("/api/me", headers=auth).json()["user"]["id"]
    job = db.create_job(uid, "subtitles", "ru", "uz", 1.0, "x.srt", {})
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO shares (id, job_id, user_id, created_at, expires_at) VALUES (?,?,?,?,?)",
        (db.new_id(), job["id"], uid, db.now_iso(), db.now_iso()))
    conn.commit()
    db.delete_job(job["id"])  # used to raise sqlite3.IntegrityError
    assert db.get_job(job["id"]) is None


def test_payment_replay_repairs_a_missing_credit(client, auth):
    """payments.insert and ledger.credit were separate commits: a crash between
    them made every PSP replay answer 'duplicate' and the paid amount vanished
    forever. The duplicate branch now verifies the credit and completes it."""
    from app import billing, db

    uid = client.get("/api/me", headers=auth).json()["user"]["id"]
    # simulate: payment recorded, credit lost
    db.create_payment(uid, "payme", "ext-repair-1", 12_000_000, "UZS", 120.0)
    assert not db.payment_credited("ext-repair-1")
    bal0 = billing.balance(uid)
    out = billing.apply_payment_webhook("payme", "ext-repair-1", uid,
                                        12_000_000, "UZS", 120.0)
    assert out["status"] == "duplicate"
    assert billing.balance(uid) == pytest.approx(bal0 + 120), "replay must repair the credit"
    # and remain exactly once on a third delivery
    billing.apply_payment_webhook("payme", "ext-repair-1", uid, 12_000_000, "UZS", 120.0)
    assert billing.balance(uid) == pytest.approx(bal0 + 120)


def test_safe_webhook_url_shuts_the_private_network_door():
    """The paid Studio webhook sender was an open SSRF proxy: startswith('https://')
    blocks nothing that matters. Literal private/link-local targets must all fail
    closed; a plain public IP passes without needing DNS."""
    from app.webhooks import safe_webhook_url

    assert safe_webhook_url("http://example.com/hook") is not None      # not https
    assert safe_webhook_url("https://127.0.0.1:8080/hook") is not None  # loopback
    assert safe_webhook_url("https://169.254.169.254/latest/meta-data/") is not None
    assert safe_webhook_url("https://10.0.0.5/hook") is not None        # RFC1918
    assert safe_webhook_url("https://172.16.0.9/hook") is not None
    assert safe_webhook_url("https://user:pass@example.com/") is not None  # userinfo
    assert safe_webhook_url("https://8.8.8.8/hook") is None             # public, no DNS needed


def test_webhook_registration_refuses_internal_targets(client, auth):
    """End-to-end through the paid endpoint, not just the helper."""
    from app import db

    uid = client.get("/api/me", headers=auth).json()["user"]["id"]
    db.set_user_plan(uid, "studio")
    r = client.post("/api/webhooks", headers=auth,
                    data={"url": "https://169.254.169.254/hook", "events": "job.done"})
    assert r.status_code == 422
    assert "private" in r.json()["detail"].lower() or "refused" in r.json()["detail"].lower()
