from __future__ import annotations

import time
from typing import Any

from . import runtime
from ..tools.registry import TOOL_METADATA


MODE_ASK = "ask"
MODE_FULL = "full"
MODES = (MODE_ASK, MODE_FULL)
DEFAULT_GRANT_MINUTES = 15

# Every tool in the computer category is gated: seeing the screen and acting
# on it are both user-owned capabilities.
GATED_TOOLS = frozenset(
    name for name, meta in TOOL_METADATA.items() if meta.get("category") == "computer"
)


def _state() -> dict[str, Any]:
    data = runtime.runtime_settings()
    raw = data.get("computer_access")
    return raw if isinstance(raw, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    data = runtime.runtime_settings()
    data["computer_access"] = state
    runtime.save_runtime_settings(data)


def mode() -> str:
    value = str(_state().get("mode") or MODE_ASK).lower()
    return value if value in MODES else MODE_ASK


def set_mode(value: str) -> dict[str, Any]:
    clean = str(value or "").strip().lower()
    if clean not in MODES:
        raise ValueError(f"mode must be one of: {', '.join(MODES)}")
    state = _state()
    state["mode"] = clean
    if clean == MODE_FULL:
        state.pop("granted_until", None)
    _save_state(state)
    return status()


def grant(minutes: float = DEFAULT_GRANT_MINUTES) -> dict[str, Any]:
    clean_minutes = max(1.0, min(float(minutes or DEFAULT_GRANT_MINUTES), 480.0))
    state = _state()
    state["mode"] = mode()
    state["granted_until"] = time.time() + clean_minutes * 60
    _save_state(state)
    return status()


def revoke() -> dict[str, Any]:
    state = _state()
    state.pop("granted_until", None)
    _save_state(state)
    return status()


def allowed() -> bool:
    if mode() == MODE_FULL:
        return True
    try:
        granted_until = float(_state().get("granted_until") or 0)
    except (TypeError, ValueError):
        granted_until = 0.0
    return time.time() < granted_until


def status() -> dict[str, Any]:
    state = _state()
    try:
        granted_until = float(state.get("granted_until") or 0)
    except (TypeError, ValueError):
        granted_until = 0.0
    remaining = max(0, int(granted_until - time.time()))
    return {
        "mode": mode(),
        "allowed": allowed(),
        "granted": mode() == MODE_ASK and remaining > 0,
        "seconds_remaining": remaining if mode() == MODE_ASK else None,
        "gated_tools": sorted(GATED_TOOLS),
    }


def denial_message(tool: str) -> str:
    return (
        f"Computer control is not authorized for {tool}. Access mode is 'ask' and no active "
        "grant exists. Do not retry this tool; report to the user that they must grant "
        "computer access (timed grant) or switch the computer access mode to 'full' in Neo settings."
    )
