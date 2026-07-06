from __future__ import annotations

import time

from openai import OpenAI

from ..config import Settings
from ..services.provider_runtime import provider_config
from .base import BaseProvider, ProviderResult


class OpenAIProvider(BaseProvider):
    provider_name = "openai"
    supports_structured = True

    def __init__(self, model: str) -> None:
        super().__init__(model or "gpt-5.5")
        cfg = provider_config("openai")
        api_key = str(cfg.get("api_key") or "")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        kwargs = {"api_key": api_key}
        self.base_url = str(cfg.get("base_url") or "").strip()
        if cfg.get("base_url"):
            kwargs["base_url"] = self.base_url.rstrip("/")
        self.client = OpenAI(**kwargs)

    def generate(self, prompt: str, images: list[str] | None = None, structured: bool = False) -> ProviderResult:
        start = time.perf_counter()
        loaded = self.load_images(images)
        if self._uses_responses_api():
            text, total_tokens = self._generate_responses(prompt, loaded, structured)
            return ProviderResult(
                text=text,
                model=self.model,
                provider=self.provider_name,
                latency_ms=int((time.perf_counter() - start) * 1000),
                token_estimate=total_tokens or max(1, len(prompt.split()) + len(text.split())),
            )

        content: str | list = prompt
        if loaded:
            content = [
                *(
                    {"type": "image_url", "image_url": {"url": f"data:{item['media_type']};base64,{item['data_b64']}"}}
                    for item in loaded
                ),
                {"type": "text", "text": prompt},
            ]
        create_kwargs: dict = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.1,
        }
        if structured:
            # Native JSON mode: guarantees the reply is a valid JSON object.
            # (The prompt already contains "JSON", which this mode requires.)
            create_kwargs["response_format"] = {"type": "json_object"}
        response = self.client.chat.completions.create(**create_kwargs)
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

    def _uses_responses_api(self) -> bool:
        return not self.base_url and self.model.startswith("gpt-5")

    def _generate_responses(self, prompt: str, loaded_images: list[dict], structured: bool = False) -> tuple[str, int]:
        content: list[dict] = [{"type": "input_text", "text": prompt}]
        content.extend(
            {
                "type": "input_image",
                "image_url": f"data:{item['media_type']};base64,{item['data_b64']}",
            }
            for item in loaded_images
        )
        create_kwargs: dict = {
            "model": self.model,
            "input": [{"role": "user", "content": content}],
            "max_output_tokens": 8192,
            "reasoning": {"effort": "medium"},
            "store": False,
        }
        if structured:
            create_kwargs["text"] = {"format": {"type": "json_object"}}
        response = self.client.responses.create(**create_kwargs)
        text = self._responses_text(response)
        usage = getattr(response, "usage", None)
        total_tokens = int(
            getattr(usage, "total_tokens", 0)
            or (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0)
            or 0
        )
        return text, total_tokens

    def _responses_text(self, response) -> str:
        direct = getattr(response, "output_text", None)
        if direct:
            return str(direct)
        parts: list[str] = []
        for item in getattr(response, "output", None) or []:
            blocks = getattr(item, "content", None)
            if blocks is None and isinstance(item, dict):
                blocks = item.get("content")
            for block in blocks or []:
                text = getattr(block, "text", None)
                if text is None and isinstance(block, dict):
                    text = block.get("text")
                if text:
                    parts.append(str(text))
        return "\n".join(parts).strip()
