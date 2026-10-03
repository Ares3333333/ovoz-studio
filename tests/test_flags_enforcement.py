"""Round 42: feature flags became real kill switches.

Field evidence (01.10): `flags.is_enabled()` had zero call sites — the admin
panel listed switches, `/api/v1/info` advertised "feature_flags" as a feature,
and flipping anything changed nothing. These tests pin the opposite contract:

* every flag in _DEFAULTS gates a named endpoint (no decorative flags);
* the flag check runs BEFORE plan/ownership checks, so a disabled feature
  answers identically to everyone and leaks nothing behind it;
* stale defaults written by the old release are migrated, deliberate operator
  overrides are not touched.
"""
import copy
import json

import pytest

from app import flags


@pytest.fixture()
def switch():
    """Toggle a flag and hand the module's flag table back untouched — in
    memory AND on disk, because set_flag persists immediately and a leaked
    disabled flag would blind the next test session booting from that file."""
    saved = copy.deepcopy(flags.get_all())
    yield lambda flag, **kw: flags.set_flag(flag, **kw)
    with flags._lock:
        flags._flags = saved
        flags._save()


def test_every_default_flag_gates_a_real_call_site():
    """A flag nobody reads is decoration. Each default must appear either in a
    _require_flag(...) call or in the fail-open _ling_guard lookup — enforced
    by source, not by trust."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    gated = {f for f in flags._DEFAULTS
             if f'_require_flag("{f}"' in src or f'_flags_all().get("{f}")' in src}
    assert gated == set(flags._DEFAULTS), \
        f"decorative flags survived: {set(flags._DEFAULTS) - gated}"


def test_share_kill_switch_precedes_ownership(client, auth, switch):
    # enabled: unknown job answers 404 (ownership logic runs)
    r = client.post("/api/jobs/nosuchjob/share", headers=auth, data={})
    assert r.status_code == 404
    # disabled: the same call must not reach ownership logic at all
    switch("job_sharing", enabled=False)
    r = client.post("/api/jobs/nosuchjob/share", headers=auth, data={})
    assert r.status_code == 403
    assert r.json()["detail"] == "Feature disabled"


def test_batch_kill_switch(client, auth, switch):
    srt = b"1\n00:00:01,000 --> 00:00:02,000\nSalom\n"
    switch("batch_jobs", enabled=False)
    r = client.post("/api/jobs/batch", headers=auth,
                    data={"jtype": "subtitles", "src": "uz", "tgt": "ru"},
                    files=[("files", ("a.srt", srt, "application/octet-stream"))])
    assert r.status_code == 403
    assert r.json()["detail"] == "Feature disabled"
    switch("batch_jobs", enabled=True)
    r = client.post("/api/jobs/batch", headers=auth,
                    data={"jtype": "subtitles", "src": "uz", "tgt": "ru"},
                    files=[("files", ("a.srt", srt, "application/octet-stream"))])
    assert r.status_code != 403  # the switch is the only thing that said no


def test_api_keys_switch_precedes_plan_text(client, auth, switch):
    # enabled + free plan: the plan gate answers
    r = client.post("/api/account/keys", headers=auth, data={"label": "x"})
    assert r.status_code == 403
    assert "Pro" in r.json()["detail"]
    # disabled: same caller, different (earlier, non-leaky) answer
    switch("api_keys", enabled=False)
    r = client.post("/api/account/keys", headers=auth, data={"label": "x"})
    assert r.status_code == 403
    assert r.json()["detail"] == "Feature disabled"


def test_webhook_switch_precedes_plan_and_ssrf_checks(client, auth, switch):
    from app import db
    uid = client.get("/api/me", headers=auth).json()["user"]["id"]
    db.set_user_plan(uid, "studio")
    switch("webhook_out", enabled=False)
    r = client.post("/api/webhooks", headers=auth,
                    data={"url": "https://169.254.169.254/hook", "events": "job.done"})
    assert r.status_code == 403
    assert r.json()["detail"] == "Feature disabled"
    switch("webhook_out", enabled=True)
    r = client.post("/api/webhooks", headers=auth,
                    data={"url": "https://169.254.169.254/hook", "events": "job.done"})
    assert r.status_code == 422  # past the switch, into the SSRF gate


def test_load_migrates_stale_defaults_but_keeps_operator_choice(switch, tmp_path, monkeypatch):
    # `switch` is not toggled here; it is requested so the global table (which
    # load() rewrites from the tmp file) is restored after the assertions.
    stale = {
        "job_sharing": {"enabled": True, "min_plan": "free", "pct": 100},
        "api_keys": {"enabled": True, "min_plan": "pro", "pct": 100},
        "webhook_out": {"enabled": False, "min_plan": "studio", "pct": 0},
        "batch_jobs": {"enabled": False, "min_plan": "studio", "pct": 10},
    }
    path = tmp_path / "feature_flags.json"
    path.write_text(json.dumps(stale), encoding="utf-8")
    monkeypatch.setattr(flags, "_FLAGS_FILE", path)
    flags.load()
    all_flags = flags.get_all()
    assert all_flags["webhook_out"] == flags._DEFAULTS["webhook_out"]
    assert all_flags["batch_jobs"] == flags._DEFAULTS["batch_jobs"]

    deliberate = {
        "webhook_out": {"enabled": True, "min_plan": "free", "pct": 5},
    }
    path.write_text(json.dumps(deliberate), encoding="utf-8")
    flags.load()
    all_flags = flags.get_all()
    assert all_flags["webhook_out"] == deliberate["webhook_out"], \
        "an operator's deliberate rollout was steamrolled by a migration"
