"""Every database and artifact write in a run's lifecycle.

The journal is pure persistence: it decides nothing about WHETHER a run is
complete, blocked, stopped, or failed (that is the runner's and the verdict
engine's job) - it only records the outcome it is handed, always through the
same rows: runs, messages, shared_context, agents.status, run_events, and the
on-disk run artifact."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from ...config import ARTIFACTS_DIR
from ...db import Database
from .state import TurnState


def utc_stamp() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


class RunJournal:
    def __init__(self, db: Database) -> None:
        self.db = db

    def event(self, run_id: int, event_type: str, payload: Dict[str, Any]) -> None:
        self.db.insert_json_event(run_id, event_type, payload)

    def open_run(
        self,
        agent_id: int,
        agent_name: str,
        user_message: str,
        provider_name: str,
        model: str,
    ) -> int:
        """Create the run row + opening messages and mark the agent running."""
        run_id = self.db.execute(
            "INSERT INTO runs (agent_id, status, provider, model, user_prompt) VALUES (?, ?, ?, ?, ?)",
            (agent_id, "running", provider_name, model, user_message),
        )
        self.db.execute(
            "INSERT INTO messages (agent_id, role, content, run_id) VALUES (?, ?, ?, ?)",
            (agent_id, "user", user_message, run_id),
        )
        self.db.execute(
            "INSERT INTO shared_context (source_agent_id, role, content, importance) VALUES (?, ?, ?, ?)",
            (agent_id, "user", f"{agent_name} received: {user_message}", 2),
        )
        # The runner owns the live status: auto-recovery follow-up runs start
        # WITHOUT the API endpoint (which used to set this), and the UI only
        # re-fetches transcripts for agents marked running: run 103 executed
        # invisibly with the agent still shown as "blocked".
        self.db.execute("UPDATE agents SET status=? WHERE id=?", ("running", agent_id))
        self.event(run_id, "run_started", {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "provider": provider_name,
            "model": model,
        })
        return run_id

    def finalize_terminal(
        self,
        run_id: int,
        agent_id: int,
        state: TurnState,
        latency_ms: int,
        *,
        status: str,
        final_text: str,
        error_text: str | None,
        agent_status: str,
        shared_note: str,
        shared_importance: int,
        event_type: str,
        event_extra: Dict[str, Any],
    ) -> None:
        """The one persistence path shared by the verdict and stopped outcomes:
        write the run row, the assistant message, the shared-context note, the
        agent status, the completion event, and the run artifact."""
        self.db.execute(
            "UPDATE runs SET status=?, final_output=?, error_text=?, latency_ms=?, tool_count=?, loop_count=?, token_estimate=?, ended_at=? WHERE id=?",
            (status, final_text, error_text, latency_ms, state.tool_count, state.loop_count, state.token_estimate, utc_stamp(), run_id),
        )
        self.db.execute(
            "INSERT INTO messages (agent_id, role, content, run_id) VALUES (?, ?, ?, ?)",
            (agent_id, "assistant", final_text, run_id),
        )
        self.db.execute(
            "INSERT INTO shared_context (source_agent_id, role, content, importance) VALUES (?, ?, ?, ?)",
            (agent_id, "assistant", shared_note, shared_importance),
        )
        self.db.execute("UPDATE agents SET status=? WHERE id=?", (agent_status, agent_id))
        self.event(run_id, event_type, {
            "latency_ms": latency_ms,
            "tool_count": state.tool_count,
            "loop_count": state.loop_count,
            **event_extra,
        })
        artifact_path = self.write_artifact(run_id, state.observations)
        self.db.execute("UPDATE runs SET artifact_path=? WHERE id=?", (artifact_path, run_id))

    def finalize_error(self, run_id: int, agent_id: int, exc: Exception, latency_ms: int) -> None:
        """The exception path: mark the run failed. Kept separate from the
        terminal path because it writes no assistant message or shared note."""
        self.db.execute(
            "UPDATE runs SET status=?, error_text=?, latency_ms=?, ended_at=? WHERE id=?",
            ("error", str(exc), latency_ms, utc_stamp(), run_id),
        )
        self.db.execute("UPDATE agents SET status=? WHERE id=?", ("error", agent_id))
        self.event(run_id, "run_error", {"error": str(exc), "error_type": type(exc).__name__})
        artifact_path = self.write_artifact(run_id, [], error=str(exc))
        self.db.execute("UPDATE runs SET artifact_path=? WHERE id=?", (artifact_path, run_id))

    def write_artifact(self, run_id: int, observations: List[Dict[str, Any]], error: str | None = None) -> str:
        run = self.db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,)) or {}
        events = self.db.fetchall("SELECT * FROM run_events WHERE run_id=? ORDER BY id ASC", (run_id,))
        plans = self.db.fetchall("SELECT * FROM plans WHERE run_id=? ORDER BY sort_order ASC", (run_id,))
        normalized_events = []
        for event in events:
            payload_json = event.pop("payload_json", "{}")
            try:
                event["payload"] = json.loads(payload_json)
            except Exception:
                event["payload"] = {"raw": payload_json}
            normalized_events.append(event)
        payload = {
            "run": run,
            "plans": plans,
            "events": normalized_events,
            "observations": observations,
            "error": error,
            "written_at": utc_stamp(),
        }
        out_dir: Path = ARTIFACTS_DIR / "runs"
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"run_{run_id}.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
        return str(target)
