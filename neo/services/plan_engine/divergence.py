"""Guards that keep one agent from silently forking the shared team plan.

Both gates compare this run's observations against the shared project brief
and tasks (the cross-agent plan stored in the db), and return a blocking
reason string, or "" when nothing diverges.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List

from ...db import Database

# Files that mark a directory as a runnable APP (not just any subfolder).
APP_ENTRY_FILES = {"server.js", "index.js", "app.js", "app.py", "main.js", "main.py", "package.json", "index.html"}
INFRA_DIRS = {"node_modules", ".neo", ".git", "dist", "build", "__pycache__", "uploads", "venv", ".venv", "artifacts"}
PLAN_CHANGE_TOOLS = {"write_file", "append_file", "edit_file", "make_dir", "move_path", "copy_path", "download_url"}


def _plan_text(plan: Dict[str, Any]) -> str:
    """The shared brief + task text flattened into one searchable string."""
    parts = [str(plan["brief"].get(key) or "") for key in ("goal", "stack", "conventions")]
    for task in plan.get("tasks") or []:
        parts.append(str(task.get("title") or ""))
        parts.append(str(task.get("deliverable") or ""))
    return " ".join(parts)


class DivergenceGates:
    def __init__(self, db: Database, workspace_getter: Callable[[], Any] | None = None) -> None:
        self.db = db
        # Optional: resolves the current workspace path so the plan-divergence
        # gate can scope the shared brief to this project. None -> fall back to
        # the most-recent brief (fine for single-workspace tests).
        self._workspace_getter = workspace_getter

    def _current_project_plan(self) -> Dict[str, Any] | None:
        """The shared brief + tasks for the current workspace (or the most
        recent brief when no workspace resolver is wired)."""
        try:
            if self._workspace_getter is not None:
                workspace = str(self._workspace_getter().resolve())
                brief = self.db.fetchone("SELECT goal, stack, conventions FROM project_brief WHERE workspace=?", (workspace,))
                tasks = self.db.fetchall("SELECT title, deliverable FROM project_tasks WHERE workspace=?", (workspace,))
            else:
                brief = self.db.fetchone("SELECT goal, stack, conventions FROM project_brief ORDER BY id DESC LIMIT 1")
                tasks = self.db.fetchall("SELECT title, deliverable FROM project_tasks ORDER BY id DESC LIMIT 50")
        except Exception:
            return None
        if not brief:
            return None
        return {"brief": brief, "tasks": tasks}

    @staticmethod
    def _plan_declared_dirs(plan: Dict[str, Any]) -> set[str]:
        """Top-level directory names the shared plan declares (any token that
        appears immediately before a slash in the brief or task text)."""
        return {match.lower() for match in re.findall(r"([A-Za-z0-9._-]+)[\\/]", _plan_text(plan))}

    def plan_divergence(self, observations: List[Dict[str, Any]]) -> str:
        """Catch a rogue agent forking a SECOND app outside the shared plan.

        When a shared project plan declares a structure and this run created a
        NEW top-level directory (with a runnable-app entry file) that the plan
        never mentions, that is the exact CRM failure (a second `crm-backend/`
        app beside the agreed `crm/`). Returns a blocking reason, or "" when
        there is no plan, no declared structure, or no divergent app dir."""
        plan = self._current_project_plan()
        if not plan:
            return ""
        allowed = self._plan_declared_dirs(plan)
        if not allowed:
            # The plan declares no directory structure, so nothing is divergent.
            return ""
        for item in observations:
            if not item.get("ok") or str(item.get("tool")) not in PLAN_CHANGE_TOOLS:
                continue
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            args = item.get("args") if isinstance(item.get("args"), dict) else {}
            rel = str(meta.get("relative_path") or meta.get("final_path") or args.get("path") or args.get("destination") or "")
            parts = [segment for segment in re.split(r"[\\/]+", rel) if segment]
            if len(parts) < 2:
                continue  # a top-level file is not a new app directory
            top = parts[0].lower()
            filename = parts[-1].lower()
            if top in allowed or top in INFRA_DIRS:
                continue
            if filename in APP_ENTRY_FILES:
                return (
                    f"Plan divergence: this run created a second app under {parts[0]}/ (found {parts[-1]}), "
                    f"but the shared project plan builds under: {', '.join(sorted(allowed))}. Do not fork a "
                    "parallel app or a different stack. Build inside the plan's structure, or update the shared "
                    "plan first with project_brief_set / project_task_add."
                )
        return ""

    @staticmethod
    def _declared_ports(plan: Dict[str, Any]) -> set[int]:
        """Ports the shared plan assigns (BACKEND_PORT=N, 'port N', or ':N')."""
        text = _plan_text(plan)
        ports: set[int] = set()
        for pattern in (r"BACKEND_PORT\s*=\s*(\d{2,5})", r"\bport\s*[:=]?\s*(\d{2,5})\b", r":(\d{4,5})\b"):
            for match in re.findall(pattern, text, re.IGNORECASE):
                try:
                    ports.add(int(match))
                except ValueError:
                    continue
        return {p for p in ports if 1 <= p <= 65535}

    def port_divergence(self, observations: List[Dict[str, Any]]) -> str:
        """Catch an agent binding a port the shared plan did NOT assign, so the
        team's services actually connect (the CRM's server ran on 5002 while
        the config said 3001). Returns a blocking reason, or ""."""
        plan = self._current_project_plan()
        if not plan:
            return ""
        declared = self._declared_ports(plan)
        if not declared:
            return ""
        for item in observations:
            if not item.get("ok") or str(item.get("tool")) != "start_process":
                continue
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            args = item.get("args") if isinstance(item.get("args"), dict) else {}
            bound: set[int] = set()
            for value in (meta.get("ports") or []):
                try:
                    bound.add(int(value))
                except (TypeError, ValueError):
                    continue
            for candidate in (meta.get("listening_port"), args.get("port")):
                try:
                    if candidate:
                        bound.add(int(candidate))
                except (TypeError, ValueError):
                    continue
            bound.discard(0)
            if bound and not (bound & declared):
                return (
                    f"Port divergence: this run bound port {sorted(bound)[0]} but the shared plan assigns port "
                    f"{sorted(declared)[0]}. Use the port the plan assigns so the team's services connect; do not "
                    "pick a different port, or update the shared plan first."
                )
        return ""
