"""Benchmark infrastructure: the pieces every scenario runs on.

The model is a deterministic script (:class:`ScriptedProvider`) and the
browser never touches the desktop (:class:`SimulatedBrowserToolbox`), so a
:class:`Scenario` exercises the real production stack while every metric
measures the HARNESS, not a model.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ...providers.base import BaseProvider, ProviderResult
from ..tools import Toolbox, ToolResult


class SimulatedBrowserToolbox(Toolbox):
    """Real toolbox except open_browser, which must not touch the desktop."""

    def _open_browser(self, url: str) -> ToolResult:
        clean = url.strip()
        self._validate_url(clean)
        return ToolResult(True, "open_browser", f"Simulated browser open for {clean} (benchmark mode).", {
            "url": clean,
            "opened": True,
            "simulated": True,
        })


class ScriptedProvider(BaseProvider):
    """Deterministic stand-in for a model: a policy function drives each turn."""

    provider_name = "script"

    def __init__(self, script: Callable[[int, str, dict], dict], ctx: dict, record: dict) -> None:
        super().__init__("script")
        self.script = script
        self.ctx = ctx
        self.record = record
        self.turn = 0

    def generate(self, prompt: str, images: list[str] | None = None) -> ProviderResult:
        self.turn += 1
        self.record.setdefault("images_per_call", []).append(list(images) if images else None)
        payload = self.script(self.turn, prompt, self.ctx)
        return ProviderResult(
            text=json.dumps(payload),
            model="script",
            provider=self.provider_name,
            latency_ms=0,
            token_estimate=max(1, len(prompt) // 4),
        )


def observed(prompt: str) -> str:
    """The observations slice of the built prompt (what the model has seen)."""
    return prompt.split("Tool observations so far:")[-1]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def setup_noop(workspace: Path, ctx: dict) -> None:
    return None


@dataclass
class Scenario:
    id: str
    category: str
    prompt: str
    expected_status: str
    setup: Callable[[Path, dict], None]
    script: Callable[[int, str, dict], dict]
    grade: Callable[[dict], tuple[bool, dict]]
    cleanup: Callable[[dict], None] = field(default=lambda ctx: None)
