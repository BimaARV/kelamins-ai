"""Circuit breaker tests (reliability principle #13)."""

import time

from app.kela_ai.circuit_breaker import (
    STATE_CLOSED,
    STATE_HALF_OPEN,
    STATE_OPEN,
    CircuitBreaker,
)


def test_closed_by_default_and_allows():
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
    assert breaker.state_of("k1") == STATE_CLOSED
    assert breaker.allow("k1") is True


def test_opens_after_threshold_failures():
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
    for _ in range(3):
        breaker.record_failure("k1")
    assert breaker.state_of("k1") == STATE_OPEN
    assert breaker.allow("k1") is False


def test_success_resets_failures_and_closes():
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
    breaker.record_failure("k1")
    breaker.record_success("k1")
    assert breaker.state_of("k1") == STATE_CLOSED
    assert breaker.allow("k1") is True


def test_half_open_probe_after_cooldown():
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.06)
    breaker.record_failure("k1")
    assert breaker.state_of("k1") == STATE_OPEN
    time.sleep(0.08)
    assert breaker.state_of("k1") == STATE_HALF_OPEN
    assert breaker.allow("k1") is True  # probe allowed
    breaker.record_success("k1")
    assert breaker.state_of("k1") == STATE_CLOSED


def test_failed_probe_reopens():
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=0.05)
    breaker.record_failure("k1")
    breaker.record_failure("k1")
    breaker.record_failure("k1")
    time.sleep(0.07)
    assert breaker.state_of("k1") == STATE_HALF_OPEN
    breaker.record_failure("k1")
    assert breaker.state_of("k1") == STATE_OPEN
    assert breaker.allow("k1") is False