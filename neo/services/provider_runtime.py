from __future__ import annotations

import json
import re
from typing import Any
from urllib import error, request

from google import genai
from openai import OpenAI

from ..config import Settings
from ..db import Database


SUPPORTED_PROVIDERS = {"gemini", "claude", "openai", "local"}
PROVIDER_LABELS = {
    "gemini": "Gemini",
    "claude": "Claude",
    "openai": "OpenAI",
    "local": "Local/self-hosted",
}
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_PROVIDER_MODELS = {
    "gemini": [
        "gemini-3.5-flash",
        "gemini-3.1-pro-preview",
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    ],
    "claude": [
        "claude-fable-5",
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    ],
    "openai": [
        "gpt-5.5",
        "gpt-5.5-pro",
        "gpt-5.4",
        "gpt-5.4-pro",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4o",
    ],
    "local": ["llama3.1", "gemma3", "qwen2.5-coder"],
}
_GEMINI_BLOCKED_PREFIXES = (
    "gemini-1.",
    "gemini-2.0",
)
_GEMINI_BLOCKED_PARTS = (
    "antigravity",
    "aqa",
    "computer-use",
    "customtools",
    "deep-research",
    "embedding",
    "gemma",
    "image",
    "imagen",
    "latest",
    "live",
    "lyria",
    "nano-banana",
    "native-audio",
    "omni",
    "robotics",
    "tts",
    "veo",
)
_GEMINI_BLOCKED_EXACT = {
    "gemini-3-pro-preview",
    "gemini-3.1-flash-lite-preview",
}
_OPENAI_BLOCKED_PREFIXES = (
    "babbage",
    "davinci",
    "dall-e",
    "ft:",
    "gpt-3.5",
    "gpt-4-",
    "gpt-4-turbo",
    "gpt-5-",
    "gpt-5.1",
    "gpt-5.2",
    "gpt-5.3",
    "o1",
    "o3",
    "o4",
)
_OPENAI_BLOCKED_PARTS = (
    "audio",
    "embedding",
    "image",
    "instruct",
    "moderation",
    "realtime",
    "search",
    "sora",
    "transcribe",
    "tts",
    "whisper",
)
_OPENAI_BLOCKED_EXACT = {
    "chat-latest",
    "gpt-4",
    "gpt-4-turbo",
    "gpt-5",
}
_OPENAI_DATED_SNAPSHOT_RE = re.compile(r".*-\d{4}-\d{2}-\d{2}$")
_CLAUDE_BLOCKED_PREFIXES = (
    "claude-2",
    "claude-instant",
    "claude-v1",
)

_SCHEMA_SIGNATURE: tuple[Any, ...] | None = None
_ENV_SEEDED_SIGNATURE: tuple[Any, ...] | None = None


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output


def _split_models(raw: str | None) -> list[str]:
    return [item.strip() for item in (raw or "").replace("\n", ",").split(",") if item.strip()]


def _json_models(raw: Any) -> list[str]:
    if not raw:
        return []
    try:
        payload = json.loads(str(raw))
    except Exception:
        return _split_models(str(raw))
    if isinstance(payload, list):
        return [str(item).strip() for item in payload if str(item).strip()]
    return []


def default_provider_models(provider: str) -> list[str]:
    return list(DEFAULT_PROVIDER_MODELS.get((provider or "").strip().lower(), []))


def _canonical_model_name(model: Any) -> str:
    name = str(model or "").strip()
    if name.startswith("models/"):
        name = name.split("/", 1)[1]
    return name


def _is_blocked_model(provider: str, model: str) -> bool:
    provider = (provider or "").strip().lower()
    name = _canonical_model_name(model).lower()
    if not name:
        return True
    if provider == "gemini":
        if name in _GEMINI_BLOCKED_EXACT:
            return True
        return name.startswith(_GEMINI_BLOCKED_PREFIXES) or any(part in name for part in _GEMINI_BLOCKED_PARTS)
    if provider == "openai":
        return (
            name in _OPENAI_BLOCKED_EXACT
            or bool(_OPENAI_DATED_SNAPSHOT_RE.match(name))
            or name.startswith(_OPENAI_BLOCKED_PREFIXES)
            or any(part in name for part in _OPENAI_BLOCKED_PARTS)
        )
    if provider == "claude":
        return name.startswith(_CLAUDE_BLOCKED_PREFIXES)
    return False


def _include_recommended_models(provider: str, base_url: Any = "") -> bool:
    provider = (provider or "").strip().lower()
    if provider in {"gemini", "claude"}:
        return True
    if provider == "openai":
        return not str(base_url or "").strip()
    return False


def normalize_provider_models(provider: str, models: list[str] | tuple[str, ...], *, include_recommended: bool = False) -> list[str]:
    provider = (provider or "").strip().lower()
    cleaned = [
        _canonical_model_name(model)
        for model in (models or [])
        if not _is_blocked_model(provider, _canonical_model_name(model))
    ]
    cleaned = _dedupe([model for model in cleaned if model])
    if include_recommended:
        return _dedupe([*default_provider_models(provider), *cleaned])
    return cleaned


def api_key_preview(value: Any) -> str:
    key = str(value or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "saved key"
    return f"{key[:4]}...{key[-4:]}"


def _schema_signature(db: Database) -> tuple[Any, ...]:
    if db.driver == "sqlite":
        return (db.driver, Settings.sqlite_path)
    return (db.driver, Settings.mysql_host, Settings.mysql_port, Settings.mysql_database)


def _db() -> Database:
    global _SCHEMA_SIGNATURE
    db = Database()
    signature = _schema_signature(db)
    if _SCHEMA_SIGNATURE != signature:
        db.init_schema()
        _SCHEMA_SIGNATURE = signature
    return db


def _setting(key: str) -> str:
    row = _db().fetchone("SELECT setting_value FROM app_settings WHERE setting_key=?", (key,))
    return str(row.get("setting_value") or "") if row else ""


def _set_setting(key: str, value: Any) -> None:
    db = _db()
    payload = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    existing = db.fetchone("SELECT setting_key FROM app_settings WHERE setting_key=?", (key,))
    if existing:
        db.execute("UPDATE app_settings SET setting_value=? WHERE setting_key=?", (payload, key))
    else:
        db.execute("INSERT INTO app_settings (setting_key, setting_value) VALUES (?, ?)", (key, payload))


def _provider_row(provider: str) -> dict[str, Any]:
    row = _db().fetchone("SELECT * FROM provider_settings WHERE provider=?", (provider,))
    return row or {}


def _save_provider_row(provider: str, values: dict[str, Any]) -> None:
    db = _db()
    base_url = str(values.get("base_url") or "")
    models = normalize_provider_models(
        provider,
        values.get("models") or [],
        include_recommended=_include_recommended_models(provider, base_url),
    )
    params = (
        provider,
        values.get("api_key") or "",
        base_url,
        json.dumps(models, ensure_ascii=False),
    )
    if db.fetchone("SELECT provider FROM provider_settings WHERE provider=?", (provider,)):
        db.execute(
            "UPDATE provider_settings SET api_key=?, base_url=?, models_json=? WHERE provider=?",
            (params[1], params[2], params[3], provider),
        )
    else:
        db.execute(
            "INSERT INTO provider_settings (provider, api_key, base_url, models_json) VALUES (?, ?, ?, ?)",
            params,
        )


def _env_provider_config(provider: str) -> dict[str, Any]:
    if provider == "gemini":
        return {"api_key": Settings.gemini_api_key, "base_url": "", "models": []}
    if provider == "claude":
        return {"api_key": Settings.anthropic_api_key, "base_url": "", "models": []}
    if provider == "openai":
        return {"api_key": Settings.openai_api_key, "base_url": "", "models": []}
    if provider == "local":
        return {
            "api_key": Settings.local_api_key,
            "base_url": Settings.local_base_url,
            "models": _split_models(Settings.local_models),
        }
    return {"api_key": "", "base_url": "", "models": []}


def seed_provider_settings_from_env() -> dict[str, Any]:
    """Copy existing local env credentials into SQL once per DB target."""
    global _ENV_SEEDED_SIGNATURE
    db = _db()
    signature = _schema_signature(db)
    if _ENV_SEEDED_SIGNATURE == signature:
        return {"seeded": False, "providers": []}

    seeded: list[str] = []
    for provider in sorted(SUPPORTED_PROVIDERS):
        env_cfg = _env_provider_config(provider)
        row = _provider_row(provider)
        current = {
            "api_key": str(row.get("api_key") or ""),
            "base_url": str(row.get("base_url") or ""),
            "models": _json_models(row.get("models_json")),
        }
        next_cfg = dict(current)
        changed = False
        if not current["api_key"] and env_cfg.get("api_key"):
            next_cfg["api_key"] = str(env_cfg["api_key"])
            changed = True
        if not current["base_url"] and env_cfg.get("base_url"):
            next_cfg["base_url"] = str(env_cfg["base_url"])
            changed = True
        if not current["models"] and env_cfg.get("models"):
            next_cfg["models"] = list(env_cfg["models"])
            changed = True
        if not row or changed:
            _save_provider_row(provider, next_cfg)
            seeded.append(provider)

    if not _setting("engine"):
        _set_setting("engine", {"provider": Settings.provider, "model": Settings.model})

    _ENV_SEEDED_SIGNATURE = signature
    return {"seeded": bool(seeded), "providers": seeded}


def provider_config(provider: str) -> dict[str, Any]:
    provider = (provider or "").strip().lower()
    if provider == "mock":
        return {"models": ["mock"]}
    if provider not in SUPPORTED_PROVIDERS:
        return {"models": []}

    seed_provider_settings_from_env()
    row = _provider_row(provider)
    env_cfg = _env_provider_config(provider)
    base_url = str(row.get("base_url") or env_cfg.get("base_url") or "")
    raw_models = _json_models(row.get("models_json")) or list(env_cfg.get("models") or [])
    return {
        "api_key": str(row.get("api_key") or env_cfg.get("api_key") or ""),
        "base_url": base_url,
        "models": normalize_provider_models(
            provider,
            raw_models,
            include_recommended=_include_recommended_models(provider, base_url),
        ),
    }


def default_engine() -> dict[str, str]:
    seed_provider_settings_from_env()
    raw = _setting("engine")
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    provider = str(data.get("provider") or Settings.provider)
    model = str(data.get("model") or Settings.model)
    if provider == "mock":
        provider = "gemini"
        model = default_provider_models("gemini")[0]
    if provider in SUPPORTED_PROVIDERS and _is_blocked_model(provider, model):
        models = provider_config(provider).get("models") or default_provider_models(provider)
        model = models[0] if models else model
        _set_setting("engine", {"provider": provider, "model": model})
    return {"provider": provider, "model": model}


def save_engine(provider: str, model: str) -> dict[str, str]:
    provider = (provider or "").strip().lower()
    model = str(model or "").strip()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("Unsupported provider.")
    if not model:
        raise ValueError("Model is required.")
    _set_setting("engine", {"provider": provider, "model": model})
    return default_engine()


def save_provider_config(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    provider = (provider or "").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("Unsupported provider.")

    current = provider_config(provider)
    next_cfg = {
        "api_key": str(current.get("api_key") or ""),
        "base_url": str(current.get("base_url") or ""),
        "models": list(current.get("models") or []),
    }

    if "api_key" in payload and str(payload.get("api_key") or "").strip():
        next_cfg["api_key"] = str(payload.get("api_key") or "").strip()
    if payload.get("clear_api_key") is True:
        next_cfg["api_key"] = ""
    if "base_url" in payload:
        next_cfg["base_url"] = str(payload.get("base_url") or "").strip()
    if "models" in payload:
        models = payload.get("models")
        if isinstance(models, str):
            next_cfg["models"] = _split_models(models)
        elif isinstance(models, list):
            next_cfg["models"] = [str(item).strip() for item in models if str(item).strip()]

    _save_provider_row(provider, next_cfg)
    return public_provider_config(provider)


def public_provider_config(provider: str) -> dict[str, Any]:
    cfg = provider_config(provider)
    return {
        "provider": provider,
        "has_api_key": bool(cfg.get("api_key")),
        "api_key": "",
        "api_key_preview": api_key_preview(cfg.get("api_key")),
        "base_url": cfg.get("base_url", ""),
        "models": cfg.get("models") or [],
    }


def reveal_provider_api_key(provider: str) -> dict[str, Any]:
    provider = (provider or "").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("Unsupported provider.")
    cfg = provider_config(provider)
    key = str(cfg.get("api_key") or "")
    return {
        "provider": provider,
        "has_api_key": bool(key),
        "api_key": key,
        "api_key_preview": api_key_preview(key),
    }


def refresh_provider_models(provider: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    provider = (provider or "").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("Unsupported provider.")
    if payload:
        save_provider_config(provider, payload)
    cfg = provider_config(provider)
    if provider == "gemini":
        models = _fetch_gemini_models(str(cfg.get("api_key") or ""))
    elif provider == "claude":
        models = _fetch_anthropic_models(str(cfg.get("api_key") or ""), str(cfg.get("base_url") or ""))
    else:
        models = _fetch_openai_compatible_models(
            str(cfg.get("api_key") or ("local" if provider == "local" else "")),
            str(cfg.get("base_url") or ""),
        )
    models = normalize_provider_models(
        provider,
        models,
        include_recommended=_include_recommended_models(provider, cfg.get("base_url") or ""),
    )
    save_provider_config(provider, {"models": models})
    return {"provider": provider, "models": models, "count": len(models), "source": "provider"}


def _fetch_gemini_models(api_key: str) -> list[str]:
    if not api_key:
        raise ValueError("Gemini API key is required.")
    client = genai.Client(api_key=api_key)
    models = []
    for model in client.models.list():
        name = str(getattr(model, "name", "") or "")
        if name.startswith("models/"):
            name = name.split("/", 1)[1]
        actions = [str(item).lower() for item in (getattr(model, "supported_actions", None) or [])]
        if actions and not any("generate" in item for item in actions):
            continue
        if name:
            models.append(name)
    return sorted(set(models))


def _fetch_openai_compatible_models(api_key: str, base_url: str) -> list[str]:
    if not api_key:
        raise ValueError("API key is required.")
    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url.rstrip("/")
    client = OpenAI(**kwargs)
    response = client.models.list()
    models = [str(item.id) for item in response.data if getattr(item, "id", None)]
    return sorted(set(models))


def _anthropic_url(base_url: str, path: str) -> str:
    return f"{(base_url or ANTHROPIC_API_BASE).rstrip('/')}/{path.lstrip('/')}"


def _fetch_anthropic_models(api_key: str, base_url: str = "") -> list[str]:
    if not api_key:
        raise ValueError("Claude API key is required.")
    req = request.Request(
        _anthropic_url(base_url, "models"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "accept": "application/json",
        },
        method="GET",
    )
    try:
        with request.urlopen(req, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(detail or str(exc)) from exc
    models = [str(item.get("id") or "").strip() for item in payload.get("data", []) if isinstance(item, dict)]
    return sorted({item for item in models if item})
