"""Circuit breaker (per key) - reliability principle #13.

CLOSED   normal operation
OPEN     too many consecutive failures; reject until cooldown elapses
HALF_OPEN one probe allowed after cooldown; success closes, failure reopens

The AI worker is inherently single-flight, so HALF_OPEN probes one at a time
without an explicit queue discipline.
"""

from __future__ import annotations

import threading
import time

STATE_CLOSED = "CLOSED"
STATE_OPEN = "OPEN"
STATE_HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, cooldown_seconds: int = 60):
        self._threshold = max(1, failure_threshold)
        self._cooldown = max(0.0, float(cooldown_seconds))
        self._consecutive_failures: dict[str, int] = {}
        self._state: dict[str, str] = {}
        self._opened_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def _failures(self, name: str) -> int:
        return self._consecutive_failures.get(name, 0)

    def _opened_remaining(self, name: str) -> float:
        opened = self._opened_at.get(name)
        if opened is None:
            return 0.0
        return max(0.0, self._cooldown - (time.monotonic() - opened))

    def state_of(self, name: str) -> str:
        if self._state.get(name) == STATE_OPEN:
            if self._opened_remaining(name) <= 0:
                self._state[name] = STATE_HALF_OPEN
        return self._state.get(name, STATE_CLOSED)

    def allow(self, name: str) -> bool:
        return self.state_of(name) != STATE_OPEN

    def record_success(self, name: str) -> None:
        with self._lock:
            self._consecutive_failures[name] = 0
            self._state[name] = STATE_CLOSED
            self._opened_at.pop(name, None)

    def record_failure(self, name: str) -> None:
        with self._lock:
            self._consecutive_failures[name] = self._failures(name) + 1
            if self._state.get(name) == STATE_HALF_OPEN:
                self._state[name] = STATE_OPEN
                self._opened_at[name] = time.monotonic()
            elif self._consecutive_failures[name] >= self._threshold:
                self._state[name] = STATE_OPEN
                self._opened_at[name] = time.monotonic()

    def stats(self) -> dict:
        return {
            name: {
                "state": self.state_of(name),
                "consecutive_failures": self._failures(name),
                "reopens_in_s": round(self._opened_remaining(name), 1),
            }
            for name in list(self._state)
        }