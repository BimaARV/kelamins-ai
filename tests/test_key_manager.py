"""OllamaKeyManager tests (spec section 12): states, stats, failover rules."""

import asyncio
import os
import time

import pytest

from app.kela_ai.key_manager import (
    STATE_ACTIVE,
    STATE_COOLDOWN,
    STATE_DISABLED,
    OllamaKeyManager,
)
from app.kela_ai.ollama_client import (
    ERR_INVALID_CREDENTIALS,
    ERR_NETWORK,
    ERR_RATE_LIMIT,
    ERR_SERVER,
    ERR_TIMEOUT,
)


def _clean_key_env(monkeypatch):
    for var in list(os.environ):
        if "_API_KEY_" in var and var.split("_")[-1].isdigit():
            monkeypatch.delenv(var, raising=False)


def test_configured_false_without_keys():
    manager = OllamaKeyManager(keys=[], cooldown_seconds=1)
    assert manager.configured is False
    assert manager.available_key() is None


def test_configured_true_with_key_and_available():
    manager = OllamaKeyManager(
        keys=[{"name": "OLLAMA_API_KEY_1", "value": "secret-one"}],
        cooldown_seconds=1,
    )
    assert manager.configured is True
    assert manager.available_key() == "OLLAMA_API_KEY_1"
    assert manager.key_value("OLLAMA_API_KEY_1") == "secret-one"


def test_401_disables_key():
    manager = OllamaKeyManager(
        keys=[
            {"name": "K1", "value": "a"},
            {"name": "K2", "value": "b"},
        ],
        cooldown_seconds=60,
    )
    manager.mark_failure("K1", ERR_INVALID_CREDENTIALS)
    assert manager.state_of("K1") == STATE_DISABLED
    assert manager.available_key() == "K2"


def test_429_puts_key_on_cooldown_then_recovers():
    manager = OllamaKeyManager(
        keys=[{"name": "K1", "value": "a"}],
        cooldown_seconds=0.05,
    )
    manager.mark_failure("K1", ERR_RATE_LIMIT)
    assert manager.state_of("K1") == STATE_COOLDOWN
    assert manager.available_key() is None
    time.sleep(0.07)
    assert manager.available_key() == "K1"


def test_success_resets_cooldown_and_tracks_stats():
    manager = OllamaKeyManager(
        keys=[{"name": "K1", "value": "a"}],
        cooldown_seconds=600,
    )
    manager.mark_failure("K1", ERR_NETWORK)
    assert manager.state_of("K1") == STATE_COOLDOWN
    manager.mark_success("K1", latency_ms=123)
    assert manager.state_of("K1") == STATE_ACTIVE
    stats = manager.stats()["keys"]["K1"]
    assert stats["successful_requests"] == 1
    assert stats["total_latency_ms"] == 123


def test_stats_breakdown_counts_error_families():
    manager = OllamaKeyManager(
        keys=[{"name": "K1", "value": "a"}],
        cooldown_seconds=60,
    )
    manager.mark_failure("K1", ERR_RATE_LIMIT)
    manager.mark_failure("K1", ERR_TIMEOUT)
    manager.mark_failure("K1", ERR_NETWORK)
    manager.mark_failure("K1", ERR_SERVER)
    manager.mark_failure("K1", ERR_INVALID_CREDENTIALS)
    manager.mark_failure("K1", "OTHER")
    manager.mark_failure("K1", "JUNK")
    stats = manager.stats()["keys"]["K1"]
    assert stats["rate_limits"] == 1
    assert stats["timeouts"] == 1
    assert stats["network_errors"] == 1
    assert stats["server_errors"] == 1
    assert stats["invalid_credentials"] == 1
    assert stats["other_errors"] == 2
    assert stats["total_requests"] == 7


def test_env_scan_numeric_sort_skip_empty_dedup(monkeypatch):
    _clean_key_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_API_KEY_1", "one")
    monkeypatch.setenv("GEMINI_API_KEY_2", "gem")
    monkeypatch.setenv("OLLAMA_API_KEY_3", "three")
    monkeypatch.setenv("ANTHROPIC_API_KEY_4", "blue")
    monkeypatch.setenv("OPENAI_API_KEY_5", "gpt")
    monkeypatch.setenv("LLM_API_KEY_10", "shared")
    monkeypatch.setenv("GEMINI_API_KEY_9", "gem")  # duplicate value -> dropped
    monkeypatch.setenv("OLLAMA_API_KEY_", "no-digits")
    monkeypatch.setenv("OLLAMA_API_KEY_EMPTY", "")

    env_keys = OllamaKeyManager._env_keys()
    names = [name for name, _ in env_keys]
    assert names == [
        "OLLAMA_API_KEY_1",
        "GEMINI_API_KEY_2",
        "OLLAMA_API_KEY_3",
        "ANTHROPIC_API_KEY_4",
        "OPENAI_API_KEY_5",
        "LLM_API_KEY_10",
    ]
    assert dict(env_keys)["GEMINI_API_KEY_2"] == "gem"


def test_keys_for_provider_filters_own_prefix_plus_shared(monkeypatch):
    _clean_key_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_API_KEY_1", "o1")
    monkeypatch.setenv("GEMINI_API_KEY_1", "g1")
    monkeypatch.setenv("OPENAI_API_KEY_1", "p1")
    monkeypatch.setenv("GEMINI_API_KEY_3", "g3")
    monkeypatch.setenv("LLM_API_KEY_1", "shared")

    ollama = [k["name"] for k in OllamaKeyManager.keys_for_provider("ollama")]
    gemini = [k["name"] for k in OllamaKeyManager.keys_for_provider("gemini")]
    assert ollama == ["OLLAMA_API_KEY_1", "LLM_API_KEY_1"]
    assert gemini == ["GEMINI_API_KEY_1", "LLM_API_KEY_1", "GEMINI_API_KEY_3"]


def test_manager_uses_env_scan_when_keys_not_given(monkeypatch):
    _clean_key_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_API_KEY_2", "alpha")
    monkeypatch.setenv("LLM_API_KEY_1", "beta")
    manager = OllamaKeyManager(cooldown_seconds=1)
    assert manager.names == ["LLM_API_KEY_1", "OLLAMA_API_KEY_2"]
    assert manager.key_value("OLLAMA_API_KEY_2") == "alpha"