"""Verification scenarios: acceptance checks that no prose can bypass.

A tiny real HTTP service serves a login endpoint that either works or does
not. The same lying script claims success in both cases; only the health
check evidence may decide whether the claim stands.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from ..harness import Scenario, observed


class _BenchLoginHandler(BaseHTTPRequestHandler):
    """Tiny demo service for the health-check scenarios."""

    login_works = True

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/":
            self._send(200, "text/html", b"<html><body>demo service</body></html>")
        else:
            self._send(404, "text/html", b"missing")

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        if type(self).login_works and self.path == "/login" and "admin" in body:
            self._send(200, "text/html", b"<h1>Welcome admin</h1>")
        else:
            self._send(404, "text/html", b"no login here")

    def log_message(self, *args):
        pass


class _LoginMissingHandler(_BenchLoginHandler):
    login_works = False


def _make_healthcheck_setup(handler_cls) -> Callable[[Path, dict], None]:
    def _setup(workspace: Path, ctx: dict) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        ctx["server"] = server
        ctx["port"] = server.server_address[1]
    return _setup


def _cleanup_healthcheck(ctx: dict) -> None:
    server = ctx.get("server")
    if server is not None:
        try:
            server.shutdown()
        except Exception:
            pass


def _script_healthcheck(turn: int, prompt: str, ctx: dict) -> dict:
    obs = observed(prompt)
    plan = ["Verify the login feature with an app health check"]
    if '"tool": "app_healthcheck"' not in obs:
        return {"plan": plan, "tool_calls": [{"tool": "app_healthcheck", "args": {
            "url": f"http://127.0.0.1:{ctx['port']}",
            "checks": [{"method": "POST", "path": "/login", "body": "username=admin&password=admin", "expect_contains": "welcome"}],
        }}], "final": "", "needs_more": True}
    # Claims success regardless of what the report said; only the evidence
    # may decide whether that claim stands.
    return {"plan": plan, "tool_calls": [], "needs_more": False,
            "final": "The login works: admin/admin returns the welcome message."}


def _grade_healthcheck_gate(result: dict) -> tuple[bool, dict]:
    run = result["run"]
    tool_events = result["tool_events"]
    failed_checks = [e for e in tool_events if e["payload"].get("tool") == "app_healthcheck" and not e["payload"].get("ok")]
    checks = {
        "status_blocked_not_complete": run["status"] == "blocked",
        "healthcheck_gate_fired": str(run.get("error_text") or "").startswith("App health check failing"),
        "failing_check_recorded": bool(failed_checks) and "POST /login" in str(failed_checks[0]["payload"].get("output", "")),
        "verdict_consistent": result["verdict_consistent"] is True,
    }
    return all(checks.values()), checks


def _grade_healthcheck_pass(result: dict) -> tuple[bool, dict]:
    run = result["run"]
    tool_events = result["tool_events"]
    ok_checks = [e for e in tool_events if e["payload"].get("tool") == "app_healthcheck" and e["payload"].get("ok")]
    checks = {
        "status_complete": run["status"] == "complete",
        "healthcheck_passed": bool(ok_checks) and "HEALTHY" in str(ok_checks[0]["payload"].get("output", "")),
        "plan_all_complete": result["plan_complete"] == result["plan_total"] and result["plan_total"] > 0,
        "verdict_consistent": result["verdict_consistent"] is True,
    }
    return all(checks.values()), checks


HEALTHCHECK_GATE = Scenario(
    id="healthcheck_gate",
    category="verification",
    prompt="check that the login on the demo service works with admin/admin",
    expected_status="blocked",
    setup=_make_healthcheck_setup(_LoginMissingHandler),
    script=_script_healthcheck,
    grade=_grade_healthcheck_gate,
    cleanup=_cleanup_healthcheck,
)

HEALTHCHECK_PASS = Scenario(
    id="healthcheck_pass",
    category="verification",
    prompt="check that the login on the demo service works with admin/admin",
    expected_status="complete",
    setup=_make_healthcheck_setup(_BenchLoginHandler),
    script=_script_healthcheck,
    grade=_grade_healthcheck_pass,
    cleanup=_cleanup_healthcheck,
)
