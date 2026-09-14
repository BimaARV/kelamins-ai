"""Async multi-provider chat client.

Only transport concerns live here: HTTP, timeouts, status mapping, request
shaping per provider and response normalisation. Policy (failover, rate limit,
circuit breaker) lives in the gateway, key hygiene in the key manager. Auth is
Bearer per key - nothing is stored or logged.

Providers:
  - ollama : native /api/chat (or OpenAI /chat/completions when the base URL
             already carries /v1 or /api)
  - openai / gemini / claude : OpenAI-compatible /chat/completions
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

PROVIDER_OLLAMA = "ollama"
PROVIDER_OPENAI = "openai"
PROVIDER_GEMINI = "gemini"
PROVIDER_CLAUDE = "claude"
PROVIDERS = {
    PROVIDER_OLLAMA,
    PROVIDER_OPENAI,
    PROVIDER_GEMINI,
    PROVIDER_CLAUDE,
}

# Stable error codes persisted into ai_requests.error_code / key stats.
ERR_TIMEOUT = "TIMEOUT"
ERR_NETWORK = "NETWORK"
ERR_INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
ERR_FORBIDDEN = "FORBIDDEN"
ERR_RATE_LIMIT = "RATE_LIMIT"
ERR_SERVER = "SERVER"
ERR_OTHER = "OTHER"


class OllamaError(Exception):
    """Base transport error; error_code is stable across implementations."""

    def __init__(self, message: str, error_code: str = ERR_OTHER):
        super().__init__(message)
        self.error_code = error_code


class OllamaTimeoutError(OllamaError):
    def __init__(self, message: str):
        super().__init__(message, ERR_TIMEOUT)


class OllamaNetworkError(OllamaError):
    def __init__(self, message: str):
        super().__init__(message, ERR_NETWORK)


class OllamaAuthError(OllamaError):
    def __init__(self, message: str, error_code: str = ERR_INVALID_CREDENTIALS):
        super().__init__(message, error_code)


class OllamaRateLimitError(OllamaError):
    def __init__(self, message: str):
        super().__init__(message, ERR_RATE_LIMIT)


class OllamaServerError(OllamaError):
    def __init__(self, message: str):
        super().__init__(message, ERR_SERVER)


class OllamaClientError(OllamaError):
    def __init__(self, message: str):
        super().__init__(message, ERR_OTHER)


def _compile_error(status: int, body: Any) -> OllamaError:
    message = f"ollama http {status}: {_peek(body)}"
    if status in (401, 403):
        code = ERR_INVALID_CREDENTIALS if status == 401 else ERR_FORBIDDEN
        return OllamaAuthError(message, code)
    if status == 429:
        return OllamaRateLimitError(message)
    if status >= 500:
        return OllamaServerError(message)
    return OllamaClientError(message)


def _peek(body: Any, limit: int = 200) -> str:
    text = body if isinstance(body, str) else str(body)
    return text[:limit]


class OllamaResponse:
    def __init__(self, content: str, input_tokens: int, output_tokens: int):
        self.content = content
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class OllamaClient:
    """Thin async client for chat endpoints (Ollama native or OpenAI-compatible).

    base_url is the provider root used to build the chat endpoint; the exact
    path depends on the provider (see _resolve_chat_url).
    """

    def __init__(
        self,
        base_url: str = "https://ollama.com",
        *,
        provider: str = PROVIDER_OLLAMA,
        model: str = "gemma4:cloud",
        timeout: float = 120.0,
        max_connections: int = 10,
    ):
        self._base_url = base_url.rstrip("/")
        self._provider = provider if provider in PROVIDERS else PROVIDER_OLLAMA
        self._model = model
        self._timeout = timeout
        self._chat_url = self._resolve_chat_url(self._base_url, self._provider)
        # HTTP/2 only when h2 is installed; otherwise fall back to HTTP/1.1.
        try:
            import h2  # noqa: F401

            http2 = True
        except Exception:  # noqa: BLE001
            http2 = False
        # Reuse one connection pool; every request sends its own key.
        self._client = httpx.AsyncClient(
            http2=http2,
            timeout=httpx.Timeout(timeout, connect=30.0),
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
        )

    @staticmethod
    def _resolve_chat_url(base_url: str, provider: str) -> str:
        base = base_url.rstrip("/")
        if provider == PROVIDER_OLLAMA:
            if base.endswith("/v1") or "/api" in base:
                return f"{base}/chat/completions"
            return f"{base}/api/chat"
        return f"{base}/chat/completions"

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def chat_url(self) -> str:
        return self._chat_url

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _build_body(
        provider: str,
        messages: list[dict],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        if provider == PROVIDER_OLLAMA:
            return {
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": temperature, "num_predict": max_tokens},
            }
        return {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }

    @staticmethod
    def _parse_payload(payload: Any) -> OllamaResponse:
        """Normalise a chat payload to OllamaResponse (OpenAI & native shapes)."""
        if isinstance(payload, dict):
            message = payload.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            if content is None and isinstance(payload.get("choices"), list) and payload["choices"]:
                choice = payload["choices"][0]
                if isinstance(choice.get("message"), dict):
                    content = choice["message"].get("content")
            if content is None:
                raise OllamaClientError("chat response missing content")
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            input_tokens = int(
                payload.get("prompt_eval_count")
                or usage.get("prompt_tokens")
                or usage.get("input_tokens")
                or 0
            )
            output_tokens = int(
                payload.get("eval_count")
                or usage.get("completion_tokens")
                or usage.get("output_tokens")
                or 0
            )
            return OllamaResponse(content=content, input_tokens=input_tokens, output_tokens=output_tokens)
        raise OllamaClientError(f"unexpected chat response: {_peek(payload)}")

    async def chat(
        self,
        messages: list[dict],
        *,
        api_key: str,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 800,
        timeout: float | None = None,
    ) -> OllamaResponse:
        body = self._build_body(
            self._provider,
            messages,
            model or self._model,
            temperature,
            max_tokens,
        )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        request_timeout = timeout or self._timeout
        started = time.monotonic()
        try:
            response = await self._client.post(
                self._chat_url, json=body, headers=headers, timeout=request_timeout
            )
        except httpx.TimeoutException as exc:
            logger.warning("%s timeout after %.1fs: %s", self._provider, time.monotonic() - started, exc)
            raise OllamaTimeoutError(str(exc)) from exc
        except httpx.RequestError as exc:
            logger.warning("%s network error: %s", self._provider, exc)
            raise OllamaNetworkError(str(exc)) from exc

        try:
            payload = response.json()
        except Exception:
            payload = response.text

        if response.status_code >= 400:
            raise _compile_error(response.status_code, payload)

        return self._parse_payload(payload)