from __future__ import annotations

import time

from openai import OpenAI

from ..config import Settings
from ..services.provider_runtime import provider_config
from .base import BaseProvider, ProviderResult


class LocalProvider(BaseProvider):
    provider_name = "local"
    supports_structured = True

    def __init__(self, model: str) -> None:
        cfg = provider_config("local")
        local_models = [str(item).strip() for item in (cfg.get("models") or []) if str(item).strip()]
        super().__init__(model or (local_models[0] if local_models else "llama3.1"))
        base_url = str(cfg.get("base_url") or "")
        if not base_url:
            raise RuntimeError("NEO_LOCAL_BASE_URL is not configured")
        self.client = OpenAI(
            api_key=str(cfg.get("api_key") or "local"),
            base_url=base_url.rstrip("/"),
        )

    @staticmethod
    def _is_param_rejection(exc: Exception) -> bool:
        # A server that does not understand response_format returns a 400/422
        # or complains about the field by name. Anything else is treated as a
        # real error to be retried, not silently downgraded.
        status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
        if status in {400, 404, 422, 501}:
            return True
        text = f"{type(exc).__name__}: {exc}".lower()
        return any(term in text for term in ["response_format", "json_object", "unsupported", "not supported", "unknown", "unexpected", "invalid"])

    def generate(self, prompt: str, images: list[str] | None = None, structured: bool = False) -> ProviderResult:
        # Local models frequently lack vision support; images are accepted but not sent.
        start = time.perf_counter()
        messages = [{"role": "user", "content": prompt}]
        response = None
        if structured:
            # Ollama and most OpenAI-compatible servers honor json_object, which
            # is exactly where small local models need the most parse help. Some
            # servers reject the PARAMETER; fall back to a plain call only for
            # that case. Transient errors (timeouts, 5xx, connection) propagate
            # so the runner's retry/backoff handles them instead of double-billing.
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.1,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:
                if not self._is_param_rejection(exc):
                    raise
                response = None
        if response is None:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.1,
            )
        text = response.choices[0].message.content or ""
        usage = getattr(response, "usage", None)
        total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
        return ProviderResult(
            text=text,
            model=self.model,
            provider=self.provider_name,
            latency_ms=int((time.perf_counter() - start) * 1000),
            token_estimate=total_tokens or max(1, len(prompt.split()) + len(text.split())),
        )
