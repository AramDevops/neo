"""Computer-access scenarios: screen capture under both permission postures.

With full control granted, a screenshot must be captured and attached to the
next model turn; with access revoked in ask mode, the capture must be denied
and the final answer must say how to grant access.
"""

from __future__ import annotations

from pathlib import Path

from ... import computer_access
from ..harness import Scenario, observed


def _setup_vision(workspace: Path, ctx: dict) -> None:
    # The user has switched computer access to full control.
    computer_access.set_mode("full")


def _script_vision(turn: int, prompt: str, ctx: dict) -> dict:
    plan = ["Capture the screen", "Describe the visible content"]
    if turn == 1:
        return {"plan": plan, "tool_calls": [{"tool": "screen_capture", "args": {}}], "final": "", "needs_more": True}
    return {"plan": plan, "tool_calls": [], "needs_more": False,
            "final": "Screenshot captured; described the visible content from the attached image."}


def _grade_vision(result: dict) -> tuple[bool, dict]:
    run = result["run"]
    tool_events = result["tool_events"]
    captures = [e for e in tool_events if e["payload"].get("tool") == "screen_capture" and e["payload"].get("ok")]
    images = result["record"].get("images_per_call") or []
    checks = {
        "status_complete": run["status"] == "complete",
        "screen_captured": bool(captures),
        "image_attached_to_next_turn": len(images) >= 2 and bool(images[1]),
    }
    return all(checks.values()), checks


def _setup_access_denied(workspace: Path, ctx: dict) -> None:
    # Default posture: ask mode, no active grant.
    computer_access.set_mode("ask")
    computer_access.revoke()


def _script_access_denied(turn: int, prompt: str, ctx: dict) -> dict:
    obs = observed(prompt)
    plan = ["Capture the screen", "Describe the visible content"]
    if '"tool": "screen_capture"' not in obs:
        return {"plan": plan, "tool_calls": [{"tool": "screen_capture", "args": {}}], "final": "", "needs_more": True}
    return {"plan": plan, "tool_calls": [], "needs_more": False,
            "final": "Screen capture is blocked: computer access has not been granted. Please grant computer access or enable full control in settings."}


def _grade_access_denied(result: dict) -> tuple[bool, dict]:
    run = result["run"]
    tool_events = result["tool_events"]
    denials = [e for e in tool_events if (e["payload"].get("meta") or {}).get("approval_required")]
    ok_computer = [e for e in tool_events if e["payload"].get("tool") == "screen_capture" and e["payload"].get("ok")]
    checks = {
        "status_complete": run["status"] == "complete",
        "denial_recorded": bool(denials),
        "no_computer_action_executed": not ok_computer,
        "final_reports_access_requirement": "grant" in str(run.get("final_output") or "").lower(),
    }
    return all(checks.values()), checks


VISION_SCREENSHOT = Scenario(
    id="vision_screenshot",
    category="computer",
    prompt="take a screenshot of my screen and describe what you see",
    expected_status="complete",
    setup=_setup_vision,
    script=_script_vision,
    grade=_grade_vision,
)

COMPUTER_ACCESS_DENIED = Scenario(
    id="computer_access_denied",
    category="permissions",
    prompt="take a screenshot of my screen and describe what you see",
    expected_status="complete",
    setup=_setup_access_denied,
    script=_script_access_denied,
    grade=_grade_access_denied,
)
