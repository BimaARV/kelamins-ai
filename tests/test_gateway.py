"""Gateway tests: failover, no-keys degradation, circuit interaction.

Uses a fake Ollama transport - no network, no real keys.
"""

import pytest

from app.kela_ai.circuit_breaker import STATE_OPEN, CircuitBreaker
from app.kela_ai.gateway import AIUnavailable, KELAAIGateway
from app.kela_ai.key_manager import STATE_DISABLED, OllamaKeyManager
from app.kela_ai.ollama_client import (
    ERR_INVALID_CREDENTIALS,
    ERR_OTHER,
    OllamaError,
    OllamaResponse,
)
from app.kela_ai.rate_limiter import SlidingWindowRateLimiter


class FakeTransport:
    """Mimics OllamaClient.chat; inject failures per key name."""

    def __init__(self):
        self.fail: dict[str, str] = {}  # key -> error_code to raise

    async def chat(self, messages, **kwargs):
        key = kwargs["api_key"]
        code = self.fail.get(key)
        if code is not None:
            raise OllamaError(f"boom: {code}", code)
        return OllamaResponse(
            content=f'{{"answer":"ok from {key}"}}', input_tokens=10, output_tokens=5
        )


def _gateway(transport: FakeTransport, keys: list[dict]) -> KELAAIGateway:
    return KELAAIGateway(
        client=transport,
        key_manager=OllamaKeyManager(keys=keys, cooldown_seconds=60),
        rate_limiter=SlidingWindowRateLimiter(limit_per_minute=1000),
        circuit_breaker=CircuitBreaker(failure_threshold=10, cooldown_seconds=60),
        model="test-model",
    )


async def test_no_keys_raises_no_keys():
    gateway = _gateway(FakeTransport(), [])
    gateway._key_manager = OllamaKeyManager(keys=[], cooldown_seconds=1)
    with pytest.raises(AIUnavailable) as exc:
        await gateway.complete([{"role": "user", "content": "hi"}])
    assert exc.value.error_code == "NO_KEYS"


async def test_success_returns_content_and_records_key():
    transport = FakeTransport()
    keys = [{"name": "K1", "value": "a"}]
    gateway = _gateway(transport, keys)
    result = await gateway.complete([{"role": "user", "content": "hi"}])
    assert result.content == '{"answer":"ok from a"}'
    assert result.key_name == "K1"
    assert result.input_tokens == 10
    stats = gateway.status["keys"]["keys"]["K1"]
    assert stats["successful_requests"] == 1


async def test_failover_to_second_key_on_auth_failure():
    transport = FakeTransport()
    keys = [
        {"name": "K1", "value": "bad"},
        {"name": "K2", "value": "good"},
    ]
    transport.fail["bad"] = ERR_INVALID_CREDENTIALS
    gateway = _gateway(transport, keys)
    result = await gateway.complete([{"role": "user", "content": "hi"}])
    assert result.key_name == "K2"
    assert gateway.status["keys"]["keys"]["K1"]["state"] == STATE_DISABLED


async def test_all_keys_failed_raises_with_last_code():
    transport = FakeTransport()
    keys = [
        {"name": "K1", "value": "a"},
        {"name": "K2", "value": "b"},
    ]
    transport.fail.update({"a": ERR_OTHER, "b": ERR_OTHER})
    gateway = _gateway(transport, keys)
    with pytest.raises(AIUnavailable) as exc:
        await gateway.complete([{"role": "user", "content": "hi"}])
    assert exc.value.error_code == ERR_OTHER


async def test_circuit_open_skips_key_until_recovered():
    transport = FakeTransport()
    keys = [{"name": "K1", "value": "a"}]
    transport.fail["a"] = ERR_OTHER  # OTHER: key stays ACTIVE, breaker trips
    gateway = _gateway(transport, keys)
    gateway._circuit_breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=600)

    for _ in range(2):
        with pytest.raises(AIUnavailable):
            await gateway.complete([{"role": "user", "content": "hi"}])

    breaker = gateway.status["circuit_breaker"]["K1"]
    assert breaker["state"] == STATE_OPEN
    with pytest.raises(AIUnavailable) as exc:
        await gateway.complete([{"role": "user", "content": "hi"}])
    assert exc.value.error_code == "EXHAUSTED"