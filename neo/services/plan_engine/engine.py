"""The PlanEngine facade.

One object the runner wires up; every public method delegates to the
per-concern module that owns the behavior:

- intents.py     what the user asked for (classified intent + keyword fallback)
- divergence.py  guards against forking the shared team plan (dirs, ports)
- evidence.py    which observations prove which intents / plan steps
- steps.py       normalizing the model's plan into operational steps
- progress.py    plans-table persistence and progression
- completion.py  end-of-run blocking gates
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from ...db import Database
from .completion import CompletionGates
from .divergence import DivergenceGates
from .evidence import EvidenceContracts
from .intents import IntentDetectors, IntentRegistry
from .progress import EventSink, PlanProgressStore
from .steps import PlanStepNormalizer

__all__ = ["EventSink", "PlanEngine"]


class PlanEngine:
    def __init__(self, db: Database, event_sink: EventSink, workspace_getter: Callable[[], Any] | None = None) -> None:
        self.db = db
        self._event = event_sink
        self._registry = IntentRegistry()
        self._detectors = IntentDetectors(self._registry)
        self._divergence = DivergenceGates(db, workspace_getter)
        self._contracts = EvidenceContracts(self._detectors)
        self._steps = PlanStepNormalizer(self._detectors)
        self._progress = PlanProgressStore(db, event_sink)
        self._completion = CompletionGates(db, self._detectors)

    # ------------------------------------------------------------------
    # Classified intents (set by the runner after classification)
    # ------------------------------------------------------------------
    def note_intent(self, user_message: str, intent: Dict[str, Any] | None) -> None:
        self._registry.note(user_message, intent)

    def intent_for(self, user_message: str) -> Dict[str, Any] | None:
        return self._registry.get(user_message)

    # ------------------------------------------------------------------
    # Intent detection
    # ------------------------------------------------------------------
    def is_status_question(self, user_message: str) -> bool:
        return self._detectors.is_status_question(user_message)

    def goal_requires_work(self, user_message: str) -> bool:
        return self._detectors.goal_requires_work(user_message)

    def modification_required(self, user_message: str) -> bool:
        return self._detectors.modification_required(user_message)

    def organization_required(self, user_message: str) -> bool:
        return self._detectors.organization_required(user_message)

    def is_web_action(self, user_message: str) -> bool:
        return self._detectors.is_web_action(user_message)

    def is_desktop_gui_task(self, user_message: str) -> bool:
        return self._detectors.is_desktop_gui_task(user_message)

    def browser_required(self, user_message: str, plan: List[Any], final_text: str) -> bool:
        return self._detectors.browser_required(user_message, plan, final_text)

    def security_audit_required(self, user_message: str) -> bool:
        return self._detectors.security_audit_required(user_message)

    def screen_view_required(self, user_message: str) -> bool:
        return self._detectors.screen_view_required(user_message)

    def workspace_inventory_required(self, user_message: str) -> bool:
        return self._detectors.workspace_inventory_required(user_message)

    def coordination_status_required(self, user_message: str) -> bool:
        return self._detectors.coordination_status_required(user_message)

    # ------------------------------------------------------------------
    # Shared-plan divergence gates
    # ------------------------------------------------------------------
    def plan_divergence(self, observations: List[Dict[str, Any]]) -> str:
        return self._divergence.plan_divergence(observations)

    def port_divergence(self, observations: List[Dict[str, Any]]) -> str:
        return self._divergence.port_divergence(observations)

    # ------------------------------------------------------------------
    # Evidence injection
    # ------------------------------------------------------------------
    def required_tool_calls(
        self,
        user_message: str,
        observations: List[Dict[str, Any]],
        requested_calls: List[Any],
    ) -> List[Any]:
        return self._contracts.required_tool_calls(user_message, observations, requested_calls)

    # ------------------------------------------------------------------
    # Plan persistence and progression
    # ------------------------------------------------------------------
    def store_plan(self, run_id: int, plan: List[Any], user_message: str = "") -> List[str]:
        """Normalize/expand the model's plan, persist it, and return the stored
        step texts. The returned steps are the deterministic intent record:
        the runtime controller drives off them, not off the model's raw
        phrasing."""
        raw_steps = [str(step).strip() for step in plan[:12] if str(step).strip()]
        normalized = self._steps.operational_steps(raw_steps, user_message)
        if not normalized:
            return []
        self._progress.sync(run_id, normalized)
        return normalized

    def advance_for_observation(self, run_id: int, observation: Dict[str, Any]) -> None:
        self._progress.advance_for_observation(run_id, observation)

    def normalize_activation(self, run_id: int) -> None:
        self._progress.normalize_activation(run_id)

    def finalize_plan(
        self,
        run_id: int,
        observations: List[Dict[str, Any]],
        final_text: str,
        failed: bool = False,
    ) -> None:
        self._progress.finalize(run_id, observations, final_text, failed)

    # ------------------------------------------------------------------
    # Completion gating
    # ------------------------------------------------------------------
    def blocking_reason(
        self,
        run_id: int,
        user_message: str,
        plan: List[Any],
        final_text: str,
        observations: List[Dict[str, Any]],
    ) -> str:
        return self._completion.blocking_reason(run_id, user_message, plan, final_text, observations)

    def modification_satisfied(self, observations: List[Dict[str, Any]], final_text: str) -> bool:
        return self._completion.modification_satisfied(observations, final_text)

    def organization_satisfied(self, observations: List[Dict[str, Any]], final_text: str) -> bool:
        return self._completion.organization_satisfied(observations, final_text)
