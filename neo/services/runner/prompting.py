"""Prompt assembly and context budgeting for the agent loop.

Everything the model sees in one turn is built here: the system contract, the
agent's identity/role/scope, the shared project plan, tool catalog, shared
context, peer run summaries, local transcript, and the (budgeted) tool
observations. The module also owns the context diet: clipping, arg previews,
and the observation compaction that keeps long runs inside a small model's
window."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from ...db import Database
from ...tools.base import parse_scope_paths
from ..agent_contract import SYSTEM_CONTRACT
from ..agent_identity import agent_label, replace_agent_refs
from ..tools import Toolbox

# Above this serialized size, the oldest observation outputs are compacted so
# long runs never overflow a small model's context window.
OBSERVATION_CHAR_BUDGET = 60_000


def clip(text: Any, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[:limit] + f"...[+{len(value) - limit} chars]"


def compact_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Preview of tool args for the observation history. Large values
    (file contents, long commands) were already delivered to the tool;
    re-serializing them into every subsequent prompt is pure token burn
    and drowns small models in their own past writes."""
    compact: Dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > 600:
            compact[key] = value[:300] + f"...[+{len(value) - 300} chars applied]"
        else:
            compact[key] = value
    return compact


def budgeted_observations(observations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep the serialized observation history under a hard budget.

    Newest observations stay verbatim; when the total overflows, the
    OLDEST observations are compacted (output AND the meta payload, which
    can carry log tails / health reports) to a short head. Deterministic
    context control so a long run shrinks old evidence instead of
    overflowing the model's window (the full data remains in run
    events/artifacts)."""
    def weight(item: Dict[str, Any]) -> int:
        meta = item.get("meta")
        meta_len = len(json.dumps(meta, default=str)) if meta else 0
        return len(str(item.get("output") or "")) + meta_len
    total = sum(weight(item) for item in observations)
    if total <= OBSERVATION_CHAR_BUDGET:
        return observations
    budgeted: List[Dict[str, Any]] = [dict(item) for item in observations]
    excess = total - OBSERVATION_CHAR_BUDGET
    for item in budgeted:
        if excess <= 0:
            break
        if weight(item) <= 400:
            continue
        output = str(item.get("output") or "")
        compacted = output[:200] + f"...[compacted; {len(output)} chars total, full output in the run log]"
        saved = weight(item) - len(compacted)
        item["output"] = compacted
        # Drop the heavy meta payload from the compacted view; it is still
        # in the run events and artifact.
        if item.get("meta"):
            item["meta"] = {"compacted": True}
        excess -= max(0, saved)
    return budgeted


class PromptBuilder:
    def __init__(self, db: Database, toolbox: Toolbox) -> None:
        self.db = db
        self.toolbox = toolbox

    def build(
        self,
        agent_id: int,
        user_message: str,
        observations: List[Dict[str, Any]],
        plan_section: str | None = None,
    ) -> str:
        """Assemble the full prompt for one loop iteration.

        `plan_section` is computed by the CALLER (normally the runner's
        _project_plan_section) rather than internally, so tests that patch the
        runner's method keep steering live runs."""
        agent = self.db.fetchone("SELECT * FROM agents WHERE id=?", (agent_id,)) or {}
        agent_names = self.agent_names()
        local_rows = self.db.fetchall(
            "SELECT role, content FROM messages WHERE agent_id=? ORDER BY id DESC LIMIT 12",
            (agent_id,),
        )
        local_messages = [
            {**row, "content": replace_agent_refs(row.get("content"), agent_names)}
            for row in local_rows
        ]
        # Newest shared notes reach every agent (recency is the load-bearing
        # invariant: a fresh handoff must never be starved by old high-priority
        # rows). The auto "X received: ..." echo rows (role=user) are dropped
        # in Python - they duplicate the peers' own transcripts and are a
        # proven noise-hijack hazard; a genuine context_write mentioning
        # "received:" (role=tool) is kept. Fetch a wider window, then trim.
        raw_shared = self.db.fetchall(
            "SELECT c.id, c.source_agent_id, a.name AS source_agent_name, a.title AS source_agent_title, "
            "c.role, c.content, c.importance, c.created_at "
            "FROM shared_context c LEFT JOIN agents a ON a.id=c.source_agent_id "
            "ORDER BY c.id DESC LIMIT 48"
        )
        kept = [
            row for row in raw_shared
            if not (str(row.get("role")) == "user" and " received: " in str(row.get("content") or ""))
        ][:24]
        shared = []
        for row in sorted(kept, key=lambda item: int(item.get("id") or 0)):
            source_agent_id = row.get("source_agent_id")
            shared.append({
                "source_agent_name": row.get("source_agent_name") or (agent_names.get(int(source_agent_id)) if source_agent_id else None),
                "source_agent_title": row.get("source_agent_title") or "",
                "role": row.get("role"),
                "content": clip(replace_agent_refs(row.get("content"), agent_names), 900),
                "created_at": row.get("created_at"),
            })
        peer_rows = self.db.fetchall(
            "SELECT r.id AS run_id, a.name AS agent_name, a.title AS agent_title, "
            "r.status, r.final_output, r.error_text "
            "FROM runs r LEFT JOIN agents a ON a.id=r.agent_id "
            "WHERE r.agent_id <> ? ORDER BY r.id DESC LIMIT 12",
            (agent_id,),
        )
        peer_runs = [
            {
                **row,
                # final_output is unbounded MEDIUMTEXT; uncapped it can dwarf
                # the actual task in every loop of every sibling agent.
                "final_output": clip(replace_agent_refs(row.get("final_output"), agent_names), 600),
                "error_text": clip(replace_agent_refs(row.get("error_text"), agent_names), 600),
            }
            for row in peer_rows
        ]
        sections = [
            SYSTEM_CONTRACT,
            f"Agent identity: {agent_label(agent_id, agent)} / {agent.get('title', '')}",
        ]
        role_prompt = str(agent.get("system_prompt") or "").strip()
        if role_prompt:
            sections.append(
                "ROLE INSTRUCTIONS for this agent (authoritative for scope: interpret every request "
                "within this role, hand work outside it to the responsible agent via context_write):\n" + role_prompt
            )
        scope_list = parse_scope_paths(agent.get("scope_paths"))
        if scope_list:
            sections.append(
                "ENFORCED WRITE SCOPE: only create/edit/move/delete paths matching: "
                + ", ".join(scope_list)
                + ". The file tools block writes outside this scope. Make all file changes with the file tools "
                "(write_file/edit_file/append_file/move_path/copy_path/delete_path) so the boundary is enforced; "
                "do not route writes through the shell to escape it. Read access is workspace-wide."
            )
        if plan_section:
            sections.append(plan_section)
        sections.extend([
            "Available tools:\n" + json.dumps(self.toolbox.describe(), indent=2),
            "Recent shared context (use source_agent_name, never numeric agent ids):\n" + json.dumps(shared, default=str, ensure_ascii=False, indent=2),
            "Peer run summaries (use agent_name, never numeric agent ids):\n" + json.dumps(peer_runs, default=str, ensure_ascii=False, indent=2),
            "Local terminal messages:\n" + json.dumps(list(reversed(local_messages)), ensure_ascii=False, indent=2),
            "Tool observations so far:\n" + json.dumps(budgeted_observations(observations), ensure_ascii=False, indent=2),
            f"Current user message:\n{user_message}",
        ])
        return "\n\n".join(sections)

    def project_plan_section(self) -> str | None:
        """Render the shared project plan (brief + task board) as authoritative
        prompt context, so a team of agents builds to ONE stack/port/layout and
        against a common task board. Returns None when no plan is set (single-
        agent runs are unaffected)."""
        try:
            snapshot = self.toolbox.project_plan_snapshot()
        except Exception:
            return None
        brief = snapshot.get("brief")
        tasks = snapshot.get("tasks") or []
        if not brief and not tasks:
            return None
        lines = ["SHARED PROJECT PLAN (authoritative for the whole team - build to this, do not diverge):"]
        if brief:
            if brief.get("goal"):
                lines.append(f"- Goal: {clip(brief.get('goal'), 600)}")
            if brief.get("stack"):
                lines.append(f"- Stack (use exactly this framework/database/language, no substitutes): {clip(brief.get('stack'), 400)}")
            if brief.get("conventions"):
                lines.append(f"- Conventions (ports, directory layout, naming): {clip(brief.get('conventions'), 600)}")
        if tasks:
            lines.append("Task board (owner / dependencies / status) - work your own tasks, do not redo others':")
            for task in tasks:
                owner = task.get("owner") or "unassigned"
                deps = f", after: {task.get('depends_on')}" if task.get("depends_on") else ""
                note = f" - {clip(task.get('notes'), 160)}" if task.get("notes") else ""
                lines.append(f"  #{task.get('id')} [{task.get('status') or 'todo'}] {task.get('title')} (owner: {owner}{deps}){note}")
        lines.append(
            "Before starting, read this plan and project_status: if a task you would do depends on an unfinished "
            "task, do the prerequisite or wait; keep the stack and ports identical to the brief; mark your task "
            "in_progress/done with project_task_update as you go."
        )
        return "\n".join(lines)

    def agent_names(self) -> Dict[int, str]:
        rows = self.db.fetchall("SELECT id, name FROM agents ORDER BY id")
        names: Dict[int, str] = {}
        for row in rows:
            try:
                current_id = int(row.get("id"))
            except Exception:
                continue
            name = str(row.get("name") or "").strip()
            if name:
                names[current_id] = name
        return names
