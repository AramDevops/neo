"""Runtime scenarios: launching real processes and recovering from failures.

Covers a wrong entrypoint that must fail loudly and be relaunched, a port
conflict the runtime controller must route around, a Node.js listener, and a
model that edits files then walks away without running anything.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

from ..harness import Scenario, free_port, observed


def _setup_launch_recovery(workspace: Path, ctx: dict) -> None:
    port = free_port()
    ctx["port"] = port
    target = workspace / "probe" / "main.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "\n"
        f"PORT = {port}\n"
        "\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body = b'listener ok'\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Length', str(len(body)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "\n"
        "    def log_message(self, *args):\n"
        "        pass\n"
        "\n"
        "ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()\n",
        encoding="utf-8",
    )


def _script_launch_recovery(turn: int, prompt: str, ctx: dict) -> dict:
    obs = observed(prompt)
    plan = [
        "Start the diagnostic listener process",
        "Verify the listener responds over HTTP",
    ]
    port = ctx["port"]
    if '"tool": "start_process"' not in obs:
        # Deliberately wrong entrypoint: the harness must fail loudly with evidence.
        return {"plan": plan, "tool_calls": [
            {"tool": "start_process", "args": {"command": "python missing_entry.py", "name": "probe_listener", "path": "probe"}},
        ], "final": "", "needs_more": True}
    if '"listening_port"' not in obs:
        # Read the failure evidence, correct the entrypoint, declare the port.
        return {"plan": plan, "tool_calls": [
            {"tool": "start_process", "args": {"command": "python main.py", "name": "probe_listener", "path": "probe", "port": port, "wait_seconds": 25}},
        ], "final": "", "needs_more": True}
    if '"status": 200' not in obs:
        # Retry until a real 200 is observed; attempted is not verified.
        return {"plan": plan, "tool_calls": [
            {"tool": "http_get", "args": {"url": f"http://127.0.0.1:{port}/"}},
        ], "final": "", "needs_more": True}
    return {"plan": plan, "tool_calls": [], "needs_more": False, "final": (
        f"The diagnostic listener is serving on port {port}, verified over HTTP "
        "after recovering from the initial launch failure."
    )}


def _grade_launch_recovery(result: dict) -> tuple[bool, dict]:
    run = result["run"]
    tool_events = result["tool_events"]
    failed_starts = [e for e in tool_events if e["payload"].get("tool") == "start_process" and not e["payload"].get("ok")]
    ok_starts = [e for e in tool_events if e["payload"].get("tool") == "start_process" and e["payload"].get("ok")]
    ok_http = [e for e in tool_events if e["payload"].get("tool") == "http_get" and e["payload"].get("ok")]
    checks = {
        "status_complete": run["status"] == "complete",
        "first_launch_failed_with_evidence": bool(failed_starts) and "missing_entry" in str(failed_starts[0]["payload"].get("output", "")),
        "relaunch_verified_listening": bool(ok_starts) and any("listening_port" in json.dumps(e["payload"].get("meta") or {}) for e in ok_starts),
        "http_verified": bool(ok_http),
        "plan_all_complete": result["plan_complete"] == result["plan_total"] and result["plan_total"] > 0,
    }
    return all(checks.values()), checks


def _setup_port_conflict(workspace: Path, ctx: dict) -> None:
    occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupier.bind(("127.0.0.1", 0))
    occupier.listen(1)
    ctx["occupied_port"] = occupier.getsockname()[1]
    ctx["occupier"] = occupier
    target = workspace / "calendar-app" / "backend" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from flask import Flask\n"
        "\n"
        "app = Flask(__name__)\n"
        "\n"
        "@app.route('/')\n"
        "def home():\n"
        "    return 'calendar ok'\n"
        "\n"
        "if __name__ == '__main__':\n"
        f"    app.run(port={ctx['occupied_port']})\n",
        encoding="utf-8",
    )


def _script_port_conflict(turn: int, prompt: str, ctx: dict) -> dict:
    obs = observed(prompt)
    plan = [
        "Inspect the project structure and runtime requirements",
        "Check the intended localhost port",
        "Start the app on a verified available port",
        "Verify the app URL responds",
        "Open the browser to the verified URL",
    ]
    if '"listening_port"' not in obs:
        # Defer to the deterministic runtime controller: probe -> free port ->
        # patch app.run port -> start. The model just keeps the plan alive.
        return {"plan": plan, "tool_calls": [], "final": "", "needs_more": True}
    return {"plan": plan, "tool_calls": [], "needs_more": False,
            "final": "The calendar app is running on a verified free port; opening the verified URL."}


def _cleanup_port_conflict(ctx: dict) -> None:
    occupier = ctx.get("occupier")
    if occupier is not None:
        try:
            occupier.close()
        except OSError:
            pass


def _grade_port_conflict(result: dict) -> tuple[bool, dict]:
    run = result["run"]
    tool_events = result["tool_events"]
    occupied = result["ctx"]["occupied_port"]
    ok_starts = [e for e in tool_events if e["payload"].get("tool") == "start_process" and e["payload"].get("ok")]
    listening_ports = [
        (e["payload"].get("meta") or {}).get("listening_port")
        for e in ok_starts
        if (e["payload"].get("meta") or {}).get("listening_port")
    ]
    port_edits = [e for e in tool_events if e["payload"].get("tool") == "edit_file" and e["payload"].get("ok")]
    ok_http = [e for e in tool_events if e["payload"].get("tool") == "http_get" and e["payload"].get("ok")]
    ok_browser = [e for e in tool_events if e["payload"].get("tool") == "open_browser" and e["payload"].get("ok")]
    checks = {
        "status_complete": run["status"] == "complete",
        "occupied_port_avoided": bool(listening_ports) and occupied not in listening_ports,
        "app_port_patched": bool(port_edits),
        "http_verified": bool(ok_http),
        "browser_step_recorded": bool(ok_browser),
        "verdict_consistent": result["verdict_consistent"] is True,
    }
    return all(checks.values()), checks


def _setup_node_service(workspace: Path, ctx: dict) -> None:
    port = free_port()
    ctx["port"] = port
    target = workspace / "svc" / "listener.js"
    target.parent.mkdir(parents=True)
    target.write_text(
        "const http = require('http');\n"
        f"const PORT = {port};\n"
        "const server = http.createServer((req, res) => {\n"
        "  res.writeHead(200, {'Content-Type': 'text/plain'});\n"
        "  res.end('listener ok');\n"
        "});\n"
        "server.listen(PORT, '127.0.0.1');\n",
        encoding="utf-8",
    )


def _script_node_service(turn: int, prompt: str, ctx: dict) -> dict:
    obs = observed(prompt)
    plan = [
        "Start the JavaScript listener process",
        "Verify the listener responds over HTTP",
    ]
    port = ctx["port"]
    if '"listening_port"' not in obs:
        return {"plan": plan, "tool_calls": [
            {"tool": "start_process", "args": {"command": "node listener.js", "name": "js_listener", "path": "svc", "port": port, "wait_seconds": 25}},
        ], "final": "", "needs_more": True}
    if '"status": 200' not in obs:
        # Retry until a real 200 is observed; attempted is not verified.
        return {"plan": plan, "tool_calls": [
            {"tool": "http_get", "args": {"url": f"http://127.0.0.1:{port}/"}},
        ], "final": "", "needs_more": True}
    return {"plan": plan, "tool_calls": [], "needs_more": False,
            "final": f"The JavaScript listener is serving on port {port}, verified over HTTP."}


def _grade_node_service(result: dict) -> tuple[bool, dict]:
    run = result["run"]
    tool_events = result["tool_events"]
    ok_starts = [e for e in tool_events if e["payload"].get("tool") == "start_process" and e["payload"].get("ok")]
    ok_http = [e for e in tool_events if e["payload"].get("tool") == "http_get" and e["payload"].get("ok")]
    checks = {
        "status_complete": run["status"] == "complete",
        "node_launch_verified_listening": bool(ok_starts) and any("listening_port" in json.dumps(e["payload"].get("meta") or {}) for e in ok_starts),
        "http_verified": bool(ok_http),
        "plan_all_complete": result["plan_complete"] == result["plan_total"] and result["plan_total"] > 0,
        "verdict_consistent": result["verdict_consistent"] is True,
    }
    return all(checks.values()), checks


def _setup_abandoned_finish(workspace: Path, ctx: dict) -> None:
    port = free_port()
    ctx["port"] = port
    app = workspace / "clockapp"
    app.mkdir(parents=True)
    (app / "package.json").write_text('{"name": "clockapp", "scripts": {"start": "node server.js"}}\n', encoding="utf-8")
    (app / "server.js").write_text(
        "const http = require('http');\n"
        f"const PORT = {port};\n"
        "const server = http.createServer((req, res) => {\n"
        "  res.writeHead(200, {'Content-Type': 'text/html'});\n"
        "  res.end('<html><body>clock ok</body></html>');\n"
        "});\n"
        "server.listen(PORT, '127.0.0.1', () => console.log(`Server listening on port ${PORT}`));\n",
        encoding="utf-8",
    )


def _script_abandoned_finish(turn: int, prompt: str, ctx: dict) -> dict:
    obs = observed(prompt)
    plan = [
        "Inspect the clockapp project",
        "Update the clock app files",
    ]
    if '"tool": "write_file"' not in obs:
        return {"plan": plan, "tool_calls": [
            {"tool": "project_probe", "args": {"path": "clockapp"}},
            {"tool": "write_file", "args": {"path": "clockapp/NOTES.md", "content": "upgraded\n"}},
        ], "final": "", "needs_more": True}
    # Walks away after editing: never starts, never verifies, never opens.
    return {"plan": plan, "tool_calls": [], "needs_more": False,
            "final": "Upgraded the clock app files."}


def _grade_abandoned_finish(result: dict) -> tuple[bool, dict]:
    run = result["run"]
    tool_events = result["tool_events"]
    ok_starts = [e for e in tool_events if e["payload"].get("tool") == "start_process" and e["payload"].get("ok")]
    ok_http = [e for e in tool_events if e["payload"].get("tool") == "http_get" and e["payload"].get("ok")]
    ok_browser = [e for e in tool_events if e["payload"].get("tool") == "open_browser" and e["payload"].get("ok")]
    checks = {
        "status_complete": run["status"] == "complete",
        "change_written": (result["workspace"] / "clockapp" / "NOTES.md").is_file(),
        "harness_started_the_app": bool(ok_starts),
        "http_verified": bool(ok_http),
        "browser_step_recorded": bool(ok_browser),
        "verdict_consistent": result["verdict_consistent"] is True,
    }
    return all(checks.values()), checks


LAUNCH_RECOVERY = Scenario(
    id="launch_recovery",
    category="runtime",
    prompt="start the diagnostic listener process in probe and verify it responds",
    expected_status="complete",
    setup=_setup_launch_recovery,
    script=_script_launch_recovery,
    grade=_grade_launch_recovery,
)

PORT_CONFLICT_RECOVERY = Scenario(
    id="port_conflict_recovery",
    category="runtime",
    prompt="run the calendar app and open it in the browser",
    expected_status="complete",
    setup=_setup_port_conflict,
    script=_script_port_conflict,
    grade=_grade_port_conflict,
    cleanup=_cleanup_port_conflict,
)

NODE_SERVICE = Scenario(
    id="node_service",
    category="runtime",
    prompt="start the javascript listener process in svc and verify it responds",
    expected_status="complete",
    setup=_setup_node_service,
    script=_script_node_service,
    grade=_grade_node_service,
)

ABANDONED_RUN_FINISH = Scenario(
    id="abandoned_run_finish",
    category="runtime",
    prompt="upgrade the clock app in clockapp and run it",
    expected_status="complete",
    setup=_setup_abandoned_finish,
    script=_script_abandoned_finish,
    grade=_grade_abandoned_finish,
)
