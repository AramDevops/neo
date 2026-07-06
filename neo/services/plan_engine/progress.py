"""Persistence and progression of a run's plan rows.

Owns every read/write of the plans table: syncing the normalized steps,
advancing status as observations arrive, and finalizing at run end. The
stored steps are the deterministic intent record: the runtime controller
drives off them, not off the model's raw phrasing.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from ...db import Database
from .evidence import step_status

EventSink = Callable[[int, str, Dict[str, Any]], None]


class PlanProgressStore:
    def __init__(self, db: Database, event_sink: EventSink) -> None:
        self.db = db
        self._event = event_sink

    def sync(self, run_id: int, steps: List[str]) -> None:
        """Persist the normalized steps, carrying over the status of any step
        text that survived the rewrite. No-op when the stored plan already
        matches."""
        existing = self.db.fetchall("SELECT id, step_text, status FROM plans WHERE run_id=? ORDER BY sort_order ASC", (run_id,))
        if [str(row.get("step_text") or "") for row in existing] == steps:
            return

        existing_by_text: Dict[str, List[Dict[str, Any]]] = {}
        for row in existing:
            existing_by_text.setdefault(str(row.get("step_text") or ""), []).append(row)

        self.db.execute("DELETE FROM plans WHERE run_id=?", (run_id,))
        for index, text in enumerate(steps):
            previous = existing_by_text.get(text, [])
            status = str(previous.pop(0).get("status")) if previous else "pending"
            if status not in {"pending", "in_progress", "complete", "error"}:
                status = "pending"
            self.db.execute(
                "INSERT INTO plans (run_id, step_text, status, sort_order) VALUES (?, ?, ?, ?)",
                (run_id, text, status, index),
            )
        self.normalize_activation(run_id)
        self._event(run_id, "plan_progress", {"status": "synced", "steps": len(steps)})

    def advance_for_observation(self, run_id: int, observation: Dict[str, Any]) -> None:
        rows = self.db.fetchall("SELECT id, step_text, status FROM plans WHERE run_id=? ORDER BY sort_order ASC", (run_id,))
        if not rows:
            return

        candidates = [row for row in rows if row.get("status") == "in_progress"]
        candidates.extend(row for row in rows if row.get("status") == "error")
        candidates.extend(row for row in rows if row.get("status") == "pending")
        for row in candidates:
            used: set[int] = set()
            status = step_status(str(row.get("step_text") or ""), [observation], used, "")
            if status != "complete":
                continue
            self.db.execute("UPDATE plans SET status=? WHERE id=?", ("complete", row["id"]))
            self._event(run_id, "plan_progress", {"plan_id": row["id"], "status": "complete", "tool": observation.get("tool")})
            self.normalize_activation(run_id)
            return
        if not observation.get("ok"):
            active = next((row for row in rows if row.get("status") == "in_progress"), None)
            if active:
                self.db.execute("UPDATE plans SET status=? WHERE id=?", ("error", active["id"]))
                self._event(run_id, "plan_progress", {"plan_id": active["id"], "status": "error", "tool": observation.get("tool")})

    def normalize_activation(self, run_id: int) -> None:
        # Silent: this only shuffles which pending step is "in_progress". The
        # plan pane reads current state from the plans table, so emitting a
        # plan_progress activity per shuffled step just spammed the feed with
        # successive "plan updated" rows (and, firing before sync's
        # "synced", showed "in_progress" then "synced" out of order).
        rows = self.db.fetchall("SELECT id, status FROM plans WHERE run_id=? ORDER BY sort_order ASC", (run_id,))
        unfinished = [row for row in rows if row.get("status") in {"pending", "in_progress", "error"}]
        if not unfinished:
            return
        target = unfinished[0]
        for row in rows:
            if row.get("status") != "in_progress" or row["id"] == target["id"]:
                continue
            self.db.execute("UPDATE plans SET status=? WHERE id=?", ("pending", row["id"]))
        if target.get("status") == "pending":
            self.db.execute("UPDATE plans SET status=? WHERE id=?", ("in_progress", target["id"]))

    def finalize(
        self,
        run_id: int,
        observations: List[Dict[str, Any]],
        final_text: str,
        failed: bool = False,
    ) -> None:
        rows = self.db.fetchall("SELECT id, step_text FROM plans WHERE run_id=? ORDER BY sort_order ASC", (run_id,))
        if not rows:
            return
        if failed:
            self.db.execute("UPDATE plans SET status=? WHERE run_id=? AND status=?", ("error", run_id, "in_progress"))
            return

        used_evidence: set[int] = set()
        current_by_id = {row["id"]: row for row in self.db.fetchall("SELECT id, status FROM plans WHERE run_id=?", (run_id,))}
        for row in rows:
            current_status = str(current_by_id.get(row["id"], {}).get("status") or "")
            if current_status == "complete":
                # Still consume this step's evidence so a later step cannot
                # claim the same observation.
                step_status(str(row.get("step_text") or ""), observations, used_evidence, final_text)
                continue
            status = step_status(str(row.get("step_text") or ""), observations, used_evidence, final_text)
            if status == "pending" and current_status == "error":
                continue
            self.db.execute("UPDATE plans SET status=? WHERE id=?", (status, row["id"]))
