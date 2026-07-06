from __future__ import annotations

import json
import time
from urllib import error, request

from ..services.provider_runtime import ANTHROPIC_API_BASE, ANTHROPIC_VERSION, provider_config
from .base import BaseProvider, ProviderResult


class ClaudeProvider(BaseProvider):
    provider_name = "claude"
    # NOT structured: assistant-turn prefill returns HTTP 400 on all current
    # Claude models, and output_config.format needs a strict json_schema that
    # cannot express the contract's open tool_calls[].args object. Claude
    # follows the JSON contract reliably on the plain path anyway.
    supports_structured = False

    def __init__(self, model: str) -> None:
        cfg = provider_config("claude")
        models = list(cfg.get("models") or [])
        super().__init__(model or (models[0] if models else "claude-sonnet-5"))
        self.api_key = str(cfg.get("api_key") or "")
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")
        self.base_url = str(cfg.get("base_url") or ANTHROPIC_API_BASE).rstrip("/")

    def generate(self, prompt: str, images: list[str] | None = None, structured: bool = False) -> ProviderResult:
        start = time.perf_counter()
        content: str | list = prompt
        loaded = self.load_images(images)
        if loaded:
            content = [
                *(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": item["media_type"], "data": item["data_b64"]},
                    }
                    for item in loaded
                ),
                {"type": "text", "text": prompt},
            ]
        payload = {
            "model": self.model,
            "max_tokens": 8192,
            # No temperature: current Claude models (Opus 4.8, Sonnet 5) reject it
            # ("temperature is deprecated for this model"); the default is fine.
            "messages": [{"role": "user", "content": content}],
        }
        req = request.Request(
            f"{self.base_url}/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "accept": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=90) as response:
                data = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(detail or str(exc)) from exc

        text = "\n".join(
            str(block.get("text") or "")
            for block in data.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        usage = data.get("usage") or {}
        total_tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        return ProviderResult(
            text=text,
            model=self.model,
            provider=self.provider_name,
            latency_ms=int((time.perf_counter() - start) * 1000),
            token_estimate=total_tokens or max(1, len(prompt.split()) + len(text.split())),
        )
