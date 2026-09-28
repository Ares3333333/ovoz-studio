"""Unified error model: machine-readable codes + HTTP response helper."""
from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """Stable API error codes — clients match on these, not on locale strings."""
    BAD_REQUEST = "bad_request"
    UNAUTHORIZED = "unauthorized"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    GONE = "gone"
    # 415 is the aligner's answer to a container it will not decode. Without its own
    # code it fell through to internal_error, which tells an SDK "our fault, retry"
    # about the caller's file — the one thing a readable refusal must never say.
    UNSUPPORTED_MEDIA = "unsupported_media"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    # 411 has its own code because it is not "too large": an upload without a
    # declared length may be perfectly legal, we are only saying that we cannot
    # bound it before it reaches the disk. An SDK that matches on
    # `payload_too_large` would trim a file that never was too big.
    LENGTH_REQUIRED = "length_required"
    VALIDATION_ERROR = "validation_error"
    RATE_LIMITED = "rate_limited"
    NOT_IMPLEMENTED = "not_implemented"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INTERNAL_ERROR = "internal_error"


def error_response(status_code: int, code: ErrorCode, detail: str) -> dict:
    """Build the standard error envelope body (used by middleware / handlers)."""
    body: dict = {"detail": detail, "error_code": code.value}
    if status_code >= 500:
        body["retryable"] = True
    return body
