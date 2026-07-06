from __future__ import annotations

import time

from google import genai
from google.genai import types

from ..config import Settings
from ..services.provider_runtime import provider_config
from .base import BaseProvider, ProviderResult


class GeminiProvider(BaseProvider):
    provider_name = "gemini"
    supports_structured = True

    def __init__(self, model: str) -> None:
        super().__init__(model or Settings.model)
        cfg = provider_config("gemini")
        api_key = str(cfg.get("api_key") or "")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        self.client = genai.Client(api_key=api_key)

    def generate(self, prompt: str, images: list[str] | None = None, structured: bool = False) -> ProviderResult:
        start = time.perf_counter()
        contents: list = []
        for image in self.load_images(images):
            contents.append(types.Part.from_bytes(
                data=__import__("base64").b64decode(image["data_b64"]),
                mime_type=image["media_type"],
            ))
        contents.append(prompt)
        config_kwargs: dict = {"temperature": 0.1, "top_p": 0.9, "max_output_tokens": 8192}
        if structured:
            # Native JSON output: Gemini guarantees a parseable JSON body. The
            # contract's tool_calls[].args is an open object, so we force JSON
            # via mime type rather than a rigid response_schema.
            config_kwargs["response_mime_type"] = "application/json"
        response = self.client.models.generate_content(
            model=self.model,
            contents=contents if len(contents) > 1 else prompt,
            config=types.GenerateContentConfig(**config_kwargs),
        )
        # response.text can be None or raise on a blocked/empty candidate
        # (safety stop, MAX_TOKENS with no text part). Forcing JSON output
        # widens that window, so read it defensively.
        try:
            text = response.text or ""
        except Exception:
            text = ""
        return ProviderResult(
            text=text,
            model=self.model,
            provider=self.provider_name,
            latency_ms=int((time.perf_counter() - start) * 1000),
            token_estimate=max(1, len(prompt.split()) + len(text.split())),
        )
