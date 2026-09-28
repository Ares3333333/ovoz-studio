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
import json
import logging
import threading
import time
import urllib.request
import urllib.error
from typing import Optional

log = logging.getLogger("ovoz.webhooks")


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
            resp = urllib.request.urlopen(req, timeout=5)
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
