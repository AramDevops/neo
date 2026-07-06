"""Execute scenarios through the real production stack and collect evidence.

Each scenario gets an isolated sqlite database and workspace, then runs
through the real AgentRunner, verdict engine, runtime controller, Toolbox,
and ProcessManager. Grading inputs come from database state and the real
filesystem, never just the model's words.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from ...config import Settings
from ...db import Database
from .. import runtime
from ..agent_runner import AgentRunner
from .harness import Scenario, ScriptedProvider, SimulatedBrowserToolbox
from .report import summarize, write_artifact
from .scenarios import SCENARIOS


def configure(base: Path, db_name: str) -> None:
    Settings.db_driver = "sqlite"
    Settings.sqlite_path = str(base / db_name)
    Settings.provider = "mock"
    Settings.model = "mock"
    Settings.shell_enabled = True
    Settings.tool_timeout_seconds = 20
    # The benchmark measures harness LOGIC deterministically; workspace git
    # snapshots add subprocess noise and are exercised by their own unit tests.
    Settings.checkpoints_enabled = False
    runtime.RUNTIME_PATH = base / "runtime_settings.json"


def run_scenario(scenario: Scenario, base: Path) -> Dict[str, Any]:
    configure(base, f"bench_{scenario.id}.db")
    db = Database()
    db.init_schema()
    workspace = base / f"ws_{scenario.id}"
    workspace.mkdir(parents=True, exist_ok=True)
    ctx: dict = {}
    record: dict = {}
    scenario.setup(workspace, ctx)
    toolbox = SimulatedBrowserToolbox(workspace)
    runner = AgentRunner(db, toolbox, provider_factory=lambda provider, model: ScriptedProvider(scenario.script, ctx, record))
    agent_id = db.execute(
        "INSERT INTO agents (name, title, status, provider, model) VALUES (?, ?, ?, ?, ?)",
        (f"bench-{scenario.id}", "benchmark", "idle", "script", "script"),
    )
    wall_start = time.perf_counter()
    try:
        run_id = runner.run_turn(agent_id, scenario.prompt, "script", "script")
    finally:
        # Stop ONLY processes this benchmark run started (live registry
        # entries), never persisted orphans loaded from the shared registry -
        # those may be a live Neo backend's servers on the same machine.
        for entry in toolbox.processes.list_managed():
            if entry.get("orphaned"):
                continue
            toolbox.processes.stop(int(entry["pid"]))
        scenario.cleanup(ctx)
    wall_ms = int((time.perf_counter() - wall_start) * 1000)

    run = db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,)) or {}
    raw_events = db.fetchall("SELECT event_type, payload_json FROM run_events WHERE run_id=? ORDER BY id ASC", (run_id,))
    events = []
    for row in raw_events:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except Exception:
            payload = {}
        events.append({"type": row.get("event_type"), "payload": payload})
    plans = db.fetchall("SELECT step_text, status FROM plans WHERE run_id=? ORDER BY sort_order ASC", (run_id,))

    tool_events = [e for e in events if e["type"] == "tool_result"]
    verdict_events = [e for e in events if e["type"] == "run_verdict"]
    verdict_consistent = verdict_events[-1]["payload"].get("consistent") if verdict_events else None
    plan_total = len(plans)
    plan_complete = sum(1 for row in plans if row.get("status") == "complete")

    result_context = {
        "run": run,
        "events": events,
        "tool_events": tool_events,
        "plans": plans,
        "plan_total": plan_total,
        "plan_complete": plan_complete,
        "verdict_consistent": verdict_consistent,
        "record": record,
        "ctx": ctx,
        "workspace": workspace,
    }
    passed, checks = scenario.grade(result_context)
    tool_failures = sum(1 for e in tool_events if not e["payload"].get("ok"))
    status = str(run.get("status") or "")
    return {
        "id": scenario.id,
        "category": scenario.category,
        "expected_status": scenario.expected_status,
        "status": status,
        "passed": bool(passed),
        "checks": checks,
        "false_success": scenario.expected_status == "blocked" and status == "complete",
        "recovered": tool_failures > 0 and status == "complete",
        "latency_ms": int(run.get("latency_ms") or 0),
        "wall_ms": wall_ms,
        "loops": int(run.get("loop_count") or 0),
        "tool_calls": int(run.get("tool_count") or 0),
        "tool_failures": tool_failures,
        "policy_retries": sum(1 for e in events if e["type"] == "policy_retry"),
        "provider_retries": sum(1 for e in events if e["type"] == "provider_retry"),
        "recovery_attempts": sum(1 for e in events if e["type"] == "recovery_attempt"),
        "plan_total": plan_total,
        "plan_complete": plan_complete,
        "verdict_consistent": verdict_consistent,
        "blocked_reason": str(run.get("error_text") or ""),
        "final_output": str(run.get("final_output") or "")[:800],
    }


def run_benchmark(
    only: List[str] | None = None,
    base_dir: Path | None = None,
    artifact_dir: Path | None = None,
) -> Dict[str, Any]:
    scenarios = [s for s in SCENARIOS if not only or s.id in only]
    if only:
        unknown = set(only) - {s.id for s in SCENARIOS}
        if unknown:
            raise ValueError(f"Unknown scenario id(s): {', '.join(sorted(unknown))}")
    base = Path(base_dir) if base_dir else Path(tempfile.mkdtemp(prefix="neo_bench_"))
    base.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    results = [run_scenario(scenario, base) for scenario in scenarios]
    total_wall_ms = int((time.perf_counter() - started) * 1000)

    summary = summarize(results, total_wall_ms)
    summary["artifact_path"] = str(write_artifact(summary, artifact_dir))
    return summary
