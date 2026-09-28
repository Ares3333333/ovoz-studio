"""Ovoz Studio — HTTP API (FastAPI) + статический фронтенд."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import (Depends, FastAPI, File, Form, Header, HTTPException,
                     Query, Request, UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from . import __version__, billing, db, pipeline, telegram
from .providers import asr as p_asr
from .providers import translate as p_tr
from .providers import tts as p_tts
from .ling import align as align_mod
from .ling import diarize as diar_mod
from .ling import engine as ling
from .ling import layout as lay_mod
from .ling import nafis as nafis_mod
from .ling import srt as srt_mod
from .ling import word as word_mod
from .ling.romanizer import normalize_target as _ling_target, normalize_uzbek
from .config import admin_secret, settings, webhook_secret
from .flags import load as _flags_load, get_all as _flags_all, set_flag as _flag_set
from .pipeline import JOB_TYPES
from .errors import ErrorCode
from .notify import notify, NotifKind, maybe_notify_balance_low

# FastAPI RequestValidationError import (handler registered after app creation)
from fastapi.exceptions import RequestValidationError as _FastAPIValError

# --- structured JSON logging → stdout (logs aggregators friendly) ---
_RESERVED_LOG_ATTRS = frozenset(vars(logging.LogRecord(
    "", 0, "", 0, "", (), None)).keys()) | {"message", "asctime", "taskName"}


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "lvl": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Anything passed via `extra=` is part of the record, not a comment:
        # drop-list of reserved LogRecord attributes keeps the line JSON-clean.
        for k, v in record.__dict__.items():
            if k in _RESERVED_LOG_ATTRS or k.startswith("_"):
                continue
            payload.setdefault(k, v)
        # default=str: one non-serializable extra (a Path, a sqlite3.Row) must not
        # cost us the whole log line — StreamHandler would swallow it silently.
        return json.dumps(payload, ensure_ascii=False, default=str)


_root = logging.getLogger("ovoz")
if not _root.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(_JSONFormatter())
    _root.addHandler(_h)
    # Ops needs to be able to turn on the realtime trace without a code change.
    _root.setLevel(os.getenv("OVOZ_LOG_LEVEL", "INFO").upper())
log = logging.getLogger("ovoz.app")

LANGS = {"uz", "ru", "en"}

# --- константы политики безопасности ---
PBKDF2_ITERS = 120_000                       # OWG 2023 floor for PBKDF2-HMAC-SHA256
SECRET_MIN_LEN = 10
MAX_UPLOAD_BYTES = 4 * 1024 * 1024           # 4 МБ — документ/аудио на MVP
MAX_BODY_BYTES = MAX_UPLOAD_BYTES + 512_000  # + форма
MAX_ACTIVE_JOBS_PER_USER = 5                 # flood-guard
DAILY_QUOTA_MINUTES = {"free": 30, "pro": 240, "studio": 1000}  # per-plan soft ceiling
# How far past a limit we still read a rejected body (see _drain_body).
_REFUSAL_DRAIN_SLACK = 1_048_576
# …and for how long: draining is bounded in bytes *and* wall-clock, so a client
# that dribbles one byte per second past the cap cannot hold a refusal open.
_REFUSAL_DRAIN_SECONDS = 3.0
GLOBAL_WORKER_SLOTS = 8                      # ThreadPoolExecutor size
MAX_JOB_TIMELINE_SEC = 30 * 60               # потолок таймлайна job'а
ALLOWED_EXT_AUDIO = {".wav", ".mp3", ".m4a", ".ogg", ".mp4", ".mov"}
ALLOWED_EXT_TEXT = {".txt", ".srt", ".vtt", ".md"}
ALLOWED_UPLOAD_EXT = ALLOWED_EXT_AUDIO | ALLOWED_EXT_TEXT

# --- in-memory rate limiter: LRU + скользящее окно + IP-контакт ---
_FAILS: "OrderedDict[str, list[float]]" = OrderedDict()
_FAIL_LOCK = threading.Lock()
_FAIL_WINDOW_SEC = 900.0
_FAIL_LIMIT = 8
_FAIL_MAX_KEYS = 10_000


def _throttle(key: str) -> None:
    now = time.monotonic()
    with _FAIL_LOCK:
        stamps = [t for t in _FAILS.get(key, []) if now - t < _FAIL_WINDOW_SEC]
        remaining = _FAIL_LIMIT - len(stamps)
        if remaining <= 0:
            raise HTTPException(429, "Too many failed attempts, try later",
                                headers={"Retry-After": "900",
                                         "X-RateLimit-Limit": str(_FAIL_LIMIT),
                                         "X-RateLimit-Remaining": "0"})
        _FAILS[key] = stamps
        _FAILS.move_to_end(key)
        # evict: по размеру, а не по ключу
        while len(_FAILS) > _FAIL_MAX_KEYS:
            _FAILS.popitem(last=False)


def _fail(key: str) -> None:
    now = time.monotonic()
    with _FAIL_LOCK:
        _FAILS.setdefault(key, []).append(now)
        _FAILS.move_to_end(key)
        while len(_FAILS) > _FAIL_MAX_KEYS:
            _FAILS.popitem(last=False)


def _client_ip(request: Request) -> str:
    """Берём socket IP. XFF доверяется только если запрос пришёл с доверенного прокси."""
    peer = request.client.host if request.client else "anon"
    trusted = os.environ.get("OVOZ_TRUSTED_PROXIES", "")
    if trusted and peer in {p.strip() for p in trusted.split(",") if p.strip()}:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return peer


# --- per-endpoint sliding-window rate limiter (requests per minute) ---
_ENDPOINT_BUCKETS: "OrderedDict[str, list[float]]" = OrderedDict()
_EP_LOCK = threading.Lock()
_EP_WINDOW_SEC = 60.0
_EP_MAX_KEYS = 20_000
# path-prefix -> limit-per-minute. First matching prefix wins (order matters).
# The two listening routes are a different cost class from the text ones: one
# request reads every sample of a five-megabyte tape, so their lines must come
# BEFORE "/api/v1/ling" — otherwise an anonymous stranger buys 60 engine runs a
# minute instead of 12, which is the difference between a demo and a denial.
_ENDPOINT_LIMITS: list[tuple[str, int]] = [
    ("/api/auth/", 20),
    ("/api/jobs", 60),
    ("/api/glossary", 120),
    ("/api/account/export", 6),
    ("/api/admin/", 30),
    ("/api/v1/ling/align", 12),
    ("/api/v1/ling/words", 12),
    ("/api/v1/ling", 60),
]
_EP_DEFAULT_LIMIT = 300


def _endpoint_limit(path: str) -> int:
    for prefix, lim in _ENDPOINT_LIMITS:
        if path.startswith(prefix):
            return lim
    return _EP_DEFAULT_LIMIT


def _rate_class(path: str) -> str:
    """Collapse /api/jobs/{id}/... into a shared bucket keyed by the matched
    prefix, so per-id paths don't each get a fresh allowance."""
    for prefix, _lim in _ENDPOINT_LIMITS:
        if path.startswith(prefix):
            return prefix
    # default: bucket by first 2 path segments (e.g. /api/account)
    return "/".join(path.split("/")[:3]) or path


def _endpoint_throttle_or_none(key: str, limit: int) -> Optional[int]:
    """Record a hit. Return None if allowed, else seconds until a slot frees."""
    now = time.monotonic()
    with _EP_LOCK:
        stamps = [t for t in _ENDPOINT_BUCKETS.get(key, []) if now - t < _EP_WINDOW_SEC]
        if len(stamps) >= limit:
            retry = int(_EP_WINDOW_SEC - (now - stamps[0])) + 1
            _ENDPOINT_BUCKETS[key] = stamps
            _ENDPOINT_BUCKETS.move_to_end(key)
            return max(1, retry)
        stamps.append(now)
        _ENDPOINT_BUCKETS[key] = stamps
        _ENDPOINT_BUCKETS.move_to_end(key)
        while len(_ENDPOINT_BUCKETS) > _EP_MAX_KEYS:
            _ENDPOINT_BUCKETS.popitem(last=False)
        return None


# --- hashing (PBKDF2-HMAC-SHA256, self-describing) ---
def _hash_secret_pbkdf2(secret: str) -> tuple[str, str, str]:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"),
                             bytes.fromhex(salt), PBKDF2_ITERS)
    algo = f"pbkdf2_sha256${PBKDF2_ITERS}"
    return dk.hex(), salt, algo


def _verify_secret(secret: str, stored_hash: str | None,
                   salt: str | None, algo: str | None) -> bool:
    if not stored_hash:
        return False
    secret = (secret or "").strip()
    if not secret:
        return False
    if algo and algo.startswith("pbkdf2_sha256$"):
        try:
            iters = int(algo.split("$", 1)[1])
        except (ValueError, IndexError):
            return False
        try:
            dk = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"),
                                     bytes.fromhex(salt or ""), iters)
        except ValueError:
            return False
        return hmac.compare_digest(dk.hex(), stored_hash)
    # legacy: unsalted SHA-256 (миграция на входе)
    legacy = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy, stored_hash)


# --- contact normalization (E.164-lite + email lowercase) ---
def _normalize_contact(raw: str) -> str:
    c = (raw or "").strip()
    if not c:
        return ""
    if "@" in c:
        return c.lower().replace(" ", "")
    # phone: оставляем только цифры и ведущий +
    digits = "".join(ch for ch in c if ch.isdigit())
    if not digits:
        return c.lower()
    if c.startswith("+"):
        return "+" + digits
    return "+" + digits  # нормализуем к E.164-lite; валидатор — на фронте


# --- worker pool: вместо «поток на job» ---
_POOL = ThreadPoolExecutor(max_workers=GLOBAL_WORKER_SLOTS,
                           thread_name_prefix="ovoz-worker")


# --- magic-byte validation for audio/video uploads ---
_MAGIC_SIGS: dict[str, list[bytes]] = {
    ".wav": [b"RIFF"],
    ".mp3": [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"],
    ".m4a": [b"\x00\x00\x00", b"ftyp"],  # ISO base media: offset 4-8 = 'ftyp'
    ".ogg": [b"OggS"],
    ".mp4": [b"\x00\x00\x00", b"ftyp"],
    ".mov": [b"\x00\x00\x00", b"ftyp"],
}


def _validate_magic(data: bytes, ext: str) -> bool:
    """Check first bytes match expected container format."""
    sigs = _MAGIC_SIGS.get(ext)
    if not sigs or len(data) < 12:
        return True  # unknown ext or too short — pass through
    if ext in (".m4a", ".mp4", ".mov"):
        # ISO BMFF: bytes 4-8 should be 'ftyp'
        return data[4:8] == b"ftyp"
    return any(data.startswith(s) for s in sigs)


def _dispatch_job(jid: str) -> None:
    """Отправить job в пул. Если очередь пула переполнена — поток воркера
    подождёт в FIFO (ThreadPoolExecutor default)."""
    _POOL.submit(pipeline.run_job, jid)


# --- WebSocket hub: broadcast job events to connected clients ---
class _Sub:
    """One live socket's delivery channel: the queue plus the loop that owns it.

    The loop must be captured explicitly. asyncio.Queue's own `get_loop` getter
    was removed in Python 3.10, and inferring it any other way from a pipeline
    worker thread is how the realtime channel can end up publishing into nothing.
    """
    __slots__ = ("queue", "loop")

    def __init__(self, queue: "asyncio.Queue", loop) -> None:
        self.queue = queue
        self.loop = loop


class _WSHub:
    """Thread-safe pub/sub for job progress events."""
    def __init__(self):
        self._lock = threading.Lock()
        # user_id -> set of _Sub
        self._subscribers: dict[str, set] = {}
        self.published = 0
        self.dropped = 0

    def _count(self, which: str) -> None:
        """Counters are touched by pipeline threads and by the event loop thread at
        once, so they move under the lock (a lost update here means /metrics lies
        exactly when the hub is busiest)."""
        with self._lock:
            if which == "published":
                self.published += 1
            else:
                self.dropped += 1

    def try_subscribe(self, uid: str, queue, loop, limit: int) -> "_Sub | None":
        """Admit one socket per user atomically: checking `count` and then
        subscribing lets N concurrent handshakes all pass the gate."""
        sub = _Sub(queue, loop)
        with self._lock:
            subs = self._subscribers.setdefault(uid, set())
            if len(subs) >= limit:
                return None
            subs.add(sub)
        return sub

    def subscribe(self, uid: str, queue, loop) -> _Sub:
        sub = _Sub(queue, loop)
        with self._lock:
            self._subscribers.setdefault(uid, set()).add(sub)
        return sub

    def unsubscribe(self, uid: str, sub: _Sub) -> None:
        with self._lock:
            subs = self._subscribers.get(uid)
            if subs:
                subs.discard(sub)
                if not subs:
                    del self._subscribers[uid]

    def count(self, uid: str) -> int:
        with self._lock:
            return len(self._subscribers.get(uid, ()))

    def uids(self) -> list:
        """Users with at least one live socket (for the /metrics gauge)."""
        with self._lock:
            return [uid for uid, subs in self._subscribers.items() if subs]

    def broadcast(self, uid: str, event: dict) -> None:
        """Called from pipeline worker threads (sync context).
        Hands the event to the owning loop with call_soon_threadsafe."""
        with self._lock:
            subs = tuple(self._subscribers.get(uid, ()))
        for sub in subs:
            try:
                if sub.loop.is_closed():
                    self._count("dropped")
                    continue

                # Delivery is decided on the loop itself: a queue that fills
                # between any pre-check and the put would otherwise raise inside
                # the loop, where nothing is logging — the exact shape of the
                # incident that killed this channel silently.
                def _deliver(q=sub.queue, ev=event):
                    try:
                        q.put_nowait(ev)
                        self._count("published")
                    except asyncio.QueueFull:
                        # Slow or stalled client: drop the frame rather than grow an
                        # unbounded buffer. The frontend falls back to polling.
                        self._count("dropped")
                sub.loop.call_soon_threadsafe(_deliver)
            except Exception as exc:  # dead socket, loop shutting down, ...
                # Never swallow this: a silently dropped exception here is how the
                # whole realtime channel dies without a single log line.
                self._count("dropped")
                log.warning("ws_publish_failed", extra={
                    "uid": uid, "step": event.get("step"), "err": repr(exc)})
        if subs:
            log.debug("ws_broadcast", extra={"uid": uid, "step": event.get("step"),
                                             "subs": len(subs)})


_ws_hub = _WSHub()

# Expose broadcast hook for pipeline (called after add_job_event)
# Coarse progress percentage per step: the client drives its progress bar from
# the live channel, so it must not need an HTTP round-trip to move the bar.
# "orphan" is terminal for this worker (the results exist, the job record moved on):
# a percentage below the last work step walks the client's bar backwards.
_STEP_PCT = {"queued": 5, "requeued": 5, "start": 12, "asr": 40, "align": 45,
             "diarize": 50, "translate": 65, "polish": 70, "words": 72, "tts": 85,
             "done": 100, "failed": 100, "canceled": 100, "orphan": 100}


def _ws_notify(uid: str, jid: str, step: str, message: str, data: dict | None = None) -> None:
    frame = {"job_id": jid, "step": step, "message": message,
             "pct": _STEP_PCT.get(step, 30)}
    # Carried only when there is something to carry: `message` is the log line an
    # English-speaking developer reads, `data` is what the client can localise, and
    # a live progress line should not have to wait for the job to end to get it.
    if data:
        frame["data"] = data
    _ws_hub.broadcast(uid, frame)

# monkey-patch pipeline to notify WS hub on events
_original_add_event = db.add_job_event
def _patched_add_event(jid, step, message, data=None):
    _original_add_event(jid, step, message, data)
    # lookup user_id (lightweight) and notify
    try:
        job = db.get_job(jid)
    except Exception as exc:
        log.warning("ws_event_lookup_failed", extra={"jid": jid, "err": repr(exc)})
        job = None
    if not job:
        return
    # Side effects first, then publish: a client that reacts to this event
    # by fetching /api/notifications must not outrun the notification row. Each
    # side effect gets its own guard — a broken webhook must not cancel the
    # notification, and neither may cost the user their live progress feed.
    if step == "done" or step == "failed":
        kind = NotifKind.JOB_DONE if step == "done" else NotifKind.JOB_FAILED
        prose = "Job completed" if step == "done" else "Job failed"
        try:
            notify(job["user_id"], kind, prose, message)
        except Exception as exc:
            log.warning("event_notify_failed", extra={"jid": jid, "err": repr(exc)})
        try:
            _fire_webhooks(job, "job." + step)
        except Exception as exc:
            log.warning("event_webhooks_failed", extra={"jid": jid, "err": repr(exc)})
    try:
        _ws_notify(job["user_id"], jid, step, message, data)
    except Exception as exc:
        log.warning("ws_notify_failed", extra={"jid": jid, "err": repr(exc)})
db.add_job_event = _patched_add_event  # type: ignore[assignment]


def _fire_webhooks(job: dict, event: str) -> None:
    """Dispatch outbound webhooks for a terminal job event."""
    try:
        from .webhooks import fire_async
        hooks = db.get_active_webhooks(job["user_id"], event)
        payload = {"job_id": job["id"], "status": job.get("status", event.split(".")[-1]),
                   "type": job.get("type"), "src": job.get("src"), "tgt": job.get("tgt")}
        for h in hooks:
            fire_async(h["url"], h["secret"], event, payload)
    except Exception as exc:  # webhook failure never blocks the pipeline
        # ...but it must be visible: a Studio customer's webhook silently doing
        # nothing is a support ticket we cannot reproduce without this line.
        log.warning("webhook_fire_failed", extra={
            "jid": job.get("id"), "event": event, "err": repr(exc)})


# --- retention cleanup thread ---
_retention_stop = threading.Event()

def _retention_loop() -> None:
    """Каждые 6 ч удаляем файлы завершённых jobs старше RETENTION_DAYS."""
    interval = 6 * 3600
    while not _retention_stop.wait(timeout=interval):
        days = settings.retention_days
        if days <= 0:
            continue
        try:
            stale_ids = db.get_finished_jobs_older_than(days)
            for jid in stale_ids:
                db.delete_job(jid, unlink_files=True)
            if stale_ids:
                log.info(f"retention: purged {len(stale_ids)} jobs older than {days}d")
        except Exception:
            log.exception("retention: error in cleanup")


_SHUTDOWN_EVENT = threading.Event()

def _sigterm_handler(signum, frame):
    """Chain to the previous handler (uvicorn's) so graceful shutdown still works."""
    _SHUTDOWN_EVENT.set()
    _retention_stop.set()
    # call the previous handler (likely uvicorn's) so it can drain connections
    prev = getattr(_sigterm_handler, '_prev', None)
    if callable(prev):
        prev(signum, frame)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.ensure_dirs()
    _flags_load()  # feature flags
    db.get_conn()  # инициализация схемы + миграции
    # восстановление после рестарта: сиротские queued/running → failed + возврат
    for stale in db.recover_stale_jobs():
        billing.refund_job(stale, reason="refund:restart")
    # retention cleanup thread
    _rt = threading.Thread(target=_retention_loop, daemon=True, name="ovoz-retention")
    _rt.start()
    # SIGTERM graceful shutdown: chain to uvicorn's handler
    try:
        prev = signal.getsignal(signal.SIGTERM)
        _sigterm_handler._prev = prev  # type: ignore[attr-defined]
        signal.signal(signal.SIGTERM, _sigterm_handler)
    except (OSError, ValueError, AttributeError):
        pass  # Windows dev: SIGTERM not always available in main thread
    yield
    _retention_stop.set()
    _POOL.shutdown(wait=True, cancel_futures=False)


app = FastAPI(title="Ovoz AI Studio", version=__version__,
              lifespan=lifespan,
              description="Субтитры, даббинг и перевод uz↔ru↔en для креаторов, SME и мигрантов",
              openapi_tags=[
                  {"name": "auth", "description": "Registration, login, Telegram Mini App auth, logout"},
                  {"name": "jobs", "description": "Create, list, cancel, retry, download subtitle/dubbing jobs"},
                  {"name": "billing", "description": "Plans, payments, webhooks, wallet, ledger"},
                  {"name": "glossary", "description": "User glossary terms for translation"},
                  {"name": "notifications", "description": "In-app notification feed"},
                  {"name": "admin", "description": "Operational status and metrics"},
                  {"name": "service", "description": "Health checks, metrics, WebSocket"},
              ],
              docs_url="/docs", redoc_url="/redoc",
              contact={"name": "Ovoz Team", "url": "https://ovoz.app"},
              license_info={"name": "Proprietary"})


# --- structured JSON error handler ---
@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    """Consistent error envelope: {detail, error_code, req_id}."""
    # map status -> standard error code
    code_map = {
        400: ErrorCode.BAD_REQUEST, 401: ErrorCode.UNAUTHORIZED,
        402: ErrorCode.INSUFFICIENT_CREDITS, 403: ErrorCode.FORBIDDEN,
        404: ErrorCode.NOT_FOUND, 409: ErrorCode.CONFLICT,
        410: ErrorCode.GONE, 413: ErrorCode.PAYLOAD_TOO_LARGE,
        415: ErrorCode.UNSUPPORTED_MEDIA,
        422: ErrorCode.VALIDATION_ERROR, 429: ErrorCode.RATE_LIMITED,
        501: ErrorCode.NOT_IMPLEMENTED, 503: ErrorCode.SERVICE_UNAVAILABLE,
    }
    error_code = code_map.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
    content = {"detail": exc.detail, "error_code": error_code.value}
    if exc.status_code >= 500:
        content["retryable"] = True
    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers=dict(exc.headers or {}),
    )


@app.exception_handler(_FastAPIValError)
async def val_exc_handler(request: Request, exc: _FastAPIValError):
    """Map FastAPI 422 validation errors to our structured envelope."""
    errors = exc.errors()
    detail = errors[0].get("msg", "Validation error") if errors else "Validation error"
    return JSONResponse(
        status_code=422,
        content={"detail": detail, "error_code": ErrorCode.VALIDATION_ERROR.value,
                 "errors": [{"loc": ".".join(str(x) for x in e.get("loc", [])),
                             "msg": e.get("msg", "")} for e in errors[:5]]},
    )


# --- security headers на всех ответах ---
async def _drain_body(request: Request, ceiling: int) -> None:
    """Read and discard a body we have already decided to refuse.

    A 413 written while the client is still sending makes the stack tear the
    socket down, and the customer's SDK gets `connection reset` where this API
    promised a structured error: the status code survives, the `error_code`,
    the human detail and the request id do not. Draining costs only bytes that
    are already on the wire and buffers nothing. Past `ceiling` we stop being
    polite — a client that far outside the contract deserves back-pressure,
    not a JSON body — and a disconnect is never allowed to turn a refusal
    into a 500."""
    received = 0
    try:
        async with asyncio.timeout(_REFUSAL_DRAIN_SECONDS):
            async for chunk in request.stream():
                received += len(chunk)
                if received >= ceiling:
                    break
    except Exception:  # abort or deadline: the 413 is still the answer, and a
        pass           # client that sent its bytes at all has already finished


_CSP = (
    "default-src 'self'; "
    # No 'unsafe-inline' and no 'unsafe-eval': the SPA ships zero inline scripts
    # and zero on* attributes (tests/test_frontend_invariants.py holds that line),
    # so an injected <script> from stored XSS is fetched-or-blocked instead of
    # executing. The Telegram loader lives in static/tg-boot.js for the same reason.
    "script-src 'self' https://telegram.org https://web.telegram.org; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob: data:; "
    "connect-src 'self'; "
    "worker-src 'self'; "
    "frame-ancestors https://web.telegram.org https://*.telegram.org; "
    "base-uri 'self'; form-action 'self'; object-src 'none'; "
    "upgrade-insecure-requests"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    t0 = time.perf_counter()
    req_id = secrets.token_hex(6)
    # ─── rate limit FIRST, and it covers refusals ───────────────────────
    # Everything below can cost the server work: a drain holds a socket for up to
    # three seconds, a 413 arrives after the bytes were counted. When the budget was
    # spent only on admitted requests, a refusal was the free path — an anonymous
    # client could churn the drain loop forever without ever touching a bucket.
    # Bucket by rate-class (matched prefix) so per-id paths share one allowance.
    path = request.url.path
    if (os.environ.get("OVOZ_ENDPOINT_RATE_LIMIT", "1") != "0"
            and path.startswith("/api/") and path not in ("/api/healthz",)):
        authz = (request.headers.get("authorization") or
                 request.headers.get("x-api-key") or "")
        ident = hashlib.sha256(authz.encode()).hexdigest()[:16] if authz else _client_ip(request)
        lim = _endpoint_limit(path)
        retry = _endpoint_throttle_or_none(f"{ident}|{_rate_class(path)}", lim)
        if retry is not None:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded, slow down",
                         "error_code": ErrorCode.RATE_LIMITED.value},
                headers={"Retry-After": str(retry),
                         "X-RateLimit-Limit": str(lim),
                         "X-RateLimit-Remaining": "0"},
            )
        # A listening route holds one of two slots for seconds of pure Python. The
        # handler enforces that for real; this says it before the five megabytes are
        # parsed, so a refused tape costs the caller a request instead of costing us
        # a multipart decode. `_value` is a snapshot: losing the race only means this
        # fast path missed, which the handler's own acquire still catches.
        if (request.method == "POST" and path.rstrip("/") in _LING_AUDIO_PATHS
                and _LING_LISTENERS._value <= 0):
            return JSONResponse(
                status_code=429,
                content={"detail": "language listeners are busy: two tapes at a time",
                         "error_code": ErrorCode.RATE_LIMITED.value},
                headers={"Retry-After": "5"},
            )
    # An upload route with no Content-Length is a hole, not a corner case: Starlette
    # streams multipart *file* parts into a temp file with no per-part cap (only
    # declared lengths are bounded above), so an anonymous caller that omits the
    # header puts its whole body on our disk before any refusal can fire. Every
    # client this product actually uses — the SPA, curl, urllib, the live probes —
    # declares a length, so requiring one closes the only unbounded write path while
    # costing nobody. 411, not 413: the body may be a perfectly good size, we are
    # saying we cannot tell. `/api/jobs/batch` keeps its own handler-side budget.
    if (request.method == "POST" and request.url.path != "/api/jobs/batch"
            and (request.url.path.startswith("/api/jobs")
                 or request.url.path.rstrip("/") in _LING_AUDIO_PATHS)
            and not request.headers.get("content-length")):
        # Drain a bounded prefix before answering, for the reason Round 16 learned
        # the hard way: a refusal written while the client is still sending reaches
        # it as a connection reset, not as an `error_code`. A sender that stops
        # within the budget gets to read this answer; one that keeps flooding past it
        # gets back-pressure instead, which is the whole point of refusing it.
        await _drain_body(request, (LING_ALIGN_MAX_BODY_BYTES
                                    if request.url.path.rstrip("/") in _LING_AUDIO_PATHS
                                    else MAX_BODY_BYTES) + _REFUSAL_DRAIN_SLACK)
        return JSONResponse(
            status_code=411, headers={"Connection": "close"},
            content={"detail": "an upload must declare its Content-Length",
                     "error_code": ErrorCode.LENGTH_REQUIRED.value},
        )
    # Early body-size rejection: BEFORE FastAPI parses/JSON-buffers the body.
    # Skip /api/jobs/batch — its per-file budget is validated in the handler
    # (a legitimate multi-file batch exceeds the single-file MAX_BODY_BYTES).
    if (request.method in ("POST", "PUT")
            and request.url.path.startswith("/api/jobs")
            and request.url.path != "/api/jobs/batch"):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
            await _drain_body(request, MAX_BODY_BYTES + _REFUSAL_DRAIN_SLACK)
            return JSONResponse(
                status_code=413, headers={"Connection": "close"},
                content={"detail": f"Body too large (max {MAX_BODY_BYTES} bytes)",
                         "error_code": ErrorCode.PAYLOAD_TOO_LARGE.value},
            )
    # Public language endpoints: cap the JSON body up-front (they carry no auth,
    # so the small budget must be enforced before Starlette buffers the payload).
    if (request.method in ("POST", "PUT", "PATCH")
            and request.url.path.startswith("/api/v1/ling")):
        # The two language endpoints that carry a recording get their own ceiling
        # instead of the JSON-body budget; every other rule (drain, Connection:
        # close, structured refusal) is identical.
        ceiling = (LING_ALIGN_MAX_BODY_BYTES
                   if request.url.path.rstrip("/") in _LING_AUDIO_PATHS
                   else LING_MAX_BODY_BYTES)
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > ceiling:
            # Connection: close — an early response before the body is consumed
            # must not leave a keep-alive connection half-duplicated (smuggling-
            # shaped desync on the next pipelined request).
            await _drain_body(request, ceiling + _REFUSAL_DRAIN_SLACK)
            return JSONResponse(
                status_code=413, headers={"Connection": "close"},
                content={"detail": f"Body too large (max {ceiling} bytes)",
                         "error_code": ErrorCode.PAYLOAD_TOO_LARGE.value},
            )
    # Gate docs endpoints in production (OVOZ_ENV=production)
    if os.environ.get("OVOZ_ENV") == "production" and request.url.path in ("/docs", "/redoc"):
        return JSONResponse(status_code=404, content={"detail": "Not found",
                                                      "error_code": ErrorCode.NOT_FOUND.value})
    try:
        response: Response = await call_next(request)
    except Exception:
        log.exception("unhandled", extra={"req_id": req_id, "path": request.url.path,
                                             "method": request.method, "ip": _client_ip(request)})
        raise
    dur_ms = round((time.perf_counter() - t0) * 1000, 1)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("Permissions-Policy", "geolocation=(), camera=()")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    response.headers["X-Request-Id"] = req_id
    response.headers["X-Response-Time"] = f"{dur_ms}ms"
    # API responses: no-store
    if request.url.path.startswith("/api/") or request.url.path == "/metrics":
        response.headers["Cache-Control"] = "no-store, private, max-age=0"
    # The document is the build manifest: it carries the ?v= stamps that pin every
    # asset URL of this release. StaticFiles gives it only ETag/Last-Modified, so a
    # browser falls back to *heuristic* freshness — 10% of the file's age, which in
    # a Docker image means 10% of the age of the build. A returning visitor can then
    # run yesterday's app.js beside today's i18n.js for days, which is exactly what
    # live QA caught: a 0.15.2 shell served over a 0.16.0 server. A document that
    # decides which code the visitor runs is never cached heuristically.
    elif request.url.path in ("/", "/index.html") or request.url.path.endswith(".html"):
        response.headers["Cache-Control"] = "no-cache"
    # Static assets: must-revalidate (filename never changes, so no long max-age).
    # By suffix, not by hand-written list: tg-boot.js and sw-boot.js were added and
    # silently fell out of the list, and /manifest.webmanifest was never in it.
    elif (request.url.path.endswith((".js", ".css"))
          or request.url.path == "/manifest.webmanifest"
          or request.url.path.startswith("/demo/")):
        response.headers.setdefault("Cache-Control", "public, max-age=0, must-revalidate")
    # Service Worker: never cache (browser handles lifecycle)
    if request.url.path == "/sw.js":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    # one log line per request (skip static noise)
    if request.url.path.startswith("/api/") or request.url.path in ("/healthz", "/metrics"):
        log.info("http", extra={"req_id": req_id, "ip": _client_ip(request),
                                 "path": request.url.path, "method": request.method,
                                 "status": response.status_code, "dur_ms": dur_ms})
    return response


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


# ---------- telegram mini app auth ----------

@app.post("/api/auth/telegram", tags=["auth"])
def telegram_auth(request: Request, init_data: str = Form(...)) -> dict:
    if not telegram.bot_token():
        raise HTTPException(501, "TELEGRAM_BOT_TOKEN is not configured on the server")
    ip = _client_ip(request)
    _throttle(f"tg:{ip}")
    tg_user = telegram.verify_init_data(init_data)
    if not tg_user:
        _fail(f"tg:{ip}")
        raise HTTPException(403, "Invalid initData signature")
    contact = f"tg:{tg_user['tg_id']}"
    user = db.find_user_by_contact(contact)
    is_new = user is None
    if is_new:
        auto_secret = secrets.token_urlsafe(24)
        h, s, a = _hash_secret_pbkdf2(auto_secret)
        user = db.create_user(tg_user["name"], contact,
                              secret_hash=h, secret_salt=s, secret_algo=a)
        if user is None:
            # lost creation race to concurrent login — fetch existing
            user = db.find_user_by_contact(contact)
            if user is None:
                raise HTTPException(500, "Account creation failed")
        else:
            billing.credit(user["id"], settings.signup_bonus_minutes, "signup:bonus:telegram")
    token = db.create_session(user["id"])
    return {"token": token, "user": _public_user(user), "is_new": is_new}


# ---------- auth ----------

def current_user(authorization: str | None = Header(None),
                 x_api_key: str | None = Header(None)) -> dict:
    """Authenticate via Bearer token OR X-API-Key header. API keys are for programmatic access."""
    # Try API key first (for automation/scripting)
    if x_api_key and x_api_key.startswith("ovoz_"):
        user = db.verify_api_key(x_api_key.strip())
        if user:
            return user
        raise HTTPException(401, "Invalid API key")
    # Bearer token (session auth)
    raw = (authorization or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:]
    raw = raw.strip()
    if not raw:
        raise HTTPException(401, "Unauthorized")
    user = db.user_by_token(raw)
    if not user:
        raise HTTPException(401, "Invalid token")
    return user


@app.post("/api/auth/register", tags=["auth"])
def register(request: Request, name: str = Form(...), contact: str = Form(...),
             secret: str = Form(...)) -> dict:
    """Регистрация. Секрет ОБЯЗАТЕЛЕН (min 10 симв.) — иначе anyone-with-contact залогинится."""
    ip = _client_ip(request)
    _throttle(f"reg:{ip}")  # защита от массового создания аккаунтов с одного IP
    contact_n = _normalize_contact(contact)
    if not contact_n or len(contact_n) > 64:
        raise HTTPException(422, "Contact is required and must be ≤64 chars")
    if len(name or "") > 64:
        raise HTTPException(422, "Name too long")
    secret = (secret or "").strip()
    if len(secret) < SECRET_MIN_LEN:
        raise HTTPException(422, f"Secret must be at least {SECRET_MIN_LEN} characters")
    h, s, a = _hash_secret_pbkdf2(secret)
    user = db.create_user((name.strip() or "User"), contact_n,
                          secret_hash=h, secret_salt=s, secret_algo=a)
    if not user:
        # засчитываем неудачную регистрацию — иначе с одного IP можно
        # бесконечно перечислять занятые контакты и фармить signup-бонус
        _fail(f"reg:{ip}")
        db.audit("register.duplicate", actor_ip=ip, target=contact_n)
        raise HTTPException(409, "Contact already registered")
    billing.credit(user["id"], settings.signup_bonus_minutes, "signup:bonus")
    db.audit("register.ok", actor_id=user["id"], actor_ip=ip,
             amount=settings.signup_bonus_minutes)
    return {"token": db.create_session(user["id"]), "user": _public_user(user)}


@app.post("/api/auth/login", tags=["auth"])
def login(request: Request, contact: str = Form(...), secret: str = Form("")) -> dict:
    contact_n = _normalize_contact(contact)
    ip = _client_ip(request)
    key = f"login:{contact_n or 'anon'}|{ip}"
    _throttle(key)
    user = db.find_user_by_contact(contact_n) if contact_n else None
    # одинаковый ответ для «нет контакта» и «неверный секрет» — нет оракула перечисления
    if not user or not user.get("secret_hash"):
        _fail(key)
        db.audit("login.fail", actor_ip=ip, target=contact_n, meta={"reason": "no_user"})
        raise HTTPException(401, "Invalid contact or secret")
    if not _verify_secret(secret, user.get("secret_hash"),
                          user.get("secret_salt"), user.get("secret_algo")):
        _fail(key)
        db.audit("login.fail", actor_id=user["id"], actor_ip=ip,
                 meta={"reason": "bad_secret"})
        raise HTTPException(401, "Invalid contact or secret")
    # прозрачная миграция legacy SHA-256 → PBKDF2
    if not (user.get("secret_algo") or "").startswith("pbkdf2_"):
        h, s, a = _hash_secret_pbkdf2(secret.strip())
        db.set_user_secret(user["id"], h, s, a)
        db.audit("login.migrated", actor_id=user["id"], actor_ip=ip,
                 meta={"from": user.get("secret_algo") or "sha256", "to": a})
    db.audit("login.ok", actor_id=user["id"], actor_ip=ip)
    return {"token": db.create_session(user["id"]), "user": _public_user(user)}


@app.post("/api/auth/logout", tags=["auth"])
def logout(request: Request, authorization: str | None = Header(None)) -> dict:
    raw = (authorization or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:]
    raw = raw.strip()
    if raw:
        db.revoke_session(raw)
        db.audit("logout", actor_ip=_client_ip(request),
                 meta={"token_prefix": raw[:8]})
    return {"ok": True}


# ---------- single-use WebSocket handshake tickets ----------
# The session bearer is a 30-day, full-account credential. Query strings land
# verbatim in the uvicorn/proxy access log, so it must never travel there: the
# client trades it for a 15-second, one-time ticket over an Authorization header.

WS_TICKET_TTL = 15.0
_ws_tickets: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
_ws_tickets_lock = threading.Lock()
WS_TICKETS_MAX = 4096


def _ws_tickets_prune() -> None:
    now = time.monotonic()
    for k in [k for k, (_, exp) in _ws_tickets.items() if exp <= now]:
        _ws_tickets.pop(k, None)


@app.post("/api/auth/ws-ticket", tags=["auth"])
def ws_ticket(user: dict = Depends(current_user)) -> dict:
    with _ws_tickets_lock:
        _ws_tickets_prune()
        while len(_ws_tickets) >= WS_TICKETS_MAX:  # bound memory under load
            _ws_tickets.popitem(last=False)
        ticket = secrets.token_urlsafe(24)
        _ws_tickets[ticket] = (user["id"], time.monotonic() + WS_TICKET_TTL)
    return {"ticket": ticket, "expires_in": int(WS_TICKET_TTL)}


def _consume_ws_ticket(ticket: str) -> "str | None":
    """Pop the ticket (single use) and return its user id, if still valid."""
    if not ticket:
        return None
    with _ws_tickets_lock:
        entry = _ws_tickets.pop(ticket, None)
        if entry is None:
            return None
        uid, exp = entry
        return uid if exp > time.monotonic() else None


def _public_user(u: dict) -> dict:
    return {"id": u["id"], "name": u["name"], "contact": u["contact"], "plan": u["plan"]}


# ---------- wallet / plans ----------

@app.get("/api/me", tags=["billing"])
def me(user: dict = Depends(current_user)) -> dict:
    plan = user.get("plan", "free")
    daily_cap = DAILY_QUOTA_MINUTES.get(plan, 30)
    spent_today = db.minutes_spent_today(user["id"])
    return {
        "user": _public_user(user),
        "balance_minutes": billing.balance(user["id"]),
        "quota": {"daily_cap": daily_cap, "spent_today": round(spent_today, 1),
                  "remaining": round(max(0, daily_cap - spent_today), 1)},
    }


@app.get("/api/plans", tags=["billing"])
def plans() -> dict:
    return {"plans": billing.PLANS}


@app.post("/api/payments/webhook/{provider}", tags=["billing"])
def payment_webhook(provider: str, external_id: str = Form(...), user_id: str = Form(...),
                    amount_minor: int = Form(...), currency: str = Form("UZS"),
                    minutes: float = Form(0.0),
                    signature: str = Form("")) -> dict:
    """Webhook ПС. При настроенном OVOZ_WEBHOOK_SECRET подпись обязательна:
    sig = HMAC-SHA256(secret, "provider|external_id|user_id|amount_minor|currency").
    Начисление возможно ТОЛЬКО если amount совпал с тарифом (иначе 422).
    Провайдер 'manual' отключён — для демо есть /api/dev/demo-credit."""
    if provider not in {"payme", "click", "telegram_stars"}:
        raise HTTPException(400, "Unknown provider")
    secret = webhook_secret()
    if not secret:
        raise HTTPException(501, "Payment webhooks disabled: set OVOZ_WEBHOOK_SECRET")
    canon = f"{provider}|{external_id}|{user_id}|{amount_minor}|{currency}"
    want = hmac.new(secret.encode(), canon.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want.encode(), signature.encode()):
        raise HTTPException(403, "Bad signature")
    if amount_minor <= 0:
        raise HTTPException(422, "Invalid payment amount")
    if minutes <= 0 or minutes > billing.MAX_WEBHOOK_MINUTES:
        raise HTTPException(422, "Implausible minutes; must match a plan")
    if not db.get_user(user_id):
        raise HTTPException(404, "No such user")
    result = billing.apply_payment_webhook(provider, external_id, user_id,
                                            amount_minor, currency, minutes)
    if result.get("status") == "error" and result.get("error") == "unmatched_amount":
        # сумма не из прайса: не начисляем ничего, ручной разбор
        db.audit("payment.unmatched", actor_id=user_id, target=external_id,
                 amount=amount_minor, meta={"currency": currency, "provider": provider})
        raise HTTPException(422, "Amount does not match any plan; contact support")
    db.audit("payment.received", actor_id=user_id, target=external_id,
             amount=result["payment"]["minutes"],
             meta={"status": result["status"], "amount_minor": amount_minor,
                   "currency": currency, "provider": provider})
    return result


@app.post("/api/dev/demo-credit")
def demo_credit(request: Request, user: dict = Depends(current_user),
                minutes: float = Form(60.0), admin_key: str = Form("")) -> dict:
    """Демо-начисление (для витрины/тренеров). Требует OVOZ_ADMIN_SECRET + OVOZ_ALLOW_DEMO_CREDIT=1."""
    # в проде закрыто по умолчанию
    if os.environ.get("OVOZ_ALLOW_DEMO_CREDIT", "0") != "1":
        raise HTTPException(404, "Not found")
    key = admin_secret()
    klabel = f"demo:{user['id']}|{_client_ip(request)}"
    _throttle(klabel)
    if not key or not hmac.compare_digest(key.encode(), admin_key.strip().encode()):
        _fail(klabel)
        raise HTTPException(403, "Admin key required")
    minutes = min(max(1.0, minutes), billing.MAX_WEBHOOK_MINUTES)
    billing.credit(user["id"], minutes, "demo:credit")
    db.audit("demo.credit", actor_id=user["id"], actor_ip=_client_ip(request),
             amount=minutes)
    return {"balance_minutes": billing.balance(user["id"])}


# ---------- jobs ----------

@app.post("/api/jobs", tags=["jobs"])
def create_job(request: Request, file: UploadFile = File(...), jtype: str = Form(...),
               src: str = Form("uz"), tgt: str = Form("ru"),
               diarize: str = Form(""), polish: str = Form(""),
               align: str = Form(""), words: str = Form(""),
               user: dict = Depends(current_user)) -> JSONResponse:
    # 1. дешёвые валидации до чтения тела
    if jtype not in JOB_TYPES:
        raise HTTPException(400, f"type must be one of {sorted(JOB_TYPES)}")
    if src not in LANGS or tgt not in LANGS or src == tgt:
        raise HTTPException(400, "src/tgt must be distinct codes from uz/ru/en")
    # 2. Content-Length pre-check — до полного приёма 500MB тела
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
        raise HTTPException(413, f"Body too large (max {MAX_BODY_BYTES} bytes)")
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    job = _submit_job(user, data, file.filename or "", jtype, src, tgt,
                      diarize=_truthy(diarize), polish=_truthy(polish),
                      align=_truthy(align), words=_truthy(words))
    return JSONResponse({"job": _public_job(db.get_job(job["id"]))}, status_code=201)


def _truthy(value) -> bool:
    """Form-флаги приходят строками: '1'/'true'/'on' — включено, всё остальное нет."""
    return str(value or "").strip().lower() in {"1", "true", "on", "yes"}


def _submit_job(user: dict, data: bytes, filename: str, jtype: str,
                src: str, tgt: str, diarize: bool = False,
                polish: bool = False, align: bool = False,
                words: bool = False) -> dict:
    """Validate bytes, store, estimate, charge and dispatch one job.
    Shared by single-upload and batch-upload endpoints. Raises HTTPException
    on any validation/budget failure. Returns the created job dict."""
    if jtype not in JOB_TYPES:
        raise HTTPException(400, f"type must be one of {sorted(JOB_TYPES)}")
    if src not in LANGS or tgt not in LANGS or src == tgt:
        raise HTTPException(400, "src/tgt must be distinct codes from uz/ru/en")
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXT:
        raise HTTPException(400, f"Unsupported file type: {ext}")
    if db.count_active_jobs(user["id"]) >= MAX_ACTIVE_JOBS_PER_USER:
        raise HTTPException(429, f"Too many active jobs (max {MAX_ACTIVE_JOBS_PER_USER})",
                            headers={"Retry-After": "60",
                                     "X-RateLimit-Limit": str(MAX_ACTIVE_JOBS_PER_USER),
                                     "X-RateLimit-Remaining": "0"})
    plan = user.get("plan", "free")
    daily_cap = DAILY_QUOTA_MINUTES.get(plan, 30)
    spent = db.minutes_spent_today(user["id"])
    if spent >= daily_cap:
        raise HTTPException(429, f"Daily quota reached ({daily_cap} min). Try tomorrow or upgrade.",
                            headers={"Retry-After": "3600",
                                     "X-RateLimit-Limit": str(daily_cap),
                                     "X-RateLimit-Remaining": "0"})
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    upload_key = db.new_id()
    store_path = settings.uploads_dir / f"{upload_key}{ext}"
    try:
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"File too large (max {MAX_UPLOAD_BYTES} bytes)")
        if ext in ALLOWED_EXT_AUDIO and not _validate_magic(data, ext):
            raise HTTPException(400, "File content does not match its extension")
        store_path.write_bytes(data)
    except HTTPException:
        store_path.unlink(missing_ok=True)
        raise
    try:
        minutes = max(0.1, round(_estimate_minutes(store_path, ext), 2))
        if minutes * 60 > MAX_JOB_TIMELINE_SEC:
            raise HTTPException(413, f"Media longer than {MAX_JOB_TIMELINE_SEC // 60} min is not supported")
        job = db.create_job(user["id"], jtype, src, tgt, minutes, str(store_path),
                            meta={"providers": {
                                "asr": settings.asr_provider,
                                "translate": settings.translate_provider,
                                "tts": settings.tts_provider,
                            }, **({"diarize": True} if diarize else {}),
                                **({"polish": True} if polish else {}),
                                **({"align": True} if align else {}),
                                **({"words": True} if words else {})})
    except HTTPException:
        store_path.unlink(missing_ok=True)
        raise
    except Exception:
        # DB/disk failure must not leave an orphan upload behind
        store_path.unlink(missing_ok=True)
        raise
    jid = job["id"]
    if not billing.charge_for_job(job):
        db.delete_job(jid, unlink_files=True)
        raise HTTPException(402, f"Insufficient credits: need {minutes} min")
    remaining = billing.balance(user["id"])
    maybe_notify_balance_low(user["id"], remaining)
    db.add_job_event(jid, "queued", f"minutes={minutes}")
    if settings.sync_pipeline:
        pipeline.run_job(jid)
    else:
        _dispatch_job(jid)
    return job


_BATCH_MAX_FILES = 10


@app.post("/api/jobs/batch", tags=["jobs"])
def create_jobs_batch(
    request: Request,
    files: list[UploadFile] = File(...),
    jtype: str = Form(...),
    src: str = Form("uz"),
    tgt: str = Form("ru"),
    diarize: str = Form(""),
    polish: str = Form(""),
    align: str = Form(""),
    words: str = Form(""),
    user: dict = Depends(current_user),
) -> JSONResponse:
    """Upload several files as independent jobs in one request.
    Per-file results: successes get a job, failures carry an error message.
    Whole-batch is rejected only for global validation (bad type/langs/too many files)."""
    if jtype not in JOB_TYPES:
        raise HTTPException(400, f"type must be one of {sorted(JOB_TYPES)}")
    if src not in LANGS or tgt not in LANGS or src == tgt:
        raise HTTPException(400, "src/tgt must be distinct codes from uz/ru/en")
    if not files:
        raise HTTPException(422, "At least one file is required")
    if len(files) > _BATCH_MAX_FILES:
        raise HTTPException(422, f"Max {_BATCH_MAX_FILES} files per batch")
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES * len(files):
        raise HTTPException(413, "Batch body too large")
    results: list[dict] = []
    created = 0
    for f in files:
        try:
            data = f.file.read(MAX_UPLOAD_BYTES + 1)
            job = _submit_job(user, data, f.filename or "", jtype, src, tgt,
                              diarize=_truthy(diarize), polish=_truthy(polish),
                              align=_truthy(align), words=_truthy(words))
            results.append({"filename": f.filename, "ok": True,
                            "job": _public_job(db.get_job(job["id"]))})
            created += 1
        except HTTPException as e:
            results.append({"filename": f.filename, "ok": False,
                            "error": e.detail, "status": e.status_code})
        except Exception as e:  # noqa: BLE001 — never abort the whole batch
            results.append({"filename": f.filename, "ok": False,
                            "error": str(e), "status": 500})
    status = 201 if created == len(files) else (207 if created else 422)
    return JSONResponse({"created": created, "total": len(files), "results": results},
                        status_code=status)


def _estimate_minutes(path: Path, ext: str) -> float:
    if ext in ALLOWED_EXT_TEXT:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 1.0
        if ext == ".srt":
            from .ling import srt as srt_mod
            cues = srt_mod.parse_srt(text)
            secs = srt_mod.duration(cues) if cues else 0
            return max(0.1, secs / 60)
        words = len(text.split())
        return max(0.1, words / 150)
    return _probe_duration(path)


def _probe_duration(path: Path) -> float:
    """Реальная длительность аудио/видео через ffprobe (есть в системе)."""
    try:
        proc = subprocess.run(
            [settings.ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        return max(0.1, round(float(proc.stdout.strip()), 2) / 60)
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return 1.0  # ffprobe недоступен — консервативная оценка в 1 минуту


@app.get("/api/jobs", tags=["jobs"])
def jobs(
    user: dict = Depends(current_user),
    cursor: Optional[str] = Query(None, description="Opaque cursor from previous page"),
    limit: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None, description="Filter by status: queued|running|done|failed|canceled"),
    jtype: Optional[str] = Query(None, description="Filter by type: subtitles|dubbing|translate_doc"),
) -> dict:
    # Validate filter params
    if status and status not in ("queued", "running", "done", "failed", "canceled"):
        raise HTTPException(422, "Invalid status filter")
    if jtype and jtype not in JOB_TYPES:
        raise HTTPException(422, f"Invalid type filter; must be one of {sorted(JOB_TYPES)}")
    job_list = db.list_jobs_cursor(user["id"], cursor=cursor, limit=limit,
                                    status=status, jtype=jtype)
    # batch optimization: 1 artifact query + 1 event query вместо 6*N + N
    ids = [j["id"] for j in job_list]
    art_map = db.batch_artifacts(ids)
    active_ids = [j["id"] for j in job_list if j["status"] in ("queued", "running")]
    evt_map = db.batch_latest_events(active_ids)
    out = []
    for j in job_list:
        arts = {k: f"/api/jobs/{j['id']}/download/{k}" for k in art_map.get(j["id"], {})}
        progress = evt_map.get(j["id"]) if j["status"] in ("queued", "running") else None
        out.append({k: j[k] for k in ("id", "type", "src", "tgt", "status", "minutes", "error", "created_at")} |
                   {"artifacts": arts, "progress": progress, "options": _job_options(j),
                    "engines": _job_engines(j)})
    # next_cursor: if we got exactly `limit`, there might be more
    next_cursor = None
    if len(job_list) == limit and job_list:
        import base64
        last = job_list[-1]
        raw = f"{last['created_at']}|{last['id']}"
        next_cursor = base64.urlsafe_b64encode(raw.encode()).decode()
    return {"jobs": out, "next_cursor": next_cursor}


@app.get("/api/jobs/{jid}", tags=["jobs"])
def job_detail(jid: str, user: dict = Depends(current_user)) -> dict:
    job = db.get_job(jid)
    if not job or job["user_id"] != user["id"]:
        raise HTTPException(404, "Job not found")
    return {"job": _public_job(job), "timeline": db.job_timeline(jid)}


@app.post("/api/jobs/{jid}/cancel", tags=["jobs"])
def cancel_job(jid: str, user: dict = Depends(current_user)) -> dict:
    job = db.get_job(jid)
    if not job or job["user_id"] != user["id"]:
        raise HTTPException(404, "Job not found")
    if not db.cancel_job(jid):  # CAS: выигрываем гонку у воркера или проигрываем
        raise HTTPException(409, "Job already left the queue")
    db.add_job_event(jid, "canceled", "user canceled")
    billing.refund_job(job, reason="refund:canceled")
    return {"job": _public_job(db.get_job(jid))}


@app.post("/api/jobs/{jid}/retry", tags=["jobs"])
def retry_job(jid: str, user: dict = Depends(current_user)) -> dict:
    job = db.get_job(jid)
    if not job or job["user_id"] != user["id"]:
        raise HTTPException(404, "Job not found")
    if job["status"] not in {"failed", "canceled"}:
        raise HTTPException(409, "Retry is only available for failed/canceled jobs")
    if not Path(job["source_path"]).exists():
        raise HTTPException(410, "Source file is gone; upload again")
    if db.count_active_jobs(user["id"]) >= MAX_ACTIVE_JOBS_PER_USER:
        raise HTTPException(429, f"Too many active jobs (max {MAX_ACTIVE_JOBS_PER_USER})",
                            headers={"Retry-After": "60"})
    if not db.claim_for_retry(jid):  # сначала атомарно захватываем статус…
        raise HTTPException(409, "Retry already in progress")
    if not billing.debit(user["id"], job["minutes"], f"job:{jid}", ref=jid):
        db.update_job(jid, status=job["status"])  # откатили статус, деньги не взялись
        raise HTTPException(402, "Insufficient credits for retry")
    db.update_job(jid, error=None)
    db.add_job_event(jid, "requeued", "user retry")
    if settings.sync_pipeline:
        pipeline.run_job(jid)
    else:
        _dispatch_job(jid)
    return {"job": _public_job(db.get_job(jid))}


# Kinds a job may expose, in the order the UI lists them. One list because the
# private job view and the public share view must offer exactly the same files:
# a kind added to one and not the other either hides an artifact or leaks it.
_JOB_ARTIFACT_KINDS = ("transcript", "srt", "srt_bilingual", "ass", "dubbing",
                       "document", "diarization", "layout", "align", "words",
                       "ass_karaoke")

# Which paid engine options a job asked for. Exactly these four booleans and not
# the whole meta: the client has to know whether to offer "what the engine did" at
# all, and a card that asks a question it cannot answer is worse than no card. Hand
# on meta_json as well and provider configuration walks back out to the browser.
_JOB_OPTIONS = ("diarize", "polish", "align", "words")


def _job_options(job: dict) -> dict:
    meta = job.get("meta") or {}
    return {k: bool(meta.get(k)) for k in _JOB_OPTIONS}


def _job_engines(job: dict) -> dict:
    """What produced the text in this job, as the pipeline recorded it.

    A job can complete successfully on a demo transcript when the deployment asked
    for real ASR and the binary was not there. The `[демо]` prefix inside a subtitle
    is not a disclosure the customer can see from the list, so the fact rides with
    the job and the card renders it."""
    meta = job.get("meta") or {}
    return {"asr": meta.get("asr_mode") or "unknown",
            "asr_demo": bool(meta.get("asr_demo")),
            "asr_reason": meta.get("asr_reason") or ""}


def _public_job(job: dict) -> dict:
    arts = {}
    for kind in _JOB_ARTIFACT_KINDS:
        p = db.get_artifact(job["id"], kind)
        if p:
            arts[kind] = f"/api/jobs/{job['id']}/download/{kind}"
    # progress: latest event step/message for running/queued jobs
    progress = None
    if job["status"] in ("queued", "running"):
        timeline = db.job_timeline(job["id"])
        if timeline:
            last = timeline[-1]
            progress = {"step": last["step"], "message": last["message"],
                        "data": last.get("data") or {}}
    return {k: job[k] for k in ("id", "type", "src", "tgt", "status", "minutes", "error", "created_at")} | {
        "artifacts": arts, "progress": progress, "options": _job_options(job),
        "engines": _job_engines(job),
    }


@app.get("/api/jobs/{jid}/download/{kind}", tags=["jobs"])
def download(jid: str, kind: str, user: dict = Depends(current_user)):
    job = db.get_job(jid)
    if not job or job["user_id"] != user["id"]:
        raise HTTPException(404, "Job not found")
    path = db.get_artifact(jid, kind)
    if not path or not Path(path).exists():
        raise HTTPException(404, "Artifact not ready")
    media = "audio/wav" if kind == "dubbing" else "text/plain; charset=utf-8"
    return FileResponse(path, media_type=media, filename=Path(path).name)


# ---------- предпросмотр: медиа job'а и собранные карточки ----------
# Студия умела отдавать файлы; посмотреть результат можно было только скачав их и
# подобрав себе видеозапись вручную («p-vfile»). Просмотр, который требует от
# клиента заново найти свой файл, — это работа, которую продукт и обязан снять.
_MEDIA_EXT = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
              ".ogg": "audio/ogg", ".mp4": "video/mp4", ".mov": "video/quicktime"}
_AUDIO_EXT = {".wav", ".mp3", ".m4a", ".ogg"}
# Предпросмотр — экран, а не передача архива: потолка на число карточек и на число
# слов хватает на весь законченный job (сегментов в подряде и так не больше 1500),
# но не дают одному ответу разрастись до мегабайт JSON'а.
_CAPTION_MAX_CUES = 1600
_CAPTION_MAX_WORDS = 20_000


def _num(x, default: float = 0.0) -> float:
    """Конечное число или ничего: в этот JSON браузер кладёт тайминги подсветки."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _media_of(job: dict) -> dict | None:
    """Медиа записи клиента: тип и размер, без пути на диске.

    Путь не уходит наружу никогда (тот же закон, что у диагностики провайдеров);
    MIME берётся из расширения сохранённого файла, которое выбрал сервер по
    белосписку, а не из имени, присланного клиентом.
    """
    try:
        src = Path(job["source_path"])
        if not src.exists():
            return None
        ext, size = src.suffix.lower(), src.stat().st_size
    except OSError:
        return None
    mime = _MEDIA_EXT.get(ext)
    if not mime or size <= 0:
        return None
    return {"kind": "audio" if ext in _AUDIO_EXT else "video", "mime": mime,
            "bytes": size}


_CUE_TAG_RE = re.compile(r"^\[S(\d+)\]\s*")


def _cue_tag(text: str) -> tuple[int | None, str]:
    """Разделить метку диктора и то, что произносится: «[S1] Salom» → (1, «Salom»).

    Метка — часть строки субтитра (её видно в файле и на burn-in), но в предпросмотре
    для неё есть отдельное место: чип голоса. Пока метка остаётся в тексте, счётчик
    слов считает скобки, а подсветка горит на служебном слове — то есть предпросмотр
    показывает то, чего на ленте нет.
    """
    m = _CUE_TAG_RE.match(text or "")
    if not m:
        return None, text or ""
    try:
        speaker = int(m.group(1))
    except ValueError:
        return None, text or ""
    return speaker, text[m.end():]


def _caption_of(job: dict) -> dict:
    """Карточки, слова и голоса одной задачей, из тех же артефактов, что скачивают.

    Клиент умел читать SRT и раскрашивать слова только по пяти запросам и своему
    парсеру таймкодов; здесь сборка идёт сервером, потому что тайминги слов —
    результат движка, и пересобирать их в браузере значило бы иметь две
    интерпретации одного файла.
    """
    jid = job["id"]
    cues: list[dict] = []
    path = db.get_artifact(jid, "srt")
    truncated = False
    total = 0
    if path and Path(path).exists():
        try:
            parsed = srt_mod.parse_srt(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            parsed = []
        total = len(parsed)
        if total > _CAPTION_MAX_CUES:
            parsed, truncated = parsed[:_CAPTION_MAX_CUES], True
        rows = []
        for c in parsed:
            speaker, text = _cue_tag(c.text or "")
            rows.append({"start": round(_num(c.start), 3),
                         "end": round(_num(c.end), 3),
                         "text": text[:600], "words": [], "speaker": speaker})
        cues = rows
    words = 0
    wp = db.get_artifact(jid, "words")
    if cues and wp and Path(wp).exists():
        try:
            report = json.loads(Path(wp).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {}
        by_index = {c.get("i"): c for c in (report.get("cues") or [])
                    if isinstance(c, dict)}
        for i, cue in enumerate(cues, start=1):
            if words >= _CAPTION_MAX_WORDS:
                truncated = True
                break
            item = by_index.get(i) or {}
            taken = []
            for w in (item.get("words") or []):
                if words + len(taken) >= _CAPTION_MAX_WORDS:
                    truncated = True
                    break
                if isinstance(w, dict) and w.get("w"):
                    taken.append({"w": str(w["w"])[:120],
                                  "s": round(_num(w.get("s")), 3),
                                  "e": round(_num(w.get("e")), 3)})
            cue["words"] = taken
            words += len(taken)
    dp = db.get_artifact(jid, "diarization")
    if cues and dp and Path(dp).exists():
        try:
            dia = json.loads(Path(dp).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            dia = {}
        # Голос привязывается по накрытию временем, а не по номеру карточки:
        # диаризация считает реплики, вёрстка перекладывает карточки между ними,
        # и любой кэш индексов после этого был бы догадкой.
        lines = [l for l in (dia.get("lines") or []) if isinstance(l, dict)]
        for cue in cues:
            if cue["speaker"] is not None:
                continue            # метка в строке — первичнее накрытия временем
            best, score = None, 0.0
            for line in lines:
                ov = (min(_num(line.get("end")), cue["end"])
                      - max(_num(line.get("start")), cue["start"]))
                if ov > score:
                    best, score = line, ov
            if best is not None and score > 0:
                speaker = best.get("speaker")
                cue["speaker"] = speaker if isinstance(speaker, int) else None
    meta = job.get("meta") or {}
    return {"cues": cues, "media": _media_of(job), "count": len(cues),
            "total": max(total, len(cues)),
            "words": words, "karaoke": words > 0, "speakers": any(
                c["speaker"] is not None for c in cues),
            "demo": bool(meta.get("asr_demo")), "truncated": truncated}


@app.get("/api/jobs/{jid}/caption", tags=["jobs"],
         summary="Cues, word timings and speakers for the in-app preview")
def job_caption(jid: str, user: dict = Depends(current_user)) -> dict:
    job = db.get_job(jid)
    if not job or job["user_id"] != user["id"]:
        raise HTTPException(404, "Job not found")
    return _caption_of(job)


@app.get("/api/jobs/{jid}/media", tags=["jobs"],
         summary="The uploaded recording, for the owner's preview only")
def job_media(jid: str, user: dict = Depends(current_user)):
    """Исходник job'а — только владельцу и только в студии.

    Share-ссылка отдаёт результаты работы, а не чужую запись: исходное видео
    человека, которое он загрузил, — не часть продукта, которую он разрешил
    показывать всем, у кого есть ссылка.
    """
    job = db.get_job(jid)
    if not job or job["user_id"] != user["id"]:
        raise HTTPException(404, "Job not found")
    info = _media_of(job)
    if not info:
        # Файл мог быть стёрт retention-очисткой: это 410, а не 500 и не путь к
        # домашней папке сервера.
        raise HTTPException(410, "Source media is gone, or was never playable "
                                "audio/video")
    return FileResponse(job["source_path"], media_type=info["mime"])


# ---------- job sharing (public expiring links) ----------

@app.post("/api/jobs/{jid}/share", tags=["jobs"])
def create_job_share(jid: str, user: dict = Depends(current_user),
                     ttl_hours: int = Form(72),
                     max_downloads: int = Form(0)) -> dict:
    """Generate a time-limited public share link for job artifacts."""
    job = db.get_job(jid)
    if not job or job["user_id"] != user["id"]:
        raise HTTPException(404, "Job not found")
    if job["status"] != "done":
        raise HTTPException(409, "Only completed jobs can be shared")
    ttl_hours = max(1, min(ttl_hours, 720))  # 1h to 30d
    sid = db.create_share(jid, user["id"], ttl_hours=ttl_hours, max_downloads=max_downloads)
    return {"share_id": sid, "share_url": f"/s/{sid}", "expires_hours": ttl_hours}


@app.get("/s/{sid}", tags=["jobs"])
def share_page(sid: str) -> JSONResponse:
    """Public (no auth) share page: lists artifacts for anonymous download."""
    share = db.get_share(sid)
    if not share:
        raise HTTPException(404, "Share not found")
    # check expiry
    if share["expires_at"] < db.now_iso():
        raise HTTPException(410, "Share link expired")
    # check download limit
    if share["max_downloads"] > 0 and share["download_count"] >= share["max_downloads"]:
        raise HTTPException(410, "Download limit reached")
    arts = {}
    for kind in _JOB_ARTIFACT_KINDS:
        p = db.get_artifact(share["job_id"], kind)
        if p:
            arts[kind] = f"/s/{sid}/dl/{kind}"
    return JSONResponse({
        "job_type": share["job_type"], "artifacts": arts,
        "expires_at": share["expires_at"],
    })


@app.get("/s/{sid}/dl/{kind}", tags=["jobs"])
def share_download(sid: str, kind: str):
    """Public artifact download via share link (no auth)."""
    share = db.get_share(sid)
    if not share:
        raise HTTPException(404, "Share not found")
    if share["expires_at"] < db.now_iso():
        raise HTTPException(410, "Share link expired")
    if share["max_downloads"] > 0 and share["download_count"] >= share["max_downloads"]:
        raise HTTPException(410, "Download limit reached")
    path = db.get_artifact(share["job_id"], kind)
    if not path or not Path(path).exists():
        raise HTTPException(404, "Artifact not found")
    db.increment_share_download(sid)
    media = "audio/wav" if kind == "dubbing" else "text/plain; charset=utf-8"
    return FileResponse(path, media_type=media, filename=Path(path).name)


# ---------- glossary ----------

GLOSSARY_MAX_TERMS = 500
GLOSSARY_MAX_TERM_LEN = 128


@app.post("/api/glossary", tags=["glossary"])
def add_terms(rows: list[dict], user: dict = Depends(current_user)) -> dict:
    if len(rows) > 200:
        raise HTTPException(422, "Too many terms in one call (max 200)")
    cleaned = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        s = str(r.get("src") or "").strip()[:GLOSSARY_MAX_TERM_LEN]
        t = str(r.get("tgt") or "").strip()[:GLOSSARY_MAX_TERM_LEN]
        if s and t:
            cleaned.append((s, t))
    saved = [db.upsert_glossary_term(user["id"], s, t) for s, t in cleaned]
    return {"saved": len(saved)}


@app.get("/api/glossary", tags=["glossary"])
def get_terms(user: dict = Depends(current_user)) -> dict:
    return {"terms": db.glossary_for(user["id"])}


@app.delete("/api/glossary", tags=["glossary"])
def delete_term(payload: dict, user: dict = Depends(current_user)) -> dict:
    src = (payload.get("src") or "").strip()[:GLOSSARY_MAX_TERM_LEN]
    if not src:
        raise HTTPException(422, "src is required")
    return {"deleted": db.delete_glossary_term(user["id"], src)}


# ---------- account ----------

@app.get("/api/account/export", tags=["auth"])
def export_data(user: dict = Depends(current_user)) -> JSONResponse:
    """GDPR Art. 20: Data portability — download all user data as JSON."""
    uid = user["id"]
    full = db.get_user_full(uid) or {}
    jobs = db.list_jobs(uid, limit=9999)
    glossary = db.glossary_for(uid)
    payments = db.list_payments(uid)
    notifications = db.list_notifications(uid, limit=9999)
    shares = db.list_shares(uid)
    export = {
        "exported_at": db.now_iso(),
        "format_version": 1,
        "user": {
            "name": full.get("name"),
            "contact": full.get("contact"),
            "plan": full.get("plan"),
            "created_at": full.get("created_at"),
        },
        "balance_minutes": billing.balance(uid),
        "jobs": [{"id": j["id"], "type": j["type"], "src": j["src"], "tgt": j["tgt"],
                  "status": j["status"], "minutes": j["minutes"], "created_at": j.get("created_at")}
                 for j in jobs],
        "glossary": [{"src": g["src_term"], "tgt": g["tgt_term"]} for g in glossary],
        "payments": [{"id": p["id"], "amount_minor": p["amount_minor"], "currency": p["currency"],
                      "minutes": p["minutes"], "status": p["status"], "created_at": p.get("created_at")}
                     for p in payments],
        "notifications": [{"kind": n["kind"], "title": n["title"], "body": n.get("body"),
                           "created_at": n.get("created_at")} for n in notifications],
        "shares": [{"id": s["id"], "job_id": s["job_id"], "expires_at": s["expires_at"],
                    "download_count": s["download_count"]} for s in shares],
    }
    db.audit("account.exported", actor_id=uid)
    return JSONResponse(
        content=export,
        headers={"Content-Disposition": 'attachment; filename="ovoz-data-export.json"'},
    )


@app.delete("/api/account", tags=["auth"])
def delete_account(request: Request, user: dict = Depends(current_user)) -> dict:
    """GDPR Art. 17: полное удаление аккаунта, файлов, сессий, глоссария, истории."""
    uid = user["id"]
    db.revoke_all_sessions(uid)
    for j in db.list_jobs(uid, limit=9999):
        db.delete_job(j["id"], unlink_files=True)
    db.delete_all_glossary(uid)
    db.audit("account.deleted", actor_id=uid, actor_ip=_client_ip(request))
    db.delete_user(uid)
    return {"ok": True}


@app.patch("/api/account/name", tags=["auth"])
def update_name(payload: dict, user: dict = Depends(current_user)) -> dict:
    """Change display name."""
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 64:
        raise HTTPException(422, "Name must be 1–64 characters")
    db.update_user_name(user["id"], name)
    return {"ok": True, "name": name}


@app.post("/api/account/change-secret", tags=["auth"])
def change_secret(request: Request, user: dict = Depends(current_user),
                  old_secret: str = Form(...), new_secret: str = Form(...)) -> dict:
    """Change password with current-password verification."""
    if len(new_secret.strip()) < SECRET_MIN_LEN:
        raise HTTPException(422, f"New secret must be at least {SECRET_MIN_LEN} characters")
    _throttle(f"chsec:{user['id']}")
    # verify old secret
    full = db.get_user_full(user["id"])
    if not full or not _verify_secret(old_secret, full.get("secret_hash"),
                                       full.get("secret_salt"), full.get("secret_algo")):
        _fail(f"chsec:{user['id']}")
        raise HTTPException(403, "Current secret is incorrect")
    h, s, a = _hash_secret_pbkdf2(new_secret.strip())
    db.set_user_secret(user["id"], h, s, a)
    # Revoke all sessions for security (including current)
    db.revoke_all_sessions(user["id"])
    db.audit("account.secret.changed", actor_id=user["id"], actor_ip=_client_ip(request))
    # Issue new session token
    new_token = db.create_session(user["id"])
    return {"ok": True, "token": new_token}


# ---------- API keys (programmatic auth for Pro/Studio) ----------

@app.get("/api/account/onboarding", tags=["auth"])
def get_onboarding(user: dict = Depends(current_user)) -> dict:
    """Whether the first-run tour has been completed."""
    seen = db.get_onboarding_state(user["id"])
    return {"seen_at": seen, "needs_tour": seen is None}


@app.post("/api/account/onboarding", tags=["auth"])
def complete_onboarding(user: dict = Depends(current_user)) -> dict:
    """Mark the onboarding tour as seen (idempotent)."""
    db.mark_onboarding_seen(user["id"])
    return {"ok": True, "seen_at": db.get_onboarding_state(user["id"])}


@app.get("/api/account/usage", tags=["auth"])
def account_usage(
    user: dict = Depends(current_user),
    days: int = Query(30, ge=1, le=365),
) -> dict:
    """Per-user usage analytics: daily minutes, jobs by type, spend, streak."""
    summary = db.usage_summary(user["id"], days=days)
    summary["balance_minutes"] = billing.balance(user["id"])
    summary["plan"] = user.get("plan", "free")
    return summary


@app.post("/api/account/keys", tags=["auth"])
def create_key(user: dict = Depends(current_user),
               label: str = Form("")) -> dict:
    """Generate a new API key. Shown only once — store securely."""
    plan = user.get("plan", "free")
    if plan == "free":
        raise HTTPException(403, "API keys require Pro or Studio plan")
    existing = db.list_api_keys(user["id"])
    active = [k for k in existing if not k.get("revoked_at")]
    if len(active) >= 5:
        raise HTTPException(422, "Maximum 5 active API keys")
    raw, record = db.create_api_key(user["id"], label[:64])
    return {"key": raw, "record": record}


@app.get("/api/account/keys", tags=["auth"])
def list_keys(user: dict = Depends(current_user)) -> dict:
    """List API keys (without full values)."""
    keys = db.list_api_keys(user["id"])
    return {"keys": keys}


@app.delete("/api/account/keys/{kid}", tags=["auth"])
def delete_key(kid: str, user: dict = Depends(current_user)) -> dict:
    """Revoke an API key."""
    if not db.revoke_api_key(user["id"], kid):
        raise HTTPException(404, "Key not found or already revoked")
    return {"ok": True}


# ---------- Outbound webhooks (Studio plan) ----------

@app.post("/api/webhooks", tags=["billing"])
def create_webhook_endpoint(user: dict = Depends(current_user),
                           url: str = Form(...), events: str = Form("job.done,job.failed")) -> dict:
    """Register an outbound webhook URL. Studio plan required."""
    if user.get("plan", "free") != "studio":
        raise HTTPException(403, "Webhooks require Studio plan")
    if not url.startswith("https://"):
        raise HTTPException(422, "Webhook URL must be HTTPS")
    existing = db.list_webhooks(user["id"])
    if len(existing) >= 5:
        raise HTTPException(422, "Max 5 webhooks per user")
    import secrets as _sec
    wh_secret = _sec.token_hex(32)
    record = db.create_webhook(user["id"], url[:512], wh_secret, events[:128])
    record["secret"] = wh_secret  # shown once
    return record


@app.get("/api/webhooks", tags=["billing"])
def list_webhooks_endpoint(user: dict = Depends(current_user)) -> dict:
    """List registered webhooks."""
    return {"webhooks": db.list_webhooks(user["id"])}


@app.delete("/api/webhooks/{wid}", tags=["billing"])
def delete_webhook_endpoint(wid: str, user: dict = Depends(current_user)) -> dict:
    """Remove a webhook."""
    if not db.delete_webhook(user["id"], wid):
        raise HTTPException(404, "Webhook not found")
    return {"ok": True}


# ---------- notifications ----------

@app.get("/api/notifications", tags=["notifications"])
def notifications(
    user: dict = Depends(current_user),
    unread: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    items = db.list_notifications(user["id"], unread_only=unread, limit=limit)
    unread_count = db.count_unread_notifications(user["id"])
    return {"notifications": items, "unread_count": unread_count}


@app.post("/api/notifications/{nid}/read", tags=["notifications"])
def mark_notif_read(nid: int, user: dict = Depends(current_user)) -> dict:
    ok = db.mark_notification_read(nid, user["id"])
    if not ok:
        raise HTTPException(404, "Notification not found")
    return {"ok": True}


@app.post("/api/notifications/read-all", tags=["notifications"])
def mark_all_notif_read(user: dict = Depends(current_user)) -> dict:
    """Bulk: mark all unread notifications as read."""
    count = db.mark_all_notifications_read(user["id"])
    return {"ok": True, "marked": count}


# ---------- ledger / billing history ----------

@app.get("/api/ledger", tags=["billing"])
def ledger(
    user: dict = Depends(current_user),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    entries = db.ledger_history(user["id"], limit=limit)
    balance = billing.balance(user["id"])
    return {"entries": entries, "balance_minutes": balance}


# ---------- admin / operational ----------

def _require_admin(request: Request) -> None:
    """Gate admin endpoints by OVOZ_ADMIN_SECRET in Authorization header."""
    key = admin_secret()
    if not key:
        raise HTTPException(501, "Admin panel disabled: set OVOZ_ADMIN_SECRET")
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        auth = auth[7:]
    if not hmac.compare_digest(key.encode(), auth.strip().encode()):
        raise HTTPException(403, "Admin key required")


@app.get("/api/admin/status", tags=["admin"])
def admin_status(request: Request) -> dict:
    """Operational dashboard: total users, active jobs, revenue, analytics."""
    _require_admin(request)
    conn = db.get_conn()
    from datetime import date
    today = date.today().isoformat()
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    new_today = conn.execute(
        "SELECT COUNT(*) FROM users WHERE created_at >= ?", (today,)).fetchone()[0]
    active_jobs = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
    total_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    completed_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='done'").fetchone()[0]
    failed_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='failed'").fetchone()[0]
    jobs_today = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE created_at >= ?", (today,)).fetchone()[0]
    # jobs by type
    by_type = {}
    for row in conn.execute("SELECT type, COUNT(*) c FROM jobs GROUP BY type").fetchall():
        by_type[row[0]] = row[1]
    total_revenue_minor = conn.execute(
        "SELECT COALESCE(SUM(amount_minor),0) FROM payments WHERE status='paid'").fetchone()[0]
    revenue_today = conn.execute(
        "SELECT COALESCE(SUM(amount_minor),0) FROM payments WHERE status='paid' AND created_at >= ?",
        (today,)).fetchone()[0]
    total_minutes_credited = conn.execute(
        "SELECT COALESCE(SUM(delta),0) FROM ledger WHERE delta > 0").fetchone()[0]
    total_minutes_spent = conn.execute(
        "SELECT COALESCE(SUM(ABS(delta)),0) FROM ledger WHERE delta < 0").fetchone()[0]
    sessions_active = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    import platform
    import sys
    return {
        "users": {"total": total_users, "new_today": new_today},
        "jobs": {"total": total_jobs, "active": active_jobs, "today": jobs_today,
                 "completed": completed_jobs, "failed": failed_jobs, "by_type": by_type},
        "revenue": {"total_minor_units": total_revenue_minor, "today_minor_units": revenue_today,
                    "minutes_credited": round(total_minutes_credited, 1),
                    "minutes_spent": round(total_minutes_spent, 1)},
        "sessions_active": sessions_active,
        "api_keys_active": conn.execute(
            "SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NULL").fetchone()[0],
        "system": {"version": __version__, "python": platform.python_version(),
                    "platform": sys.platform, "workers": GLOBAL_WORKER_SLOTS,
                    "schema_version": db._SCHEMA_VERSION},
        # The operator half of the self-check — binary names, model paths, key
        # presence — lives here and nowhere public.
        "providers": _provider_status(with_operator=True),
    }


@app.get("/api/admin/flags", tags=["admin"])
def admin_flags(request: Request) -> dict:
    """List all feature flags."""
    _require_admin(request)
    return {"flags": _flags_all()}


@app.patch("/api/admin/flags/{flag_name}", tags=["admin"])
def admin_update_flag(request: Request, flag_name: str, payload: dict) -> dict:
    """Update a feature flag (enabled, pct, min_plan)."""
    _require_admin(request)
    allowed_keys = {"enabled", "pct", "min_plan"}
    updates = {k: v for k, v in payload.items() if k in allowed_keys}
    if not updates:
        raise HTTPException(422, "Provide at least one of: enabled, pct, min_plan")
    _flag_set(flag_name, **updates)
    return {"ok": True, "flag": flag_name, "updated": updates}


# ---------- service ----------

@app.get("/api/v1/info", tags=["service"])
def api_info() -> dict:
    """Public API version info (no auth needed). Useful for client SDKs."""
    return {
        "name": "Ovoz AI Studio",
        "version": __version__,
        "api_version": "v1",
        "docs": "/docs",
        "websocket": "/ws/jobs",
        "features": ["subtitles", "dubbing", "transcribe", "document_translate",
                     "glossary", "notifications", "pagination", "realtime_ws",
                     "offline_pwa", "job_sharing", "telegram_theme", "api_keys",
                     "webhooks", "feature_flags", "gdpr_export", "onboarding",
                     "usage_analytics", "batch_upload", "endpoint_rate_limit",
                     "uzbek_language_engine", "uzbek_2026_alphabet_converter",
                     "speaker_diarization", "subtitle_layout_engine",
                     "silence_gap_alignment", "word_level_timing",
                     "subtitle_cut_law"],
        "plans": list(billing.PLANS.keys()),
        "schema_version": db._SCHEMA_VERSION,
        # Claimed against provable, per stage, with the reason. No key material is
        # here: `key_present`, binary names, model paths. An operator asking "is my
        # dubbing actually being voiced, or is it a stub tone?" gets an answer from
        # the running process instead of from the env file it hopes is applied.
        "providers": _provider_status(),
    }


# The self-check reads live configuration, and a stage that cannot answer is still
# worth reporting: letting it raise would take /api/v1/info — the endpoint an SDK
# and the studio's own health banner depend on — down over a status line.
#
# Two shapes, because the two readers are not the same. Anonymous gets mode, a
# stable code and a reason that names variables but never values: no binary paths,
# no model paths, no exception classes. An operator behind the admin key gets the
# `operator` half too, because that is exactly the information a deployment needs
# to fix itself and exactly the map a stranger should not be handed.
def _provider_status(with_operator: bool = False) -> dict:
    out = {}
    for name, mod in (("asr", p_asr), ("translate", p_tr), ("tts", p_tts)):
        try:
            st = mod.status()
        except Exception as exc:  # noqa: BLE001
            log.warning("provider_self_check_failed",
                        extra={"stage": name, "err": f"{type(exc).__name__}: {exc}"})
            st = {"provider": "unknown", "mode": "unknown", "code": "self_check_failed",
                  "reason": "the provider self-check could not run", "operator": {}}
        public = {k: v for k, v in st.items() if k != "operator"}
        if with_operator:
            public["operator"] = st.get("operator", {})
        out[name] = public
    return out


# ---------- linguistics (public, rate-limited — the moat showcase) ----------

_LING_MAX_TEXT = 5000            # anonymous demo: no need for novel-sized input
LING_MAX_BODY_BYTES = 130_000    # enforced pre-parse in the middleware
# The listening routes are the only language calls that carry a recording: a
# 30-second 16-bit mono WAV is ~0.5 MB, so the JSON-body budget is simply the wrong
# unit here. What the cap really buys is *tape*: at the rate both engines ask for
# (8 kHz mono 16-bit = 16 000 byte/s) five megabytes is ~312 s. It stays below the
# engine's own 900-second window because this route is anonymous — a stranger should
# not be able to make us buffer a quarter of a minute of upload per request.
LING_ALIGN_MAX_BODY_BYTES = 6_000_000
_LING_ALIGN_MAX_AUDIO_BYTES = 5_000_000   # the file alone, inside the multipart envelope
_LING_TAPE_BYTE_RATE = 16_000             # 8 kHz mono s16le: bytes per second of tape
# Every language route whose envelope holds a recording. Listed in one place on
# purpose: a new listening endpoint that is not added here gets the 130 KB JSON
# ceiling and refuses every honest upload with 413 — loud, not silent.
_LING_AUDIO_PATHS = ("/api/v1/ling/align", "/api/v1/ling/words")
# How many tapes the anonymous listeners decode at once. Both engines are pure
# Python, so a request that is *allowed* is also a request that *runs*: without a
# bound, a client that spaces its 12 permitted requests a second apart can occupy
# more threads than the box has cores. Two is enough for a demo-grade deployment,
# and the answer when the third arrives is an honest 429 with Retry-After rather
# than a queue that looks like a hang.
_LING_LISTENERS = threading.BoundedSemaphore(2)
# The text engines are cheaper than a tape and still not free: 400 cues of hostile
# timing is seconds of pure Python, and every one of these routes is `async def`, so
# an unbounded number of *permitted* requests is an unbounded number of event-loop
# hijacks. Round 23's review found exactly this: the audio class got a threadpool and
# slots, `diarize`/`layout`/`nafis` did not, so the cheaper class was the more
# dangerous one.
_LING_TEXT = threading.BoundedSemaphore(3)


async def _ling_run(sem: threading.BoundedSemaphore, what: str, fn, *args, **kw):
    """One slot, off the event loop, released whatever happens.

    Refusing with 429 + Retry-After beats queueing: a queued client sees latency it
    cannot explain, and the threads it occupies are threads no other request can
    use."""
    if not sem.acquire(blocking=False):
        raise HTTPException(429, f"{what} is busy: try again in a moment",
                            headers={"Retry-After": "5"})
    try:
        return await run_in_threadpool(fn, *args, **kw)
    finally:
        sem.release()


def _ling_listener() -> None:
    """Take one of the two listening slots or refuse now.

    Non-blocking on purpose: waiting for a slot inside the request would hold an
    upload thread and a client both, and the client would learn nothing about why
    it is slow."""
    if not _LING_LISTENERS.acquire(blocking=False):
        raise HTTPException(429, "language listeners are busy: two tapes at a time",
                            headers={"Retry-After": "5"})


def _ling_guard() -> None:
    """Ops kill-switch: admin disables the public engine via the feature flag.
    Fails OPEN — a missing flag (older installs predate it) keeps the moat on;
    only an explicit enabled:false turns it off."""
    flag = _flags_all().get("uzbek_language_engine")
    if isinstance(flag, dict) and not flag.get("enabled", True):
        raise HTTPException(404, "Language engine disabled")


def _ling_refuse_audio(data: bytes) -> None:
    """A 413 that answers in the caller's unit. Bytes are what the server protects,
    seconds are what the client thought it was sending, and the engine's own window
    is what they would hit next — one sentence with all three, so a rejected upload
    knows whether to trim, to resample, or to book a paid job instead."""
    if len(data) <= _LING_ALIGN_MAX_AUDIO_BYTES:
        return
    tape_sec = _LING_ALIGN_MAX_AUDIO_BYTES // _LING_TAPE_BYTE_RATE
    raise HTTPException(413, f"audio too large ({len(data)} bytes, max "
                             f"{_LING_ALIGN_MAX_AUDIO_BYTES}: about {tape_sec}s of "
                             f"8 kHz mono 16-bit WAV; longer tape belongs in a job)")


def _ling_text(payload: dict) -> str:
    raw = payload.get("text")
    if not isinstance(raw, str):
        raise HTTPException(422, "text must be a string")
    text = raw
    if not text.strip():
        raise HTTPException(422, "text is required")
    if len(text) > _LING_MAX_TEXT:
        raise HTTPException(413, f"text too long (max {_LING_MAX_TEXT} chars)")
    return text


def _ling_terms(payload: dict) -> dict | None:
    raw = payload.get("terms")
    if not isinstance(raw, dict) or not raw:
        return None
    if len(raw) > 100:
        raise HTTPException(422, "too many terms (max 100)")
    out = {}
    for k, v in raw.items():
        # Normalise apostrophe variants in keys: transliteration output always
        # uses ASCII ', so an official-spelling key (oʻ/gʻ, U+02BB) must still
        # match — otherwise such terms are silently dead.
        ks = normalize_uzbek(str(k).strip())[:120]
        vs = str(v).strip()[:120]
        if ks and vs:
            out[ks] = vs
    return out or None


async def _ling_body(request: Request) -> dict:
    """Read the JSON body with a hard byte cap enforced at the stream level,
    so chunked (Content-Length-less) bodies can't exhaust memory either.

    Once the cap is crossed the loop keeps *reading* without keeping: a 413
    thrown while the client is still writing gets the socket torn down, and the
    caller's SDK sees a reset instead of the structured error this API promises.
    Memory stays bounded because nothing past the cap is buffered, and the drain
    stops at the same slack ceiling the middleware uses."""
    total = 0
    chunks: list[bytes] = []
    over = False
    t_over = 0.0
    try:
        async for part in request.stream():
            total += len(part)
            if total > LING_MAX_BODY_BYTES:
                if not over:
                    over = True
                    t_over = time.monotonic()
                elif time.monotonic() - t_over > _REFUSAL_DRAIN_SECONDS:
                    break                # dribbling past the cap: refuse faster than answer
                if total > LING_MAX_BODY_BYTES + _REFUSAL_DRAIN_SLACK:
                    break
                continue
            chunks.append(part)
    except HTTPException:
        raise
    except Exception:
        if not over:                        # an abort during a refusal is still a refusal
            raise HTTPException(400, "unreadable body")
    if over:
        raise HTTPException(413, "Body too large")
    raw = b"".join(chunks)
    if len(raw) > LING_MAX_BODY_BYTES:
        raise HTTPException(413, "Body too large")
    try:
        data = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(422, "invalid JSON")
    if not isinstance(data, dict):
        raise HTTPException(422, "body must be a JSON object")
    return data


@app.post("/api/v1/ling/analyze", tags=["linguistics"])
async def ling_analyze(request: Request) -> dict:
    """Full Uzbek linguistic read: script, confidence, both transliterations,
    official-apostrophe form, metrics, code-switch signal, glossary hits.
    Public and side-effect free."""
    _ling_guard()
    payload = await _ling_body(request)
    return ling.analyze(_ling_text(payload), _ling_terms(payload))


@app.post("/api/v1/ling/transliterate", tags=["linguistics"])
async def ling_transliterate(request: Request) -> dict:
    """Convert Uzbek text between the three co-existing script states:
    Cyrillic, legacy Latin and 2026-reform Latin (Ö/Ğ/Ş/Ç).
    Body: {text, to?: 'latin'|'cyrillic'|'new_latin', official?: bool, terms?: {src:tgt}}.
    Aliases ('lat', 'cyr', 'new') are accepted; idempotent — text already in the
    target script is only normalized."""
    _ling_guard()
    payload = await _ling_body(request)
    # absent/null → default; present but unparseable ("", "klingon", 5) → 422,
    # so a typo in a client never silently returns the wrong script
    raw_to = payload.get("to")
    to = _ling_target("latin" if raw_to is None else raw_to)
    if to is None:
        raise HTTPException(422, "to must be 'latin', 'cyrillic' or 'new_latin'")
    official = bool(payload.get("official"))
    return ling.convert(_ling_text(payload), to=to, official=official,
                        terms=_ling_terms(payload))


@app.get("/api/v1/ling/detect", tags=["linguistics"], openapi_extra={
    "parameters": [{
        "name": "text", "in": "query", "required": True,
        "schema": {"type": "string", "maxLength": _LING_MAX_TEXT},
    }]})
async def ling_detect(request: Request) -> dict:
    """Detect the dominant script of Uzbek text with confidence."""
    _ling_guard()  # first: with the kill-switch off the whole family is 404,
                   # never a 422 that would leak validation order
    payload = dict(request.query_params)
    return ling.detect(_ling_text(payload))


_LING_MAX_LINES = 400          # an anonymous demo never needs a 400-cue interview
_LING_MAX_CUE_CHARS = 300      # one subtitle cue, not a chapter
_LING_MAX_TIME = 86400.0       # 24 h: nothing an anonymous demo posts runs longer


def _ling_cue_text(raw: str, where: str, strict: bool) -> str:
    """The cue caps have always trimmed long text rather than refusing it. For the
    layout engine that is not neutral: its headline claim is word-for-word
    preservation, so a silently shortened cue would turn `words_preserved: true`
    into a lie about text the customer never lost. An endpoint that promises
    preservation asks to be refused instead of trimmed."""
    if len(raw) > _LING_MAX_CUE_CHARS:
        if strict:
            raise HTTPException(
                422, f"{where} is longer than {_LING_MAX_CUE_CHARS} chars — "
                     f"this endpoint never cuts your text")
        return raw[:_LING_MAX_CUE_CHARS]
    return raw


def _ling_cues(payload: dict, strict_text: bool = False) -> list:
    """Timed cues for the diarizer, from either shape a client can honestly send:
    a {lines:[{start,end,text}]} array or a raw SRT document. This endpoint is
    public, so cue count and per-cue text are bounded before the engine runs —
    the body cap alone would still allow 130KB of one-word cues."""
    doc = payload.get("srt")
    if isinstance(doc, str) and doc.strip():
        if len(doc) > _LING_MAX_TEXT:
            raise HTTPException(413, f"srt too long (max {_LING_MAX_TEXT} chars)")
        cues = srt_mod.parse_srt(doc)
        # Today the 5000-char document cap above already bounds the cue count at
        # ~180; the check stays because it is the invariant the engine is sized for,
        # not a favour to the current text limit.
        if len(cues) > _LING_MAX_LINES:
            raise HTTPException(422, f"too many cues (max {_LING_MAX_LINES})")
        # The same impossible-timing rule as the `lines` shape, on both ends: a cue
        # that runs for 99 hours hands the engine a huge negative gap against the
        # next line, which reads as an overlap and mints a speaker. The chained
        # comparison also drops NaN/Infinity, which a hand-written JSON body can
        # otherwise smuggle straight into the aggregate (and into the JSON encoder).
        for c in cues:
            if not (-0.001 <= c.start <= _LING_MAX_TIME and
                    c.start - 0.001 <= c.end <= _LING_MAX_TIME):
                raise HTTPException(422, f"cue {c.index} has impossible timings")
        return [srt_mod.Cue(c.index, c.start, c.end,
                            _ling_cue_text(c.text, f"cue {c.index}", strict_text))
                for c in cues]
    raw = payload.get("lines")
    if not isinstance(raw, list):
        raise HTTPException(422, "body needs 'lines' (array) or 'srt' (string)")
    if len(raw) > _LING_MAX_LINES:
        raise HTTPException(422, f"too many lines (max {_LING_MAX_LINES})")
    cues = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise HTTPException(422, f"lines[{i}] must be an object")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(422, f"lines[{i}].text is required")
        try:
            start = float(item.get("start", 0) or 0)
            end = float(item.get("end", start) or start)
        except (TypeError, ValueError):
            raise HTTPException(422, f"lines[{i}] start/end must be numbers")
        # impossible timings are an input bug, not a decision for the engine:
        # a negative or 10^9 gap would otherwise fake a 'long pause' boundary, and
        # NaN/Infinity survive a plain `end < start` test while breaking the reply
        if not (-0.001 <= start <= _LING_MAX_TIME and
                start - 0.001 <= end <= _LING_MAX_TIME):
            raise HTTPException(422, f"lines[{i}] has impossible timings")
        cues.append(srt_mod.Cue(i + 1, round(start, 3), round(end, 3),
                                _ling_cue_text(text, f"lines[{i}].text",
                                               strict_text)))
    return cues


@app.post("/api/v1/ling/diarize", tags=["linguistics"])
async def ling_diarize(request: Request) -> dict:
    """Ovoz Turn — speaker attribution on timed cues. Public, text-only, no model
    and no audio: structure, Uzbek morphology (both scripts) and dialogue rules
    decide who holds the floor. Deterministic and explained: every line carries
    the cues that produced its speaker. Body: {lines:[{start,end,text}]} or
    {srt:"..."}, optional max_speakers."""
    _ling_guard()
    payload = await _ling_body(request)
    cues = _ling_cues(payload)
    if not cues:
        raise HTTPException(422, "no cues to attribute")
    limit = payload.get("max_speakers", diar_mod.MAX_SPEAKERS_DEFAULT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
        raise HTTPException(422, "max_speakers must be an integer from 1 to 8")
    return await _ling_run(_LING_TEXT, "the attribution engine",
                           diar_mod.diarize, cues, max_speakers=limit)


@app.post("/api/v1/ling/layout", tags=["linguistics"])
async def ling_layout(request: Request) -> dict:
    """Ovoz Qator — subtitle layout and readability repair. Public, text-only:
    the broadcast reading-rate law, the 42-character measure, dwell and clearance
    are measured on every card, and the same cards are rebuilt to pass. Optional
    `speakers:[int]` carries an Ovoz Turn attribution so a card never mixes two
    voices. Body: {lines:[{start,end,text}]} or {srt:"..."}."""
    _ling_guard()
    payload = await _ling_body(request)
    # strict_text: this engine sells "not one word added, dropped or reordered",
    # so it would rather refuse a too-long cue than report words_preserved=true on
    # text it never actually saw.
    cues = _ling_cues(payload, strict_text=True)
    if not cues:
        raise HTTPException(422, "no cues to lay out")
    speakers = payload.get("speakers")
    if speakers is not None:
        # A speaker list shorter than the cues would silently mis-attribute the
        # tail, so it is either full length or nothing at all.
        if not isinstance(speakers, list) or len(speakers) != len(cues):
            raise HTTPException(422, "speakers must be one integer per cue")
        for s in speakers:
            if isinstance(s, bool) or not isinstance(s, int) or not 0 <= s <= 8:
                raise HTTPException(422, "speakers must be integers from 0 to 8")
    return await _ling_run(_LING_TEXT, "the layout engine",
                           lay_mod.polish, cues, speakers=speakers)


# An AlignError code is a fact about the input, so it maps to exactly one status:
# a container we will not decode is a media-type refusal, a cue/tuning problem is
# the caller's own request to fix. Every code the engine can raise is listed, and
# an unlisted one answers 500 rather than silently guessing — the engine refusing
# is normal, the engine inventing a code we never mapped is a bug worth surfacing.
_ALIGN_REFUSAL_STATUS = {
    "not_wav": 415, "compressed": 415, "bad_depth": 415, "bad_channels": 415,
    "bad_header": 415, "invalid_audio": 415,
    "bad_input": 422, "empty": 422, "bad_rate": 422, "too_short": 422,
    "no_cues": 422, "too_many_cues": 422, "bad_shift": 422,
    "too_long": 413,
    # word-timer codes: WordError subclasses AlignError, so one table serves both
    # listeners. A silent tape is not a bad request format — it is a request the
    # engine cannot answer honestly, which is exactly what 422 says to the client.
    "too_many_words": 422, "silent_tape": 422,
}


def _align_cues(srt: str, lines: str) -> list:
    """Cues for the aligner, from either honest shape — but read as form fields, not
    as a JSON body, because this request carries a recording in the same envelope.
    strict_text: the report claims not one word changed, so a cue long enough to be
    truncated must be refused rather than quietly trimmed into a false claim."""
    doc = (srt or "").strip()
    raw = (lines or "").strip()
    if bool(doc) == bool(raw):
        raise HTTPException(422, "send either 'srt' or 'lines', exactly one")
    if doc:
        if len(doc) > _LING_MAX_TEXT:
            raise HTTPException(413, f"srt too long (max {_LING_MAX_TEXT} chars)")
        payload = {"srt": doc}
    else:
        if len(raw) > LING_MAX_BODY_BYTES:
            raise HTTPException(413, "lines too large")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(422, "lines must be JSON: [{start,end,text}]")
        payload = {"lines": parsed} if isinstance(parsed, list) else parsed
    if not isinstance(payload, dict):
        raise HTTPException(422, "lines must be an array of cue objects")
    return _ling_cues(payload, strict_text=True)


@app.post("/api/v1/ling/align", tags=["linguistics"])
async def ling_align(
    request: Request,
    audio: UploadFile = File(...),
    srt: str = Form(""),
    lines: str = Form(""),
    max_shift: str = Form(""),
) -> dict:
    """Ovoz Jimlik — subtitle boundaries moved into the pauses the speaker actually
    took. The only language endpoint that listens: it reads the tape's own energy
    envelope, finds the silences, and snaps each cut to the nearest one. Text is
    never edited, only timed, so the report carries `words_preserved` and
    `never_worse` as checkable facts instead of promises. Multipart body:
    `audio` (uncompressed 8/16-bit WAV), one of `srt` / `lines`, optional
    `max_shift` in seconds."""
    _ling_guard()
    # cues first and cheaply: an out-of-contract subtitle set should be refused
    # before we buffer a five-megabyte upload to discover the same thing
    cues = _align_cues(srt, lines)
    if not cues:
        raise HTTPException(422, "no cues to align")
    data = await audio.read(_LING_ALIGN_MAX_AUDIO_BYTES + 1)
    _ling_refuse_audio(data)
    shift = align_mod.MAX_SHIFT_DEFAULT
    if (max_shift or "").strip():
        try:
            shift = float(max_shift)
        except ValueError:
            raise HTTPException(422, "max_shift must be a number of seconds")
    _ling_listener()
    try:
        # run_in_threadpool, не просто вызов: движки — чистый Python на несколько
        # секунд CPU, а маршрут `async`. Без пула они считаются в самом event
        # loop — то есть один анонимный запрос на пятиминутную ленту останавливает
        # /healthz, WebSocket-хаб и весь API, пока читает кадр за кадром.
        pcm, rate = await run_in_threadpool(align_mod.pcm_from_wav, data)
        return await run_in_threadpool(align_mod.align, pcm, rate, cues,
                                       max_shift=shift)
    except align_mod.AlignError as exc:
        status = _ALIGN_REFUSAL_STATUS.get(exc.code, 500)
        if status == 500:
            raise HTTPException(500, f"align engine reported {exc.code}")
        raise HTTPException(status, str(exc))
    finally:
        _LING_LISTENERS.release()


# The word timer speaks the same transport as the aligner: same envelope, same
# ceilings, same refusal table. Only the answer shape differs — one entry per word
# instead of one per boundary — so the two listeners can never disagree about what
# a request costs or how a bad one is refused.
@app.post("/api/v1/ling/words", tags=["linguistics"])
async def ling_words(
    request: Request,
    audio: UploadFile = File(...),
    srt: str = Form(""),
    lines: str = Form(""),
    fmt: str = Form(""),
) -> dict:
    """Ovoz So'z — word-level timing inside the cues. The second endpoint that
    listens: it takes the cue boundaries the aligner already placed and splits the
    air between them, weighting each word by the syllables it has to speak and
    preferring a real dip in the tape over a geometric guess. Every boundary says
    how it was found (`valley` / `quiet` / `speech`), so the client can show a
    highlight and the engineer can tell measured from interpolated. Multipart body:
    `audio` (uncompressed 8/16-bit WAV), one of `srt` / `lines`, optional
    `fmt=ass|vtt` to append a player-ready export next to the report."""
    _ling_guard()
    cues = _align_cues(srt, lines)
    if not cues:
        raise HTTPException(422, "no cues to time")
    want = (fmt or "").strip().lower()
    if want not in ("", "ass", "vtt"):
        raise HTTPException(422, "fmt must be 'ass' or 'vtt'")
    data = await audio.read(_LING_ALIGN_MAX_AUDIO_BYTES + 1)
    _ling_refuse_audio(data)
    _ling_listener()
    try:
        # См. `ling_align`: та же работа обязана идти вне event loop.
        pcm, rate = await run_in_threadpool(align_mod.pcm_from_wav, data)
        report = await run_in_threadpool(word_mod.words, pcm, rate, cues)
    except align_mod.AlignError as exc:     # WordError is a subclass: one handler
        status = _ALIGN_REFUSAL_STATUS.get(exc.code, 500)
        if status == 500:
            raise HTTPException(500, f"word engine reported {exc.code}")
        raise HTTPException(status, str(exc))
    finally:
        _LING_LISTENERS.release()
    if want == "ass":
        report["ass"] = word_mod.to_ass(report)
    elif want == "vtt":
        report["vtt"] = word_mod.to_vtt(report)
    return report


# The third engine that reads text rather than the tape, and the one that answers a
# question no general-purpose subtitle tool asks: not "does this fit" (Ovoz Qator)
# nor "did the speaker stop here" (Ovoz Jimlik), but **is a cut here legal in
# Uzbek**. Public and deterministic like the rest of the family; it is the audit
# editors ask for before they trust an automatic layout.
@app.post("/api/v1/ling/nafis", tags=["linguistics"])
async def ling_nafis(request: Request) -> dict:
    """Ovoz Nafis — the right to cut an Uzbek sentence. Public, text-only.

    Five left-binding laws (postposition, compound verb, bind-right conjunction,
    fixed junction, numeral from its unit) judge every position in the text and
    every boundary between the delivered cards. The engine reads and never edits:
    `words` come back counted from the caller's own tokens. Body:
    {lines:[{start,end,text}]} or {srt:"..."}, optional {text:"..."} to judge a
    paragraph without inventing timings for it."""
    _ling_guard()
    payload = await _ling_body(request)
    # Two honest shapes. With timings the engine judges the boundaries a client will
    # actually ship; without them it judges a paragraph, which is what an editor
    # asks before any file exists. `text` is only read when no cue shape came, so a
    # client that sends both gets a verdict about the file it really has.
    prose = payload.get("text")
    has_cues = isinstance(payload.get("srt"), str) or isinstance(payload.get("lines"), list)
    if isinstance(prose, str) and prose.strip() and not has_cues:
        if len(prose) > _LING_MAX_TEXT:
            raise HTTPException(413, f"text too long (max {_LING_MAX_TEXT} chars)")
        try:
            report = await _ling_run(_LING_TEXT, "the cut engine",
                                     nafis_mod.analyze, [], source_text=prose)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        report["mode"] = "prose"
        return report
    # strict_text: judging text the caller did not actually send would be a verdict
    # about a file nobody uploaded.
    cues = _ling_cues(payload, strict_text=True)
    if not cues:
        raise HTTPException(422, "no cues to judge")
    try:
        report = await _ling_run(_LING_TEXT, "the cut engine", nafis_mod.analyze, cues)
    except ValueError as exc:          # the engine's own refusal of an empty input
        raise HTTPException(422, str(exc))
    report["mode"] = "cues"
    return report


@app.get("/healthz", tags=["service"])
async def health() -> dict:
    """Deep health: verify DB connection + provider circuits."""
    from .circuit import all_status
    try:
        db.get_conn().execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        db_ok = False
    circuits = all_status()
    any_open = any(c["state"] == "open" for c in circuits)
    status = 200 if db_ok and not any_open else 503
    return JSONResponse(
        {"ok": db_ok and not any_open, "version": __version__, "db": db_ok,
         "circuits": circuits},
        status_code=status,
    )


# ---------- metrics (Prometheus-compatible) ----------

_metrics_start = time.monotonic()

@app.get("/metrics", tags=["service"])
async def metrics() -> Response:
    """Minimal Prometheus text-format metrics for observability."""
    import platform
    uptime = time.monotonic() - _metrics_start
    active = db.get_conn().execute(
        "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')"
    ).fetchone()[0]
    total_jobs = db.get_conn().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    sessions = db.get_conn().execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    lines = [
        "# HELP ovoz_uptime_seconds Process uptime.",
        "# TYPE ovoz_uptime_seconds gauge",
        f"ovoz_uptime_seconds {uptime:.1f}",
        "# HELP ovoz_active_jobs Currently queued/running jobs.",
        "# TYPE ovoz_active_jobs gauge",
        f"ovoz_active_jobs {active}",
        "# HELP ovoz_total_jobs All-time job count.",
        "# TYPE ovoz_total_jobs counter",
        f"ovoz_total_jobs {total_jobs}",
        "# HELP ovoz_active_sessions Active session tokens.",
        "# TYPE ovoz_active_sessions gauge",
        f"ovoz_active_sessions {sessions}",
        "# HELP ovoz_worker_threads ThreadPool size.",
        "# TYPE ovoz_worker_threads gauge",
        f"ovoz_worker_threads {GLOBAL_WORKER_SLOTS}",
        "# HELP ovoz_python_info Build info.",
        "# TYPE ovoz_python_info gauge",
        f'ovoz_python_info{{version="{platform.python_version()}",app="{__version__}"}} 1',
        "# HELP ovoz_ws_events_total Live-channel frames, by outcome.",
        "# TYPE ovoz_ws_events_total counter",
        f'ovoz_ws_events_total{{result="published"}} {_ws_hub.published}',
        f'ovoz_ws_events_total{{result="dropped"}} {_ws_hub.dropped}',
        "# HELP ovoz_ws_sockets Live job-progress sockets currently attached.",
        "# TYPE ovoz_ws_sockets gauge",
        f"ovoz_ws_sockets {sum(_ws_hub.count(u) for u in _ws_hub.uids())}",
    ]
    return Response("\n".join(lines) + "\n", media_type="text/plain; charset=utf-8")


# ---------- WebSocket real-time job progress ----------

WS_MAX_SOCKETS_PER_USER = 4

@app.websocket("/ws/jobs")  # tags not available on websocket
async def ws_jobs(websocket: WebSocket, ticket: str = Query("")):
    """Client trades its bearer for a one-time ticket, then connects here to
    receive live job events. Replaces 2.5s polling with instant push updates.

    Authentication uses the short-lived ticket, never the session token: a
    query string is copied into every access log between the browser and us.
    """
    uid = _consume_ws_ticket(ticket)
    if not uid:
        await websocket.close(code=4001, reason="Unauthorized")
        return
    if _ws_hub.count(uid) >= WS_MAX_SOCKETS_PER_USER:
        # Cheap early gate; the authoritative admission is try_subscribe below.
        await websocket.close(code=4003, reason="Too many connections")
        return
    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    # The pipeline publishes from worker threads: bind this queue to the loop that
    # is actually running this handler, or the events go nowhere.
    sub = _ws_hub.try_subscribe(uid, queue, asyncio.get_running_loop(),
                                WS_MAX_SOCKETS_PER_USER)
    if sub is None:
        await websocket.close(code=4003, reason="Too many connections")
        return
    try:
        # send initial state
        await websocket.send_json({"type": "connected", "uid": uid})
        while True:
            # asyncio.timeout (not wait_for) so a frame that lands exactly on the
            # timeout is never cancelled away together with the getter.
            try:
                async with asyncio.timeout(30.0):
                    event = await queue.get()
            except TimeoutError:
                await websocket.send_json({"type": "ping"})  # keepalive
                continue
            await websocket.send_json({"type": "job_event", **event})
    except WebSocketDisconnect:
        pass  # the client walked away: normal, nothing to report
    except Exception as exc:
        # This consumer is exactly as load-bearing as the publisher. A handler that
        # dies on its first iteration would stream nothing and still look like a
        # healthy connection — which is how the last incident stayed invisible.
        log.warning("ws_socket_failed", extra={"uid": uid, "err": repr(exc)})
    finally:
        _ws_hub.unsubscribe(uid, sub)


# ---------- CORS (for future API consumers) ----------
# Registered last on purpose: Starlette runs middleware inside-out, so the
# rate-limiter, the request-id and the security-header layers must stay outside
# this one. The import itself lives with the other FastAPI imports at the top.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://web.telegram.org", "https://ovoz.app"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-Id", "X-API-Key"],
)


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
