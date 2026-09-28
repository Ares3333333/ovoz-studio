"""Notification service: in-app notification queue for users.

Triggers:
- job completed / failed
- low balance (< 1 minute)
- files expiring soon (retention warning)
- payment received / refund

Notifications are stored in the `notifications` table and delivered via
the /api/notifications endpoint + WebSocket.
"""
from __future__ import annotations

from enum import Enum
from . import db


class NotifKind(str, Enum):
    JOB_DONE = "job_done"
    JOB_FAILED = "job_failed"
    BALANCE_LOW = "balance_low"
    FILES_EXPIRING = "files_expiring"
    PAYMENT_RECEIVED = "payment_received"
    REFUND = "refund"


def notify(uid: str, kind: NotifKind, title: str, body: str = "") -> dict:
    """Persist a notification to the notifications table."""
    db.create_notification(uid, kind.value, title, body)
    return {"uid": uid, "kind": kind.value, "title": title}


def maybe_notify_balance_low(uid: str, balance: float, threshold: float = 1.0) -> None:
    """Called after debit: warn if user is about to run out."""
    if balance < threshold and balance > 0:
        notify(uid, NotifKind.BALANCE_LOW,
               title="Balance low",
               body=f"Only {balance:.1f} minutes remaining")
