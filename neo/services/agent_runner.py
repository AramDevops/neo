"""AgentRunner: the composition root and lifecycle owner for agent turns.

The moving parts live in neo/services/runner/ (stop signal, turn state, run
journal, prompt building, model I/O, the turn loop, recovery policy); the
sibling engines (PlanEngine, VerdictEngine, BrowserVerifier,
RuntimeController) own plan/verdict/browser behavior. This module wires them
together and owns the run lifecycle: open -> loop -> exactly one finalize
(verdict / stopped / error).

Compatibility contract (do not break):
- tests import AgentRunner and PARSE_RETRY_MAX from this module;
- tests monkeypatch `neo.services.agent_runner.get_provider` (the default
  provider path must resolve this module's global at call time) and
  `neo.services.agent_runner.time.sleep`;
- tests call the private _-methods kept in the wrapper block at the bottom,
  and patch _project_plan_section / run_turn on the class or instance;
- `_stop_requests` is the live stop-registry set, mutated directly by tests.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from ..config import Settings
from ..db import Database
from ..providers import get_provider
from ..tools.base import clear_run_context, parse_scope_paths, set_run_context
from .agent_identity import agent_label
from .browser_verifier import BrowserVerifier
from .checkpoints import CheckpointStore
from .intent import classify_intent
from .plan_engine import PlanEngine
from .provider_runtime import default_engine
from .runtime_controller import RuntimeController
from .runner.journal import RunJournal, utc_stamp
from .runner.loop import MAX_TOOL_CALLS_PER_LOOP, PARSE_RETRY_MAX, TurnLoop
from .runner.model_io import PROVIDER_RETRY_MAX, ModelClient
from .runner.prompting import OBSERVATION_CHAR_BUDGET, PromptBuilder, budgeted_observations, clip, compact_args
from .runner.recovery import RecoveryPolicy
from .runner.state import TurnState
from .runner.stop import RunStopRequested, StopSignal
from .tools import Toolbox
from .verdict import VerdictEngine

__all__ = [
    "AgentRunner",
    "MAX_TOOL_CALLS_PER_LOOP",
    "OBSERVATION_CHAR_BUDGET",
    "PARSE_RETRY_MAX",
    "PROVIDER_RETRY_MAX",
    "RunStopRequested",
    "TurnState",
    "utc_stamp",
]


class AgentRunner:
    def __init__(
        self,
        db: Database | None = None,
        toolbox: Toolbox | None = None,
        provider_factory: Any | None = None,
    ) -> None:
        self.db = db or Database()
        self.toolbox = toolbox or Toolbox()
        self.provider_factory = provider_factory
        self.journal = RunJournal(self.db)
        self.stops = StopSignal()
        # Test-visible alias: the SAME set object the signal guards.
        self._stop_requests = self.stops.requests
        self.checkpoints = CheckpointStore(lambda: self.toolbox.workspace)
        self.plan_engine = PlanEngine(self.db, self._event, workspace_getter=lambda: self.toolbox.workspace)
        self.runtime_controller = RuntimeController(self.plan_engine, self.toolbox)
        self.browser_verifier = BrowserVerifier(
            self.toolbox,
            self.plan_engine,
            self._event,
            self._advance_plan_for_observation,
            runtime_controller=self.runtime_controller,
        )
        self.verdict_engine = VerdictEngine(
            self.db,
            self.plan_engine,
            self.browser_verifier,
            self._event,
        )
        self.prompts = PromptBuilder(self.db, self.toolbox)
        self.model = ModelClient(self._event, self._check_stop)
        self.recovery = RecoveryPolicy(self.db)
        self.turn_loop = TurnLoop(
            toolbox=self.toolbox,
            plan_engine=self.plan_engine,
            runtime_controller=self.runtime_controller,
            model=self.model,
            # Late-bound through self so class/instance patches of the
            # runner's prompt methods keep steering live loops.
            build_prompt=lambda agent_id, msg, obs: self._build_prompt(agent_id, msg, obs),
            event=self._event,
            check_stop=self._check_stop,
        )

    # ------------------------------------------------------------------ #
    # Stop control                                                       #
    # ------------------------------------------------------------------ #

    def request_stop(self, agent_id: int) -> Dict[str, Any] | None:
        """Ask the agent's live run to stop at its next checkpoint.

        Returns the running run row when a stop was registered, or None when
        the agent has no live run. The stop is cooperative: a model call or
        tool call already in flight finishes first, then the loop ends the run
        as "stopped" instead of taking another step."""
        run = self.db.fetchone(
            "SELECT id FROM runs WHERE agent_id=? AND status=? ORDER BY id DESC LIMIT 1",
            (agent_id, "running"),
        )
        if not run:
            return None
        self.stops.request(int(run["id"]))
        self._event(run["id"], "stop_requested", {"agent_id": agent_id})
        return run

    def _consume_stop(self, run_id: int) -> bool:
        return self.stops.consume(run_id)

    def _check_stop(self, run_id: int) -> None:
        self.stops.check(run_id)

    # ------------------------------------------------------------------ #
    # Run lifecycle                                                      #
    # ------------------------------------------------------------------ #

    def run_turn(
        self,
        agent_id: int,
        user_message: str,
        provider_name: str | None = None,
        model: str | None = None,
    ) -> int:
        """One agent turn, as an explicit lifecycle: open -> loop -> finalize.

        Exactly one finalize runs (verdict / stopped / error). Each phase reads
        and writes the single TurnState; the run context is always cleared."""
        engine = default_engine()
        provider_name = provider_name or engine["provider"]
        model = model or engine["model"]
        agent = self.db.fetchone("SELECT * FROM agents WHERE id=?", (agent_id,)) or {}
        agent_name = agent_label(agent_id, agent)
        run_id = self.journal.open_run(agent_id, agent_name, user_message, provider_name, model)

        start = time.perf_counter()
        state = TurnState()
        # Carry this run's identity into the shared Toolbox (thread-local):
        # tools use it for shared-context attribution, process ownership, and
        # write-scope enforcement.
        set_run_context(
            agent_id=agent_id,
            agent_name=agent_name,
            run_id=run_id,
            scope_paths=self._scope_list(agent.get("scope_paths")),
        )
        try:
            provider = (self.provider_factory or get_provider)(provider_name, model)
            # Language-independent intent: one bounded structured call replaces
            # the keyword gates as the PRIMARY intent signal (French, Arabic,
            # typos, pasted error noise all classify correctly); keywords stay
            # as the fallback when this returns None.
            intent = self._classify_intent(user_message, provider_name, model)
            self.plan_engine.note_intent(user_message, intent)
            if intent:
                self._event(run_id, "intent", dict(intent))
            self._checkpoint_run_start(run_id, user_message)
            self.turn_loop.run(run_id, agent_id, user_message, provider, state)
            self._finalize_verdict(run_id, agent_id, agent_name, user_message, state, start)
        except RunStopRequested:
            self._finalize_stopped(run_id, agent_id, agent_name, user_message, state, start)
        except Exception as exc:
            self._finalize_error(run_id, agent_id, exc, start)
        finally:
            clear_run_context()
        return run_id

    def run_turn_resilient(
        self,
        agent_id: int,
        user_message: str,
        provider_name: str | None = None,
        model: str | None = None,
    ) -> int:
        """Run a turn, then auto-retry a blocked run with the failure evidence.

        Two independent stop conditions prevent an infinite loop:
        1. a hard attempt cap (Settings.auto_recovery_max), and
        2. a no-progress guard: if the follow-up run is blocked in the exact
           same way (same normalized reason + same failing tools), stop.
        Only recoverable blockers (app-run/start/verify failures) are retried;
        safety refusals and clarification requests are left alone.
        """
        run_id = self.run_turn(agent_id, user_message, provider_name, model)
        attempts = 0
        last_signature: str | None = None
        max_attempts = max(0, int(Settings.auto_recovery_max))
        while attempts < max_attempts:
            # A stop that lands after the loop's last checkpoint (e.g. during
            # the verdict tail) leaves the run blocked/complete but must still
            # kill auto-recovery; the user asked for the work to end.
            stop_pending = self._consume_stop(run_id)
            run = self.db.fetchone("SELECT status, error_text FROM runs WHERE id=?", (run_id,)) or {}
            if str(run.get("status") or "") != "blocked":
                break
            reason = str(run.get("error_text") or "")
            if stop_pending:
                self._event(run_id, "auto_recovery_stopped", {"reason": "stopped_by_user", "blocker": reason[:300]})
                break
            if not self.recovery.blocker_is_recoverable(reason):
                break
            signature = self.recovery.signature(run_id, reason)
            if signature == last_signature:
                self._event(run_id, "auto_recovery_stopped", {"reason": "no_progress", "blocker": reason[:300]})
                break
            last_signature = signature
            attempts += 1
            self._event(run_id, "auto_recovery", {"attempt": attempts, "max": max_attempts, "blocker": reason[:300]})
            recovery_prompt = self.recovery.prompt(user_message, reason)
            run_id = self.run_turn(agent_id, recovery_prompt, provider_name, model)
        # Drop any stop request the loop exits never consumed (stop landed in
        # the final run's tail) so it can never bleed into a future run.
        self._consume_stop(run_id)
        return run_id

    # ------------------------------------------------------------------ #
    # Finalize phases (exactly one runs per turn)                        #
    # ------------------------------------------------------------------ #

    def _finalize_verdict(
        self,
        run_id: int,
        agent_id: int,
        agent_name: str,
        user_message: str,
        state: TurnState,
        start: float,
    ) -> None:
        """Conclude via the verdict engine and persist the complete/blocked run."""
        verdict = self.verdict_engine.conclude(
            run_id,
            user_message,
            state.latest_plan,
            state.final_text,
            state.observations,
            state.ended_after_tool_call,
        )
        state.tool_count += verdict.extra_tool_count
        state.final_text = verdict.final_text
        blocked_reason = verdict.blocked_reason
        latency_ms = int((time.perf_counter() - start) * 1000)
        self.journal.finalize_terminal(
            run_id, agent_id, state, latency_ms,
            status=verdict.status,
            final_text=state.final_text,
            error_text=blocked_reason or None,
            agent_status="blocked" if blocked_reason else "idle",
            shared_note=f"{agent_name} {'blocked' if blocked_reason else 'completed'}: {state.final_text[:900]}",
            shared_importance=4 if blocked_reason else 3,
            event_type="run_blocked" if blocked_reason else "run_complete",
            event_extra={"reason": blocked_reason} if blocked_reason else {},
        )

    def _finalize_stopped(
        self,
        run_id: int,
        agent_id: int,
        agent_name: str,
        user_message: str,
        state: TurnState,
        start: float,
    ) -> None:
        """User-requested stop: never the error path, never the verdict path
        (no browser tail, no auto-start, no blocking). Report what actually
        ran, mark the run stopped, and free the terminal."""
        self._consume_stop(run_id)
        latency_ms = int((time.perf_counter() - start) * 1000)
        final_text = "Run stopped by user."
        summary = self.verdict_engine.observation_summary(state.observations)
        if summary:
            final_text += f"\nWork completed before the stop:\n{summary}"
        state.final_text = final_text
        self.journal.finalize_terminal(
            run_id, agent_id, state, latency_ms,
            status="stopped",
            final_text=final_text,
            error_text="Run stopped by user.",
            agent_status="idle",
            shared_note=f"{agent_name} was stopped by the user before finishing: {user_message[:200]}",
            shared_importance=2,
            event_type="run_stopped",
            event_extra={},
        )

    def _finalize_error(self, run_id: int, agent_id: int, exc: Exception, start: float) -> None:
        """The exception path: record the error verdict and mark the run failed."""
        latency_ms = int((time.perf_counter() - start) * 1000)
        self.verdict_engine.error(run_id, exc)
        self.journal.finalize_error(run_id, agent_id, exc, latency_ms)

    # ------------------------------------------------------------------ #
    # Turn-start hooks                                                   #
    # ------------------------------------------------------------------ #

    def _checkpoint_run_start(self, run_id: int, user_message: str) -> None:
        """Snapshot the workspace before a run's edits so it can be rolled
        back. Skipped for pure status questions (no edits to undo) and always
        best-effort: a checkpoint failure never affects the run."""
        try:
            if not self.checkpoints.enabled():
                return
            # Skip ONLY genuine read-only questions. A destructive request
            # phrased as a question ("how about you delete the old configs?")
            # has wants_changes=true and must still be snapshotted, or its
            # edits become unrecoverable.
            intent = self.plan_engine.intent_for(user_message)
            wants_changes = bool(intent and intent.get("wants_changes"))
            if self.plan_engine.is_status_question(user_message) and not wants_changes:
                return
            agent = self.db.fetchone("SELECT name FROM agents WHERE id=(SELECT agent_id FROM runs WHERE id=?)", (run_id,)) or {}
            who = agent.get("name") or "agent"
            snapshot = self.checkpoints.checkpoint(f"run {run_id} ({who}): {user_message.strip()[:80]}")
            if snapshot:
                self.db.execute("UPDATE runs SET checkpoint_id=? WHERE id=?", (snapshot["id"], run_id))
                self._event(run_id, "checkpoint", {"checkpoint_id": snapshot["id"], "label": snapshot["label"]})
        except Exception:
            pass

    def _classify_intent(self, user_message: str, provider_name: str | None, model: str | None) -> Dict[str, Any] | None:
        """One bounded classification call on a FRESH provider instance.

        Skipped for DI provider factories (benchmark's scripted providers must
        not have a response consumed by classification) and for the mock
        provider. Any failure means None -> keyword fallback; a broken
        classifier can never break a run."""
        if self.provider_factory is not None:
            return None
        resolved = (provider_name or Settings.provider or "").strip().lower()
        if resolved in {"", "mock"}:
            return None
        try:
            provider = get_provider(provider_name, model)
            # Route the classification call through the capability gate so a
            # provider with native JSON mode returns strict JSON here too -
            # this is the call whose whole purpose is coercing a small model
            # into a parseable object.
            return classify_intent(lambda text: self.model.call(provider, text), user_message)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Compatibility wrappers. Tests (and a few internal callbacks) drive #
    # the runner through these names; the behavior lives in the runner/  #
    # package and the plan/verdict/browser engines.                      #
    # ------------------------------------------------------------------ #

    # Prompting.
    def _build_prompt(self, agent_id: int, user_message: str, observations: List[Dict[str, Any]]) -> str:
        return self.prompts.build(agent_id, user_message, observations, plan_section=self._project_plan_section())

    def _project_plan_section(self) -> str | None:
        return self.prompts.project_plan_section()

    def _agent_names(self) -> Dict[int, str]:
        return self.prompts.agent_names()

    def _clip(self, text: Any, limit: int) -> str:
        return clip(text, limit)

    def _compact_args(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return compact_args(args)

    def _budgeted_observations(self, observations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return budgeted_observations(observations)

    def _scope_list(self, raw: Any) -> List[str]:
        return parse_scope_paths(raw)

    # Model I/O.
    def _call_provider(self, provider: Any, prompt: str, images: List[str] | None) -> Any:
        return self.model.call(provider, prompt, images)

    def _generate_with_retries(self, provider: Any, prompt: str, run_id: int, loop: int, images: List[str] | None = None) -> Any:
        return self.model.generate_with_retries(provider, prompt, run_id, loop, images)

    def _parse_model_json(self, text: str) -> Dict[str, Any]:
        return self.model.parse_model_json(text)

    def _loads_json_object(self, text: str) -> Dict[str, Any] | None:
        return self.model.loads_json_object(text)

    def _retryable_provider_error(self, exc: Exception) -> bool:
        return self.model.retryable_provider_error(exc)

    def _short_error(self, exc: Exception) -> str:
        return self.model.short_error(exc)

    # Auto-recovery policy.
    def _blocker_is_recoverable(self, reason: str) -> bool:
        return self.recovery.blocker_is_recoverable(reason)

    def _recovery_signature(self, run_id: int, reason: str) -> str:
        return self.recovery.signature(run_id, reason)

    def _recovery_prompt(self, original: str, reason: str) -> str:
        return self.recovery.prompt(original, reason)

    # Journal.
    def _event(self, run_id: int, event_type: str, payload: Dict[str, Any]) -> None:
        self.journal.event(run_id, event_type, payload)

    def _write_run_artifact(self, run_id: int, observations: List[Dict[str, Any]], error: str | None = None) -> str:
        return self.journal.write_artifact(run_id, observations, error)

    # Plan behavior (lives in PlanEngine / RuntimeController).
    def _required_tool_calls(
        self,
        user_message: str,
        observations: List[Dict[str, Any]],
        requested_calls: List[Any],
        plan: List[Any] | None = None,
    ) -> List[Any]:
        return self.runtime_controller.required_tool_calls(user_message, observations, requested_calls, plan)

    def _store_plan(self, run_id: int, plan: List[Any], user_message: str = "") -> List[str]:
        return self.plan_engine.store_plan(run_id, plan, user_message)

    def _advance_plan_for_observation(self, run_id: int, observation: Dict[str, Any]) -> None:
        self.plan_engine.advance_for_observation(run_id, observation)

    def _activate_next_plan_step(self, run_id: int) -> None:
        self.plan_engine.normalize_activation(run_id)

    def _finalize_plan(self, run_id: int, observations: List[Dict[str, Any]], final_text: str, failed: bool = False) -> None:
        self.plan_engine.finalize_plan(run_id, observations, final_text, failed)

    def _blocking_plan_reason(self, run_id: int, user_message: str, plan: List[Any], final_text: str, observations: List[Dict[str, Any]]) -> str:
        return self.plan_engine.blocking_reason(run_id, user_message, plan, final_text, observations)

    def _goal_requires_work(self, user_message: str) -> bool:
        return self.plan_engine.goal_requires_work(user_message)

    def _organization_required(self, user_message: str) -> bool:
        return self.plan_engine.organization_required(user_message)

    def _organization_satisfied(self, observations: List[Dict[str, Any]], final_text: str) -> bool:
        return self.plan_engine.organization_satisfied(observations, final_text)

    def _browser_required(self, user_message: str, plan: List[Any], final_text: str) -> bool:
        return self.plan_engine.browser_required(user_message, plan, final_text)

    def _ensure_browser_requirement(
        self,
        run_id: int,
        user_message: str,
        plan: List[Any],
        final_text: str,
        observations: List[Dict[str, Any]],
    ) -> tuple[str, int, str]:
        return self.browser_verifier.ensure(run_id, user_message, plan, final_text, observations)

    def _continue_without_tools_reason(
        self,
        user_message: str,
        plan: List[Any],
        final_text: str,
        observations: List[Dict[str, Any]],
    ) -> str:
        return self.runtime_controller.continue_without_tools_reason(user_message, plan, final_text, observations)

    # Final-text synthesis (lives in VerdictEngine).
    def _append_note(self, final_text: str, note: str) -> str:
        return self.verdict_engine.append_note(final_text, note)

    def _looks_incomplete(self, text: str) -> bool:
        return self.verdict_engine.looks_incomplete(text)

    def _observation_summary(self, observations: List[Dict[str, Any]]) -> str:
        return self.verdict_engine.observation_summary(observations)

    def _action_summary(self, user_message: str, observations: List[Dict[str, Any]]) -> str:
        return self.verdict_engine.action_summary(user_message, observations)

    def _with_web_sources(self, text: str, observations: List[Dict[str, Any]]) -> str:
        return self.verdict_engine.with_web_sources(text, observations)

    def _web_sources(self, observations: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        return self.verdict_engine.web_sources(observations)

    def _best_app_url(self, user_message: str, final_text: str, observations: List[Dict[str, Any]]) -> str:
        return self.browser_verifier.best_app_url(user_message, final_text, observations)
