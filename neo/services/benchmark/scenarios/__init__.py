"""Scenario registry, grouped by the harness concern each module exercises.

The list preserves the historical execution order; append new scenarios to
the end so category rates in old artifacts stay comparable.
"""

from __future__ import annotations

from typing import List

from ..harness import Scenario
from .computer import COMPUTER_ACCESS_DENIED, VISION_SCREENSHOT
from .intent import STATUS_QUESTION_NO_SIDE_EFFECTS
from .runtime import ABANDONED_RUN_FINISH, LAUNCH_RECOVERY, NODE_SERVICE, PORT_CONFLICT_RECOVERY
from .safety import SAFETY_REFUSAL
from .security import SECURITY_AUDIT
from .truthfulness import FALSE_SUCCESS_REJECTION, NOOP_CHANGE_REJECTION
from .verification import HEALTHCHECK_GATE, HEALTHCHECK_PASS
from .workspace import EVIDENCE_GREP, ORGANIZE_WORKSPACE

SCENARIOS: List[Scenario] = [
    LAUNCH_RECOVERY,
    FALSE_SUCCESS_REJECTION,
    PORT_CONFLICT_RECOVERY,
    ORGANIZE_WORKSPACE,
    EVIDENCE_GREP,
    SECURITY_AUDIT,
    VISION_SCREENSHOT,
    COMPUTER_ACCESS_DENIED,
    STATUS_QUESTION_NO_SIDE_EFFECTS,
    NODE_SERVICE,
    NOOP_CHANGE_REJECTION,
    HEALTHCHECK_GATE,
    HEALTHCHECK_PASS,
    ABANDONED_RUN_FINISH,
    SAFETY_REFUSAL,
]

__all__ = ["SCENARIOS"]
