"""Workspace scenarios: file operations graded on the real filesystem.

Organizing loose files into a structure and creating a file whose content is
then proven with grep — both graded by inspecting the resulting tree and tool
evidence, never the model's words.
"""

from __future__ import annotations

from pathlib import Path

from ..harness import Scenario, setup_noop


def _setup_organize(workspace: Path, ctx: dict) -> None:
    (workspace / "app.py").write_text("print('planner')\n", encoding="utf-8")
    (workspace / "index.html").write_text("<html><body>planner</body></html>\n", encoding="utf-8")
    (workspace / "styles.css").write_text("body { margin: 0; }\n", encoding="utf-8")
    (workspace / "README.txt").write_text("planner files\n", encoding="utf-8")


def _script_organize(turn: int, prompt: str, ctx: dict) -> dict:
    plan = [
        "Inspect the workspace tree and relevant files",
        "Move or group relevant files into the intended structure",
        "Verify the resulting workspace tree",
    ]
    if turn == 1:
        return {"plan": plan, "tool_calls": [
            {"tool": "tree", "args": {"path": ".", "max_depth": 2}},
            {"tool": "make_dir", "args": {"path": "backend"}},
            {"tool": "make_dir", "args": {"path": "frontend"}},
            {"tool": "move_path", "args": {"source": "app.py", "destination": "backend/app.py"}},
        ], "final": "", "needs_more": True}
    if turn == 2:
        return {"plan": plan, "tool_calls": [
            {"tool": "move_path", "args": {"source": "index.html", "destination": "frontend/index.html"}},
            {"tool": "move_path", "args": {"source": "styles.css", "destination": "frontend/styles.css"}},
            {"tool": "tree", "args": {"path": ".", "max_depth": 2}},
        ], "final": "", "needs_more": True}
    return {"plan": plan, "tool_calls": [], "needs_more": False,
            "final": "Grouped the planner files: app.py into backend/, index.html and styles.css into frontend/, verified with tree."}


def _grade_organize(result: dict) -> tuple[bool, dict]:
    run = result["run"]
    workspace: Path = result["workspace"]
    checks = {
        "status_complete": run["status"] == "complete",
        "backend_file_moved": (workspace / "backend" / "app.py").is_file(),
        "frontend_files_moved": (workspace / "frontend" / "index.html").is_file() and (workspace / "frontend" / "styles.css").is_file(),
        "plan_all_complete": result["plan_complete"] == result["plan_total"] and result["plan_total"] > 0,
        "verdict_consistent": result["verdict_consistent"] is True,
    }
    return all(checks.values()), checks


def _script_evidence_grep(turn: int, prompt: str, ctx: dict) -> dict:
    plan = [
        "Create the diagnostic file with the marker text",
        "Verify the marker with grep",
    ]
    if turn == 1:
        return {"plan": plan, "tool_calls": [
            {"tool": "write_file", "args": {"path": "diagnostic/target.txt", "content": "needle-neo-42\n"}},
            {"tool": "grep", "args": {"path": "diagnostic", "pattern": "needle-neo-42"}},
        ], "final": "", "needs_more": True}
    return {"plan": plan, "tool_calls": [], "needs_more": False,
            "final": "Created diagnostic/target.txt and grep verified needle-neo-42 inside it."}


def _grade_evidence_grep(result: dict) -> tuple[bool, dict]:
    run = result["run"]
    tool_events = result["tool_events"]
    workspace: Path = result["workspace"]
    greps = [e for e in tool_events if e["payload"].get("tool") == "grep" and e["payload"].get("ok")]
    checks = {
        "status_complete": run["status"] == "complete",
        "file_exists_with_token": (workspace / "diagnostic" / "target.txt").is_file() and "needle-neo-42" in (workspace / "diagnostic" / "target.txt").read_text(encoding="utf-8"),
        "grep_found_token": bool(greps) and "needle-neo-42" in str(greps[0]["payload"].get("output", "")),
        "plan_all_complete": result["plan_complete"] == result["plan_total"] and result["plan_total"] > 0,
        "verdict_consistent": result["verdict_consistent"] is True,
    }
    return all(checks.values()), checks


ORGANIZE_WORKSPACE = Scenario(
    id="organize_workspace",
    category="operations",
    prompt="organize the workspace better and group the calendar planner files",
    expected_status="complete",
    setup=_setup_organize,
    script=_script_organize,
    grade=_grade_organize,
)

EVIDENCE_GREP = Scenario(
    id="evidence_grep",
    category="tools",
    prompt="Create diagnostic/target.txt containing the exact token needle-neo-42, then verify it with grep and report the filename.",
    expected_status="complete",
    setup=setup_noop,
    script=_script_evidence_grep,
    grade=_grade_evidence_grep,
)
