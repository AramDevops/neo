from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from ..config import ARTIFACTS_DIR, Settings
from ..db import Database
from .agent_runner import AgentRunner
from .names import generate_agent_name


TASKS_PATH = Path(__file__).resolve().parents[1] / "evals" / "eval_tasks.json"

# A run that dies on DNS/socket/rate-limit trouble never reached the model, so it
# is a transport failure, not a wrong answer. Grading it as a miss would blame the
# model for the network. We retry these, and if they still fail we exclude them
# from the score instead of counting them against the model.
_TRANSPORT_MARKERS = (
    "getaddrinfo", "unreachable host", "10065", "11001", "timed out", "timeout",
    "connection aborted", "connection reset", "connection refused", "broken pipe",
    "max retries", "temporarily unavailable", "handshake", "ssl",
    "502", "503", "504", "rate limit", "429", "overloaded", "resource exhausted",
)

_TRANSPORT_RETRIES = 3


def utc_stamp() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def is_transport_error(run: Dict[str, Any]) -> bool:
    """True when a run errored on the network/provider transport, not on content.

    Such runs never got a model response (status error, no loops, no tools), so
    the error text is a raw socket/HTTP failure rather than anything the model
    said. Those must not be graded as task failures.
    """
    if (run.get("status") or "") != "error":
        return False
    if (run.get("loop_count") or 0) or (run.get("tool_count") or 0):
        return False
    text = (run.get("error_text") or run.get("final_output") or "").lower()
    return any(marker in text for marker in _TRANSPORT_MARKERS)


class EvalService:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()
        self.runner = AgentRunner(self.db)

    def run_eval(self, provider: str | None = None, model: str | None = None) -> int:
        provider = provider or Settings.provider
        model = model or Settings.model
        tasks = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
        eval_id = self.db.execute(
            "INSERT INTO eval_runs (status, provider, model, total) VALUES (?, ?, ?, ?)",
            ("running", provider, model, len(tasks)),
        )
        existing = {row["name"] for row in self.db.fetchall("SELECT name FROM agents")}
        name, title = generate_agent_name(existing)
        agent_id = self.db.execute(
            "INSERT INTO agents (name, title, status, provider, model) VALUES (?, ?, ?, ?, ?)",
            (name, f"eval-{title}", "idle", provider, model),
        )

        start = time.perf_counter()
        passed = 0
        errored = 0
        results: List[Dict[str, Any]] = []
        for task in tasks:
            run, run_id = self._run_task(agent_id, task["prompt"], provider, model)
            output = run.get("final_output") or run.get("error_text") or ""
            if is_transport_error(run):
                # Network/provider transport died before the model answered: do not
                # blame the model. Record it, but keep it out of the graded score.
                ok = False
                errored += 1
                detail = {"errored": True, "reason": "transport", "error": output[:200]}
            else:
                # Grade against the whole run, not just the final sentence. A model
                # that computes 5525 with the python tool but loops without echoing
                # it (or whose final text the harness replaced with a generic block
                # summary) still DID the task; the answer is in its tool evidence.
                # This measures capability, not finalization luck.
                graded_text = self._evidence_text(run_id, output)
                ok, detail = self._grade(graded_text, task.get("grader", {}))
                if ok:
                    passed += 1
            score = 1.0 if ok else 0.0
            item = {
                "task_id": task["id"],
                "category": task.get("category", ""),
                "passed": ok,
                "errored": bool(detail.get("errored")),
                "score": score,
                "run_id": run_id,
                "detail": detail,
                "output": output,
            }
            results.append(item)
            self.db.execute(
                "INSERT INTO eval_items (eval_run_id, run_id, task_id, category, passed, score, latency_ms, output_text, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    eval_id,
                    run_id,
                    task["id"],
                    task.get("category", ""),
                    1 if ok else 0,
                    score,
                    int(run.get("latency_ms") or 0),
                    output,
                    json.dumps(detail, ensure_ascii=False),
                ),
            )

        latency_ms = int((time.perf_counter() - start) * 1000)
        task_total = len(tasks)
        graded = task_total - errored  # transport errors are not the model's fault
        power_score = round((passed / graded) * 100, 1) if graded else 0.0
        summary = {
            "power_score": power_score,
            "passed": passed,
            "graded": graded,
            "errored": errored,
            "task_total": task_total,
            "provider": provider,
            "model": model,
            "items": results,
        }
        self.db.execute(
            "UPDATE eval_runs SET status=?, score=?, passed=?, total=?, latency_ms=?, summary_json=?, ended_at=? WHERE id=?",
            ("complete", power_score, passed, graded, latency_ms, json.dumps(summary, ensure_ascii=False), utc_stamp(), eval_id),
        )
        out_dir = ARTIFACTS_DIR / "evals"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"eval_{eval_id}.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        return eval_id

    def _run_task(self, agent_id: int, prompt: str, provider: str, model: str) -> tuple[Dict[str, Any], int]:
        """Run one task, retrying transient transport failures with backoff.

        Most getaddrinfo/socket/rate-limit blips clear on a retry, so a flaky
        connection stops silently deciding a model's score.
        """
        run: Dict[str, Any] = {}
        run_id = 0
        for attempt in range(_TRANSPORT_RETRIES):
            run_id = self.runner.run_turn(agent_id, prompt, provider, model)
            run = self.db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,)) or {}
            if not is_transport_error(run):
                break
            if attempt < _TRANSPORT_RETRIES - 1:
                time.sleep(2 * (attempt + 1))  # 2s, 4s backoff
        return run, run_id

    def _evidence_text(self, run_id: int, base_output: str) -> str:
        """The model's final text plus every tool result output from the run, so
        grading sees the answer wherever the model actually produced it."""
        parts = [base_output or ""]
        rows = self.db.fetchall(
            "SELECT payload_json FROM run_events WHERE run_id=? AND event_type='tool_result' ORDER BY id",
            (run_id,),
        )
        for row in rows:
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except Exception:
                continue
            out = payload.get("output")
            if out:
                parts.append(str(out))
        return "\n".join(parts)

    def _grade(self, output: str, grader: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
        lower = output.lower()
        words = [str(x).lower() for x in grader.get("must_contain", [])]
        if grader.get("type") == "contains_all":
            missing = [word for word in words if word not in lower]
            return not missing, {"missing": missing}
        if grader.get("type") == "contains_any":
            found = [word for word in words if word in lower]
            return bool(found), {"found": found, "expected_any": words}
        return False, {"error": "unknown grader"}
