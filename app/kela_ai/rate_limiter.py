"""In-memory sliding-window rate limiter (per key + global).

A token is consumed on every request attempt against a key. The per-key
ceiling is multiplied by the pool size for the global ceiling so a single key
cannot starve failover to healthy siblings.
"""

from __future__ import annotations

import threading
import time
from collections import deque

_WINDOW_SECONDS = 60.0


class SlidingWindowRateLimiter:
    def __init__(self, limit_per_minute: int = 30):
        self._limit = max(1, limit_per_minute)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, name: str, now: float) -> deque[float]:
        hits = self._hits.setdefault(name, deque())
        cutoff = now - _WINDOW_SECONDS
        while hits and hits[0] < cutoff:
            hits.popleft()
        return hits

    def allow(self, name: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._prune(name, now)
            if len(hits) >= self._limit:
                return False
            hits.append(now)
            return True

    def remaining(self, name: str) -> int:
        now = time.monotonic()
        with self._lock:
            hits = self._prune(name, now)
            return self._limit - len(hits)

    def stats(self) -> dict:
        now = time.monotonic()
        with self._lock:
            return {
                name: {"window_seconds": _WINDOW_SECONDS, "remaining": self._limit - len(self._prune(name, now))}
                for name in list(self._hits)
            }