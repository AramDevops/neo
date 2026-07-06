"""Provider I/O for the agent loop: capability-gated calls, retry/backoff on
transient failures, and tolerant parsing of the JSON response contract."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Dict, List

PROVIDER_RETRY_MAX = 5


class ModelClient:
    """Wraps every provider.generate call the runner makes.

    `event` records retry telemetry on the run; `check_stop` raises
    RunStopRequested so a user stop can land during backoff sleeps."""

    def __init__(
        self,
        event: Callable[[int, str, Dict[str, Any]], None],
        check_stop: Callable[[int], None],
    ) -> None:
        self._event = event
        self._check_stop = check_stop

    def call(self, provider: Any, prompt: str, images: List[str] | None = None) -> Any:
        """Call a provider, asking for native structured JSON output when it
        advertises support. Providers WITHOUT support (mock, scripted, custom)
        are called with the original signature, so nothing existing breaks."""
        kwargs: Dict[str, Any] = {}
        if images:
            kwargs["images"] = images
        if getattr(provider, "supports_structured", False):
            kwargs["structured"] = True
        return provider.generate(prompt, **kwargs) if kwargs else provider.generate(prompt)

    def generate_with_retries(
        self,
        provider: Any,
        prompt: str,
        run_id: int,
        loop: int,
        images: List[str] | None = None,
    ) -> Any:
        last_error: Exception | None = None
        for retry_index in range(PROVIDER_RETRY_MAX + 1):
            # A stop can arrive during the exponential-backoff sleeps; check
            # before every attempt so a stopped run never keeps retrying.
            self._check_stop(run_id)
            try:
                return self.call(provider, prompt, images)
            except Exception as exc:
                last_error = exc
                if retry_index >= PROVIDER_RETRY_MAX or not self.retryable_provider_error(exc):
                    if retry_index > 0:
                        self._event(run_id, "provider_retry_failed", {
                            "loop": loop,
                            "attempt": retry_index,
                            "max": PROVIDER_RETRY_MAX,
                            "error": self.short_error(exc),
                        })
                    raise
                retry_attempt = retry_index + 1
                delay_seconds = min(0.6 * (2 ** retry_index), 5.0)
                self._event(run_id, "provider_retry", {
                    "loop": loop,
                    "attempt": retry_attempt,
                    "max": PROVIDER_RETRY_MAX,
                    "delay_ms": int(delay_seconds * 1000),
                    "error": self.short_error(exc),
                })
                time.sleep(delay_seconds)
        raise last_error or RuntimeError("Provider request failed")

    @staticmethod
    def retryable_provider_error(exc: Exception) -> bool:
        status = (
            getattr(exc, "status_code", None)
            or getattr(exc, "status", None)
            or getattr(getattr(exc, "response", None), "status_code", None)
        )
        try:
            status_code = int(status) if status is not None else 0
        except Exception:
            status_code = 0
        if status_code:
            return status_code in {408, 425, 429, 500, 502, 503, 504}

        text = f"{type(exc).__name__}: {exc}".lower()
        if any(term in text for term in ["api_key", "api key", "not configured", "invalid model", "bad request", "unauthorized", "forbidden"]):
            return False
        return any(term in text for term in [
            "timeout", "timed out", "temporarily", "temporary", "connection", "network",
            "reset", "aborted", "unavailable", "overloaded", "rate limit", "too many requests",
            "502", "503", "504", "429",
        ])

    @staticmethod
    def short_error(exc: Exception) -> str:
        return " ".join(str(exc or type(exc).__name__).split())[:500]

    @classmethod
    def parse_model_json(cls, text: str) -> Dict[str, Any]:
        cleaned = text.strip()
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.S)
        if fence:
            cleaned = fence.group(1)
        parsed = cls.loads_json_object(cleaned)
        if parsed is None and not cleaned.startswith("{"):
            brace = cleaned.find("{")
            if brace >= 0:
                parsed = cls.loads_json_object(cleaned[brace:])
        if parsed is not None:
            parsed.setdefault("plan", [])
            parsed.setdefault("tool_calls", [])
            parsed.setdefault("final", text)
            parsed.setdefault("needs_more", False)
            return parsed
        # Parse failure: no ugly "Parse fallback" plan (it leaked into the
        # UI). Flag it so the loop can re-prompt for valid JSON instead of
        # accepting prose and stopping.
        return {"plan": [], "tool_calls": [], "final": text, "needs_more": False, "_parse_failed": True}

    @staticmethod
    def loads_json_object(text: str) -> Dict[str, Any] | None:
        """Tolerant object parse: raw_decode reads a complete JSON object and
        ignores trailing prose, the most common small-model contract slip
        ('{...} Hope this helps!')."""
        try:
            parsed, _ = json.JSONDecoder().raw_decode(text)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
