"""Sliding-window rate limiter tests."""

import time

from app.kela_ai import rate_limiter
from app.kela_ai.rate_limiter import SlidingWindowRateLimiter


def test_allow_until_limit_then_block():
    limiter = SlidingWindowRateLimiter(limit_per_minute=2)
    assert limiter.allow("k1") is True
    assert limiter.allow("k1") is True
    assert limiter.allow("k1") is False
    assert limiter.remaining("k1") == 0


def test_keys_are_independent():
    limiter = SlidingWindowRateLimiter(limit_per_minute=1)
    assert limiter.allow("k1") is True
    assert limiter.allow("k1") is False
    assert limiter.allow("k2") is True  # different key unaffected


def test_window_slides_after_expiry(monkeypatch):
    limiter = SlidingWindowRateLimiter(limit_per_minute=1)
    assert limiter.allow("k1") is True
    assert limiter.allow("k1") is False
    monkeypatch.setattr(rate_limiter, "_WINDOW_SECONDS", 0.04)
    time.sleep(0.06)
    assert limiter.allow("k1") is True
    assert limiter.remaining("k1") == 0