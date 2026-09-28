"""Circuit breaker for external providers (ASR, Translate, TTS).

Prevents cascading failures when a provider is down: after N consecutive
failures the circuit opens and short-circuits requests for a cooldown period,
then allows one probe request through (half-open).
"""
from __future__ import annotations

import time
import threading
from enum import Enum


class State(str, Enum):
    CLOSED = "closed"       # normal: requests pass through
    OPEN = "open"           # failing: requests short-circuit immediately
    HALF_OPEN = "half_open" # one probe allowed to test recovery


class CircuitBreaker:
    """Per-provider circuit breaker (thread-safe)."""

    def __init__(self, name: str, failure_threshold: int = 5,
                 cooldown_sec: float = 60.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_sec = cooldown_sec
        self._lock = threading.Lock()
        self._state = State.CLOSED
        self._failures = 0
        self._opened_at: float = 0

    @property
    def state(self) -> State:
        with self._lock:
            if self._state == State.OPEN:
                if time.monotonic() - self._opened_at >= self.cooldown_sec:
                    self._state = State.HALF_OPEN
            return self._state

    def allow(self) -> bool:
        """Whether a request is allowed through."""
        s = self.state
        return s in (State.CLOSED, State.HALF_OPEN)

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = State.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == State.HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = State.OPEN
                self._opened_at = time.monotonic()

    def status(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "failures": self._failures,
            "threshold": self.failure_threshold,
        }


# Registry: one breaker per provider kind
_breakers: dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def get_breaker(name: str, threshold: int = 5, cooldown: float = 60.0) -> CircuitBreaker:
    with _registry_lock:
        if name not in _breakers:
            _breakers[name] = CircuitBreaker(name, threshold, cooldown)
        return _breakers[name]


def all_status() -> list[dict]:
    with _registry_lock:
        return [b.status() for b in _breakers.values()]
