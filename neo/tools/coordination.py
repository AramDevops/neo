from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Dict

from ..config import Settings
from .base import ToolResult, ToolboxHelpers


class CoordinationTools(ToolboxHelpers):
    """Harness database access, git diagnostics, and shared-context notes."""

    def _sql_query(self, query: str) -> ToolResult:
        stripped = query.strip().rstrip(";")
        lowered = stripped.lower()
        allowed = ("select ", "show ", "describe ", "desc ", "explain ")
        if not lowered.startswith(allowed):
            return ToolResult(False, "sql_query", "Only read-only SELECT/SHOW/DESCRIBE/EXPLAIN queries are allowed.", {})
        if ";" in stripped:
            return ToolResult(False, "sql_query", "Multiple SQL statements are not allowed.", {})
        from ..db import Database

        rows = Database().fetchall(stripped)
        rows = rows[:200]
        return ToolResult(True, "sql_query", json.dumps(rows, indent=2, ensure_ascii=False, default=str), {"rows": len(rows)})

    def _git(self, command: list[str]) -> ToolResult:
        result = subprocess.run(
            ["git", *command],
            cwd=str(self.workspace),
            capture_output=True,
            text=True,
            timeout=Settings.tool_timeout_seconds,
        )
        output = ((result.stdout or "") + (result.stderr or ""))[-12000:]
        return ToolResult(result.returncode == 0, f"git_{command[0]}", output, {"returncode": result.returncode})

    def _context_read(self, limit: int) -> ToolResult:
        from ..db import Database
        from ..services.agent_identity import replace_agent_refs

        limit = max(1, min(limit, 100))
        db = Database()
        agents = db.fetchall("SELECT id, name FROM agents ORDER BY id")
        agent_names = {int(row["id"]): str(row["name"]) for row in agents if row.get("name")}
        rows = db.fetchall(
            "SELECT c.id, c.source_agent_id, a.name AS source_agent_name, c.role, c.content, c.importance, c.created_at "
            "FROM shared_context c LEFT JOIN agents a ON a.id=c.source_agent_id "
            "ORDER BY c.id DESC LIMIT ?",
            (limit,),
        )
        for row in rows:
            row["display_content"] = replace_agent_refs(row.get("content"), agent_names)
        return ToolResult(True, "context_read", json.dumps(rows, indent=2, ensure_ascii=False, default=str), {"rows": len(rows)})

    def _context_write(self, content: str, importance: int) -> ToolResult:
        if not content.strip():
            return ToolResult(False, "context_write", "Content is required.", {})
        from ..db import Database
        from .base import current_run_context

        importance = max(1, min(importance, 5))
        # Stamp the writing agent so handoff notes are attributable: a QA
        # agent must be able to tell the auth agent's "login endpoints done"
        # from another agent's guess.
        source_agent_id = current_run_context().get("agent_id")
        context_id = Database().execute(
            "INSERT INTO shared_context (source_agent_id, role, content, importance) VALUES (?, ?, ?, ?)",
            (source_agent_id, "tool", content.strip(), importance),
        )
        return ToolResult(True, "context_write", f"Wrote shared context #{context_id}", {"id": context_id, "source_agent_id": source_agent_id})

    def _list_agents(self) -> ToolResult:
        from ..db import Database

        rows = Database().fetchall("SELECT id, name, title, status, provider, model, updated_at FROM agents ORDER BY id")
        return ToolResult(True, "list_agents", json.dumps(rows, indent=2, ensure_ascii=False, default=str), {"rows": len(rows)})

    # --- Shared project plan: one authoritative brief + task board that every
    # agent builds against, so a team converges on ONE stack/port/layout
    # instead of each agent independently choosing (the CRM run shipped two
    # apps, three ports, and two databases because nothing pinned the plan).

    def _workspace_key(self) -> str:
        return str(self.workspace.resolve())

    def _project_brief_set(self, goal: str, stack: str, conventions: str) -> ToolResult:
        from ..db import Database
        from .base import current_run_context

        db = Database()
        workspace = self._workspace_key()
        updated_by = current_run_context().get("agent_id")
        existing = db.fetchone("SELECT id FROM project_brief WHERE workspace=?", (workspace,))
        if existing:
            db.execute(
                "UPDATE project_brief SET goal=?, stack=?, conventions=?, updated_by=? WHERE workspace=?",
                (goal.strip(), stack.strip(), conventions.strip(), updated_by, workspace),
            )
        else:
            db.execute(
                "INSERT INTO project_brief (workspace, goal, stack, conventions, updated_by) VALUES (?, ?, ?, ?, ?)",
                (workspace, goal.strip(), stack.strip(), conventions.strip(), updated_by),
            )
        return ToolResult(True, "project_brief_set", (
            "Project brief set. Every agent now builds to this goal, stack, and conventions. "
            "Use the SAME stack and ports; do not introduce a different framework or database."
        ), {"goal": goal.strip()[:200], "stack": stack.strip()[:200]})

    def _project_task_add(self, title: str, owner: str, depends_on: str, deliverable: str) -> ToolResult:
        if not title.strip():
            return ToolResult(False, "project_task_add", "A task title is required.", {})
        from ..db import Database

        db = Database()
        task_id = db.execute(
            "INSERT INTO project_tasks (workspace, title, owner, depends_on, deliverable, status) VALUES (?, ?, ?, ?, ?, ?)",
            (self._workspace_key(), title.strip(), owner.strip(), depends_on.strip(), deliverable.strip(), "todo"),
        )
        return ToolResult(True, "project_task_add", f"Added task #{task_id}: {title.strip()} (owner: {owner.strip() or 'unassigned'})", {"id": task_id})

    def _project_task_update(self, task_id: int, status: str, notes: str) -> ToolResult:
        from ..db import Database

        db = Database()
        workspace = self._workspace_key()
        row = db.fetchone("SELECT id, title FROM project_tasks WHERE id=? AND workspace=?", (task_id, workspace))
        if not row:
            return ToolResult(False, "project_task_update", f"No task #{task_id} in this project.", {"id": task_id})
        clean_status = (status or "").strip().lower()
        allowed = {"todo", "in_progress", "done", "blocked"}
        if clean_status and clean_status not in allowed:
            return ToolResult(False, "project_task_update", f"status must be one of: {', '.join(sorted(allowed))}.", {"id": task_id})
        if clean_status and notes.strip():
            db.execute("UPDATE project_tasks SET status=?, notes=? WHERE id=? AND workspace=?", (clean_status, notes.strip(), task_id, workspace))
        elif clean_status:
            db.execute("UPDATE project_tasks SET status=? WHERE id=? AND workspace=?", (clean_status, task_id, workspace))
        elif notes.strip():
            db.execute("UPDATE project_tasks SET notes=? WHERE id=? AND workspace=?", (notes.strip(), task_id, workspace))
        return ToolResult(True, "project_task_update", f"Task #{task_id} ({row.get('title')}) updated: {clean_status or 'notes only'}.", {"id": task_id, "status": clean_status})

    def _project_status(self) -> ToolResult:
        payload = self.project_plan_snapshot()
        if not payload.get("brief") and not payload.get("tasks"):
            return ToolResult(True, "project_status", "No shared project plan yet. The lead agent should set one with project_brief_set, then add tasks.", payload)
        return ToolResult(True, "project_status", json.dumps(payload, indent=2, ensure_ascii=False, default=str), payload)

    def project_plan_snapshot(self) -> Dict[str, Any]:
        """The shared plan for the current workspace: {brief, tasks}. Also used
        by the runner to inject the plan into every agent's prompt."""
        from ..db import Database

        db = Database()
        workspace = self._workspace_key()
        brief = db.fetchone("SELECT goal, stack, conventions, updated_by, updated_at FROM project_brief WHERE workspace=?", (workspace,))
        tasks = db.fetchall(
            "SELECT id, title, owner, depends_on, deliverable, status, notes FROM project_tasks WHERE workspace=? ORDER BY id ASC",
            (workspace,),
        )
        return {"brief": brief, "tasks": tasks}

    def _metrics_snapshot(self) -> ToolResult:
        from ..db import Database

        db = Database()
        counts = {}
        for table in ["agents", "runs", "run_events", "messages", "shared_context", "eval_runs", "eval_items"]:
            counts[table] = db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")["n"]
        latest_eval = db.fetchone("SELECT id, status, provider, model, score, passed, total, latency_ms FROM eval_runs ORDER BY id DESC LIMIT 1")
        latest_runs = db.fetchall(
            "SELECT r.id, r.agent_id, a.name AS agent_name, r.status, r.provider, r.model, r.latency_ms, r.tool_count "
            "FROM runs r LEFT JOIN agents a ON a.id=r.agent_id ORDER BY r.id DESC LIMIT 5"
        )
        payload = {"counts": counts, "latest_eval": latest_eval, "latest_runs": latest_runs}
        return ToolResult(True, "metrics_snapshot", json.dumps(payload, indent=2, default=str), payload)

    def _write_artifact(self, name: str, content: str) -> ToolResult:
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name or "artifact.txt").strip("._") or "artifact.txt"
        target = self._artifacts_root() / "tool_outputs" / safe_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult(True, "write_artifact", f"Wrote artifact {target}", {"path": str(target), "bytes": len(content.encode("utf-8"))})
