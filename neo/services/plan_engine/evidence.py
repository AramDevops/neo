"""Which observations prove what.

Two layers live here:

- EvidenceContracts injects the tool calls a detected observation intent
  needs BEFORE the model answers (screen questions need a screen_capture,
  inventory questions need a tree, ...).
- step_status / consume_evidence map a plan step's text onto the observation
  that completes it, consuming each observation at most once so one lucky
  tool call cannot prove several steps.
"""

from __future__ import annotations

from typing import Any, Dict, List

import re

from .intents import IntentDetectors

# Final-text phrases that count as an honest "there was nothing to move",
# shared by the organization step matcher and the organization gate.
NO_MOVE_NEEDED_PHRASES = (
    "nothing to move", "no files to move", "no relevant files", "already organized", "already organised",
)


class EvidenceContracts:
    """Evidence contracts: each observation intent names the observations
    that count as an answer (any tool in the satisfied_by set) and the call
    to inject when that evidence is missing. Attempted-but-failed evidence is
    never re-injected; the model must report the failure, not loop. Extend by
    adding a row, not a keyword block."""

    def __init__(self, detectors: IntentDetectors) -> None:
        self._detectors = detectors

    def required_tool_calls(
        self,
        user_message: str,
        observations: List[Dict[str, Any]],
        requested_calls: List[Any],
    ) -> List[Any]:
        """Guarantee the evidence each detected intent needs, regardless of
        what the model requested. One declarative mechanism for every
        observation intent; the recurring failure class was each intent
        getting its own ad-hoc keyword block (or none, and a question like
        "what apps we have" being answered from a single narrow tool)."""
        calls = [call for call in requested_calls if isinstance(call, dict)]
        attempted = {str(item.get("tool") or "") for item in observations}
        queued = {str(call.get("tool") or "") for call in calls}
        required: list[dict[str, Any]] = []
        for rule in self._rules():
            if not rule["detect"](user_message):
                continue
            for satisfied_by, call in rule["evidence"]:
                if satisfied_by & attempted or satisfied_by & queued:
                    continue
                if any(str(existing.get("tool")) == str(call.get("tool")) for existing in required):
                    continue
                required.append(dict(call))
        return [*required, *calls]

    def _rules(self) -> List[Dict[str, Any]]:
        detect = self._detectors
        return [
            {"name": "screen_view", "detect": detect.screen_view_required, "evidence": [
                (frozenset({"screen_capture"}), {"tool": "screen_capture", "args": {}}),
            ]},
            {"name": "coordination_status", "detect": detect.coordination_status_required, "evidence": [
                (frozenset({"list_agents"}), {"tool": "list_agents", "args": {}}),
                (frozenset({"context_read"}), {"tool": "context_read", "args": {"limit": 30}}),
            ]},
            {"name": "workspace_inventory", "detect": detect.workspace_inventory_required, "evidence": [
                (frozenset({"tree", "list_files", "project_probe"}), {"tool": "tree", "args": {"path": ".", "max_depth": 2}}),
                (frozenset({"list_processes"}), {"tool": "list_processes", "args": {}}),
            ]},
            {"name": "security_audit", "detect": detect.security_audit_required, "evidence": [
                (frozenset({"system_security_audit"}), {"tool": "system_security_audit", "args": {"scope": "host"}}),
                (frozenset({"secrets_scan"}), {"tool": "secrets_scan", "args": {"path": ".", "max_files": 500}}),
                (frozenset({"dependency_audit"}), {"tool": "dependency_audit", "args": {"path": ".", "ecosystem": "auto"}}),
            ]},
        ]


def consume_evidence(
    observations: List[Dict[str, Any]],
    used_evidence: set[int],
    allowed_tools: set[str] | None,
    require_ok: bool = True,
    predicate: Any | None = None,
) -> bool:
    for index, observation in enumerate(observations):
        if index in used_evidence:
            continue
        tool = str(observation.get("tool") or "")
        if allowed_tools is not None and tool not in allowed_tools:
            continue
        if require_ok and not observation.get("ok"):
            continue
        if predicate and not predicate(observation):
            continue
        used_evidence.add(index)
        return True
    return False


def _is_port_evidence(observation: Dict[str, Any]) -> bool:
    tool = str(observation.get("tool") or "")
    meta = observation.get("meta") or {}
    if tool in {"project_probe", "port_check", "find_free_port"}:
        return True
    if tool == "start_process" and meta.get("port_conflicts"):
        return True
    return False


def looks_incomplete(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return True
    starters = ("i will ", "i'll ", "let me ", "i need to ", "i am going to ")
    if lowered.startswith(starters):
        return True
    return lowered.startswith("tool results:")


def step_status(
    step_text: str,
    observations: List[Dict[str, Any]],
    used_evidence: set[int],
    final_text: str,
) -> str:
    """Map one plan step onto its completing observation: "complete" when a
    matching, unconsumed observation exists, "pending" otherwise."""
    lowered = step_text.lower()
    if "vscode" in lowered:
        return "complete" if consume_evidence(observations, used_evidence, {"open_vscode"}) else "pending"
    if any(term in lowered for term in ["browser", "open browser"]):
        return "complete" if consume_evidence(observations, used_evidence, {"open_browser"}) else "pending"
    if re.search(r"\blogs?\b|\blogfiles?\b|\.log\b", lowered):
        # Word-bounded on purpose: a bare "log" substring swallowed steps
        # like "Verify the LOGin feature" into the read-the-logs bucket,
        # which then demanded read_file evidence a login check never has.
        return "complete" if consume_evidence(observations, used_evidence, {"read_file", "process_status"}) else "pending"
    if any(term in lowered for term in ["organize", "organise", "move", "relocate", "group", "put into", "place into"]) or (
        "clean" in lowered and any(term in lowered for term in ["workspace", "folder", "directory", "files", "project"])
    ):
        if consume_evidence(observations, used_evidence, {"move_path"}):
            return "complete"
        no_move_needed = any(phrase in (final_text or "").lower() for phrase in NO_MOVE_NEEDED_PHRASES)
        if no_move_needed and consume_evidence(observations, used_evidence, {"tree", "list_files", "search_files", "project_probe", "file_info"}):
            return "complete"
        return "pending"
    words = set(re.findall(r"[a-z0-9]+", lowered))
    if "start" in words or "serve" in words or any(term in lowered for term in ["launch server", "run server", "backend process", "frontend process"]):
        return "complete" if consume_evidence(observations, used_evidence, {"start_process"}) else "pending"
    if "run" in lowered and any(term in lowered for term in ["app", "server", "backend", "frontend", "flask", "vite", "node"]):
        return "complete" if consume_evidence(observations, used_evidence, {"start_process"}) else "pending"
    if "free port" in lowered or ("find" in lowered and "port" in lowered):
        return "complete" if consume_evidence(observations, used_evidence, {"find_free_port"}) else "pending"
    if "port" in lowered and any(term in lowered for term in ["check", "occupied", "available", "conflict", "preflight", "intended", "default"]):
        return "complete" if consume_evidence(observations, used_evidence, {"project_probe", "port_check", "find_free_port", "start_process"}, require_ok=False, predicate=_is_port_evidence) else "pending"
    if any(term in lowered for term in ["http", "verify url", "check url", "respond", "reachable", "preview", "localhost", "website"]) or (
        "access" in lowered and any(term in lowered for term in ["url", "port", "endpoint", "app", "website"])
    ):
        return "complete" if consume_evidence(observations, used_evidence, {"http_get", "http_head", "app_healthcheck"}) else "pending"
    if any(term in lowered for term in ["secret", "credential", "token"]):
        return "complete" if consume_evidence(observations, used_evidence, {"secrets_scan"}, require_ok=False) else "pending"
    if any(term in lowered for term in ["dependency", "dependencies", "vulnerability", "vulnerabilities"]):
        return "complete" if consume_evidence(observations, used_evidence, {"dependency_audit"}, require_ok=False) else "pending"
    if any(term in lowered for term in ["security", "secure", "os", "host", "system", "virus", "viruses", "malware", "antivirus", "defender"]) and any(term in lowered for term in ["audit", "check", "scan", "posture"]):
        return "complete" if consume_evidence(observations, used_evidence, {"system_security_audit", "secrets_scan", "dependency_audit"}, require_ok=False) else "pending"
    if any(term in lowered for term in ["probe", "inspect", "list", "read", "look at", "understand", "audit", "diagnose", "investigate", "search", "grep"]):
        return "complete" if consume_evidence(observations, used_evidence, {"project_probe", "list_files", "tree", "read_file", "search_files", "grep", "python_symbols", "file_info", "git_status", "git_diff", "context_read", "tool_catalog", "sql_query", "list_agents", "metrics_snapshot", "system_security_audit", "secrets_scan", "dependency_audit"}) else "pending"
    if any(term in lowered for term in ["test", "verify", "check", "validate", "confirm"]):
        return "complete" if consume_evidence(observations, used_evidence, {"process_status", "powershell", "wsl", "python", "http_get", "http_head", "app_healthcheck", "json_validate", "tree", "list_files", "search_files", "grep", "file_info", "git_status", "git_diff", "sql_query", "system_security_audit", "secrets_scan", "dependency_audit"}, require_ok=False) else "pending"
    if any(term in lowered for term in ["delete", "remove", "rm ", "clean up", "clear out"]):
        return "complete" if consume_evidence(observations, used_evidence, {"delete_path"}) else "pending"
    if any(term in lowered for term in ["directory", "folder", "mkdir"]):
        return "complete" if consume_evidence(observations, used_evidence, {"make_dir", "powershell", "delete_path"}) else "pending"
    if any(term in lowered for term in ["create", "write", "edit", "file", "backend", "frontend", "install", "build", "implement", "update", "modify", "configure", "refactor", "fix", "apply", "set up", "setup", "download"]):
        # start_process is deliberately NOT create-step evidence: a greedy
        # match here consumed the launch observation and left the real
        # "Start the app" step unprovable (run 44 false-blocked verdict).
        return "complete" if consume_evidence(observations, used_evidence, {"write_file", "append_file", "edit_file", "make_dir", "move_path", "python_venv", "powershell", "wsl", "python", "download_url"}) else "pending"
    if any(term in lowered for term in ["scrape", "web", "source", "read page", "research"]):
        return "complete" if consume_evidence(observations, used_evidence, {"research_web", "scrape_page", "scrape_urls", "web_fetch", "web_search"}) else "pending"
    if any(term in lowered for term in ["answer", "respond", "return", "summarize", "explain"]):
        return "complete" if final_text and not looks_incomplete(final_text) else "pending"
    return "pending"
