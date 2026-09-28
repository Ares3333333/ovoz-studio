"""Outbound webhooks: notify user's server when jobs complete.

Users (Studio plan) can register callback URLs. On terminal job events
(done/failed), we POST a signed payload to each registered URL.

Security:
  - URL must be HTTPS (enforced at registration)
  - Payload signed with HMAC-SHA256 using a per-webhook secret
  - 5-second timeout; 2 retries with exponential backoff
  - Max 5 webhooks per user
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import threading
import time
import urllib.parse
import urllib.request
import urllib.error

log = logging.getLogger("ovoz.webhooks")


def safe_webhook_url(url: str) -> str | None:
    """None — URL годен к отправке; иначе причина отказа.

    Защита от SSRF: платный Studio-plan даёт пользователю право заставить СЕРВЕР
    сделать POST. `startswith("https://")` от этого не защищает: редирект уходит
    куда угодно, а https://169.254.169.254/ (мета-данные облака), loopback и
    RFC1918 — «внутренний глаз» из хорошо укрепленного контейнера. Проверяем
    схему, userinfo и ВСЕ резолвы хоста — и при регистрации, и при отправке
    (DNS-rebinding между этими моментами — тоже сценарий).
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "invalid URL"
    if parts.scheme != "https":
        return "HTTPS is required"
    if not parts.hostname or parts.username or parts.password:
        return "URL must not carry credentials"
    try:
        port = parts.port or 443
        infos = socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return "hostname does not resolve"
    for _fam, _t, _p, _c, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return "hostname does not resolve"
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return "target resolves to a private address"
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """User-supplied webhook targets must never bounce: a friendly https host that
    302s to 169.254.169.254 defeats every check at registration time."""

    def redirect_request(self, *args, **kwargs):  # noqa: D102
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def send_webhook(url: str, secret: str, event_type: str, payload: dict) -> bool:
    """Fire a webhook POST. Returns True on 2xx, False otherwise.
    Called from background thread (non-blocking).
    """
    body = json.dumps({
        "event": event_type,
        "timestamp": int(time.time()),
        "data": payload,
    }, ensure_ascii=False).encode("utf-8")

    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    for attempt in range(3):
        # проверка при каждой попытке: DNS может переродиться между регистрацией
        # и доставкой, а повтор — через 1–4 секунды после первого ответа хоста
        reason = safe_webhook_url(url)
        if reason is not None:
            log.warning(f"webhook refused: {reason} event={event_type}")
            return False
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Ovoz-Signature": signature,
                    "X-Ovoz-Event": event_type,
                    "User-Agent": "Ovoz-Webhooks/1.0",
                },
                method="POST",
            )
            resp = _opener.open(req, timeout=5)
            if 200 <= resp.status < 300:
                return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as e:
            if attempt < 2:
                time.sleep(2 ** attempt)  # 1s, 2s backoff
            else:
                log.warning(f"webhook delivery failed: {url} event={event_type} error={e}")
    return False


def fire_async(url: str, secret: str, event_type: str, payload: dict) -> None:
    """Fire-and-forget: dispatch webhook in a daemon thread."""
    t = threading.Thread(
        target=send_webhook,
        args=(url, secret, event_type, payload),
        daemon=True,
        name=f"webhook-{event_type}",
    )
    t.start()
