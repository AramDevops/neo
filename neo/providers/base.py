from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProviderResult:
    text: str
    model: str
    provider: str
    latency_ms: int
    token_estimate: int = 0


class BaseProvider:
    provider_name = "base"
    # True when generate() honors structured=True by forcing valid-JSON output
    # through the provider's native mechanism. The runner only passes
    # structured=True to providers that advertise it, so mock/scripted/other
    # providers keep the original single-arg call unchanged.
    supports_structured = False

    def __init__(self, model: str) -> None:
        self.model = model

    def generate(self, prompt: str, images: list[str] | None = None, structured: bool = False) -> ProviderResult:
        """Generate a response. images is an optional list of local PNG/JPEG paths
        that vision-capable providers attach to the request. structured=True asks
        the provider to force valid-JSON output matching the Neo contract."""
        raise NotImplementedError

    @staticmethod
    def load_images(images: list[str] | None, limit: int = 2) -> list[dict]:
        """Read image files into {media_type, data_b64} dicts, newest last, bounded."""
        loaded: list[dict] = []
        for raw_path in (images or [])[-limit:]:
            path = Path(raw_path)
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            media_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".gif": "image/gif",
            }.get(suffix)
            if not media_type:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if len(data) > 8_000_000:
                continue
            loaded.append({"media_type": media_type, "data_b64": base64.b64encode(data).decode("ascii")})
        return loaded
