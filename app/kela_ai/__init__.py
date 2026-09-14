"""KELA AI Gateway (Phase 3).

All AI traffic must pass through the gateway (app.kela_ai.gateway) - services
never call Ollama directly. The AI layer is exclusively an interpreter of facts
that were collected and stored raw first; it never originates monitoring data.
"""

from app.kela_ai.gateway import AIUnavailable, AIResult, KELAAIGateway
from app.kela_ai.key_manager import OllamaKeyManager
from app.kela_ai.ollama_client import OllamaClient

__all__ = [
    "KELAAIGateway",
    "AIResult",
    "AIUnavailable",
    "OllamaKeyManager",
    "OllamaClient",
]