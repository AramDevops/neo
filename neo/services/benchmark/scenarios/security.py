"""Security scenario: the bounded audits must run even when the model stalls.

The scripted model would rather ask for clarification; the harness must
inject the host audit, secrets scan, and dependency audit deterministically
anyway.
"""

from __future__ import annotations

from ..harness import Scenario, observed, setup_noop


def _script_security_audit(turn: int, prompt: str, ctx: dict) -> dict:
    obs = observed(prompt)
    if '"tool": "system_security_audit"' not in obs:
        # A weak model that would rather ask for clarification; the harness
        # must inject the bounded audits deterministically anyway.
        return {"plan": ["Clarify the scope of the audit"], "tool_calls": [], "final": "", "needs_more": True}
    return {"plan": [], "tool_calls": [], "needs_more": False,
            "final": "Summarized findings, limits, and recommended actions from the host audit, secrets scan, and dependency audit."}


def _grade_security_audit(result: dict) -> tuple[bool, dict]:
    run = result["run"]
    tool_events = result["tool_events"]
    seen = {e["payload"].get("tool") for e in tool_events if e["payload"].get("ok")}
    checks = {
        "status_complete": run["status"] == "complete",
        "host_audit_ran": "system_security_audit" in seen,
        "secrets_scan_ran": "secrets_scan" in seen,
        "dependency_audit_ran": "dependency_audit" in seen,
        "verdict_consistent": result["verdict_consistent"] is True,
    }
    return all(checks.values()), checks


SECURITY_AUDIT = Scenario(
    id="security_audit",
    category="security",
    prompt="audit the current os security posture",
    expected_status="complete",
    setup=setup_noop,
    script=_script_security_audit,
    grade=_grade_security_audit,
)
