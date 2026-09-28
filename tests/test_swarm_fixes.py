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
