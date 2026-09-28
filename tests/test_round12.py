"""Round 12 — onboarding, usage analytics, batch upload, per-endpoint rate limiting."""
from __future__ import annotations

from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"


def _srt_bytes():
    return (DEMO_DIR / "sample_ru.srt").read_bytes()


def test_onboarding_defaults_to_needs_tour(client, auth):
    r = client.get("/api/account/onboarding", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["needs_tour"] is True
    assert body["seen_at"] is None


def test_onboarding_mark_seen_idempotent(client, auth):
    r = client.post("/api/account/onboarding", headers=auth)
    assert r.status_code == 200
    assert r.json()["seen_at"]
    r2 = client.post("/api/account/onboarding", headers=auth)
    assert r2.json()["seen_at"] == r.json()["seen_at"]
    r3 = client.get("/api/account/onboarding", headers=auth)
    assert r3.json()["needs_tour"] is False


def test_usage_summary_shape(client, auth):
    r = client.get("/api/account/usage", headers=auth)
    assert r.status_code == 200
    body = r.json()
    for k in ("days", "daily_minutes", "jobs_by_type", "total_jobs",
              "total_minutes", "total_paid_minor", "streak_days",
              "balance_minutes", "plan"):
        assert k in body, k
    assert body["days"] == 30


def test_usage_reflects_job(client, auth):
    client.post("/api/jobs", headers=auth,
                files={"file": ("sample_ru.srt", _srt_bytes(), "text/plain")},
                data={"jtype": "subtitles", "src": "ru", "tgt": "uz"})
    body = client.get("/api/account/usage", headers=auth).json()
    assert body["total_jobs"] >= 1
    assert "subtitles" in body["jobs_by_type"]


def test_batch_upload_two_files(client, auth):
    b = _srt_bytes()
    files = [
        ("files", ("a.srt", b, "text/plain")),
        ("files", ("b.srt", b, "text/plain")),
    ]
    r = client.post("/api/jobs/batch", headers=auth, files=files,
                    data={"jtype": "subtitles", "src": "ru", "tgt": "uz"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] == 2 and body["total"] == 2
    assert all(x["ok"] for x in body["results"])


def test_batch_rejects_too_many(client, auth):
    one = ("files", ("x.srt", _srt_bytes(), "text/plain"))
    r = client.post("/api/jobs/batch", headers=auth, files=[one] * 11,
                    data={"jtype": "subtitles", "src": "ru", "tgt": "uz"})
    assert r.status_code == 422


def test_batch_partial_failure(client, auth):
    files = [
        ("files", ("good.srt", _srt_bytes(), "text/plain")),
        ("files", ("bad.exe", b"MZ\x90\x00", "application/octet-stream")),
    ]
    r = client.post("/api/jobs/batch", headers=auth, files=files,
                    data={"jtype": "subtitles", "src": "ru", "tgt": "uz"})
    assert r.status_code == 207, r.text
    body = r.json()
    assert body["created"] == 1 and body["total"] == 2
    oks = [x["ok"] for x in body["results"]]
    assert True in oks and False in oks


def test_batch_requires_auth(client):
    one = ("files", ("x.srt", _srt_bytes(), "text/plain"))
    r = client.post("/api/jobs/batch", files=[one],
                    data={"jtype": "subtitles", "src": "ru", "tgt": "uz"})
    assert r.status_code in (401, 403)


def test_endpoint_rate_limit_auth(client, monkeypatch):
    # Opt this test back into the endpoint limiter (off globally in tests).
    monkeypatch.setenv("OVOZ_ENDPOINT_RATE_LIMIT", "1")
    from app import main as m
    with m._EP_LOCK:
        m._ENDPOINT_BUCKETS.clear()
    # /api/auth/* capped at 20 req/min per identity
    codes = []
    for _ in range(25):
        r = client.post("/api/auth/login",
                        data={"contact": "+00000000", "secret": "wrong"})
        codes.append(r.status_code)
    assert 429 in codes, codes
    last = client.post("/api/auth/login",
                       data={"contact": "+00000000", "secret": "wrong"})
    assert last.status_code == 429
    assert last.headers.get("Retry-After")
    assert last.json()["error_code"] == "rate_limited"
