"""Regressions the organized review swarm caught that no prior test did.

Two defects that shipped silently for many rounds:
- Outbound webhooks (a paid Studio feature) never fired: `get_active_webhooks`
  read a column it never selected, raised on every call, and the caller swallowed
  it -- so the product delivered zero webhooks while claiming the feature existed.

(The romanizer's Cyrillic `ы` gap is a real mixed-script leak the swarm also found,
but fixing it globally regresses Russian code-switch romanization -- it needs a
language-aware ы->i (Uzbek) vs ы->y (Russian) mapping, tracked as its own moat task,
not a one-line map.)
"""
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
