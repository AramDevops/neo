"""End-of-run blocking gates.

blocking_reason decides whether a run may finish; the *_satisfied checks say
whether the evidence backs the specific kind of work the user asked for.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ...db import Database
from .evidence import NO_MOVE_NEEDED_PHRASES
from .intents import IntentDetectors

_NO_CHANGE_PHRASES = (
    "no change was needed", "no changes were needed", "no change needed", "no changes needed",
    "already has", "already exists", "already implemented", "already supports", "nothing to change",
)


class CompletionGates:
    def __init__(self, db: Database, detectors: IntentDetectors) -> None:
        self.db = db
        self._detectors = detectors

    def blocking_reason(
        self,
        run_id: int,
        user_message: str,
        plan: List[Any],
        final_text: str,
        observations: List[Dict[str, Any]],
    ) -> str:
        # A failing outcome check outranks every shape-based signal: the health
        # check tested the ACTUAL app, so if the last one failed, the run is not
        # done no matter which tools happened to fire.
        last_health = next(
            (item for item in reversed(observations) if item.get("tool") == "app_healthcheck"),
            None,
        )
        if last_health is not None and not last_health.get("ok"):
            meta = last_health.get("meta") if isinstance(last_health.get("meta"), dict) else {}
            summary = str(meta.get("summary") or last_health.get("output") or "checks failing")
            return f"App health check failing: {summary[:400]}"
        # Same principle for the test suite, but ONLY for a genuine test
        # FAILURE (runner ran and a test failed: returncode 1). "No tests
        # collected" (pytest exit 5), a missing test script, or a missing
        # runner all return ok=False with a non-1 (or absent) returncode and
        # must NOT block, otherwise "do we have tests?" can never be answered
        # and auto-recovery burns attempts on an unfixable non-failure.
        last_tests = next(
            (item for item in reversed(observations) if item.get("tool") == "run_tests"),
            None,
        )
        if last_tests is not None and not last_tests.get("ok"):
            meta = last_tests.get("meta") if isinstance(last_tests.get("meta"), dict) else {}
            if meta.get("returncode") == 1:
                tail = str(last_tests.get("output") or "test run failed")
                return f"Tests failing: the last run_tests call reported failing tests. Fix them and rerun. Evidence: {tail[-400:]}"
        runtime_started = any(item.get("ok") and item.get("tool") == "start_process" for item in observations)
        failed_http = any((not item.get("ok")) and item.get("tool") in {"http_get", "http_head"} for item in observations)
        verified_http = any(item.get("ok") and item.get("tool") in {"http_get", "http_head"} for item in observations)
        if runtime_started and failed_http and not verified_http:
            return "App verification blocked: the process was started, but no successful HTTP check was recorded."
        if self._detectors.browser_required(user_message, plan, final_text) and not any(item.get("ok") and item.get("tool") == "open_browser" for item in observations):
            return "Browser verification blocked: no successful browser open was recorded."
        if self._detectors.modification_required(user_message) and not self.modification_satisfied(observations, final_text):
            return (
                "Change verification blocked: the request asks to add/upgrade/implement something, "
                "but the run made no changes at all. Implement the change with the file tools, verify it, "
                "and only then finish, or state explicitly why no change was needed."
            )
        if self._detectors.organization_required(user_message) and not self.organization_satisfied(observations, final_text):
            return "Workspace organization incomplete: the run did not move/group any existing files or verify that there was nothing relevant to move."
        rows = self.db.fetchall("SELECT step_text, status FROM plans WHERE run_id=? ORDER BY sort_order ASC", (run_id,))
        unfinished = [
            row for row in rows
            if row.get("status") in {"pending", "in_progress", "error"}
            and not any(term in str(row.get("step_text") or "").lower() for term in ["answer", "respond", "return", "summarize", "explain"])
        ]
        if unfinished and self._detectors.goal_requires_work(user_message):
            return "Run blocked with unfinished evidence-backed plan steps: " + "; ".join(str(row.get("step_text") or "") for row in unfinished[:3])
        return ""

    def modification_satisfied(self, observations: List[Dict[str, Any]], final_text: str) -> bool:
        change_tools = {
            "write_file", "append_file", "edit_file", "make_dir", "move_path", "delete_path",
            "copy_path", "download_url", "write_artifact", "python", "powershell", "wsl", "context_write",
        }
        if any(item.get("ok") and item.get("tool") in change_tools for item in observations):
            return True
        # An honest "nothing needed changing" is acceptable, but only when
        # backed by inspection evidence, mirroring the organization gate.
        inspected = any(item.get("ok") and item.get("tool") in {"tree", "list_files", "read_file", "project_probe", "grep", "search_files"} for item in observations)
        final_lower = (final_text or "").lower()
        return inspected and any(phrase in final_lower for phrase in _NO_CHANGE_PHRASES)

    def organization_satisfied(self, observations: List[Dict[str, Any]], final_text: str) -> bool:
        if any(item.get("ok") and item.get("tool") == "move_path" for item in observations):
            return True
        inspected = any(item.get("ok") and item.get("tool") in {"tree", "list_files", "search_files", "project_probe", "file_info"} for item in observations)
        final_lower = (final_text or "").lower()
        no_move_needed = any(phrase in final_lower for phrase in NO_MOVE_NEEDED_PHRASES)
        return inspected and no_move_needed
