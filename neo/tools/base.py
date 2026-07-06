from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict


@dataclass
class ToolResult:
    ok: bool
    tool: str
    output: str
    meta: Dict[str, Any]


# Per-thread identity of the agent whose run is executing tools right now.
# Each run executes on its own thread, so a threading.local carries the
# calling agent through the shared Toolbox without changing every tool
# signature. Empty context (direct Toolbox use, tests) means: unrestricted
# scope, unowned processes, unattributed notes - the pre-existing behavior.
_RUN_CONTEXT = threading.local()


def set_run_context(
    agent_id: int | None = None,
    agent_name: str = "",
    run_id: int | None = None,
    scope_paths: list[str] | None = None,
) -> None:
    _RUN_CONTEXT.value = {
        "agent_id": agent_id,
        "agent_name": agent_name,
        "run_id": run_id,
        "scope_paths": [str(item) for item in (scope_paths or []) if str(item).strip()],
    }


def clear_run_context() -> None:
    _RUN_CONTEXT.value = {}


def current_run_context() -> Dict[str, Any]:
    return getattr(_RUN_CONTEXT, "value", {}) or {}


def parse_scope_paths(raw: Any) -> list[str]:
    """Single source of truth for turning stored/user scope input into a glob
    list. Accepts a real list, a JSON-array string ('["server/*"]'), or a
    comma/space separated string. Used at write time (API) and enforce time
    (runner) so the two can never disagree."""
    import json as _json
    import re as _re

    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw).strip()
    if not text:
        return []
    try:
        parsed = _json.loads(text)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except ValueError:
        pass
    return [part for part in _re.split(r"[,\s]+", text) if part]


class ToolboxHelpers:
    """Workspace-safety and output-hygiene helpers shared by all tool mixins.

    The composing Toolbox provides `workspace` (a resolved Path property),
    `processes` (a ProcessManager), and `_artifacts_root()` (the artifacts
    directory, resolved at call time so tests can monkeypatch it).
    """

    workspace: Path

    def _artifacts_root(self) -> Path:  # overridden by the Toolbox composition
        raise NotImplementedError

    def _safe_path(self, path: str) -> Path:
        candidate = (self.workspace / (path or ".")).resolve()
        root = self.workspace.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"Path escapes workspace: {path}")
        return candidate

    def _ensure_workspace_path(self, candidate: Path, label: str) -> Path:
        resolved = candidate.resolve()
        root = self.workspace.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"Path escapes workspace: {label}")
        return resolved

    def _safe_child_path(self, root: Path, path: str) -> Path:
        raw = Path(path or ".")
        candidate = raw if raw.is_absolute() else root / raw
        return self._ensure_workspace_path(candidate, path)

    def _scope_violation(self, target: Path) -> str | None:
        """Enforce the calling agent's write scope, when one is configured.

        Scope patterns are fnmatch globs over the workspace-relative posix
        path (e.g. "server/*", "docs/*.md"; fnmatch's * crosses slashes, so
        "server/*" covers the whole subtree). No scope configured = full
        workspace access. This turns role boundaries ("backend agent: server/
        only") from prompt hopes into deterministic guarantees."""
        scope = current_run_context().get("scope_paths") or []
        if not scope:
            return None
        try:
            relative = target.resolve().relative_to(self.workspace.resolve()).as_posix()
        except ValueError:
            return None  # outside workspace - _safe_path already rejects that
        for pattern in scope:
            clean = pattern.strip().strip("/").replace("\\", "/")
            if not clean:
                continue
            if relative == clean or fnmatch(relative, clean) or fnmatch(relative, clean + "/*"):
                return None
            # "server/*" also owns the scope directory itself, so the agent
            # can create or remove its own root folder.
            for suffix in ("/*", "/**"):
                if clean.endswith(suffix) and relative == clean[: -len(suffix)]:
                    return None
        agent_name = current_run_context().get("agent_name") or "this agent"
        return (
            f"Write blocked: {relative} is outside the write scope assigned to {agent_name} "
            f"({', '.join(scope)}). Work only inside your scope, or ask the user to widen it."
        )

    def _flag(self, value: Any) -> bool:
        """Parse a model-supplied boolean. bool("false") is True in Python,
        which silently INVERTED destructive flags whenever a model sent the
        string "false" - this treats common string spellings correctly."""
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "on"}
        return bool(value)

    def _process_text(self, result: subprocess.CompletedProcess[str]) -> str:
        return ((result.stdout or "") + (result.stderr or "")).replace("\x00", "")

    def _bounded_output(self, text: str, limit: int = 12000) -> str:
        clean = self._redact_sensitive((text or "").replace("\x00", ""))
        return clean[-limit:]

    def _redact_sensitive(self, text: str) -> str:
        if not text:
            return text
        redacted = re.sub(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
        redacted = re.sub(r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)['\"]?[^'\"\s,;]+", r"\1[REDACTED]", redacted)
        redacted = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[REDACTED_OPENAI_KEY]", redacted)
        redacted = re.sub(r"AIza[0-9A-Za-z\-_]{20,}", "[REDACTED_GOOGLE_KEY]", redacted)
        return redacted

    def _safe_subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        secret_key = re.compile(r"(key|token|secret|password|credential|auth)", re.I)
        for key in list(env.keys()):
            if secret_key.search(key):
                env.pop(key, None)
        env.setdefault("NO_COLOR", "1")
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        return env

    def _read_limited_bytes(self, path: Path, limit: int) -> bytes:
        with path.open("rb") as handle:
            return handle.read(limit)

    def _tail_file(self, path: Path, limit: int = 4000) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]

    def _looks_like_human_text(self, text: str) -> bool:
        stripped = text.strip()
        if len(stripped) < 2:
            return False
        printable = sum(1 for char in stripped if char.isprintable() or char.isspace())
        return printable / max(len(stripped), 1) > 0.85
