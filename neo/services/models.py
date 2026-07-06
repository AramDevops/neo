from __future__ import annotations

from .provider_runtime import api_key_preview, default_engine, default_provider_models, provider_config


def _provider_item(provider: str, label: str, notes: str, custom_model: bool = True) -> dict:
    cfg = provider_config(provider)
    models = cfg.get("models") or default_provider_models(provider)
    status = "ready"
    if provider in {"gemini", "claude", "openai"} and not cfg.get("api_key"):
        status = "missing_key"
    if provider == "local" and not cfg.get("base_url"):
        status = "not_configured"
    return {
        "id": provider,
        "label": label,
        "status": status,
        "default_model": models[0] if models else "",
        "models": models,
        "custom_model": custom_model,
        "base_url": cfg.get("base_url", ""),
        "has_api_key": bool(cfg.get("api_key")),
        "api_key": "",
        "api_key_preview": api_key_preview(cfg.get("api_key")),
        "notes": notes,
    }


def model_catalog() -> dict:
    engine = default_engine()
    return {
        "default_provider": engine["provider"],
        "default_model": engine["model"],
        "providers": [
            _provider_item("gemini", "Gemini", "Google Gemini API provider."),
            _provider_item("claude", "Claude", "Anthropic Claude API provider."),
            _provider_item("openai", "OpenAI", "OpenAI API or compatible hosted endpoint."),
            _provider_item("local", "Local/self-hosted", "OpenAI-compatible local endpoint for Ollama, LM Studio, vLLM, LocalAI, or similar servers."),
        ],
    }
