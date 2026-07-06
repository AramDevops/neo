from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from ..db import Database
from .browser_verifier import BrowserVerifier
from .plan_engine import PlanEngine


EventSink = Callable[[int, str, Dict[str, Any]], None]

STATUS_COMPLETE = "complete"
STATUS_BLOCKED = "blocked"
STATUS_ERROR = "error"


@dataclass
class RunVerdict:
    """The single terminal statement about how a run ended.

    Run status, plan-step states, and the final message are all projections of
    this one object, so they can never contradict each other. A verdict is
    blocked if and only if it carries a blocked_reason, and a blocked verdict
    always has at least one non-complete plan step (enforced structurally by
    VerdictEngine before the verdict is built).
    """

    status: str
    final_text: str
    blocked_reason: str = ""
    error_text: str = ""
    extra_tool_count: int = 0
    plan: List[Dict[str, Any]] = field(default_factory=list)
    evidence: Dict[str, int] = field(default_factory=dict)

    def is_consistent(self) -> bool:
        if self.status == STATUS_BLOCKED:
            if not self.blocked_reason:
                return False
            if self.plan and all(step.get("status") == "complete" for step in self.plan):
                return False
            return self.blocked_reason in (self.final_text or "")
        if self.status == STATUS_COMPLETE:
            return not self.blocked_reason
        if self.status == STATUS_ERROR:
            return bool(self.error_text)
        return False

    def as_event(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "blocked_reason": self.blocked_reason,
            "error": self.error_text,
            "plan_total": len(self.plan),
            "plan_complete": sum(1 for step in self.plan if step.get("status") == "complete"),
            "evidence": self.evidence,
            "consistent": self.is_consistent(),
        }


class VerdictEngine:
    """Computes the RunVerdict for a finished agent loop.

    Everything that used to be scattered across run_turn (summary synthesis,
    browser verification, plan finalization, blocking detection, final-text
    notes) funnels through conclude(), which returns one verdict that the
    runner persists verbatim.
    """

    def __init__(
        self,
        db: Database,
        plan_engine: PlanEngine,
        browser_verifier: BrowserVerifier,
        event_sink: EventSink,
    ) -> None:
        self.db = db
        self.plan_engine = plan_engine
        self.browser_verifier = browser_verifier
        self._event = event_sink

    def conclude(
        self,
        run_id: int,
        user_message: str,
        plan: List[Any],
        candidate_final: str,
        observations: List[Dict[str, Any]],
        ended_after_tool_call: bool,
    ) -> RunVerdict:
        final_text = candidate_final
        if ended_after_tool_call and observations:
            final_text = self.action_summary(user_message, observations)
        elif self.looks_incomplete(final_text) and observations:
            final_text = self.observation_summary(observations)
        final_text = self.with_web_sources(final_text, observations)

        # A forked second app (a divergence from the shared plan) is a more
        # fundamental defect than "app not verified", so it is decided BEFORE
        # the browser tail - and the tail is skipped, since we must not start
        # or verify the divergent app.
        divergence = self.plan_engine.plan_divergence(observations) or self.plan_engine.port_divergence(observations)
        if divergence:
            extra_tool_count = 0
            self.plan_engine.finalize_plan(run_id, observations, final_text)
            blocked_reason = divergence
        else:
            final_text, extra_tool_count, blocked_reason = self.browser_verifier.ensure(
                run_id, user_message, plan, final_text, observations
            )
            self.plan_engine.finalize_plan(run_id, observations, final_text)
            blocked_reason = blocked_reason or self.plan_engine.blocking_reason(
                run_id, user_message, plan, final_text, observations
            )
        if blocked_reason:
            final_text = self.append_note(final_text, blocked_reason)
            self._enforce_blocked_plan_invariant(run_id, blocked_reason)

        verdict = RunVerdict(
            status=STATUS_BLOCKED if blocked_reason else STATUS_COMPLETE,
            final_text=final_text,
            blocked_reason=blocked_reason,
            extra_tool_count=extra_tool_count,
            plan=self._plan_snapshot(run_id),
            evidence=self._evidence_counts(observations),
        )
        self._event(run_id, "run_verdict", verdict.as_event())
        return verdict

    def error(self, run_id: int, exc: Exception) -> RunVerdict:
        self.plan_engine.finalize_plan(run_id, [], "", failed=True)
        verdict = RunVerdict(
            status=STATUS_ERROR,
            final_text="",
            error_text=str(exc) or type(exc).__name__,
            plan=self._plan_snapshot(run_id),
        )
        self._event(run_id, "run_verdict", verdict.as_event())
        return verdict

    def _plan_snapshot(self, run_id: int) -> List[Dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT id, step_text, status FROM plans WHERE run_id=? ORDER BY sort_order ASC",
            (run_id,),
        )
        return [dict(row) for row in rows]

    def _enforce_blocked_plan_invariant(self, run_id: int, blocked_reason: str) -> None:
        """A blocked run must show at least one non-complete plan step.

        The old contradiction ("plan all-green while status=blocked") happened
        because plan completion and blocking were computed by independent
        keyword systems. If they still disagree, the unmet requirement becomes
        a visible error step instead of a silent inconsistency.
        """
        rows = self.db.fetchall(
            "SELECT id, status FROM plans WHERE run_id=? ORDER BY sort_order ASC",
            (run_id,),
        )
        if rows and any(row.get("status") != "complete" for row in rows):
            return
        step_text = " ".join(f"Unmet requirement: {blocked_reason}".split())[:300]
        self.db.execute(
            "INSERT INTO plans (run_id, step_text, status, sort_order) VALUES (?, ?, ?, ?)",
            (run_id, step_text, "error", len(rows)),
        )
        self._event(run_id, "plan_progress", {
            "status": "error",
            "step_text": step_text,
            "reason": "verdict_invariant",
        })

    def _evidence_counts(self, observations: List[Dict[str, Any]]) -> Dict[str, int]:
        ok = sum(1 for item in observations if item.get("ok"))
        return {
            "observations": len(observations),
            "tool_ok": ok,
            "tool_failures": len(observations) - ok,
        }

    # ------------------------------------------------------------------ #
    # Final-text synthesis (moved verbatim from AgentRunner)             #
    # ------------------------------------------------------------------ #

    def append_note(self, final_text: str, note: str) -> str:
        clean = (final_text or "").rstrip()
        if "blocked" in note.lower() and clean.startswith("Completed the requested work"):
            clean = clean.replace("Completed the requested work using tool-backed evidence.", "Recorded partial tool-backed progress.", 1)
        if not clean:
            return note
        if note in clean:
            return clean
        return f"{clean}\n\n{note}"

    def looks_incomplete(self, text: str) -> bool:
        lowered = (text or "").strip().lower()
        if not lowered:
            return True
        starters = ("i will ", "i'll ", "let me ", "i need to ", "i am going to ")
        if lowered.startswith(starters):
            return True
        return lowered.startswith("tool results:")

    def observation_summary(self, observations: List[Dict[str, Any]]) -> str:
        lines = ["Tool results:"]
        for observation in observations[-5:]:
            status = "ok" if observation.get("ok") else "error"
            output = str(observation.get("output") or "").strip()
            if len(output) > 1200:
                output = output[:1200] + "... [truncated]"
            lines.append(f"- {observation.get('tool')} ({status}): {output}")
        return "\n".join(lines)

    def action_summary(self, user_message: str, observations: List[Dict[str, Any]]) -> str:
        changes: list[str] = []
        checks: list[str] = []
        errors: list[str] = []

        for observation in observations:
            tool = str(observation.get("tool") or "")
            ok = bool(observation.get("ok"))
            args = observation.get("args") if isinstance(observation.get("args"), dict) else {}
            meta = observation.get("meta") if isinstance(observation.get("meta"), dict) else {}
            if not ok:
                output = " ".join(str(observation.get("output") or "").split())
                errors.append(f"{tool}: {output[:180]}")
                continue

            if tool == "move_path":
                source = args.get("source") or meta.get("source") or "source"
                destination = args.get("destination") or meta.get("relative_path") or meta.get("destination") or "destination"
                changes.append(f"Moved {source} -> {destination}")
            elif tool == "make_dir":
                path = args.get("path") or meta.get("relative_path") or meta.get("path")
                changes.append(f"Created directory {path}")
            elif tool in {"write_file", "append_file", "edit_file", "download_url"}:
                path = args.get("path") or meta.get("relative_path") or meta.get("path")
                detail = []
                if meta.get("added") is not None:
                    detail.append(f"+{meta.get('added')}")
                if meta.get("removed") is not None:
                    detail.append(f"-{meta.get('removed')}")
                suffix = f" ({', '.join(detail)})" if detail else ""
                changes.append(f"Updated {path}{suffix}")
            elif tool == "start_process":
                name = meta.get("name") or "process"
                pid = meta.get("pid")
                listening = meta.get("listening_port")
                if listening:
                    changes.append(f"Started {name} (pid {pid}), verified listening on port {listening}")
                else:
                    changes.append(f"Started {name} (pid {pid})")
            elif tool == "stop_process":
                changes.append(f"Stopped process {args.get('pid') or meta.get('pid')}")
            elif tool == "computer_click":
                changes.append(f"Clicked at ({meta.get('x')}, {meta.get('y')})")
            elif tool == "computer_move":
                changes.append(f"Moved cursor to ({meta.get('x')}, {meta.get('y')})")
            elif tool == "computer_type":
                changes.append(f"Typed {meta.get('typed_chars', 0)} characters")
            elif tool == "computer_key":
                keys = meta.get("keys") or []
                changes.append(f"Pressed {'+'.join(keys) if isinstance(keys, list) else keys}")
            elif tool == "computer_scroll":
                changes.append(f"Scrolled {meta.get('amount')}")
            elif tool == "focus_window":
                changes.append(f"Focused window {meta.get('title')}")
            elif tool in {"tree", "list_files", "search_files", "grep", "file_info", "project_probe", "syntax_check", "run_tests", "http_get", "http_head", "open_browser", "screen_capture", "list_windows", "system_security_audit", "secrets_scan", "dependency_audit"}:
                checks.append(tool)

        if errors and not changes and not checks:
            header = "Attempted the requested work, but the tool steps failed. Details below."
        else:
            header = "Completed the requested work using tool-backed evidence."
        lines = [header]
        if changes:
            lines.append("Changes:")
            lines.extend(f"- {item}" for item in changes[:12])
        if checks:
            seen_checks = []
            for check in checks:
                if check not in seen_checks:
                    seen_checks.append(check)
            lines.append("Verified with: " + ", ".join(seen_checks[:8]) + ".")
        if errors:
            lines.append("Tool issues:")
            lines.extend(f"- {item}" for item in errors[:5])
        if not changes and not checks and not errors:
            lines.append("No material tool result was recorded.")
        return "\n".join(lines)

    def with_web_sources(self, text: str, observations: List[Dict[str, Any]]) -> str:
        sources = self.web_sources(observations)
        if not sources or "sources:" in (text or "").lower():
            return text
        lines = [text.rstrip(), "", "Sources:"]
        for source in sources[:6]:
            title = source.get("title") or source.get("url")
            url = source.get("url")
            if not url:
                continue
            lines.append(f"- {title}: {url}")
        return "\n".join(lines).strip()

    def web_sources(self, observations: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        web_tools = {"web_search", "web_fetch", "web_links", "scrape_page", "scrape_urls", "research_web", "http_head", "http_get", "download_url"}
        sources: List[Dict[str, str]] = []
        seen: set[str] = set()

        def add(url: Any, title: Any = "") -> None:
            clean_url = str(url or "").strip()
            if not clean_url.startswith(("http://", "https://")) or clean_url in seen:
                return
            seen.add(clean_url)
            clean_title = " ".join(str(title or "").split())[:160] or clean_url
            sources.append({"title": clean_title, "url": clean_url})

        for observation in observations:
            if observation.get("tool") not in web_tools:
                continue
            raw_output = observation.get("output")
            try:
                payload = json.loads(raw_output) if isinstance(raw_output, str) else raw_output
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                continue

            add(payload.get("url"), payload.get("title") or payload.get("search_title"))

            for page in payload.get("pages", []) or []:
                if isinstance(page, dict):
                    add(page.get("url"), page.get("title") or page.get("search_title"))

            # search_results and links are deliberately NOT cited: they are raw
            # listings the run never opened. Citing them put a paintball park
            # and two dictionaries on a text-editor build (run 65).

        return sources
