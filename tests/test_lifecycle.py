"""Cancel/retry жизненного цикла jobs и оценка длительности через ffprobe."""
import subprocess
from pathlib import Path

import pytest

from app import main as main_mod

ROOT = Path(__file__).resolve().parent.parent


def _upload(client, auth, name="sample_ru.srt"):
    f = (ROOT / "demo" / name).read_bytes()
    r = client.post("/api/jobs", headers=auth, files={"file": (name, f, "text/plain")},
                    data={"jtype": "subtitles", "src": "ru", "tgt": "uz"})
    assert r.status_code == 201
    return r.json()["job"]


def test_cancel_only_queued(client, auth):
    job = _upload(client, auth)  # sync-пайплайн ⇒ сразу done
    r = client.post(f"/api/jobs/{job['id']}/cancel", headers=auth)
    assert r.status_code == 409  # отменить выполненную нельзя


def test_retry_canceled_job(client, auth):
    job = _upload(client, auth)
    # доведём статус вручную до canceled, чтобы проверить путь retry без фонового воркера
    from app import db
    db.update_job(job["id"], status="canceled")
    from app import billing
    billing.refund_job(db.get_job(job["id"]), reason="refund:canceled")
    bal_before = client.get("/api/me", headers=auth).json()["balance_minutes"]
    r = client.post(f"/api/jobs/{job['id']}/retry", headers=auth)
    assert r.status_code == 200
    assert r.json()["job"]["status"] == "done"  # sync-режим: прогоняется сразу
    bal_after = client.get("/api/me", headers=auth).json()["balance_minutes"]
    assert bal_after == pytest.approx(bal_before - job["minutes"])


def test_retry_wrong_status_409(client, auth):
    job = _upload(client, auth)
    r = client.post(f"/api/jobs/{job['id']}/retry", headers=auth)
    assert r.status_code == 409


@pytest.mark.skipif(
    subprocess.run(["ffprobe", "-version"], capture_output=True).returncode != 0,
    reason="ffprobe not installed",
)
def test_probe_duration_on_generated_wav(tmp_path):
    # 24-секундный wav через встроенный StubTTS: ffprobe должен измерить его точно
    # (3 сек не берём — минимальный биллинг 0.1 мин скроет ошибку измерения)
    from app.providers.tts import StubTTS
    wav = tmp_path / "t.wav"
    StubTTS().synthesize("test", "uz", wav, dur_sec=24.0)
    minutes = main_mod._probe_duration(wav)
    assert 0.35 < minutes < 0.45  # ~24 сек = 0.4 мин
