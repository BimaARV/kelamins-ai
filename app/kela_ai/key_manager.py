"""Ollama API key management (spec section 12).

Keys enter the process exclusively from the environment (.env / Docker secrets
/ Secret Manager). They are held only in memory; MariaDB stores key_name, never
the value itself.

Failover rules per spec:
  401/403 -> DISABLED
  429 / 5xx / timeout / network -> COOLDOWN (temporary)
  success  -> reset to ACTIVE, update statistics
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import Counter

from app.config import settings
from app.kela_ai.ollama_client import (
    ERR_FORBIDDEN,
    ERR_INVALID_CREDENTIALS,
    ERR_NETWORK,
    ERR_RATE_LIMIT,
    ERR_SERVER,
    ERR_TIMEOUT,
)

logger = logging.getLogger(__name__)

STATE_ACTIVE = "ACTIVE"
STATE_COOLDOWN = "COOLDOWN"
STATE_DISABLED = "DISABLED"

# Provider -> env key prefix. LLM_API_KEY_* is a shared pool usable by any model.
KEY_PREFIXES = {
    "ollama": "OLLAMA_API_KEY_",
    "openai": "OPENAI_API_KEY_",
    "gemini": "GEMINI_API_KEY_",
    "claude": "ANTHROPIC_API_KEY_",
}
SHARED_KEY_PREFIX = "LLM_API_KEY_"
_VALID_PROVIDERS = {*KEY_PREFIXES, "llm"}

_key_prefix_pattern = re.compile(
    r"^(OLLAMA_API_KEY_|LLM_API_KEY_|OPENAI_API_KEY_|GEMINI_API_KEY_|ANTHROPIC_API_KEY_)(\d+)$"
)

_COOLDOWN_ERRORS = {ERR_RATE_LIMIT, ERR_SERVER, ERR_TIMEOUT, ERR_NETWORK, ERR_FORBIDDEN}
_DISABLE_ERRORS = {ERR_INVALID_CREDENTIALS}

_COUNTER_KEYS = (
    "total_requests",
    "successful_requests",
    "rate_limits",
    "timeouts",
    "network_errors",
    "server_errors",
    "invalid_credentials",
    "other_errors",
    "total_latency_ms",
)


class OllamaKeyManager:
    def __init__(
        self,
        *,
        keys: list[dict] | None = None,
        cooldown_seconds: int | None = None,
    ):
        # keys: [{"name": "OLLAMA_API_KEY_1", "value": "..."}]
        self._keys: list[tuple[str, str]] = []
        for entry in keys if keys is not None else self._keys_from_settings():
            if entry.get("value"):
                self._keys.append((entry["name"], entry["value"]))
        self._names = [name for name, _ in self._keys]
        self._value = {name: value for name, value in self._keys}
        self._state = {name: STATE_ACTIVE for name in self._names}
        self._cooldown_until = {name: 0.0 for name in self._names}
        self._stats = {name: Counter() for name in self._names}
        self._cooldown_seconds = cooldown_seconds or settings.ai_key_cooldown_seconds
        if not self._names:
            logger.warning(
                "no OLLAMA_API_KEY_x configured - AI worker will skip (graceful degradation)"
            )

    @staticmethod
    def _env_keys() -> list[tuple[str, str]]:
        """All *_API_KEY_<N> entries from the environment (numeric order, dedup).

        Scans every provider prefix plus the shared LLM_* pool; empty values are
        skipped; identical values seen twice keep the first name.
        """
        found: list[tuple[int, str, str]] = []
        for name, value in os.environ.items():
            match = _key_prefix_pattern.match(name)
            if not match or not value:
                continue
            found.append((int(match.group(2)), name, value))
        found.sort(key=lambda item: item[0])
        dedup: dict[str, str] = {}
        for _, name, value in found:
            dedup.setdefault(value, name)
        return [(name, value) for value, name in dedup.items()]

    @staticmethod
    def keys_for_provider(provider: str) -> list[dict]:
        """Keys available to a provider model: its own prefix + the shared LLM pool."""
        prefix = KEY_PREFIXES.get(provider or "ollama", KEY_PREFIXES["ollama"])
        return [
            {"name": name, "value": value}
            for name, value in OllamaKeyManager._env_keys()
            if name.startswith(prefix) or name.startswith(SHARED_KEY_PREFIX)
        ]

    @staticmethod
    def _keys_from_settings() -> list[dict]:
        return [
            {"name": name, "value": value}
            for name, value in OllamaKeyManager._env_keys()
        ]

    @property
    def configured(self) -> bool:
        return bool(self._names)

    @property
    def names(self) -> list[str]:
        return list(self._names)

    def key_value(self, name: str) -> str | None:
        return self._value.get(name)

    def state_of(self, name: str) -> str:
        if name not in self._state:
            return STATE_DISABLED
        if self._state[name] == STATE_COOLDOWN:
            if time.monotonic() >= self._cooldown_until[name]:
                self._state[name] = STATE_ACTIVE
        return self._state[name]

    def available_key(self) -> str | None:
        """First ACTIVE key, or None when the pool is exhausted."""
        for name in self._names:
            if self.state_of(name) == STATE_ACTIVE:
                return name
        return None

    def mark_success(self, name: str, latency_ms: int = 0) -> None:
        if name not in self._stats:
            return
        self._state[name] = STATE_ACTIVE
        self._cooldown_until[name] = 0.0
        self._stats[name]["total_requests"] += 1
        self._stats[name]["successful_requests"] += 1
        self._stats[name]["total_latency_ms"] += latency_ms

    def mark_failure(self, name: str, error_code: str, latency_ms: int = 0) -> None:
        if name not in self._stats:
            return
        self._stats[name]["total_requests"] += 1
        self._stats[name]["total_latency_ms"] += latency_ms
        if error_code == ERR_RATE_LIMIT:
            self._stats[name]["rate_limits"] += 1
        elif error_code == ERR_TIMEOUT:
            self._stats[name]["timeouts"] += 1
        elif error_code == ERR_NETWORK:
            self._stats[name]["network_errors"] += 1
        elif error_code == ERR_SERVER:
            self._stats[name]["server_errors"] += 1
        elif error_code == ERR_INVALID_CREDENTIALS or error_code == ERR_FORBIDDEN:
            self._stats[name]["invalid_credentials"] += 1
        else:
            self._stats[name]["other_errors"] += 1

        if error_code in _DISABLE_ERRORS:
            self._state[name] = STATE_DISABLED
            logger.warning("ollama key %s DISABLED (%s)", name, error_code)
        elif error_code in _COOLDOWN_ERRORS:
            self._state[name] = STATE_COOLDOWN
            self._cooldown_until[name] = time.monotonic() + self._cooldown_seconds
            logger.warning("ollama key %s COOLDOWN %.0fs (%s)", name, self._cooldown_seconds, error_code)

    def stats(self) -> dict:
        breakdown = {}
        for name in self._names:
            breakdown[name] = {
                "state": self.state_of(name),
                "cooldown_remaining_s": max(
                    0.0, self._cooldown_until[name] - time.monotonic()
                )
                if self._state[name] == STATE_COOLDOWN
                else 0.0,
                **{k: self._stats[name].get(k, 0) for k in _COUNTER_KEYS},
            }
        return {
            "keys": breakdown,
            "configured": self.configured,
            "active": len([n for n in self._names if self.state_of(n) == STATE_ACTIVE]),
        }