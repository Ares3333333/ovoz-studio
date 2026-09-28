"""Ovoz Studio — слой персистентности (SQLite, ledger-first)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import settings

_LOCAL = threading.local()
_SCHEMA_READY = threading.Event()  # гарантируем DDL только один раз

# ─── Schema version tracking ───
_SCHEMA_VERSION = 7  # increment when adding migrations

_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    contact TEXT NOT NULL UNIQUE,
    plan TEXT NOT NULL DEFAULT 'free',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id),
    delta REAL NOT NULL,
    reason TEXT NOT NULL,
    ref TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    type TEXT NOT NULL,
    src TEXT NOT NULL,
    tgt TEXT NOT NULL,
    status TEXT NOT NULL,
    minutes REAL NOT NULL DEFAULT 0,
    source_path TEXT,
    error TEXT,
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    ts TEXT NOT NULL,
    step TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL,
    path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS glossary (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    src_term TEXT NOT NULL,
    tgt_term TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL UNIQUE,
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    minutes REAL NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id);
CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor_id TEXT,
    actor_ip TEXT,
    action TEXT NOT NULL,
    target TEXT,
    amount REAL,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_id, action);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    read_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, read_at);
CREATE TABLE IF NOT EXISTS shares (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    max_downloads INTEGER DEFAULT 0,
    download_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_shares_job ON shares(job_id);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    label TEXT NOT NULL DEFAULT '',
    key_hash TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_apikeys_user ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_apikeys_hash ON api_keys(key_hash) WHERE revoked_at IS NULL;
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def get_conn() -> sqlite3.Connection:
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        target = getattr(_LOCAL, "db_path", None)
        conn = _connect(target or settings.db_path)
        _LOCAL.conn = conn
        _LOCAL.db_path = target or settings.db_path
    return conn


def bind_db(path: Path) -> None:
    """Переключить текущий поток на другую БД (используется в тестах)."""
    old = getattr(_LOCAL, "conn", None)
    if old is not None:
        old.close()
    # сбрасываем schema-flag при смене БД (тесты биндят tmp_path)
    _SCHEMA_READY.clear()
    _LOCAL.conn = _connect(path)
    _LOCAL.db_path = path


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # DDL + миграции выполняются ровно один раз при первом подключении
    if not _SCHEMA_READY.is_set():
        conn.executescript(SCHEMA)
        conn.executescript(_VERSION_TABLE)
        # versioned migrations
        current = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
        migrations = _get_migrations(current)
        for ver, stmts in migrations:
            for sql in stmts:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass  # column/index already exists
            conn.execute("INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
                         (ver, now_iso()))
        conn.commit()
        _SCHEMA_READY.set()
    return conn


def _get_migrations(current_ver: int) -> list[tuple[int, list[str]]]:
    """Return list of (version, [sql_statements]) for migrations after current_ver."""
    all_migrations: list[tuple[int, list[str]]] = [
        (1, [
            "ALTER TABLE users ADD COLUMN secret_hash TEXT",
            "ALTER TABLE users ADD COLUMN secret_salt TEXT",
            "ALTER TABLE users ADD COLUMN secret_algo TEXT",
            "ALTER TABLE sessions ADD COLUMN expires_at TEXT",
            "ALTER TABLE sessions ADD COLUMN last_seen_at TEXT",
            "CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_user_status ON jobs(user_id, status)",
        ]),
        (2, [
            "CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT DEFAULT '', read_at TEXT, created_at TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, read_at)",
        ]),
        (3, [
            "CREATE TABLE IF NOT EXISTS shares (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, user_id TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, max_downloads INTEGER DEFAULT 0, download_count INTEGER DEFAULT 0)",
            "CREATE INDEX IF NOT EXISTS idx_shares_job ON shares(job_id)",
        ]),
        (4, [
            "CREATE TABLE IF NOT EXISTS api_keys (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', key_hash TEXT NOT NULL, key_prefix TEXT NOT NULL, created_at TEXT NOT NULL, last_used_at TEXT, revoked_at TEXT)",
            "CREATE INDEX IF NOT EXISTS idx_apikeys_user ON api_keys(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_apikeys_hash ON api_keys(key_hash) WHERE revoked_at IS NULL",
        ]),
        (5, [
            "CREATE TABLE IF NOT EXISTS webhooks (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL, events TEXT NOT NULL DEFAULT 'job.done,job.failed', created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)",
            "CREATE INDEX IF NOT EXISTS idx_webhooks_user ON webhooks(user_id) WHERE active=1",
        ]),
        (6, [
            "ALTER TABLE users ADD COLUMN onboarding_seen_at TEXT",
            "CREATE INDEX IF NOT EXISTS idx_jobs_user_created ON jobs(user_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_ledger_user_created ON ledger(user_id, created_at)",
        ]),
        # Step results carried only a human sentence. The client cannot localise a
        # sentence, so a finished card either printed English prose or showed nothing
        # at all about the paid option it had just bought. Numbers and reason codes
        # now travel as data; `message` stays for logs and support.
        (7, [
            "ALTER TABLE job_events ADD COLUMN data_json TEXT NOT NULL DEFAULT '{}'",
        ]),
    ]
    return [(v, stmts) for v, stmts in all_migrations if v > current_ver]


# ---------- users / sessions ----------

def create_user(name: str, contact: str, plan: str = "free",
                secret_hash: str | None = None,
                secret_salt: str | None = None,
                secret_algo: str | None = None) -> Optional[dict]:
    cur = get_conn()
    try:
        uid = new_id()
        cur.execute(
            """INSERT INTO users (id, name, contact, plan, created_at,
                                   secret_hash, secret_salt, secret_algo)
               VALUES (?,?,?,?,?,?,?,?)""",
            (uid, name, contact.lower(), plan, now_iso(),
             secret_hash, secret_salt, secret_algo),
        )
        cur.commit()
    except sqlite3.IntegrityError:
        return None
    return get_user(uid)


def set_user_plan(uid: str, plan: str) -> None:
    """Upgrade/downgrade user plan (called on successful payment)."""
    cur = get_conn()
    cur.execute("UPDATE users SET plan = ? WHERE id = ?", (plan, uid))
    cur.commit()


def set_user_secret(uid: str, secret_hash: str, secret_salt: str, secret_algo: str) -> None:
    """Миграция legacy-секрета (например, sha256→pbkdf2) при успешном входе."""
    cur = get_conn()
    cur.execute(
        "UPDATE users SET secret_hash=?, secret_salt=?, secret_algo=? WHERE id=?",
        (secret_hash, secret_salt, secret_algo, uid),
    )
    cur.commit()


def find_user_by_contact(contact: str) -> Optional[dict]:
    row = get_conn().execute(
        "SELECT * FROM users WHERE contact = ?", (contact.lower(),)
    ).fetchone()
    return dict(row) if row else None


def get_user(uid: str) -> Optional[dict]:
    row = get_conn().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return dict(row) if row else None


def get_user_full(uid: str) -> Optional[dict]:
    """Alias for get_user(): returns all fields including secret_hash/salt/algo."""
    return get_user(uid)


# TTL сессии: 30 дней. Продлевается last_seen_at при каждом запросе (sliding).
SESSION_TTL_SEC = 30 * 24 * 3600


def create_session(uid: str) -> str:
    import time
    token = uuid.uuid4().hex + uuid.uuid4().hex
    now = time.time()
    cur = get_conn()
    cur.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at, last_seen_at) VALUES (?,?,?,?,?)",
        (token, uid, now_iso(), str(int(now + SESSION_TTL_SEC)), str(int(now))),
    )
    # фоном чистим протухшие (дешёвый запрос, не блокирует)
    # Важно: параметр должен быть INTEGER, иначе SQLite сравнивает TEXT > INTEGER
    # и удаляет ВСЕ сессии (включая только что созданную).
    cur.execute("DELETE FROM sessions WHERE expires_at IS NOT NULL AND CAST(expires_at AS INTEGER) < ?", (int(now),))
    cur.commit()
    return token


def user_by_token(token: str) -> Optional[dict]:
    import time
    conn = get_conn()
    row = conn.execute(
        """SELECT u.*, s.expires_at AS _exp, s.last_seen_at AS _seen FROM sessions s
           JOIN users u ON u.id = s.user_id WHERE s.token = ?""",
        (token,),
    ).fetchone()
    if not row:
        return None
    now = int(time.time())
    exp_raw = row["_exp"] if "_exp" in row.keys() else None
    try:
        exp = int(exp_raw) if exp_raw else 0
    except (TypeError, ValueError):
        exp = 0
    # legacy rows без expires_at — считаем не истёкшими до миграции
    if exp_raw and exp < now:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        return None
    # sliding extend: обновляем expires_at раз в час по last_seen_at
    last_seen_raw = row["_seen"]
    try:
        last_seen = int(last_seen_raw) if last_seen_raw else 0
    except (TypeError, ValueError):
        last_seen = 0
    if now - last_seen > 3600:
        conn.execute(
            "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE token = ?",
            (str(now), str(now + SESSION_TTL_SEC), token),
        )
        conn.commit()
    d = dict(row)
    d.pop("_exp", None)
    d.pop("_seen", None)
    return d


def revoke_session(token: str) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    return cur.rowcount > 0


def revoke_all_sessions(uid: str) -> int:
    conn = get_conn()
    cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
    conn.commit()
    return cur.rowcount


# ---------- jobs ----------

def create_job(uid: str, jtype: str, src: str, tgt: str, minutes: float,
               source_path: str, meta: dict) -> dict:
    jid = new_id()
    ts = now_iso()
    cur = get_conn()
    cur.execute(
        """INSERT INTO jobs (id, user_id, type, src, tgt, status, minutes,
                             source_path, meta_json, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,'{}',?,?)""",
        (jid, uid, jtype, src, tgt, "queued", minutes, source_path, ts, ts),
    )
    if meta:
        cur.execute("UPDATE jobs SET meta_json = ? WHERE id = ?", (json.dumps(meta), jid))
    cur.commit()
    return get_job(jid)  # type: ignore[return-value]


def merge_job_meta(jid: str, patch: dict) -> dict:
    """Add facts to a running job's meta without dropping what is already there.

    A read-modify-write, so it is only safe from the worker that owns the job (the
    pipeline) — which is the only caller. Used for what the client is owed after the
    fact: which engine really produced the text.
    """
    cur = get_conn()
    row = cur.execute("SELECT meta_json FROM jobs WHERE id = ?", (jid,)).fetchone()
    meta = json.loads(row["meta_json"] or "{}") if row else {}
    meta.update(patch)
    cur.execute("UPDATE jobs SET meta_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False), now_iso(), jid))
    cur.commit()
    return meta


def get_job(jid: str) -> Optional[dict]:
    row = get_conn().execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    if not row:
        return None
    job = dict(row)
    job["meta"] = json.loads(job.pop("meta_json") or "{}")
    return job


def update_job(jid: str, **fields: Any) -> None:
    fields["updated_at"] = now_iso()
    keys = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [jid]
    cur = get_conn()
    cur.execute(f"UPDATE jobs SET {keys} WHERE id = ?", vals)
    cur.commit()


def set_status_if(jid: str, new_status: str, allowed: tuple[str, ...],
                  **extra: Any) -> bool:
    """Атомарный переход статуса (compare-and-set) — основа state machine.
    extra — дополнительные поля (например, error=None при успехе)."""
    placeholders = ",".join("?" for _ in allowed)
    sets = ", ".join(["status = ?"] + [f"{k} = ?" for k in extra] + ["updated_at = ?"])
    vals: list[Any] = [new_status, *extra.values(), now_iso(), jid, *allowed]
    conn = get_conn()
    cur = conn.execute(
        f"UPDATE jobs SET {sets} WHERE id = ? AND status IN ({placeholders})",
        vals,
    )
    conn.commit()
    return cur.rowcount > 0


def recover_stale_jobs() -> list[dict]:
    """После рестарта воркеров нет: queued/running → failed (возврат делает billing)."""
    cur = get_conn()
    rows = cur.execute("SELECT * FROM jobs WHERE status IN ('queued','running')").fetchall()
    stale = [dict(r) for r in rows]
    if stale:
        cur.execute(
            "UPDATE jobs SET status='failed', error='worker restart', updated_at=? "
            "WHERE status IN ('queued','running')", (now_iso(),),
        )
        cur.commit()
    return stale


def cancel_job(jid: str) -> bool:
    """Атомарная отмена: succeeds только пока job ещё в queued."""
    return set_status_if(jid, "canceled", ("queued",))


def claim_for_retry(jid: str) -> bool:
    """Атомарный захват retry: failed/canceled → queued (один победитель гонки)."""
    return set_status_if(jid, "queued", ("failed", "canceled"))


def count_active_jobs(uid: str) -> int:
    """Сколько queued/running job у пользователя — защита от flood."""
    row = get_conn().execute(
        "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status IN ('queued','running')",
        (uid,),
    ).fetchone()
    return int(row[0]) if row else 0


def delete_job(jid: str, *, unlink_files: bool = False) -> None:
    """Полное удаление job. Если unlink_files=True — сносит upload и артефакты.
    Вызывается при отказе списания и в фоновом retention."""
    conn = get_conn()
    paths: list[str] = []
    if unlink_files:
        row = conn.execute("SELECT source_path FROM jobs WHERE id = ?", (jid,)).fetchone()
        if row and row["source_path"]:
            paths.append(row["source_path"])
        for r in conn.execute("SELECT path FROM artifacts WHERE job_id = ?", (jid,)).fetchall():
            paths.append(r["path"])
    conn.execute("DELETE FROM job_events WHERE job_id = ?", (jid,))
    conn.execute("DELETE FROM artifacts WHERE job_id = ?", (jid,))
    conn.execute("DELETE FROM jobs WHERE id = ?", (jid,))
    conn.commit()
    if unlink_files:
        for p in paths:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
        # job-специфичная папка артефактов
        try:
            art_dir = settings.artifacts_dir / jid
            if art_dir.is_dir():
                import shutil
                shutil.rmtree(art_dir, ignore_errors=True)
        except Exception:
            pass


def list_jobs(uid: str, limit: int = 50) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (uid, limit),
    ).fetchall()
    out = []
    for r in rows:
        job = dict(r)
        job["meta"] = json.loads(job.pop("meta_json") or "{}")
        out.append(job)
    return out


def list_jobs_cursor(uid: str, cursor: str | None = None, limit: int = 20,
                    status: str | None = None, jtype: str | None = None) -> list[dict]:
    """Keyset pagination with optional status/type filters.
    cursor = base64('created_at|id') from previous page."""
    import base64
    conn = get_conn()
    # build WHERE clause dynamically
    conditions = ["user_id = ?"]
    params: list = [uid]
    if status:
        conditions.append("status = ?")
        params.append(status)
    if jtype:
        conditions.append("type = ?")
        params.append(jtype)
    if cursor:
        try:
            raw = base64.urlsafe_b64decode(cursor.encode()).decode()
            ts, jid = raw.split("|", 1)
        except Exception:
            ts, jid = None, None
        if ts and jid:
            conditions.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([ts, ts, jid])
    where = " AND ".join(conditions)
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM jobs WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?",
        params,
    ).fetchall()
    out = []
    for r in rows:
        job = dict(r)
        job["meta"] = json.loads(job.pop("meta_json") or "{}")
        out.append(job)
    return out


def add_job_event(jid: str, step: str, message: str, data: dict | None = None) -> None:
    """Record one step. `message` is for humans reading logs; `data` is what the
    client renders — stable codes and finite numbers, never a sentence it would
    have to parse."""
    cur = get_conn()
    cur.execute(
        "INSERT INTO job_events (job_id, ts, step, message, data_json) VALUES (?,?,?,?,?)",
        (jid, now_iso(), step, message, json.dumps(data or {}, ensure_ascii=False)),
    )
    cur.commit()


def job_timeline(jid: str) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM job_events WHERE job_id = ? ORDER BY id", (jid,)
    ).fetchall()
    out = []
    for r in rows:
        ev = dict(r)
        # `{}` is the honest answer for a row written before the column existed.
        try:
            ev["data"] = json.loads(ev.pop("data_json") or "{}")
        except (TypeError, ValueError):
            ev["data"] = {}
        out.append(ev)
    return out


def add_artifact(jid: str, kind: str, path: str) -> None:
    cur = get_conn()
    cur.execute("INSERT INTO artifacts (job_id, kind, path) VALUES (?,?,?)", (jid, kind, path))
    cur.commit()


def get_artifact(jid: str, kind: str) -> Optional[str]:
    row = get_conn().execute(
        "SELECT path FROM artifacts WHERE job_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (jid, kind),
    ).fetchone()
    return row["path"] if row else None


def batch_artifacts(job_ids: list[str]) -> dict[str, dict[str, str]]:
    """Один запрос вместо 6*N: {job_id: {kind: path}}."""
    if not job_ids:
        return {}
    ph = ",".join("?" for _ in job_ids)
    rows = get_conn().execute(
        f"SELECT job_id, kind, path FROM artifacts WHERE job_id IN ({ph}) ORDER BY id",
        job_ids,
    ).fetchall()
    out: dict[str, dict[str, str]] = {}
    for r in rows:
        out.setdefault(r["job_id"], {})[r["kind"]] = r["path"]
    return out


def batch_latest_events(job_ids: list[str]) -> dict[str, dict]:
    """Один запрос: {job_id: {step, message, data}} — последнее событие."""
    if not job_ids:
        return {}
    ph = ",".join("?" for _ in job_ids)
    rows = get_conn().execute(
        f"""SELECT je.job_id, je.step, je.message, je.data_json FROM job_events je
            JOIN (SELECT job_id, MAX(id) AS mid FROM job_events WHERE job_id IN ({ph}) GROUP BY job_id) latest
            ON je.id = latest.mid""",
        job_ids,
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        try:
            data = json.loads(r["data_json"] or "{}")
        except (TypeError, ValueError):
            data = {}
        out[r["job_id"]] = {"step": r["step"], "message": r["message"], "data": data}
    return out


# ---------- glossary ----------

def upsert_glossary_term(uid: str, src: str, tgt: str) -> dict:
    cur = get_conn()
    existing = cur.execute(
        "SELECT * FROM glossary WHERE user_id = ? AND src_term = ?", (uid, src)
    ).fetchone()
    if existing:
        cur.execute("UPDATE glossary SET tgt_term = ? WHERE id = ?", (tgt, existing["id"]))
        cur.commit()
        return dict(existing) | {"tgt_term": tgt}
    gid = new_id()
    cur.execute(
        "INSERT INTO glossary (id, user_id, src_term, tgt_term, created_at) VALUES (?,?,?,?,?)",
        (gid, uid, src, tgt, now_iso()),
    )
    cur.commit()
    return {"id": gid, "src_term": src, "tgt_term": tgt}


def delete_glossary_term(uid: str, src_term: str) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM glossary WHERE user_id = ? AND src_term = ?", (uid, src_term))
    conn.commit()
    return cur.rowcount > 0


def glossary_for(uid: str) -> list[dict]:
    rows = get_conn().execute(
        "SELECT src_term, tgt_term FROM glossary WHERE user_id = ?", (uid,)
    ).fetchall()
    return [dict(r) for r in rows]


def delete_all_glossary(uid: str) -> int:
    conn = get_conn()
    cur = conn.execute("DELETE FROM glossary WHERE user_id = ?", (uid,))
    conn.commit()
    return cur.rowcount


def delete_user(uid: str) -> bool:
    """GDPR: удаляет пользователя. FK ON DELETE CASCADE убирает ledger/payments/glossary."""
    conn = get_conn()
    # SQLite: cascade не включён на old-style REFERENCES, чистим вручную
    conn.execute("DELETE FROM ledger WHERE user_id = ?", (uid,))
    conn.execute("DELETE FROM payments WHERE user_id = ?", (uid,))
    conn.execute("DELETE FROM glossary WHERE user_id = ?", (uid,))
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
    conn.execute("DELETE FROM notifications WHERE user_id = ?", (uid,))
    conn.execute("DELETE FROM shares WHERE user_id = ?", (uid,))
    conn.execute("DELETE FROM api_keys WHERE user_id = ?", (uid,))
    conn.execute("DELETE FROM webhooks WHERE user_id = ?", (uid,))
    conn.execute("DELETE FROM audit_log WHERE actor_id = ?", (uid,))
    # jobs уже удалены через delete_job в endpoint
    cur = conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    conn.commit()
    return cur.rowcount > 0


# ---------- audit log ----------

# ---------- retention ----------

def get_finished_jobs_older_than(days: int) -> list[str]:
    """IDs jobs in done/failed/canceled state whose updated_at is older than `days`."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = get_conn().execute(
        "SELECT id FROM jobs WHERE status IN ('done','failed','canceled') AND updated_at < ?",
        (cutoff,),
    ).fetchall()
    return [r["id"] for r in rows]


def audit(action: str, *, actor_id: str | None = None, actor_ip: str | None = None,
          target: str | None = None, amount: float | None = None,
          meta: dict | None = None) -> None:
    """Однострочник безопасного/финансового события. Пишется всегда —
    даже если вызывающий поток упал, потеря журнала хуже потери ошибки."""
    try:
        cur = get_conn()
        cur.execute(
            """INSERT INTO audit_log (ts, actor_id, actor_ip, action, target, amount, meta)
               VALUES (?,?,?,?,?,?,?)""",
            (now_iso(), actor_id, actor_ip, action, target, amount,
             json.dumps(meta, ensure_ascii=False) if meta else None),
        )
        cur.commit()
    except Exception:
        pass  # журнал не должен ронять бизнес-операцию


# ---------- payments ----------

def create_payment(uid: str, provider: str, external_id: str, amount_minor: int,
                   currency: str, minutes: float, status: str = "paid") -> dict:
    pid = new_id()
    cur = get_conn()
    cur.execute(
        """INSERT INTO payments (id, user_id, provider, external_id, amount_minor,
                                 currency, minutes, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (pid, uid, provider, external_id, amount_minor, currency, minutes, status, now_iso()),
    )
    cur.commit()
    return {"id": pid, "user_id": uid, "provider": provider, "external_id": external_id,
            "amount_minor": amount_minor, "currency": currency, "minutes": minutes,
            "status": status}


def refund_exists(uid: str, job_id: str) -> bool:
    row = get_conn().execute(
        "SELECT 1 FROM ledger WHERE user_id = ? AND ref = ? AND reason LIKE 'refund:%' LIMIT 1",
        (uid, job_id),
    ).fetchone()
    return row is not None


def debit_atomic(uid: str, minutes: float, reason: str, ref: str | None) -> bool:
    """Списание одним SQL-выражением: нет окна между проверкой и вставкой."""
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO ledger (user_id, delta, reason, ref, created_at)
           SELECT ?, ?, ?, ?, ?
           WHERE (SELECT COALESCE(SUM(delta), 0) FROM ledger WHERE user_id = ?) >= ?""",
        (uid, -minutes, reason, ref, now_iso(), uid, minutes),
    )
    conn.commit()
    return cur.rowcount > 0


def find_payment(external_id: str) -> Optional[dict]:
    row = get_conn().execute(
        "SELECT * FROM payments WHERE external_id = ?", (external_id,)
    ).fetchone()
    return dict(row) if row else None


def list_payments(uid: str, limit: int = 100) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM payments WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (uid, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- notifications ----------

def create_notification(uid: str, kind: str, title: str, body: str = "") -> dict:
    cur = get_conn()
    cur.execute(
        "INSERT INTO notifications (user_id, kind, title, body, created_at) VALUES (?,?,?,?,?)",
        (uid, kind, title, body, now_iso()),
    )
    cur.commit()
    return {"uid": uid, "kind": kind, "title": title}


def list_notifications(uid: str, unread_only: bool = False, limit: int = 50) -> list[dict]:
    q = "SELECT * FROM notifications WHERE user_id = ?"
    params: list = [uid]
    if unread_only:
        q += " AND read_at IS NULL"
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = get_conn().execute(q, params).fetchall()
    return [dict(r) for r in rows]


def mark_notification_read(nid: int, uid: str) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "UPDATE notifications SET read_at = ? WHERE id = ? AND user_id = ? AND read_at IS NULL",
        (now_iso(), nid, uid),
    )
    conn.commit()
    return cur.rowcount > 0


def count_unread_notifications(uid: str) -> int:
    row = get_conn().execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL", (uid,)
    ).fetchone()
    return int(row[0]) if row else 0


def mark_all_notifications_read(uid: str) -> int:
    """Bulk mark all unread as read. Returns count."""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
        (now_iso(), uid),
    )
    conn.commit()
    return cur.rowcount


def update_user_name(uid: str, name: str) -> bool:
    conn = get_conn()
    cur = conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, uid))
    conn.commit()
    return cur.rowcount > 0


def mark_onboarding_seen(uid: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE users SET onboarding_seen_at = ? WHERE id = ? AND onboarding_seen_at IS NULL",
        (now_iso(), uid),
    )
    conn.commit()


def get_onboarding_state(uid: str) -> str | None:
    row = get_conn().execute(
        "SELECT onboarding_seen_at FROM users WHERE id = ?", (uid,)
    ).fetchone()
    return row[0] if row and row[0] else None


def usage_summary(uid: str, days: int = 30) -> dict:
    """Aggregate a user's activity: per-day minutes, per-type counts, spend."""
    from datetime import timedelta
    # created_at is stored in UTC (now_iso) -> bucket by the UTC calendar date
    today_utc = datetime.now(timezone.utc).date()
    cutoff = (today_utc - timedelta(days=days - 1)).isoformat() + "T00:00:00"
    conn = get_conn()
    # minutes consumed per day (debits only)
    daily: dict[str, float] = {}
    for row in conn.execute(
        "SELECT substr(created_at,1,10) d, COALESCE(SUM(-delta),0) m FROM ledger "
        "WHERE user_id=? AND delta<0 AND created_at>=? GROUP BY d ORDER BY d",
        (uid, cutoff),
    ).fetchall():
        daily[row[0]] = float(row[1])
    # jobs per type
    by_type: dict[str, int] = {}
    for row in conn.execute(
        "SELECT type, COUNT(*) c FROM jobs WHERE user_id=? AND created_at>=? GROUP BY type",
        (uid, cutoff),
    ).fetchall():
        by_type[row[0]] = int(row[1])
    # totals
    tot_row = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(minutes),0) m FROM jobs WHERE user_id=? AND created_at>=?",
        (uid, cutoff),
    ).fetchone()
    total_jobs = int(tot_row[0]) if tot_row else 0
    total_minutes = float(tot_row[1]) if tot_row else 0.0
    paid_row = conn.execute(
        "SELECT COALESCE(SUM(amount_minor),0) FROM payments WHERE user_id=? AND status='paid' AND created_at>=?",
        (uid, cutoff),
    ).fetchone()
    total_paid_minor = int(paid_row[0]) if paid_row else 0
    # streak: consecutive days ending today (UTC) with activity
    streak = 0
    d = today_utc
    for _ in range(days):
        if daily.get(d.isoformat(), 0) > 0:
            streak += 1
            d -= timedelta(days=1)
        else:
            break
    return {
        "days": days,
        "since": cutoff[:10],
        "daily_minutes": daily,
        "jobs_by_type": by_type,
        "total_jobs": total_jobs,
        "total_minutes": total_minutes,
        "total_paid_minor": total_paid_minor,
        "streak_days": streak,
    }


# ---------- ledger history ----------

def ledger_history(uid: str, limit: int = 50) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?", (uid, limit)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- daily quota ----------

def minutes_spent_today(uid: str) -> float:
    """Sum of debits (negative deltas) today for this user."""
    from datetime import date
    today_start = date.today().isoformat() + "T00:00:00"
    row = get_conn().execute(
        "SELECT COALESCE(SUM(-delta), 0) FROM ledger WHERE user_id = ? AND delta < 0 AND created_at >= ?",
        (uid, today_start),
    ).fetchone()
    return float(row[0]) if row else 0.0


# ---------- job sharing ----------

def create_share(job_id: str, uid: str, ttl_hours: int = 72,
                 max_downloads: int = 0) -> str:
    """Create a public share link for a job's artifacts. Returns share_id."""
    from datetime import timedelta
    sid = new_id()
    now = datetime.now(timezone.utc)
    exp = (now + timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
    get_conn().execute(
        "INSERT INTO shares (id, job_id, user_id, created_at, expires_at, max_downloads) VALUES (?,?,?,?,?,?)",
        (sid, job_id, uid, now.isoformat(timespec="seconds"), exp, max_downloads),
    )
    get_conn().commit()
    return sid


def get_share(sid: str) -> Optional[dict]:
    row = get_conn().execute(
        "SELECT s.*, j.status AS job_status, j.type AS job_type "
        "FROM shares s JOIN jobs j ON j.id=s.job_id WHERE s.id=?",
        (sid,),
    ).fetchone()
    return dict(row) if row else None


def increment_share_download(sid: str) -> bool:
    conn = get_conn()
    conn.execute("UPDATE shares SET download_count = download_count + 1 WHERE id = ?", (sid,))
    conn.commit()
    return True


def list_shares(uid: str) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM shares WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (uid,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- API keys (programmatic auth) ----------

def create_api_key(uid: str, label: str = "") -> tuple[str, dict]:
    """Generate an API key. Returns (raw_key, key_record) — raw shown only once."""
    raw = "ovoz_" + uuid.uuid4().hex + uuid.uuid4().hex[:16]
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    prefix = raw[:14]  # "ovoz_xxxxxxxx" visible portion
    kid = new_id()
    conn = get_conn()
    conn.execute(
        "INSERT INTO api_keys (id, user_id, label, key_hash, key_prefix, created_at) VALUES (?,?,?,?,?,?)",
        (kid, uid, label, key_hash, prefix, now_iso()),
    )
    conn.commit()
    record = {"id": kid, "label": label, "key_prefix": prefix, "created_at": now_iso()}
    return raw, record


def list_api_keys(uid: str) -> list[dict]:
    rows = get_conn().execute(
        "SELECT id, label, key_prefix, created_at, last_used_at, revoked_at FROM api_keys WHERE user_id=? ORDER BY created_at DESC",
        (uid,),
    ).fetchall()
    return [dict(r) for r in rows]


def revoke_api_key(uid: str, kid: str) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "UPDATE api_keys SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
        (now_iso(), kid, uid),
    )
    conn.commit()
    return cur.rowcount > 0


def verify_api_key(raw_key: str) -> Optional[dict]:
    """Return user dict if valid API key, None otherwise."""
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    conn = get_conn()
    row = conn.execute(
        "SELECT user_id FROM api_keys WHERE key_hash=? AND revoked_at IS NULL", (key_hash,)
    ).fetchone()
    if not row:
        return None
    # update last_used (best-effort, don't block auth)
    try:
        conn.execute("UPDATE api_keys SET last_used_at=? WHERE key_hash=?", (now_iso(), key_hash))
        conn.commit()
    except Exception:
        pass
    return get_user(row[0])


# ---------- Webhooks (outbound notifications) ----------

def create_webhook(uid: str, url: str, secret: str, events: str = "job.done,job.failed") -> dict:
    wid = new_id()
    conn = get_conn()
    conn.execute(
        "INSERT INTO webhooks (id, user_id, url, secret, events, created_at) VALUES (?,?,?,?,?,?)",
        (wid, uid, url, secret, events, now_iso()),
    )
    conn.commit()
    return {"id": wid, "url": url, "events": events, "created_at": now_iso()}


def list_webhooks(uid: str) -> list[dict]:
    rows = get_conn().execute(
        "SELECT id, url, events, created_at, active FROM webhooks WHERE user_id=? ORDER BY created_at DESC",
        (uid,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_webhook(uid: str, wid: str) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM webhooks WHERE id=? AND user_id=?", (wid, uid))
    conn.commit()
    return cur.rowcount > 0


def get_active_webhooks(uid: str, event: str) -> list[dict]:
    """Return active webhooks subscribed to a specific event.

    `events` must be in the SELECT *and* split, not just referenced: the earlier
    version read `r["events"]` on a row that never selected it, so this raised on
    every call, `_fire_webhooks` swallowed it, and the paid Studio feature silently
    delivered nothing. Subscriptions are stored as a CSV ("job.done,job.failed").
    """
    rows = get_conn().execute(
        "SELECT url, secret, events FROM webhooks WHERE user_id=? AND active=1",
        (uid,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        subs = {e.strip() for e in (r["events"] or "").split(",") if e.strip()}
        if event in subs:
            out.append({"url": r["url"], "secret": r["secret"]})
    return out
