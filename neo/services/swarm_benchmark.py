"""Swarm benchmark: measure how well a TEAM of agents coordinates.

Where the benchmark package grades ONE agent finishing ONE task, this drives
several role agents through the REAL AgentRunner/Toolbox against a shared
workspace and grades the COMBINED outcome on two axes:

  collaboration - many agents on ONE app: do they converge on one shared plan
                  (same stack, same port, one app) instead of fragmenting?
  isolation     - many agents on DIFFERENT apps: do they stay inside their own
                  scope and leave each other's work untouched?

The model is a deterministic script per agent, so every metric measures the
HARNESS coordination machinery (shared project plan, write scope, ownership),
not a language model. Run:  python -m neo.services.swarm_benchmark
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List

from ..db import Database
from .agent_runner import AgentRunner
from .benchmark import ScriptedProvider, SimulatedBrowserToolbox, configure, free_port


@dataclass
class SwarmAgent:
    name: str
    title: str
    prompt: str
    script: Callable[[int, str, dict], dict]
    role: str = ""
    scope: List[str] = field(default_factory=list)


@dataclass
class SwarmScenario:
    id: str
    category: str
    agents: List[SwarmAgent]
    grade: Callable[[dict], "tuple[bool, dict]"]
    setup: Callable[[Path, dict], None] = field(default=lambda ws, ctx: None)


# ---------------------------------------------------------------------------
# Helpers shared by the scripts
# ---------------------------------------------------------------------------

def _plan_port(prompt: str) -> int | None:
    """The agreed backend port, read from the shared plan the architect set.
    The architect encodes it as BACKEND_PORT=<n> so it cannot be confused with
    an example port in the system contract."""
    match = re.search(r"BACKEND_PORT=(\d+)", prompt)
    return int(match.group(1)) if match else None


def _done(final: str) -> dict:
    return {"plan": ["Report"], "tool_calls": [], "final": final, "needs_more": False}


# ---------------------------------------------------------------------------
# Scenario 1: collaboration - three agents, ONE app, one shared plan
# ---------------------------------------------------------------------------

def _architect_script(turn: int, prompt: str, ctx: dict) -> dict:
    port = ctx["port"]
    observed = prompt.split("Tool observations so far:")[-1]
    if '"tool": "project_brief_set"' not in observed:
        return {
            "plan": ["Set the shared brief", "Add the team's tasks", "Scaffold the app folder"],
            "tool_calls": [
                {"tool": "project_brief_set", "args": {
                    "goal": "A small CRM",
                    "stack": "Node built-in http server, SQLite, one app",
                    "conventions": f"BACKEND_PORT={port}; one app under crm/; SQLite only; no second framework or database; frontend calls BACKEND_PORT",
                }},
                {"tool": "project_task_add", "args": {"title": "Build the backend server", "owner": "Backend", "depends_on": "", "deliverable": "crm/server.js listening on the agreed port"}},
                {"tool": "project_task_add", "args": {"title": "Wire the frontend", "owner": "Frontend", "depends_on": "1", "deliverable": "crm/app.js calling the agreed port"}},
                {"tool": "write_file", "args": {"path": "crm/README.md", "content": "# CRM\nShared plan set; backend + frontend build to it.\n"}},
            ],
            "final": "",
            "needs_more": True,
        }
    return _done("Shared plan set and app scaffolded. Backend and frontend can build against it.")


def _backend_script(turn: int, prompt: str, ctx: dict) -> dict:
    observed = prompt.split("Tool observations so far:")[-1]
    if '"tool": "write_file"' not in observed:
        port = _plan_port(prompt)
        ctx.setdefault("record", {})["backend_port"] = port
        server = (
            "const http = require('http');\n"
            f"const PORT = {port};\n"
            "const server = http.createServer((req, res) => {\n"
            "  res.setHeader('Content-Type', 'application/json');\n"
            "  if (req.url.startsWith('/api/contacts')) { res.end(JSON.stringify([])); }\n"
            "  else { res.writeHead(404); res.end(JSON.stringify({error:'not found'})); }\n"
            "});\n"
            "server.listen(PORT, () => console.log('CRM backend on ' + PORT));\n"
        )
        return {
            "plan": ["Read the shared plan", "Write the backend on the agreed port", "Mark my task done"],
            "tool_calls": [
                {"tool": "write_file", "args": {"path": "crm/server.js", "content": server}},
                {"tool": "project_task_update", "args": {"task_id": 1, "status": "done", "notes": "backend serving contacts"}},
            ],
            "final": "",
            "needs_more": True,
        }
    return _done("Backend built on the port from the shared plan; task marked done.")


def _frontend_script(turn: int, prompt: str, ctx: dict) -> dict:
    observed = prompt.split("Tool observations so far:")[-1]
    if '"tool": "write_file"' not in observed:
        port = _plan_port(prompt)
        ctx.setdefault("record", {})["frontend_port"] = port
        app = (
            "async function loadContacts() {\n"
            f"  const res = await fetch('http://localhost:{port}/api/contacts');\n"
            "  return res.json();\n"
            "}\n"
            "document.addEventListener('DOMContentLoaded', loadContacts);\n"
        )
        return {
            "plan": ["Read the shared plan", "Wire the frontend to the agreed port", "Mark my task done"],
            "tool_calls": [
                {"tool": "write_file", "args": {"path": "crm/app.js", "content": app}},
                {"tool": "project_task_update", "args": {"task_id": 2, "status": "done", "notes": "frontend calls the backend"}},
            ],
            "final": "",
            "needs_more": True,
        }
    return _done("Frontend wired to the backend port from the shared plan; task marked done.")


def _grade_collaboration(result: dict) -> "tuple[bool, dict]":
    workspace: Path = result["workspace"]
    db: Database = result["db"]
    record = result["ctx"].get("record", {})
    port = result["ctx"]["port"]
    server = workspace / "crm" / "server.js"
    app = workspace / "crm" / "app.js"
    server_text = server.read_text(encoding="utf-8") if server.exists() else ""
    app_text = app.read_text(encoding="utf-8") if app.exists() else ""
    briefs = db.fetchall("SELECT id FROM project_brief")
    tasks = db.fetchall("SELECT status FROM project_tasks")
    top_level = {p.name for p in workspace.iterdir() if p.is_dir() and p.name != ".neo"}
    checks = {
        "one_shared_brief": len(briefs) == 1,
        "all_tasks_done": bool(tasks) and all(t.get("status") == "done" for t in tasks),
        "backend_read_port_from_plan": record.get("backend_port") == port,
        "frontend_read_port_from_plan": record.get("frontend_port") == port,
        "backend_uses_agreed_port": str(port) in server_text,
        "frontend_uses_same_port": str(port) in app_text,
        "single_app_no_fork": top_level == {"crm"},
        "no_scope_violations": result["scope_blocked"] == 0,
    }
    return all(checks.values()), checks


# ---------------------------------------------------------------------------
# Scenario 2: isolation - three agents, DIFFERENT apps, enforced scope
# ---------------------------------------------------------------------------

def _isolated_builder_script(app_dir: str, steal_dir: str | None) -> Callable[[int, str, dict], dict]:
    def script(turn: int, prompt: str, ctx: dict) -> dict:
        observed = prompt.split("Tool observations so far:")[-1]
        if '"tool": "write_file"' not in observed:
            calls = [{"tool": "write_file", "args": {"path": f"{app_dir}/main.js", "content": f"console.log('{app_dir} app');\n"}}]
            if steal_dir:
                # Try to write into a sibling agent's app: scope must block it.
                calls.append({"tool": "write_file", "args": {"path": f"{steal_dir}/injected.js", "content": "console.log('should be blocked');\n"}})
            return {"plan": ["Build my own app"], "tool_calls": calls, "final": "", "needs_more": True}
        return _done(f"Built {app_dir}/main.js within my scope.")
    return script


def _grade_isolation(result: dict) -> "tuple[bool, dict]":
    workspace: Path = result["workspace"]
    events = result["events_by_agent"]
    checks = {
        "app_a_built": (workspace / "app_a" / "main.js").exists(),
        "app_b_built": (workspace / "app_b" / "main.js").exists(),
        "app_c_built": (workspace / "app_c" / "main.js").exists(),
        "foreign_write_blocked": not (workspace / "app_b" / "injected.js").exists(),
        "block_was_recorded": any(
            obs.get("meta", {}).get("scope_blocked")
            for obs in events.get("Alpha", [])
        ),
        "no_cross_contamination": _dir_files(workspace / "app_a") <= {"main.js"}
        and _dir_files(workspace / "app_b") <= {"main.js"}
        and _dir_files(workspace / "app_c") <= {"main.js"},
    }
    return all(checks.values()), checks


def _dir_files(path: Path) -> set:
    return {p.name for p in path.iterdir() if p.is_file()} if path.exists() else set()


# ---------------------------------------------------------------------------
# Scenario 3: divergence detection - a rogue agent forks a second app
# ---------------------------------------------------------------------------

def _plan_architect_script(turn: int, prompt: str, ctx: dict) -> dict:
    observed = prompt.split("Tool observations so far:")[-1]
    if '"tool": "project_brief_set"' not in observed:
        return {
            "plan": ["Set the shared brief", "Scaffold the app folder"],
            "tool_calls": [
                {"tool": "project_brief_set", "args": {
                    "goal": "A CRM",
                    "stack": "Node http, SQLite",
                    "conventions": "one app under crm/; no second app or framework",
                }},
                {"tool": "project_task_add", "args": {"title": "Build backend", "owner": "Backend", "depends_on": "", "deliverable": "crm/server.js"}},
                {"tool": "write_file", "args": {"path": "crm/README.md", "content": "# CRM\n"}},
            ],
            "final": "",
            "needs_more": True,
        }
    return _done("Plan set and crm/ scaffolded.")


def _rogue_script(turn: int, prompt: str, ctx: dict) -> dict:
    observed = prompt.split("Tool observations so far:")[-1]
    if '"tool": "write_file"' not in observed:
        # Ignores the shared plan (which says build under crm/) and forks a
        # SECOND app in its own directory - the exact CRM failure.
        return {
            "plan": ["Build my own backend"],
            "tool_calls": [
                {"tool": "write_file", "args": {"path": "crm-backend/server.js", "content": "console.log('a second, divergent app');\n"}},
            ],
            "final": "Built a backend (in my own folder).",
            "needs_more": True,
        }
    return _done("Backend built.")


def _grade_divergence(result: dict) -> "tuple[bool, dict]":
    workspace: Path = result["workspace"]
    runs = result["runs_by_agent"]
    rogue = runs.get("Rex", {})
    architect = runs.get("Ada", {})
    reason = str(rogue.get("error_text") or "")
    checks = {
        "architect_completed": architect.get("status") == "complete",
        "rogue_blocked": rogue.get("status") == "blocked",
        "blocked_for_divergence": "Plan divergence" in reason,
        "forked_app_was_flagged": (workspace / "crm-backend" / "server.js").exists(),
    }
    return all(checks.values()), checks


# ---------------------------------------------------------------------------
# Scenario 4: update an existing app - a swarm MODIFIES, does not clobber
# ---------------------------------------------------------------------------

_SEED_SERVER = (
    "const http = require('http');\n"
    "const PORT = 4000;\n"
    "const server = http.createServer((req, res) => {\n"
    "  if (req.url === '/api/contacts') { res.end('[]'); }\n"
    "  // MORE_ROUTES\n"
    "  else { res.writeHead(404); res.end(); }\n"
    "});\n"
    "server.listen(PORT);\n"
)
_SEED_APP = (
    "async function loadContacts() { const r = await fetch('/api/contacts'); return r.json(); }\n"
    "// MORE_LOADERS\n"
    "document.addEventListener('DOMContentLoaded', loadContacts);\n"
)


def _setup_existing_app(workspace: Path, ctx: dict) -> None:
    crm = workspace / "crm"
    crm.mkdir(parents=True, exist_ok=True)
    (crm / "server.js").write_text(_SEED_SERVER, encoding="utf-8")
    (crm / "app.js").write_text(_SEED_APP, encoding="utf-8")


def _update_architect_script(turn: int, prompt: str, ctx: dict) -> dict:
    observed = prompt.split("Tool observations so far:")[-1]
    if '"tool": "project_brief_set"' not in observed:
        return {
            "plan": ["Set the plan for the existing app", "Add the tasks"],
            "tool_calls": [
                {"tool": "project_brief_set", "args": {
                    "goal": "Add a Companies feature to the EXISTING CRM (do not rebuild it)",
                    "stack": "the existing Node http app under crm/, port 4000",
                    "conventions": "one app under crm/; edit existing files, never replace them; no second app",
                }},
                {"tool": "project_task_add", "args": {"title": "Add /api/companies to the backend", "owner": "Backend", "depends_on": "", "deliverable": "crm/server.js serves /api/companies AND keeps /api/contacts"}},
                {"tool": "project_task_add", "args": {"title": "Add companies loading to the frontend", "owner": "Frontend", "depends_on": "1", "deliverable": "crm/app.js loads companies AND keeps contacts"}},
            ],
            "final": "",
            "needs_more": True,
        }
    return _done("Plan set for the existing app; backend and frontend extend it.")


def _update_backend_script(turn: int, prompt: str, ctx: dict) -> dict:
    observed = prompt.split("Tool observations so far:")[-1]
    if '"tool": "edit_file"' not in observed:
        return {
            "plan": ["Read the plan", "Extend the existing server without clobbering", "Mark done"],
            "tool_calls": [
                {"tool": "edit_file", "args": {
                    "path": "crm/server.js",
                    "old": "  // MORE_ROUTES\n",
                    "new": "  else if (req.url === '/api/companies') { res.end('[]'); }\n  // MORE_ROUTES\n",
                }},
                {"tool": "project_task_update", "args": {"task_id": 1, "status": "done", "notes": "companies route added, contacts preserved"}},
            ],
            "final": "",
            "needs_more": True,
        }
    return _done("Extended the existing backend with /api/companies; contacts untouched.")


def _update_frontend_script(turn: int, prompt: str, ctx: dict) -> dict:
    observed = prompt.split("Tool observations so far:")[-1]
    if '"tool": "edit_file"' not in observed:
        return {
            "plan": ["Read the plan", "Extend the existing frontend without clobbering", "Mark done"],
            "tool_calls": [
                {"tool": "edit_file", "args": {
                    "path": "crm/app.js",
                    "old": "// MORE_LOADERS\n",
                    "new": "async function loadCompanies() { const r = await fetch('/api/companies'); return r.json(); }\n// MORE_LOADERS\n",
                }},
                {"tool": "project_task_update", "args": {"task_id": 2, "status": "done", "notes": "companies loader added, contacts preserved"}},
            ],
            "final": "",
            "needs_more": True,
        }
    return _done("Extended the existing frontend with companies loading; contacts untouched.")


def _grade_update_existing(result: dict) -> "tuple[bool, dict]":
    workspace: Path = result["workspace"]
    db: Database = result["db"]
    server = (workspace / "crm" / "server.js").read_text(encoding="utf-8") if (workspace / "crm" / "server.js").exists() else ""
    app = (workspace / "crm" / "app.js").read_text(encoding="utf-8") if (workspace / "crm" / "app.js").exists() else ""
    tasks = db.fetchall("SELECT status FROM project_tasks")
    top_level = {p.name for p in workspace.iterdir() if p.is_dir() and p.name != ".neo"}
    checks = {
        "existing_contacts_route_preserved": "/api/contacts" in server,
        "new_companies_route_added": "/api/companies" in server,
        "existing_contacts_loader_preserved": "loadContacts" in app,
        "new_companies_loader_added": "loadCompanies" in app,
        "all_tasks_done": bool(tasks) and all(t.get("status") == "done" for t in tasks),
        "no_second_app": top_level == {"crm"},
        "no_scope_violations": result["scope_blocked"] == 0,
    }
    return all(checks.values()), checks


# ---------------------------------------------------------------------------
# Scenario 5: port divergence - an agent binds a port the plan didn't assign
# ---------------------------------------------------------------------------

def _port_architect_script(turn: int, prompt: str, ctx: dict) -> dict:
    observed = prompt.split("Tool observations so far:")[-1]
    if '"tool": "project_brief_set"' not in observed:
        return {
            "plan": ["Set the plan with the assigned port"],
            "tool_calls": [
                {"tool": "project_brief_set", "args": {
                    "goal": "A static-served app",
                    "stack": "python http.server under site/",
                    "conventions": f"BACKEND_PORT={ctx['port']}; serve site/ on the assigned port",
                }},
                {"tool": "project_task_add", "args": {"title": "Serve the site on the assigned port", "owner": "Backend", "depends_on": "", "deliverable": "site served on BACKEND_PORT"}},
                {"tool": "make_dir", "args": {"path": "site"}},
                {"tool": "write_file", "args": {"path": "site/index.html", "content": "<h1>site</h1>\n"}},
            ],
            "final": "",
            "needs_more": True,
        }
    return _done("Plan set; site scaffolded on the assigned port.")


def _port_rogue_script(turn: int, prompt: str, ctx: dict) -> dict:
    observed = prompt.split("Tool observations so far:")[-1]
    if '"tool": "start_process"' not in observed:
        wrong = ctx["wrong_port"]
        # Ignores BACKEND_PORT from the plan and binds a different port.
        return {
            "plan": ["Serve the site"],
            "tool_calls": [
                {"tool": "start_process", "args": {"command": f"python -m http.server {wrong}", "name": "site", "path": "site", "port": wrong, "wait_seconds": 12}},
            ],
            "final": "Serving the site.",
            "needs_more": True,
        }
    return _done("Site served.")


def _grade_port_divergence(result: dict) -> "tuple[bool, dict]":
    runs = result["runs_by_agent"]
    rogue = runs.get("Rita", {})
    reason = str(rogue.get("error_text") or "")
    checks = {
        "architect_completed": runs.get("Ada", {}).get("status") == "complete",
        "rogue_blocked": rogue.get("status") == "blocked",
        "blocked_for_port_divergence": "Port divergence" in reason,
    }
    return all(checks.values()), checks


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

SCENARIOS: List[SwarmScenario] = [
    SwarmScenario(
        id="collaboration_one_app",
        category="collaboration",
        agents=[
            SwarmAgent("Ada", "architect", "Define the shared project plan and scaffold the CRM app folder.", _architect_script,
                       role="You are the lead/architect. Set the shared brief and task board first, then scaffold."),
            SwarmAgent("Boris", "backend", "Build the CRM backend to the shared plan.", _backend_script,
                       role="You are the backend agent. Build the server to the shared plan's stack and port.", scope=["crm/*"]),
            SwarmAgent("Cleo", "frontend", "Wire the CRM frontend to the shared plan.", _frontend_script,
                       role="You are the frontend agent. Wire the UI to the backend at the shared plan's port.", scope=["crm/*"]),
        ],
        grade=_grade_collaboration,
    ),
    SwarmScenario(
        id="parallel_isolation",
        category="isolation",
        agents=[
            SwarmAgent("Alpha", "builder-a", "Build your own app in app_a.", _isolated_builder_script("app_a", "app_b"),
                       role="Build ONLY your app.", scope=["app_a/*"]),
            SwarmAgent("Beta", "builder-b", "Build your own app in app_b.", _isolated_builder_script("app_b", None),
                       role="Build ONLY your app.", scope=["app_b/*"]),
            SwarmAgent("Gamma", "builder-c", "Build your own app in app_c.", _isolated_builder_script("app_c", None),
                       role="Build ONLY your app.", scope=["app_c/*"]),
        ],
        grade=_grade_isolation,
    ),
    SwarmScenario(
        id="divergence_detected",
        category="divergence",
        agents=[
            SwarmAgent("Ada", "architect", "Set the shared plan and scaffold the CRM under crm/.", _plan_architect_script,
                       role="You are the lead. Set the shared plan first."),
            SwarmAgent("Rex", "rogue-backend", "Build the CRM backend.", _rogue_script,
                       role="You are the backend agent. Follow the shared plan."),
        ],
        grade=_grade_divergence,
    ),
    SwarmScenario(
        id="update_existing_app",
        category="update",
        setup=_setup_existing_app,
        agents=[
            SwarmAgent("Ada", "architect", "Plan the Companies feature on the existing CRM.", _update_architect_script,
                       role="You are the lead. Plan changes to the EXISTING app; do not rebuild it."),
            SwarmAgent("Boris", "backend", "Add the companies backend route.", _update_backend_script,
                       role="You are the backend agent. Extend the existing server; never replace it.", scope=["crm/*"]),
            SwarmAgent("Cleo", "frontend", "Add companies loading to the frontend.", _update_frontend_script,
                       role="You are the frontend agent. Extend the existing UI; never replace it.", scope=["crm/*"]),
        ],
        grade=_grade_update_existing,
    ),
    SwarmScenario(
        id="port_divergence",
        category="divergence",
        agents=[
            SwarmAgent("Ada", "architect", "Set the plan with the assigned backend port.", _port_architect_script,
                       role="You are the lead. Assign the port in the plan."),
            SwarmAgent("Rita", "rogue-backend", "Serve the site.", _port_rogue_script,
                       role="You are the backend agent. Use the plan's assigned port."),
        ],
        grade=_grade_port_divergence,
    ),
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run_swarm_scenario(scenario: SwarmScenario, base: Path) -> Dict[str, Any]:
    configure(base, f"swarm_{scenario.id}.db")
    db = Database()
    db.init_schema()
    workspace = base / f"ws_{scenario.id}"
    workspace.mkdir(parents=True, exist_ok=True)
    ports = {free_port()}
    while len(ports) < 2:
        ports.add(free_port())
    right, wrong = sorted(ports)
    ctx: dict = {"port": right, "wrong_port": wrong, "record": {}}
    record: dict = {}
    scenario.setup(workspace, ctx)
    toolbox = SimulatedBrowserToolbox(workspace)
    runner = AgentRunner(db, toolbox, provider_factory=lambda p, m: ScriptedProvider(ctx["_script"], ctx, record))

    events_by_agent: Dict[str, List[dict]] = {}
    runs_by_agent: Dict[str, dict] = {}
    scope_blocked = 0
    wall_start = time.perf_counter()
    total_tools = 0
    try:
        for spec in scenario.agents:
            agent_id = db.execute(
                "INSERT INTO agents (name, title, status, provider, model, system_prompt, scope_paths) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (spec.name, spec.title, "idle", "script", "script", spec.role, json.dumps(spec.scope) if spec.scope else ""),
            )
            ctx["_script"] = spec.script
            run_id = runner.run_turn(agent_id, spec.prompt, "script", "script")
            run_row = db.fetchone("SELECT status, error_text FROM runs WHERE id=?", (run_id,)) or {}
            runs_by_agent[spec.name] = {"status": run_row.get("status"), "error_text": run_row.get("error_text")}
            obs = []
            for row in db.fetchall("SELECT payload_json FROM run_events WHERE run_id=? AND event_type=? ORDER BY id ASC", (run_id, "tool_result")):
                try:
                    payload = json.loads(row.get("payload_json") or "{}")
                except Exception:
                    payload = {}
                obs.append(payload)
                total_tools += 1
                if payload.get("meta", {}).get("scope_blocked"):
                    scope_blocked += 1
            events_by_agent[spec.name] = obs
    finally:
        for entry in toolbox.processes.list_managed():
            if not entry.get("orphaned"):
                toolbox.processes.stop(int(entry["pid"]))
    wall_ms = int((time.perf_counter() - wall_start) * 1000)

    result = {
        "db": db,
        "workspace": workspace,
        "ctx": ctx,
        "events_by_agent": events_by_agent,
        "runs_by_agent": runs_by_agent,
        "scope_blocked": scope_blocked,
    }
    passed, checks = scenario.grade(result)
    return {
        "id": scenario.id,
        "category": scenario.category,
        "agents": len(scenario.agents),
        "passed": passed,
        "checks": checks,
        "wall_ms": wall_ms,
        "tool_calls": total_tools,
        "scope_blocked": scope_blocked,
    }


def run_swarm_benchmark(only: List[str] | None = None) -> Dict[str, Any]:
    import tempfile

    scenarios = [s for s in SCENARIOS if not only or s.id in only]
    results: List[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="neo_swarm_") as tmp:
        base = Path(tmp)
        for scenario in scenarios:
            results.append(_run_swarm_scenario(scenario, base / scenario.id))
    passed = sum(1 for r in results if r["passed"])
    return {"scenarios": len(results), "passed": passed, "results": results}


def _print_summary(summary: Dict[str, Any]) -> None:
    header = f"{'scenario':<24} {'category':<14} {'agents':>6} {'pass':>5} {'ms':>7} {'tools':>6} {'scope_blk':>9}"
    print(header)
    print("-" * len(header))
    for r in summary["results"]:
        print(f"{r['id']:<24} {r['category']:<14} {r['agents']:>6} {'PASS' if r['passed'] else 'FAIL':>5} "
              f"{r['wall_ms']:>7} {r['tool_calls']:>6} {r['scope_blocked']:>9}")
        if not r["passed"]:
            for name, ok in r["checks"].items():
                if not ok:
                    print(f"    x {name}")
    print("-" * len(header))
    print(f"swarm pass rate {100.0 * summary['passed'] / max(summary['scenarios'], 1):.1f}% ({summary['passed']}/{summary['scenarios']})")


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Neo swarm (multi-agent) benchmark.")
    parser.add_argument("--only", help="Comma-separated scenario ids to run.")
    args = parser.parse_args(argv)
    only = [item.strip() for item in args.only.split(",") if item.strip()] if args.only else None
    summary = run_swarm_benchmark(only=only)
    _print_summary(summary)
    return 0 if summary["passed"] == summary["scenarios"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
