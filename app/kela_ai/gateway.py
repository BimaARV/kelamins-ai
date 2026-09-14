"""KELA AI Gateway - the single entry point for all AI calls (spec section 11).

Pipeline per request (multi-provider model pool):
  1. pick a configured model pool (priority order from AI_MODELS / AI_* / OLLAMA_*)
  2. inside the pool: pick an ACTIVE key (failover-aware)
  3. rate limiter check
  4. circuit breaker check
  5. chat call via the async client (provider-aware request shaping)
  6. on failure: update key state + breaker, try the next key
  7. when a whole pool is exhausted, move to the next model pool. With a single
     pool (the common case) this collapses to plain key failover - no
     cross-model failover happens.

The gateway is storage-agnostic: it returns AIResult (or raises AIUnavailable).
Persistence of ai_requests / ai_interpretations is handled by services.py so
this class stays unit-testable without a database.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.kela_ai.circuit_breaker import CircuitBreaker
from app.kela_ai.key_manager import STATE_ACTIVE, OllamaKeyManager
from app.kela_ai.ollama_client import (
    PROVIDER_OLLAMA,
    PROVIDERS,
    OllamaClient,
    OllamaError,
)
from app.kela_ai.rate_limiter import SlidingWindowRateLimiter

logger = logging.getLogger(__name__)

CODE_NO_KEYS = "NO_KEYS"
CODE_EXHAUSTED = "EXHAUSTED"
CODE_RATE_LIMITED = "RATE_LIMITED"
CODE_CIRCUIT_OPEN = "CIRCUIT_OPEN"


class AIUnavailable(Exception):
    def __init__(self, message: str, error_code: str = CODE_EXHAUSTED):
        super().__init__(message)
        self.error_code = error_code


@dataclass
class AIResult:
    content: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    key_name: str | None = None
    model: str | None = None
    provider: str | None = None


@dataclass
class ModelSpec:
    provider: str = PROVIDER_OLLAMA
    base_url: str = "https://ollama.com"
    model: str = "gemma4:cloud"


@dataclass
class ModelPool:
    spec: ModelSpec
    key_manager: OllamaKeyManager | None = None
    client: OllamaClient | None = None

    def configured(self) -> bool:
        return bool(self.key_manager and self.key_manager.configured)

    def as_dict(self) -> dict:
        stats = self.key_manager.stats() if self.key_manager else {}
        return {
            "provider": self.spec.provider,
            "base_url": self.spec.base_url,
            "model": self.spec.model,
            "configured": self.configured(),
            "active_keys": int(stats.get("active", 0)),
        }


class KELAAIGateway:
    def __init__(
        self,
        *,
        client: OllamaClient | None = None,
        key_manager: OllamaKeyManager | None = None,
        rate_limiter: SlidingWindowRateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        model: str = "gemma4:cloud",
        max_tokens: int = 800,
        temperature: float = 0.7,
        timeout: float = 120.0,
        models: list[ModelPool] | None = None,
    ):
        if models:
            self._models = list(models)
        else:
            # Legacy single-pool (used by hermetic tests and callers that
            # construct a gateway from a bare client + key manager).
            self._models = [
                ModelPool(
                    spec=ModelSpec(provider=PROVIDER_OLLAMA, base_url="", model=model),
                    key_manager=key_manager,
                    client=client,
                )
            ]
        self._client = client
        self._key_manager = key_manager
        self._rate_limiter = rate_limiter
        self._circuit_breaker = circuit_breaker
        self.model = model or (self._models[0].spec.model if self._models else model)
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout

    @property
    def client(self) -> OllamaClient | None:
        return self._client

    @property
    def models(self) -> list[ModelPool]:
        return list(self._models)

    @property
    def provider(self) -> str:
        return self._models[0].spec.provider if self._models else PROVIDER_OLLAMA

    @property
    def configured(self) -> bool:
        return any(pool.configured() for pool in self._models)

    @property
    def status(self) -> dict:
        keys_by_name: dict[str, dict] = {}
        for pool in self._models:
            if not pool.key_manager:
                continue
            keys_by_name.update(pool.key_manager.stats().get("keys", {}) or {})
        return {
            "configured": self.configured,
            "keys": {
                "keys": keys_by_name,
                "configured": bool(keys_by_name),
                "active": len([k for k in keys_by_name.values() if k.get("state") == "ACTIVE"]),
            },
            "rate_limiter": self._rate_limiter.stats() if self._rate_limiter else {},
            "circuit_breaker": self._circuit_breaker.stats() if self._circuit_breaker else {},
            "model": self.model,
            "models": [pool.as_dict() for pool in self._models],
        }

    async def complete(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AIResult:
        pools = self._models
        if not pools or not any(pool.configured() for pool in pools):
            raise AIUnavailable(
                "no AI API key configured - AI processing skipped", CODE_NO_KEYS
            )

        temperature = temperature if temperature is not None else self.temperature
        max_tokens = max_tokens if max_tokens is not None else self.max_tokens

        if len(pools) == 1:
            # Single model - plain key failover, no cross-model failover.
            return await self._complete_with_pool(pools[0], messages, temperature, max_tokens)

        failures: list[str] = []
        for pool in pools:
            if not pool.configured():
                continue
            try:
                return await self._complete_with_pool(pool, messages, temperature, max_tokens)
            except AIUnavailable as exc:
                failures.append(f"{pool.spec.provider}/{pool.spec.model}: {exc.error_code}")
                logger.warning("model pool %s exhausted (%s) - trying next", pool.spec.model, exc.error_code)

        if not failures:
            raise AIUnavailable("all keys skipped by rate limiter/circuit", CODE_EXHAUSTED)
        last = failures[-1].split(": ")[-1]
        raise AIUnavailable("all model pools exhausted: " + "; ".join(failures), last)

    async def _complete_with_pool(
        self,
        pool: ModelPool,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> AIResult:
        key_manager = pool.key_manager
        attempts = 0
        last_error: OllamaError | None = None

        for name in key_manager.names:
            if key_manager.available_key() is None:
                break
            if key_manager.state_of(name) != STATE_ACTIVE:
                continue
            if not self._rate_limiter.allow(name):
                continue
            if self._circuit_breaker and not self._circuit_breaker.allow(name):
                logger.warning("circuit OPEN for key %s - skip", name)
                continue

            attempts += 1
            started = time.monotonic()
            try:
                response = await pool.client.chat(
                    messages,
                    api_key=key_manager.key_value(name),
                    model=pool.spec.model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=self.timeout,
                )
            except OllamaError as exc:
                last_error = exc
                latency_ms = int((time.monotonic() - started) * 1000)
                key_manager.mark_failure(name, exc.error_code, latency_ms)
                if self._circuit_breaker:
                    self._circuit_breaker.record_failure(name)
                logger.warning(
                    "key %s failed (%s) - failover to next key", name, exc.error_code
                )
                continue

            latency_ms = int((time.monotonic() - started) * 1000)
            key_manager.mark_success(name, latency_ms)
            if self._circuit_breaker:
                self._circuit_breaker.record_success(name)
            return AIResult(
                content=response.content,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                latency_ms=latency_ms,
                key_name=name,
                model=pool.spec.model,
                provider=pool.spec.provider,
            )

        if attempts == 0:
            raise AIUnavailable("all keys skipped by rate limiter/circuit", CODE_EXHAUSTED)
        raise AIUnavailable(
            f"all keys failed (last: {last_error.error_code if last_error else 'unknown'})",
            last_error.error_code if last_error else CODE_EXHAUSTED,
        )


_definitions: dict | None = None


def _normalize_provider(provider: str | None) -> str:
    provider = (provider or PROVIDER_OLLAMA).strip().lower()
    return provider if provider in PROVIDERS else PROVIDER_OLLAMA


def _load_model_specs(settings) -> list[ModelSpec]:
    raw = settings.load_fixtures("ai_models")
    specs: list[ModelSpec] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        provider = _normalize_provider(entry.get("provider"))
        base_url = entry.get("base_url") or settings.ai_base_url or settings.ollama_base_url
        model = entry.get("model") or settings.ai_model or settings.ollama_model
        specs.append(ModelSpec(provider=provider, base_url=base_url, model=model))
    return specs


def _single_model_spec(settings) -> ModelSpec:
    provider = _normalize_provider(getattr(settings, "ai_provider", None))
    return ModelSpec(
        provider=provider,
        base_url=settings.ai_base_url or settings.ollama_base_url,
        model=settings.ai_model or settings.ollama_model,
    )


def build_gateway(settings) -> KELAAIGateway:
    """Construct the production gateway from settings (single source of truth)."""
    from app.kela_ai.circuit_breaker import CircuitBreaker
    from app.kela_ai.ollama_client import OllamaClient
    from app.kela_ai.rate_limiter import SlidingWindowRateLimiter

    global _definitions

    specs = _load_model_specs(settings)
    if not specs:
        specs = [_single_model_spec(settings)]

    pools: list[ModelPool] = []
    for spec in specs:
        client = OllamaClient(
            base_url=spec.base_url,
            provider=spec.provider,
            model=spec.model,
            timeout=settings.ai_timeout_seconds,
        )
        key_manager = OllamaKeyManager(
            keys=OllamaKeyManager.keys_for_provider(spec.provider),
            cooldown_seconds=settings.ai_key_cooldown_seconds,
        )
        pools.append(
            ModelPool(spec=spec, client=client, key_manager=key_manager)
        )

    first = pools[0].spec
    _definitions = {
        "provider": first.provider,
        "base_url": first.base_url,
        "model": first.model,
        "models": [
            {
                "provider": pool.spec.provider,
                "base_url": pool.spec.base_url,
                "model": pool.spec.model,
                "key_names": pool.key_manager.names,
            }
            for pool in pools
        ],
    }
    return KELAAIGateway(
        models=pools,
        rate_limiter=SlidingWindowRateLimiter(
            limit_per_minute=settings.ai_rate_limit_per_minute
        ),
        circuit_breaker=CircuitBreaker(
            failure_threshold=settings.ai_circuit_failure_threshold,
            cooldown_seconds=settings.ai_circuit_cooldown_seconds,
        ),
        model=first.model,
        max_tokens=settings.ai_max_tokens,
        temperature=settings.ai_temperature,
        timeout=settings.ai_timeout_seconds,
    )


def gateway_definitions() -> dict | None:
    return _definitions