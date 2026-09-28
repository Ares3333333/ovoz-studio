# -*- coding: utf-8 -*-
"""In-process realtime diagnostic.

Starts uvicorn inside this event loop, connects a real WebSocket client with a
single-use ticket, submits a job over HTTP and asserts the live channel
actually carries job_event frames. Everything runs in ONE loop, so the test
exercise the same call_soon_threadsafe path the pipeline worker threads hit in
production.

Usage: python scripts/probe_ws_live.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("SESSION_SECRET", "probe-session-secret-0123456789")
os.environ.setdefault("ADMIN_API_KEY", "probe-admin-key-0123456789")
os.environ.setdefault("STORAGE_BACKEND", "local")
# The probe shares the dev database; a real phone number would clash with the
# browser QA account and the collision would masquerade as an auth bug.
CONTACT = "+998900006660"

PORT = 8199
BASE = f"http://127.0.0.1:{PORT}"


def _http(path: str, fields: dict | None = None, file_field: str | None = None,
          filename: str = "", content: bytes = b"", token: str = "") -> tuple[int, dict]:
    """POST as multipart (with a file) or urlencoded form, like the browser does."""
    boundary = "probeboundary1234567890"
    lines: list[bytes] = []
    for k, v in (fields or {}).items():
        lines.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n"
                     .encode())
    if file_field:
        lines.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
                      f"filename=\"{filename}\"\r\nContent-Type: text/plain\r\n\r\n").encode()
                     + content + b"\r\n")
    body = (b"".join(lines) + f"--{boundary}--\r\n".encode()) if file_field else \
        urllib.parse.urlencode(fields or {}).encode()
    req = urllib.request.Request(BASE + path, data=body)
    req.add_header("Content-Type",
                   f"multipart/form-data; boundary={boundary}" if file_field
                   else "application/x-www-form-urlencoded")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


async def main() -> int:
    import uvicorn
    from app.main import app, _ws_hub

    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning",
                            lifespan="on", access_log=False)
    server = uvicorn.Server(config)
    serve = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)

    import websockets

    st, reg = await asyncio.to_thread(
        _http, "/api/auth/register",
        {"contact": CONTACT, "name": "RealtimeProbe", "secret": "probe-secret-123"})
    if st >= 400:
        st, reg = await asyncio.to_thread(
            _http, "/api/auth/login",
            {"contact": CONTACT, "secret": "probe-secret-123"})
    if "token" not in reg:
        print("AUTH FAILED:", st, reg)
        return 2
    print("auth:", st, reg["user"]["id"])
    token = reg["token"]
    st, ticket = await asyncio.to_thread(_http, "/api/auth/ws-ticket", {}, token=token)
    print("ws-ticket:", st, ticket)

    frames: list[dict] = []
    url = f"ws://127.0.0.1:{PORT}/ws/jobs?ticket={ticket['ticket']}"
    async with websockets.connect(url) as sock:
        async def pump():
            try:
                async for raw in sock:
                    frames.append(json.loads(raw))
            except Exception as exc:  # noqa: BLE001
                print("pump ended:", type(exc).__name__, exc)

        pump_task = asyncio.create_task(pump())
        await asyncio.sleep(0.3)
        st, job = await asyncio.to_thread(
            _http, "/api/jobs",
            {"jtype": "document", "src": "en", "tgt": "uz"},
            file_field="file", filename="a.txt",
            content=b"hello world\nsecond line\nthird line\n", token=token)
        jid = job.get("job", {}).get("id") or job.get("id")
        print("job:", st, jid, job.get("job", {}).get("status"))

        for _ in range(40):
            await asyncio.sleep(0.25)
            # stop early once the terminal frame landed
            if any(f.get("type") == "job_event" and f.get("step") in ("done", "failed")
                   for f in frames):
                break

        pump_task.cancel()

    steps = [f.get("step") for f in frames if f.get("type") == "job_event"]
    print("FRAMES:", [f.get("type") for f in frames])
    print("STEPS:", steps)
    print("HUB published:", _ws_hub.published, "dropped:", _ws_hub.dropped)
    server.should_exit = True
    await serve
    ok = "done" in steps
    print("RESULT:", "LIVE WS OK" if ok else "LIVE WS DELIVERS NOTHING")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
