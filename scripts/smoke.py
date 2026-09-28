# -*- coding: utf-8 -*-
"""Смоук-тест живого сервера: python scripts/smoke.py [base_url]"""
import random
import sys
import time
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8077"
ROOT = Path(__file__).resolve().parent.parent

c = httpx.Client(base_url=BASE, timeout=30)

print("== health ==")
print(c.get("/healthz").json())

print("== register ==")
r = c.post("/api/auth/register",
           data={"name": "Smoke",
                 "contact": f"+99890{random.randint(10**7, 10**8-1)}",
                 "secret": "smoke-abc123"})  # min 10 chars — обязательное поле v0.4
r.raise_for_status()
token = r.json()["token"]
H = {"Authorization": f"Bearer {token}"}
print("balance:", c.get("/api/me", headers=H).json()["balance_minutes"])

def wait_done(job_id: str) -> dict:
    """Ждём завершения фоновой обработки."""
    for _ in range(50):
        d = c.get(f"/api/jobs/{job_id}", headers=H).json()
        if d["job"]["status"] in {"done", "failed"}:
            return d
        time.sleep(0.2)
    raise RuntimeError("job did not finish")


print("== subtitles ru->uz (demo/sample_ru.srt) ==")
f = (ROOT / "demo/sample_ru.srt").read_bytes()
job = c.post("/api/jobs", headers=H, files={"file": ("sample_ru.srt", f, "text/plain")},
             data={"jtype": "subtitles", "src": "ru", "tgt": "uz"}).json()["job"]
detail = wait_done(job["id"])
job = detail["job"]
print("status:", job["status"], "| minutes:", job["minutes"])
for ev in detail["timeline"]:
    print("  ·", ev["step"], "→", ev["message"])

srt = c.get(job["artifacts"]["srt"], headers=H).text
print("== subs_uz.srt ==")
print(srt)

print("== dubbing uz->ru (demo/sample_uz_cyr.txt) ==")
f2 = (ROOT / "demo/sample_uz_cyr.txt").read_bytes()
job2 = c.post("/api/jobs", headers=H, files={"file": ("sample_uz_cyr.txt", f2, "text/plain")},
              data={"jtype": "dubbing", "src": "uz", "tgt": "ru"}).json()["job"]
job2 = wait_done(job2["id"])["job"]
print("status:", job2["status"])
wav = c.get(job2["artifacts"]["dubbing"], headers=H).content
print("dubbing.wav:", len(wav), "bytes, header:", wav[:4])

print("\nSMOKE OK")
