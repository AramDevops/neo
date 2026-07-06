"""Auto-recovery policy for blocked runs.

Pure decisions: is this blocker worth retrying, has the follow-up made any
progress, and what does the continuation prompt say. The retry DRIVER (the
loop that actually re-runs turns) stays in AgentRunner.run_turn_resilient."""

from __future__ import annotations

import json
import re

from ...db import Database


class RecoveryPolicy:
    def __init__(self, db: Database) -> None:
        self.db = db

    def blocker_is_recoverable(self, reason: str) -> bool:
        lowered = (reason or "").lower()
        if not lowered:
            return False
        # Never auto-retry a deliberate refusal or a genuine ask for input.
        if any(term in lowered for term in ["safe alternative", "will not", "refus", "clarif", "need more information"]):
            return False
        recoverable_markers = [
            "failed to start", "did not respond", "no verified app url", "verification blocked",
            "port", "process exited", "install", "start_process", "http", "unfinished evidence-backed",
            "health check", "change verification", "tests failing",
        ]
        return any(term in lowered for term in recoverable_markers)

    def signature(self, run_id: int, reason: str) -> str:
        """Fingerprint of HOW a run is blocked (normalized reason + failing
        tools); two identical signatures in a row mean no progress."""
        rows = self.db.fetchall(
            "SELECT payload_json FROM run_events WHERE run_id=? AND event_type=? ORDER BY id DESC LIMIT 20",
            (run_id, "tool_result"),
        )
        failed_tools: set[str] = set()
        for row in rows:
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except Exception:
                continue
            if not payload.get("ok"):
                failed_tools.add(str(payload.get("tool") or ""))
        # Strip digits so a changing free-port number does not look like progress.
        norm_reason = re.sub(r"\d+", "#", reason.lower())[:160]
        return norm_reason + "|" + ",".join(sorted(failed_tools))

    def prompt(self, original: str, reason: str) -> str:
        return (
            f"Continue the previous task: {original.strip()[:300]}. "
            f"The last attempt was blocked: {reason.strip()[:300]}. "
            "Read the tool evidence from the previous attempt, find the real root cause, and fix it. "
            "Do not repeat the exact command that already failed; change what was wrong, then finish and verify."
        )
