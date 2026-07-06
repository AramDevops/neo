"""Intent scenario: a status question must be answered without side effects.

Asking "is there any app running" must produce observation only — no process
starts, no browser, no policy retries — and an honest answer.
"""

from __future__ import annotations

from pathlib import Path

from ..harness import Scenario, observed


def _setup_status_question(workspace: Path, ctx: dict) -> None:
    target = workspace / "calendar-app" / "backend" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from flask import Flask\n\napp = Flask(__name__)\n\nif __name__ == '__main__':\n    app.run(port=5000)\n",
        encoding="utf-8",
    )


def _script_status_question(turn: int, prompt: str, ctx: dict) -> dict:
    obs = observed(prompt)
    plan = ["List all background processes started by Neo", "Report whether any application is running"]
    if '"tool": "list_processes"' not in obs:
        return {"plan": plan, "tool_calls": [{"tool": "list_processes", "args": {}}], "final": "", "needs_more": True}
    return {"plan": plan, "tool_calls": [], "needs_more": False,
            "final": "No application is currently running under Neo."}


def _grade_status_question(result: dict) -> tuple[bool, dict]:
    run = result["run"]
    tool_events = result["tool_events"]
    tools_used = {e["payload"].get("tool") for e in tool_events}
    checks = {
        "status_complete": run["status"] == "complete",
        "observation_only": tools_used <= {"list_processes"},
        "no_process_started": "start_process" not in tools_used,
        "no_browser_opened": "open_browser" not in tools_used and "http_get" not in tools_used,
        "no_policy_retries": sum(1 for e in result["events"] if e["type"] == "policy_retry") == 0,
        "honest_answer": "no application is currently running" in str(run.get("final_output") or "").lower(),
    }
    return all(checks.values()), checks


STATUS_QUESTION_NO_SIDE_EFFECTS = Scenario(
    id="status_question_no_side_effects",
    category="intent",
    prompt="is there any app running",
    expected_status="complete",
    setup=_setup_status_question,
    script=_script_status_question,
    grade=_grade_status_question,
)
