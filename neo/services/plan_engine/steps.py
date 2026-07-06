"""Normalizing the model's raw plan into operational, checkable steps.

A stored plan step must be provable by an observation (see evidence.py), so
report-only and shallow steps ("fix it", "handle the request") are rewritten
or expanded into inspect -> act -> verify shapes before persistence.
"""

from __future__ import annotations

from typing import List

from .intents import IntentDetectors


def dedupe_steps(steps: List[str]) -> List[str]:
    normalized: List[str] = []
    seen: set[str] = set()
    for step in steps:
        clean = " ".join(str(step or "").split())
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(clean)
    return normalized


def is_report_only_step(step: str) -> bool:
    lowered = step.lower().strip()
    return any(term in lowered for term in ["answer", "respond", "return", "summarize", "summarise", "explain", "report", "propose"]) and not any(
        term in lowered for term in ["http", "browser", "file", "test", "verify", "check", "read", "open", "move", "group", "edit", "write", "implement", "apply"]
    )


def is_shallow_step(step: str) -> bool:
    lowered = step.lower().strip(" .")
    if len(lowered.split()) <= 2 and any(term in lowered for term in ["fix", "build", "clean", "organize", "organise", "arrange", "arage", "improve"]):
        return True
    shallow_phrases = [
        "prepare", "think", "plan", "handle", "do the task", "complete the task", "make it better",
        "work on it", "address request", "address the request", "parse fallback", "return raw answer",
    ]
    return any(phrase in lowered for phrase in shallow_phrases)


def is_inspection_step(step: str) -> bool:
    lowered = step.lower()
    return any(term in lowered for term in ["inspect", "probe", "list", "read", "search", "grep", "audit", "diagnose", "investigate", "look at", "understand"])


def is_action_step(step: str) -> bool:
    lowered = step.lower()
    return any(term in lowered for term in [
        "create", "write", "edit", "update", "modify", "implement", "build", "move", "group", "relocate",
        "install", "start", "run", "launch", "open", "download", "configure", "refactor", "fix", "apply", "arrange",
    ])


def is_verification_step(step: str) -> bool:
    lowered = step.lower()
    return any(term in lowered for term in ["verify", "test", "check", "validate", "confirm", "open browser", "http", "responds", "resulting"])


class PlanStepNormalizer:
    def __init__(self, detectors: IntentDetectors) -> None:
        self._detectors = detectors

    def operational_steps(self, plan: List[str], user_message: str = "") -> List[str]:
        steps = [" ".join(step.split()) for step in plan if str(step).strip()]
        if not steps:
            if self._detectors.goal_requires_work(user_message):
                return dedupe_steps(self._expanded_plan(user_message, []))[:12]
            return []
        if not self._detectors.goal_requires_work(user_message):
            return dedupe_steps(steps)[:12]

        work_steps = [step for step in steps if not is_report_only_step(step)]
        if not work_steps:
            work_steps = steps
        if self._needs_expansion(work_steps, user_message):
            return dedupe_steps(self._expanded_plan(user_message, work_steps))[:12]

        rewritten = [self._rewrite_shallow_step(step, user_message) for step in work_steps]
        return dedupe_steps(rewritten)[:12]

    def _needs_expansion(self, steps: List[str], user_message: str) -> bool:
        if not steps:
            return True
        if self._detectors.security_audit_required(user_message):
            return True
        if any(is_report_only_step(step) for step in steps):
            return True
        if any(is_shallow_step(step) for step in steps):
            return True
        if not self._detectors.vague_work_required(user_message):
            return False
        return not (
            any(is_inspection_step(step) for step in steps)
            and any(is_action_step(step) for step in steps)
            and any(is_verification_step(step) for step in steps)
        )

    def _expanded_plan(self, user_message: str, existing_steps: List[str]) -> List[str]:
        lowered = user_message.lower()
        if self._detectors.security_audit_required(user_message):
            return [
                "Run the host OS security posture audit",
                "Scan the workspace for exposed secrets",
                "Run dependency security audit probes",
                "Summarize findings, limits, and recommended actions",
            ]
        if self._detectors.app_work_required(user_message):
            steps = ["Inspect the project structure and runtime requirements"]
            if any(term in lowered for term in ["build", "create", "make", "implement"]):
                steps.append("Create or edit the requested app files")
            steps.extend([
                "Check the intended localhost port",
                "Start the app on a verified available port",
                "Verify the app URL responds",
                "Open the browser to the verified URL",
            ])
            return steps
        if self._detectors.organization_required(user_message):
            return [
                "Inspect the workspace tree and relevant files",
                "Move or group relevant files into the intended structure",
                "Verify the resulting workspace tree",
            ]
        if any(term in lowered for term in ["fix", "debug", "bug", "error", "broken", "repair"]):
            return [
                "Inspect relevant files and errors for the requested fix",
                "Edit the relevant implementation to fix the requested behavior",
                "Run a targeted verification for the requested change",
            ]
        if any(term in lowered for term in ["clean", "improve", "better", "usable", "logical", "industry-grade", "refactor"]):
            return [
                "Inspect the relevant workspace files",
                "Edit or refactor the relevant files for the requested improvement",
                "Run a focused verification for the improvement",
            ]
        action_step = self._rewrite_shallow_step(existing_steps[0], user_message) if existing_steps else "Apply the requested workspace change"
        if is_report_only_step(action_step):
            action_step = "Apply the requested workspace change"
        return [
            "Inspect the relevant workspace state",
            action_step,
            "Verify the resulting state",
        ]

    def _rewrite_shallow_step(self, step: str, user_message: str) -> str:
        if not is_shallow_step(step):
            return step
        lowered = " ".join([step, user_message]).lower()
        if self._detectors.organization_required(lowered):
            return "Move or group relevant files into the intended structure"
        if self._detectors.security_audit_required(lowered):
            return "Run the host OS security posture audit"
        if any(term in lowered for term in ["fix", "debug", "bug", "error", "broken", "repair"]):
            return "Edit the relevant implementation to fix the requested behavior"
        if self._detectors.app_work_required(lowered):
            return "Create or edit the requested app files"
        if any(term in lowered for term in ["clean", "improve", "better", "usable", "logical", "industry-grade", "refactor"]):
            return "Edit or refactor the relevant files for the requested improvement"
        return "Apply the requested workspace change"
