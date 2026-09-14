"""Provider-aware client tests (pure - no network).

Covers chat URL routing, request body shaping and response normalisation for
Ollama native / OpenAI / Gemini / Claude (OpenAI-compatible).
"""

import pytest

from app.kela_ai.ollama_client import (
    PROVIDER_CLAUDE,
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
    PROVIDER_OLLAMA,
    OllamaClient,
    OllamaClientError,
    OllamaResponse,
)


def test_resolve_chat_url_ollama_native():
    assert OllamaClient._resolve_chat_url("https://ollama.com", PROVIDER_OLLAMA) == (
        "https://ollama.com/api/chat"
    )
    assert OllamaClient._resolve_chat_url("http://localhost:11434", PROVIDER_OLLAMA) == (
        "http://localhost:11434/api/chat"
    )
    assert OllamaClient._resolve_chat_url("https://ollama.com/v1", PROVIDER_OLLAMA) == (
        "https://ollama.com/v1/chat/completions"
    )


def test_resolve_chat_url_openai_compatible():
    assert OllamaClient._resolve_chat_url("https://api.openai.com/v1", PROVIDER_OPENAI) == (
        "https://api.openai.com/v1/chat/completions"
    )
    assert OllamaClient._resolve_chat_url(
        "https://generativelanguage.googleapis.com/v1beta/openai", PROVIDER_GEMINI
    ) == ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
    assert OllamaClient._resolve_chat_url("https://api.anthropic.com/v1", PROVIDER_CLAUDE) == (
        "https://api.anthropic.com/v1/chat/completions"
    )


def test_build_body_ollama_uses_options():
    body = OllamaClient._build_body(PROVIDER_OLLAMA, [{"role": "user", "content": "x"}], "m", 0.6, 500)
    assert body == {
        "model": "m",
        "messages": [{"role": "user", "content": "x"}],
        "stream": False,
        "options": {"temperature": 0.6, "num_predict": 500},
    }


def test_build_body_openai_compatible_uses_top_level():
    for provider in (PROVIDER_OPENAI, PROVIDER_GEMINI, PROVIDER_CLAUDE):
        body = OllamaClient._build_body(provider, [{"role": "user", "content": "x"}], "m", 0.6, 500)
        assert body["model"] == "m"
        assert body["temperature"] == 0.6
        assert body["max_tokens"] == 500
        assert body["stream"] is False
        assert "options" not in body


def test_parse_ollama_native_payload():
    payload = {
        "model": "gemma4:cloud",
        "message": {"content": "halo"},
        "prompt_eval_count": 12,
        "eval_count": 34,
    }
    result = OllamaClient._parse_payload(payload)
    assert isinstance(result, OllamaResponse)
    assert result.content == "halo"
    assert result.input_tokens == 12
    assert result.output_tokens == 34


def test_parse_openai_payload():
    payload = {
        "choices": [{"message": {"content": "hi from gpt"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 4},
    }
    result = OllamaClient._parse_payload(payload)
    assert result.content == "hi from gpt"
    assert result.input_tokens == 7
    assert result.output_tokens == 4


def test_parse_gemini_usage_tokens():
    payload = {
        "choices": [{"message": {"content": "gemini says hi"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13},
    }
    result = OllamaClient._parse_payload(payload)
    assert result.input_tokens == 5
    assert result.output_tokens == 8


def test_parse_missing_content_raises():
    with pytest.raises(OllamaClientError):
        OllamaClient._parse_payload({"choices": []})