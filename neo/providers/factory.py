from __future__ import annotations

from ..config import Settings
from .base import BaseProvider
from .claude import ClaudeProvider
from .gemini import GeminiProvider
from .local_provider import LocalProvider
from .mock import MockProvider
from .openai_provider import OpenAIProvider


def get_provider(provider: str | None = None, model: str | None = None) -> BaseProvider:
    selected = (provider or Settings.provider or "gemini").lower()
    selected_model = model or Settings.model
    if selected == "gemini":
        return GeminiProvider(selected_model)
    if selected in {"claude", "anthropic"}:
        return ClaudeProvider(selected_model)
    if selected == "openai":
        return OpenAIProvider(selected_model)
    if selected in {"local", "self_hosted", "self-hosted"}:
        return LocalProvider(selected_model)
    if selected == "mock":
        return MockProvider(selected_model or "mock")
    raise ValueError(f"Unknown provider: {selected}")
