"""Model pool tests: 2-level failover (keys inside a pool, then across pools).

- 1 model  -> plain key failover, NO cross-model failover
- 2+ models -> when pool 1 is fully exhausted, pool 2 takes over
- provider/model of the winning pool are reported on AIResult
"""

import pytest

from app.kela_ai.circuit_breaker import CircuitBreaker
from app.kela_ai.gateway import AIUnavailable, KELAAIGateway, ModelPool, ModelSpec
from app.kela_ai.key_manager import OllamaKeyManager
from app.kela_ai.ollama_client import ERR_OTHER, ERR_SERVER, OllamaError, OllamaResponse
from app.kela_ai.rate_limiter import SlidingWindowRateLimiter


class FakeTransport:
    """Mimics OllamaClient.chat; routes failure per key or per model."""

    def __init__(self):
        self.call_log: list[dict] = []
        self.fail_by_key: dict[str, str] = {}
        self.fail_by_model: dict[str, str] = {}

    async def chat(self, messages, **kwargs):
        self.call_log.append({"api_key": kwargs["api_key"], "model": kwargs["model"]})
        code = self.fail_by_key.get(kwargs["api_key"])
        if code is None:
            code = self.fail_by_model.get(kwargs["model"])
        if code is not None:
            raise OllamaError(f"boom: {code}", code)
        return OllamaResponse(
            content=f'{{"answer":"ok {kwargs["model"]}"}}', input_tokens=1, output_tokens=2
        )


def _pool(transport, provider: str, model: str, keys: list[dict]) -> ModelPool:
    return ModelPool(
        spec=ModelSpec(provider=provider, base_url=f"https://{provider}.test", model=model),
        client=transport,
        key_manager=OllamaKeyManager(keys=keys, cooldown_seconds=1),
    )


def _gateway(transport, pools: list[ModelPool]) -> KELAAIGateway:
    return KELAAIGateway(
        models=pools,
        rate_limiter=SlidingWindowRateLimiter(limit_per_minute=1000),
        circuit_breaker=CircuitBreaker(failure_threshold=10, cooldown_seconds=60),
        model="test",
    )


async def test_single_pool_success_reports_provider_and_model():
    transport = FakeTransport()
    gw = _gateway(transport, [_pool(transport, "gemini", "gemini-flash", [{"name": "GK", "value": "g"}])])
    result = await gw.complete([{"role": "user", "content": "hi"}])
    assert result.content == '{"answer":"ok gemini-flash"}'
    assert result.key_name == "GK"
    assert result.provider == "gemini"
    assert result.model == "gemini-flash"


async def test_single_pool_key_failover_no_cross_model():
    transport = FakeTransport()
    transport.fail_by_key["bad"] = ERR_OTHER
    gw = _gateway(
        transport,
        [_pool(transport, "ollama", "gemma4", [
            {"name": "K1", "value": "bad"},
            {"name": "K2", "value": "good"},
        ])],
    )
    result = await gw.complete([{"role": "user", "content": "hi"}])
    assert result.key_name == "K2"
    assert result.provider == "ollama"
    assert result.model == "gemma4"


async def test_single_pool_all_failed_keeps_original_error_code():
    transport = FakeTransport()
    transport.fail_by_model["a"] = ERR_OTHER
    transport.fail_by_model["b"] = ERR_OTHER
    gw = _gateway(
        transport,
        [_pool(transport, "ollama", "a", [{"name": "K1", "value": "x"}, {"name": "K2", "value": "y"}])],
    )
    with pytest.raises(AIUnavailable) as exc:
        await gw.complete([{"role": "user", "content": "hi"}])
    assert exc.value.error_code == ERR_OTHER


async def test_second_pool_takes_over_when_first_exhausted():
    transport = FakeTransport()
    transport.fail_by_model["slow-model"] = ERR_SERVER
    pools = [
        _pool(transport, "openai", "slow-model", [{"name": "O1", "value": "a"}, {"name": "O2", "value": "b"}]),
        _pool(transport, "claude", "claude-sonnet", [{"name": "C1", "value": "c"}]),
    ]
    gw = _gateway(transport, pools)
    result = await gw.complete([{"role": "user", "content": "hi"}])
    assert result.provider == "claude"
    assert result.model == "claude-sonnet"
    assert result.key_name == "C1"
    assert [c["api_key"] for c in transport.call_log] == ["a", "b", "c"]


async def test_first_pool_success_never_touches_second():
    transport = FakeTransport()
    pools = [
        _pool(transport, "gemini", "flash", [{"name": "G1", "value": "g"}]),
        _pool(transport, "openai", "gpt", [{"name": "P1", "value": "p"}]),
    ]
    gw = _gateway(transport, pools)
    result = await gw.complete([{"role": "user", "content": "hi"}])
    assert result.model == "flash"
    assert [c["api_key"] for c in transport.call_log] == ["g"]


async def test_pool_without_keys_is_skipped():
    transport = FakeTransport()
    transport.fail_by_model["dead"] = ERR_OTHER
    pools = [
        _pool(transport, "openai", "dead", []),
        _pool(transport, "gemini", "gemini-flash", [{"name": "G1", "value": "g"}]),
    ]
    gw = _gateway(transport, pools)
    result = await gw.complete([{"role": "user", "content": "hi"}])
    assert result.provider == "gemini"
    assert [c["api_key"] for c in transport.call_log] == ["g"]


async def test_all_pools_without_keys_raises_no_keys():
    transport = FakeTransport()
    gw = _gateway(
        transport,
        [
            _pool(transport, "ollama", "m1", []),
            _pool(transport, "gemini", "m2", []),
        ],
    )
    with pytest.raises(AIUnavailable) as exc:
        await gw.complete([{"role": "user", "content": "hi"}])
    assert exc.value.error_code == "NO_KEYS"


async def test_status_lists_each_pool():
    transport = FakeTransport()
    pools = [
        _pool(transport, "ollama", "gemma4", [{"name": "K1", "value": "a"}]),
        _pool(transport, "gemini", "flash", [{"name": "G1", "value": "g"}]),
    ]
    gw = _gateway(transport, pools)
    models = gw.status["models"]
    assert [(m["provider"], m["model"], m["active_keys"]) for m in models] == [
        ("ollama", "gemma4", 1),
        ("gemini", "flash", 1),
    ]